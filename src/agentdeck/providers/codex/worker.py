"""Durable opaque-key workers backed by AgentDeck-owned Codex threads."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ...models import (
    RUNTIME_BOOTED_AT,
    SNAPSHOT_BOOTED_AT_KEY,
    SNAPSHOT_CAPABILITIES_KEY,
    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
    SNAPSHOT_FORK_V1,
    SNAPSHOT_RELOCATE_V1,
    WORKER_SNAPSHOT_CAPABILITIES,
    Account,
)
from ..delivery import DeliverResult, DeliveryCorrelation, DeliveryReceipts
from ..instructions import (
    SessionRole,
    agentdeck_session_instructions,
    resolve_session_role,
    session_guide_fingerprint,
    stored_session_role,
    wrap_session_guide,
)
from .appserver import CodexAppServer
from .diagnostics import redact_diagnostic, redacted_exc_info

log = logging.getLogger(__name__)

# How often a worker's activity clock is written to disk while its turn is busy (#1400).
_ACTIVITY_SAVE_S = 30.0


@dataclass
class CodexWorkerRecord:
    key: str
    cwd: str
    session_id: str | None = None
    last_delivery_at: float = 0.0
    last_result_at: float = 0.0
    last_result_subtype: str | None = None
    # Why the account refused the turn `last_result_at` names, when it did — the one failure
    # cause that means no work was attempted rather than that work went wrong (#1519). Empty
    # for every other ending, and absent from every state file written before it existed.
    last_result_refusal: str = ""
    # When this thread last received input or produced any output, which is the only thing
    # that separates a wedged worker from a working one (#1400). Durable, because a runtime
    # restart is exactly when the in-memory evidence of a wedge is lost: the app-server keeps
    # owning the thread, its open turn is forgotten, and nothing left says how long ago it
    # stopped. Zero means never observed, and is read as unknown rather than as silence.
    last_activity_at: float = 0.0
    delegated: bool = True
    role: SessionRole | None = None
    session_token: str | None = None
    model: str | None = None
    effort: str | None = None
    command_artifacts: dict[str, Any] | None = None
    # Which guide text this thread was actually given. Codex takes
    # `developerInstructions` at `thread/start` and offers no verb to replace
    # them — `thread/resume` accepts history, path, roots, permissions, turn
    # paging and permission profile, and nothing else — so without this a thread
    # keeps whatever `providers/instructions.py` said on the day it was created.
    # None means "never told", which is what every record written before this
    # field existed says, so those threads refresh on their next turn.
    guide_fingerprint: str | None = None
    deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_context_delivery_id: str | None = None
    pending_context_operation: str | None = None
    pending_context_source_session_id: str | None = None
    pending_context_state: str | None = None
    pending_context_pre_last_result_at: float | None = None
    pending_context_post_last_result_at: float | None = None
    pending_context_maintenance_status: str | None = None
    pending_context_maintenance_reason: str | None = None

    @property
    def receipts(self) -> DeliveryReceipts:
        return DeliveryReceipts(self.deliveries)


class CodexWorkerHost:
    """Map stable worker keys onto persistent Codex threads for one account.

    The poller sees the same small keyed-worker interface for Claude and Codex —
    declared as ``KeyedWorkerHost`` in ``providers/delivery.py``. This adapter owns
    everything provider-specific behind that seam: thread creation, active steering,
    idle follow-ups, durable key lineage, and at-most-once delivery receipts.
    """

    def __init__(
        self,
        account: Account,
        client: CodexAppServer,
        *,
        state_dir: Path,
        usage_cache_dir: Path | None = None,
        bind_session_token: Callable[[str, str], None] | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        del usage_cache_dir
        self.account = account
        self.client = client
        self._state_path = Path(state_dir) / f"{account.label}.json"
        self._bind_session_token = bind_session_token or (lambda _token, _key: None)
        self._artifact_root = artifact_root or state_dir / "command-artifacts"
        self._records: dict[str, CodexWorkerRecord] = {}
        # Told that the records were saved, so the runtime's change feed can compare
        # and tell the web (#2865). Thread events reach the feed from the app-server.
        self.on_state_change: Callable[[], None] = lambda: None
        self._key_locks: dict[str, asyncio.Lock] = {}
        # What each key's activity clock was when it was last written to disk. The clock
        # itself moves on every app-server notification, which is far too often to persist
        # each time; this is what makes the throttle below a real one rather than a value
        # that always looks fresh enough to skip the write.
        self._activity_saved: dict[str, float] = {}
        self._load_state()
        self._activity_saved = {
            key: record.last_activity_at for key, record in self._records.items()
        }
        for record in self._records.values():
            if record.session_token and record.session_id:
                self._bind_session_token(
                    record.session_token, f"{self.account.key}:{record.session_id}"
                )
                self._publish_artifact_context(record)
        self.client.adopt_owned(
            {record.session_id for record in self._records.values() if record.session_id}
        )

    @staticmethod
    def _publish_artifact_context(record: CodexWorkerRecord) -> bool:
        """Whether this record's command-artifact policy can be honoured.

        Only the poller's ``kanban`` workers ever had a capture context published; that
        role went in #3741, so an ``enforce`` policy, which needs one, is refused.
        """
        return str((record.command_artifacts or {}).get("mode") or "off") != "enforce"

    def _key_lock(self, key: str) -> asyncio.Lock:
        return self._key_locks.setdefault(key, asyncio.Lock())

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        for item in raw.get("workers", []):
            try:
                record = CodexWorkerRecord(**item)
            except TypeError:
                continue
            # A thread the poller started under its retired ``kanban`` role (#3741).
            record.role = stored_session_role(record.role)
            self._records[record.key] = record

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workers": [vars(record) for record in self._records.values()]}
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=1))
        temporary.replace(self._state_path)
        self.on_state_change()

    def _sync_result(self, record: CodexWorkerRecord) -> None:
        if not record.session_id:
            return
        result = self.client.last_result(record.session_id)
        if result is None:
            return
        # Tolerated rather than unpacked positionally: `snapshot()` calls this for every
        # record, so one client returning the older two-value shape would take down the
        # whole account's snapshot rather than one field of one worker.
        at, subtype, refusal = (*result, "")[:3]
        if at > record.last_result_at:
            record.last_result_at = at
            record.last_result_subtype = subtype
            # Overwritten every turn, including with "": it describes the turn named by
            # `last_result_at` and must never outlive it onto the next one.
            record.last_result_refusal = refusal
            self._save_state()

    def _note_activity(self, record: CodexWorkerRecord) -> None:
        """Move this worker's activity clock, persisting it at most every ``_ACTIVITY_SAVE_S``.

        The clock is in memory always and on disk often enough: it is read against a window
        of hours, so a stamp up to half a minute behind changes no verdict, while writing the
        whole state file on every notification of a busy turn would be hundreds of writes.
        """
        now = time.time()
        record.last_activity_at = now
        if now - self._activity_saved.get(record.key, 0.0) >= _ACTIVITY_SAVE_S:
            self._activity_saved[record.key] = now
            self._save_state()

    def thread_changed(self, thread_id: str) -> None:
        """Persist a completed result immediately when the app-server reports it.

        Every notification the app-server routes here is also proof this thread did
        something, which is the fact a wedged thread stops producing (#1400).
        """
        for record in self._records.values():
            if record.session_id == thread_id:
                self._note_activity(record)
                self._sync_result(record)
                return

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
        """Idempotent work delivery onto this key's durable Codex thread.

        ``automated`` is accepted for the shared contract and ignored: Codex workers
        hold no account-local admission slot for it to bind.

        ``delegated`` has no default here (see ``KeyedWorkerHost``): it decides the
        session's trust level, so every caller must state it.
        """
        role = resolve_session_role(delegated=delegated, role=role)
        fingerprint = DeliveryReceipts.fingerprint(
            message,
            cwd=cwd,
            fresh=fresh,
            images=images or [],
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            allow_over_budget=allow_over_budget,
            operation=(
                "context_rollover"
                if context_rollover
                else "context_maintenance" if context_maintenance else None
            ),
            require_idle=require_idle,
            require_active=require_active,
            command_artifacts=command_artifacts,
        )
        async with self._key_lock(key):
            if (context_maintenance or context_rollover) and not delivery_id:
                return DeliverResult(
                    False, "rejected", reason="context_delivery_id_required"
                )
            if context_maintenance and (not require_idle or fresh or context_rollover):
                return DeliverResult(
                    False,
                    "rejected",
                    reason="context_maintenance_requires_idle",
                )
            if context_rollover and (
                not require_idle or not fresh or require_active or context_maintenance
            ):
                return DeliverResult(False, "rejected", reason="context_rollover_invalid")
            record = self._records.get(key)
            previous_artifact_mode = str(
                ((record.command_artifacts if record else None) or {}).get("mode") or "off"
            )
            context_operation = (
                "rollover" if context_rollover else "compact" if context_maintenance else None
            )
            retrying_context = bool(
                record is not None
                and delivery_id
                and record.pending_context_delivery_id == delivery_id
                and record.pending_context_operation == context_operation
            )
            pending_rollover_target = bool(
                retrying_context
                and context_rollover
                and record is not None
                and record.session_id != record.pending_context_source_session_id
            )
            if record is not None and record.receipts.adopt_correlation(
                delivery_id, fingerprint, correlation
            ):
                # A receipt from before #2142 takes this delivery's Work and Run; durable
                # before the replay below answers with it.
                self._save_state()
            cached = (
                record.receipts.lookup(
                    delivery_id,
                    fingerprint,
                    live_session_id=record.session_id,
                    correlation=correlation,
                )
                if record is not None
                else None
            )
            if cached is not None:
                return cached
            if record is not None and record.pending_context_delivery_id:
                if not retrying_context:
                    return DeliverResult(
                        False, "rejected", reason="context_operation_pending"
                    )
            if expected_session_id is not None and (
                record is None
                or (
                    record.session_id != expected_session_id
                    and not (
                        retrying_context
                        and record.pending_context_source_session_id
                        == expected_session_id
                    )
                )
            ):
                return DeliverResult(False, "rejected", reason="worker_changed")
            if (
                expected_last_result_at is not None
                and not retrying_context
                and (record.last_result_at if record is not None else 0.0)
                != expected_last_result_at
            ):
                return DeliverResult(False, "rejected", reason="worker_changed")
            if (
                require_active
                and (
                    record is None
                    or record.session_id is None
                    or self.client.active_turn(record.session_id) is None
                )
            ):
                return DeliverResult(False, "rejected", reason="worker_inactive")
            if (
                require_idle
                and record is not None
                and record.session_id is not None
                and self.client.active_turn(record.session_id) is not None
            ):
                if (
                    retrying_context
                    and context_maintenance
                    and record.pending_context_state == "compacting"
                ):
                    return DeliverResult(
                        False,
                        "rejected",
                        reason="context_operation_pending",
                        session_id=record.session_id,
                        retryable_same_delivery=True,
                        context_maintenance_status="unknown",
                    )
                return DeliverResult(False, "rejected", reason="active_turn")
            artifact_mode = str((command_artifacts or {}).get("mode") or "off")
            if (
                record is not None
                and record.session_id is not None
                and artifact_mode != previous_artifact_mode
            ):
                if self.client.active_turn(record.session_id) is not None:
                    return DeliverResult(False, "rejected", reason="active_turn")
                configured = await self.client.set_command_artifacts(
                    record.session_id,
                    worker_key=record.key,
                    session_token=record.session_token or "",
                    artifact_root=self._artifact_root,
                    artifact_policy=dict(command_artifacts or {}),
                )
                if not configured.accepted:
                    return DeliverResult(
                        False,
                        "rejected",
                        reason=configured.reason or "artifact_policy_unavailable",
                    )
            if record is not None:
                previous_artifacts = record.command_artifacts
                record.command_artifacts = (
                    dict(command_artifacts) if command_artifacts is not None else None
                )
                if not self._publish_artifact_context(record):
                    record.command_artifacts = previous_artifacts
                    self._publish_artifact_context(record)
                    return DeliverResult(
                        False, "rejected", reason="artifact_policy_unavailable"
                    )
            if record is None and cwd is None:
                return DeliverResult(False, "rejected", reason="cwd_required")

            replace = (
                record is None
                or (fresh and not pending_rollover_target)
                or (cwd is not None and cwd != record.cwd)
            )
            if replace and record is not None and record.session_id is not None:
                old_session_id = record.session_id
                try:
                    if self.client.active_turn(old_session_id) is not None:
                        if require_idle:
                            return DeliverResult(
                                False, "rejected", reason="active_turn"
                            )
                        interrupted = await self.client.interrupt(old_session_id)
                        if not interrupted.accepted:
                            return DeliverResult(
                                False,
                                "uncertain",
                                reason="codex_replace_active",
                                session_id=old_session_id,
                            )
                        await self.client.wait_for_thread(old_session_id)
                except Exception:
                    # The old turn may still be running. Do not create a second lineage until
                    # its state is known.
                    return DeliverResult(
                        False,
                        "uncertain",
                        reason="codex_replace_uncertain",
                        session_id=old_session_id,
                    )
                if self.client.active_turn(old_session_id) is not None:
                    return DeliverResult(
                        False,
                        "uncertain",
                        reason="codex_replace_active",
                        session_id=old_session_id,
                    )
            source_record = record
            if replace:
                previous = record
                preserved = previous if context_rollover else None
                record = CodexWorkerRecord(
                    key=key,
                    cwd=cwd or (previous.cwd if previous else ""),
                    delegated=preserved.delegated if preserved else delegated,
                    role=preserved.role if preserved else role,
                    session_token=session_token or uuid.uuid4().hex,
                    model=model if model is not None else (preserved.model if preserved else None),
                    effort=(
                        effort if effort is not None else (preserved.effort if preserved else None)
                    ),
                    command_artifacts=(
                        dict(command_artifacts)
                        if command_artifacts is not None
                        else None
                    ),
                    deliveries=preserved.deliveries if preserved else {},
                )
                self._records[key] = record

            if context_operation and not retrying_context:
                record.pending_context_delivery_id = delivery_id
                record.pending_context_operation = context_operation
                record.pending_context_source_session_id = expected_session_id
                record.pending_context_state = (
                    "compacting" if context_maintenance else "preparing"
                )
                record.pending_context_pre_last_result_at = (
                    record.last_result_at if context_maintenance else None
                )
                record.pending_context_post_last_result_at = None
                record.pending_context_maintenance_status = (
                    "pending" if context_maintenance else None
                )
                record.pending_context_maintenance_reason = None
                self._save_state()

            def restore_source_after_clean_rollover_failure() -> None:
                if not context_rollover or source_record is None or record.session_id:
                    return
                source_record.pending_context_delivery_id = delivery_id
                source_record.pending_context_operation = "rollover"
                source_record.pending_context_source_session_id = expected_session_id
                source_record.pending_context_state = "preparing"
                self._records[key] = source_record
                self._save_state()

            receipt_prepared = False

            def prepare_receipt() -> None:
                nonlocal receipt_prepared
                if receipt_prepared:
                    return
                record.receipts.prepare(
                    delivery_id,
                    fingerprint,
                    session_id=record.session_id,
                    correlation=correlation,
                )
                self._save_state()
                receipt_prepared = True

            def begin_provider_write() -> None:
                prepare_receipt()
                if record.receipts.begin_write(
                    delivery_id, fingerprint, session_id=record.session_id
                ):
                    self._save_state()

            def identify_provider_turn(turn_id: str) -> None:
                if record.receipts.identify_turn(
                    delivery_id,
                    fingerprint,
                    turn_id,
                    session_id=record.session_id,
                ):
                    self._save_state()

            # Context maintenance is safe to retry until compaction has finished.
            # Prepare the ordinary-message receipt only at the app-server's
            # turn/start boundary so a crash during compaction does not strand the
            # attention batch as an uncertain write that never happened.
            defer_receipt = context_maintenance and record.session_id is not None
            if not defer_receipt:
                prepare_receipt()
            created_thread = False
            if record.session_id is None:
                try:
                    created = await self.client.create_thread(
                        Path(record.cwd),
                        model=record.model,
                        role=resolve_session_role(
                            delegated=record.delegated, role=record.role
                        ),
                        session_token=record.session_token,
                    )
                except Exception as exc:
                    log.exception(
                        "Codex thread creation failed for %s",
                        self.account.key,
                        exc_info=redacted_exc_info(exc),
                    )
                    record.receipts.forget(delivery_id, write_started=False)
                    restore_source_after_clean_rollover_failure()
                    self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason="codex_create_failed",
                        retryable_same_delivery=context_rollover,
                        context_rollover=context_rollover,
                    )
                if not created.accepted or not created.session_id:
                    log.error(
                        "Codex thread creation rejected for %s: %s",
                        self.account.key,
                        redact_diagnostic(created.reason or "missing thread id"),
                    )
                    record.receipts.forget(delivery_id, write_started=False)
                    restore_source_after_clean_rollover_failure()
                    self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason="codex_create_failed",
                        retryable_same_delivery=context_rollover,
                        context_rollover=context_rollover,
                    )
                record.session_id = created.session_id
                if not self._publish_artifact_context(record):
                    record.receipts.forget(delivery_id, write_started=False)
                    self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason="artifact_policy_unavailable",
                        session_id=record.session_id,
                    )
                # `create_thread` composed the guide from the same record fields,
                # so the thread now holds exactly this text as its developer
                # instructions and owes no refresh until the text itself changes.
                record.guide_fingerprint = session_guide_fingerprint(
                    self._current_guide(record)
                )
                if record.session_token:
                    self._bind_session_token(
                        record.session_token,
                        f"{self.account.key}:{record.session_id}",
                    )
                created_thread = True
                # Persist lineage before the first user message can be written. A crash after
                # this point replays the prepared receipt as uncertain but never loses which
                # thread may have received it.
                self._save_state()
            try:
                if created_thread:
                    begin_provider_write()
                    start_kwargs = {
                        "images": [Path(path) for path in images or []],
                        "effort": record.effort,
                        "wait": False,
                    }
                    result = await self.client.start_turn(
                        record.session_id, message, **start_kwargs
                    )
                    if not result.accepted:
                        record.receipts.forget(delivery_id, write_started=False)
                        self._save_state()
                        return DeliverResult(
                            False,
                            "rejected",
                            reason="codex_start_failed",
                            session_id=record.session_id,
                            successor_created=context_rollover,
                            retryable_same_delivery=context_rollover,
                            context_rollover=context_rollover,
                        )
                    action = "spawned"
                elif self.client.active_turn(record.session_id) is not None:
                    if require_idle:
                        record.receipts.forget(delivery_id, write_started=False)
                        self._save_state()
                        return DeliverResult(
                            False,
                            "rejected",
                            reason="active_turn",
                            session_id=record.session_id,
                        )
                    active_turn = self.client.active_turn(record.session_id)
                    begin_provider_write()
                    if active_turn is not None:
                        identify_provider_turn(active_turn)
                    result = await self.client.steer(
                        record.session_id,
                        message,
                        images=[Path(path) for path in images or []],
                    )
                    if (
                        not result.accepted
                        and result.reason == "there is no agentdeck-owned active turn"
                    ):
                        if require_active:
                            record.receipts.forget(delivery_id, write_started=False)
                            self._save_state()
                            return DeliverResult(
                                False, "rejected", reason="worker_inactive"
                            )
                        # The turn ended between our state check and the steer preflight. The
                        # app-server sent nothing in this rejection, so starting an ordinary
                        # follow-up is safe and preserves the caller's one logical delivery.
                        fallback_kwargs = {
                            "images": [Path(path) for path in images or []],
                            "effort": record.effort,
                        }
                        result = await self.client.queue_turn(
                            record.session_id, message, **fallback_kwargs
                        )
                        action = "queued"
                    else:
                        action = "steered"
                    if not result.accepted:
                        record.receipts.forget(delivery_id, write_started=False)
                        self._save_state()
                        if context_maintenance:
                            return DeliverResult(
                                False,
                                "rejected",
                                reason="context_maintenance_failed",
                                session_id=record.session_id,
                                retryable_same_delivery=True,
                                context_maintenance_status="failed",
                                context_maintenance_reason=result.reason,
                            )
                        return DeliverResult(
                            False,
                            "rejected",
                            reason=result.reason,
                            session_id=record.session_id,
                        )
                else:
                    queue_kwargs = {
                        "images": [Path(path) for path in images or []],
                        "effort": record.effort,
                    }
                    run_maintenance = context_maintenance
                    if context_maintenance and retrying_context:
                        self._sync_result(record)
                        if record.pending_context_state == "compacting":
                            if self.client.active_turn(record.session_id) is not None:
                                return DeliverResult(
                                    False,
                                    "rejected",
                                    reason="context_operation_pending",
                                    session_id=record.session_id,
                                    retryable_same_delivery=True,
                                    context_maintenance_status="unknown",
                                )
                            pre = record.pending_context_pre_last_result_at or 0.0
                            if record.last_result_at > pre:
                                record.pending_context_state = "compact_done"
                                record.pending_context_post_last_result_at = (
                                    record.last_result_at
                                )
                                record.pending_context_maintenance_status = "unknown"
                                self._save_state()
                        run_maintenance = record.pending_context_state == "compacting"

                    def after_maintenance(status: str, reason: str | None) -> None:
                        record.pending_context_state = "compact_done"
                        record.pending_context_post_last_result_at = record.last_result_at
                        record.pending_context_maintenance_status = status
                        record.pending_context_maintenance_reason = reason
                        self._save_state()

                    if context_maintenance:
                        queue_kwargs["maintain_context"] = run_maintenance
                        if run_maintenance:
                            queue_kwargs["after_maintenance"] = after_maintenance
                        queue_kwargs["before_start"] = prepare_receipt
                    if require_idle:
                        queue_kwargs["require_idle"] = True
                    # Only an ordinary idle follow-up carries a guide refresh. A
                    # steer is a mid-turn nudge, and compaction is the deck
                    # talking to itself; neither is a turn the operator chose.
                    guide_refresh = None if context_maintenance else self._stale_guide(record)
                    result = await self.client.queue_turn(
                        record.session_id,
                        wrap_session_guide(guide_refresh, message)
                        if guide_refresh is not None
                        else message,
                        **queue_kwargs,
                    )
                    if not result.accepted:
                        record.receipts.forget(delivery_id, write_started=False)
                        self._save_state()
                        if context_maintenance:
                            return DeliverResult(
                                False,
                                "rejected",
                                reason="context_maintenance_failed",
                                session_id=record.session_id,
                                retryable_same_delivery=True,
                                context_maintenance_status=(
                                    record.pending_context_maintenance_status
                                    if record.pending_context_maintenance_status
                                    in {"succeeded", "failed", "unknown"}
                                    else "unknown"
                                ),
                                context_maintenance_reason=(
                                    record.pending_context_maintenance_reason
                                    or result.reason
                                ),
                            )
                        return DeliverResult(
                            False,
                            "rejected",
                            reason=result.reason,
                            session_id=record.session_id,
                        )
                    if guide_refresh is not None:
                        # Only once the turn was accepted: a refresh recorded for a
                        # turn that never landed would leave the thread stale with
                        # nothing left to tell it.
                        record.guide_fingerprint = session_guide_fingerprint(guide_refresh)
                    action = "queued"
            except Exception:  # the app-server may have accepted before transport failed
                if not receipt_prepared:
                    self._sync_result(record)
                    pre = record.pending_context_pre_last_result_at or 0.0
                    if context_maintenance and record.last_result_at > pre:
                        record.pending_context_state = "compact_done"
                        record.pending_context_post_last_result_at = record.last_result_at
                        record.pending_context_maintenance_status = "unknown"
                        self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason="context_maintenance_failed",
                        session_id=record.session_id,
                        retryable_same_delivery=True,
                        context_maintenance_status=(
                            record.pending_context_maintenance_status
                            if record.pending_context_maintenance_status
                            in {"succeeded", "failed", "unknown"}
                            else "unknown"
                        ),
                        context_maintenance_reason=(
                            record.pending_context_maintenance_reason
                        ),
                    )
                return DeliverResult(
                    True,
                    "uncertain",
                    session_id=record.session_id,
                    provider_write_state="unknown",
                    successor_created=context_rollover,
                    context_maintenance_status=(
                        "unknown" if context_maintenance else None
                    ),
                    context_rollover=context_rollover,
                )

            record.last_delivery_at = time.time()
            # Receiving work is activity too, and this is the one write that is certain to
            # land even for a thread that then produces nothing at all.
            record.last_activity_at = record.last_delivery_at
            self._activity_saved[record.key] = record.last_activity_at
            delivered = DeliverResult(
                True,
                action,
                session_id=record.session_id,
                provider_write_state="started",
                successor_created=context_rollover,
                context_maintenance_status=(
                    record.pending_context_maintenance_status
                    if context_maintenance
                    and record.pending_context_maintenance_status
                    in {"succeeded", "failed", "unknown"}
                    else None
                ),
                context_maintenance_reason=(
                    record.pending_context_maintenance_reason
                    if context_maintenance
                    else None
                ),
                context_rollover=context_rollover,
            )
            record.receipts.finalize(delivery_id, fingerprint, delivered)
            if context_operation:
                record.pending_context_delivery_id = None
                record.pending_context_operation = None
                record.pending_context_source_session_id = None
                record.pending_context_state = None
                record.pending_context_pre_last_result_at = None
                record.pending_context_post_last_result_at = None
                record.pending_context_maintenance_status = None
                record.pending_context_maintenance_reason = None
            self._save_state()
            return delivered

    def change_entry(self, thread_id: str) -> tuple[tuple[str, dict[str, Any]], ...]:
        """What the web reads of the workers on one thread, for the change feed (#2865).

        A record with no thread yet is addressed by its own key instead. The activity
        clock moves on every notification of a busy turn and nothing in the web reads
        it, so it is left out of the comparison.
        """
        records = [
            (key, record)
            for key, record in self._records.items()
            if record.session_id == thread_id or (not record.session_id and key == thread_id)
        ]
        rows = self._snapshot_rows(records)
        for row in rows.values():
            row.pop("last_activity_at", None)
        return tuple(sorted(rows.items()))

    def change_keys(self) -> set[str]:
        """Each record's thread id, or its key while it has none (#2865)."""
        return {record.session_id or key for key, record in self._records.items()}

    def snapshot(self) -> dict[str, Any]:
        return self._snapshot_envelope(self._snapshot_rows(self._records.items()))

    def _snapshot_rows(
        self, records: Iterable[tuple[str, CodexWorkerRecord]]
    ) -> dict[str, dict[str, Any]]:
        workers = {}
        for key, record in records:
            self._sync_result(record)
            thread_id = record.session_id
            owned = bool(thread_id and self.client.owns(thread_id))
            active = bool(owned and self.client.active_turn(thread_id))
            interaction = self.client.interaction(thread_id) if owned else None
            workers[key] = {
                "session_id": thread_id,
                "cwd": record.cwd,
                "live": owned,
                "turn_active": active,
                "effective_activity": active,
                "last_delivery_at": record.last_delivery_at,
                "last_result_at": record.last_result_at,
                "last_result_subtype": record.last_result_subtype,
                "last_result_refusal": record.last_result_refusal,
                "last_activity_at": record.last_activity_at,
                "pending_interaction": asdict(interaction) if interaction is not None else None,
                "pending_context_delivery_id": record.pending_context_delivery_id,
                "pending_context_operation": record.pending_context_operation,
                **DeliveryCorrelation.snapshot_fields(record.receipts),
                "model": record.model,
                "effort": record.effort,
                "role": record.role,
                "delegated": record.delegated,
            }
        return workers

    def _snapshot_envelope(self, workers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "workers": workers,
            # When this runtime started, so a reader can tell a restart that replaced every
            # worker process from a death that took one (#1659).
            SNAPSHOT_BOOTED_AT_KEY: RUNTIME_BOOTED_AT,
            # What these records can be read for. A poller from a newer deployment uses it
            # to tell an absent field from a runtime that cannot publish one (#1407).
            SNAPSHOT_CAPABILITIES_KEY: [
                *WORKER_SNAPSHOT_CAPABILITIES,
                SNAPSHOT_CONTEXT_MANAGEMENT_V1,
                # Advertised here because this is the list the deck reads; the fork
                # itself is the app-server's `thread/fork` behind `/start` (#2485).
                SNAPSHOT_FORK_V1,
                # The app-server resumes a thread whose directory is gone in its
                # repository when it starts the next turn (#2524).
                SNAPSHOT_RELOCATE_V1,
                        ],
            "live_count": sum(w["live"] for w in workers.values()),
        }

    def find_key_by_session(self, session_id: str) -> str | None:
        for key, record in self._records.items():
            if record.session_id == session_id:
                return key
        return None

    def _current_guide(self, record: CodexWorkerRecord) -> str:
        return agentdeck_session_instructions(
            role=resolve_session_role(delegated=record.delegated, role=record.role),
            provider="codex",
            session_token=record.session_token,
        )

    def _stale_guide(self, record: CodexWorkerRecord) -> str | None:
        """Return the guide this thread has not been told yet, else None.

        Claude rebuilds `--append-system-prompt` for every CLI process, including
        a `--resume` revive, so an edit to the guide reaches it on the next turn
        for free. Codex cannot: the thread holds the instructions it was born
        with. Prefixing one user turn is the only channel left, so it is spent
        once per guide version rather than on a turn counter — the text is ~13k
        characters and stays in the thread's history forever.
        """
        guide = self._current_guide(record)
        if session_guide_fingerprint(guide) == record.guide_fingerprint:
            return None
        return guide

    def worker_role(self, key: str) -> SessionRole | None:
        record = self._records.get(key)
        return record.role if record is not None else None

    def delivery_receipt(self, key: str, delivery_id: str) -> dict[str, Any] | None:
        record = self._records.get(key)
        receipt = record.deliveries.get(delivery_id) if record is not None else None
        return dict(receipt) if receipt is not None else None

    async def resolve_context_delivery(
        self, key: str, delivery_id: str, choice: str, rationale: str
    ) -> DeliverResult:
        async with self._key_lock(key):
            record = self._records.get(key)
            if record is None:
                return DeliverResult(False, "rejected", reason="worker_not_found")
            result = record.receipts.resolve(
                delivery_id, choice, rationale, session_id=record.session_id
            )
            if not result.accepted:
                return result
            if record.pending_context_delivery_id == delivery_id:
                record.pending_context_delivery_id = None
                record.pending_context_operation = None
                record.pending_context_source_session_id = None
                record.pending_context_state = None
                record.pending_context_pre_last_result_at = None
                record.pending_context_post_last_result_at = None
                record.pending_context_maintenance_status = None
                record.pending_context_maintenance_reason = None
            self._save_state()
            return result

    async def park_worker(self, key: str) -> DeliverResult:
        """End this key's running turn and keep its thread, so a later delivery resumes it.

        What the Runtime's Run stop asks of every provider (#2159). Idempotent: a key with
        no running turn, or none at all, is already parked.
        """
        async with self._key_lock(key):
            record = self._records.get(key)
            if record and record.session_id and self.client.active_turn(record.session_id):
                await self.client.interrupt(record.session_id)
                await self.client.wait_for_thread(record.session_id)
            return DeliverResult(
                True, "parked", session_id=record.session_id if record else None
            )

    async def release_worker(self, key: str) -> DeliverResult:
        async with self._key_lock(key):
            record = self._records.get(key)
            if record is not None and record.pending_context_delivery_id:
                return DeliverResult(
                    False, "rejected", reason="context_operation_pending"
                )
            if record and record.session_id and self.client.active_turn(record.session_id):
                await self.client.interrupt(record.session_id)
                await self.client.wait_for_thread(record.session_id)
            if record is not None:
                self._records.pop(key, None)
                self._save_state()
            return DeliverResult(
                True, "released", session_id=record.session_id if record else None
            )
