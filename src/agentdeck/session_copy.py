"""The web's live chats, copied to the services process (#4001, #4016).

The Work router and the merge release read a few things about each live chat: whether it is
mid-turn, when it last moved, which Work its issue link names. Only the web holds the live
session model, so it sends the services process a copy of exactly those fields
(:data:`FIELDS`), the way the runtime's change feed travels the other way (ADR 0007): the web
keeps one change number, and a held ``GET /internal/services/sessions`` is answered as soon
as it moves, with the chats that changed since the reader's number.

Everything else the moved code reads (chat legs, registrations, Work links, review history)
is in ``agentdeck.db``, which the services process reads with its own connection. A
registration the web wrote moves the same number, so the services process reads the
registrations again (``registrations`` in the answer) rather than waiting for its next start.

A copied chat is a :class:`CopySession`. Reading a field of the real ``Session`` that the copy
does not carry raises :class:`CopyFieldError`, never ``AttributeError``, so a
``getattr(session, name, default)`` cannot quietly read a default where the web would have
read a value: a missed field fails in tests and reaches Sentry live.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Session, SessionStatus

log = logging.getLogger(__name__)

#: What the moved code reads of a chat (``work_router``'s ``_latest_chat``, ``owner_of_*``,
#: ``work_busy``; ``work_overview._leg_working``; ``AppState.work_projection`` and
#: ``current_session_for_chat``; ``agent_review.deck_hooks``' ``checkout_of``).
FIELDS = (
    "key",
    "account_key",
    "session_id",
    "status",
    "last_activity",
    "last_progress",
    "thinking",
    "activity",
    "stalled",
    "turn_failed",
    "issue_url",
    "cwd",
)
#: Not a ``Session`` field: what the deck shows of ``thinking``, its descendants' work
#: included (``agent_review.deck_hooks``' ``alive``). Sent only for a chat the deck minted a
#: token for, the only chats a reviewer's ``alive`` asks about; ``None`` for the rest.
SHOWN_THINKING = "shown_thinking"
_DATES = frozenset({"last_activity", "last_progress"})
_SESSION_NAMES = frozenset(
    {field.name for field in dataclasses.fields(Session)}
    | {name for name, value in vars(Session).items() if isinstance(value, property)}
)

#: Marks gathered for this long before the copy is compared (#4001: "debounced to 250 ms").
DEBOUNCE_S = 0.25
#: How long a held read waits with nothing to say; a keep-alive, it reads nothing.
KEEPALIVE_S = 50.0


class CopyFieldError(RuntimeError):
    """A ``Session`` field the copy does not carry was read: add it to ``FIELDS``."""


class CopySession:
    """One copied chat: the :data:`FIELDS` of the web's ``Session``, and nothing else."""

    __slots__ = (*FIELDS, SHOWN_THINKING)

    def __init__(self, **fields: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, fields.get(name))

    def __getattr__(self, name: str) -> Any:
        # Only reached for a name ``__slots__`` does not hold.
        if name in _SESSION_NAMES:
            raise CopyFieldError(f"the services' copy of a chat has no {name!r} (#4001)")
        raise AttributeError(name)

    def __repr__(self) -> str:
        return f"CopySession({self.key!r})"


def entry(session: Session, shown: bool | None) -> dict:
    """What the copy sends of one chat, as plain JSON."""
    out: dict[str, Any] = {}
    for name in FIELDS:
        value = getattr(session, name, None)
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, SessionStatus):
            value = value.value
        elif isinstance(value, Path):
            value = str(value)
        out[name] = value
    out[SHOWN_THINKING] = shown
    return out


def session_from(raw: dict) -> CopySession:
    """:func:`entry` read back."""
    fields = dict(raw)
    for name in _DATES:
        if fields.get(name):
            fields[name] = datetime.fromisoformat(fields[name])
    if fields.get("status") is not None:
        fields["status"] = SessionStatus(fields["status"])
    if fields.get("cwd"):
        fields["cwd"] = Path(fields["cwd"])
    return CopySession(**fields)


def registrations_of(state) -> tuple:
    """The registrations' own fingerprint: a change sends ``registrations``."""
    return (
        tuple(
            sorted(
                (leg, tuple((ref.identity, ref.work_id) for ref in refs))
                for leg, refs in tuple(state.session_pull_requests.items())
            )
        ),
        tuple(
            sorted(
                (leg, tuple(ref.identity for ref in refs))
                for leg, refs in tuple(state.session_issues.items())
            )
        ),
    )


class SessionCopyFeed:
    """The web's half: the change number over the copy, and the held reads of it."""

    def __init__(self, state, scanned=lambda: True, *, shown: bool = True) -> None:
        self._state = state
        # Whether a deck reviewer's ``alive`` reads :data:`SHOWN_THINKING`: only with a Claude
        # or Codex Work reviewer, and building the display model for it is not free.
        self._shown = shown
        # Whether the web's first scan of every account is done: until then the copy is not
        # yet the deck's chats, and the release's start sweep waits (#2982).
        self._scanned = scanned
        # A fresh epoch per web worker: a reader holding a number from another must start over.
        self.epoch = uuid.uuid4().hex
        self.revision = 0
        self._seen: dict[str, dict] = {}
        self._moved: dict[str, int] = {}
        self._registrations: tuple | None = None
        self._registrations_moved = 0
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.readers = 0

    def compare(self) -> bool:
        """Compare the live chats with the copy last sent; whether the number moved."""
        state = self._state
        tokened = set(state.session_tokens.values()) if self._shown else set()
        presentation = None
        current: dict[str, dict] = {}
        for key, session in tuple(state.sessions.items()):
            shown = None
            if key in tokened:
                if presentation is None:
                    presentation = state.session_presentation()
                shown = bool(presentation.display(session).thinking)
            current[key] = entry(session, shown)
        moved = False
        self.revision += 1
        for key, value in current.items():
            if self._seen.get(key) != value:
                self._moved[key] = self.revision
                moved = True
        for key in set(self._seen) - set(current):
            self._moved[key] = self.revision
            moved = True
        self._seen = current
        registrations = registrations_of(state)
        if registrations != self._registrations:
            if self._registrations is not None:
                self._registrations_moved = self.revision
                moved = True
            self._registrations = registrations
        if not moved:
            self.revision -= 1
        else:
            self._changed.set()
        return moved

    def answer(self, since: int | None, epoch: str | None) -> dict:
        if self._registrations is None:
            self.compare()  # the first read, before any announcement
        reset = since is None or epoch != self.epoch or since > self.revision
        if reset:
            return {
                "epoch": self.epoch,
                "revision": self.revision,
                "reset": True,
                "sessions": list(self._seen.values()),
                "removed": [],
                "registrations": True,
                "scanned": bool(self._scanned()),
            }
        sessions, removed = [], []
        for key, revision in self._moved.items():
            if revision > since:
                if key in self._seen:
                    sessions.append(self._seen[key])
                else:
                    removed.append(key)
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "reset": False,
            "sessions": sessions,
            "removed": removed,
            "registrations": self._registrations_moved > since,
            "scanned": bool(self._scanned()),
        }

    async def wait(self, since: int | None, epoch: str | None, timeout: float) -> dict:
        """Answer at once when anything moved past ``since``, else after ``timeout`` at most."""
        if self._registrations is None:
            self.compare()  # the first read, before any announcement
        self.readers += 1
        try:
            if since is not None and epoch == self.epoch and since == self.revision:
                self._changed.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout)
            return self.answer(since, epoch)
        finally:
            self.readers -= 1

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._follow(), name="session-copy")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _follow(self) -> None:
        """Compare once per burst of ``sessions`` announcements, at most every 250 ms."""
        with self._state.bus.subscribe("sessions") as changes:
            while True:
                try:
                    self.compare()
                except Exception:  # noqa: BLE001 -- the next announcement compares again
                    log.exception("session copy: comparing the live chats failed")
                await changes.get()
                await asyncio.sleep(DEBOUNCE_S)
                while changes.get_nowait() is not None:
                    pass


def apply(state, answer: dict) -> tuple[frozenset[str], bool]:
    """Apply one answer to the services process's ``state``: the chats it changed, and
    whether the registrations moved."""
    changed: set[str] = set()
    if answer.get("reset"):
        changed.update(state.sessions)
        state.sessions.clear()
    for raw in answer.get("sessions") or ():
        session = session_from(raw)
        state.sessions[session.key] = session
        changed.add(session.key)
    for key in answer.get("removed") or ():
        if state.sessions.pop(key, None) is not None:
            changed.add(key)
    return frozenset(changed), bool(answer.get("registrations"))
