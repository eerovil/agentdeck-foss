"""Render a shared SSE fragment once per event instead of once per client.

Every attached board wakes from the same ``EventBus`` publication, and until
#1357 each one then rebuilt byte-identical HTML on its own: the topbar meters
about twenty times a minute on the host sampler alone, and every admitted
session card on every ``sessions`` event. #1344 stopped *sending* an unchanged
card; nothing stopped building it. The only genuinely per-client part is which
already-built cards a stream still owes its reader, which is cheap.

The key is the bus revision of the topics that can change the fragment, plus
whatever really does differ per client (the selected chat, the history window,
the account the topbar highlights). A hit therefore means "nothing has announced
a change to this since it was built" — and a change nobody announced would not
have woken a client to re-render it either, so the cache cannot serve HTML that
an uncached deck would have replaced.

Clock-derived text is deliberately not part of any key. The usage meters'
"updated Ns ago" is ticked in the browser off ``data-since``, so a server render
reused across a quiet window shows the reader the same thing a fresh one would.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

# Room for every shared fragment on the deck at several distinct per-client keys
# at once, with headroom well past the three devices this deck is sized for.
# Bounded at all because the per-client part of a key is attacker-visible
# (``history_limit`` rides in on the query string), so the cache must not be
# able to grow with the number of connections. A whole session list is hundreds
# of kilobytes of HTML, so this ceiling is memory, not just tidiness.
DEFAULT_MAXSIZE = 32


class SharedFragments:
    """A tiny keyed memo shared by every SSE stream in one web worker."""

    def __init__(self, bus, *, maxsize: int = DEFAULT_MAXSIZE):
        self._bus = bus
        self._maxsize = maxsize
        # slot -> (topic revisions it was built at, the render). One slot holds
        # one render: a fresh revision replaces the old HTML rather than piling
        # up beside it, so a busy deck cannot accumulate megabytes of fragments
        # nothing will ever be served again.
        self._entries: OrderedDict[tuple, tuple[tuple[int, ...], Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(
        self,
        name: str,
        topics: Sequence[str],
        key: tuple,
        build: Callable[[], Any],
    ) -> Any:
        """``build()`` once per (topic revision, key); every later caller reuses it."""
        slot = (name, key)
        revision = self._bus.revision(*topics)
        entry = self._entries.get(slot)
        if entry is not None and entry[0] == revision:
            self.hits += 1
            self._entries.move_to_end(slot)
            return entry[1]
        self.misses += 1
        value = build()
        self._entries[slot] = (revision, value)
        self._entries.move_to_end(slot)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)
        return value
