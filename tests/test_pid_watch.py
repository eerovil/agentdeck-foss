"""A process exit is watched even on a Python built without ``os.pidfd_open`` (#3056).

uv's standalone CPython 3.12 leaves it out, so a suite run with ``uv run`` on this host
fell back to polling and every test that waits on an exit timed out.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from agentdeck import pid_watch

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="pidfd is Linux")


@pytest.fixture(params=["os", "syscall"])
def pidfd_source(request, monkeypatch):
    if request.param == "syscall":
        # Hidden from pid_watch alone: asyncio's own child watcher needs it.
        monkeypatch.setattr(pid_watch, "_NATIVE_PIDFD_OPEN", None)
    return request.param


async def test_an_exit_is_heard_whichever_way_the_pidfd_was_opened(pidfd_source):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    exited = asyncio.Event()
    heard: list[int] = []

    def on_exit(pid):
        heard.append(pid)
        exited.set()

    watches = pid_watch.ExitWatches(on_exit)
    try:
        watches.watch([process.pid])
        assert watches.unwatched == frozenset()
        assert watches.pids == {process.pid}
        process.kill()
        await asyncio.wait_for(exited.wait(), 3)
        assert heard == [process.pid]
    finally:
        watches.close()
        await process.wait()


async def test_a_process_already_gone_counts_as_an_exit(pidfd_source):
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await process.wait()  # reaped, so the pid names nothing
    heard: list[int] = []
    watches = pid_watch.ExitWatches(heard.append)
    try:
        watches.watch([process.pid])
        await asyncio.sleep(0)
        assert heard == [process.pid]
        assert watches.unwatched == frozenset()
    finally:
        watches.close()
