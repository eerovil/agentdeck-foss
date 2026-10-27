"""The poller board watch never stops watching for good (#2865).

Nothing else publishes ``board``: the chat pages and the Work overview stopped re-reading
the board on a clock, so a watch that returned left every open page on an old board until
the web process restarted.
"""

from __future__ import annotations

import asyncio

import pytest

from agentdeck import board_watch, watch_retry
from agentdeck.events import EventBus

pytestmark = pytest.mark.asyncio


async def _published(sub, timeout: float = 10.0) -> None:
    await asyncio.wait_for(sub.get(), timeout)


def _quick_awatch(monkeypatch) -> asyncio.Queue:
    """Swap in `awatch` with a short debounce; queue each watch's paths once it is open."""
    real_awatch = board_watch.awatch
    opened: asyncio.Queue = asyncio.Queue()

    async def quick_awatch(*paths, **kwargs):
        changes = real_awatch(*paths, **{**kwargs, "debounce": 20, "step": 10})
        # `awatch` opens its watch before its first suspension: announce it after that.
        asyncio.get_running_loop().call_soon(opened.put_nowait, paths)
        async for batch in changes:
            yield batch

    monkeypatch.setattr(board_watch, "awatch", quick_awatch)
    return opened


async def _opened(opened: asyncio.Queue, *paths) -> None:
    """Wait until a watch on exactly ``paths`` is open."""
    want = tuple(str(path) for path in paths)
    while tuple(map(str, await asyncio.wait_for(opened.get(), 10.0))) != want:
        pass


async def test_a_board_installed_after_start_is_watched(tmp_path, monkeypatch):
    """No instances at start: the watch waits for one instead of returning."""
    root = tmp_path / "poller"
    opened = _quick_awatch(monkeypatch)
    bus = EventBus()
    watch = board_watch.BoardWatch(root, bus)
    with bus.subscribe(board_watch.BOARD_TOPIC) as sub:
        watch.start()
        try:
            await _opened(opened, tmp_path)  # waiting on the nearest existing directory
            (root / "instances" / "agentdeck").mkdir(parents=True)
            (root / "instances" / "agentdeck" / "state.json").write_text("{}")
            await _published(sub)
            while sub.get_nowait() is not None:
                pass
            await _opened(opened, root / "instances")  # moved onto the instances tree
            (root / "instances" / "agentdeck" / "state.json").write_text('{"cards": {}}')
            await _published(sub)
        finally:
            await watch.stop()


async def test_a_failed_watch_is_retried_and_catches_up(tmp_path, monkeypatch):
    """The watch raising backs off and watches again; a write made meanwhile is published."""
    root = tmp_path / "poller"
    state = root / "instances" / "agentdeck" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")
    calls = {"n": 0}
    opened = _quick_awatch(monkeypatch)
    real_awatch = board_watch.awatch

    def flaky_awatch(*paths, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            state.write_text('{"moved": true}')  # lands while nothing watches
            raise OSError("inotify watch limit reached")
        return real_awatch(*paths, **kwargs)

    monkeypatch.setattr(board_watch, "awatch", flaky_awatch)
    monkeypatch.setattr(watch_retry, "RETRY_MIN_S", 0.05)
    bus = EventBus()
    watch = board_watch.BoardWatch(root, bus)
    with bus.subscribe(board_watch.BOARD_TOPIC) as sub:
        watch.start()
        try:
            await _published(sub)  # the catch-up after the back-off
            await _opened(opened, state.parent.parent)  # watching again
            assert not watch._task.done()
            state.write_text('{"moved": "again"}')
            await _published(sub)
            assert calls["n"] == 2
        finally:
            await watch.stop()


async def test_a_write_before_the_watch_opens_is_caught_up(tmp_path, monkeypatch):
    """Observe before catching up (#2865): the start-up look ran before `awatch` opened its
    watch, so a board write landing between the two was neither seen nor caught up."""
    root = tmp_path / "poller"
    state = root / "instances" / "agentdeck" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}")
    real_awatch = board_watch.awatch

    def awatch_after_a_write(*paths, **kwargs):
        state.write_text('{"moved": "in the gap"}')  # after the first look, before the watch
        return real_awatch(*paths, **kwargs)

    monkeypatch.setattr(board_watch, "awatch", awatch_after_a_write)
    bus = EventBus()
    watch = board_watch.BoardWatch(root, bus)
    with bus.subscribe(board_watch.BOARD_TOPIC) as sub:
        watch.start()
        try:
            await _published(sub, timeout=3.0)
        finally:
            await watch.stop()
