"""Which chats the web process keeps live in memory (#2841).

The deck used to hold every chat it had ever owned — over five thousand — and every
background loop walked all of them every few seconds, so the web process was busy even
with nobody looking. It now keeps a chat live while any of these is true:

- it was active within the window (``days``, 7 by default), or is one of a provider's
  newest-two-hundred, which the providers keep listing as before;
- it belongs to a Work that is not finished (``pinned``);
- someone opened it in the last hour (``request``), so an older chat reached from
  search, a link or a pin can be read and answered.

A chat whose Work is finished is let go even inside the window (``finished``), unless it
is working, live or waiting on a question, or was just opened. Nothing is deleted: a
chat that is let go is simply not scanned or walked until it is active or opened again.

``days = 0`` turns all of this off and keeps every chat, as before.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from contextvars import ContextVar

from .models import Session, SessionStatus

# How long a chat opened on demand stays live after its last open.
ON_DEMAND_S = 3600.0

# The window a provider scan applies, set by whoever runs the scan for the duration of
# that call. A context variable rather than a provider attribute: providers are
# process-wide singletons, and a scan run with no window (a test, a one-off tool) must
# list exactly what it always listed.
CURRENT: ContextVar[LiveWindow | None] = ContextVar("agentdeck_live_window", default=None)


def current() -> LiveWindow | None:
    """The window of the scan running in this context, if it has one switched on."""
    window = CURRENT.get()
    return window if window is not None and window.enabled else None


class LiveWindow:
    def __init__(self, days: float = 0.0, *, clock=time.time) -> None:
        self.window_s = max(0.0, float(days)) * 86400.0
        self._clock = clock
        self._opened: dict[str, float] = {}
        self.pinned: frozenset[str] = frozenset()
        self.finished: frozenset[str] = frozenset()
        # Every leg of a chat the deck holds, and its parent and child chats: a chat
        # comes whole, and delegation keeps nesting (D1, D2). Published after each scan.
        self.companions: frozenset[str] = frozenset()

    @property
    def enabled(self) -> bool:
        return self.window_s > 0

    def cutoff(self) -> float:
        """The oldest last-write time (epoch seconds) a chat may have and stay live."""
        return self._clock() - self.window_s

    def request(self, keys: Iterable[str]) -> None:
        """Keep these chats live for ``ON_DEMAND_S`` from now: someone opened them."""
        now = self._clock()
        for key in keys:
            self._opened[key] = now

    def opened(self) -> frozenset[str]:
        """The chats opened within the last hour. Called on the event loop only, where
        ``request`` writes the map; a scan's worker thread gets ``extra_keys()`` handed
        in, and its ``keeps`` only reads one key."""
        now = self._clock()
        live = {key: at for key, at in list(self._opened.items()) if now - at <= ON_DEMAND_S}
        self._opened = live
        return frozenset(live)

    def _opened_now(self, key: str) -> bool:
        at = self._opened.get(key)
        return at is not None and self._clock() - at <= ON_DEMAND_S

    def extra_keys(self) -> frozenset[str]:
        """Chats kept whatever their age: opened on demand, or in a Work not finished."""
        return self.opened() | self.pinned | self.companions

    def set_works(self, *, pinned: Iterable[str], finished: Iterable[str]) -> frozenset[str]:
        """The chats of the Works that are not finished, and of those that are; returns
        the chats newly kept for an unfinished Work, which a scan has yet to list.

        A chat in both kinds of Work is kept: the unfinished one still needs it.
        """
        before = self.pinned
        self.pinned = frozenset(pinned)
        self.finished = frozenset(finished) - self.pinned
        return self.pinned - before

    def set_companions(self, keys: Iterable[str]) -> frozenset[str]:
        """The legs and relatives of the chats held; returns the ones newly added."""
        before = self.companions
        self.companions = frozenset(keys)
        return self.companions - before

    def keeps(self, key: str, last_write: float | None) -> bool:
        """Whether a provider should list this owned chat, from its transcript's mtime.

        A chat with no transcript yet (``None``) is a worker just starting: kept.
        """
        # Membership, not ``extra_keys()``: a scan asks this for every owned chat.
        if (
            not self.enabled
            or key in self.pinned
            or key in self.companions
            or self._opened_now(key)
        ):
            return True
        return last_write is None or last_write >= self.cutoff()

    def drops(self, session: Session) -> bool:
        """Whether a scanned chat is let go because its Work is finished."""
        if not self.enabled or session.key not in self.finished:
            return False
        if self._opened_now(session.key):
            return False
        return not (
            session.status is SessionStatus.LIVE
            or session.thinking
            or bool(session.question)
        )
