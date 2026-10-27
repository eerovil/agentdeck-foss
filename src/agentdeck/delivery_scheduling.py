"""Where one ``ensure_delivery`` runs: the Runtime's account selection (#2146).

The Poller decides what work should happen. This module decides where it runs. The Poller
sends ``requirements`` — the operator's candidate accounts in policy order, an avoid-account
hint, and usage targets — and the Runtime picks the provider account here, beside the
host-wide capacity check, under the same lock.

The selection rules are the ones the Poller host once applied itself (``poller_wake`` and
``runner.choose_account``, removed in #2150), moved rather than reinvented:

* a hard-quota circuit takes an account out of the running;
* a card with no account yet starts where the most budget is left per hour (#2757);
* an account about to lose unspent subscription budget moves to the front (#1965);
* otherwise the operator's order, with the card's warm account first (its last target);
* an account whose usage window will refuse a turn is passed over, and when every account
  would refuse, the one whose block clears first still gets the work (#1519, #1726);
* a card whose worker is still inside a turn goes back to that worker, and nowhere else (#2059);
* a revive's ``avoid_account`` outranks the warm account, unless nothing else is left (#802);
* a steer goes to the account already holding its Run.

The target is then frozen per ``delivery_id`` (``delivery_routes``) before the selected host is
called. A retry of that id reaches the same account before anything about current health or
affinity is read, so a delivery whose provider write may have started can never move.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .db import DeliveryRoute
from .planned_reset import plan_for
from .poller_engine import github as usage
from .poller_engine.stop import STALE_SECONDS
from .providers import account_health
from .providers.delivery import DeliverResult, delivery_participated

log = logging.getLogger(__name__)

# The provider a bare account label means. The Poller has always read `main` as Claude's.
DEFAULT_PROVIDER = "claude_code"

# Structured refusals. Each is decided before anything is written to any provider.
NO_CANDIDATE_ACCOUNTS = "no_candidate_accounts"
ALL_ACCOUNTS_HARD_INELIGIBLE = "all_accounts_hard_ineligible"
REQUIREMENTS_UNSATISFIED = "requirements_unsatisfied"
RUNTIME_PROVIDER_UNAVAILABLE = "runtime_provider_unavailable"
ROUTING_UNAVAILABLE = "routing_unavailable"


class DeliveryRequirements(BaseModel):
    """What the Poller needs from a target, never the target itself."""

    model_config = ConfigDict(extra="forbid")

    # The operator's accounts in policy order, spelled as the Overlay spells them.
    candidates: list[str] = Field(default_factory=list, max_length=64)
    avoid_account: str | None = Field(default=None, max_length=256)
    usage_targets: dict[str, float] = Field(default_factory=dict)


def target_of(account: str) -> tuple[str, str]:
    """``provider:label`` → both halves; a bare label is Claude's, as it always was."""
    provider, sep, label = account.partition(":")
    return (provider, label) if sep else (DEFAULT_PROVIDER, account)


@dataclass
class Placement:
    """One decision: a target, or a structured refusal, and the notes explaining it."""

    decision: str  # selected | frozen | run_holder | refused
    account: str = ""
    reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def target(self) -> tuple[str, str]:
        return target_of(self.account)

    def trace(self) -> dict[str, Any]:
        """The Run-trace view of this decision, returned with the delivery's answer."""
        provider, label = self.target if self.account else ("", "")
        return {
            "decision": self.decision,
            "account": self.account or None,
            "provider": provider or None,
            "label": label or None,
            "reason": self.reason,
            "notes": list(self.notes),
        }


def request_fingerprint(body: Any) -> str:
    """What makes two requests under one ``delivery_id`` the same logical delivery.

    The candidate policy is deliberately left out: an operator editing the Overlay between two
    retries must not turn the retry into a conflict, and a frozen target wins over policy anyway.
    The chat it continues and the card it registers are in: ``ensure_delivery`` links the
    replayed session to both, so a retry naming others is a different request (#2185 review).
    """
    payload = {
        "work_id": body.work_id,
        "run_id": body.run_id,
        "worker_key": body.worker_key,
        "message": body.message,
        "cwd": body.cwd,
        "fresh": body.fresh,
        "role": body.role,
        "allow_over_budget": body.allow_over_budget,
        "require_idle": body.require_idle,
        "require_active": body.require_active,
        "command_artifacts": body.command_artifacts,
        "continues_session_url": body.continues_session_url,
        "issue_ref": body.issue_ref,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _spelled(candidates: list[str], provider: str, label: str) -> str:
    """The candidate naming this target, in the Overlay's own spelling when it has one."""
    for name in candidates:
        if name and target_of(name) == (provider, label):
            return name
    return f"{provider}:{label}"


def _worker(host: Any, key: str) -> dict | None:
    try:
        workers = host.snapshot().get("workers") or {}
    except Exception:  # noqa: BLE001 -- an unreadable host says nothing, as the socket read did
        return None
    record = workers.get(key) if isinstance(workers, dict) else None
    return record if isinstance(record, dict) else None


def still_working(record: dict | None, *, now: float | None = None) -> bool:
    """Is this worker inside a turn right now (#2059, #2111)? Moved from the Poller host.

    A human-parked worker pins; a busy claim pins only while its activity clock is fresh; a
    dead process, a quiet record and no record at all never pin.
    """
    if not isinstance(record, dict) or record.get("live") is False:
        return False
    if record.get("pending_interaction"):
        return True
    if not (record.get("turn_active") or record.get("effective_activity")):
        return False
    evidence = max(
        float(record.get("last_activity_at") or 0.0),
        float(record.get("turn_open_at") or 0.0),
    )
    if evidence <= 0:
        return True
    moment = time.time() if now is None else now
    return (moment - evidence) <= STALE_SECONDS


def order_with_preference(accounts: list[str], preferred: str) -> list[str]:
    """The operator's order with the warm account first. Reordered, never filtered."""
    order = list(accounts)
    if not preferred or preferred not in order:
        return order
    return [preferred] + [name for name in order if name != preferred]


def _expiring_budget_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    planned_resets: Mapping[str, datetime] | None = None,
) -> list[str]:
    """An account about to waste the most subscription budget leads (#1965); order ties."""
    shortfalls = {
        name: usage.account_expiring_headroom(
            usage_cache,
            name,
            target_pct=usage_targets.get(name, usage.USAGE_TARGET_PCT),
            planned_reset=plan_for(planned_resets, name),
        )
        for name in order
    }
    if not any(shortfalls.values()):
        return order
    ranked = sorted(enumerate(order), key=lambda pair: (-shortfalls[pair[1]], pair[0]))
    promoted = [name for _, name in ranked]
    if promoted != order:
        leader = promoted[0]
        notes.append(
            f"{leader} is {shortfalls[leader]:.0f} points short of its target with its window "
            f"about to reset — offering it the work ahead of {order[0]}"
        )
    return promoted


# Points per hour within which two accounts count as equally behind, so the operator's order
# decides rather than the jitter between two usage readings.
PACE_TIE = 0.1


def _budget_pace_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    planned_resets: Mapping[str, datetime] | None = None,
) -> list[str]:
    """A new card starts on the account with the most budget left per hour (#2757).

    Only accounts of the leading account's provider are ranked, each keeping one of the slots
    those accounts held, so another provider's fallback never moves: Codex's meter reads 0%
    whatever it has spent. Any ranked account without a fresh reading leaves the order alone,
    because ignorance must not move work. Paces within ``PACE_TIE`` keep the operator's order.
    """
    provider = target_of(order[0])[0]
    slots = [index for index, name in enumerate(order) if target_of(name)[0] == provider]
    if len(slots) < 2:
        return order
    paces: dict[str, float] = {}
    for index in slots:
        name = order[index]
        pace = usage.account_budget_pace(
            usage_cache,
            name,
            target_pct=usage_targets.get(name, usage.USAGE_TARGET_PCT),
            planned_reset=plan_for(planned_resets, name),
        )
        if pace is None:
            return order
        paces[name] = pace
    ranked = sorted((order[index] for index in slots), key=lambda name: -paces[name])
    if paces[ranked[0]] - paces[order[0]] <= PACE_TIE:
        return order  # readings taken moments apart differ by a hair; that is not a reason
    promoted = list(order)
    for index, name in zip(slots, ranked, strict=True):
        promoted[index] = name
    if promoted[0] != order[0]:
        leader, before = promoted[0], order[0]
        notes.append(
            f"{leader} has {paces[leader]:.1f} points/hour of its budget left against "
            f"{before}'s {paces[before]:.1f} — starting the card there"
        )
    return promoted


# A five-hour window this full is about to stop the chat mid-turn (#3580).
FIVE_HOUR_NEAR_FULL_PCT = 90.0


def _five_hour_room_first(
    order: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
) -> list[str]:
    """A new card skips an account whose five-hour window is nearly full (#3580).

    The budget rankings ignore the five-hour window, so on their own they pile every new card
    onto one account until that window runs out and stops its chats mid-turn. Accounts of the
    leading account's provider at ``FIVE_HOUR_NEAR_FULL_PCT`` or more move behind those with
    room, each keeping their order. When none has room, or nothing reads, the order stands.
    An account its operator opted out with a target of 0 never counts as having room.
    """
    provider = target_of(order[0])[0]
    slots = [index for index, name in enumerate(order) if target_of(name)[0] == provider]
    if len(slots) < 2:
        return order
    now = datetime.now(UTC)
    full: dict[str, float] = {}
    for index in slots:
        name = order[index]
        for window, pct, resets in usage.account_window_readings(usage_cache, name, now=now):
            if window == "five_hour" and pct >= FIVE_HOUR_NEAR_FULL_PCT and resets > now:
                full[name] = pct
    roomy = [
        order[index]
        for index in slots
        if order[index] not in full
        and usage_targets.get(order[index], usage.USAGE_TARGET_PCT) > 0
    ]
    if not full or not roomy:
        return order
    promoted = list(order)
    ranked = roomy + [order[index] for index in slots if order[index] not in roomy]
    for index, name in zip(slots, ranked, strict=True):
        promoted[index] = name
    if promoted[0] != order[0]:
        before = order[0]
        why = (
            f"{before}'s five-hour window is {full[before]:.0f}% full"
            if before in full
            else f"{before} is opted out and {', '.join(full)} nearly full for five hours"
        )
        notes.append(f"{why} — starting the card on {promoted[0]} instead")
    return promoted


_NEVER_CLEARS = datetime.max.replace(tzinfo=UTC)


def choose_account(
    accounts: list[str],
    usage_cache: Path | None,
    usage_targets: Mapping[str, float],
    notes: list[str],
    *,
    cold: bool = False,
    planned_resets: Mapping[str, datetime] | None = None,
) -> str:
    """An eligible account, or ``""`` when every one has an open hard-quota circuit.

    ``cold`` is a card with no account of its own yet; its first account is chosen by the
    budget each account has left per hour (#2757) rather than by the operator's order alone.
    ``planned_resets`` is ``Db.planned_resets``: an account a person will reset soon has its
    budget end then, aimed at 100%, so it is offered the work first (#3379). A cold card
    then skips an account whose five-hour window is nearly full (#3580).
    """
    order = [name for name in accounts if name]
    if not order:
        return ""
    circuits = [(name, *account_health.refusal_detail(usage_cache, name)) for name in order]
    open_accounts = {name for name, reason, _ in circuits if reason is not None}
    if open_accounts:
        details = "; ".join(
            f"{name} ({reason})" for name, reason, _ in circuits if reason is not None
        )
        notes.append(f"skipping provider-ineligible accounts: {details}")
    order = [name for name in order if name not in open_accounts]
    if not order:
        notes.append("all configured accounts have open hard-quota circuits; withholding delivery")
        return ""
    if cold:
        order = _budget_pace_first(order, usage_cache, usage_targets, notes, planned_resets)
    order = _expiring_budget_first(order, usage_cache, usage_targets, notes, planned_resets)
    if cold:
        order = _five_hour_room_first(order, usage_cache, usage_targets, notes)
    refused: list[tuple[str, str, datetime | None]] = []
    for name in order:
        reason, clears_at = usage.account_refusal_detail(usage_cache, name)
        if not reason:
            if refused:
                skipped = "; ".join(f"{who} ({why})" for who, why, _ in refused)
                notes.append(f"skipping {skipped} — delivering to {name} instead")
            return name
        refused.append((name, reason, clears_at))
    chosen, _, chosen_clears = min(
        refused, key=lambda entry: entry[2] if entry[2] is not None else _NEVER_CLEARS
    )
    blocked = "; ".join(f"{who} ({why})" for who, why, _ in refused)
    why_chosen = (
        f"its block clears first, at {chosen_clears.isoformat()}"
        if chosen_clears is not None
        else "no refusal says when it clears, so the operator's order decides"
    )
    notes.append(
        f"every configured account will refuse a turn: {blocked} — delivering to "
        f"{chosen} anyway rather than withholding the work: {why_chosen}"
    )
    return chosen


@dataclass
class Selector:
    """The Runtime's view of its fleet, for one decision.

    ``hosts`` maps ``(provider, label)`` to the keyed-worker host of every account this Runtime
    can deliver to. ``db`` holds the routing records.
    """

    hosts: Mapping[tuple[str, str], Any]
    db: Any
    usage_cache: Path | None
    now: Callable[[], float] = time.time

    def place(self, body: Any, requirements: DeliveryRequirements) -> Placement:
        """Choose a target for a request with no frozen route yet."""
        if body.require_active:
            return self._run_holder(body, requirements)
        return self._select(body, requirements)

    def _run_holder(self, body: Any, requirements: DeliveryRequirements) -> Placement:
        """A steer reaches the worker already running its Run, whatever health says now.

        The Poller used to read the account off its own watch file. The Runtime knows it
        directly: the route it froze for that Run, or else the host whose worker for this key
        carries this Work's Run (a Run delivered before routes existed).
        """
        candidates = requirements.candidates
        route = self.db.latest_delivery_route(body.work_id, run_id=body.run_id)
        if route is not None:
            account = _spelled(candidates, route.provider, route.account)
            return Placement("run_holder", account, notes=[f"steering the Run on {account}"])
        for (provider, label), host in sorted(self.hosts.items()):
            record = _worker(host, body.worker_key)
            if (
                record is not None
                and record.get("work_id") == body.work_id
                and record.get("run_id") == body.run_id
            ):
                account = _spelled(candidates, provider, label)
                return Placement("run_holder", account, notes=[f"steering the Run on {account}"])
        return Placement(
            "refused",
            reason=REQUIREMENTS_UNSATISFIED,
            notes=[f"nothing on this runtime holds Run {body.run_id} to steer"],
        )

    def _warm_account(self, body: Any, candidates: list[str]) -> str:
        """The account the card last ran on, as a candidate name, or ``""``.

        The newest route this Runtime froze for the Work. Before any route exists — the first
        delivery after the cutover — the account whose worker for this key, recorded for this
        Work, was most recently active, which is where its session lineage lives.
        """
        route = self.db.latest_delivery_route(body.work_id)
        if route is not None:
            return _spelled(candidates, route.provider, route.account)
        best: tuple[float, str] | None = None
        for (provider, label), host in sorted(self.hosts.items()):
            record = _worker(host, body.worker_key)
            if record is None or record.get("work_id") != body.work_id:
                # The key is only a compatibility name; the Work is the identity (#2185 review).
                continue
            seen = max(
                float(record.get("last_result_at") or 0.0),
                float(record.get("last_activity_at") or 0.0),
                float(record.get("turn_open_at") or 0.0),
            )
            if best is None or seen > best[0]:
                best = (seen, _spelled(candidates, provider, label))
        return best[1] if best is not None else ""

    def _mid_turn(self, account: str, body: Any) -> bool:
        """Is this Work's worker on ``account`` inside a turn? Another Work's never pins.

        A record naming no Work (no correlated delivery yet) keeps the legacy by-key answer.
        """
        host = self.hosts.get(target_of(account))
        record = _worker(host, body.worker_key) if host is not None else None
        if record is not None and record.get("work_id") not in (None, body.work_id):
            return False
        return still_working(record, now=self.now())

    def _select(self, body: Any, requirements: DeliveryRequirements) -> Placement:
        notes: list[str] = []
        accounts = list(requirements.candidates)
        avoid = requirements.avoid_account or ""
        cold = False
        # Compared as targets: the Poller learns a refused account from the Run's outcome,
        # which spells it ``provider:label`` whatever the Overlay wrote (#2200).
        candidates = [
            name for name in accounts if not avoid or target_of(name) != target_of(avoid)
        ]
        if not candidates:
            candidates = list(accounts)
        else:
            preferred = self._warm_account(body, candidates)
            cold = not preferred
            candidates = order_with_preference(candidates, preferred)
            if preferred in candidates and self._mid_turn(preferred, body):
                # A second worker in the same worktree costs more than a cold start, so while the
                # card's worker is inside a turn this delivery has exactly one candidate (#2059).
                notes.append(
                    f"{preferred} is still mid-turn on this card — delivering there rather "
                    "than opening a second worker for it"
                )
                candidates = [preferred]
        selected = choose_account(
            candidates,
            self.usage_cache,
            requirements.usage_targets,
            notes,
            cold=cold,
            planned_resets=self.db.planned_resets(),
        )
        if not selected:
            reason = ALL_ACCOUNTS_HARD_INELIGIBLE if candidates else NO_CANDIDATE_ACCOUNTS
            return Placement("refused", reason=reason, notes=notes)
        return Placement("selected", selected, notes=notes)


DeliverTo = Callable[[str, str, Callable[[], bool]], Awaitable[DeliverResult]]
ReceiptOf = Callable[[str, str], "dict[str, Any] | None"]


def _refused(placement: Placement) -> tuple[DeliverResult, Placement]:
    return DeliverResult(False, "rejected", reason=placement.reason), placement


async def deliver_scheduled(
    body: Any,
    requirements: DeliveryRequirements,
    *,
    selector: Selector,
    deliver_to: DeliverTo,
    receipt_of: ReceiptOf,
) -> tuple[DeliverResult, Placement]:
    """Place one delivery, freeze its target, and hand it to that account's host.

    The caller holds host-wide admission for the whole call, so choosing a target, taking a
    capacity slot and freezing the route are one step. ``deliver_to(provider, label, freeze)``
    is the runtime's own admission-checked delivery; it calls ``freeze`` after admission and
    before the host, and sends nothing when ``freeze`` answers False.
    """
    db = selector.db
    fingerprint = request_fingerprint(body)
    try:
        frozen = db.delivery_route(body.delivery_id)
        if frozen is None:
            placement = selector.place(body, requirements)
    except Exception:  # noqa: BLE001 -- an unreadable route is never "no route"
        log.exception("delivery routing store unreadable for %s", body.delivery_id)
        return _refused(Placement("refused", reason=ROUTING_UNAVAILABLE))
    if frozen is not None:
        if (frozen.work_id, frozen.run_id) != (body.work_id, body.run_id):
            return _refused(
                Placement("refused", reason="delivery_correlation_conflict",
                          notes=["this delivery id is already routed for another Work or Run"])
            )
        if frozen.request_fingerprint != fingerprint or frozen.worker_key != body.worker_key:
            return _refused(
                Placement("refused", reason="delivery_id_conflict",
                          notes=["this delivery id is already routed for a different request"])
            )
        account = _spelled(requirements.candidates, frozen.provider, frozen.account)
        placement = Placement(
            "frozen", account, notes=[f"replaying the target frozen at {frozen.decided_at}"]
        )
    if placement.decision == "refused":
        return _refused(placement)
    provider, label = placement.target
    if (provider, label) not in selector.hosts:
        placement.notes.append(f"{placement.account} is not an account this runtime can deliver to")
        placement.reason = RUNTIME_PROVIDER_UNAVAILABLE
        return DeliverResult(False, "rejected", reason=RUNTIME_PROVIDER_UNAVAILABLE), placement

    froze = False

    def freeze() -> bool:
        nonlocal froze
        if frozen is not None:
            return True
        froze = db.freeze_delivery_route(
            DeliveryRoute(
                body.delivery_id,
                fingerprint,
                body.work_id,
                body.run_id,
                provider,
                label,
                body.worker_key,
                datetime.now(UTC).isoformat(),
                # The same judgement ``choose_account`` made, kept with the route so the Run's
                # outcome can tell a refused turn from a dead worker (#1519, #2200).
                usage.account_refusal(selector.usage_cache, placement.account),
            )
        )
        if not froze:
            placement.reason = ROUTING_UNAVAILABLE
        return froze

    result = await deliver_to(provider, label, freeze)
    if not result.accepted:
        placement.reason = result.reason
    if froze and _provably_unwritten(result, receipt_of(provider, label)):
        # The characterized release (#2146): this call froze the route, the host refused, and
        # neither its answer nor its durable receipt shows a provider write that may have
        # begun. A retry may then choose afresh. Anything short of that keeps the route.
        if db.release_delivery_route(body.delivery_id):
            placement.notes.append("route released: the refusal provably wrote nothing")
    return result, placement


def _provably_unwritten(result: DeliverResult, receipt: dict[str, Any] | None) -> bool:
    if result.accepted or result.provider_write_state != "not_started":
        return False
    return not delivery_participated(receipt if receipt is not None else vars(result))
