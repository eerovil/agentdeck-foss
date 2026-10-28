"""Per-worker memory caps (#1546).

One runaway worker used to take the whole host with it, because a worker's
memory was the runtime's memory. These cover the three things that has to mean:
the worker runs in a capped cgroup of its own, a host that cannot give it one
still starts the chat, and a kill by the cap is said out loud on that chat
instead of leaving it looking mysteriously stalled.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys

import pytest
from worker_harness import claude_host as _host
from worker_harness import settle as _settle

from agentdeck import worker_limits
from agentdeck.config import ClaudeWorkersConfig
from agentdeck.models import SessionStatus
from agentdeck.web.render import worker_exit_message


@pytest.fixture(autouse=True)
def _no_probe_cache():
    """Availability is cached per process; each test states its own answer."""
    worker_limits._probed.clear()
    yield
    worker_limits._probed.clear()


def _capped(monkeypatch, tmp_path, *, available=True, **kwargs):
    async def fake_available(memory_max: str) -> bool:
        return available

    monkeypatch.setattr(worker_limits, "available", fake_available)
    return _host(tmp_path, memory_max="8G", **kwargs)


async def _spawn(host, spawned, cwd):
    result = await host.deliver("issue-1", "work it", cwd=str(cwd), delegated=False)
    assert result.accepted
    await _settle()
    return spawned[-1]


async def _die(record, returncode: int) -> None:
    """The child is gone and its stdout has hit EOF — how the host finds out."""
    record["process"].returncode = returncode
    record["process"].stdout.queue.put_nowait(b"")
    await _settle()


# --- the cap itself --------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_spawns_inside_its_own_capped_scope(monkeypatch, tmp_path):
    host, spawned = _capped(monkeypatch, tmp_path)
    argv = list((await _spawn(host, spawned, tmp_path))["args"])

    assert argv[0] == "systemd-run"
    assert "--scope" in argv and "--user" in argv
    assert "MemoryMax=8G" in argv
    # Without a swap limit a runaway worker buys headroom in swap first and
    # drags the host down before the cap ever fires.
    assert "MemorySwapMax=0" in argv
    unit = next(a for a in argv if a.startswith("--unit="))
    assert unit.endswith(".scope")
    # The worker command still follows, unaltered.
    assert argv[argv.index("--") + 1] == "claude"
    await host.stop()


@pytest.mark.asyncio
async def test_healthy_path_is_unchanged_without_a_cap(tmp_path):
    host, spawned = _host(tmp_path)
    argv = list((await _spawn(host, spawned, tmp_path))["args"])

    assert argv[0] == "claude"
    await host.stop()


@pytest.mark.asyncio
async def test_uncappable_host_still_starts_the_chat(monkeypatch, tmp_path):
    """No systemd, no user manager, an unparseable cap — all the same answer.

    An uncapped worker is exactly today's behaviour. Refusing to work would be
    a worse failure than working without the cap.
    """
    host, spawned = _capped(monkeypatch, tmp_path, available=False)
    argv = list((await _spawn(host, spawned, tmp_path))["args"])

    assert argv[0] == "claude"
    await host.stop()


@pytest.mark.asyncio
async def test_each_worker_gets_a_scope_of_its_own(monkeypatch, tmp_path):
    host, spawned = _capped(monkeypatch, tmp_path)
    await _spawn(host, spawned, tmp_path)
    await host.deliver("issue-2", "work it too", cwd=str(tmp_path), delegated=False)
    await _settle()

    units = [
        next(a for a in record["args"] if a.startswith("--unit="))
        for record in spawned
    ]
    assert len(units) == 2
    assert units[0] != units[1]
    await host.stop()


def test_a_restarted_runtime_never_reuses_a_scope_name(monkeypatch):
    """A scope can outlive its runtime (a worker's leftover process group keeps it
    loaded), and the counter starts over with the next runtime. The same key and
    count from a new runtime must still name a different unit, or systemd-run
    refuses the spawn and the chat's resume fails (#3079)."""
    import itertools

    monkeypatch.setattr(worker_limits, "_counter", itertools.count())
    before = worker_limits.scope_unit("main", "chat-7f55759ccf39")
    monkeypatch.setattr(worker_limits, "_counter", itertools.count())
    monkeypatch.setattr(worker_limits, "_INCARNATION", "r0123abcd")
    after = worker_limits.scope_unit("main", "chat-7f55759ccf39")

    assert before != after
    assert after == "agentdeck-worker-main-chat-7f55759ccf39-r0123abcd-0.scope"


# --- saying so on the chat -------------------------------------------------


def _reads_scope(monkeypatch, result: str) -> list[str]:
    cleared: list[str] = []

    async def fake_result(unit: str) -> str:
        return result

    async def fake_clear(unit: str) -> None:
        cleared.append(unit)

    monkeypatch.setattr(worker_limits, "scope_result", fake_result)
    monkeypatch.setattr(worker_limits, "clear_scope", fake_clear)
    return cleared


@pytest.mark.asyncio
async def test_cap_kill_is_attributed_to_the_chat(monkeypatch, tmp_path):
    host, spawned = _capped(monkeypatch, tmp_path)
    cleared = _reads_scope(monkeypatch, worker_limits.RESULT_OOM_KILL)
    record = await _spawn(host, spawned, tmp_path)

    await _die(record, -9)

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["live"] is False
    assert worker["exit_reason"] == worker_limits.EXIT_MEMORY_CAP
    # The failed scope is tidied away rather than left in `systemctl --failed`.
    assert len(cleared) == 1
    await host.stop()


@pytest.mark.asyncio
async def test_an_ordinary_death_says_nothing(monkeypatch, tmp_path):
    """A process that simply ended needs no explanation; `live: false` is it."""
    host, spawned = _capped(monkeypatch, tmp_path)
    _reads_scope(monkeypatch, "")
    record = await _spawn(host, spawned, tmp_path)

    await _die(record, 0)

    assert host.snapshot()["workers"]["issue-1"]["exit_reason"] == ""
    await host.stop()


@pytest.mark.asyncio
async def test_reviving_the_chat_clears_the_reason(monkeypatch, tmp_path):
    host, spawned = _capped(monkeypatch, tmp_path)
    _reads_scope(monkeypatch, worker_limits.RESULT_OOM_KILL)
    record = await _spawn(host, spawned, tmp_path)
    await _die(record, -9)
    assert host.snapshot()["workers"]["issue-1"]["exit_reason"]

    await host.deliver("issue-1", "try again", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["exit_reason"] == ""
    await host.stop()


def test_the_reason_reads_as_a_sentence():
    message = worker_exit_message(worker_limits.EXIT_MEMORY_CAP)
    assert "memory" in message.lower()
    # Distinct from a stall or an ordinary turn failure, and it says what to do.
    assert "another message" in message
    # A death the deck cannot explain stays silent rather than being dressed up.
    assert worker_exit_message("") == ""
    assert worker_exit_message("something_new") == ""


@pytest.mark.asyncio
async def test_the_chat_page_says_why_the_worker_died(tmp_path):
    """The point of the reason: a person opening the chat can read it."""
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    session.status = SessionStatus.IDLE
    session.worker_exit_reason = worker_limits.EXIT_MEMORY_CAP
    app.state.app_state.update_session(session)

    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")

    assert "worker-exit-notice" in page.text
    # The sentence itself, not just its container (HTML-escaped apostrophe aside).
    assert "using far more memory than a worker may" in page.text
    assert "Send another message to start it again." in page.text


@pytest.mark.asyncio
async def test_a_healthy_chat_page_carries_no_notice(tmp_path):
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path)

    async with _client(app) as client:
        page = await client.get("/sessions/claude_code:test:sid1")

    assert "worker-exit-notice" not in page.text


# --- configuration ---------------------------------------------------------


def test_the_cap_is_configurable_with_a_documented_default():
    assert ClaudeWorkersConfig().memory_max == "8G"
    assert ClaudeWorkersConfig(memory_max="").memory_max == ""

    per_account = ClaudeWorkersConfig(accounts={"alt": {"memory_max": "2G"}})
    assert per_account.for_account("alt").memory_max == "2G"
    assert per_account.for_account("main").memory_max == "8G"


# --- the real thing --------------------------------------------------------


def _systemd_scopes_work() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    try:
        return subprocess.run(
            [*worker_limits.scope_prefix(worker_limits.scope_unit("probe", "test"), "64M"), "true"],
            capture_output=True,
            timeout=15,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_REAL_SCOPES = pytest.mark.skipif(
    not _systemd_scopes_work(), reason="host has no usable `systemd-run --user`"
)

# What a process inside the scope reads back from its own cgroup: the path,
# then the memory and swap limits the kernel will enforce on it.
_READ_OWN_LIMITS = """
import pathlib
path = pathlib.Path("/proc/self/cgroup").read_text().strip().rsplit(":", 1)[-1]
cgroup = pathlib.Path("/sys/fs/cgroup" + path)
print(path)
print((cgroup / "memory.max").read_text().strip())
print((cgroup / "memory.swap.max").read_text().strip())
"""


@_REAL_SCOPES
@pytest.mark.asyncio
async def test_a_real_scope_carries_the_cap_and_no_swap():
    """The cap against real systemd, without killing anything (#3717).

    The kernel enforces exactly these two numbers on the cgroup, so reading
    them back from inside proves the cap is in force. Triggering it here would
    pop a KDE "Memory Shortage Avoided" notification on the owner's desktop
    for every test run; the kill itself is the opt-in test below.
    """
    unit = worker_limits.scope_unit("test", "cap-readback")
    process = await asyncio.create_subprocess_exec(
        *worker_limits.scope_prefix(unit, "64M"),
        sys.executable,
        "-c",
        _READ_OWN_LIMITS,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await asyncio.wait_for(process.communicate(), timeout=30)

    assert process.returncode == 0
    path, memory_max, swap_max = out.decode().split()
    assert path.endswith("/" + unit)  # a scope of its own, not the runtime's
    assert memory_max == str(64 * 1024 * 1024)
    assert swap_max == "0"


@_REAL_SCOPES
@pytest.mark.skipif(
    os.environ.get("AGENTDECK_REAL_OOM_TEST") != "1",
    reason="kills a real process; KDE turns that into a desktop popup (#3717). "
    "AGENTDECK_REAL_OOM_TEST=1 runs it",
)
@pytest.mark.asyncio
async def test_a_runaway_process_is_killed_alone_and_names_itself():
    """The acceptance criterion, against real systemd rather than a fake.

    A child that allocates past its cap dies; we are still here to observe it,
    and the scope says *why* — which no exit code can, since an ordinary
    `kill -9` produces exactly the same `-9`. Opt-in: every real OOM kill in a
    user unit shows as a KDE notification, and the per-change suite runs on
    the owner's desktop several times an hour.
    """
    unit = worker_limits.scope_unit("test", "runaway")
    process = await asyncio.create_subprocess_exec(
        *worker_limits.scope_prefix(unit, "64M"),
        "python3",
        "-c",
        "b = bytearray()\nwhile True:\n    b += bytes(10**7)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    returncode = await asyncio.wait_for(process.wait(), timeout=60)

    assert returncode == -9  # killed by its own cgroup, not by us
    assert await worker_limits.scope_result(unit) == worker_limits.RESULT_OOM_KILL
    await worker_limits.clear_scope(unit)
