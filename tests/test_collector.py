import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from watchfiles import Change

from agentdeck import collector as collector_module
from agentdeck.collector import Collector
from agentdeck.config import AccountConfig, AppConfig, PollingConfig
from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db
from agentdeck.models import (
    Account,
    AttentionReport,
    Session,
    SessionStatus,
    TranscriptEvent,
)
from agentdeck.providers.base import ContextObservation
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.codex.provider import CodexProvider
from agentdeck.questions import OpenQuestion
from agentdeck.state import AppState


async def test_failed_initial_scan_still_completes_collector_readiness(
    tmp_path, monkeypatch
):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=999),
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ],
    )
    collector = Collector(config, AppState())

    class FailingProvider:
        async def scan_sessions(self, _account):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(collector, "_provider", lambda _account: FailingProvider())
    task = asyncio.create_task(collector._scan_loop(collector.accounts[0]))
    try:
        for _ in range(20):
            if collector.initial_scans_finished():
                break
            await asyncio.sleep(0.01)
        assert collector.initial_scans_finished()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
async def test_collector_records_compact_observations_only_while_enforced(tmp_path, mode):
    db = Db(tmp_path / f"{mode}.db")
    try:
        state = AppState(db=db)
        config = AppConfig.model_validate(
            {
                "accounts": [
                    {"provider": "codex", "label": "main", "config_dir": str(tmp_path)}
                ],
                "context_management": {"mode": mode},
            }
        )
        collector = Collector(config, state)
        account = collector.accounts[0]
        state.sessions_scanned(account.key)
        row = db.reserve_context_delivery(
            ContextDelivery(
                action_id="compact-1",
                attempt=1,
                provider_id="codex",
                account_key=account.key,
                worker_key="issue-1488",
                delivery_id="delivery-1",
                request_sha256="a" * 64,
                chat_id="chat-1",
                source_session_key=f"{account.key}:source",
                source_session_id="source",
                mode="enforce",
                proposed_action="compact",
                executed_action="compact",
                reason="threshold",
            )
        )
        assert row is not None
        assert db.mark_completed(
            row.action_id,
            maintenance_result="succeeded",
            provider_write_state="started",
        )
        completed = db.get_context_delivery(
            "codex", account.key, "issue-1488", "delivery-1"
        )
        assert completed is not None and completed.maintenance_completed_at
        observed_at = datetime.fromisoformat(
            completed.maintenance_completed_at
        ) + timedelta(seconds=1)

        class Provider:
            async def context_observations(self, _account, _sessions):
                return (
                    ContextObservation(
                        "issue-1488", "source", 55_000, observed_at, 1
                    ),
                )

        await collector._record_context_observations(Provider(), account, [])

        stored = db.get_context_delivery(
            "codex", account.key, "issue-1488", "delivery-1"
        )
        assert stored is not None
        assert stored.rearmed is (mode == "enforce")
        assert stored.after_tokens == (55_000 if mode == "enforce" else None)
    finally:
        db.close()


async def test_watched_scan_waits_for_a_source_change(tmp_path, monkeypatch):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=0.05),
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ],
    )
    collector = Collector(config, AppState())
    account = collector.accounts[0]
    wake = asyncio.Event()
    collector._scan_wakes[account.key] = wake
    scans = []

    class Provider:
        async def scan_sessions(self, _account):
            scans.append(asyncio.get_running_loop().time())
            return []

    monkeypatch.setattr(collector, "_provider", lambda _account: Provider())
    task = asyncio.create_task(collector._scan_loop(account))
    try:
        await asyncio.sleep(0.08)
        assert len(scans) == 1

        wake.set()
        await asyncio.sleep(0.08)
        assert len(scans) == 2
        assert scans[1] - scans[0] >= 0.05
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_source_watcher_wakes_for_a_real_file_change(tmp_path):
    collector = Collector(AppConfig(), AppState())
    wake = asyncio.Event()
    account_key = "codex:main"
    collector._scan_wakes[account_key] = wake
    task = asyncio.create_task(
        collector._watch_loop(account_key, [str(tmp_path)], wake)
    )
    try:
        await asyncio.sleep(0.1)
        (tmp_path / "rollout.jsonl").write_text("{}\n")
        await asyncio.wait_for(wake.wait(), timeout=2.0)
        assert collector._scan_changes[account_key] == {
            (Change.added, tmp_path / "rollout.jsonl")
        }
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_watched_scan_merges_a_safe_changed_session(tmp_path, monkeypatch):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=0.05),
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ],
    )
    state = AppState()
    collector = Collector(config, state)
    account = collector.accounts[0]
    wake = asyncio.Event()
    collector._scan_wakes[account.key] = wake
    source = tmp_path / "sessions" / "known.jsonl"
    initial = Session(
        f"{account.key}:known", account.key, "known", SessionStatus.IDLE, title="old"
    )
    fresh = Session(
        f"{account.key}:known", account.key, "known", SessionStatus.IDLE, title="fresh"
    )
    full_scans = 0
    changed_scans = []

    class Provider:
        async def scan_sessions(self, _account):
            nonlocal full_scans
            full_scans += 1
            return [initial]

        async def scan_changed_sessions(self, _account, changes, _state):
            changed_scans.append(changes)
            return [fresh]

        async def read_transcript(self, _account, _session, after_seq=0):
            return []

    monkeypatch.setattr(collector, "_provider", lambda _account: Provider())
    task = asyncio.create_task(collector._scan_loop(account))
    try:
        await asyncio.sleep(0.08)
        collector._scan_changes[account.key] = {(Change.modified, source)}
        wake.set()
        await asyncio.sleep(0.08)
        assert full_scans == 1
        assert changed_scans == [frozenset({(Change.modified, source)})]
        assert state.sessions[f"{account.key}:known"].title == "fresh"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_provider_watch_filters_only_accept_session_sources(tmp_path):
    codex = Account("codex:main", "codex", "main", tmp_path)
    claude = Account("claude_code:main", "claude_code", "main", tmp_path)

    assert CodexProvider().watch_root_change(codex, tmp_path / "sessions")
    assert not CodexProvider().watch_root_change(codex, tmp_path / "log")
    assert ClaudeCodeProvider().watch_root_change(
        claude, tmp_path / "projects" / "chat.jsonl"
    )
    assert ClaudeCodeProvider().watch_root_change(claude, tmp_path / "history.jsonl")
    assert not ClaudeCodeProvider().watch_root_change(
        claude, tmp_path / "debug" / "worker.log"
    )


@pytest.mark.parametrize("replace_existing", [False, True])
async def test_stable_root_watcher_sees_created_or_replaced_source(
    tmp_path, replace_existing
):
    sessions = tmp_path / "sessions"
    if replace_existing:
        sessions.mkdir()
    collector = Collector(AppConfig(), AppState())
    account_key = "codex:main"
    wake = asyncio.Event()
    collector._scan_wakes[account_key] = wake

    def watch_filter(_change, path):
        return Path(path).relative_to(tmp_path).parts[0] == "sessions"

    task = asyncio.create_task(
        collector._watch_loop(
            account_key, [str(tmp_path)], wake, watch_filter, recursive=False
        )
    )
    try:
        await asyncio.sleep(0.1)
        if replace_existing:
            sessions.rename(tmp_path / "sessions-old")
        sessions.mkdir()
        (sessions / "rollout.jsonl").write_text("{}\n")
        await asyncio.wait_for(wake.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def _quick_awatch(monkeypatch) -> asyncio.Queue:
    """Swap in `awatch` with a short debounce; queue each watch's paths once it is open."""
    real_awatch = collector_module.awatch
    opened: asyncio.Queue = asyncio.Queue()

    async def quick_awatch(*paths, **kwargs):
        changes = real_awatch(*paths, **{**kwargs, "debounce": 20, "step": 10})
        # `awatch` opens its watch before its first suspension: announce it after that.
        asyncio.get_running_loop().call_soon(opened.put_nowait, paths)
        async for batch in changes:
            yield batch

    monkeypatch.setattr(collector_module, "awatch", quick_awatch)
    return opened


async def _opened(opened: asyncio.Queue, *paths) -> None:
    """Wait until a watch on exactly ``paths`` is open."""
    want = tuple(str(path) for path in paths)
    while tuple(map(str, await asyncio.wait_for(opened.get(), 2.0))) != want:
        pass


async def test_stable_root_watcher_ignores_unrelated_account_changes(tmp_path, monkeypatch):
    opened = _quick_awatch(monkeypatch)
    collector = Collector(AppConfig(), AppState())
    wake = asyncio.Event()
    account_key = "codex:main"
    collector._scan_wakes[account_key] = wake
    filtered = asyncio.Event()

    def watch_filter(_change, path):
        if Path(path).name == "unrelated.log":
            filtered.set()
        return Path(path).relative_to(tmp_path).parts[0] == "sessions"

    task = asyncio.create_task(
        collector._watch_loop(
            account_key, [str(tmp_path)], wake, watch_filter, recursive=False
        )
    )
    try:
        await _opened(opened, tmp_path)
        (tmp_path / "unrelated.log").write_text("noise\n")
        # The watch saw the write and dropped it: a kept change wakes in the same step.
        await asyncio.wait_for(filtered.wait(), timeout=2.0)
        await asyncio.sleep(0)
        assert not wake.is_set()

        (tmp_path / "sessions").mkdir()
        await asyncio.wait_for(wake.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_appending_to_a_watched_root_file_does_not_force_a_full_scan(tmp_path):
    """Issue #1487: Claude's ``history.jsonl`` is both a direct child of the
    account root and a watched source, so the topology watcher saw every prompt
    the CLI recorded, rebuilt the source watches and forced a full account scan.
    A topology change is a watch root appearing or being replaced, never a write
    into one — and a replacement still arrives as deleted+added."""
    (tmp_path / "projects").mkdir()
    history = tmp_path / "history.jsonl"
    history.write_text("{}\n")
    config = AppConfig(
        accounts=[
            AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))
        ]
    )
    collector = Collector(config, AppState())
    account = collector.accounts[0]
    wake = asyncio.Event()
    collector._scan_wakes[account.key] = wake
    task = asyncio.create_task(collector._account_watch_loop(account, wake))
    try:
        await asyncio.sleep(0.3)
        with history.open("a") as handle:
            handle.write('{"sessionId": "abc"}\n')
        await asyncio.wait_for(wake.wait(), timeout=2.0)

        # The source change still wakes the scan loop; what it must not do is
        # take the incremental path's authority away from it.
        assert account.key not in collector._force_full_scans
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_account_watcher_attaches_to_a_source_created_after_startup(tmp_path, monkeypatch):
    opened = _quick_awatch(monkeypatch)
    config = AppConfig(
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ]
    )
    collector = Collector(config, AppState())
    account = collector.accounts[0]
    wake = asyncio.Event()
    collector._scan_wakes[account.key] = wake
    task = asyncio.create_task(collector._account_watch_loop(account, wake))
    try:
        await _opened(opened, tmp_path)
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        await asyncio.wait_for(wake.wait(), timeout=2.0)

        wake.clear()
        await _opened(opened, sessions)
        nested = sessions / "2026" / "08"
        nested.mkdir(parents=True)
        (nested / "rollout.jsonl").write_text("{}\n")
        await asyncio.wait_for(wake.wait(), timeout=2.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_failed_watched_scan_retries_without_waiting_for_a_change(
    tmp_path, monkeypatch
):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=0.05),
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ],
    )
    collector = Collector(config, AppState())
    account = collector.accounts[0]
    collector._scan_wakes[account.key] = asyncio.Event()
    attempts = 0

    class Provider:
        async def scan_sessions(self, _account):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return []

    monkeypatch.setattr(collector, "_provider", lambda _account: Provider())
    task = asyncio.create_task(collector._scan_loop(account))
    try:
        for _ in range(20):
            if attempts >= 2:
                break
            await asyncio.sleep(0.01)
        assert attempts >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_failed_source_watcher_restores_periodic_scans(tmp_path, monkeypatch):
    async def failed_watch(*_paths, **_kwargs):
        raise RuntimeError("watch failed")
        yield

    monkeypatch.setattr("agentdeck.collector.awatch", failed_watch)
    collector = Collector(AppConfig(), AppState())
    account_key = "codex:main"
    wake = asyncio.Event()
    collector._scan_wakes[account_key] = wake

    await collector._watch_loop(account_key, [str(tmp_path)], wake)

    assert account_key not in collector._scan_wakes
    assert wake.is_set()


async def test_collector_populates_sessions(fake_config_dir):
    config = AppConfig(
        polling=PollingConfig(scan_interval_s=0.05, liveness_interval_s=0.05, usage_interval_s=999),
        accounts=[
            AccountConfig(
                provider="claude_code", label="main", config_dir=str(fake_config_dir.path)
            )
        ],
    )
    state = AppState()
    collector = Collector(config, state)
    await collector.start()
    try:
        # Unlike its neighbours, this waits on a real provider scan of a
        # synthetic config dir — filesystem work, not in-process state. Bound it
        # by wall clock rather than a tick count: 60 ticks of 20ms ran out while
        # the rest of the suite had the cores, and the test then failed for load
        # instead of for behaviour. The deadline costs nothing when it passes.
        deadline = time.monotonic() + 15
        while not state.sessions and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
    finally:
        await collector.stop()

    sids = {s.session_id for s in state.sessions.values()}
    assert fake_config_dir.live_sid in sids
    assert fake_config_dir.idle_sid in sids


async def test_open_question_transcripts_are_read_concurrently(tmp_path):
    config = AppConfig(
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ]
    )
    state = AppState()
    collector = Collector(config, state)
    account = collector.accounts[0]
    now = datetime.now(UTC)
    sessions = [
        Session(
            key=f"{account.key}:thread-{index}",
            account_key=account.key,
            session_id=f"thread-{index}",
            status=SessionStatus.IDLE,
            question="Which approach?",
            last_event_seq=2,
            last_activity=now,
        )
        for index in range(2)
    ]
    for session in sessions:
        state.open_questions[session.key] = OpenQuestion(
            session.key,
            session.question,
            now,
            "trailing",
            1,
            1,
        )

    all_started = asyncio.Event()
    started: list[str] = []

    class Provider:
        async def read_transcript(self, account, session, after_seq=0):
            started.append(session.key)
            if len(started) == len(sessions):
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=0.2)
            return []

    await collector._reconcile_open_questions(Provider(), account, sessions)

    assert set(started) == {session.key for session in sessions}


async def test_an_earlier_leg_question_reads_the_current_leg_from_its_start(tmp_path):
    config = AppConfig(
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))
        ]
    )
    db = Db(tmp_path / "state.db")
    state = AppState(db=db)
    collector = Collector(config, state)
    account = collector.accounts[0]
    old_key = f"{account.key}:thread-old"
    old = Session(
        key=old_key,
        account_key=account.key,
        session_id="thread-old",
        status=SessionStatus.IDLE,
    )
    current = Session(
        key=f"{account.key}:thread-current",
        account_key=account.key,
        session_id="thread-current",
        status=SessionStatus.IDLE,
    )
    state.budget_db.ensure_session_chats((old_key,))
    state.budget_db.append_chat_leg(old_key, current.key)
    # The control-plane link can land before the next provider scan installs
    # the successor in state. Selection must trust the durable chat identity,
    # not fall back to the predecessor merely because it is still resolvable.
    state.update_session(old)
    state.open_questions[old_key] = OpenQuestion(
        old_key,
        "Which approach?",
        datetime.now(UTC),
        "interaction",
        120,
        120,
    )
    reads: list[tuple[str, int]] = []

    class Provider:
        async def read_transcript(self, account, session, after_seq=0):
            reads.append((session.key, after_seq))
            return []

    await collector._reconcile_open_questions(Provider(), account, [old, current])

    assert reads == [(current.key, 0)]
    db.close()


async def test_a_question_derived_for_an_idle_chat_is_not_rewritten_every_scan(tmp_path):
    """#3010: a scan can report an idle chat with no ``last_event_seq``. A question derived
    for it was stamped at 0, so every later pass re-read the whole transcript, cleared the
    question on its old answer and reply, and derived the same one again — a delete and an
    insert of one row per chat per scan, a quarter of the web process's busy time."""
    config = AppConfig(
        accounts=[AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))]
    )
    db = Db(tmp_path / "state.db")
    state = AppState(db=db)
    collector = Collector(config, state)
    account = collector.accounts[0]
    asked = datetime.now(UTC) - timedelta(hours=1)
    session = Session(
        key=f"{account.key}:thread-idle",
        account_key=account.key,
        session_id="thread-idle",
        status=SessionStatus.IDLE,
        last_role="agent",
        last_text="Nothing was lost in the restart.",
        last_activity=asked,
        last_event_seq=0,
    )
    state.update_session(session)
    state.attention_reports[session.key] = AttentionReport("waiting", asked + timedelta(seconds=1))
    transcript = [
        TranscriptEvent(seq=1, role="user", text="Did the restart lose anything?"),
        TranscriptEvent(seq=2, role="assistant", text="Nothing was lost in the restart."),
    ]
    reads: list[int] = []

    class Provider:
        async def read_transcript(self, account, session, after_seq=0):
            reads.append(after_seq)
            return [event for event in transcript if event.seq > after_seq]

    writes: list[str] = []
    for name in ("record_open_question", "delete_open_question"):
        real = getattr(db, name)

        def spy(*args, _real=real, _name=name):
            writes.append(_name)
            return _real(*args)

        setattr(db, name, spy)

    for _ in range(4):
        await collector._reconcile_open_questions(Provider(), account, [session])

    assert state.open_question_for(session.key).question == "Nothing was lost in the restart."
    # Settled after the first full read: later passes read nothing new and write nothing.
    assert reads[-2:] == [2, 2]
    writes_after_settling = len(writes)
    await collector._reconcile_open_questions(Provider(), account, [session])
    assert len(writes) == writes_after_settling
    db.close()


async def test_a_question_answered_between_the_scan_and_the_read_still_clears(tmp_path):
    """Review of #3021: the transcript read runs after the scan. A question the stale
    scan still names, answered and replied to before the read, must not be re-stamped at
    the read's end, or the next pass reads nothing and the answered question stays open."""
    config = AppConfig(
        accounts=[AccountConfig(provider="codex", label="main", config_dir=str(tmp_path))]
    )
    state = AppState()
    collector = Collector(config, state)
    account = collector.accounts[0]
    asked = datetime.now(UTC) - timedelta(minutes=5)
    key = f"{account.key}:thread-raced"
    stale = Session(
        key=key,
        account_key=account.key,
        session_id="thread-raced",
        status=SessionStatus.IDLE,
        question="Merge it?",
        last_role="agent",
        last_text="Merge it?",
        last_activity=asked,
        last_event_seq=10,
    )
    state.update_session(stale)
    state.open_questions[key] = OpenQuestion(key, "Merge it?", asked, "interaction", 10, 10)
    transcript = [
        TranscriptEvent(seq=10, role="assistant", text="Merge it?"),
        TranscriptEvent(seq=11, role="user", text="yes"),
        TranscriptEvent(seq=12, role="assistant", text="Merged."),
    ]

    class Provider:
        async def read_transcript(self, account, session, after_seq=0):
            return [event for event in transcript if event.seq > after_seq]

    await collector._reconcile_open_questions(Provider(), account, [stale])
    fresh = replace(
        stale,
        question=None,
        last_text="Merged.",
        last_activity=asked + timedelta(minutes=1),
        last_event_seq=12,
    )
    state.update_session(fresh)
    await collector._reconcile_open_questions(Provider(), account, [fresh])

    assert state.open_question_for(key) is None
