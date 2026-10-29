"""Partial startup must neither freeze healthy chats nor discard unread history."""

import asyncio
import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest

import agentdeck.assistant as module
from agentdeck.assistant import AssistantService
from agentdeck.events import EventBus
from agentdeck.models import SessionStatus

HEALTHY = "codex:healthy"
UNREAD = "codex:unread"
# After the attention cutoff, so a chat that moved since then can still card.
OLD = module.ATTENTION_CUTOFF + timedelta(hours=1)


class _Store:
    """Persist actual checkpoint JSON across service objects, without the app DB."""

    def __init__(self, path):
        # Like the app's Db, usable from the thread a pass writes its checkpoint in (#2841).
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, body TEXT)"
        )

    def load_assistant_checkpoint(self):
        row = self.conn.execute(
            "SELECT body FROM checkpoint WHERE id=1"
        ).fetchone()
        return json.loads(row[0]) if row else None

    def record_assistant_checkpoint(self, payload):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?)",
                              (json.dumps(payload),))

    def close(self):
        self.conn.close()


class _Contexts:
    def __init__(self):
        self.view = {}

    def restore(self, value):
        self.view = dict(value)

    def retain(self, keys):
        allowed = set(keys)
        self.view = {key: value for key, value in self.view.items() if key in allowed}

    def as_json(self):
        return dict(self.view)


class _State:
    def __init__(self, store=None):
        self.db = store
        self.sessions = {}
        self.scans = {}
        self.sessions_revision = 0
        self.delegation_parents = {}
        self.recorded_delegation_parents = {}
        self.delegated_session_keys = set()
        self.children = {}
        self.bus = EventBus()
        self.changed = asyncio.Event()
        self.scan_read = asyncio.Event()
        self.published = 0
        self.unmuted = []
        self.opened = {}

    def session_scan_revision(self, key):
        self.scan_read.set()
        return self.scans.get(key, 0)

    def chat_current_session_key(self, key, _snapshot=None):
        return key

    def chat_snapshot(self):
        return None

    def chat_session_keys(self, key, _snapshot=None):
        return (key,)

    def chat_last_opened(self, key, _snapshot=None):
        return self.opened.get(key)

    def session_presentation(self):
        nested = {s.key for group in self.children.values() for s in group}
        return SimpleNamespace(
            top_level=tuple(s for s in self.sessions.values() if s.key not in nested),
            children_of=self.children,
            working_count=sum(s.thinking for s in self.sessions.values()),
            display=lambda s: s,
        )

    def session_awaiting_reply(self, session):
        return False

    def attention_report_for(self, key):
        return None

    def assistant_changed(self):
        self.published += 1
        self.changed.set()

    # Deliberately the real ``AppState`` signature, with no ``**kwargs``: a stub
    # that swallows unknown arguments is what let #1861 reach production.
    def release_mute_lease(self, key):
        self.unmuted.append(key)

    def session_is_muted(self, key):
        return False


def _session(account=HEALTHY, name="chat", *, working=False, stamp=OLD, **kwargs):
    fields = dict(
        key=f"{account}:{name}", account_key=account, session_id=name,
        thinking=working, last_activity=stamp, last_progress=stamp,
        is_subagent=False, is_delegated=False, parent_session_key=None,
        pull_requests=(), capabilities=frozenset(), status=SessionStatus.LIVE,
        title=name, display_title=name, chat_id=name, last_prompt="", last_role="assistant",
        last_text="Done", question=None, turn_failed=False, cwd=None, kind="session",
        worker_type=None, subagent_count=0, subagents=(), issues=(), issue_url=None,
        issue_status_kind="", show_when_idle=True, is_waiting=False, stalled=False,
    )
    fields.update(kwargs)
    return SimpleNamespace(**fields)


@pytest.fixture
def build(monkeypatch):
    monkeypatch.setattr(module, "GitContexts", _Contexts)
    monkeypatch.setattr(module, "pending_interaction_for", lambda *_args: None)

    def make(state=None, accounts=(HEALTHY, UNREAD)):
        state = state or _State()
        config = SimpleNamespace(
            assistant=SimpleNamespace(enabled=True),
            poller=SimpleNamespace(root="unused"),
            build_accounts=lambda: [SimpleNamespace(key=key) for key in accounts],
        )
        resolver = SimpleNamespace(
            reconcile_cached_pulls=lambda _context: None,
            reconcile_cached_pulls_many=lambda _contexts: {},
        )
        return AssistantService(config, state, context_resolver=resolver)
    return make


@pytest.fixture
def store(tmp_path):
    value = _Store(tmp_path / "checkpoints.db")
    yield value
    value.close()


def _seed(service, *, account=UNREAD):
    """What a checkpoint carries for an account that has not been read yet."""
    service._contexts.view[f"{account}:saved"] = {"saved": True}


def _cards(service):
    return {card.session_key: card for card in service.view.insights}


async def _wake(service):
    service.state.changed.clear()
    service.request_refresh()
    await asyncio.wait_for(service.state.changed.wait(), 2.0)


async def test_failed_initial_account_no_longer_blocks_healthy_handoffs(build, monkeypatch):
    monkeypatch.setattr(module, "_INITIAL_SCAN_GRACE_S", 0, raising=False)
    service = build()
    state = service.state
    state.scans[HEALTHY] = 1
    session = _session(working=True)
    state.sessions[session.key] = session
    _seed(service)
    await service.start()
    try:
        await _wake(service)
        assert service.view.insights == ()
        session.thinking = False
        await _wake(service)
        assert session.key in _cards(service)
        assert service._contexts.view[f"{UNREAD}:saved"] == {"saved": True}
        assert UNREAD in service.view.error
        assert state.scans.get(UNREAD, 0) == 0
    finally:
        await service.stop()


async def test_normal_startup_still_waits_for_all_initial_scans(build, monkeypatch):
    monkeypatch.setattr(module, "_INITIAL_SCAN_GRACE_S", 1000, raising=False)
    service = build()
    _seed(service)
    state = service.state
    state.scans[HEALTHY] = 1
    before = service._checkpoint_payload()
    await service.start()
    try:
        service.request_refresh()
        await asyncio.sleep(0.02)
        assert service._checkpoint_payload() == before
        assert state.published == 0
        state.scans[UNREAD] = 1
        await _wake(service)
        assert service.view.error is None
    finally:
        await service.stop()


async def test_grace_has_an_end_even_with_no_successful_accounts(build, monkeypatch):
    monkeypatch.setattr(module, "_INITIAL_SCAN_GRACE_S", 0.03, raising=False)
    service = build()
    _seed(service)
    await service.start()
    try:
        service.request_refresh()
        await asyncio.wait_for(service.state.scan_read.wait(), 2.0)
        # The grace's end is a deadline the loop wakes for by itself (#2865).
        await asyncio.wait_for(service.state.changed.wait(), 2.0)
        assert service._contexts.view[f"{UNREAD}:saved"] == {"saved": True}
        assert all(key in service.view.error for key in (HEALTHY, UNREAD))
    finally:
        await service.stop()


@pytest.mark.parametrize("present", [False, True])
async def test_unread_context_survives_partial_reads(build, present):
    service = build()
    _seed(service)
    if present:
        # Even a partial local record must not card or overwrite unread state.
        session = _session(UNREAD, "saved", stamp=OLD + timedelta(minutes=5))
        service.state.sessions[session.key] = session
    for _ in range(3):
        await service.refresh(unscanned_accounts=frozenset({UNREAD}))
    assert service._contexts.view[f"{UNREAD}:saved"] == {"saved": True}
    assert service.view.insights == ()


@pytest.mark.parametrize("restart", [False, True])
async def test_a_turn_that_finished_while_its_account_was_unread_still_cards(
    build, store, restart,
):
    service = build(_State(store))
    _seed(service)
    await service.refresh(unscanned_accounts=frozenset({UNREAD}))
    if restart:
        # Read the JSON saved by the actual service, not a hand-copied object.
        service = build(_State(store))
    fast = _session(UNREAD, "fast", stamp=OLD + timedelta(seconds=10))
    service.state.sessions[fast.key] = fast
    await service.refresh()
    assert fast.key in _cards(service)
    assert service.view.error is None


async def test_successful_empty_scan_cleans_saved_state_without_waiting_for_session_change(
    build, monkeypatch,
):
    monkeypatch.setattr(module, "_INITIAL_SCAN_GRACE_S", 0, raising=False)
    service = build()
    _seed(service)
    service.state.scans[HEALTHY] = 1
    await service.start()
    try:
        await _wake(service)
        service.state.changed.clear()
        old_revision = service.state.sessions_revision
        service.state.scans[UNREAD] = 1
        # A scan event, not a manual force; the empty account has no session diff.
        service.state.bus.publish("session_scans")
        await asyncio.wait_for(service.state.changed.wait(), 2.0)
        assert service.state.sessions_revision == old_revision
        assert f"{UNREAD}:saved" not in service._contexts.view
        assert service.view.error is None
    finally:
        await service.stop()


async def test_removed_account_is_not_preserved_as_an_unread_account(build):
    service = build(accounts=(HEALTHY,))
    _seed(service, account="codex:removed")
    await service.refresh(unscanned_accounts=frozenset({HEALTHY}))
    assert service._contexts.view == {}


@pytest.mark.parametrize("version", [6, 7, 8, 9, 10, 11])
def test_only_v10_and_later_checkpoints_load(build, version):
    service = build()
    _seed(service)
    payload = service._checkpoint_payload()
    payload["version"] = version
    restored = build()
    restored._restore_checkpoint(payload)
    loads = version >= 10
    assert (restored._contexts.view == service._contexts.view) is loads


async def test_unread_chats_mute_leases_are_not_changed(build):
    service = build()
    _seed(service)
    child = _session(UNREAD, "saved", stamp=OLD + timedelta(minutes=5))
    parent = _session(UNREAD, "parent", stamp=OLD + timedelta(minutes=5))
    service.state.sessions = {child.key: child, parent.key: parent}
    service.state.children[parent.key] = [child]
    service.state.delegated_session_keys = {child.key}
    await service.refresh(unscanned_accounts=frozenset({UNREAD}))
    assert service.state.unmuted == []


async def test_absent_unread_child_holds_back_its_healthy_parents_card(build):
    service = build()
    parent = _session(HEALTHY, "parent", working=False)
    child_key = f"{UNREAD}:child"
    service.state.sessions[parent.key] = parent
    service.state.recorded_delegation_parents[child_key] = parent.key

    await service.refresh(unscanned_accounts=frozenset({UNREAD}))

    assert service.view.insights == ()


async def test_unread_prefix_does_not_include_another_account(build):
    account = UNREAD + "-other"
    service = build(accounts=(UNREAD, account))
    session = _session(account, stamp=OLD + timedelta(seconds=1))
    service.state.sessions[session.key] = session
    await service.refresh(unscanned_accounts=frozenset({UNREAD}))
    assert session.key in _cards(service)


@pytest.mark.parametrize("waiting", [False, True])
async def test_normal_complete_snapshot_still_tracks_work_and_operator_handoffs(build, waiting):
    service = build()
    session = _session(working=True)
    service.state.sessions[session.key] = session
    await service.refresh()
    assert service.view.insights == ()
    session.thinking = False
    session.is_waiting = waiting
    await service.refresh()
    assert _cards(service)[session.key].kind == ("waiting" if waiting else "finished")
    assert service.view.error is None


async def test_opening_the_chat_takes_its_card_away(build):
    service = build()
    session = _session(stamp=OLD + timedelta(minutes=5))
    service.state.sessions[session.key] = session
    await service.refresh()
    assert session.key in _cards(service)
    service.state.opened[session.key] = OLD + timedelta(minutes=6)
    await service.refresh()
    assert service.view.insights == ()


async def test_checkpoint_survives_a_new_database_connection(build, tmp_path):
    path = tmp_path / "reload.db"
    first_store = _Store(path)
    first = build(_State(first_store))
    _seed(first)
    await first.refresh(unscanned_accounts=frozenset({UNREAD}))
    first_store.close()
    second_store = _Store(path)
    try:
        second = build(_State(second_store))
        assert second._contexts.view == first._contexts.view
        assert second._contexts.view[f"{UNREAD}:saved"] == {"saved": True}
    finally:
        second_store.close()
