"""The runtime's change feed (#2865): the web reads runtime state when it moved.

Driven through the real pieces — a worker host's stdout, the runtime app's routes and a
web-side client over an in-process transport — and asserting the reaction, never seconds.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI
from worker_harness import claude_host
from worker_harness import settle as _settle

from agentdeck import runtime_feed
from agentdeck.config import AppConfig
from agentdeck.runtime import create_runtime_app
from agentdeck.runtime_feed import (
    ALL,
    FEED,
    JOBS,
    LEGACY,
    RELATIONS,
    ChangeCheck,
    ChangeFeed,
    RuntimeFeedClient,
    workers_item,
)

ITEM = workers_item("claude_code", "test")


def _watched_host(tmp_path, **kwargs):
    host, spawned = claude_host(tmp_path, **kwargs)
    feed = ChangeFeed()
    host.on_state_change = lambda key: feed.mark(ITEM, ALL if key is None else key)
    feed.add_check(
        ChangeCheck(ITEM, host.change_keys, host.change_entry, deadline=host.next_change_at)
    )
    return host, spawned, feed


def _moved_since(feed: ChangeFeed, revision: int) -> list[str]:
    """The items bumped after ``revision``, flushing any marks still pending.

    The feed flushes marks itself ``CHECK_COALESCE_S`` after the first one, so on a slow
    host that timer can run the checks before the test does (#3716): ask the feed what
    moved, not which run of the checks saw it.
    """
    feed.run_checks()
    return feed.changed_since(revision)


async def test_a_held_read_is_answered_by_a_bump_and_names_what_moved():
    feed = ChangeFeed()
    first = await feed.wait(None, None, timeout=5)
    assert first["reset"] is True

    held = asyncio.create_task(feed.wait(first["revision"], first["epoch"], timeout=30))
    await _settle()
    assert not held.done()
    feed.bump(JOBS)
    answer = await asyncio.wait_for(held, 1)
    assert answer["changed"] == [JOBS] and answer["reset"] is False


async def test_a_read_from_another_runtime_is_a_reset():
    feed = ChangeFeed()
    feed.bump(JOBS)
    answer = await feed.wait(1, "an-older-runtime", timeout=30)
    assert answer["reset"] is True


async def test_a_keep_alive_with_nothing_changed_names_nothing():
    feed = ChangeFeed()
    answer = await feed.wait(0, feed.epoch, timeout=0.01)
    assert answer == {"epoch": feed.epoch, "revision": 0, "reset": False, "changed": []}


async def test_shutdown_answers_held_reads_at_once():
    feed = ChangeFeed()
    held = asyncio.create_task(feed.wait(0, feed.epoch, timeout=30))
    await _settle()
    feed.shutdown_soon()
    answer = await asyncio.wait_for(held, 1)
    assert answer["changed"] == []
    # And a read arriving during the shutdown is not held at all.
    assert (await asyncio.wait_for(feed.wait(0, feed.epoch, timeout=30), 1))["changed"] == []


async def test_a_check_bumps_only_when_its_projection_moves():
    values = {"a": 1, "b": 2}
    feed = ChangeFeed()
    feed.add_check(ChangeCheck("x", lambda: list(values), values.get))
    feed.mark("x", "a")
    assert feed.run_checks() == []
    values["a"] = 3
    feed.mark("x", "a")
    assert feed.run_checks() == ["x"]
    del values["b"]
    feed.mark("x")
    assert feed.run_checks() == ["x"]
    assert feed.revision == 2


async def test_a_streamed_line_moves_nothing_but_the_turn_ending_does(tmp_path):
    """The web reads no activity clock, so a working turn costs no snapshot fetch."""
    host, spawned, feed = _watched_host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    feed.run_checks()
    process = spawned[0]["process"]

    revision = feed.revision
    for _ in range(3):
        process.stdout.emit({"type": "assistant", "message": {"content": []}})
    await _settle()
    assert _moved_since(feed, revision) == []

    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert _moved_since(feed, revision) == [ITEM]
    await host.stop()


async def test_a_stall_is_announced_when_it_falls_due(tmp_path):
    """A stalled turn prints nothing, so its badge is a deadline, not an event."""
    host, _spawned, feed = _watched_host(tmp_path, stall_after_s=0.3)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    feed.run_checks()
    due = host.next_change_at()
    assert due is not None and due > time.time()

    answer = await asyncio.wait_for(feed.wait(feed.revision, feed.epoch, timeout=5), 3)
    assert answer["changed"] == [ITEM]
    assert next(iter(host.snapshot()["workers"].values()))["stalled"] is True
    await host.stop()


def _runtime_app(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path / "workers")},
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": str(tmp_path)}
            ],
            "history": {"db_path": str(tmp_path / "deck.db")},
            "jobs": {"state_dir": str(tmp_path / "jobs")},
            "keyed_workers": {"state_dir": str(tmp_path / "keyed")},
        }
    )
    return create_runtime_app(cfg)


async def test_the_runtime_serves_its_feed_and_wires_its_sources(tmp_path):
    app = _runtime_app(tmp_path)
    feed = app.state.change_feed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        first = (await client.get("/changes")).json()
        assert first["reset"] is True
        held = asyncio.create_task(
            client.get(
                "/changes",
                params={"since": first["revision"], "epoch": first["epoch"], "wait": 30},
            )
        )
        await _settle()
        # A job saved is a job changed.
        app.state.jobs.on_change()
        answer = (await asyncio.wait_for(held, 2)).json()
        assert JOBS in answer["changed"]

    # A Claude host is watched from the moment it exists.
    app.state.claude_workers.host("main")
    assert workers_item("claude_code", "main") in feed._checks
    assert RELATIONS in feed._checks

    # #3008: the boot's revival scan ending moves what the deck reads of that account's
    # workers (the revivals owed) with no worker changing, so the feed bumps it.
    feed.run_checks()
    revision = feed.revision
    nudges = app.state.restart_nudges
    nudges._revival_scanned = True
    nudges._revivals_changed()
    await _settle()
    assert workers_item("claude_code", "main") in _moved_since(feed, revision)
    revision = feed.revision
    nudges._revivals_changed()  # nothing moved: no bump
    await _settle()
    assert _moved_since(feed, revision) == []


async def _changes_app(feed: ChangeFeed) -> FastAPI:
    app = FastAPI()

    @app.get(runtime_feed.CHANGES_PATH)
    async def changes(since: int | None = None, epoch: str | None = None, wait: float = 0):
        return await feed.wait(since, epoch, min(wait, 5))

    return app


async def test_the_web_wakes_only_the_readers_of_what_moved():
    feed = ChangeFeed()
    client = RuntimeFeedClient(transport=httpx.ASGITransport(app=await _changes_app(feed)))
    published: list[frozenset[str]] = []
    client.add_listener(published.append)
    jobs = client.subscribe(JOBS)
    workers = client.subscribe(ITEM)
    try:
        # Connecting is the catch-up: every reader reads once.
        await asyncio.wait_for(jobs.wait(60), 2)
        await asyncio.wait_for(workers.wait(60), 2)
        assert client.mode == FEED
        published.clear()

        feed.bump(JOBS)
        await asyncio.wait_for(jobs.wait(60), 2)
        await _settle()
        assert not workers._event.is_set()
        assert published == [frozenset({JOBS})]

        feed.bump(ITEM)
        await asyncio.wait_for(workers.wait(60), 2)
        assert not jobs._event.is_set()
    finally:
        jobs.close()
        workers.close()


async def test_a_runtime_without_the_feed_keeps_the_old_cadence():
    client = RuntimeFeedClient(transport=httpx.ASGITransport(app=FastAPI()))
    sub = client.subscribe(JOBS)
    try:
        for _ in range(50):
            if client.mode == LEGACY:
                break
            await asyncio.sleep(0.01)
        assert client.mode == LEGACY
        started = time.monotonic()
        await asyncio.wait_for(sub.wait(0.05), 2)  # the fallback, not a hang
        assert time.monotonic() - started < 1.5
    finally:
        sub.close()
    assert client._task is None
