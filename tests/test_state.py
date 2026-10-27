"""AppState-owned Session transitions: apply_session_changes coherence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentdeck import state as state_mod
from agentdeck.db import Db
from agentdeck.models import (
    Capability,
    PullRequestRef,
    Session,
    SessionStatus,
    SubagentProgress,
    UsageSnapshot,
)
from agentdeck.state import (
    OPERATOR_PRESENCE_IDLE_AFTER_S,
    AppState,
    copy_session,
)


def _usage(pct: float) -> UsageSnapshot:
    return UsageSnapshot(
        account_key="claude_code:main",
        five_hour_pct=pct,
        five_hour_resets_at=None,
        seven_day_pct=None,
        seven_day_resets_at=None,
        fetched_at=datetime.now(UTC),
    )


def test_session_presentation_is_shared_until_a_change_or_its_activity_deadline():
    # No cache age any more (ADR 0007, #2865): an unchanged deck is reused however
    # long it sits, and the one clock-derived input is a deadline.
    state = AppState()
    session = Session(
        key="codex:test:one",
        account_key="codex:test",
        session_id="one",
        status=SessionStatus.LIVE,
    )
    state.update_session(session)

    first = state.session_presentation()
    assert state.session_presentation() is first
    assert state._session_presentation_expires_at is None

    state.update_session(replace(session, title="Changed"))
    changed = state.session_presentation()
    assert changed is not first
    assert changed._visible_parent_graph is first._visible_parent_graph
    assert changed.top_level[0].title == "Changed"

    # A subagent count promotes its parent only until STALL_S after its last
    # progress, and nothing is published when that passes: that moment is the deadline.
    stamp = datetime.now(UTC) - timedelta(seconds=state_mod.STALL_S - 60)
    state.update_session(replace(session, subagent_count=1, last_progress=stamp))
    promoted = state.session_presentation()
    assert promoted.display(session).thinking
    assert state._session_presentation_expires_at == stamp + timedelta(
        seconds=state_mod.STALL_S
    )
    assert state.session_presentation() is promoted
    state._session_presentation_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert state.session_presentation() is not promoted


def test_session_presentation_rebuilds_when_another_process_links_a_chat_leg(tmp_path):
    """The runtime writes chat legs straight into the shared database.

    Nothing in the web process announces that, so an in-process invalidation
    cannot see it. The cache therefore keys on the chat snapshot itself, which
    is a fresh object exactly when the chat tables have moved.
    """
    db = Db(tmp_path / "chat.db")
    state = AppState(db=db, budget_db=db)
    source_key = "claude_code:main:source"
    target_key = "codex:alt:target"
    for key in (source_key, target_key):
        state.update_session(
            Session(
                key=key,
                account_key=key.rsplit(":", 1)[0],
                session_id=key.rsplit(":", 1)[1],
                status=SessionStatus.LIVE,
            )
        )

    first = state.session_presentation()
    assert state.session_presentation() is first
    assert len(first.top_level) == 2

    other_process = Db(tmp_path / "chat.db")
    other_process.ensure_session_chats((source_key, target_key))
    other_process.append_chat_leg(source_key, target_key)

    linked = state.session_presentation()
    assert linked is not first
    assert len(linked.top_level) == 1


def test_session_presentation_reuses_parent_graphs_until_parentage_changes(
    monkeypatch,
):
    state = AppState()
    parent = Session(
        key="claude_code:main:parent",
        account_key="claude_code:main",
        session_id="parent",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:main:child",
        account_key="codex:main",
        session_id="child",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key=parent.key,
    )
    state.update_session(parent)
    state.update_session(child)

    builds = 0
    original = state._parents_by_key

    def counted_parents(*args, **kwargs):
        nonlocal builds
        builds += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_parents_by_key", counted_parents)
    first = state.session_presentation()
    assert builds == 1  # the visible tree; the all-session spend roll-up is gone (#2864)

    # Ordinary session state invalidates the display projection, but neither
    # relationship graph.
    state.update_session(replace(child, title="Changed"))
    builds_before_presentation = builds
    changed = state.session_presentation()
    assert changed is not first
    assert changed._visible_parent_graph is first._visible_parent_graph
    assert builds == builds_before_presentation
    assert [session.key for session in changed.children_of[parent.key]] == [child.key]

    # A real parentage change rebuilds the graph and immediately changes the
    # visible tree.
    state.update_session(replace(child, title="Changed", parent_session_key=None))
    builds_before_presentation = builds
    reparented = state.session_presentation()
    assert builds == builds_before_presentation + 1
    assert {session.key for session in reparented.top_level} == {parent.key, child.key}

    state.mark_delegated_session(child.key, parent_session_id=parent.session_id)
    builds_before_presentation = builds
    delegated = state.session_presentation()
    assert builds == builds_before_presentation + 1
    assert [session.key for session in delegated.children_of[parent.key]] == [child.key]


def test_session_presentation_rebuilds_parent_graph_after_scanned_delegation():
    state = AppState()
    parent = Session(
        key="claude_code:main:parent",
        account_key="claude_code:main",
        session_id="parent",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:main:child",
        account_key="codex:main",
        session_id="child",
        status=SessionStatus.LIVE,
    )
    state.update_session(parent)
    state.update_session(child)
    first = state.session_presentation()

    state.set_delegation_parents(parent.account_key, {child.key: parent.key})
    delegated = state.session_presentation()

    assert delegated._visible_parent_graph is not first._visible_parent_graph
    assert [session.key for session in delegated.children_of[parent.key]] == [child.key]


def test_chat_snapshot_revision_moves_when_a_local_chat_is_created(tmp_path):
    db = Db(tmp_path / "chat.db")
    snapshot = db.chat_snapshot()

    db.ensure_session_chats(("codex:main:new",))

    assert db.chat_snapshot() is snapshot
    assert snapshot.revision == 1


def test_session_presentation_rebuilds_parent_graph_after_local_chat_link(tmp_path):
    db = Db(tmp_path / "chat.db")
    state = AppState(db=db, budget_db=db)
    source_key = "claude_code:main:source"
    target_key = "codex:alt:target"
    for key in (source_key, target_key):
        state.update_session(
            Session(
                key=key,
                account_key=key.rsplit(":", 1)[0],
                session_id=key.rsplit(":", 1)[1],
                status=SessionStatus.LIVE,
            )
        )

    first = state.session_presentation()
    snapshot = db.chat_snapshot()
    before_revision = snapshot.revision
    db.append_chat_leg(source_key, target_key)
    assert db.chat_snapshot() is snapshot
    assert snapshot.revision > before_revision

    linked = state.session_presentation()
    assert linked._visible_parent_graph is not first._visible_parent_graph
    assert len(linked.top_level) == 1




def test_a_live_mute_lease_remains_on_the_provider_leg(tmp_path):
    """#1264: a lease is live process state, so it never folds across a chat."""
    db = Db(tmp_path / "chat.db")
    source_key = "claude_code:main:source"
    target_key = "codex:alt:target"
    db.ensure_session_chats((source_key, target_key))
    db.append_chat_leg(source_key, target_key)
    state = AppState(db=db)

    state.grant_mute_lease(source_key)

    assert state.session_is_muted(source_key)
    assert not state.session_is_muted(target_key)
    assert not state.release_mute_lease(target_key)
    assert source_key in state.mute_leases
    assert target_key not in state.mute_leases
    db.close()


def test_warm_usage_seeds_an_empty_gap():
    state = AppState()
    state.warm_usage(_usage(12.0))
    assert state.usage["claude_code:main"].five_hour_pct == 12.0


def test_warm_usage_never_clobbers_a_live_poll():
    state = AppState()
    state.set_usage(_usage(50.0))  # a live poll already landed
    state.warm_usage(_usage(12.0))  # stale warm value must not overwrite it
    assert state.usage["claude_code:main"].five_hour_pct == 50.0


def test_a_rejected_meter_credential_withdraws_the_percentage():
    """#758. A refused poll learned nothing, so the last number is not merely old
    — it is unsupported. Marking it stale left the header rendering ``2%`` for
    hours; every reader must see no meter instead."""
    state = AppState()
    state.set_usage(_usage(2.0))

    with state.bus.subscribe("usage") as events:
        state.mark_usage_credential_rejected("claude_code:main", "401 (host auth.json)")

    assert "claude_code:main" not in state.usage
    assert state.usage_credential_errors["claude_code:main"] == "401 (host auth.json)"
    assert events.get_nowait() == ("usage", None)


def test_the_same_rejection_twice_does_not_re_publish():
    """The poll repeats every cycle; only a change is worth a live swap."""
    state = AppState()
    state.set_usage(_usage(2.0))
    state.mark_usage_credential_rejected("claude_code:main", "401")

    with state.bus.subscribe("usage") as events:
        state.mark_usage_credential_rejected("claude_code:main", "401")

    assert events.get_nowait() is None


def test_a_successful_read_clears_the_rejection():
    state = AppState()
    state.mark_usage_credential_rejected("claude_code:main", "401")
    state.set_usage(_usage(3.0))
    assert state.usage_credential_errors == {}
    assert state.usage["claude_code:main"].five_hour_pct == 3.0


def _session(
    sid: str, status: SessionStatus = SessionStatus.IDLE, *, is_delegated: bool = False
) -> Session:
    return Session(
        key=f"claude_code:main:{sid}",
        account_key="claude_code:main",
        session_id=sid,
        status=status,
        is_delegated=is_delegated,
    )


def test_named_invalidations_own_event_topics():
    state = AppState()

    with state.bus.subscribe("sessions", "usage", "assistant") as events:
        state.sessions_changed()
        state.usage_changed()
        state.assistant_changed()

    assert events.get_nowait()[0] == "sessions"
    assert events.get_nowait() == ("usage", None)
    assert events.get_nowait() == ("assistant", None)
    assert events.get_nowait() is None


def test_operator_presence_is_away_until_recent_input_and_ages_back_to_away():
    state = AppState()
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)

    assert state.operator_presence(now).status == "away"

    with state.bus.subscribe("presence") as events:
        reported = state.report_operator_input(now)

    assert reported.status == "present"
    assert reported.last_input_at == now
    assert reported.idle_for_s == 0
    assert events.get_nowait() == ("presence", None)
    assert events.get_nowait() is None

    overnight = now + timedelta(days=1)
    presence = state.operator_presence(overnight)
    assert presence.status == "away"
    assert presence.idle_for_s == 24 * 60 * 60


def test_operator_presence_uses_a_five_minute_idle_boundary_and_never_rewinds():
    state = AppState()
    first = datetime(2026, 7, 28, 12, tzinfo=UTC)
    latest = first + timedelta(seconds=30)
    state.report_operator_input(latest)

    assert state.operator_presence(
        latest + timedelta(seconds=OPERATOR_PRESENCE_IDLE_AFTER_S - 0.001)
    ).status == "present"
    assert state.operator_presence(
        latest + timedelta(seconds=OPERATOR_PRESENCE_IDLE_AFTER_S)
    ).status == "away"

    state.report_operator_input(first)
    assert state.last_operator_input_at == latest


def test_successful_session_scans_advance_an_account_revision():
    state = AppState()

    with state.bus.subscribe("session_scans") as events:
        state.sessions_scanned("codex:main")
        state.sessions_scanned("codex:main")

    assert state.session_scan_revision("codex:main") == 2
    assert state.session_scan_revision("claude_code:main") == 0
    assert events.get_nowait() == ("session_scans", "codex:main")
    assert events.get_nowait() == ("session_scans", "codex:main")
    assert events.get_nowait() is None




def test_apply_session_changes_publishes_once_and_returns_changed():
    state = AppState()
    session = _session("a")
    state.sessions[session.key] = session

    def project(s: Session) -> None:
        s.status = SessionStatus.LIVE
        s.thinking = True

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([session], project)

    # Mutation lands on the read model's own object (same identity), and the
    # transition publishes exactly once.
    assert changed == [session]
    assert changed[0] is session
    assert session.status is SessionStatus.LIVE
    assert session.thinking is True
    assert events.get_nowait()[0] == "sessions"
    assert events.get_nowait() is None


def test_apply_session_changes_is_silent_on_noop():
    state = AppState()
    session = _session("a", SessionStatus.LIVE)
    state.sessions[session.key] = session

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([session], lambda s: None)

    # No field changed → nothing to announce.
    assert changed == []
    assert events.get_nowait() is None


def test_apply_session_changes_ignores_untracked_field_free_projection():
    # A projection that writes the same value it read is not a change: detection
    # is whole-object, so there is no per-field list to keep in sync.
    state = AppState()
    session = _session("a", SessionStatus.LIVE)
    state.sessions[session.key] = session

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes(
            [session], lambda s: setattr(s, "status", SessionStatus.LIVE)
        )

    assert changed == []
    assert events.get_nowait() is None


def test_apply_session_changes_batches_a_single_publish():
    state = AppState()
    a, b, c = _session("a"), _session("b"), _session("c")
    for s in (a, b, c):
        state.sessions[s.key] = s

    def project(s: Session) -> None:
        if s.session_id in {"a", "c"}:
            s.status = SessionStatus.LIVE

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([a, b, c], project)

    # Only the two that actually changed are reported, and the whole batch
    # publishes once rather than per changed session.
    assert changed == [a, c]
    assert b.status is SessionStatus.IDLE
    assert events.get_nowait()[0] == "sessions"
    assert events.get_nowait() is None


# --- admission (_admit): the delegation-learn + stamp rule ---------------


def test_replace_learns_and_persists_a_self_flagged_delegation():
    db = _FakeDb()
    state = AppState(db=db)

    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=True)])

    assert "claude_code:main:a" in state.delegated_session_keys
    assert db.recorded == [("claude_code:main:a", None)]
    assert state.sessions["claude_code:main:a"].is_delegated is True
    assert [s.key for s in db.upserted[-1]] == ["claude_code:main:a"]


def test_the_scan_only_persists_the_sessions_it_moved():
    """#1348: a scan is authoritative over the account, but re-writing an

    unchanged session's row rewrites the columns it already holds — one write
    per session per scan per account.
    """
    db = _FakeDb()
    state = AppState(db=db)
    state.replace_account_sessions("claude_code:main", [_session("a"), _session("b")])
    assert [s.key for s in db.upserted[-1]] == [
        "claude_code:main:a",
        "claude_code:main:b",
    ]

    moved = _session("b")
    moved.title = "renamed"
    state.replace_account_sessions("claude_code:main", [_session("a"), moved])

    assert [s.key for s in db.upserted[-1]] == ["claude_code:main:b"]


def test_incremental_scan_replaces_only_named_existing_sessions():
    db = _FakeDb()
    state = AppState(db=db)
    state.replace_account_sessions("claude_code:main", [_session("a"), _session("b")])
    moved = _session("a")
    moved.title = "fresh"

    with state.bus.subscribe("sessions") as events:
        assert state.merge_account_sessions("claude_code:main", [moved]) is True

    assert state.sessions["claude_code:main:a"].title == "fresh"
    assert state.sessions["claude_code:main:b"].title is None
    assert [session.key for session in db.upserted[-1]] == ["claude_code:main:a"]
    assert events.get_nowait()[0] == "sessions"
    assert events.get_nowait() is None


def test_incremental_scan_cannot_admit_an_unknown_session():
    state = AppState()
    state.replace_account_sessions("claude_code:main", [_session("a")])

    with pytest.raises(ValueError, match="cannot admit unknown session"):
        state.merge_account_sessions("claude_code:main", [_session("b")])


def test_replace_stamps_a_persisted_key_arriving_unflagged():
    db = _FakeDb()
    state = AppState(db=db)
    state.delegated_session_keys.add("claude_code:main:a")

    # Session arrives without its own delegation flag; admission stamps it.
    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=False)])

    assert state.sessions["claude_code:main:a"].is_delegated is True
    # A pre-known key is not re-persisted.
    assert db.recorded == []


def test_update_session_learns_and_persists_a_self_flagged_delegation():
    db = _FakeDb()
    state = AppState(db=db)

    with state.bus.subscribe("sessions") as events:
        state.update_session(_session("a", is_delegated=True))

    assert "claude_code:main:a" in state.delegated_session_keys
    assert db.recorded == [("claude_code:main:a", None)]
    assert state.sessions["claude_code:main:a"].is_delegated is True
    assert events.get_nowait()[0] == "sessions"


def test_admission_tracks_in_memory_without_a_db():
    # No store: admission still learns the delegation in memory and stamps it,
    # without touching any persistence.
    state = AppState()  # db=None
    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=True)])
    assert "claude_code:main:a" in state.delegated_session_keys
    assert state.sessions["claude_code:main:a"].is_delegated is True


def test_mark_delegated_session_is_unconditional_and_separate_from_admission():
    # mark_delegated_session flags an un-self-flagged, already-stored session and
    # does NOT route through _admit's conditional-learn path.
    db = _FakeDb()
    state = AppState(db=db)
    stored = _session("a", is_delegated=False)
    state.sessions[stored.key] = stored

    state.mark_delegated_session(stored.key, "parent-raw-id")

    assert state.sessions[stored.key].is_delegated is True
    assert state.recorded_delegation_parents[stored.key] == "parent-raw-id"
    assert db.recorded == [("claude_code:main:a", "parent-raw-id")]




def test_registered_pull_requests_are_the_sessions_own_and_publish_on_change():
    state = AppState()
    session = _session("a")
    state.update_session(session)
    key = session.key

    with state.bus.subscribe("sessions") as events:
        state.add_pull_request(key, PullRequestRef("eerovil/agentdeck", 230))
        # Case differs but GitHub repository names do not, so this is the same PR.
        state.add_pull_request(key, PullRequestRef("EeroVil/AgentDeck", 230))

    assert state.pull_requests_for(key) == (PullRequestRef("eerovil/agentdeck", 230),)
    assert state.sessions[key].pull_requests == (PullRequestRef("eerovil/agentdeck", 230),)
    assert events.get_nowait()[0] == "sessions"
    # A duplicate registration changes nothing, so nothing is published.
    assert events.get_nowait() is None

    state.set_pull_requests(
        key,
        (PullRequestRef("eerovil/agentdeck", 231), PullRequestRef("acmecorp/bazaar", 7)),
    )
    assert [ref.number for ref in state.sessions[key].pull_requests] == [231, 7]

    state.remove_pull_request(key, PullRequestRef("acmecorp/bazaar", 7))
    assert state.sessions[key].pull_requests == (PullRequestRef("eerovil/agentdeck", 231),)

    state.set_pull_requests(key, ())
    assert state.pull_requests_for(key) == ()
    assert state.sessions[key].pull_requests == ()


def test_a_pull_request_the_old_worker_registered_is_learned_once_it_drained(tmp_path):
    """#3244: during a deploy the old web worker took a registration after the new one had
    loaded them, so the new worker's router found no Work for the ready event."""
    db = Db(tmp_path / "deck.db")
    new, old = AppState(db=db), AppState(db=db)
    old.add_pull_request("claude_code:main:a", PullRequestRef("eerovil/agentdeck", 3243))
    old.add_pull_request("claude_code:main:b", PullRequestRef("eerovil/agentdeck", 7))
    assert new.pull_requests_for("claude_code:main:a") == ()  # loaded before they landed
    assert {ref.number for ref in new.reload_pull_requests()} == {3243, 7}

    old.add_pull_request("claude_code:main:a", PullRequestRef("eerovil/agentdeck", 3244))
    old.remove_pull_request("claude_code:main:b", PullRequestRef("eerovil/agentdeck", 7))

    assert new.reload_pull_requests() == [PullRequestRef("eerovil/agentdeck", 3244)]
    assert new.pull_requests_for("claude_code:main:a") == (
        PullRequestRef("eerovil/agentdeck", 3243),
        PullRequestRef("eerovil/agentdeck", 3244),
    )
    assert new.pull_requests_for("claude_code:main:b") == ()
    assert new.reload_pull_requests() == []  # nothing new since

    # A known pull request given its Work over there is learned again here (#3392).
    work = "github_issue:eerovil/agentdeck#3242"
    old.add_pull_request("claude_code:main:a", PullRequestRef("eerovil/agentdeck", 3243, work))
    assert [ref.work_id for ref in new.reload_pull_requests()] == [work]
    assert new.pull_requests_for("claude_code:main:a")[0].work_id == work
    assert new.reload_pull_requests() == []


def test_pull_requests_can_be_registered_before_the_session_is_scanned():
    # A worker may register its PR while the provider scan that discovers the
    # session is still in flight; the registration must survive that race.
    state = AppState()
    state.add_pull_request("claude_code:main:a", PullRequestRef("eerovil/agentdeck", 230))

    state.replace_account_sessions("claude_code:main", [_session("a")])

    assert state.sessions["claude_code:main:a"].pull_requests == (
        PullRequestRef("eerovil/agentdeck", 230),
    )


def test_attention_report_survives_final_reply_then_expires_on_next_user_turn():
    state = AppState()
    key = "claude_code:main:a"
    started = datetime.now(UTC)
    state.update_session(
        Session(
            key=key,
            account_key="claude_code:main",
            session_id="a",
            status=SessionStatus.LIVE,
            thinking=True,
            last_role="user",
            last_prompt="First turn",
            last_activity=started,
        )
    )
    report = state.set_attention_report(key, "waiting")

    state.update_session(
        Session(
            key=key,
            account_key="claude_code:main",
            session_id="a",
            status=SessionStatus.LIVE,
            last_role="agent",
            last_prompt="First turn",
            last_text="Which option?",
            last_activity=report.updated_at + timedelta(seconds=1),
        )
    )
    assert state.attention_report_for(key) == report

    state.update_session(
        Session(
            key=key,
            account_key="claude_code:main",
            session_id="a",
            status=SessionStatus.LIVE,
            thinking=True,
            last_role="user",
            last_prompt="Second turn",
            last_activity=report.updated_at + timedelta(seconds=2),
        )
    )
    assert state.attention_report_for(key) is None


















# --- #891: the worker-idle edge -------------------------------------------


def _turning(sid: str, **overrides) -> Session:
    return Session(
        key=f"claude_code:main:{sid}",
        account_key="claude_code:main",
        session_id=sid,
        status=SessionStatus.LIVE,
        **overrides,
    )


def _idle_edges(before: list[Session], after: list[Session]) -> list[str]:
    state = AppState()
    state.replace_account_sessions("claude_code:main", before)
    with state.bus.subscribe("worker_idle") as events:
        state.replace_account_sessions("claude_code:main", after)
        seen = []
        while (event := events.get_nowait()) is not None:
            seen.append(event[1])
    return seen




def _orchestration_idle_edges(
    before: list[Session], after: list[Session]
) -> list[str]:
    state = AppState()
    state.replace_account_sessions("claude_code:main", before)
    with state.bus.subscribe("orchestration_idle") as events:
        state.replace_account_sessions("claude_code:main", after)
        seen = []
        while (event := events.get_nowait()) is not None:
            seen.append(event[1])
    return seen


def test_a_chat_that_stops_working_publishes_the_idle_edge():
    assert _idle_edges([_turning("w1", thinking=True)], [_turning("w1")]) == [
        "claude_code:main:w1"
    ]


def test_a_chat_that_was_already_resting_publishes_nothing():
    """The edge, not the state: otherwise every scan would re-announce everyone."""
    assert _idle_edges([_turning("w1")], [_turning("w1", last_role="user")]) == []


def test_a_chat_that_starts_working_publishes_nothing():
    assert _idle_edges([_turning("w1")], [_turning("w1", thinking=True)]) == []


def test_a_stalled_turn_is_not_an_idle_edge():
    """#891 leaves stalls to the fallback sweep: the turn is still open."""
    assert _idle_edges(
        [_turning("w1", thinking=True)], [_turning("w1", stalled=True)]
    ) == []


def test_a_subagent_finishing_is_not_a_worker_going_idle():
    assert _idle_edges(
        [_turning("w1", thinking=True, kind="subagent")],
        [_turning("w1", kind="subagent")],
    ) == []


def test_the_last_child_finishing_publishes_the_parent_orchestration_idle_edge():
    parent = _turning("parent")
    working_child = _turning(
        "child",
        thinking=True,
        kind="subagent",
        parent_session_key=parent.key,
    )
    finished_child = _turning(
        "child",
        kind="subagent",
        parent_session_key=parent.key,
    )

    assert _orchestration_idle_edges(
        [parent, working_child], [parent, finished_child]
    ) == [parent.key]


def test_embedded_subagents_finishing_publish_the_parent_orchestration_idle_edge():
    working = _turning(
        "parent",
        subagent_count=1,
        subagents=(SubagentProgress(agent_id="child", status="working"),),
    )
    finished = _turning(
        "parent",
        subagents=(SubagentProgress(agent_id="child", status="finished"),),
    )

    assert _orchestration_idle_edges([working], [finished]) == [working.key]


def test_a_parent_with_another_working_child_does_not_publish_orchestration_idle():
    parent = _turning("parent")
    first = _turning(
        "first",
        thinking=True,
        kind="subagent",
        parent_session_key=parent.key,
    )
    second = _turning(
        "second",
        thinking=True,
        kind="subagent",
        parent_session_key=parent.key,
    )
    finished_first = _turning(
        "first",
        kind="subagent",
        parent_session_key=parent.key,
    )

    assert _orchestration_idle_edges(
        [parent, first, second], [parent, finished_first, second]
    ) == []






def test_a_liveness_sweep_that_changes_nothing_does_not_recompute_the_wave(monkeypatch):
    """#1348: the sweep visits every session every few seconds and normally

    changes none of them. Deriving the wave edge from the whole session graph on
    such a tick can only reproduce the answer the last writer published, and
    doing it anyway was the largest single cost in the liveness loop.
    """
    state = AppState()
    session = _session("a", SessionStatus.LIVE)
    state.sessions[session.key] = session
    calls = []
    monkeypatch.setattr(
        AppState,
        "_publish_orchestration_idle",
        lambda self, parent_by_key=None: calls.append(1),
    )

    state.apply_session_changes([session], lambda s: None)
    assert calls == []

    def project(s: Session) -> None:
        s.thinking = True

    state.apply_session_changes([session], project)
    assert calls == [1]


def test_the_full_scan_still_republishes_the_wave_when_nothing_changed(monkeypatch):
    """The republish contract lives on the scan, not on the sweep (#1348)."""
    state = AppState()
    session = _turning("a")
    state.replace_account_sessions("claude_code:main", [session])
    calls = []
    monkeypatch.setattr(
        AppState,
        "_publish_orchestration_idle",
        lambda self, parent_by_key=None: calls.append(1),
    )

    state.replace_account_sessions("claude_code:main", [_turning("a")])
    assert calls == [1]




def test_orchestration_reader_given_no_parent_map_builds_its_own(monkeypatch):
    """The sharing is an optimisation, not a new requirement on callers."""
    state = AppState()
    state.replace_account_sessions("claude_code:main", [_turning("a")])

    builds = []
    real = AppState._parents_by_key

    def counting(self, sessions, parent_index=None):
        builds.append(len(sessions))
        return real(self, sessions, parent_index)

    monkeypatch.setattr(AppState, "_parents_by_key", counting)
    state._publish_orchestration_idle()
    assert len(builds) == 1




def test_a_chat_the_deck_has_never_seen_before_is_not_an_edge():
    """A first sight is not a transition — the previous turn is simply unknown."""
    assert _idle_edges([], [_turning("w1")]) == []


def test_a_single_session_update_publishes_the_edge_too():
    state = AppState()
    state.update_session(_turning("w1", thinking=True))
    with state.bus.subscribe("worker_idle") as events:
        state.update_session(_turning("w1"))
        assert events.get_nowait() == ("worker_idle", "claude_code:main:w1")


def test_the_liveness_refresh_publishes_the_idle_edge_too():
    """The path a finished turn normally lands on: in place, not a replacement.

    The liveness sweep and the Codex/Cursor runtime callbacks refresh the read
    model's own objects, so watching only the full scan would have missed most
    real turn endings.
    """
    state = AppState()
    state.update_session(_turning("w1", thinking=True))
    live = state.sessions["claude_code:main:w1"]

    with state.bus.subscribe("worker_idle") as events:
        state.apply_session_changes([live], lambda s: setattr(s, "thinking", False))
        assert events.get_nowait() == ("worker_idle", "claude_code:main:w1")


def test_a_turn_holding_an_answerable_interaction_is_not_an_idle_edge():
    """Thinking is suppressed by the interaction, but the turn is still open."""
    assert (
        _idle_edges(
            [_turning("w1", thinking=True)],
            [_turning("w1", capabilities=frozenset({Capability.INTERACT}))],
        )
        == []
    )




# --- display projection copies (#1088) --------------------------------------


def test_copy_session_matches_dataclasses_replace_field_for_field():
    """The projection copies a Session by hand now, so it must stay a Session.

    ``copy_session`` replaced ``dataclasses.replace`` on the hot path because
    ``replace`` reads all forty-odd fields back one at a time. A copy that
    quietly dropped or aliased a field would corrupt every card, so pin the
    equivalence rather than trust it (#1088).
    """
    original = _session("s1", SessionStatus.LIVE)
    original.issues = ()
    original.capabilities = frozenset({Capability.INJECT})

    plain = copy_session(original)
    changed = copy_session(original, thinking=True, activity="Working")

    assert plain == replace(original)
    assert plain is not original
    assert changed == replace(original, thinking=True, activity="Working")
    # A shallow copy shares field values, exactly as ``replace`` does.
    assert plain.capabilities is original.capabilities
    assert original.thinking is False  # the source is never touched


def test_the_hot_session_passes_read_chat_identity_once_not_once_per_session(tmp_path):
    """The per-session chat lookup was the web worker's largest single cost (#1291).

    ``visible_sessions`` runs on every SSE tick per attached browser. Going back
    to a chat-table lookup per session would burn a core.
    """
    db = Db(tmp_path / "control.db")
    keys = tuple(f"claude_code:main:s{i:03d}" for i in range(60))
    db.ensure_session_chats(keys)
    state = AppState(db=db)
    for i, key in enumerate(keys):
        state.update_session(
            Session(
                key=key,
                account_key="claude_code:main",
                session_id=f"s{i:03d}",
                status=SessionStatus.LIVE,
            )
        )

    calls: list[str] = []
    original = db.chat_for_session
    db.chat_for_session = lambda key: (calls.append(key), original(key))[1]
    try:
        assert len(state.visible_sessions()) == len(keys)
        assert len(state.session_presentation().top_level) == len(keys)
        state.replace_account_sessions(
            "claude_code:main",
            list(state.sessions.values()),
        )
    finally:
        db.chat_for_session = original

    assert calls == [], f"{len(calls)} per-session chat lookups in the hot passes"
    db.close()


def test_a_chat_snapshot_agrees_with_the_per_session_reader(tmp_path):
    """The batched path must not be a second, subtly different identity rule."""
    db = Db(tmp_path / "control.db")
    source = "codex:main:source"
    target = "claude_code:alt:target"
    lone = "claude_code:main:lone"
    db.ensure_session_chats((source, lone))
    absorbed = db.ensure_session_chats((target,))[0]
    linked = db.append_chat_leg(source, target)
    state = AppState(db=db)

    chats = state.chat_snapshot()
    for key in (source, target, lone, "claude_code:main:unknown"):
        assert state.chat_for_session(key, chats) == state.chat_for_session(key)
    # an absorbed chat's old id still resolves to the chat that swallowed it
    assert chats.chat_by_id(absorbed.chat_id) == linked
    assert chats.chat_by_id(linked.chat_id) == linked
    assert chats.chat_by_id("nosuchchat") is None
    db.close()


def test_a_liveness_sweep_reads_the_chat_mapping_a_fixed_number_of_times(tmp_path):
    """Chat identity is chat-wide, so the sweep's question reconciliation asks
    for it — but it must ask once for the pass, not three times per session.

    Per session it was a ``PRAGMA data_version`` plus, whenever the separate
    runtime process had committed since, a full reload of the chat index; over
    a few thousand sessions every three seconds that made the sweep the largest
    single consumer of the web worker's event loop (#1310).
    """

    def sweep_reads(session_count: int) -> tuple[int, int]:
        db = Db(tmp_path / f"deck-{session_count}.db")
        state = AppState(db=db, budget_db=db)
        for index in range(session_count):
            # A trailing question is what makes the sweep reconcile at all, and
            # reconciliation is the chat-wide read this test is about.
            state.sessions[f"claude_code:main:s{index}"] = replace(
                _session(f"s{index}"),
                last_role="agent",
                last_text=f"Chat {index} is ready. Shall I ship it now?",
                last_event_seq=index + 1,
            )
        per_session: list[str] = []
        snapshots: list[int] = []
        reader = db.chat_for_session
        db.chat_for_session = lambda key: (per_session.append(key), reader(key))[1]
        whole = db.chat_snapshot
        db.chat_snapshot = lambda: (snapshots.append(1), whole())[1]

        state.apply_session_changes(list(state.sessions.values()), lambda _s: None)
        return (len(per_session), len(snapshots))

    small = sweep_reads(4)
    large = sweep_reads(40)

    assert small[0] == large[0] == 0  # never one lookup per session
    assert small[1] == large[1]  # and the whole-mapping reads do not grow either


class _FakeDb:
    """Records the writes AppState admission performs, with empty loads."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str | None]] = []
        self.upserted: list[list[Session]] = []

    def load_delegation_parents(self) -> dict[str, str]:
        return {}

    def load_delegated_sessions(self) -> set[str]:
        return set()

    def load_generated_titles(self) -> dict:
        return {}

    def load_session_pull_requests(self) -> dict[str, tuple]:
        return {}

    def load_session_issues(self) -> dict[str, tuple]:
        return {}

    def load_session_attention_reports(self) -> dict:
        return {}

    def load_open_questions(self) -> dict:
        return {}

    def load_boss_turn_windows(self) -> dict:
        return {}

    def load_session_tokens(self) -> dict[str, str]:
        return {}

    def delete_session_attention_report(self, session_key: str) -> None: ...

    def record_delegated_session(self, key: str, parent: str | None = None) -> None:
        self.recorded.append((key, parent))

    def upsert_sessions_seen(self, sessions: list[Session]) -> None:
        self.upserted.append(list(sessions))


