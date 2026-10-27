"""The runtime's change feed: the web learns *that* runtime state moved (#2865).

The web process used to pull every account's worker snapshot from the persistent runtime
once a second — about 3.5 MB a second here — only to find, almost every time, that nothing
had moved. The heavy-command queue, the relations the runtime writes and the delegation
takeover each kept a timer of their own as well. ADR 0007 replaces all of them with one
held request.

**The runtime half** (:class:`ChangeFeed`) keeps one change number. Whatever might have
moved *marks* a :class:`ChangeCheck` — a worker's stdout event, a process exit, a record
saved, any control request, the job queue saving, a deadline falling due — and the check
compares a cheap per-key projection of what the web reads against the last one it saw.
Only a real difference *bumps* the number, so a streaming turn whose ``last_activity_at``
moves forty times a second costs no fetch at all. ``GET /changes?since=<n>`` is answered
as soon as the number moves past ``n``, or after :data:`KEEPALIVE_S` with nothing changed
(which does no work), and names the items that moved.

**The web half** (:class:`RuntimeFeedClient`) holds that one request open, and each
consumer waits on a :class:`FeedSubscription` for the items it reads and then reads them.
A (re)connect wakes every subscription — that read is the catch-up for anything a dropped
connection hid. A runtime from before the feed answers 404; then, and while the runtime
cannot be reached at all, a subscription falls back to its consumer's old cadence, because
the runtime restarts later than the web during a deploy (ADR 0007, "Consequences").

Items are ``<kind>`` or ``<kind>:<scope>``; the kinds are :data:`WORKERS`, :data:`JOBS`,
:data:`RELATIONS` and :data:`RUNS`. Every change the client learns of is also published
on the web's :class:`~agentdeck.events.EventBus` under :data:`RUNTIME_TOPIC`, with the
frozenset of kinds that moved, for views (the Work overview) that read runtime rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Hashable, Iterable
from typing import Any

import httpx

from .runtime_socket import runtime_socket_path

log = logging.getLogger(__name__)

RUNTIME_TOPIC = "runtime"
CHANGES_PATH = "/changes"

WORKERS = "workers"
JOBS = "jobs"
RELATIONS = "relations"
RUNS = "runs"
KINDS = frozenset({WORKERS, JOBS, RELATIONS, RUNS})

# How long the runtime holds a request with nothing to say. It is a keep-alive, not a
# poll: answering it reads nothing.
KEEPALIVE_S = 50.0
# Marks are gathered for this long before the checks run, so a burst of stdout events
# from one worker is one comparison rather than one per line.
CHECK_COALESCE_S = 0.1
# Reconnect backoff while the runtime is unreachable (it is restarting, usually).
RECONNECT_MIN_S = 0.5
RECONNECT_MAX_S = 5.0
# A runtime without the feed is asked again this often, so an upgraded runtime is
# noticed even when nothing else breaks the connection.
LEGACY_REPROBE_S = 60.0

ALL: Any = object()  # mark every key of a check


def workers_item(provider_id: str, label: str) -> str:
    """The feed item for one account's keyed workers (and, for Codex, its threads)."""
    return f"{WORKERS}:{provider_id}:{label}"


def kind_of(item: str) -> str:
    return item.split(":", 1)[0]


# --- runtime side -------------------------------------------------------------------------


class ChangeCheck:
    """One fingerprinted projection whose change bumps one feed item.

    ``keys`` lists what there is; ``entry(key)`` is what the web would read of that key,
    comparable with ``==`` and without volatile clocks. A mark of one key recomputes only
    that key; a mark of :data:`ALL` recomputes everything and notices removed keys.
    ``deadline`` answers the wall-clock time of the next *time-derived* change (a stall
    badge falling due), which the feed turns into a mark at exactly that moment.
    """

    def __init__(
        self,
        item: str,
        keys: Callable[[], Iterable[Hashable]],
        entry: Callable[[Hashable], object],
        *,
        deadline: Callable[[], float | None] | None = None,
    ) -> None:
        self.item = item
        self._keys = keys
        self._entry = entry
        self._deadline = deadline
        self._seen: dict[Hashable, object] | None = None
        self._deadline_handle: asyncio.TimerHandle | None = None

    def run(self, marked: set[Hashable] | object) -> bool:
        """Recompute the marked keys; whether anything the web reads moved."""
        if self._seen is None or marked is ALL:
            current = {key: self._entry(key) for key in self._keys()}
            changed = self._seen is not None and current != self._seen
            self._seen = current
            return changed
        changed = False
        for key in marked:  # type: ignore[union-attr]
            value = self._entry(key)
            if value is None:
                if self._seen.pop(key, None) is not None:
                    changed = True
            elif self._seen.get(key) != value:
                self._seen[key] = value
                changed = True
        return changed

    def next_deadline(self) -> float | None:
        if self._deadline is None:
            return None
        value = self._deadline()
        return value if value is not None and value > time.time() else None


class ChangeFeed:
    """The runtime's change number, the checks that move it, and the held readers."""

    def __init__(self) -> None:
        # A fresh epoch per process: a reader holding a number from the previous runtime
        # must not mistake this one's small numbers for "nothing new".
        self.epoch = uuid.uuid4().hex
        self.revision = 0
        self._moved_at: dict[str, int] = {}
        self._waiters: set[asyncio.Future[None]] = set()
        self._checks: dict[str, ChangeCheck] = {}
        self._dirty: dict[str, set[Hashable] | object] = {}
        self._flush: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: int | None = None
        self._closing = False

    # -- threading: stores written from ``asyncio.to_thread`` mark and bump too ---------

    def _on_loop(self, fn: Callable[..., None], *args: Any) -> None:
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
                self._thread = threading.get_ident()
            except RuntimeError:
                fn(*args)  # no loop yet: nobody can be waiting, nothing to schedule
                return
        if threading.get_ident() == self._thread:
            fn(*args)
        elif not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    # -- bumping --------------------------------------------------------------------------

    def bump(self, *items: str) -> None:
        """Say these items moved. Safe from any thread."""
        if items:
            self._on_loop(self._bump, items)

    def _bump(self, items: tuple[str, ...]) -> None:
        self.revision += 1
        for item in items:
            self._moved_at[item] = self.revision
        self._release_waiters()

    def changed_since(self, since: int) -> list[str]:
        return sorted(item for item, rev in self._moved_at.items() if rev > since)

    async def wait(self, since: int | None, epoch: str | None, timeout: float) -> dict[str, Any]:
        """Answer as soon as the number moves past ``since``, or after ``timeout``."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._thread = threading.get_ident()
        reset = since is None or epoch != self.epoch or since > self.revision
        if not reset and since == self.revision and timeout > 0 and not self._closing:
            waiter = self._loop.create_future()
            self._waiters.add(waiter)
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout)
            except TimeoutError:
                pass
            finally:
                self._waiters.discard(waiter)
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "reset": reset,
            "changed": [] if reset else self.changed_since(since or 0),
        }

    # -- checks ---------------------------------------------------------------------------

    def add_check(self, check: ChangeCheck) -> ChangeCheck:
        """Register a projection and record what there is now, so the next change counts."""
        self._checks[check.item] = check
        try:
            check.run(ALL)
        except Exception:  # noqa: BLE001 -- the first mark records it instead
            log.exception("runtime change check %s failed", check.item)
        if self._loop is not None:
            self._on_loop(self._schedule_deadline, check)
        return check

    def mark(self, item: str, key: Hashable | object = ALL) -> None:
        """Something about ``item`` (one ``key`` of it, or all) may have moved."""
        self._on_loop(self._mark, item, key)

    def mark_all(self) -> None:
        """Anything may have moved: every check runs in full (a control request ended)."""
        self._on_loop(self._mark_all)

    def _mark_all(self) -> None:
        for item in self._checks:
            self._mark(item, ALL)

    def _mark(self, item: str, key: Hashable | object) -> None:
        if item not in self._checks:
            return
        dirty = self._dirty.get(item)
        if key is ALL or dirty is ALL:
            self._dirty[item] = ALL
        else:
            if dirty is None:
                dirty = self._dirty[item] = set()
            dirty.add(key)  # type: ignore[union-attr]
        if self._flush is None and self._loop is not None:
            self._flush = self._loop.call_later(CHECK_COALESCE_S, self.run_checks)

    def run_checks(self) -> list[str]:
        """Run every marked check now; the items that moved (and were bumped)."""
        self._flush = None
        dirty, self._dirty = self._dirty, {}
        moved = []
        for item, keys in dirty.items():
            check = self._checks.get(item)
            if check is None:
                continue
            try:
                if check.run(keys):
                    moved.append(item)
            except Exception:  # noqa: BLE001 -- unreadable is "may have moved", never stuck
                log.exception("runtime change check %s failed", item)
                moved.append(item)
            self._schedule_deadline(check)
        if moved:
            self._bump(tuple(moved))
        return moved

    def _schedule_deadline(self, check: ChangeCheck) -> None:
        if check._deadline_handle is not None:
            check._deadline_handle.cancel()
            check._deadline_handle = None
        try:
            due = check.next_deadline()
        except Exception:  # noqa: BLE001
            log.exception("runtime change deadline %s failed", check.item)
            return
        if due is None or self._loop is None:
            return
        check._deadline_handle = self._loop.call_later(
            max(0.0, due - time.time()) + 0.01, self._mark, check.item, ALL
        )

    def shutdown_soon(self) -> None:
        """Answer every held request now: the server is stopping (#2865).

        Uvicorn waits for in-flight requests before it runs the lifespan's shutdown, so a
        request held for :data:`KEEPALIVE_S` would hold a runtime restart — and every
        chat it stops — that long. Called from the signal handler, so only a flag and a
        thread-safe hop onto the loop.
        """
        self._closing = True
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._release_waiters)

    def _release_waiters(self) -> None:
        waiters, self._waiters = self._waiters, set()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    def close(self) -> None:
        if self._flush is not None:
            self._flush.cancel()
            self._flush = None
        for check in self._checks.values():
            if check._deadline_handle is not None:
                check._deadline_handle.cancel()
                check._deadline_handle = None
        for waiter in self._waiters:
            if not waiter.done():
                waiter.cancel()
        self._waiters.clear()


# --- web side -----------------------------------------------------------------------------

FEED = "feed"  # connected to a runtime that serves the feed
LEGACY = "legacy"  # the runtime answered 404: it predates the feed
DOWN = "down"  # not connected (yet, or the runtime is unreachable)


class FeedSubscription:
    """One consumer's wait for the feed items it reads."""

    def __init__(self, feed: RuntimeFeedClient, match: Callable[[str], bool]) -> None:
        self._feed = feed
        self._match = match
        self._event = asyncio.Event()
        self.closed = False

    def matches(self, item: str) -> bool:
        return self._match(item)

    def notify(self) -> None:
        self._event.set()

    async def wait(
        self, fallback_s: float, *, timeout: float | None = None, retry: bool = False
    ) -> None:
        """Return once something this consumer reads may have moved.

        While the runtime serves the feed that is exactly when one of its items moved (or
        the feed reconnected). A runtime without the feed, or no runtime at all, gets the
        consumer's old cadence ``fallback_s`` instead. ``timeout`` bounds either wait for a
        consumer with a deadline of its own.
        """
        self._feed._ensure_running()
        # ``retry``: the consumer's last read failed. The feed only says when the runtime
        # changed, not that a read of it failed, so that read is tried again on the old
        # cadence rather than at the next unrelated change (#2865).
        limit = None if self._feed.mode == FEED and not retry else fallback_s
        if timeout is not None:
            limit = timeout if limit is None else min(limit, timeout)
        if limit is None:
            await self._event.wait()
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event.wait(), max(0.0, limit))
        # Cleared on the way out, before the caller reads: a change landing during that
        # read sets it again and is owed on the next wait.
        self._event.clear()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._feed._unsubscribe(self)

    def __enter__(self) -> FeedSubscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class RuntimeFeedClient:
    """The web process's one held ``GET /changes``, fanned out to subscriptions."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self.mode = DOWN
        self._subs: set[FeedSubscription] = set()
        self._listeners: list[Callable[[frozenset[str]], None]] = []
        self._task: asyncio.Task | None = None

    def subscribe(self, items: str | Iterable[str] | Callable[[str], bool]) -> FeedSubscription:
        """A subscription woken when any of ``items`` (exact items or kinds) moves."""
        if callable(items):
            match = items
        else:
            wanted = frozenset([items] if isinstance(items, str) else items)

            def match(item: str) -> bool:
                return item in wanted or kind_of(item) in wanted

        sub = FeedSubscription(self, match)
        self._subs.add(sub)
        self._ensure_running()
        return sub

    def add_listener(self, listener: Callable[[frozenset[str]], None]) -> Callable[[], None]:
        """Call ``listener(kinds)`` on every change learned; returns its remover."""
        self._listeners.append(listener)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return remove

    def _unsubscribe(self, sub: FeedSubscription) -> None:
        self._subs.discard(sub)
        if not self._subs and self._task is not None:
            self._task.cancel()
            self._task = None
            self.mode = DOWN

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            if not self._subs:
                return
            self._task = asyncio.create_task(self._run(), name="runtime-change-feed")

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            # A waiter parked under the old rule has to re-decide under the new one.
            for sub in list(self._subs):
                sub.notify()

    def _deliver(self, items: Iterable[str] | None) -> None:
        """Wake the subscriptions these items concern (``None``: every one)."""
        items = None if items is None else list(items)
        for sub in list(self._subs):
            if items is None or any(sub.matches(item) for item in items):
                sub.notify()
        kinds = KINDS if items is None else frozenset(kind_of(item) for item in items)
        if kinds:
            for listener in list(self._listeners):
                try:
                    listener(frozenset(kinds))
                except Exception:  # noqa: BLE001 -- a listener never stops the feed
                    log.exception("runtime feed listener failed")

    def _client(self) -> httpx.AsyncClient:
        transport = self._transport or httpx.AsyncHTTPTransport(uds=str(runtime_socket_path()))
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(10.0, read=KEEPALIVE_S + 15.0),
        )

    async def _run(self) -> None:
        since: int | None = None
        epoch: str | None = None
        backoff = RECONNECT_MIN_S
        client = self._client()
        try:
            while self._subs:
                params: dict[str, Any] = {"wait": KEEPALIVE_S}
                if since is not None and epoch is not None:
                    params.update(since=since, epoch=epoch)
                try:
                    response = await client.get(CHANGES_PATH, params=params)
                    if response.status_code == 404:
                        since = epoch = None
                        if self.mode != LEGACY:
                            log.info("the runtime has no change feed; reading on the old cadence")
                            self._set_mode(LEGACY)
                        await asyncio.sleep(LEGACY_REPROBE_S)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    revision = int(data["revision"])
                    new_epoch = str(data["epoch"])
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    if self.mode == FEED:
                        log.info("runtime change feed lost: %s", exc)
                        # Everything is suspect: let every consumer find out for itself.
                        self._deliver(None)
                    since = epoch = None
                    self._set_mode(DOWN)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX_S)
                    continue
                backoff = RECONNECT_MIN_S
                caught_up = since is None or bool(data.get("reset")) or new_epoch != epoch
                since, epoch = revision, new_epoch
                self._set_mode(FEED)
                if caught_up:
                    # The catch-up read (ADR 0007): whatever a dropped connection hid.
                    self._deliver(None)
                else:
                    changed = data.get("changed")
                    if isinstance(changed, list) and changed:
                        self._deliver(str(item) for item in changed)
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()


_shared: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, RuntimeFeedClient] = (
    weakref.WeakKeyDictionary()
)


def shared() -> RuntimeFeedClient:
    """This event loop's feed client over the runtime socket (one per web process)."""
    loop = asyncio.get_running_loop()
    feed = _shared.get(loop)
    if feed is None:
        feed = _shared[loop] = RuntimeFeedClient()
    return feed
