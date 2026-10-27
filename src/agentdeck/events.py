"""EventBus: per-topic asyncio pub/sub feeding SSE.

Publishers never block: each subscriber has a bounded queue and slow
consumers drop the oldest event. Snapshot consumers use ``changed_topics``
to recover invalidations even when another topic displaced their queue entry;
payload consumers retain the bounded, lossy queue contract.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class Subscription:
    """One subscriber queue registered under one or more topics."""

    def __init__(self, bus: EventBus, topics: tuple[str, ...], maxsize: int):
        self._bus = bus
        self.topics = topics
        self._seen_revisions = bus.revision(*topics)
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)

    def listen(self, *topics: str) -> None:
        """Also receive ``topics`` from now on: a chat page that moved to a new leg (#2865)."""
        new = tuple(topic for topic in dict.fromkeys(topics) if topic not in self.topics)
        if not new:
            return
        self.topics += new
        self._seen_revisions += self._bus.revision(*new)
        for topic in new:
            self._bus._subs.setdefault(topic, set()).add(self)

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def get_nowait(self) -> tuple[str, Any] | None:
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def changed_topics(self) -> set[str]:
        """Topics published since the last check (initially, since subscribing).

        Call while subscribed, after draining the wake-up queue. Revisions
        survive overflow and are captured before the consumer renders/yields,
        so a publication during that yield is still owed on the next pass.
        This does not read, consume or reconstruct individual event payloads.
        """
        current = self._bus.revision(*self.topics)
        changed = {
            topic
            for topic, before, after in zip(
                self.topics, self._seen_revisions, current, strict=True
            )
            if before != after
        }
        self._seen_revisions = current
        return changed

    def _deliver(self, topic: str, data: Any) -> None:
        try:
            self.queue.put_nowait((topic, data))
        except asyncio.QueueFull:  # slow consumer: drop oldest, keep newest
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait((topic, data))

    def close(self) -> None:
        """Stop receiving events. Safe to call more than once."""
        self._bus._unsubscribe(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self, maxsize: int = 64):
        self._maxsize = maxsize
        self._subs: dict[str, set[Subscription]] = {}
        self._revisions: dict[str, int] = {}

    def revision(self, *topics: str) -> tuple[int, ...]:
        """How many times each topic has been published.

        A topic is the deck's own statement that what it covers may have moved,
        so this counter is the cheapest honest answer to "is the fragment I
        built for that topic still current?". Anything that changed without a
        publication would not have woken a subscriber to re-render it either,
        which is why the counter is a sound cache key and not merely a hint.
        """
        return tuple(self._revisions.get(topic, 0) for topic in topics)

    def subscribe(self, *topics: str) -> Subscription:
        sub = Subscription(self, topics, self._maxsize)
        for topic in topics:
            self._subs.setdefault(topic, set()).add(sub)
        return sub

    def subscribers(self, topic: str) -> int:
        """How many open subscriptions listen on ``topic``.

        Work nobody is watching is not done (ADR 0007): a view that only exists for
        connected pages asks this before rebuilding.
        """
        return len(self._subs.get(topic, ()))

    def _unsubscribe(self, sub: Subscription) -> None:
        for topic in sub.topics:
            subs = self._subs.get(topic)
            if subs is not None:
                subs.discard(sub)
                # One topic per chat (#2865): an emptied one must not stay behind forever.
                if not subs:
                    del self._subs[topic]

    def publish(self, topic: str, data: Any = None) -> None:
        self._revisions[topic] = self._revisions.get(topic, 0) + 1
        for sub in tuple(self._subs.get(topic, ())):
            sub._deliver(topic, data)
