"""Keep the declared repositories' issue and pull request lists warm (#1115).

The front page's board strip reads the shared GitHub cache and deliberately
never refreshes it: a page render must not spend GitHub quota. Something else
has to fill that cache. It used to fall out of a background agent's own sweep,
which meant switching that agent off left nothing refreshing the lists at all:
they went days old, every card filed since read as a closed issue, and a whole
Poller Instance vanished from the strip.

So the refresh belongs here instead: to the deck, not to a feature that may be
turned off. It is one ``gh issue list`` and one ``gh pr list`` per declared
repository per cycle, through the same shared, rate-limited, cross-process cache
every other reader goes through, so a second process refreshing at the same
moment costs one lease rather than a second call.

Web-process owned and restart-safe by construction: it holds no state of its own,
and the cache it fills is a file every process shares.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

log = logging.getLogger(__name__)


class IssueRefreshService:
    """Refresh the shared issue/pull cache on a timer."""

    def __init__(self, issues, *, interval_s: float, enabled: bool = True) -> None:
        self._issues = issues
        self._interval_s = float(interval_s)
        # A non-positive interval is the operator turning the loop off, not a
        # busy loop to guess a default for.
        self._enabled = bool(enabled) and self._interval_s > 0
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="issue-list-refresh")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        # Refresh once at startup before the first sleep. A web restart is
        # exactly when the operator is most likely to be looking at the front
        # page, and an interval-first loop would show them the old cache for a
        # full cycle.
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._interval_s)

    async def refresh_once(self) -> None:
        """One pass, with its own failure swallowed.

        The cache treats a failed fetch as "keep the old value and back the key
        off", so a GitHub outage costs freshness rather than the loop.
        """
        try:
            await self._issues.resolve()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one failed refresh must not kill the loop
            log.exception("Issue list refresh failed")
