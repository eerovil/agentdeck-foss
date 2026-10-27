"""The Runtime's ``ensure_delivery`` boundary: Poller machine delivery without the web (#2144).

The Poller host used to reach a worker through the web process, which then called this
runtime. A web deploy or restart therefore sat in the middle of every machine delivery. This is
the replacement: the Poller host calls the runtime over its host-local socket, and the runtime
does everything that delivery needs by itself.

It is one long-lived API, not a stepping stone. The Poller sends ``requirements`` and the
runtime chooses the provider account itself (#2146, ``delivery_scheduling``).

What it keeps exactly as it was:

* **Correlation (#2142).** ``work_id``, ``run_id`` and ``delivery_id`` arrive exactly as the
  engine saved them on the durable dispatch. They are checked but never recomputed, and a
  request without them is refused. This service never guesses a missing id; that fallback
  lives only on the host adapter's legacy edge.
* **At-most-once.** The delivery goes through the runtime's existing worker delivery, so the
  DeliveryReceipt is persisted before any provider write, and a retry under the same
  ``delivery_id`` replays that receipt. Nothing here writes to a provider on its own.
* **Bookkeeping the web route did (#1267, #2127, #2142).** The session token, the chat
  continuation fence, the card registration and the ``session_run_bindings`` row are written
  straight to the shared database. These are the same rows through the same store methods;
  the web follows them rather than writing them.

Pre-#2154 phase fields and the web-only context-maintenance ledger are deliberately not part of
this contract. The request model forbids unknown fields, so an old phase envelope cannot slip in.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chat_identity import ChatLinkConflict, published_chat_reference
from .delivery_scheduling import DeliveryRequirements, Placement
from .models import IssueRef
from .providers.delivery import DeliverResult
from .providers.instructions import SessionRole
from .work import work_id_for

log = logging.getLogger(__name__)

ENSURE_DELIVERY_CONTRACT = "ensure-delivery-v1"
ENSURE_DELIVERY_PATH = "/deliveries/ensure"
# Refusal: another Run of this Work may still be writing its workspace (#2159). Decided by the
# runtime inside its admission lock, once the target is chosen and before any provider write.
PRIOR_RUN_EXECUTING = "prior_run_executing"

# How old an unclaimed chat-successor reservation must be before it counts as dead. This is the
# same window the web's Continue fence uses (`web/chat_successor.LOST_RECHECKS`). The runtime
# cannot see the web's in-flight starts, so age is the only evidence it has (#1620).
STALE_SUCCESSOR_S = 90.0

_RESERVED = frozenset({"new", "reserved", "linked"})


class EnsureDeliveryRequest(BaseModel):
    """One correlated machine delivery, as the engine durably recorded it."""

    model_config = ConfigDict(extra="forbid")

    contract: Literal["ensure-delivery-v1"]
    delivery_id: str = Field(min_length=1, max_length=512)
    work_id: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=256)
    requirements: DeliveryRequirements
    worker_key: str = Field(min_length=1, max_length=512)
    message: str
    cwd: str | None = None
    fresh: bool = False
    role: SessionRole | None = None
    allow_over_budget: bool = False
    require_idle: bool = False
    require_active: bool = False
    issue_ref: str | None = Field(default=None, max_length=512)
    continues_session_url: str | None = Field(default=None, max_length=2048)
    command_artifacts: dict[str, Any] | None = None
    session_token: str | None = Field(default=None, min_length=16, max_length=512, repr=False)

    @model_validator(mode="after")
    def _correlation_is_canonical(self) -> EnsureDeliveryRequest:
        """Refuse a correlation that is not the engine's own, before any I/O.

        Same rules as the web route's model, minus its compatibility allowances. Both ids must
        be present, the Work must be spelled canonically, and it must be the named card's Work.
        """
        if self.work_id != self.work_id.strip() or self.run_id != self.run_id.strip():
            raise ValueError("work_id and run_id must be sent exactly as saved")
        kind, _, source = self.work_id.partition(":")
        source_ref = IssueRef.parse(source) if kind == "github_issue" else None
        if source_ref is None or work_id_for(source_ref) != self.work_id:
            raise ValueError("work_id is not a canonical Work id")
        ref = IssueRef.parse(self.issue_ref or "")
        if ref is not None and work_id_for(ref) != self.work_id:
            raise ValueError("work_id is not the canonical id of issue_ref")
        return self


# The runtime's scheduled delivery: choose, freeze and deliver under admission (#2146).
Schedule = Callable[
    [EnsureDeliveryRequest, dict[str, Any]], Awaitable[tuple[DeliverResult, Placement]]
]


def refusal(reason: str) -> DeliverResult:
    """A clean refusal: nothing reached any provider."""
    return DeliverResult(False, "rejected", reason=reason)


async def ensure_delivery(
    body: EnsureDeliveryRequest,
    *,
    db: Any,
    context_mode: str = "off",
    schedule: Schedule | None = None,
) -> tuple[DeliverResult, str | None, Placement | None]:
    """Deliver once and record what the delivery established.

    ``schedule`` is the runtime's own keyed-worker delivery to a target it chooses, and it owns
    the receipt fence. This function only adds what has to happen around it. Returns the result,
    the key of the session that took the delivery if one did, and the scheduling decision when
    the runtime made one.
    """
    if context_mode == "enforce":
        # Context maintenance still lives in the web process's ledger. A configured `enforce`
        # can compact or roll over a chat before the message goes in. Delivering without it
        # would quietly ignore that policy, so this refuses before any write and says why.
        return refusal("context_maintenance_unavailable"), None, None
    provider, label = "runtime", "scheduled"
    token = body.session_token or uuid.uuid4().hex
    source = continued_leg(db, body.continues_session_url)
    reservation = _reserve_successor(db, token, source) if source else None
    reserved = reservation in _RESERVED
    if source and not reserved:
        log.warning(
            "ensure_delivery to %s:%s named chat leg %s but could not fence its successor"
            " (%s); this turn runs as its own chat instead of continuing that one",
            provider,
            label,
            source,
            reservation,
        )
    fields: dict[str, Any] = {
        "key": body.worker_key,
        "message": body.message,
        "cwd": body.cwd,
        "fresh": body.fresh,
        "delivery_id": body.delivery_id,
        "client_action_id": body.delivery_id,
        "allow_over_budget": body.allow_over_budget,
        "require_idle": body.require_idle,
        "require_active": body.require_active,
        "delegated": True,
        "role": body.role,
        "session_token": token,
        "command_artifacts": body.command_artifacts,
        "work_id": body.work_id,
        "run_id": body.run_id,
    }
    placement: Placement | None = None
    if schedule is None:
        result = refusal("routing_unavailable")
    else:
        result, placement = await schedule(body, fields)
        if placement.decision != "refused" and placement.account:
            provider, label = placement.target
    log.info(
        "ensure_delivery %s scheduled: %s",
        body.delivery_id,
        placement.trace() if placement is not None else "no scheduler",
    )
    if not result.session_id:
        if reserved:
            _release(db, token)
        return result, None, placement
    session_key = f"{provider}:{label}:{result.session_id}"
    if reserved and session_key == source:
        # Resuming the very leg it continues is not a switch; a self-link is not a chain.
        _release(db, token)
    # Resuming some other session still binds on purpose (#3587). The worker key is per card,
    # so that is normally the card's own session whose first link failed: standing alone, the
    # store links it as the next leg and the split card is one chat again. The store refuses an
    # older leg, a leg of another chat, or a session with a successor; the fence is released.
    if not _bind_token(db, token, session_key) and reserved:
        # The session could not join the chain, so nothing will ever claim the fence.
        _release(db, token)
    _register_card(db, session_key, body.issue_ref)
    _bind_run(db, session_key, body.delivery_id, result)
    return result, session_key, placement


def continued_leg(db: Any, url: str | None) -> str | None:
    """The newest leg of the chat a published URL names, read from the durable chat store.

    The web resolves the chat's *working* leg from its live session scan, which this process
    does not have. The store only lets a successor follow the chain's newest leg, so the
    newest leg is the one a fence can hold. A URL naming nothing the store knows starts a
    fresh chat, as it does on the web.
    """
    reference = published_chat_reference(url or "")
    if reference is None:
        return None
    kind, value = reference
    try:
        if kind == "chat":
            by_id = getattr(db, "chat_by_id", None)
            chat = by_id(value) if by_id is not None else None
        else:
            for_session = getattr(db, "chat_for_session", None)
            chat = for_session(value) if for_session is not None else None
    except Exception:  # noqa: BLE001 -- chat grouping must never block the delivery
        log.exception("ensure_delivery could not read the chat store for %s", url)
        return None
    return chat.newest_session_key if chat is not None else None


def resumes_behind_its_chat(
    db: Any, url: str | None, token: str | None, worker_leg: str
) -> bool:
    """Would this delivery resume a leg its chat has already moved past (#2884)?

    A person can continue a Run's chat into a new leg on another worker. The Run's keyed
    worker still holds the old leg, so resuming it would pick the work up from a transcript
    that stops before the continuation, and the new turn could not join the chat after its
    newest leg. True only when this delivery itself holds the newest leg's successor fence
    and ``worker_leg`` is an older leg of that same chat; the caller then starts the worker
    fresh, and the fresh session joins the chat as its next leg.
    """
    if not token or not worker_leg:
        return False
    source = continued_leg(db, url)
    if not source or source == worker_leg:
        return False
    pending_for = getattr(db, "pending_chat_successor", None)
    for_session = getattr(db, "chat_for_session", None)
    if pending_for is None or for_session is None:
        return False
    try:
        pending = pending_for(source)
        chat = for_session(worker_leg)
    except Exception:  # noqa: BLE001 -- unreadable keeps the delivery as it was asked
        log.exception("ensure_delivery could not read the chat store for %s", worker_leg)
        return False
    if pending is None or pending[0] != token:
        return False
    return chat is not None and chat.newest_session_key == source


def _reserve_successor(db: Any, token: str, source: str) -> str | None:
    """Fence this chat's one continuation, stepping over a reservation nothing will lift."""
    reserve = getattr(db, "reserve_chat_successor", None)
    if reserve is None:
        return None
    reservation = reserve(token, source)
    if reservation != "conflict":
        return reservation
    pending_for = getattr(db, "pending_chat_successor", None)
    pending = pending_for(source) if pending_for is not None else None
    if pending is None:
        return reservation
    blocking_token, reserved_at = pending
    if reserved_at.tzinfo is None:
        reserved_at = reserved_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - reserved_at < timedelta(seconds=STALE_SUCCESSOR_S):
        return reservation
    _release(db, blocking_token)
    return reserve(token, source)


def _release(db: Any, token: str) -> None:
    release = getattr(db, "release_chat_successor", None)
    if release is None:
        return
    try:
        release(token)
    except Exception:  # noqa: BLE001 -- a stale fence is stepped over later, not fatal now
        log.exception("ensure_delivery could not release a chat successor reservation")


def _bind_token(db: Any, token: str, session_key: str) -> bool:
    """Bind the token and consume any successor fence, as the web's own binding does.

    False when the chat link was refused or the write failed: the session already belongs to
    another place in a chain (an older leg, say), so the fence this delivery reserved is still
    unclaimed. Releasing it only ever deletes an unclaimed fence.
    """
    record = getattr(db, "record_session_token", None)
    if record is None:
        return True
    try:
        record(token, session_key)
    except ChatLinkConflict as exc:
        log.warning("chat link refused for %s: %s — it stands as its own chat", session_key, exc)
        ensure = getattr(db, "ensure_session_chats", None)
        if ensure is not None:
            ensure((session_key,))
        return False
    except Exception:  # noqa: BLE001 -- the delivery already happened; keep its answer
        log.exception("ensure_delivery could not bind the session token for %s", session_key)
        return False
    return True


def _register_card(db: Any, session_key: str, issue_ref: str | None) -> None:
    """Record the card against the session that took it (#2127). Additive, never a guess."""
    ref = IssueRef.parse(issue_ref or "")
    if ref is None:
        if issue_ref:
            log.warning(
                "ensure_delivery named card %r, which is not an owner/repo#n reference;"
                " the session keeps no durable card relation",
                issue_ref,
            )
        return
    record = getattr(db, "record_session_issue", None)
    works = getattr(db, "record_works", None)
    try:
        if record is not None:
            record(session_key, ref)
        if works is not None:
            works((ref,))
    except Exception:  # noqa: BLE001 -- the delivery already happened; keep its answer
        log.exception("ensure_delivery could not register %s on %s", ref, session_key)


def _bind_run(db: Any, session_key: str, delivery_id: str, result: DeliverResult) -> None:
    """Record the session's part in the Run when the durable receipt proves it (#2142).

    ``run_participation`` and the pair beside it are what the runtime's delivery read back from
    the receipt. On a replay, that is the pair first frozen, never this request's.
    """
    if result.run_participation is not True or not (result.work_id and result.run_id):
        return
    bind = getattr(db, "bind_session_run", None)
    if bind is None:
        return
    try:
        bind(session_key, result.work_id, result.run_id, delivery_id=delivery_id)
    except Exception:  # noqa: BLE001 -- boot reconciliation restores it from the receipt
        log.exception("ensure_delivery could not bind %s to its Run", session_key)
