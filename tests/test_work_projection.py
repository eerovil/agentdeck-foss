"""The read-only Work projection groups chats by explicit issue evidence (#2138)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck.db import Db
from agentdeck.models import IssueRef, PullRequestRef, Session, SessionStatus
from agentdeck.state import AppState
from agentdeck.work import (
    WorkBinding,
    WorkChatFacts,
    WorkEvidence,
    WorkProvenance,
    WorkRef,
    build_work_projection,
)

A = IssueRef("eerovil/agentdeck", 2138)
B = IssueRef("eerovil/agentdeck", 2140)
A_URL = "https://github.com/eerovil/agentdeck/issues/2138"
WORK_A = WorkRef("eerovil/agentdeck", 2138)
WORK_B = WorkRef("eerovil/agentdeck", 2140)


def _members(projection, work):
    found = projection.get(work)
    return found.chat_ids if found is not None else ()


# --- the pure builder ------------------------------------------------------


def test_one_poller_worker_is_one_work():
    projection = build_work_projection(
        [WorkChatFacts("chat-1", issue_urls=(("claude_code:main:w1", A_URL),))]
    )

    assert [work.ref for work in projection.works] == [WORK_A]
    (membership,) = projection.works[0].memberships
    assert membership.chat_id == "chat-1"
    assert membership.provenance == (WorkProvenance(WorkEvidence.ISSUE_URL, "claude_code:main:w1"),)


def test_continuation_legs_make_one_membership_with_every_provenance():
    # Two legs of one chat, one per account, each carrying its own evidence.
    projection = build_work_projection(
        [
            WorkChatFacts(
                "chat-1",
                registered=(("claude_code:main:old", A),),
                issue_urls=(("claude_code:main:old", A_URL),),
            ),
            WorkChatFacts(
                "chat-1",
                registered=(("claude_code:main:old", A),),
                issue_urls=(
                    ("codex:second:new", "https://GitHub.com/EeroVil/AgentDeck/issues/2138"),
                ),
            ),
        ]
    )

    (work,) = projection.works
    (membership,) = work.memberships
    assert membership.chat_id == "chat-1"
    assert set(membership.provenance) == {
        WorkProvenance(WorkEvidence.REGISTERED_ISSUE, "claude_code:main:old"),
        WorkProvenance(WorkEvidence.ISSUE_URL, "claude_code:main:old"),
        WorkProvenance(WorkEvidence.ISSUE_URL, "codex:second:new"),
    }


def test_one_chat_can_belong_to_two_works():
    projection = build_work_projection([WorkChatFacts("chat-1", registered=(("k", A), ("k", B)))])

    assert _members(projection, WORK_A) == ("chat-1",)
    assert _members(projection, WORK_B) == ("chat-1",)
    assert projection.works_for_chat("chat-1") == (WORK_A, WORK_B)


def test_two_independent_chats_share_one_work():
    # A worker found by its dispatch plus a fresh review chat registered later.
    projection = build_work_projection(
        [
            WorkChatFacts("review", registered=(("codex:main:r", A),)),
            WorkChatFacts("worker", issue_urls=(("claude_code:main:w", A_URL),)),
        ]
    )

    assert _members(projection, WORK_A) == ("review", "worker")


def test_repository_case_does_not_split_a_work():
    projection = build_work_projection(
        [
            WorkChatFacts("x", registered=(("k", IssueRef("EeroVil/AgentDeck", 2138)),)),
            WorkChatFacts("y", registered=(("k", A),)),
        ]
    )

    (work,) = projection.works
    assert work.ref == WORK_A
    assert work.chat_ids == ("x", "y")


def test_the_same_number_in_two_repositories_is_two_works():
    other = IssueRef("eerovil/trailhead", 2138)
    projection = build_work_projection(
        [
            WorkChatFacts("x", registered=(("k", A),)),
            WorkChatFacts("y", registered=(("k", other),)),
        ]
    )

    assert _members(projection, WORK_A) == ("x",)
    assert _members(projection, other) == ("y",)


def test_saved_delegated_binding_places_the_child_without_reading_a_parent():
    projection = build_work_projection(
        [
            WorkChatFacts("parent", registered=(("p", A),)),
            WorkChatFacts(
                "child", bindings=(WorkBinding("c", WORK_A.work_id, "delegated", "parent"),)
            ),
            # A grandchild with nothing saved stays out: the chain is never walked.
            WorkChatFacts("grandchild"),
        ]
    )

    assert _members(projection, WORK_A) == ("child", "parent")
    child = next(m for m in projection.get(WORK_A).memberships if m.chat_id == "child")
    assert not child.direct
    assert child.provenance == (WorkProvenance(WorkEvidence.DELEGATED, "c", "parent"),)
    assert projection.works_for_chat("grandchild") == ()


def test_backfilled_binding_is_inherited_evidence_too():
    projection = build_work_projection(
        [WorkChatFacts("old", bindings=(WorkBinding("o", WORK_B.work_id, "backfilled", "p"),))]
    )

    (membership,) = projection.get(WORK_B).memberships
    assert not membership.direct
    assert membership.provenance == (WorkProvenance(WorkEvidence.BACKFILLED, "o", "p"),)


def test_a_run_binding_is_direct_membership():
    projection = build_work_projection([WorkChatFacts("helper", runs=(("h", WORK_A.work_id),))])

    (membership,) = projection.get(WORK_A).memberships
    assert membership.direct
    assert membership.provenance == (WorkProvenance(WorkEvidence.RUN_BINDING, "h"),)
    assert projection.get(WORK_A).issue == A


def test_ambiguous_rows_join_no_work_but_stay_listed():
    projection = build_work_projection(
        [
            WorkChatFacts("parent", registered=(("p", A), ("p", B))),
            WorkChatFacts(
                "child",
                bindings=(
                    WorkBinding("c", WORK_A.work_id, "ambiguous", "parent"),
                    WorkBinding("c", WORK_B.work_id, "ambiguous", "parent,other"),
                ),
            ),
        ]
    )

    assert projection.works_for_chat("child") == ()
    assert _members(projection, WORK_A) == ("parent",)
    (ambiguous,) = projection.ambiguous
    assert ambiguous.chat_id == "child"
    assert ambiguous.candidates == (WORK_A, WORK_B)
    assert ambiguous.parent_chat_ids == ("other", "parent")


@pytest.mark.parametrize(
    "work_id",
    ["", "github_issue:", "github_issue:agentdeck#1", "github_issue:a/b#x", "jira:a/b#1"],
)
def test_malformed_saved_work_ids_place_nothing(work_id):
    projection = build_work_projection(
        [
            WorkChatFacts(
                "c",
                runs=(("c", work_id),),
                bindings=(WorkBinding("c", work_id, "delegated"),),
            )
        ]
    )

    assert projection.works == ()


def test_work_id_round_trips_through_the_ref():
    assert WorkRef.from_work_id(WORK_A.work_id) == WORK_A
    assert WorkRef.from_work_id("github_issue:EeroVil/AgentDeck#2138") == WORK_A


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "#2138",
        "2138",
        "agentdeck#2138",
        "https://github.com/eerovil/issues/2138",
        "https://github.com/eerovil/agentdeck/pull/2138",
        "https://example.com/eerovil/agentdeck/issues/2138",
        "https://github.com/eerovil/agentdeck/issues/2138#issuecomment-1",
        "eerovil/agentdeck#2138 and more",
    ],
)
def test_malformed_or_short_issue_urls_do_not_group(raw):
    projection = build_work_projection([WorkChatFacts("chat-1", issue_urls=(("k", raw),))])

    assert projection.works == ()


def test_projection_is_deterministic():
    facts = [
        WorkChatFacts("z", registered=(("z", B),)),
        WorkChatFacts("m", registered=(("m", A), ("m", B))),
        WorkChatFacts("a", issue_urls=(("a", A_URL),)),
    ]

    forward = build_work_projection(facts)
    assert forward == build_work_projection(reversed(facts))
    assert [work.ref for work in forward.works] == [WORK_A, WORK_B]
    assert forward.get(WORK_B).chat_ids == ("m", "z")


# --- through AppState ------------------------------------------------------


def _session(key: str, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(
        key=key, account_key=account, session_id=session_id, status=SessionStatus.IDLE, **fields
    )


@pytest.fixture
def state(tmp_path):
    db = Db(tmp_path / "control.db")
    try:
        yield AppState(db=db, budget_db=db)
    finally:
        db.close()


def _chat(state, key):
    return state.chat_identity_key(key)


def _record_before_cutover(state, key, parent):
    """A delegation recorded by the old code, before this rule's cutover."""
    state.budget_db._write(
        "INSERT INTO delegated_sessions(session_key, created_at, parent_session_id)"
        " VALUES (?, '2026-08-01T00:00:00+00:00', ?)",
        (key, parent),
    )
    state.recorded_delegation_parents[key] = parent


def test_app_state_worker_continuation_review_and_delegate(state):
    worker_old = "claude_code:main:worker-1"
    worker_new = "codex:second:worker-2"
    review = "codex:main:review-1"
    delegate = "claude_code:main:delegate-1"
    bystander = "claude_code:main:other-1"
    state.budget_db.ensure_session_chats((worker_old, review, delegate, bystander))
    state.budget_db.append_chat_leg(worker_old, worker_new)
    state.sessions = {
        worker_old: _session(worker_old, issue_url=A_URL),
        worker_new: _session(worker_new, issue_url=A_URL),
        review: _session(review),
        delegate: _session(delegate),
        # Weak signals only: a matching worktree, title and pull request.
        bystander: _session(
            bystander,
            cwd=Path("/home/me/agentdeck/.worktrees/issue-2138"),
            title="agentdeck#2138 · Work projection",
        ),
    }
    state.set_issues(review, (A,))
    state.set_pull_requests(bystander, (PullRequestRef("eerovil/agentdeck", 2138),))
    state.mark_delegated_session(delegate, worker_new)
    before = dict(state.sessions)

    projection = state.work_projection()

    worker_chat = _chat(state, worker_old)
    assert worker_chat == _chat(state, worker_new)
    assert _members(projection, WORK_A) == tuple(
        sorted((worker_chat, _chat(state, review), _chat(state, delegate)))
    )
    assert projection.works_for_chat(_chat(state, bystander)) == ()
    delegated = next(
        m for m in projection.get(WORK_A).memberships if m.chat_id == _chat(state, delegate)
    )
    assert delegated.provenance == (WorkProvenance(WorkEvidence.DELEGATED, delegate, worker_chat),)
    # Read-only: the session store and parent links are untouched.
    assert state.sessions == before


def test_app_state_registration_on_an_old_leg_folds_into_the_chat(state):
    old = "claude_code:main:leg-1"
    new = "claude_code:other:leg-2"
    state.budget_db.ensure_session_chats((old,))
    state.set_issues(old, (B,))
    state.budget_db.append_chat_leg(old, new)
    state.sessions = {new: _session(new)}

    projection = state.work_projection()

    (membership,) = projection.get(WORK_B).memberships
    assert membership.chat_id == _chat(state, new)
    assert {p.evidence for p in membership.provenance} == {WorkEvidence.REGISTERED_ISSUE}


def test_app_state_each_leg_keeps_the_work_saved_at_its_own_delegation(state):
    parent_a = "claude_code:main:parent-a"
    parent_b = "codex:main:parent-b"
    child_old = "claude_code:main:child-1"
    child_new = "codex:second:child-2"
    state.budget_db.ensure_session_chats((parent_a, parent_b, child_old))
    state.budget_db.append_chat_leg(child_old, child_new)
    state.sessions = {key: _session(key) for key in (parent_a, parent_b, child_old, child_new)}
    state.set_issues(parent_a, (A,))
    state.set_issues(parent_b, (B,))
    state.mark_delegated_session(child_old, parent_a)
    state.mark_delegated_session(child_new, parent_b)

    projection = state.work_projection()

    # Two explicit delegations, each from a parent in exactly one Work.
    assert projection.works_for_chat(_chat(state, child_new)) == (WORK_A, WORK_B)
    assert projection.ambiguous == ()


@pytest.mark.parametrize("reference", ["claude_code:main:parent-1", "parent-1"])
def test_app_state_parent_that_moved_to_a_new_leg_is_still_found(state, reference):
    parent_old = "claude_code:main:parent-1"
    parent_new = "codex:second:parent-2"
    child = "claude_code:main:child-1"
    state.budget_db.ensure_session_chats((parent_old, child))
    state.budget_db.append_chat_leg(parent_old, parent_new)
    # The old leg has rolled over and is no longer scanned.
    state.sessions = {parent_new: _session(parent_new), child: _session(child)}
    state.set_issues(parent_new, (A,))
    state.mark_delegated_session(child, reference)

    projection = state.work_projection()

    parent_chat = _chat(state, parent_new)
    assert _members(projection, WORK_A) == tuple(sorted((parent_chat, _chat(state, child))))
    delegated = next(
        m for m in projection.get(WORK_A).memberships if m.chat_id == _chat(state, child)
    )
    assert delegated.provenance == (WorkProvenance(WorkEvidence.DELEGATED, child, parent_chat),)


def test_app_state_child_membership_is_saved_not_recomputed(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.budget_db.ensure_session_chats((parent, child))
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A,))
    state.mark_delegated_session(child, parent)

    # The parent later takes on a second Work: the child keeps what was saved.
    state.set_issues(parent, (A, B))
    projection = state.work_projection()

    assert projection.works_for_chat(_chat(state, child)) == (WORK_A,)
    assert _members(projection, WORK_B) == (_chat(state, parent),)


def test_app_state_parent_in_several_works_places_the_child_in_none(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.budget_db.ensure_session_chats((parent, child))
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A, B))
    state.mark_delegated_session(child, parent)

    projection = state.work_projection()

    assert projection.works_for_chat(_chat(state, child)) == ()
    (ambiguous,) = projection.ambiguous
    assert ambiguous.chat_id == _chat(state, child)
    assert ambiguous.candidates == (WORK_A, WORK_B)
    assert ambiguous.parent_chat_ids == (_chat(state, parent),)
    # Saved, so a restart reads the same rows.
    rows = state.budget_db.session_work_bindings()
    assert {(r.session_key, r.work_id, r.evidence) for r in rows} == {
        (child, WORK_A.work_id, "ambiguous"),
        (child, WORK_B.work_id, "ambiguous"),
    }


def test_app_state_parent_with_no_work_saves_a_none_outcome(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.mark_delegated_session(child, parent)
    # The parent joins a Work later: the child's saved decision stands.
    state.set_issues(parent, (A,))
    state.mark_delegated_session(child, parent)

    rows = state.budget_db.session_work_bindings()
    assert [(r.session_key, r.work_id, r.evidence) for r in rows] == [(child, "", "none")]
    assert state.work_projection().works_for_chat(_chat(state, child)) == ()


def test_app_state_parent_in_a_run_binds_the_child_to_that_run_only(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    # The parent is registered on B but took part in a Run of A.
    state.set_issues(parent, (B,))
    state.bind_session_run(parent, WORK_A.work_id, "run-1")
    state.mark_delegated_session(child, parent)

    projection = state.work_projection()

    assert projection.works_for_chat(_chat(state, child)) == (WORK_A,)
    (membership,) = [
        m for m in projection.get(WORK_A).memberships if m.chat_id == _chat(state, child)
    ]
    assert membership.provenance == (WorkProvenance(WorkEvidence.RUN_BINDING, child),)
    rows = state.budget_db.session_work_bindings()
    assert [(r.work_id, r.evidence, r.parent_chat_id) for r in rows] == [
        (WORK_A.work_id, "run", "run-1")
    ]


def test_app_state_provider_subagent_is_not_a_work_member(state):
    parent = "claude_code:main:parent-1"
    subagent = "claude_code:main:agent-1"
    state.sessions = {
        parent: _session(parent),
        subagent: _session(subagent, parent_session_key=parent),
    }
    state.set_issues(parent, (A,))

    assert _members(state.work_projection(), WORK_A) == (_chat(state, parent),)


# --- the one-time backfill ---------------------------------------------------


def test_backfill_saves_the_legacy_result_once(state):
    parent = "claude_code:main:parent-1"
    busy = "claude_code:main:parent-2"
    child = "claude_code:main:child-1"
    torn = "claude_code:main:child-2"
    orphan = "claude_code:main:child-3"
    subagent = "claude_code:main:agent-1"
    keys = (parent, busy, child, torn, orphan)
    state.budget_db.ensure_session_chats(keys)
    state.sessions = {key: _session(key) for key in keys}
    state.sessions[subagent] = _session(subagent, parent_session_key=parent)
    state.set_issues(parent, (A,))
    state.set_issues(busy, (A, B))
    # Recorded before the creation-time rule existed: no saved outcome.
    for key, ref in ((child, parent), (torn, busy), (orphan, "never-scanned")):
        _record_before_cutover(state, key, ref)
    assert _members(state.work_projection(), WORK_A) == tuple(
        sorted((_chat(state, parent), _chat(state, busy)))
    )

    summary = state.backfill_delegated_work()

    assert summary == {
        "considered": 3,
        "backfilled": 1,
        "ambiguous": 1,
        "no_work": 0,
        "pending": 1,
        "rows": 3,
    }
    projection = state.work_projection()
    assert projection.works_for_chat(_chat(state, child)) == (WORK_A,)
    (membership,) = [
        m for m in projection.get(WORK_A).memberships if m.chat_id == _chat(state, child)
    ]
    assert membership.provenance == (
        WorkProvenance(WorkEvidence.BACKFILLED, child, _chat(state, parent)),
    )
    assert projection.works_for_chat(_chat(state, torn)) == ()
    assert [a.chat_id for a in projection.ambiguous] == [_chat(state, torn)]
    assert projection.works_for_chat(_chat(state, subagent)) == ()
    # Recorded, so it never runs again — even once the parent's Works change.
    assert json.loads(state.budget_db.work_backfill("delegated_work_v1")) == summary
    state.set_issues(busy, (A,))
    assert state.backfill_delegated_work() is None
    assert state.work_projection().works_for_chat(_chat(state, torn)) == ()


def test_backfill_skips_children_created_under_the_new_rule(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A, B))
    state.mark_delegated_session(child, parent)

    assert state.backfill_delegated_work() == {
        "considered": 0,
        "backfilled": 0,
        "ambiguous": 0,
        "no_work": 0,
        "pending": 0,
        "rows": 0,
    }


def test_app_state_a_re_recorded_delegation_keeps_its_first_outcome(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A,))
    state.mark_delegated_session(child, parent)
    state.set_issues(parent, (A, B))
    state.mark_delegated_session(child, parent)

    rows = state.budget_db.session_work_bindings()
    assert [(r.work_id, r.evidence) for r in rows] == [(WORK_A.work_id, "delegated")]


def test_app_state_self_delegation_saves_nothing(state):
    chat = "claude_code:main:chat-1"
    state.sessions = {chat: _session(chat)}
    state.set_issues(chat, (A,))
    state.mark_delegated_session(chat, chat)

    rows = state.budget_db.session_work_bindings()
    assert [(r.work_id, r.evidence) for r in rows] == [("", "none")]


def test_app_state_unresolved_parent_stays_pending_until_it_is_scanned(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {child: _session(child)}
    assert state.backfill_delegated_work() is not None
    state.mark_delegated_session(child, "parent-1")

    # Not yet scanned: nothing saved, nothing finalized.
    assert state.budget_db.session_work_bindings() == []
    assert state.finalize_pending_delegations() == 0

    state.sessions[parent] = _session(parent)
    state.set_issues(parent, (A,))
    assert state.finalize_pending_delegations() == 1
    assert state.finalize_pending_delegations() == 0
    assert state.work_projection().works_for_chat(_chat(state, child)) == (WORK_A,)


def test_a_child_delegated_after_the_cutover_is_not_the_backfills(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {child: _session(child)}
    # Delegated under the new rule, before the parent is scanned: pending.
    state.mark_delegated_session(child, parent)
    state.sessions[parent] = _session(parent)
    state.set_issues(parent, (A,))
    state.bind_session_run(parent, WORK_B.work_id, "run-1")

    # Pending waits for the backfill, which leaves this child alone ...
    assert state.finalize_pending_delegations() == 0
    assert state.backfill_delegated_work()["considered"] == 0
    assert state.budget_db.session_work_bindings() == []
    # ... so the creation rule decides it, Run first.
    assert state.finalize_pending_delegations() == 1
    runs = state.budget_db.session_run_bindings(session_key=child)
    assert [(r.work_id, r.run_id) for r in runs] == [(WORK_B.work_id, "run-1")]
    assert state.work_projection().works_for_chat(_chat(state, child)) == (WORK_B,)


def test_the_cutover_is_recorded_once(state, tmp_path):
    first = state.budget_db.work_backfill("delegated_work_cutover")
    AppState(db=state.budget_db, budget_db=state.budget_db)
    assert first and state.budget_db.work_backfill("delegated_work_cutover") == first


def test_app_state_a_decided_child_never_takes_a_later_run(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A, B))
    state.mark_delegated_session(child, parent)
    # The parent then joins a Run, and the delegation is recorded again.
    state.bind_session_run(parent, WORK_A.work_id, "run-1")
    state.mark_delegated_session(child, parent)

    assert state.budget_db.session_run_bindings(session_key=child) == []
    assert state.work_projection().works_for_chat(_chat(state, child)) == ()


def test_app_state_a_run_bound_child_never_takes_a_second_run(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.bind_session_run(parent, WORK_A.work_id, "run-1")
    state.mark_delegated_session(child, parent)
    state.bind_session_run(parent, WORK_B.work_id, "run-2")
    state.mark_delegated_session(child, parent)

    runs = state.budget_db.session_run_bindings(session_key=child)
    assert [(r.work_id, r.run_id) for r in runs] == [(WORK_A.work_id, "run-1")]


def test_backfill_decides_recorded_children_whose_chat_is_not_scanned(state):
    parent = "claude_code:main:parent-1"
    gone = "claude_code:main:child-gone"
    state.sessions = {parent: _session(parent)}
    state.set_issues(parent, (A,))
    _record_before_cutover(state, gone, parent)

    summary = state.backfill_delegated_work()

    assert summary["backfilled"] == 1
    rows = state.budget_db.session_work_bindings()
    assert [(r.session_key, r.work_id, r.evidence) for r in rows] == [
        (gone, WORK_A.work_id, "backfilled")
    ]
    assert state.finalize_pending_delegations() == 0


def test_an_unrecorded_cutover_blocks_the_backfill_until_a_retry_writes_it(tmp_path):
    db = Db(tmp_path / "control.db")
    real = db.record_work_backfill
    db.record_work_backfill = lambda name, summary: False  # the control DB is locked
    try:
        state = AppState(db=db, budget_db=db)
        parent = "claude_code:main:parent-1"
        child = "claude_code:main:child-1"
        state.sessions = {child: _session(child)}
        state.mark_delegated_session(child, parent)  # pending: parent not scanned
        state.sessions[parent] = _session(parent)
        state.set_issues(parent, (A,))
        state.bind_session_run(parent, WORK_B.work_id, "run-1")

        # No marker: the backfill must not read that as "everything is legacy".
        assert state.backfill_delegated_work() is None
        assert state.budget_db.session_work_bindings() == []

        db.record_work_backfill = real  # the lock clears
        summary = state.backfill_delegated_work()
        assert summary is not None and summary["considered"] == 0
        # The retry kept this process's start as the cutover, so the child recorded
        # meanwhile is newer than it: the creation rule decides it, not the backfill.
        assert state.finalize_pending_delegations() == 1
        runs = db.session_run_bindings(session_key=child)
        assert [(r.work_id, r.run_id) for r in runs] == [(WORK_B.work_id, "run-1")]
    finally:
        db.close()


def test_a_transcript_only_delegation_newer_than_the_cutover_is_not_backfilled(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {
        parent: _session(parent),
        child: _session(child, started_at=datetime.now(UTC) + timedelta(minutes=1)),
    }
    state.set_issues(parent, (A,))
    state.delegation_parents[child] = parent

    assert state.backfill_delegated_work()["considered"] == 0
    assert state.budget_db.session_work_bindings() == []


def test_a_failed_backfill_write_leaves_no_marker_and_is_retried(state):
    parent = "claude_code:main:parent-1"
    child = "claude_code:main:child-1"
    state.sessions = {key: _session(key) for key in (parent, child)}
    state.set_issues(parent, (A,))
    _record_before_cutover(state, child, parent)
    # The rows cannot be written; the marker must not be written without them.
    state.budget_db._write(
        "CREATE TRIGGER no_rows BEFORE INSERT ON session_work_bindings"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )

    assert state.backfill_delegated_work() is None
    assert state.budget_db.work_backfill("delegated_work_v1") is None
    assert not state.delegated_work_backfill_done()

    state.budget_db._write("DROP TRIGGER no_rows")
    assert state.backfill_delegated_work()["backfilled"] == 1
    assert state.budget_db.work_backfill("delegated_work_v1")
    assert [r.evidence for r in state.budget_db.session_work_bindings()] == ["backfilled"]


# --- the cached projection (#2654) -------------------------------------------------------


def test_app_state_reuses_the_projection_while_nothing_moved(state):
    key = "claude_code:main:cached-1"
    state.budget_db.ensure_session_chats((key,))
    state.sessions = {key: _session(key, issue_url=A_URL)}

    first = state.work_projection()

    assert state.work_projection() is first


def test_app_state_rebuilds_the_projection_when_any_input_moves(state, tmp_path):
    key = "claude_code:main:cached-1"
    other = "codex:main:cached-2"
    state.budget_db.ensure_session_chats((key, other))
    state.sessions = {key: _session(key, issue_url=A_URL), other: _session(other)}
    before = state.work_projection()
    assert before.works_for_chat(_chat(state, other)) == ()

    # A registration written straight into the map, with no announcement.
    state.session_issues[other] = (B,)
    registered = state.work_projection()
    assert registered is not before
    assert registered.works_for_chat(_chat(state, other)) == (WORK_B,)

    # A session's own issue link changing.
    state.sessions[key] = _session(key)
    unlinked = state.work_projection()
    assert unlinked.get(WORK_A) is None

    # A Run binding saved by another process (the runtime) on its own connection.
    runtime = Db(tmp_path / "control.db")
    try:
        runtime.bind_session_run(key, WORK_A.work_id, "run-1")
        bound = state.work_projection()
        assert _members(bound, WORK_A) == (_chat(state, key),)

        # Two legs joined into one chat by the runtime.
        runtime.append_chat_leg(other, key)
        joined = state.work_projection()
        assert _chat(state, key) == _chat(state, other)
        assert _members(joined, WORK_A) == (_chat(state, key),)
        assert joined.works_for_chat(_chat(state, key)) == (WORK_A, WORK_B)
    finally:
        runtime.close()
    assert state.work_projection() is state.work_projection()
