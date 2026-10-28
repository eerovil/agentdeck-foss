"""The provider-neutral keyed-worker seam and its at-most-once delivery receipts.

``KeyedWorkerHost`` declares the small interface both provider adapters expose to
the persistent runtime, so the neutral control path is typed rather than ``Any``
and the two adapters cannot silently drift apart.

The persistent runtime guarantees each keyed-worker delivery is applied at most
once, keyed by a stable ``delivery_id`` (AGENTS.md). This module owns that
guarantee as one value object over a worker's durable receipt dict: it hides the
fingerprint recipe, the prepared-vs-finalized record shapes, retry-horizon pruning,
and the uncertain-replay rule behind a narrow interface.

It WRAPS the durable dict rather than owning it, so the on-disk state format
(``WorkerRecord.deliveries``, serialized by the host) stays byte-identical across
runtime restarts and rollbacks. The caller persists the dict after
``prepare``/``finalize``/``forget`` — the persist-before-write ordering is the
caller's to control, since only it knows when the child write happens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..models import InjectResult
from .instructions import SessionRole

# A caller may retry the same delivery ID for this long after a confirmed
# outcome. Every settled receipt must remain replayable for the whole horizon:
# delivery volume is not evidence that an older request can no longer retry.
#
# Keep the historical TTL name as an alias because configuration and external
# embedders already import it. The old count cap is also retained as a
# compatibility constant, but it no longer shortens the retry horizon.
DELIVERY_RETRY_HORIZON_S = 24 * 3600.0
DELIVERY_RECEIPT_TTL_S = DELIVERY_RETRY_HORIZON_S
DELIVERY_RECEIPT_CAP = 4096


@dataclass
class DeliverResult:
    accepted: bool
    action: str  # spawned/revived/steered/queued/parked/released/stopped/rejected
    reason: str | None = None
    session_id: str | None = None
    provider_write_state: str = "not_started"
    successor_created: bool = False
    retryable_same_delivery: bool = False
    context_maintenance_status: str | None = None
    context_maintenance_reason: str | None = None
    context_rollover: bool = False
    delivery_stage: str | None = None
    provider_turn_id: str | None = None
    # The Work and Run the delivery belongs to, as durably recorded on its receipt (#2142).
    work_id: str | None = None
    run_id: str | None = None
    # Whether that receipt proves the session took part in the Run (`delivery_participated`).
    # Set only for a correlated delivery; None means nobody asked.
    run_participation: bool | None = None


_WRITE_MAY_HAVE_BEGUN = frozenset({"started", "unknown"})


def delivery_participated(outcome: Mapping[str, Any] | None) -> bool:
    """Did this delivery reach its session, as far as anything can prove? (#2142)

    The one rule for "a session took part in a Run". Every place that records or restores a
    ``session_run_bindings`` row asks it — the worker snapshot boot reconciliation reads, the
    runtime's answer, and the deck's fallback for an older runtime — so they cannot disagree.
    It reads a delivery receipt or a delivery result alike, because both carry the same fields:

    * still in flight (no ``action``): yes, unless it is ``prepared_before_write``, which
      provably never began its provider write;
    * a provider write that started or may have started: yes, whatever the answer says;
    * otherwise an accepted, non-rejected outcome: yes; a rejection: no.

    An uncertain write counts: the fence treats it as delivered, and so does this.
    """
    if not isinstance(outcome, Mapping):
        return False
    action = outcome.get("action")
    if not action:
        return outcome.get("delivery_stage") != "prepared_before_write"
    if outcome.get("provider_write_state") in _WRITE_MAY_HAVE_BEGUN:
        return True
    return outcome.get("accepted", True) is not False and action != "rejected"


@dataclass(frozen=True)
class DeliveryCorrelation:
    """Which Work and which Run one delivery belongs to (#2142).

    Machine metadata only: it is frozen onto the delivery receipt before any provider write,
    replayed with it, and never rendered into what the model reads. ``run_id`` is the poller
    generation that dispatched it; ``work_id`` is the source-derived id of #2140. It is not
    part of the request fingerprint, so a receipt written before it existed still matches.
    """

    work_id: str
    run_id: str

    @classmethod
    def of(cls, work_id: str | None, run_id: str | None) -> DeliveryCorrelation | None:
        """Both ids, or None: half a correlation is no correlation."""
        if work_id and run_id:
            return cls(str(work_id), str(run_id))
        return None

    @classmethod
    def from_receipt(cls, receipt: dict[str, Any] | None) -> DeliveryCorrelation | None:
        if not isinstance(receipt, dict):
            return None
        return cls.of(receipt.get("work_id"), receipt.get("run_id"))

    def stamp(self, receipt: dict[str, Any]) -> None:
        receipt["work_id"] = self.work_id
        receipt["run_id"] = self.run_id

    @staticmethod
    def snapshot_fields(receipts: DeliveryReceipts) -> dict[str, str | None]:
        """The worker snapshot's view of a key's current Work and Run (#2142).

        ``run_session_id`` is the session that delivery landed on, from its own receipt, which
        is not always the worker's current session: a later uncorrelated delivery may have
        moved the key since, and binding that session to this Run would be a false fact.
        """
        current = receipts.current_correlation()
        if current is None:
            return {
                "work_id": None,
                "run_id": None,
                "run_session_id": None,
                "run_delivery_id": None,
            }
        delivery_id, correlation, session_id = current
        return {
            "work_id": correlation.work_id,
            "run_id": correlation.run_id,
            "run_session_id": session_id,
            "run_delivery_id": delivery_id,
        }


@runtime_checkable
class KeyedWorkerHost(Protocol):
    """The keyed-worker seam the persistent runtime drives provider-neutrally.

    One host owns the deck's workers for one Provider Account and maps stable
    opaque keys onto durable sessions. ``ClaudeWorkerHost`` and
    ``CodexWorkerHost`` both satisfy this; ``WorkerDeliveries``, ``RestartNudges``
    and the ``/workers/accounts/{provider}/{label}/...`` routes reach a host
    only through the operations declared here.

    Deliberately narrow. Claude-only lifecycle controls (``interrupt``,
    ``answer``, ``stop_worker``, ``forget``, ``has_worker``,
    ``has_live_worker``) and the Codex-only ``thread_changed`` stay off the
    protocol: each is reached through its own provider-specific route, and a
    contract only one adapter can keep would be worse than no contract at all.

    ``delegated`` is keyword-only and REQUIRED. It resolves (through
    ``resolve_session_role``) to the trust level deciding whether a session may
    spawn sub-agents, so no caller may inherit it from an adapter's own default
    — the two adapters previously defaulted it to opposite values.
    """

    async def deliver(
        self,
        key: str,
        message: str,
        *,
        cwd: str | None = None,
        fresh: bool = False,
        images: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        delivery_id: str | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        require_active: bool = False,
        expected_last_result_at: float | None = None,
        expected_session_id: str | None = None,
        context_maintenance: bool = False,
        context_rollover: bool = False,
        delegated: bool,
        role: SessionRole | None = None,
        session_token: str | None = None,
        command_artifacts: dict[str, Any] | None = None,
        correlation: DeliveryCorrelation | None = None,
        automated: bool = False,
    ) -> DeliverResult:
        """Idempotent keyed delivery: steer if live, revive if finished, else spawn.

        ``correlation`` rides on the delivery receipt only (#2142): it changes nothing
        about what is delivered, to whom, or whether.

        ``automated`` says no person sent this message — a revival, a restart nudge,
        a job notice — so a host with an account-local cap holds even a person's own
        chat to it (#3009). A host with no such cap ignores it.
        """
        ...

    def snapshot(self) -> dict[str, Any]:
        """Normalized worker state for the web side, keyed by worker key."""
        ...

    async def release_worker(self, key: str) -> DeliverResult:
        """Terminate and forget a key atomically; safe to retry after success."""
        ...

    async def park_worker(self, key: str) -> DeliverResult:
        """End this key's running execution and keep its session; idempotent (#2159)."""
        ...

    def find_key_by_session(self, session_id: str) -> str | None:
        """The worker key whose session lineage matches, if any."""
        ...

    def worker_role(self, key: str) -> SessionRole | None:
        """The role this key's worker was last delivered under."""
        ...

    def delivery_receipt(self, key: str, delivery_id: str) -> dict[str, Any] | None:
        """Read one durable receipt for reconciliation without writing."""
        ...

    async def resolve_context_delivery(
        self, key: str, delivery_id: str, choice: str, rationale: str
    ) -> DeliverResult:
        """Settle one uncertain context receipt without provider I/O."""
        ...


class DeliveryReceipts:
    """The at-most-once fence for one worker key, over its durable receipt dict."""

    def __init__(self, store: dict[str, dict[str, Any]]) -> None:
        self._store = store

    @staticmethod
    def fingerprint(
        message: str,
        *,
        cwd: str | None,
        fresh: bool,
        images: list[str],
        model: str | None,
        effort: str | None,
        permission_mode: str | None,
        allow_over_budget: bool = False,
        target: str | None = None,
        operation: str | None = None,
        require_idle: bool = False,
        require_active: bool = False,
        command_artifacts: dict[str, Any] | None = None,
        fork: str | None = None,
    ) -> str:
        payload_data = {
            "message": message,
            "cwd": cwd,
            "fresh": fresh,
            # Uploaded images land at a fresh random path on every request
            # (uploads.py: mkdtemp + token_hex), so the *paths* differ across a
            # legitimate retry of the same logical delivery and would falsely
            # trip delivery_id_conflict. Fingerprint the stable count instead.
            "images": len(images),
            "model": model,
            "permission_mode": permission_mode,
        }
        # Keep the legacy fingerprint byte-identical when effort is absent so
        # receipts written before this field existed still deduplicate safely.
        if effort is not None:
            payload_data["effort"] = effort
        if allow_over_budget:
            payload_data["allow_over_budget"] = True
        # No role is part of the fingerprint: every role keeps the pre-role one, so
        # receipts written before roles existed still deduplicate. The one role that
        # was fenced here, the poller's ``kanban``, went in #3741.
        if require_idle:
            payload_data["require_idle"] = True
        if require_active:
            payload_data["require_active"] = True
        if target is not None:
            payload_data["target"] = target
        if operation is not None:
            payload_data["operation"] = operation
        if command_artifacts:
            payload_data["command_artifacts"] = command_artifacts
        if fork is not None:
            # A side chat cut at another line is a different start (#2485).
            payload_data["fork"] = fork
        payload = json.dumps(
            payload_data,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def lookup(
        self,
        delivery_id: str | None,
        fingerprint: str,
        *,
        live_session_id: str | None,
        correlation: DeliveryCorrelation | None = None,
    ) -> DeliverResult | None:
        """The cached outcome for ``delivery_id``, or None to deliver normally.

        None → no id or no receipt. A fingerprint mismatch is a
        ``delivery_id_conflict`` rejection. A finalized receipt (carries an
        ``action``) replays its original result. A prepared-only receipt means a
        prior attempt persisted the fence and then wrote — or was killed around
        the write — without confirming; at-most-once forbids writing again, so it
        surfaces as ``uncertain``, preferring the live session id (frozen None for
        a fresh spawn) so the replay still points at a resumable session.

        Correlation (#2142) is checked after the fingerprint and never enters it. A
        receipt already naming another Work or Run refuses as
        ``delivery_correlation_conflict`` before anything can reach the provider. A
        receipt written before correlation existed is given one by
        ``adopt_correlation``, which the host calls — and persists — first. Every
        replay carries the correlation the receipt holds, not the caller's.
        """
        if not delivery_id:
            return None
        receipt = self._store.get(delivery_id)
        if receipt is None:
            return None
        if receipt.get("fingerprint") != fingerprint:
            return DeliverResult(False, "rejected", reason="delivery_id_conflict")
        stored = DeliveryCorrelation.from_receipt(receipt)
        if correlation is not None and stored is not None and stored != correlation:
            return DeliverResult(False, "rejected", reason="delivery_correlation_conflict")
        action = receipt.get("action")
        if action:
            return DeliverResult(
                bool(receipt.get("accepted", True)),
                str(action),
                reason=receipt.get("reason"),
                session_id=receipt.get("session_id"),
                **self._metadata(receipt),
            )
        # A new staged receipt proves the provider write call never began. Returning
        # ``None`` permits only this same id/fingerprint to retry its frozen payload.
        # Legacy prepared receipts have no stage and remain uncertain/fenced.
        if receipt.get("delivery_stage") == "prepared_before_write":
            return None
        return DeliverResult(
            True,
            "uncertain",
            session_id=live_session_id or receipt.get("session_id"),
            provider_write_state="unknown",
            **self._metadata(receipt, include_write_state=False),
        )

    def adopt_correlation(
        self,
        delivery_id: str | None,
        fingerprint: str,
        correlation: DeliveryCorrelation | None,
    ) -> bool:
        """Give a receipt written before #2142 the incoming correlation, without resending.

        Only the same request (same fingerprint) adopts, and only a receipt holding no
        correlation of its own. True means the store changed and the caller MUST persist it
        before answering, or a restart forgets which Work and Run the delivery belonged to.
        """
        if not delivery_id or correlation is None:
            return False
        receipt = self._store.get(delivery_id)
        if (
            receipt is None
            or receipt.get("fingerprint") != fingerprint
            or DeliveryCorrelation.from_receipt(receipt) is not None
        ):
            return False
        correlation.stamp(receipt)
        return True

    @staticmethod
    def _metadata(
        receipt: dict[str, Any], *, include_write_state: bool = True
    ) -> dict[str, Any]:
        fields = (
            "successor_created",
            "retryable_same_delivery",
            "context_maintenance_status",
            "context_maintenance_reason",
            "context_rollover",
            "delivery_stage",
            "provider_turn_id",
            "work_id",
            "run_id",
        )
        result = {name: receipt[name] for name in fields if name in receipt}
        if include_write_state and "provider_write_state" in receipt:
            result["provider_write_state"] = receipt["provider_write_state"]
        return result

    def prepare(
        self,
        delivery_id: str | None,
        fingerprint: str,
        *,
        session_id: str | None,
        correlation: DeliveryCorrelation | None = None,
    ) -> None:
        """Arm the fence with a durable prepared receipt. The caller MUST persist
        the store before writing to the child, so a crash in the write→confirm
        window replays as uncertain instead of re-delivering. The correlation is
        frozen here, with the fence, so it is durable before provider I/O (#2142)."""
        if not delivery_id:
            return
        receipt = {
            "fingerprint": fingerprint,
            "session_id": session_id,
            "at": time.time(),
        }
        # A same-id retry of a receipt that provably wrote nothing keeps what it was
        # already frozen under unless the caller names its own.
        frozen = correlation or DeliveryCorrelation.from_receipt(self._store.get(delivery_id))
        if frozen is not None:
            frozen.stamp(receipt)
        self._store[delivery_id] = receipt
        self._prune()

    def begin_write(
        self,
        delivery_id: str | None,
        fingerprint: str,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Persist the last fence before provider I/O may begin."""
        if not delivery_id:
            return False
        receipt = self._store.get(delivery_id)
        if (
            receipt is None
            or receipt.get("fingerprint") != fingerprint
            or receipt.get("delivery_stage") != "prepared_before_write"
        ):
            return False
        receipt["delivery_stage"] = "write_may_have_started"
        if session_id is not None:
            receipt["session_id"] = session_id
        receipt["at"] = time.time()
        return True

    def identify_turn(
        self,
        delivery_id: str | None,
        fingerprint: str,
        turn_id: str,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Record exact provider acceptance before the caller returns it."""
        if not delivery_id or not turn_id:
            return False
        receipt = self._store.get(delivery_id)
        if (
            receipt is None
            or receipt.get("fingerprint") != fingerprint
            or "delivery_stage" not in receipt
        ):
            return False
        receipt.update(
            delivery_stage="provider_turn_identified",
            provider_turn_id=turn_id,
            at=time.time(),
        )
        if session_id is not None:
            receipt["session_id"] = session_id
        return True

    def finalize(
        self, delivery_id: str | None, fingerprint: str, result: DeliverResult
    ) -> None:
        """Clear the fence into a replayable finalized receipt. Caller persists."""
        if not delivery_id:
            return
        previous = self._store.get(delivery_id, {})
        receipt: dict[str, Any] = {
            "fingerprint": fingerprint,
            "action": result.action,
            "session_id": result.session_id,
            "at": time.time(),
        }
        for name in (
            "delivery_stage",
            "provider_turn_id",
            "work_id",
            "run_id",
        ):
            if name in previous:
                receipt[name] = previous[name]
        if result.delivery_stage is not None:
            receipt["delivery_stage"] = result.delivery_stage
        optional = {
            "accepted": result.accepted if not result.accepted else None,
            "reason": result.reason,
            "provider_write_state": (
                result.provider_write_state
                if result.provider_write_state != "not_started"
                else None
            ),
            "successor_created": result.successor_created or None,
            "retryable_same_delivery": result.retryable_same_delivery or None,
            "context_maintenance_status": result.context_maintenance_status,
            "context_maintenance_reason": result.context_maintenance_reason,
            "context_rollover": result.context_rollover or None,
            "provider_turn_id": result.provider_turn_id,
        }
        receipt.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        self._store[delivery_id] = receipt
        self._prune()

    def forget(self, delivery_id: str | None, *, write_started: bool) -> bool:
        """Drop a prepared receipt whose write CLEANLY failed (nothing sent), so
        the same id may be retried. Refuses — keeps the fence, returns False —
        when ``write_started`` is True: an uncertain write must never be re-armed
        as a fresh send. Returns True when a receipt was dropped (caller persists).
        """
        if write_started or not delivery_id:
            return False
        return self._store.pop(delivery_id, None) is not None

    def current_correlation(
        self,
    ) -> tuple[str, DeliveryCorrelation, str | None] | None:
        """This key's most recent correlated delivery still on record, as
        ``(delivery_id, correlation, session_id)``."""
        latest: tuple[float, str, DeliveryCorrelation, str | None] | None = None
        for delivery_id, receipt in self._store.items():
            correlation = DeliveryCorrelation.from_receipt(receipt)
            if correlation is None or not delivery_participated(receipt):
                continue
            at = float(receipt.get("at") or 0.0)
            if latest is None or at >= latest[0]:
                session_id = receipt.get("session_id")
                latest = (at, delivery_id, correlation, session_id or None)
        return latest[1:] if latest is not None else None

    def resolve(
        self,
        delivery_id: str,
        choice: str,
        rationale: str,
        *,
        session_id: str | None,
    ) -> DeliverResult:
        """Audit and settle a staged receipt without touching the provider."""
        receipt = self._store.get(delivery_id)
        if receipt is None or not receipt.get("phase_delivery_id"):
            return DeliverResult(False, "rejected", reason="context_delivery_not_found")
        previous_choice = receipt.get("resolution_choice")
        previous_rationale = receipt.get("resolution_rationale")
        if previous_choice is not None:
            if previous_choice != choice or previous_rationale != rationale:
                return DeliverResult(False, "rejected", reason="resolution_conflict")
            return DeliverResult(True, "resolved", session_id=session_id)
        if receipt.get("action") or choice not in {
            "accepted",
            "not_written",
            "abandoned",
        }:
            return DeliverResult(False, "rejected", reason="resolution_conflict")
        receipt.update(
            resolution_choice=choice,
            resolution_rationale=rationale,
            resolved_at=time.time(),
            at=time.time(),
        )
        if choice == "accepted":
            receipt.update(action="accepted", delivery_stage="provider_turn_completed")
        elif choice == "abandoned":
            receipt.update(
                action="rejected", accepted=False, reason="operator_abandoned"
            )
        else:
            receipt["delivery_stage"] = "prepared_before_write"
        return DeliverResult(True, "resolved", session_id=session_id)

    def _prune(self) -> None:
        """Expire settled history only after the retry horizon has passed.

        A response can be finalized yet still report uncertainty or a required
        same-delivery retry. Keep those records indefinitely until reconciliation.
        Confirmed results are disposable only after the full retry horizon: a
        burst of newer deliveries must never turn an otherwise valid retry into a
        fresh provider write.
        """
        now = time.time()
        for did, receipt in tuple(self._store.items()):
            if (
                receipt.get("action")
                and receipt["action"] != "uncertain"
                and receipt.get("provider_write_state") != "unknown"
                and not receipt.get("retryable_same_delivery")
                and now - float(receipt.get("at", 0.0)) > DELIVERY_RETRY_HORIZON_S
            ):
                self._store.pop(did, None)


class SessionDeliveryFence:
    """At-most-once fencing for the writes that reach a session with no worker key.

    A thread or agent the deck started from the dashboard has no keyed worker, so
    ``KeyedWorkerHost``'s receipt store does not cover it — yet the restart nudge
    still has to be at-most-once, or one uncertain write becomes a duplicate user
    message. Every app-server runtime needs exactly that guarantee, so it lives
    here once and is constructed per provider rather than copied per provider.

    Owns its own durable file (one per provider) and one lock per session, and
    persists after ``prepare``/``forget``/``finalize`` so a crash inside the
    write→confirm window replays as ``uncertain`` instead of re-delivering.
    """

    def __init__(
        self,
        path: Path,
        *,
        budget_exhausted: Callable[[str, str], bool] | None = None,
    ) -> None:
        # Retired in #1497. The argument remains compatible for callers
        # upgrading independently, but no delivery is refused for spend.
        del budget_exhausted
        self._path = path
        self._accounts: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(accounts, dict):
            return
        for label, sessions in accounts.items():
            if not isinstance(label, str) or not isinstance(sessions, dict):
                continue
            valid = {
                session_id: receipts
                for session_id, receipts in sessions.items()
                if isinstance(session_id, str) and isinstance(receipts, dict)
            }
            if valid:
                self._accounts[label] = valid

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"accounts": self._accounts}, indent=1))
        temporary.replace(self._path)

    async def deliver(
        self,
        label: str,
        session_id: str,
        message: str,
        *,
        images: list[Path],
        effort: str | None = None,
        operation: str,
        delivery_id: str | None,
        send: Callable[[], Awaitable[InjectResult]],
    ) -> DeliverResult:
        """Fence one session-addressed write by its client action id."""
        action = "queued" if operation == "queue" else "steered"
        if not delivery_id:
            result = await send()
            return DeliverResult(
                result.accepted,
                action if result.accepted else "rejected",
                reason=result.reason,
                session_id=session_id if result.accepted else result.session_id,
            )

        fingerprint = DeliveryReceipts.fingerprint(
            message,
            cwd=None,
            fresh=False,
            images=[str(path) for path in images],
            model=None,
            effort=effort,
            permission_mode=None,
            target=session_id,
            operation=operation,
        )
        lock = self._locks.setdefault((label, session_id), asyncio.Lock())
        async with lock:
            store = self._accounts.setdefault(label, {}).setdefault(session_id, {})
            receipts = DeliveryReceipts(store)
            cached = receipts.lookup(
                delivery_id, fingerprint, live_session_id=session_id
            )
            if cached is not None:
                return cached
            receipts.prepare(delivery_id, fingerprint, session_id=session_id)
            self._save()
            try:
                result = await send()
            except Exception:  # the child may have accepted before transport failed
                return DeliverResult(True, "uncertain", session_id=session_id)
            if not result.accepted:
                receipts.forget(delivery_id, write_started=False)
                self._save()
                return DeliverResult(
                    False,
                    "rejected",
                    reason=result.reason,
                    session_id=result.session_id,
                )

            delivered = DeliverResult(True, action, session_id=session_id)
            receipts.finalize(delivery_id, fingerprint, delivered)
            self._save()
            return delivered
