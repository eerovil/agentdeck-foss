"""The collector's loops react to changes, not timers (#2865, ADR 0007).

Liveness sweeps on a source write, a process exit or a clock deadline; the deploy pill
on a write in its state directory or a suspension; the delegation takeover on its
owner's exit; ``history.jsonl`` is read from where the last read stopped. Each test
drives the event and asserts the reaction, and where it matters that nothing happens
without one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import UTC, datetime

import pytest

from agentdeck import github_suspension, inject
from agentdeck.collector import Collector
from agentdeck.config import AccountConfig, AppConfig, PollingConfig
from agentdeck.db import Db
from agentdeck.models import Session, SessionStatus
from agentdeck.poller_engine.github_authority import Authority
from agentdeck.providers.claude_code.history import HistoryIndex
from agentdeck.state import AppState


class _Provider:
    """Counts sweeps; the deadline is whatever the test sets."""

    def __init__(self):
        self.sweeps = 0
        self.deadline: float | None = None
        self.swept = asyncio.Event()

    def sweep_liveness(self, account, sessions, state):
        self.sweeps += 1
        self.swept.set()
        return []

    def next_liveness_deadline(self, account, sessions):
        deadline, self.deadline = self.deadline, None
        return deadline


def _collector(tmp_path, provider, sessions=()):
    config = AppConfig(
        polling=PollingConfig(liveness_interval_s=0.01),
        accounts=[AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))],
    )
    state = AppState()
    collector = Collector(config, state)
    account = collector.accounts[0]
    collector._provider = lambda _account: provider
    # A live source watch: liveness waits on it instead of looking.
    collector._scan_wakes[account.key] = asyncio.Event()
    state.replace_account_sessions(account.key, list(sessions))
    return collector, account


# Only a failing test waits this long. A shorter limit raced the loop's own timers: a
# pod that stalls the process for longer than it wakes both at once, and the test's
# timeout fires before the sweep that fell due can run (#3600).
_WAIT_S = 30.0


async def _next_sweep(provider, timeout=_WAIT_S):
    provider.swept.clear()
    await asyncio.wait_for(provider.swept.wait(), timeout)


async def test_liveness_sweeps_on_a_source_write_and_not_on_a_timer(tmp_path):
    provider = _Provider()
    collector, account = _collector(tmp_path, provider)
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await asyncio.sleep(0.2)  # twenty intervals with nothing changed
        assert provider.sweeps == 1

        collector._liveness_wakes[account.key].set()
        await _next_sweep(provider)
        assert provider.sweeps == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_wake_during_the_spacing_sleep_earns_its_own_sweep(tmp_path):
    provider = _Provider()
    collector, account = _collector(tmp_path, provider)
    collector.config.polling.liveness_interval_s = 0.05
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        wake = collector._liveness_wakes[account.key]
        provider.swept.clear()
        wake.set()

        async def taken() -> None:
            while wake.is_set():
                await asyncio.sleep(0)

        await asyncio.wait_for(taken(), _WAIT_S)  # inside the spacing sleep before the second sweep
        assert provider.sweeps == 1
        wake.set()
        await asyncio.wait_for(provider.swept.wait(), _WAIT_S)
        await _next_sweep(provider)
        assert provider.sweeps == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_liveness_sweeps_when_a_clock_falls_due(tmp_path):
    provider = _Provider()
    provider.deadline = time.time() + 0.1
    collector, account = _collector(tmp_path, provider)
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await _next_sweep(provider)
        assert provider.sweeps == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_liveness_sweeps_when_a_terminal_chats_process_exits(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    session = Session(
        key="codex:main:sid",
        account_key="codex:main",
        session_id="sid",
        status=SessionStatus.LIVE,
        pid=process.pid,
        last_activity=datetime.now(UTC),
    )
    provider = _Provider()
    collector, account = _collector(tmp_path, provider, [session])
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await asyncio.sleep(0.05)
        assert provider.sweeps == 1
        process.kill()
        await _next_sweep(provider)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await process.wait()


async def test_liveness_keeps_its_cadence_without_a_source_watch(tmp_path):
    provider = _Provider()
    collector, account = _collector(tmp_path, provider)
    collector._scan_wakes.pop(account.key)
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await _next_sweep(provider)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_deploy_pill_follows_its_state_files_and_suspensions(tmp_path, monkeypatch):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    authority_dir = tmp_path / "authority"  # not there yet: watched from its parent
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(deploy))
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(authority_dir / "a.toml"))
    github_suspension.reset()
    collector = Collector(AppConfig(), AppState())
    published = asyncio.Event()
    monkeypatch.setattr(collector.state, "deployment_changed", published.set)
    task = asyncio.create_task(collector._deployment_loop())
    try:
        await asyncio.sleep(0.3)  # the watch is up
        assert not published.is_set()
        (deploy / "blocked").write_text("refused: dirty checkout\n")
        await asyncio.wait_for(published.wait(), 5)

        published.clear()
        monkeypatch.setattr(github_suspension, "_still_speaks", lambda _a, _o: True)
        github_suspension.observe(
            Authority(owner="eerovil", name="agent", login="eerovil-agent"),
            ok=False,
            text="Sorry. Your account was suspended.",
        )
        await asyncio.wait_for(published.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        github_suspension.reset()


def test_a_suspension_listener_hears_only_a_change():
    github_suspension.reset()
    heard = []
    remove = github_suspension.add_listener(lambda: heard.append(1))
    agent = Authority(owner="eerovil", name="agent", login="eerovil-agent")
    try:
        github_suspension.observe(agent, ok=True)
        github_suspension.observe(agent, ok=False, text="a timeout")
        assert heard == []
        github_suspension.observe(agent, ok=False, text="Your account was suspended")
        assert heard == [1]
        github_suspension.observe(agent, ok=True)
        assert heard == [1, 1]
    finally:
        remove()
        github_suspension.reset()


async def test_a_delegation_is_taken_over_when_its_owner_exits(tmp_path, monkeypatch):
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    db = Db(tmp_path / "control.db")
    config = AppConfig(
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))]
    )
    account = config.build_accounts()[0]
    db.save_delegation(
        {
            "id": "d-1",
            "account_key": account.key,
            "session_key": f"{account.key}:thread-1",
            "state": "running",
            "owner_pid": owner.pid,
            "created_at": time.time(),
        }
    )
    alive = {owner.pid}
    monkeypatch.setattr(inject, "_process_alive", lambda pid: pid in alive)
    monkeypatch.setattr(inject, "_sibling_pids", set)
    # Were there a timer, this would be the only way to see the takeover.
    monkeypatch.setattr(inject, "DELEGATION_TAKEOVER_INTERVAL_S", 3600.0)

    waited = asyncio.Event()

    class Provider:
        async def wait_for_session(self, account, session_id, *, timeout_s):
            waited.set()
            await asyncio.Event().wait()

    from agentdeck.config import InjectConfig

    service = inject.InjectionService(InjectConfig(enabled=True), delegation_store=db)
    service.start_delegation_takeover(lambda: [account], {"codex": Provider()})
    try:
        await asyncio.sleep(0.1)
        assert not waited.is_set()
        alive.clear()
        owner.kill()
        await asyncio.wait_for(waited.wait(), 3)
        assert db.load_delegation("d-1")["owner_pid"] == inject.os.getpid()
    finally:
        await service.stop()
        await owner.wait()


def _line(session_id, display, project="/p"):
    return json.dumps({"sessionId": session_id, "display": display, "project": project})


def test_history_is_read_from_where_the_last_read_stopped(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(_line("a", "first") + "\n")
    index = HistoryIndex(tmp_path)
    assert index.read()["a"].title == "first"
    first_size = path.stat().st_size
    assert index._offset == first_size

    with path.open("a") as handle:
        handle.write(_line("a", "second") + "\n" + _line("b", "other"))  # last line unfinished
    entries = index.read()
    assert entries["a"].title == "first" and entries["a"].last_prompt == "second"
    assert entries["b"].title == "other"
    # Only whole lines are consumed; the unfinished one is read again once it ends.
    assert index._offset == first_size + len(_line("a", "second")) + 1
    with path.open("a") as handle:
        handle.write("\n" + _line("b", "more") + "\n")
    entries = index.read()
    assert entries["b"].title == "other" and entries["b"].last_prompt == "more"


def test_history_starts_over_when_the_file_shrinks_or_is_replaced(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(_line("a", "first") + "\n" + _line("a", "second") + "\n")
    index = HistoryIndex(tmp_path)
    assert index.read()["a"].last_prompt == "second"

    path.write_text(_line("c", "x") + "\n")  # truncated in place
    assert set(index.read()) == {"c"}

    replacement = tmp_path / "history.new"
    replacement.write_text(_line("d", "y") + "\n" + _line("d", "z") + "\n" + _line("e", "w") + "\n")
    replacement.replace(path)
    assert set(index.read()) == {"d", "e"}

    path.unlink()
    assert index.read() == {}


@pytest.mark.parametrize("trailing", ["", "\n"])
def test_load_history_reads_a_last_line_with_or_without_newline(tmp_path, trailing):
    from agentdeck.providers.claude_code.history import load_history

    (tmp_path / "history.jsonl").write_text(_line("a", "only") + trailing)
    assert load_history(tmp_path)["a"].title == "only"


async def test_an_owner_dying_before_its_watch_still_has_its_delegation_taken(
    tmp_path, monkeypatch
):
    """The owner dies after the pass looked and before the exit watch opened, so no exit
    is ever announced for it: the old cadence stays on until it is taken (#2865)."""
    db = Db(tmp_path / "control.db")
    config = AppConfig(
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))]
    )
    account = config.build_accounts()[0]
    gone_pid = 2_000_000_000  # never a real process: nothing to watch
    db.save_delegation(
        {
            "id": "d-1",
            "account_key": account.key,
            "session_key": f"{account.key}:thread-1",
            "state": "running",
            "owner_pid": gone_pid,
            "created_at": time.time(),
        }
    )
    alive = {gone_pid}
    monkeypatch.setattr(inject, "_process_alive", lambda pid: pid in alive)
    monkeypatch.setattr(inject, "_sibling_pids", set)
    monkeypatch.setattr(inject, "DELEGATION_TAKEOVER_INTERVAL_S", 0.05)
    waited = asyncio.Event()

    class Provider:
        async def wait_for_session(self, account, session_id, *, timeout_s):
            waited.set()
            await asyncio.Event().wait()

    from agentdeck.config import InjectConfig

    service = inject.InjectionService(InjectConfig(enabled=True), delegation_store=db)
    first_pass = service.take_over_delegations

    def pass_then_owner_dies(accounts, providers):
        first_pass(accounts, providers)
        alive.clear()

    monkeypatch.setattr(service, "take_over_delegations", pass_then_owner_dies)
    service.start_delegation_takeover(lambda: [account], {"codex": Provider()})
    try:
        await asyncio.wait_for(waited.wait(), 3)
        assert db.load_delegation("d-1")["owner_pid"] == inject.os.getpid()
    finally:
        await service.stop()


async def test_a_failed_deploy_state_watch_is_retried(tmp_path, monkeypatch):
    """One watch error used to stop the deploy pill until a restart (#2865)."""
    from agentdeck import collector as collector_module
    from agentdeck import watch_retry

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(deploy))
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(tmp_path / "a.toml"))
    github_suspension.reset()
    monkeypatch.setattr(watch_retry, "RETRY_MIN_S", 0.05)
    real_awatch = collector_module.awatch
    calls = {"n": 0}
    opened = asyncio.Event()

    async def quick_awatch(*paths, **kwargs):
        changes = real_awatch(*paths, **{**kwargs, "debounce": 20, "step": 10})
        # `awatch` opens its watch before its first suspension: announce it after that.
        asyncio.get_running_loop().call_soon(opened.set)
        async for batch in changes:
            yield batch

    def flaky_awatch(*paths, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("inotify watch limit reached")
        return quick_awatch(*paths, **kwargs)

    monkeypatch.setattr(collector_module, "awatch", flaky_awatch)
    collector = Collector(AppConfig(), AppState())
    published = asyncio.Event()
    monkeypatch.setattr(collector.state, "deployment_changed", published.set)
    task = asyncio.create_task(collector._deployment_loop())
    try:
        await asyncio.wait_for(opened.wait(), 5)  # failed once, retried, the watch is up again
        assert calls["n"] == 2
        (deploy / "blocked").write_text("refused: dirty checkout\n")
        await asyncio.wait_for(published.wait(), 5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        github_suspension.reset()


async def test_a_scan_wakes_liveness(tmp_path):
    """A chat live at startup arrives with the first scan, after liveness's first sweep: the
    scan wakes it, so the chat gets its exit watch now, not at the next write (#2865)."""

    class Scanning(_Provider):
        async def scan_sessions(self, account):
            return [
                Session(
                    key="codex:main:sid",
                    account_key="codex:main",
                    session_id="sid",
                    status=SessionStatus.LIVE,
                    last_activity=datetime.now(UTC),
                )
            ]

    provider = Scanning()
    collector, account = _collector(tmp_path, provider)
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await asyncio.sleep(0.05)
        assert provider.sweeps == 1
        await collector._full_scan(provider, account)
        await _next_sweep(provider)
        assert provider.sweeps == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_failed_liveness_sweep_is_retried(tmp_path, monkeypatch):
    """A sweep that raised is tried again after a back-off, not at the next write (#2865)."""

    class Flaky(_Provider):
        def sweep_liveness(self, account, sessions, state):
            super().sweep_liveness(account, sessions, state)
            if self.sweeps == 1:
                raise OSError("registry unreadable")
            return []

    provider = Flaky()
    collector, account = _collector(tmp_path, provider)
    task = asyncio.create_task(collector._liveness_loop(account))
    try:
        await _next_sweep(provider)
        await _next_sweep(provider)
        assert provider.sweeps == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_failed_job_queue_read_is_retried_on_the_fallback_cadence(monkeypatch):
    """The feed says when the queue moved, not that a read of it failed (#2865)."""
    from agentdeck import collector as collector_module
    from agentdeck import runtime_feed

    reads = {"n": 0}

    async def read(_client):
        reads["n"] += 1
        return None  # unreachable runtime

    class FeedMode:
        mode = runtime_feed.FEED

        def _ensure_running(self):
            pass

        def _unsubscribe(self, sub):
            pass

    class Shared:
        def subscribe(self, items):
            return runtime_feed.FeedSubscription(FeedMode(), lambda _item: True)

    monkeypatch.setattr(collector_module, "read_job_queue", read)
    monkeypatch.setattr(collector_module.runtime_feed, "shared", lambda: Shared())
    monkeypatch.setattr(collector_module, "JOBS_INTERVAL_S", 0.05)
    collector = Collector(AppConfig(), AppState())
    task = asyncio.create_task(collector._jobs_loop())
    try:
        await asyncio.sleep(0.3)
        assert reads["n"] >= 3  # retried with no feed change at all
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
