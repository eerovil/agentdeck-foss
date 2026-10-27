"""Retry a failed watch or pass instead of dropping the change it owed (#2865).

Views no longer re-read on a clock (ADR 0007), so a loop that gives up after one error,
or clears a change before the work it owed succeeded, leaves every page it feeds stale
until something unrelated happens. ADR 0007's rule: a loop clears a change only after
its work succeeded, and retries a failed pass with a back-off. ``Backoff`` is that
back-off for a loop that keeps its own wait; ``retry_forever`` runs a watch that is the
only publisher of its topic, catching up on whatever it missed while nothing watched.
Retry back-offs are one of the clocks ADR 0007 keeps.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

# After a watch fails, wait this long before watching again, doubling up to the cap.
RETRY_MIN_S = 5.0
RETRY_MAX_S = 300.0


class Backoff:
    """The wait after a failed pass: ``RETRY_MIN_S`` doubling up to ``RETRY_MAX_S``."""

    def __init__(self, first: float | None = None, cap: float | None = None) -> None:
        self._first = first
        self._cap = cap
        self.next_s = 0.0

    def failed(self) -> float:
        """Record a failure; how long to wait before trying again."""
        first = RETRY_MIN_S if self._first is None else self._first
        cap = RETRY_MAX_S if self._cap is None else self._cap
        self.next_s = min(max(self.next_s * 2, first), cap)
        return self.next_s

    def succeeded(self) -> None:
        self.next_s = 0.0


async def retry_forever(
    watch: Callable[[], Awaitable[None]],
    *,
    name: str,
    caught_up: Callable[[], None] | None = None,
) -> None:
    """Run ``watch()`` again whenever it returns or raises; never return.

    A clean return (the watch restarting on a new set of paths) opens it again at once
    and resets the back-off. A raise waits the back-off, then calls ``caught_up``, so a
    write made while nothing watched is still announced.
    """
    backoff = Backoff()
    while True:
        try:
            await watch()
            backoff.succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- retried: nothing else would publish
            wait = backoff.failed()
            log.warning("%s watch failed, retrying in %.0fs: %s", name, wait, exc)
            await asyncio.sleep(wait)
            if caught_up is not None:
                caught_up()
