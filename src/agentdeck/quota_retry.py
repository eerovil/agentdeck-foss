"""Durable continuation of deck-owned chats after a quota reset or a lost connection."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from .models import Session
from .scheduled_messages import QUOTA_RETRY_PREFIX, ScheduledMessage

RETRY_TEXT = (
    "Your previous turn failed before it finished: this account had reached its usage limit, "
    "or this host lost its connection to the model or its sign-in. AgentDeck waited for the "
    "limit to reset or the connection to return; continue from the latest user message now."
)
QUOTA_RETRY_ID_PREFIX = "quota-retry:"


def message_id_for(session: Session) -> str | None:
    """Stable id of this session's retryable quota boundary, if it has one."""
    reset = session.automatic_retry_at
    if not session.turn_failed or reset is None:
        return None
    reset = reset.astimezone(UTC)
    identity = hashlib.sha256(f"{session.key}\n{reset.isoformat()}".encode()).hexdigest()[:24]
    return f"{QUOTA_RETRY_ID_PREFIX}{identity}"


def message_for(session: Session, *, now: datetime | None = None) -> ScheduledMessage | None:
    """One stable scheduled continuation for a retryable quota failure."""
    reset = session.automatic_retry_at
    message_id = message_id_for(session)
    if message_id is None or reset is None:
        return None
    reset = reset.astimezone(UTC)
    return ScheduledMessage(
        id=message_id,
        chat_id=session.chat_id or session.key,
        text=RETRY_TEXT,
        due_at=reset,
        created_at=(now or datetime.now(UTC)),
        hold_reason=f"{QUOTA_RETRY_PREFIX}{session.key}:{reset.timestamp()}",
    )


class QuotaRetryService:
    """Observe normalized sessions and schedule each provider-declared retry once."""

    def __init__(
        self,
        sessions: Callable[[], Iterable[Session]],
        current_key: Callable[[str], str],
        schedule: Callable[[ScheduledMessage], ScheduledMessage | None],
        exists: Callable[[str], bool] = lambda _message_id: False,
        *,
        bus=None,
    ) -> None:
        self._sessions = sessions
        self._current_key = current_key
        self._schedule = schedule
        self._exists = exists
        self._bus = bus
        self._task: asyncio.Task | None = None

    def reconcile(self, *, now: datetime | None = None) -> int:
        """Persist newly observed retries; return how many rows were accepted."""
        added = 0
        for session in self._sessions():
            if self._current_key(session.key) != session.key:
                continue
            message = message_for(session, now=now)
            if (
                message is not None
                and not self._exists(message.id)
                and self._schedule(message) is not None
            ):
                added += 1
        return added

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="quota-retries")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        sessions = self._bus.subscribe("sessions") if self._bus is not None else None
        try:
            while True:
                self.reconcile()
                if sessions is None:
                    return
                await sessions.get()
                while sessions.get_nowait() is not None:
                    pass
        finally:
            if sessions is not None:
                sessions.close()
