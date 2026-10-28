"""Durable deferred delivery through the ordinary session injection path.

Two things ride it: wake-on messages (``wake_on.py``) and a person's message sent
while the chat's runtime was restarting, held until it is back (#2847). The
person-facing "send later" button that first used it was removed in #2847.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .attachments import cleanup_uploaded_files
from .inject import InjectionService
from .models import Account, Capability, Session
from .providers.base import SessionProvider

log = logging.getLogger(__name__)

#: ``hold_reason`` of a message sent while the chat's runtime was unreachable.
RUNTIME_HOLD = "runtime"
QUOTA_RETRY_PREFIX = "quota:"
QUOTA_RETRY_SUPERSEDED = "the quota retry was superseded by newer chat activity"
#: How long a held message waits for the runtime before it is reported unsent: a
#: message landing hours late would be read out of context (#2847).
HOLD_LIMIT = timedelta(minutes=30)
HOLD_EXPIRED_REASON = "the runtime did not come back in 30 minutes"
ATTACHMENTS_LOST_REASON = "its attachments were lost"
#: The same, for a row a web restart left mid-handoff: the first attempt may have
#: reached the agent, so the deck cannot say it was not sent.
ATTACHMENTS_LOST_MID_SEND_REASON = (
    "its attachments were lost; it may have been sent before the restart"
)
HOLD_EXPIRED_MID_SEND_REASON = (
    "the runtime did not come back in 30 minutes; it may have been sent before the restart"
)


@dataclass(frozen=True)
class HeldAttachment:
    """One uploaded file a held message keeps until it is delivered."""

    path: Path
    kind: str  # "image" or "file", the two ways ``InjectionService.start`` takes one


@dataclass(frozen=True)
class ScheduledMessage:
    id: str
    chat_id: str
    text: str
    due_at: datetime
    created_at: datetime
    effort: str | None = None
    state: str = "scheduled"
    reason: str | None = None
    hold_reason: str | None = None
    attachments: tuple[HeldAttachment, ...] = ()

    @property
    def held_for_runtime(self) -> bool:
        return self.hold_reason == RUNTIME_HOLD

    @property
    def quota_retry(self) -> tuple[str, float] | None:
        """The source session and reset epoch for an automatic quota retry."""
        reason = self.hold_reason or ""
        if not reason.startswith(QUOTA_RETRY_PREFIX):
            return None
        source, mark, raw_epoch = reason[len(QUOTA_RETRY_PREFIX):].rpartition(":")
        if not mark or not source:
            return None
        try:
            epoch = float(raw_epoch)
        except ValueError:
            return None
        return (source, epoch) if epoch > 0 else None

    @property
    def images(self) -> list[Path]:
        return [item.path for item in self.attachments if item.kind == "image"]

    @property
    def files(self) -> list[Path]:
        return [item.path for item in self.attachments if item.kind == "file"]

    def hold_expired(self, now: datetime) -> bool:
        return self.held_for_runtime and now - self.created_at >= HOLD_LIMIT


@dataclass(frozen=True)
class ScheduledTarget:
    account: Account
    session: Session
    provider: SessionProvider


def _must_wait(
    message: ScheduledMessage, target: ScheduledTarget, injector: InjectionService
) -> bool:
    """Whether a due row should wait instead of being handed to a chat that cannot take it.

    Any row waits while the chat is the deck's own and its runtime cannot be reached
    (#2847). A held row also waits through the moment after the runtime returns and
    before the chat's controls are projected again; its 30-minute limit bounds that.
    """
    if Capability.INJECT in target.session.capabilities or injector.can_queue(
        target.session.key
    ):
        return False
    return message.held_for_runtime or target.provider.runtime_unreachable(
        target.account, target.session
    )


class ScheduledMessageService:
    """Claim due rows and hand them to ``InjectionService`` with a stable ID.

    A row is marked ``dispatching`` before provider I/O. If the web process dies
    in that gap, the next process retries the same client action ID; the
    persistent provider runtime's delivery fence then returns the original
    receipt instead of writing a second user message.
    """

    def __init__(
        self,
        store,
        injector: InjectionService,
        resolve_target: Callable[[str], ScheduledTarget | None],
        *,
        on_change: Callable[[str], None] | None = None,
        on_delivering: Callable[[str], None] | None = None,
        on_delivered: Callable[[str, str], None] | None = None,
        bus=None,
    ) -> None:
        self._store = store
        self._injector = injector
        self._resolve_target = resolve_target
        self._on_change = on_change or (lambda _chat_id: None)
        self._on_delivering = on_delivering or (lambda _session_key: None)
        # A held send is a person's message, so a parked card's answer is relayed from
        # here, once it reached the agent, never when it was held (#2847 review). Run in a
        # thread: it reads the board and writes the instance's state file.
        self._on_delivered = on_delivered or (
            lambda _session_key, _message: None
        )
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._chat_tasks: dict[str, asyncio.Task] = {}
        # The deck's event bus. A chat whose due row could not go yet — no target while
        # discovery warms up, a runtime that is restarting, an outcome nobody reported —
        # is waiting on its chat to change, and a chat change is a ``sessions`` publish.
        self._bus = bus
        # Chats whose due rows are waiting on a chat change, and are not tried again until
        # one arrives (or their hold runs out): retrying them on every other wake would
        # spin, since a finished attempt is itself a wake.
        self._blocked: set[str] = set()
        # When the earliest held row still due runs out its 30 minutes.
        self._hold_deadline: datetime | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="scheduled-messages")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
        for delivery in self._chat_tasks.values():
            delivery.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            if task is not None:
                await task
        await asyncio.gather(*self._chat_tasks.values(), return_exceptions=True)
        self._chat_tasks.clear()

    def schedule(self, message: ScheduledMessage) -> ScheduledMessage | None:
        recorded = self._store.record_scheduled_message(message)
        if recorded is not None:
            self._on_change(message.chat_id)
            # A new row is worth one try even for a chat that was waiting: it may be the
            # first row its chat can take.
            self._blocked.discard(message.chat_id)
            self._wake.set()
        return recorded

    def exists(self, message_id: str) -> bool:
        """Whether any row, in any state, is stored under this id."""
        return self._store.has_scheduled_message(message_id)

    def visible(self, chat_id: str) -> tuple[ScheduledMessage, ...]:
        return self._store.load_visible_scheduled_messages(chat_id)

    def pending_ids(self) -> frozenset[str]:
        """Deferred turns that still own a future delivery."""
        return self._store.pending_scheduled_message_ids()

    def handoffs(self, now: datetime, since: datetime) -> dict[str, datetime]:
        """Chats a message was handed to at or after ``since`` (#3084)."""
        return self._store.scheduled_message_handoffs(now, since)

    def cancel(self, message_id: str, chat_id: str) -> bool:
        message = self._store.load_scheduled_message(message_id)
        cancelled = self._store.cancel_scheduled_message(message_id, chat_id)
        if cancelled:
            if message is not None:
                cleanup_uploaded_files([item.path for item in message.attachments])
            self._on_change(chat_id)
            self._wake.set()
        return cancelled

    def _fail_waiting(self, message: ScheduledMessage, reason: str) -> None:
        """Report a message that never left the store unsent, and drop its files."""
        if self._store.fail_waiting_scheduled_message(message.id, reason):
            cleanup_uploaded_files([item.path for item in message.attachments])
            self._on_change(message.chat_id)

    def _fail_mid_send(self, message: ScheduledMessage, reason: str) -> None:
        """Settle a recovered mid-handoff row as unsent, and drop its files."""
        self._store.settle_scheduled_message(message.id, "failed", reason)
        cleanup_uploaded_files([item.path for item in message.attachments])
        self._on_change(message.chat_id)

    async def dispatch_due(
        self, now: datetime | None = None, *, skip_waiting: bool = False
    ) -> None:
        """Hand every due row to its chat's delivery task.

        ``skip_waiting`` is the loop's: a chat whose rows are waiting on a chat change is
        left alone until one arrives. A direct call tries every chat.
        """
        now = now or datetime.now(UTC)
        grouped: dict[str, list[ScheduledMessage]] = {}
        hold_deadline: datetime | None = None
        for message in self._store.load_due_scheduled_messages(now):
            grouped.setdefault(message.chat_id, []).append(message)
            if message.held_for_runtime:
                expires = message.created_at + HOLD_LIMIT
                hold_deadline = expires if hold_deadline is None else min(hold_deadline, expires)
        self._hold_deadline = hold_deadline
        for chat_id, messages in grouped.items():
            if skip_waiting and chat_id in self._blocked:
                continue
            running = self._chat_tasks.get(chat_id)
            if running is not None and not running.done():
                continue
            task = asyncio.create_task(
                self._deliver_chat(chat_id, tuple(messages)),
                name=f"scheduled-message:{chat_id}",
            )
            self._chat_tasks[chat_id] = task
            task.add_done_callback(
                lambda done, key=chat_id: self._delivery_done(key, done)
            )

    def _delivery_done(self, chat_id: str, task: asyncio.Task) -> None:
        if self._chat_tasks.get(chat_id) is task:
            self._chat_tasks.pop(chat_id, None)
        if task.cancelled():
            return
        if task.exception() is not None:
            exc = task.exception()
            log.error(
                "scheduled message delivery failed for %s",
                chat_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            self._blocked.add(chat_id)
        elif task.result() is False:
            # Completion is not evidence of progress: an unresolved target leaves the
            # same row due. That chat waits for a chat change instead of being retried
            # at once, which would spin (#2865).
            self._blocked.add(chat_id)
        else:
            self._blocked.discard(chat_id)
        # Anything else that became due for another chat meanwhile goes now.
        self._wake.set()

    async def _deliver_chat(
        self, chat_id: str, messages: tuple[ScheduledMessage, ...]
    ) -> bool:
        # Settle every expired held row before anything below can stop the pass: a
        # wake-on row ahead of it waits with no limit, and a held row queued behind
        # one must still be given up after its 30 minutes (#2847 review). A row left
        # mid-handoff is bounded too, or it would read "Sending now…" forever.
        now = datetime.now(UTC)
        live = []
        for message in messages:
            if not message.hold_expired(now):
                live.append(message)
            elif message.state == "scheduled":
                self._fail_waiting(message, HOLD_EXPIRED_REASON)
            else:
                self._fail_mid_send(message, HOLD_EXPIRED_MID_SEND_REASON)
        for message in live:
            waiting = message.state == "scheduled"
            # A ``dispatching`` row is one a web restart left mid-handoff. It is
            # retried under the same id, so the runtime's fence replays a write
            # that already happened; it still waits for the runtime and still
            # checks its files, exactly like a fresh row (#2847 review).
            target = self._resolve_target(chat_id)
            if target is None:
                # Discovery may still be warming after a web restart. Leave the
                # row scheduled and retry on the next chat change (#2865).
                return False
            quota_retry = message.quota_retry
            if quota_retry is not None:
                source, reset_epoch = quota_retry
                current_reset = target.session.automatic_retry_at
                still_due = bool(
                    target.session.key == source
                    and target.session.turn_failed
                    and current_reset is not None
                    and abs(current_reset.timestamp() - reset_epoch) < 1.0
                )
                if not still_due:
                    if waiting:
                        self.cancel(message.id, message.chat_id)
                    else:
                        self._fail_mid_send(message, QUOTA_RETRY_SUPERSEDED)
                    continue
            if _must_wait(message, target, self._injector):
                # The chat is the deck's own and its runtime is restarting: wait for
                # its chat to change instead of failing the send (#2847, #2865).
                return False
            if any(not item.path.is_file() for item in message.attachments):
                # A reboot can empty the temporary folder; a message read without the
                # files it refers to would be read wrongly, so it is not sent.
                if waiting:
                    self._fail_waiting(message, ATTACHMENTS_LOST_REASON)
                else:
                    self._fail_mid_send(message, ATTACHMENTS_LOST_MID_SEND_REASON)
                continue
            if not self._store.claim_scheduled_message(message.id):
                continue
            self._on_change(chat_id)
            self._on_delivering(target.session.key)
            uploads = (
                {"images": message.images, "files": message.files}
                if message.attachments
                else {}
            )
            result = await self._injector.start(
                target.account,
                target.session,
                target.provider,
                message.text,
                **uploads,
                effort=message.effort,
                client_action_id=message.id,
                allow_over_budget=True,
            )
            if not result.accepted:
                cleanup_uploaded_files([item.path for item in message.attachments])
                self._store.settle_scheduled_message(
                    message.id, "failed", result.reason or "delivery refused"
                )
                self._on_change(chat_id)
                continue
            # start() publishes the new mutable queue item before returning.
            # Keep it across the wait: status() only shows the last twelve items,
            # so a busy chat can evict this message before its delivery settles.
            # A replay may also leave an older item with the same action ID.
            status = self._injector.status(target.session.key)
            item = next(
                (
                    item
                    for item in reversed(status.items if status is not None else ())
                    if item.client_action_id == message.id
                ),
                None,
            )
            await self._injector.wait_for_delivery(target.session.key)
            if item is None or item.state not in {"accepted", "observed", "complete", "failed"}:
                # Missing evidence is not success or proof that nothing was sent.
                # Leave the row dispatching for recovery under the same stable ID.
                log.warning("scheduled message outcome unavailable for %s", message.id)
                return False
            if item.state == "failed":
                self._store.settle_scheduled_message(
                    message.id, "failed", item.reason or "delivery failed"
                )
            else:
                self._store.settle_scheduled_message(message.id, "delivered")
                self._on_delivered(target.session.key, message.text)
            self._on_change(chat_id)
        return True

    def next_due(self, now: datetime) -> datetime | None:
        """When the next row falls due, or a held row runs out its 30 minutes (#2865).

        A row scheduled for later is due at its own ``due_at``; a held row waiting on its
        runtime is given up at ``created_at + HOLD_LIMIT``. None when neither is pending.
        A store without the lookup (a test fake) has no future rows to offer.
        """
        moments = [] if self._hold_deadline is None else [self._hold_deadline]
        later = getattr(self._store, "next_scheduled_message_due", None)
        if later is not None:
            upcoming = later(now)
            if upcoming is not None:
                moments.append(upcoming)
        future = [moment for moment in moments if moment > now]
        return min(future) if future else None

    async def _run(self) -> None:
        """Deliver due rows, then sleep until something changes or the next deadline (#2865).

        No tick: `schedule` and `cancel` wake the loop, a finished delivery wakes it, a
        chat change on the bus wakes it while some chat's rows are waiting on one, and
        otherwise it sleeps exactly until `next_due`.
        """
        sessions = self._bus.subscribe("sessions") if self._bus is not None else None
        try:
            while True:
                self._wake.clear()
                if sessions is not None:
                    sessions.changed_topics()  # what the pass below sees is now current
                try:
                    await self.dispatch_due(skip_waiting=True)
                except Exception:  # noqa: BLE001 -- one bad row must not kill the clock
                    log.exception("scheduled message sweep failed")
                await self._wait_for_change(sessions)
        finally:
            if sessions is not None:
                sessions.close()

    async def _wait_for_change(self, sessions) -> None:
        due = self.next_due(datetime.now(UTC))
        timeout = None if due is None else max(0.0, (due - datetime.now(UTC)).total_seconds())
        waits = [asyncio.ensure_future(self._wake.wait())]
        if sessions is not None and self._blocked:
            if sessions.changed_topics():
                # A chat changed while the pass ran: the waiting chats are worth a look.
                self._blocked.clear()
                return
            waits.append(asyncio.ensure_future(sessions.get()))
        try:
            done, _pending = await asyncio.wait(
                waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiting in waits:
                waiting.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        # Keep why the wake fired (ADR 0007 rule 4): a chat change that lands together with
        # a `_wake` still releases the waiting chats, or the next pass would skip them and
        # their due rows would wait for another, unrelated chat change (#2865).
        chat_changed = any(waiting.done() and not waiting.cancelled() for waiting in waits[1:])
        if sessions is not None:
            while sessions.get_nowait() is not None:
                chat_changed = True
            if sessions.changed_topics():
                chat_changed = True
        if chat_changed or not self._wake.is_set():
            # A chat change or a deadline: every waiting chat gets another try.
            self._blocked.clear()
