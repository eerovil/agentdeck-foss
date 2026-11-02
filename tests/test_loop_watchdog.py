"""The loop watchdog names a call that holds the event loop (#3970)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from agentdeck import loop_watchdog


def _hold_the_loop() -> None:
    time.sleep(0.15)


async def _watched(caplog, body, report_s: float = 1.0, blocks: int = 1) -> list[logging.LogRecord]:
    caplog.set_level(logging.INFO, logger="agentdeck.loop_watchdog")
    watcher = asyncio.create_task(
        loop_watchdog.watch(blocked_s=0.05, tick_s=0.01, report_s=report_s)
    )
    for _ in range(blocks):
        await asyncio.sleep(0.02)
        body()
    await asyncio.sleep(0.02)
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(("report_s", "blocks"), [(1.0, 1), (0.1, 1), (0.1, 2)])
async def test_a_held_loop_logs_its_stack_once_and_says_for_how_long(caplog, report_s, blocks):
    """Each block is told once, a second block in a row again (#3993)."""
    records = await _watched(caplog, _hold_the_loop, report_s, blocks)
    held = [r for r in records if r.getMessage().startswith("event loop held for over")]

    # #3993: a block reaching ``report_s`` is also an error, which error reporting sends to
    # Sentry, written while the block lasts: a person restarting a frozen deck ends it first.
    levels = [r.levelno for r in held]
    assert levels.count(logging.ERROR) == (0 if report_s > 0.15 else blocks), "once per block"
    assert 0 < len(levels) and levels.count(logging.WARNING) <= blocks, "a slow look may skip it"
    assert all("_hold_the_loop" in r.getMessage() for r in held), "the stack names the call"
    ended = [r for r in records if r.getMessage().startswith("event loop was blocked for")]
    assert [r.levelno for r in ended] == [logging.WARNING] * blocks


async def test_a_running_loop_stays_quiet_and_a_cancel_stops_the_thread(caplog):
    assert await _watched(caplog, lambda: None) == []

    time.sleep(0.03)
    assert not [t for t in threading.enumerate() if t.name == "loop-watchdog"]
