from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.assistant import (
    ATTENTION_CUTOFF,
    AssistantInsight,
    AssistantService,
    AssistantView,
)
from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.db import Db
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import (
    Capability,
    IssueRef,
    PendingInteraction,
    PullRequestRef,
    Session,
    SessionStatus,
)
from agentdeck.state import WORKING_STAMP_MAX_AGE_S, AppState


class _StubResolver:
    def __init__(self, contexts=None):
        self._contexts = contexts or {}
        self.forces = []

    async def resolve(self, sessions, *, force=False):
        self.forces.append(force)
        return dict(self._contexts)

    def reconcile_cached_pulls(self, context):
        # The real resolver peeks the shared cache. A stub says "nothing moved"
        # unless a test sets `moved_to`, which is then what the cache holds.
        moved_to = getattr(self, "moved_to", None)
        return None if moved_to is None or moved_to == context else moved_to

    def reconcile_cached_pulls_many(self, contexts):
        return {
            key: moved
            for key, context in contexts.items()
            if (moved := self.reconcile_cached_pulls(context)) is not None
        }


def _config(tmp_path, **assistant):
    return AppConfig(
        assistant=AssistantConfig(enabled=True, **assistant),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )


def _service(tmp_path, *, state=None, resolver=None):
    return AssistantService(
        _config(tmp_path),
        state or AppState(),
        context_resolver=resolver or _StubResolver(),
    )


def test_a_timestamp_only_assistant_scan_does_not_publish(tmp_path):
    state = AppState()
    assistant = _service(tmp_path, state=state)
    commits = []
    state.assistant_changed = lambda: commits.append(True)
    first = AssistantView(state="ready", analyzed_at=datetime.now(UTC))
    assistant._commit_view(first)
    assert commits == [True]

    assistant._commit_view(
        replace(first, analyzed_at=first.analyzed_at + timedelta(seconds=5)),
    )
    assert commits == [True]


def _recently(minutes_ago: float) -> datetime:
    """A distinct, ordered, still-fresh activity stamp.

    Relative to the clock on purpose: the triage age gate (#944) stops carding a
    chat quiet for 72 hours, so a fixture that wants a card and a stable order
    cannot use a fixed calendar date that eventually drifts past the cutoff.
    """
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


def _finished(tmp_path, **overrides):
    values = dict(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="Build the prototype",
        initial_prompt="Build the local prototype",
        last_prompt="Build it",
        last_text="I finished the prototype.",
        last_role="agent",
        show_when_idle=True,
        # Attention is activity newer than the last open (#2599), so a fixture
        # chat has to have moved at some point to be able to ask for any.
        last_activity=_recently(5),
    )
    values.update(overrides)
    return Session(**values)








async def test_a_spawned_agent_does_not_outrank_the_question_its_parent_asked(tmp_path):
    # Issue #1878: #617's roll-up is about a chat that handed you nothing. Once
    # the chat has asked you something, work it only borrowed from a spawned
    # agent must not paper over the question — that is what sent a chat blocked
    # on AskUserQuestion to the dashboard as Working, with no waiting pill.
    state = AppState()
    parent = _finished(
        tmp_path,
        key="codex:test:parent",
        session_id="parent",
        status=SessionStatus.LIVE,
        question="Close PR 1773 or finish it?",
    )
    subagent = _finished(
        tmp_path,
        key="codex:test:drafting",
        session_id="drafting",
        kind="subagent",
        parent_session_key=parent.key,
        status=SessionStatus.LIVE,
        thinking=True,
    )
    state.update_session(parent)
    state.update_session(subagent)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()

    presented = state.session_presentation().display(parent)
    assert presented.thinking is False
    assert presented.is_waiting is True
    assert parent.key not in assistant.deckhand_statuses([presented])


async def test_a_subagent_stop_cards_its_top_level_parent(tmp_path):
    state = AppState()
    parent = _finished(tmp_path, key="codex:test:parent", session_id="parent")
    subagent = _finished(
        tmp_path,
        key="codex:test:drafting",
        session_id="drafting",
        kind="subagent",
        parent_session_key=parent.key,
        status=SessionStatus.LIVE,
        thinking=True,
    )
    state.update_session(parent)
    state.update_session(subagent)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()

    assert assistant.view.insights == ()
    state.update_session(replace(subagent, thinking=False, status=SessionStatus.IDLE))
    await assistant.refresh()

    assert [item.session_key for item in assistant.view.insights] == [parent.key]
    assert subagent.key not in _cards(assistant)


async def test_parent_stops_only_after_its_whole_descendant_tree_stops(tmp_path):
    state = AppState()
    parent = _finished(
        tmp_path,
        key="codex:test:parent",
        session_id="parent",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        kind="subagent",
        parent_session_key=parent.key,
        status=SessionStatus.LIVE,
        thinking=True,
    )
    grandchild = _finished(
        tmp_path,
        key="codex:test:grandchild",
        session_id="grandchild",
        kind="subagent",
        parent_session_key=child.key,
        status=SessionStatus.LIVE,
        thinking=True,
    )
    for session in (parent, child, grandchild):
        state.update_session(session)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()
    assert assistant.view.insights == ()
    assert assistant._attention_owner_keys() == {parent.key}

    state.update_session(replace(child, thinking=False, status=SessionStatus.IDLE))
    state.update_session(replace(grandchild, thinking=False, status=SessionStatus.IDLE))
    await assistant.refresh()
    assert assistant.view.insights == ()

    state.update_session(replace(parent, thinking=False, status=SessionStatus.IDLE))
    await assistant.refresh()
    assert [item.session_key for item in assistant.view.insights] == [parent.key]














@pytest.mark.parametrize("version", [10, 11])
def test_an_old_checkpoint_keeps_its_contexts_and_ignores_the_rest(tmp_path, version):
    """Only git contexts carry over: the inbox went in #2599, the stop edge and the
    push debt (``unsent``, ``asking``) when Deckhand stopped pushing in #3348."""
    db = Db(tmp_path / "agentdeck.db")
    session = _finished(tmp_path)
    looked_at = _recently(30)
    shipped = GitContext(
        "eerovil/agentdeck",
        "feature",
        False,
        (PullRequestContext("eerovil/agentdeck", 1, "Done", "u", "merged"),),
        pulls_complete=True,
    )
    db.record_assistant_checkpoint(
        {
            "version": version,
            "view": {"summary": "1 agent", "insights": [], "analyzed_at": None},
            "inbox": [
                {
                    "session_key": "codex:test:long-gone",
                    "kind": "finished",
                    "headline": "finished",
                    "detail": "",
                    "cause": "agent_handoff",
                }
            ],
            "working": [session.key],
            "last_look": looked_at.isoformat(),
            "account_last_look": {},
            "unsent": [{"session_key": session.key, "headline": "finished"}],
            "asking": [session.key],
            "contexts": {session.key: shipped.as_json()},
        }
    )
    state = AppState(db=db)
    state.update_session(session)

    assistant = _service(tmp_path, state=state)

    assert assistant.view.insights == ()
    assert assistant.contexts[session.key].has_merged_pr
    payload = assistant._checkpoint_payload()
    assert payload == {"version": 11, "contexts": payload["contexts"]}
    assert session.key in payload["contexts"]


def test_a_checkpoint_older_than_v10_is_ignored(tmp_path):
    db = Db(tmp_path / "agentdeck.db")
    db.record_assistant_checkpoint(
        {"version": 9, "view": {"summary": "x", "insights": []}, "contexts": {}}
    )
    assistant = _service(tmp_path, state=AppState(db=db))

    assert dict(assistant.contexts) == {}


def test_statuses_include_finished_and_hide_working_stale_state(tmp_path):
    finished = _finished(tmp_path)
    working = _finished(
        tmp_path,
        key="codex:test:working",
        session_id="working",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    state = AppState()
    state.update_session(finished)
    state.update_session(working)
    assistant = _service(tmp_path, state=state)
    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(finished.key, "finished", "finished"),),
    )

    statuses = assistant.deckhand_statuses([finished, working])
    assert statuses[finished.key].state == "finished"
    assert working.key not in statuses


def test_view_and_insight_remain_importable_from_assistant():
    view = AssistantView(
        state="ready", insights=(AssistantInsight("k", "waiting", "waiting"),)
    )
    assert replace(view, state="idle").state == "idle"


# --- a card its poller is still going to release is not a handoff (#552) ---

def _poller_card(root, key, card, *, org="eerovil", instance="agentdeck"):
    """One installed Poller Instance holding exactly this card record."""
    from agentdeck.poller_engine.state import EnginePaths, StateStore

    base = root / "instances" / instance
    base.mkdir(parents=True, exist_ok=True)
    (base / "poller.toml").write_text(
        f'contract = 1\n\n[board]\norg = "{org}"\nproject_number = 1\n'
    )
    store = StateStore(EnginePaths.from_base(base / "state.json"))
    with store.transaction() as state:
        state["issues"] = {key: card}


def _worker_service(tmp_path, state, poller_root):
    from agentdeck.config import PollerConfig

    config = _config(tmp_path)
    return AssistantService(
        config.model_copy(update={"poller": PollerConfig(root=str(poller_root))}),
        state,
        context_resolver=_StubResolver(),
    )


def _kanban_worker(tmp_path, **overrides):
    return _finished(
        tmp_path,
        worker_type="kanban",
        issue_url="https://github.com/eerovil/agentdeck/issues/549",
        **overrides,
    )










async def test_a_pending_interaction_card_ends_the_mute_and_still_publishes(
    tmp_path, monkeypatch
):
    """#1861: the unmute runs on the real ``AppState``, mid-refresh.

    ``_unmute_blocked_targets`` sits between the inbox and the committed view, so
    an exception there loses no card visibly — it silently freezes the whole
    panel at the last view while the triage loop swallows the error once a
    minute. It had been passing an argument ``release_mute_lease`` no longer
    takes since the Secretary was deleted (#1851), and every existing refresh
    test drives a session without INTERACT, so nothing ever reached this line.
    Asserting the view *and* the lease keeps both halves covered.
    """
    state = AppState()
    session = _finished(
        tmp_path,
        question="Proceed?",
        capabilities=frozenset({Capability.INTERACT}),
    )
    interaction = PendingInteraction(
        id="interaction-1",
        kind="question",
        thread_id=session.session_id,
        turn_id=None,
        title="Claude is asking",
    )
    monkeypatch.setattr(
        "agentdeck.assistant.pending_interaction_for",
        lambda s, accounts: interaction if s.key == session.key else None,
    )
    assistant = _service(tmp_path, state=state)
    state.grant_mute_lease(session.key)
    assert state.session_is_muted(session.key)

    await _stop(assistant, state, session)

    assert [insight.session_key for insight in assistant.view.insights] == [session.key]
    assert not state.session_is_muted(session.key)












def test_attention_ownership_reads_chat_identity_once_for_the_whole_deck(tmp_path):
    """#1303: this walks every session the deck holds, and it is reached from the
    Deckhand refresh, the done bar and every pill. One chat lookup per session
    meant a `PRAGMA data_version` round trip per session, several times a second."""

    def owner_pass(directory, session_count):
        db = Db(directory / "agentdeck.db")
        state = AppState(db=db)
        for index in range(session_count):
            state.update_session(
                _finished(directory, key=f"codex:test:t{index}", session_id=f"t{index}")
            )
        assistant = _service(directory, state=state)
        per_session = []
        snapshots = []
        reader = db.chat_for_session
        db.chat_for_session = lambda key: (per_session.append(key), reader(key))[1]
        whole = db.chat_snapshot
        db.chat_snapshot = lambda: (snapshots.append(1), whole())[1]

        owners = assistant._attention_owner_keys()

        assert owners == {f"codex:test:t{index}" for index in range(session_count)}
        assert per_session == []  # never one lookup per session
        return len(snapshots)

    small = (tmp_path / "small")
    large = (tmp_path / "large")
    small.mkdir()
    large.mkdir()

    # The whole mapping is read a fixed number of times per pass. What matters is
    # that the number does not grow with the deck.
    assert owner_pass(small, 4) == owner_pass(large, 40)


async def test_triage_does_not_need_the_display_projection(tmp_path):
    """The loop wakes every five seconds; the deck usually has not moved (#1310).

    Building the whole projection to discover that was the second-largest cost
    on the web worker's event-loop thread. The revision counter answers the same
    question by comparing two integers.
    """
    state = AppState()
    assistant = _service(tmp_path, state=state)
    session = _finished(tmp_path)
    state.update_session(session)
    builds = []
    original = state.session_presentation
    state.session_presentation = lambda: (builds.append(1), original())[1]

    # Scanned, not due, not forced, and nothing has changed since the last look.
    # The scan state matters since #1707: an account with no initial scan holds
    # the loop before it ever reaches the cheap gates this test is about.
    for account in assistant.accounts:
        state.sessions_scanned(account.key)
    assistant._known_unscanned_accounts = frozenset()
    assistant._last_run = asyncio.get_running_loop().time()
    assistant._known_sessions_revision = state.sessions_revision
    assistant._known_attention_inputs = assistant._attention_input_signature()
    assistant._force = False

    task = asyncio.create_task(assistant._loop())
    assistant._wake.set()
    await asyncio.sleep(0.05)
    assert builds == []

    # A real change is still picked up without consulting hierarchy.
    state.update_session(replace(session, title="Changed evidence"))
    assistant._wake.set()
    await asyncio.sleep(0.05)
    task.cancel()
    assert builds == []
    assert assistant.view.state == "ready"


async def test_active_transcript_churn_does_not_rebuild_deckhand_projection(tmp_path):
    state = AppState()
    session = _finished(
        tmp_path,
        thinking=True,
        status=SessionStatus.LIVE,
        worker_type="kanban",
        issue_status_kind="open",
    )
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    builds = []
    original = state.session_presentation
    state.session_presentation = lambda: (builds.append(1), original())[1]
    for account in assistant.accounts:
        state.sessions_scanned(account.key)
    assistant._known_unscanned_accounts = frozenset()
    assistant._last_run = asyncio.get_running_loop().time()
    assistant._known_sessions_revision = state.sessions_revision
    assistant._known_attention_inputs = assistant._attention_input_signature()
    assistant._force = False

    task = asyncio.create_task(assistant._loop())
    state.update_session(replace(session, last_text="one more streaming chunk"))
    assistant._wake.set()
    await asyncio.sleep(0.05)
    assert builds == []

    state.update_session(replace(session, last_text="claude:blocked on operator input"))
    assistant._wake.set()
    await asyncio.sleep(0.05)
    assert builds == []
    assert assistant.view.insights == ()

    state.update_session(
        replace(session, thinking=False, last_text="the finished handoff")
    )
    assistant._wake.set()
    await asyncio.sleep(0.05)
    task.cancel()
    assert builds == []
    assert [item.session_key for item in assistant.view.insights] == [session.key]


async def test_a_throwing_triage_pass_says_so_on_the_panel(tmp_path):
    """A pass that raises must not read as a calm day (#1861).

    When ``refresh`` throws, the published view stays exactly as the last good
    pass left it, so every row, pill and push is stale while the panel still
    looks current — the #1861 freeze ran for hours on one WARNING a minute. The
    rows stay, since they are the best we have, but the panel now says they are
    no longer being checked.
    """
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, state=state)
    for account in assistant.accounts:
        state.sessions_scanned(account.key)
    assistant._known_unscanned_accounts = frozenset()

    async def _throw(**kwargs):
        raise TypeError("release_mute_lease() got an unexpected keyword argument")

    assistant.refresh = _throw

    task = asyncio.create_task(assistant._loop())
    assistant.request_refresh()
    await asyncio.sleep(0.05)
    task.cancel()

    assert assistant.view.error is not None
    assert "Attention triage is failing" in assistant.view.error
    assert "release_mute_lease" in assistant.view.error


def test_attention_signature_tracks_status_and_delegation_changes(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    original = assistant._attention_input_signature()

    state.update_session(replace(session, status=SessionStatus.REMOTE))
    assert assistant._attention_input_signature() != original

    after_status = assistant._attention_input_signature()
    state.delegation_parents[session.key] = "claude_code:test:parent"
    assert assistant._attention_input_signature() != after_status


# --- the one rule: a chat stopping working is the only attention (#1450) ------










# --- the rule: a chat stopping working is the only attention (#1450) ---------
#
# Since #2599 attention is read from the deck again: a chat at rest whose newest
# activity is after its last open needs you. The working -> not-working edge
# still matters, but only to decide which of those cards is worth a push.


async def _stop(assistant, state, session):
    """Drive one chat through the edge: working, observed, then at rest."""
    state.update_session(replace(session, thinking=True, status=SessionStatus.LIVE))
    await assistant.refresh()
    state.update_session(session)
    await assistant.refresh()


def _cards(assistant):
    return {insight.session_key: insight for insight in assistant.view.insights}


async def test_a_chat_at_rest_that_moved_since_it_was_opened_cards(tmp_path):
    """No edge needed: the card is a read of the deck, so a restart cannot lose it."""
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_opening_a_chat_clears_its_card(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()
    assert list(_cards(assistant)) == [session.key]

    state.mark_session_opened(session.key)
    await assistant.refresh()

    assert assistant.view.insights == ()
    assert assistant.deckhand_statuses([session]) == {}


async def test_opening_any_leg_of_a_chat_clears_the_chats_card(tmp_path):
    db = Db(tmp_path / "agentdeck.db")
    state = AppState(db=db)
    old_leg = _finished(tmp_path, key="codex:test:old-leg", session_id="old-leg")
    new_leg = _finished(tmp_path, key="codex:test:new-leg", session_id="new-leg")
    db.ensure_session_chats((old_leg.key, new_leg.key))
    db.append_chat_leg(old_leg.key, new_leg.key)
    state.update_session(new_leg)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()
    assert list(_cards(assistant)) == [new_leg.key]

    # The read marker is recorded on the chat's current leg, whichever leg's
    # page was opened, and read back across every leg.
    state.mark_session_opened(old_leg.key)
    await assistant.refresh()

    assert assistant.view.insights == ()


async def test_activity_after_the_open_brings_the_card_back(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    state.mark_session_opened(session.key)
    await assistant.refresh()
    assert assistant.view.insights == ()

    state.update_session(
        replace(session, last_activity=datetime.now(UTC) + timedelta(seconds=5))
    )
    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_activity_before_the_cutoff_never_cards(tmp_path):
    """Thousands of never-opened old chats must not all light up at once."""
    state = AppState()
    old = _finished(tmp_path, last_activity=ATTENTION_CUTOFF - timedelta(days=1))
    at_cutoff = _finished(
        tmp_path,
        key="codex:test:at-cutoff",
        session_id="at-cutoff",
        last_activity=ATTENTION_CUTOFF,
    )
    state.update_session(old)
    state.update_session(at_cutoff)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()

    assert assistant.view.insights == ()


async def test_a_chat_with_no_activity_stamp_never_cards(tmp_path):
    state = AppState()
    session = _finished(tmp_path, last_activity=None)
    assistant = _service(tmp_path, state=state)

    await _stop(assistant, state, session)

    assert assistant.view.insights == ()


async def test_a_delegated_childs_newer_activity_cards_its_opened_parent(tmp_path):
    """The parent was opened, then its child worked on: that is new for the parent."""
    state = AppState()
    parent = _finished(tmp_path, key="codex:test:parent", session_id="parent")
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        is_delegated=True,
        status=SessionStatus.LIVE,
        thinking=True,
    )
    state.update_session(parent)
    state.update_session(child)
    state.mark_delegated_session(child.key, parent.session_id)
    state.mark_session_opened(parent.key)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()
    assert assistant.view.insights == ()  # the child is still working

    state.update_session(
        replace(
            child,
            thinking=False,
            status=SessionStatus.IDLE,
            last_activity=datetime.now(UTC) + timedelta(seconds=5),
        )
    )
    await assistant.refresh()

    assert list(_cards(assistant)) == [parent.key]


async def test_a_chat_that_works_and_stops_cards_on_the_stop(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    state.update_session(replace(session, thinking=True, status=SessionStatus.LIVE))
    await assistant.refresh()
    assert assistant.view.insights == ()  # still working: nothing to hand over

    state.update_session(session)
    await assistant.refresh()

    assert _cards(assistant)[session.key].kind == "finished"


async def test_a_waiting_report_alone_does_not_create_attention(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    state.mark_session_opened(session.key)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()

    state.set_attention_report(session.key, "waiting")
    await assistant.refresh()

    assert assistant.view.insights == ()


async def test_a_report_immediately_rewords_an_existing_card(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)
    assert _cards(assistant)[session.key].kind == "finished"
    assistant._force = False

    watcher = asyncio.create_task(assistant._watch_sessions())
    await asyncio.sleep(0)
    state.set_attention_report(session.key, "waiting")
    await asyncio.sleep(0.01)

    try:
        assert assistant._force is True
        await assistant.refresh()
        assert _cards(assistant)[session.key].kind == "waiting"
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_a_failed_live_predecessor_cannot_steal_a_finished_successors_card(
    tmp_path,
):
    db = Db(tmp_path / "agentdeck.db")
    state = AppState(db=db)
    source_key = "codex:test:failed-source"
    target_key = "codex:test:finished-successor"
    source = _finished(
        tmp_path,
        key=source_key,
        session_id="failed-source",
        status=SessionStatus.LIVE,
        turn_failed=True,
        last_activity=_recently(10),
    )
    target = _finished(
        tmp_path,
        key=target_key,
        session_id="finished-successor",
        last_activity=_recently(1),
    )
    db.ensure_session_chats((source_key, target_key))
    db.append_chat_leg(source_key, target_key)
    state.update_session(source)
    state.update_session(replace(target, status=SessionStatus.LIVE, thinking=True))
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()
    state.set_attention_report(target_key, "finished")
    state.update_session(target)
    # The abandoned provider process can still scan as live after the successor
    # stops. It is failed, so it must remain history rather than reclaiming the
    # chat and making the successor's stop edge disappear from Deckhand (#1713).
    state.update_session(source)
    await assistant.refresh()

    assert state.chat_current_session_key(source_key) == target_key
    assert list(_cards(assistant)) == [target_key]
    assert _cards(assistant)[target_key].kind == "finished"


async def test_a_card_survives_any_number_of_passes(tmp_path):
    """No sample, no rotation: a card cannot flicker out because a pass missed it.

    This is the #1450 defect itself — cards for real handoffs appeared and
    vanished within seconds as the bounded triage window rotated past them.
    """
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    for _ in range(20):
        await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_time_alone_never_removes_a_card(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    # Nothing about the chat changed, so its card is exactly as outstanding as
    # it was however many passes look at it.
    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_opening_is_what_clears_a_card_and_a_new_turn_raises_another(tmp_path):
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    state.mark_session_opened(session.key)
    await assistant.refresh()
    assert assistant.view.insights == ()

    # Nothing brings it back on its own — not another pass, not a changed title.
    state.update_session(replace(session, title="Renamed after reading"))
    await assistant.refresh()
    assert assistant.view.insights == ()

    # Working again and stopping again is a new handoff, and does.
    await _stop(
        assistant,
        state,
        replace(session, last_activity=datetime.now(UTC) + timedelta(seconds=5)),
    )
    assert list(_cards(assistant)) == [session.key]


async def test_resuming_work_clears_the_card_without_a_press(tmp_path):
    """Safe only because the edge guarantees a fresher card when it stops again."""
    state = AppState()
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    state.update_session(replace(session, thinking=True, status=SessionStatus.LIVE))
    await assistant.refresh()
    assert assistant.view.insights == ()

    state.update_session(session)
    await assistant.refresh()
    assert list(_cards(assistant)) == [session.key]


async def test_a_deck_delivery_makes_a_chat_working_before_its_transcript_moves(
    tmp_path,
):
    """The deck caused the turn, so it does not wait to read about it.

    Without this the provider scan interval is a window in which a whole turn can
    start and finish unobserved, losing the card its stop should have raised.
    """
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()

    state.note_session_sent(session.key)
    await assistant.refresh()
    assert assistant.view.insights == ()  # working, on the deck's own say-so

    state.update_session(
        replace(session, last_activity=datetime.now(UTC) + timedelta(seconds=1))
    )
    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]
    assert session.key not in state.session_sent_at


async def test_a_chat_that_goes_silent_after_a_delivery_stops_being_working(tmp_path):
    """#2003, at the root. The working stamp has to expire.

    Every route that leaves a stamp behind — and six separate ones were found on
    this card, from a callback retry to a web process cancelled mid-send — ends
    in the same place: a stamp nothing will ever clear, because clearing it takes
    either the chat acting or a caller handing it back, and a chat whose turn died
    silently does neither. Unbounded, one such stamp pins the chat under Working
    with nothing running, and it never becomes a card.
    """
    state = AppState()
    # Quiet since well before the deck wrote to it: any activity after the send
    # would count as the reply and clear the stamp on its own.
    session = _finished(
        tmp_path, last_activity=datetime.now(UTC) - timedelta(seconds=WORKING_STAMP_MAX_AGE_S * 3)
    )
    state.update_session(session)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()

    # Handed work, and then total silence — no activity, no progress, ever.
    state.note_session_sent(session.key)
    assert state.session_awaiting_reply(state.sessions[session.key]) is True
    await assistant.refresh()
    assert assistant.view.insights == ()

    # Still inside the window, so nothing changes.
    state.session_sent_at[session.key] = datetime.now(UTC) - timedelta(
        seconds=WORKING_STAMP_MAX_AGE_S - 30
    )
    assert state.session_awaiting_reply(state.sessions[session.key]) is True

    # Past it. The chat has done nothing at all since the deck wrote to it, which
    # is a stuck chat rather than a busy one.
    state.session_sent_at[session.key] = datetime.now(UTC) - timedelta(
        seconds=WORKING_STAMP_MAX_AGE_S + 1
    )
    assert state.session_awaiting_reply(state.sessions[session.key]) is False
    # Dropped, not just reported false, so the stop edge is raised once.
    assert session.key not in state.session_sent_at

    await assistant.refresh()
    assert list(_cards(assistant)) == [session.key]


async def test_a_busy_chat_is_never_cut_off_by_the_working_ceiling(tmp_path):
    """The ceiling must only ever reach a chat that has produced nothing.

    A chat that is genuinely working moves its own activity past the stamp, which
    clears it on the next read — long before any ceiling is in sight. If this ever
    fails, the ceiling has started cutting off live chats, which is worse than the
    bug it exists for.
    """
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)

    state.session_sent_at[session.key] = datetime.now(UTC) - timedelta(
        seconds=WORKING_STAMP_MAX_AGE_S * 10
    )
    # One sign of life, well after the send.
    state.update_session(replace(session, last_activity=datetime.now(UTC)))

    assert state.session_awaiting_reply(state.sessions[session.key]) is False
    assert session.key not in state.session_sent_at


async def test_a_delivery_that_reached_nothing_does_not_hold_a_chat_working(tmp_path):
    """The optimistic stamp has to be taken back, or the chat never cards again.

    ``note_session_sent`` fires before the provider has taken anything, and only
    the chat's own activity clears it — which a chat the deck failed to reach
    never produces. Left set, the chat is classified as working for good and the
    stop that should raise its card is swallowed.
    """
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(replace(session, thinking=True, status=SessionStatus.LIVE))
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()

    # The deck starts handing over, and the provider refuses it.
    state.note_session_sent(session.key)
    state.clear_session_sent(session.key)
    assert state.session_awaiting_reply(session) is False

    # The turn it was already running ends.
    state.update_session(session)
    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_a_turn_that_lands_entirely_between_two_looks_still_cards(tmp_path):
    """Never observed working — a fast reply, or a stop inside a restart window.

    The plain edge cannot see this, and losing it loses the handoff silently,
    which is the failure the inbox exists to prevent.
    """
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    state.mark_session_opened(session.key)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()
    assert assistant.view.insights == ()

    # One observation later the chat is at rest again, but it has clearly run.
    state.update_session(
        replace(session, last_activity=datetime.now(UTC) + timedelta(seconds=30))
    )
    await assistant.refresh()

    assert list(_cards(assistant)) == [session.key]


async def test_the_loop_cards_a_turn_that_lands_entirely_between_two_looks(tmp_path):
    """The cheap wake gates must not swallow the fast-turn fallback.

    A turn can start and finish between two Deckhand projections. Its resting
    transcript evidence is then the only signal that reaches ``refresh()``,
    where the moved-since-last-look check creates the handoff.
    """
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    state.mark_session_opened(session.key)
    assistant = _service(tmp_path, state=state)
    for account in assistant.accounts:
        state.sessions_scanned(account.key)
    assistant._known_unscanned_accounts = frozenset()
    await assistant.refresh()
    assert assistant.view.insights == ()
    assistant._known_visible_session_keys = {session.key}
    assistant._known_attention_population = set()
    assistant._known_sessions_revision = state.sessions_revision
    assistant._known_attention_inputs = assistant._attention_input_signature()
    assistant._last_run = asyncio.get_running_loop().time()
    assistant._force = False

    task = asyncio.create_task(assistant._loop())
    state.update_session(
        replace(
            session,
            last_text="A fast final reply",
            last_activity=datetime.now(UTC) + timedelta(seconds=30),
        )
    )
    assistant._wake.set()
    await asyncio.sleep(0.05)

    try:
        assert list(_cards(assistant)) == [session.key]

        state.update_session(
            replace(session, status=SessionStatus.LIVE, thinking=True)
        )
        assistant._wake.set()
        await asyncio.sleep(0.05)

        assert assistant.view.insights == ()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "overrides, expected_cause",
    [
        ({"question": "Shall I drop the column?"}, "session_waiting"),
        ({"stalled": True}, "stalled"),
        ({"turn_failed": True}, "turn_failed"),
    ],
)
async def test_the_reason_a_chat_stopped_names_the_card(
    tmp_path, overrides, expected_cause
):
    """The edge decides there is a card; these only choose the words."""
    state = AppState()
    session = _finished(tmp_path, **overrides)
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    card = _cards(assistant)[session.key]
    assert (card.kind, card.cause) == ("waiting", expected_cause)


async def test_a_dead_process_reads_as_waiting_not_as_completion(tmp_path):
    state = AppState()
    session = _finished(
        tmp_path,
        status=SessionStatus.IDLE,
        last_role="tool",
        capabilities=frozenset({Capability.INJECT}),
    )
    assistant = _service(tmp_path, state=state)
    await _stop(assistant, state, session)

    assert _cards(assistant)[session.key].cause == "died_mid_turn"


async def test_github_state_never_creates_a_card(tmp_path):
    """A merged pull request leaves the chat exactly as it was.

    Under the rule that is not attention. It remains decoration on a card raised
    for its own reason, which is why the pill can be stale without costing
    anything.
    """
    state = AppState()
    session = _finished(
        tmp_path,
        pull_requests=(PullRequestRef("eerovil/agentdeck", 1),),
    )
    state.update_session(session)
    state.mark_session_opened(session.key)
    assistant = _service(tmp_path, state=state)
    await assistant.refresh()

    assistant._contexts.update(
        {
            session.key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-1450",
                dirty=False,
                pull_requests=(
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=1,
                        title="Ship the inbox",
                        url="url",
                        status="merged",
                    ),
                ),
                pulls_complete=True,
            )
        }
    )
    await assistant.refresh()

    assert assistant.view.insights == ()


async def test_cards_are_read_again_after_a_restart(tmp_path):
    """A chat that stopped during one of the deck's ~14 daily deploys still gets its card."""
    db = Db(tmp_path / "deck.db")
    state = AppState(db=db)
    session = _finished(tmp_path)
    assistant = _service(tmp_path, state=state)
    state.update_session(replace(session, thinking=True, status=SessionStatus.LIVE))
    await assistant.refresh()
    assert assistant.view.insights == ()

    # A new web process over the same database; the chat stopped meanwhile.
    restarted_state = AppState(db=db)
    restarted_state.update_session(session)
    restarted = _service(tmp_path, state=restarted_state)

    assert restarted.view.insights == ()  # nothing is stored; it is read
    await restarted.refresh()
    assert list(_cards(restarted)) == [session.key]


async def test_chats_blocked_on_the_operator_sort_above_finished_ones(tmp_path):
    """Two tiers, oldest first inside each.

    A chat that needs an answer outranks one that merely ended. Within a tier the
    oldest is first: the item most likely to be forgotten is the one that has
    been there longest.
    """
    state = AppState()
    assistant = _service(tmp_path, state=state)
    old_finished = _finished(tmp_path, key="codex:test:old", session_id="old")
    blocked = _finished(
        tmp_path, key="codex:test:blocked", session_id="blocked", question="Proceed?"
    )
    new_finished = _finished(tmp_path, key="codex:test:new", session_id="new")

    for session in (old_finished, blocked, new_finished):
        await _stop(assistant, state, session)

    assert [insight.session_key for insight in assistant.view.insights] == [
        "codex:test:blocked",
        "codex:test:old",
        "codex:test:new",
    ]


async def test_a_handoff_is_never_silenced_by_another_chat_working(tmp_path):
    """Nothing hides a card except opening the chat or the chat resuming.

    Deckhand used to suppress a finished handoff while another top-level chat
    worked "the same" issue or worktree. That guess silenced real handoffs — the
    match is structural and cannot know whether the second chat is continuing the
    first one's work or merely sitting in the same checkout — and a silenced card
    is indistinguishable from work nobody has to do.
    """
    state = AppState()
    assistant = _service(tmp_path, state=state)
    issue = IssueRef("eerovil/agentdeck", 1235)
    finished = _finished(tmp_path, key="codex:test:first", session_id="first")
    replacement = _finished(tmp_path, key="codex:test:second", session_id="second")
    state.set_issues(finished.key, (issue,))
    state.set_issues(replacement.key, (issue,))

    await _stop(assistant, state, finished)
    state.update_session(
        replace(replacement, thinking=True, status=SessionStatus.LIVE)
    )
    await assistant.refresh()

    assert list(_cards(assistant)) == [finished.key]


async def test_a_parentless_delegated_chat_owns_its_own_attention(tmp_path):
    """Being created as a delegation is not what makes a chat somebody else's.

    #1441 excluded every delegated chat, parentless ones included, so that a
    helper with missing parentage could not become an operator-facing owner. On
    the live deck that silenced 33 of the 49 chats active in six hours — chats
    the operator works in — with nothing on screen to show they were missing.
    A chat with no visible parent has nobody but the operator (#1454).
    """
    state = AppState()
    orphan = _finished(
        tmp_path, key="codex:test:orphan", session_id="orphan", is_delegated=True
    )
    assistant = _service(tmp_path, state=state)

    await _stop(assistant, state, orphan)

    assert list(_cards(assistant)) == [orphan.key]


async def test_a_delegated_child_under_a_visible_parent_cards_the_parent(tmp_path):
    state = AppState()
    parent = _finished(tmp_path, key="codex:test:parent", session_id="parent")
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        is_delegated=True,
    )
    state.update_session(parent)
    state.update_session(child)
    state.mark_delegated_session(child.key, parent.session_id)
    assistant = _service(tmp_path, state=state)

    await _stop(assistant, state, child)

    assert list(_cards(assistant)) == [parent.key]


async def test_a_child_with_a_temporarily_missing_parent_is_still_exempt(tmp_path):
    state = AppState()
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        is_delegated=True,
    )
    state.update_session(child)
    state.mark_delegated_session(child.key, "missing-parent")
    assistant = _service(tmp_path, state=state)

    await _stop(assistant, state, child)

    assert assistant.view.insights == ()


async def test_child_activity_follows_parent_chat_rollover(tmp_path):
    db = Db(tmp_path / "agentdeck.db")
    state = AppState(db=db)
    old_parent = _finished(
        tmp_path, key="codex:test:old-parent", session_id="old-parent"
    )
    new_parent = replace(
        old_parent,
        key="codex:test:new-parent",
        session_id="new-parent",
        chat_id="parent-chat",
        last_activity=_recently(1),
    )
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        status=SessionStatus.LIVE,
        thinking=True,
        is_delegated=True,
    )
    state.update_session(new_parent)
    state.update_session(child)
    db.ensure_session_chats((old_parent.key, new_parent.key))
    db.append_chat_leg(old_parent.key, new_parent.key)
    state.mark_delegated_session(child.key, old_parent.key)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh()
    assert assistant._attention_owner_keys() == {new_parent.key}
    assert assistant.view.insights == ()

    state.update_session(replace(child, thinking=False, status=SessionStatus.IDLE))
    await assistant.refresh()
    assert [card.session_key for card in assistant.view.insights] == [new_parent.key]


async def test_absent_unread_child_follows_an_absent_old_parent_leg(tmp_path):
    db = Db(tmp_path / "agentdeck.db")
    state = AppState(db=db)
    old_parent_key = "codex:test:old-parent"
    new_parent = _finished(
        tmp_path, key="codex:test:new-parent", session_id="new-parent"
    )
    child_key = "codex:unread:child"
    db.ensure_session_chats((old_parent_key, new_parent.key))
    db.append_chat_leg(old_parent_key, new_parent.key)
    state.update_session(new_parent)
    state.mark_delegated_session(child_key, old_parent_key)
    assistant = _service(tmp_path, state=state)

    await assistant.refresh(unscanned_accounts=frozenset({"codex:unread"}))

    assert assistant.view.insights == ()


def test_the_row_pill_says_the_open_issues_board_column(tmp_path):
    """A card still in a column is read through that column, not its GitHub state.

    "issue in progress" says how far the work has got; "issue open" and "pr open"
    both leave the reader to guess (#1696). The column comes from the poller's own
    state file, so no row costs a GitHub call.
    """
    poller_root = tmp_path / "poller"
    _poller_card(poller_root, "agentdeck#549", {"status": "In progress"})
    state = AppState()
    session = _kanban_worker(tmp_path, issue_status="open", issue_status_kind="open")
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, poller_root)

    pill = assistant.work_item_pills([session])[session.key]

    assert pill.label == "issue in progress"
    assert pill.is_open


def test_the_row_pill_survives_a_poller_that_is_not_there(tmp_path):
    """No board is not a broken row: the pill falls back to the issue's own state."""
    state = AppState()
    session = _kanban_worker(tmp_path, issue_status="open", issue_status_kind="open")
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")

    assert assistant.work_item_pills([session])[session.key].label == "issue open"


def _pr_context(status: str) -> GitContext:
    return GitContext(
        "eerovil/agentdeck",
        "agent/issue-4321",
        False,
        (
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=4321,
                title="Ship the thing",
                url="https://github.com/eerovil/agentdeck/pull/4321",
                status=status,
            ),
        ),
    )


async def test_a_held_context_is_reused_while_the_shared_cache_still_agrees(tmp_path):
    """The whole point of holding it: opening a chat must not shell out again."""
    session = replace(
        _finished(tmp_path),
        pull_requests=(PullRequestRef("eerovil/agentdeck", 4321),),
    )
    resolver = _StubResolver({session.key: _pr_context("merged")})
    assistant = _service(tmp_path, resolver=resolver)
    assistant._contexts.update({session.key: _pr_context("open")})

    assert assistant.session_context(session).pull_requests[0].status == "open"
    await asyncio.gather(*assistant._context_tasks.values())
    assert resolver.forces == []


async def test_a_pull_request_that_moved_in_the_cache_re_resolves_the_context(tmp_path):
    """A merge the webhook already recorded must reach the chat's pill (#1697).

    Registration is unchanged here — same chat, same one pull request — so the
    old "does the registration still match" test would have returned the stale
    open context forever. What changed is the state underneath it, and that is
    what the shared cache knows.
    """
    session = replace(
        _finished(tmp_path),
        pull_requests=(PullRequestRef("eerovil/agentdeck", 4321),),
    )
    resolver = _StubResolver({session.key: _pr_context("merged")})
    resolver.moved_to = _pr_context("merged")
    assistant = _service(tmp_path, resolver=resolver)
    assistant._contexts.update({session.key: _pr_context("open")})

    # The read itself stays non-blocking: it hands back what is known and starts
    # the re-resolve behind the render (#766).
    assert assistant.session_context(session).pull_requests[0].status == "open"
    await asyncio.gather(*assistant._context_tasks.values())
    assert resolver.forces == [False]
    assert assistant._contexts.get(session.key).pull_requests[0].status == "merged"


# --- A delivery moves the pill, instead of the next sweep doing it (#2042) ---
#
# The sweep above is a minute away at worst, because only a Deckhand pass ran it.
# These are about driving the same sweep from a webhook delivery, and about the
# one thing that must not follow from it: a publish when nothing moved.


def _pr_session(tmp_path) -> Session:
    return replace(
        _finished(tmp_path),
        pull_requests=(PullRequestRef("eerovil/agentdeck", 4321),),
    )


def test_a_delivery_publishes_the_pill_the_cache_already_moved(tmp_path):
    session = _pr_session(tmp_path)
    resolver = _StubResolver()
    resolver.moved_to = _pr_context("merged")
    state = AppState()
    published = []
    state.bus.publish = lambda topic, data=None: published.append(topic)
    assistant = _service(tmp_path, state=state, resolver=resolver)
    assistant._contexts.update({session.key: _pr_context("open")})

    assistant.freshen_pull_requests()

    assert assistant._contexts.get(session.key).pull_requests[0].status == "merged"
    # The session list re-renders off the assistant topic, so that is the one to
    # publish; anything else would leave the pill sitting at its old words.
    assert published == ["assistant"]


def test_a_delivery_that_moves_nothing_publishes_nothing(tmp_path):
    """The update-only-on-real-change rule, enforced here rather than at the source.

    The consumer signals on a row it rewrote; whether the deck's own held context
    disagreed with that row is only knowable here, so this is where a no-op stops.
    """
    session = _pr_session(tmp_path)
    state = AppState()
    published = []
    state.bus.publish = lambda topic, data=None: published.append(topic)
    assistant = _service(tmp_path, state=state, resolver=_StubResolver())
    assistant._contexts.update({session.key: _pr_context("open")})

    assistant.freshen_pull_requests()

    assert published == []


def test_a_delivery_does_not_force_a_deckhand_pass(tmp_path):
    """A pull request moving is no reason to re-triage every chat, which costs tokens."""
    resolver = _StubResolver()
    resolver.moved_to = _pr_context("merged")
    assistant = _service(tmp_path, resolver=resolver)
    assistant._contexts.update({_pr_session(tmp_path).key: _pr_context("open")})
    # A fresh service is already primed for its own first pass; clear that, so what
    # is asserted is what this call did rather than what construction did.
    assistant._force = False
    assistant._wake.clear()

    assistant.freshen_pull_requests()

    assert assistant._force is False
    assert not assistant._wake.is_set()


def _reviewed(state, repo: str, sha: str, verdict: str = "pass") -> None:
    """Record what a review decided about one commit, the way the poller does."""
    from agentdeck.models import PollerReviewVerdict

    state.record_review_verdict(
        PollerReviewVerdict(
            repo=repo,
            sha=sha,
            verdict=verdict,
            result="release",
            recorded_at=datetime.now(UTC),
        )
    )


def _pr_context_at(sha: str | None, *, status: str = "open") -> GitContext:
    context = _pr_context(status)
    pull = context.pull_requests[0]
    return replace(
        context,
        pull_requests=(replace(pull, head_sha=sha),),
        head_sha=sha,
    )


def test_the_row_pill_says_the_latest_commit_passed_review(tmp_path):
    """The claim the row makes is about the commit that would merge (#2121)."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at("a" * 40)})
    _reviewed(state, "eerovil/agentdeck", "a" * 40)

    pill = assistant.work_item_pills([session])[session.key]

    assert pill.label == "pr open ✓"
    assert pill.reviewed_sha == "aaaaaaa"
    assert "latest commit aaaaaaa passed review" in pill.title


def test_the_row_pill_drops_the_tick_once_the_head_moves(tmp_path):
    """A verdict about a commit the branch has left says nothing about what would
    merge now, so the row stops claiming it rather than carrying it forward."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at("b" * 40)})
    _reviewed(state, "eerovil/agentdeck", "a" * 40)

    pill = assistant.work_item_pills([session])[session.key]

    assert pill.label == "pr open"
    assert pill.reviewed_sha == ""


def test_the_row_pill_does_not_wear_a_review_that_asked_for_changes(tmp_path):
    """Only a pass. A review that wanted something is not a row's good news."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at("a" * 40)})
    _reviewed(state, "eerovil/agentdeck", "a" * 40, verdict="changes-needed")

    pill = assistant.work_item_pills([session])[session.key]

    assert pill.label == "pr open"
    assert pill.reviewed_sha == ""


def test_the_row_pill_reads_the_verdict_however_the_repository_is_cased(tmp_path):
    """The verdict table matches repositories case-insensitively, and the row must
    not be the one place a differently-cased name loses the pass."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at("a" * 40)})
    _reviewed(state, "EeroVil/AgentDeck", "a" * 40)

    assert assistant.work_item_pills([session])[session.key].reviewed_sha == "aaaaaaa"


def test_the_row_pill_claims_nothing_when_no_commit_can_be_named(tmp_path):
    """No head sha anywhere is no subject, and a row with no verdict recorded is
    the ordinary case — neither may invent a tick."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at(None)})
    _reviewed(state, "eerovil/agentdeck", "a" * 40)

    assert assistant.work_item_pills([session])[session.key].label == "pr open"

    assistant._contexts.update({session.key: _pr_context_at("c" * 40)})
    assert assistant.work_item_pills([session])[session.key].label == "pr open"


def test_the_row_pill_reads_the_verdict_word_however_it_is_cased(tmp_path):
    """Only the session-started review route pins the word to a Literal; the
    poller host hands its own over as free text, so a differently-cased "pass"
    must not be the thing that silently costs a row its tick."""
    state = AppState()
    session = _pr_session(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, tmp_path / "no-poller-here")
    assistant._contexts.update({session.key: _pr_context_at("a" * 40)})
    _reviewed(state, "eerovil/agentdeck", "a" * 40, verdict="  PASS ")

    assert assistant.work_item_pills([session])[session.key].reviewed_sha == "aaaaaaa"


def _bits_card(*, review_passed, heads, status="In review"):
    """A poller card record carrying one published release-bits reading."""
    return {
        "status": status,
        "release_bits": {
            "bits": {
                "rebase-current": True,
                "review-passed": review_passed,
                "tests-pass": True,
            },
            "heads": heads,
            "at": "2026-09-22T10:00:00+00:00",
        },
    }


def test_the_row_pill_keeps_its_tick_across_a_rebase(tmp_path):
    """The whole point of reading the engine's own reading (#2121).

    The engine carries a clean verdict across a rebase by comparing the diff, not
    the commit — so after a rebase there is no verdict recorded against the new
    head, and a commit-keyed lookup goes quiet exactly where the poller still
    holds the review as passed. This repo rebases immediately before every
    validation, so that is the normal path and not an edge.
    """
    poller_root = tmp_path / "poller"
    rebased = "b" * 40
    _poller_card(
        poller_root,
        "agentdeck#549",
        _bits_card(review_passed=True, heads={"eerovil/agentdeck#4321": rebased}),
    )
    state = AppState()
    session = _kanban_worker(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, poller_root)
    assistant._contexts.update({session.key: _pr_context_at(rebased)})
    # The verdict on file is about the commit the rebase replaced, and nothing
    # has been recorded against the new head at all.
    _reviewed(state, "eerovil/agentdeck", "a" * 40)

    pill = assistant.work_item_pills([session])[session.key]

    assert pill.reviewed_sha == "bbbbbbb"
    assert pill.label.endswith("✓")


def test_a_red_or_unknown_review_bit_is_not_a_tick(tmp_path):
    """Unknown is never green anywhere else in the release bits, and not here."""
    for value in (False, None, "yes", 1):
        poller_root = tmp_path / f"poller-{value}"
        head = "b" * 40
        _poller_card(
            poller_root,
            "agentdeck#549",
            _bits_card(
                review_passed=value, heads={"eerovil/agentdeck#4321": head}
            ),
        )
        state = AppState()
        session = _kanban_worker(tmp_path)
        state.update_session(session)
        assistant = _worker_service(tmp_path, state, poller_root)
        assistant._contexts.update({session.key: _pr_context_at(head)})

        pill = assistant.work_item_pills([session])[session.key]

        assert pill.reviewed_sha == "", value


def test_a_bits_reading_taken_before_this_head_says_nothing(tmp_path):
    """A reading covers the commits it read. The head moved after it was taken,
    so the green bit is about a commit that is no longer what would merge."""
    poller_root = tmp_path / "poller"
    _poller_card(
        poller_root,
        "agentdeck#549",
        _bits_card(review_passed=True, heads={"eerovil/agentdeck#4321": "a" * 40}),
    )
    state = AppState()
    session = _kanban_worker(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, poller_root)
    assistant._contexts.update({session.key: _pr_context_at("b" * 40)})

    assert assistant.work_item_pills([session])[session.key].reviewed_sha == ""


def test_a_malformed_bits_reading_does_not_break_a_row(tmp_path):
    """Engine state on disk can be shaped by a rollback or an older engine, and a
    row must fall back to the verdict record rather than crash."""
    head = "a" * 40
    for record in ({}, {"bits": "green"}, {"bits": {"review-passed": True}},
                   {"bits": {"review-passed": True}, "heads": ["x"]}, "green"):
        poller_root = tmp_path / f"poller-{abs(hash(str(record)))}"
        _poller_card(
            poller_root,
            "agentdeck#549",
            {"status": "In review", "release_bits": record},
        )
        state = AppState()
        session = _kanban_worker(tmp_path)
        state.update_session(session)
        assistant = _worker_service(tmp_path, state, poller_root)
        assistant._contexts.update({session.key: _pr_context_at(head)})

        pill = assistant.work_item_pills([session])[session.key]
        assert pill.reviewed_sha == "", record

        # The deck's own verdict record still answers for this commit.
        _reviewed(state, "eerovil/agentdeck", head)
        assert assistant.work_item_pills([session])[session.key].reviewed_sha == "aaaaaaa"


def test_the_board_column_still_reaches_the_pill_beside_the_review_bit(tmp_path):
    """Both facts now come off one card lookup; neither may have cost the other."""
    poller_root = tmp_path / "poller"
    head = "a" * 40
    _poller_card(
        poller_root,
        "agentdeck#549",
        _bits_card(
            review_passed=True,
            heads={"eerovil/agentdeck#4321": head},
            status="In progress",
        ),
    )
    state = AppState()
    session = _kanban_worker(tmp_path, issue_status="open", issue_status_kind="open")
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, poller_root)
    assistant._contexts.update({session.key: _pr_context_at(head)})

    assert assistant.work_item_pills([session])[session.key].label == (
        "issue in progress ✓"
    )


def test_the_row_pill_matches_the_published_head_however_it_is_cased(tmp_path):
    """Two independently-sourced spellings of one commit meet in that comparison."""
    poller_root = tmp_path / "poller"
    _poller_card(
        poller_root,
        "agentdeck#549",
        _bits_card(review_passed=True, heads={"eerovil/agentdeck#4321": "B" * 40}),
    )
    state = AppState()
    session = _kanban_worker(tmp_path)
    state.update_session(session)
    assistant = _worker_service(tmp_path, state, poller_root)
    assistant._contexts.update({session.key: _pr_context_at("b" * 40)})

    assert assistant.work_item_pills([session])[session.key].reviewed_sha == "bbbbbbb"


async def test_opening_a_chat_lets_the_page_go_out_before_the_pass_runs(tmp_path, monkeypatch):
    """#2841: opening a chat wakes Deckhand to clear its card, and the pass runs on the web
    process's event loop. It waits a beat first, so the page that opened the chat is sent
    before the pass holds the loop; the card still clears within that beat."""
    from agentdeck import assistant as assistant_module

    monkeypatch.setattr(assistant_module, "_INITIAL_SCAN_GRACE_S", 0.0)
    monkeypatch.setattr(assistant_module, "_OPEN_SETTLE_S", 0.2)
    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    service = _service(tmp_path, state=state)
    loop = asyncio.get_running_loop()
    ran_at: list[float] = []

    async def recording(**_kwargs):
        ran_at.append(loop.time())

    service.refresh = recording
    watcher = asyncio.create_task(service._watch_sessions())
    runner = asyncio.create_task(service._loop())
    try:
        await asyncio.sleep(0)
        opened = loop.time()
        state.mark_session_opened(session.key)
        await asyncio.sleep(0.1)
        assert ran_at == [], "the pass waits for the page to go out"
        await asyncio.sleep(0.3)
        assert ran_at and ran_at[0] - opened >= 0.19
    finally:
        for task in (watcher, runner):
            task.cancel()
        await asyncio.gather(watcher, runner, return_exceptions=True)


async def test_a_pass_still_saves_its_checkpoint(tmp_path):
    """#2841: the checkpoint a pass saves is written from a thread, and still written."""
    db = Db(tmp_path / "agentdeck.db")
    state = AppState(db=db)
    state.update_session(_finished(tmp_path))
    service = _service(tmp_path, state=state)

    await service.refresh()

    saved = db.load_assistant_checkpoint()
    assert saved is not None
    assert saved == service._checkpoint_payload()
    db.close()


async def test_a_pass_asks_each_chats_current_leg_once(tmp_path, monkeypatch):
    """#2841: a pass asked every session's current leg three or four times over, on the
    event loop. It asks once per session now, and still finds the same cards."""
    state = AppState()
    keys = []
    for index in range(4):
        session = _finished(
            tmp_path,
            key=f"codex:test:thread-{index}",
            session_id=f"thread-{index}",
            last_activity=_recently(5 + index),
        )
        state.update_session(session)
        keys.append(session.key)
    service = _service(tmp_path, state=state)
    real = state.chat_current_session_key
    asked: list[str] = []

    def counting(key, chats=None):
        asked.append(key)
        return real(key, chats)

    monkeypatch.setattr(state, "chat_current_session_key", counting)
    await service.refresh()

    assert sorted(asked) == sorted(keys)
    assert sorted(_cards(service)) == sorted(keys)


async def test_a_mute_lease_running_out_unmutes_without_another_change(tmp_path):
    """No 60 s pass notices a lease end any more, so its end is a deadline (#2865): the
    Deckhand loop wakes at it, drops the lease and republishes the row unmuted, and an
    open chat page wakes at the same moment through ``presentation_deadline``."""
    import asyncio

    from agentdeck.models import MUTE_LEASE_POLICIES, MUTE_SOURCE_BOSS

    state = AppState()
    session = _finished(tmp_path)
    state.update_session(session)
    lease_s = MUTE_LEASE_POLICIES[MUTE_SOURCE_BOSS].lease_seconds
    lease = state.grant_mute_lease(
        session.key, now=datetime.now(UTC) - timedelta(seconds=lease_s - 0.3)
    )
    assert state.sessions[session.key].muted
    assert state.mute_lease_deadline() == lease.expires_at
    assert state.presentation_deadline() is not None
    assert state.presentation_deadline() <= lease.expires_at
    assistant = _service(tmp_path, state=state)
    task = asyncio.create_task(assistant._loop())
    try:
        for _ in range(60):
            if not state.mute_leases and not state.sessions[session.key].muted:
                break
            await asyncio.sleep(0.05)
        assert not state.mute_leases
        assert not state.sessions[session.key].muted
        assert state.mute_lease_deadline() is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
