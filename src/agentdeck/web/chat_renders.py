"""One transcript read and one render per chat change, shared by every tab on it (#2865).

Each open tab of a chat holds its own stream, because what each one still owes its reader
is genuinely its own: the cursor it resumed from, the primes it has sent, the fragments
whose signatures it already pushed. What every tab woken by the same change would build is
identical, though — the transcript bytes appended since a cursor, their HTML, and a live
fragment's HTML for one signature — and until this each tab built all of it again. ADR 0007
asks that an update be built once and sent to every tab; this is where that happens.

The key always carries the bus revision of the topics that can change the result, so a hit
means "nothing announced a change to this since it was built" — the same contract as
``SharedFragments``. A tail read is also shared while it is still running: two tabs woken
by one publication wait on one read rather than racing two.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

from .. import work_counter

# A handful of open chats, each with a tail entry per distinct cursor and a few fragments.
# Bounded so the number of connections cannot grow it.
DEFAULT_MAXSIZE = 64


class ChatRenders:
    """A keyed memo of chat-page builds, shared by every stream in one web worker."""

    def __init__(self, *, maxsize: int = DEFAULT_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[Hashable, Any] = OrderedDict()
        self._running: dict[Hashable, asyncio.Future] = {}

    def _keep(self, key: Hashable, value: Any) -> None:
        self._entries[key] = value
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)

    async def tail(self, key: tuple, read: Callable[[], Awaitable[Any]]) -> Any:
        """``read()`` once per key; a tab asking while it runs waits for the same answer.

        ``key`` names the leg, the cursor read from and the revisions that announce a
        write to it.
        """
        return await self._once(("tail", *key), read, work_counter.CHAT_TAIL)

    async def catch_up(self, key: tuple, read: Callable[[], Awaitable[Any]]) -> Any:
        """``read()`` once per key: a leg move's replay from one cursor onto another leg.

        An account switch moves every open tab on the chat at once, and each would replay
        the same legs and re-seed the same activity from them. ``key`` names the chain,
        the cursor left, the leg moved to and the revisions of the leg being left. An
        answer kept after the new leg was written to is still a consistent cursor: the
        tab reads the rest on its next tail.
        """
        return await self._once(("catch-up", *key), read, work_counter.CHAT_CATCH_UP)

    async def _once(self, slot: Hashable, read: Callable[[], Awaitable[Any]], step: str) -> Any:
        """The read runs as its own task, so a tab that closes while waiting cannot
        cancel it under the others. A failed read is not kept, so the next tab tries
        again."""
        if slot in self._entries:
            self._entries.move_to_end(slot)
            return self._entries[slot]
        task = self._running.get(slot)
        if task is None:
            work_counter.count(step)
            task = asyncio.ensure_future(read())
            self._running[slot] = task

            def settled(done: asyncio.Future, slot=slot) -> None:
                if self._running.get(slot) is done:
                    del self._running[slot]
                if not done.cancelled() and done.exception() is None:
                    self._keep(slot, done.result())

            task.add_done_callback(settled)
        return await asyncio.shield(task)

    def fragment(self, key: tuple, build: Callable[[], str]) -> str:
        """``build()`` once per key: the fragment, its signature and the view's revisions.

        A signature that cannot be hashed is simply built, never shared.
        """
        slot = ("fragment", *key)
        try:
            cached = self._entries.get(slot)
        except TypeError:
            work_counter.count(work_counter.CHAT_FRAGMENT)
            return build()
        if cached is not None:
            self._entries.move_to_end(slot)
            return cached
        work_counter.count(work_counter.CHAT_FRAGMENT)
        html = build()
        self._keep(slot, html)
        return html
