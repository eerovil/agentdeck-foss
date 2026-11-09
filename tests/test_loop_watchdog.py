"""The loop watchdog names a call that holds the event loop (#3970)."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import anyio.to_thread
import pytest
import sentry_sdk.worker

from agentdeck import loop_watchdog


def _held(caplog, level: int) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == level and r.getMessage().startswith("event loop held for over")
    ]


def _hold_the_loop(caplog, clock: list[float], report_s: float) -> None:
    """Hold the loop past each limit, until the watchdog has written what that limit asks.

    #4076: holding it for a fixed time left the thread a real-time window for each line. On a
    busy test pod the first stack took longer to format than the block had left, and its error
    was never written. The thread's clock is the test's, so only a hold moves it.
    """
    for passed, level in [(0.06, logging.WARNING), (0.12, logging.ERROR)]:
        if passed >= report_s or level == logging.WARNING:
            written = len(_held(caplog, level))
            clock[0] += 0.06
            deadline = time.monotonic() + 10
            while len(_held(caplog, level)) == written:
                assert time.monotonic() < deadline, f"nothing written {passed} s into a block"
                time.sleep(0.001)
            time.sleep(0.03)  # some more looks at the block, none of which may repeat it


async def _watched(
    caplog, body, report_s: float = 1.0, blocks: int = 1, blocked_s: float = 0.05
) -> list[logging.LogRecord]:
    caplog.set_level(logging.INFO, logger="agentdeck.loop_watchdog")
    watcher = asyncio.create_task(
        loop_watchdog.watch(blocked_s=blocked_s, tick_s=0.01, report_s=report_s)
    )
    for _ in range(blocks):
        await asyncio.sleep(0.02)
        body()
    await asyncio.sleep(0.02)
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(("report_s", "blocks"), [(1.0, 1), (0.1, 1), (0.1, 2)])
async def test_a_held_loop_logs_its_stack_once_and_says_for_how_long(
    caplog, monkeypatch, report_s, blocks
):
    """Each block is told once, a second block in a row again (#3993)."""
    clock = [0.0]
    monkeypatch.setattr(loop_watchdog, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    records = await _watched(
        caplog, lambda: _hold_the_loop(caplog, clock, report_s), report_s, blocks
    )
    held = [r for r in records if r.getMessage().startswith("event loop held for over")]

    # #3993: a block reaching ``report_s`` is also an error, which error reporting sends to
    # Sentry, written while the block lasts: a person restarting a frozen deck ends it first.
    levels = [r.levelno for r in held]
    assert levels.count(logging.ERROR) == (0 if report_s > 0.15 else blocks), "once per block"
    assert levels.count(logging.WARNING) == blocks, "once per block"
    assert all("_hold_the_loop" in r.getMessage() for r in held), "the stack names the call"
    others = [r for r in records if "while the loop was held" in r.getMessage()]
    assert [r.levelno for r in others] == [logging.WARNING] * len(held), "beside each stack"
    # One stack is one moment, so the block's earlier looks go with it (#4130).
    assert all("where the loop was" in r.getMessage() for r in others)
    assert all(" in _hold_the_loop" in r.getMessage() for r in others)
    ended = [r for r in records if r.getMessage().startswith("event loop was blocked for")]
    assert [r.levelno for r in ended] == [logging.WARNING] * blocks


def _park(name: str, release: threading.Lock) -> threading.Thread:
    """A thread stopped on a lock in a named function, as one waiting in `Db` is."""

    def _parked_on_the_lock() -> None:
        release.acquire()
        release.release()

    thread = threading.Thread(target=_parked_on_the_lock, name=name, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while sys._current_frames()[thread.ident].f_code.co_name != "_parked_on_the_lock":
        assert time.monotonic() < deadline
        time.sleep(0.001)
    return thread


def test_other_threads_names_the_busy_ones_and_leaves_out_the_idle(monkeypatch):
    release = threading.Lock()
    release.acquire()
    parked = [_park("db-holder", release), _park("second-holder", release)]
    with ThreadPoolExecutor(1, thread_name_prefix="idle-pool") as pool:
        pool.submit(lambda: None).result()
        deadline = time.monotonic() + 10
        while not any(
            t.name.startswith("idle-pool")
            and loop_watchdog._is_idle(sys._current_frames()[t.ident])
            for t in threading.enumerate()
        ):
            assert time.monotonic() < deadline
            time.sleep(0.001)
        text = loop_watchdog._other_threads(threading.get_ident(), sys._current_frames())
        monkeypatch.setattr(loop_watchdog, "OTHER_THREADS_MAX", 0)
        cut = loop_watchdog._other_threads(threading.get_ident(), sys._current_frames())
    release.release()
    for thread in parked:
        thread.join(timeout=10)

    assert "db-holder:" in text and "second-holder:" in text
    assert "_parked_on_the_lock" in text, "the stack names the call"
    assert "idle-pool" not in text
    assert " more:\n" in cut, "past the limit, each still has its line"
    assert "db-holder: " in cut and "second-holder: " in cut and "_parked_on_the_lock" in cut
    assert "idle-pool" not in cut


def _wait_for(check) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline
        time.sleep(0.001)


def _frame_of(thread: threading.Thread):
    return sys._current_frames()[thread.ident]


async def test_an_idle_anyio_thread_is_idle_and_one_in_a_real_call_is_not():
    """#4130, the two shapes the journal showed: `queue.get()`, and a call with no Python frame."""
    await anyio.to_thread.run_sync(lambda: None)
    (idle,) = [t for t in threading.enumerate() if t.name == "AnyIO worker thread"]
    await asyncio.to_thread(_wait_for, lambda: loop_watchdog._is_idle(_frame_of(idle)))

    held = threading.Lock()
    held.acquire()
    call = asyncio.create_task(anyio.to_thread.run_sync(held.acquire))
    await asyncio.sleep(0.05)  # `run` is the innermost frame for either, so only its line differs
    assert not loop_watchdog._is_idle(_frame_of(idle))
    held.release()
    await call


def test_an_idle_sentry_worker_is_idle_and_one_in_a_job_is_not():
    worker = sentry_sdk.worker.BackgroundWorker()
    started, release = threading.Event(), threading.Event()

    def job() -> None:
        started.set()
        release.wait()

    worker.submit(job)
    (thread,) = [t for t in threading.enumerate() if t.name == "sentry-sdk.BackgroundWorker"]
    assert started.wait(10)
    _wait_for(lambda: _frame_of(thread).f_code.co_name == "wait")
    assert not loop_watchdog._is_idle(_frame_of(thread)), "stopped in a job, not on its queue"
    release.set()
    _wait_for(lambda: loop_watchdog._is_idle(_frame_of(thread)))
    worker.kill()


@pytest.mark.parametrize(
    ("module", "parked"),
    list(
        zip(
            [
                "concurrent.futures.thread",
                "anyio._backends._asyncio",
                "sentry_sdk.worker",
                "sentry_sdk.monitor",
                "sentry_sdk.sessions",
            ],
            loop_watchdog._PARKED,
            strict=True,
        )
    ),
)
def test_each_parked_line_is_in_the_library_this_deck_runs(module, parked):
    """An upgrade that rewords one brings its idle threads back into the list."""
    path, _name, words = parked
    source = importlib.import_module(module)
    assert source.__file__.endswith(path)
    assert words in inspect.getsource(source)


def test_past_the_limit_a_thread_in_our_code_gets_the_stack_before_one_that_is_not(monkeypatch):
    """A hundred waiting threads must not push the lock's holder out (#4130)."""
    release = threading.Lock()
    release.acquire()
    waiting = threading.Event()
    other = threading.Thread(target=waiting.wait, name="waits-elsewhere", daemon=True)
    other.start()
    holder = _park("the-holder", release)
    me = threading.get_ident()
    monkeypatch.setattr(loop_watchdog, "_OWN_CODE", os.path.dirname(__file__) + os.sep)
    monkeypatch.setattr(loop_watchdog, "OTHER_THREADS_MAX", 1)
    frames = {t.ident: _frame_of(t) for t in (other, holder)}

    text = loop_watchdog._other_threads(me, frames)
    release.release()
    waiting.set()
    holder.join(timeout=10)
    other.join(timeout=10)

    assert "the-holder:\n" in text
    assert "waits-elsewhere:\n" not in text and "waits-elsewhere: " in text


def test_where_names_our_innermost_call_and_any_call_when_none_is_ours(monkeypatch):
    frame = sys._current_frames()[threading.get_ident()]
    assert "in test_where_names_our" in loop_watchdog._where(frame)
    monkeypatch.setattr(loop_watchdog, "_OWN_CODE", os.path.dirname(pytest.__file__) + os.sep)
    assert "in _pytest" not in loop_watchdog._where(frame)
    assert loop_watchdog._with_notes([], "threads") == "threads"


def test_a_thread_gone_from_the_names_shows_by_its_id_and_a_plain_wait_counts_as_busy():
    me = threading.get_ident()
    frames = {12345: sys._current_frames()[me]}
    assert "thread 12345:" in loop_watchdog._other_threads(me, frames)

    waiting = threading.Event()
    thread = threading.Thread(target=waiting.wait, name="waits-on-an-event", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while sys._current_frames()[thread.ident].f_code.co_name != "wait":
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert "waits-on-an-event:" in loop_watchdog._other_threads(me, sys._current_frames())
    waiting.set()
    thread.join(timeout=10)


def test_other_threads_says_so_when_none_is_busy():
    me = threading.get_ident()
    frames = {me: sys._current_frames()[me]}
    assert loop_watchdog._other_threads(0, frames) == (
        "no other thread was busy while the loop was held"
    )


async def test_a_failed_thread_list_never_stops_the_reports(caplog, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(loop_watchdog, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def broken(*_args):
        raise RuntimeError("no list")

    monkeypatch.setattr(loop_watchdog, "_other_threads", broken)
    records = await _watched(caplog, lambda: _hold_the_loop(caplog, clock, 0.1), 0.1)

    held = [r for r in records if r.getMessage().startswith("event loop held for over")]
    assert sorted(r.levelno for r in held) == [logging.WARNING, logging.ERROR]
    assert sum("could not list" in r.getMessage() for r in records) == 2


async def test_a_running_loop_stays_quiet_and_a_cancel_stops_the_thread(caplog):
    # Real time, so the limit is one a busy pod's pause stays under, and the run outlasts it.
    assert await _watched(caplog, lambda: None, blocks=12, blocked_s=0.2) == []

    for thread in [t for t in threading.enumerate() if t.name == "loop-watchdog"]:
        thread.join(timeout=10)
        assert not thread.is_alive()
