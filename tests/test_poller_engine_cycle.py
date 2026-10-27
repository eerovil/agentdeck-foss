"""Slice 9 of #176: the wake — one cycle composing the engine modules behind one port."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
import test_run_execution as runtime_fakes
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import (
    FakePort,
    comment,
    payloads,
    run_wake,
    store_for,
)

from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine import dispatch, health, queue, reconcile, release, revive
from agentdeck.poller_engine import paths as engine_paths
from agentdeck.poller_engine.config import CommandOutputPolicy
from agentdeck.poller_engine.cycle import (
    _live_check_due,
    _release_refusal_message,
)
from agentdeck.poller_engine.health import HealthState
from agentdeck.poller_engine.ownership import LabelEvent
from agentdeck.poller_engine.ports import (
    CommentBody,
    EffectKind,
    EnginePort,
    LinkedPRs,
    PrState,
    WorktreeCleanup,
)
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl


def test_port_protocol_is_satisfied_by_the_fake():
    assert isinstance(FakePort(), EnginePort)


def test_only_idle_terminal_cards_cool_down_between_live_checks():
    recent = _NOW.isoformat()
    always_due = [
        {"status": "Todo"},
        {"status": "In progress"},
        {"status": "In review", "stopping": True},
        {"status": "In review", "dispatch": {"id": "d"}},
        {"status": "In review", "pending": ["1"]},
        {"status": "In review", "recovery_action": "resume"},
        {"status": "In review", "mode": "merge"},
        {"status": "In review", "release_pending": True},
        # A label re-add asks for a start, and the wake that acts on it must not do so from a
        # column it last read some other time (#1581).
        {"status": "Done", "manual_start": True},
    ]
    for rec in always_due:
        rec["last_live_check"] = recent
        assert _live_check_due(rec, now=_NOW, terminal_recheck_s=900), rec

    idle = {"status": "In review", "last_live_check": recent}
    assert not _live_check_due(idle, now=_NOW, terminal_recheck_s=900)
    assert _live_check_due(idle, now=_NOW, terminal_recheck_s=900, queued_control=True)
    assert _live_check_due(idle, now=_NOW + timedelta(seconds=900), terminal_recheck_s=900)


def test_graphql_budget_recovery_resumes_without_losing_card_state(tmp_path):
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "In progress"}}})
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        graphql_deferred=True,
    )

    held = run_wake(store, port, now=_NOW)
    assert "GraphQL reserve reached" in " ".join(held.log)
    assert "last_live_check" not in store.read()["issues"]["store#5"]

    port.graphql_deferred = False
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert store.read()["issues"]["store#5"]["last_live_check"] == (
        _NOW + timedelta(seconds=30)
    ).isoformat()


def test_exhausted_graphql_budget_defers_live_wake_without_advancing_state(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    store = store_for(tmp_path, initial)
    port = FakePort(
        comments=[comment(1, "/claude run")],
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        graphql_deferred=True,
    )

    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    assert outcome.halted == "graphql-budget"
    assert state["issues"] == initial["issues"]
    assert state["ownership"] == initial["ownership"]
    assert queue.get_cursor(state) is None
    assert port.scans == 0
    assert port.labelled_reads == 0
    assert port.snapshot_batches == []


def test_exhausted_core_budget_defers_live_wake_with_a_distinct_reason(tmp_path):
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}})
    port = FakePort(core_deferred=True)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.halted == "core-budget"
    assert "REST core reserve reached" in " ".join(outcome.log)
    assert port.scans == 0


def test_healthy_graphql_budget_runs_the_live_wake(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert outcome.halted is None
    assert port.live_query_checks == 1
    assert port.scans == 1
    assert port.labelled_reads == 1
    assert port.snapshot_batches == [["store#5"]]


def test_an_unreadable_card_keeps_its_record_and_live_check_cursor(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress"},
            "store#6": {"status": "In progress"},
        }
    }
    port = FakePort(snapshots={"store#6": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    cards = store.read()["issues"]
    assert set(cards) == {"store#5", "store#6"}
    assert "last_live_check" not in cards["store#5"]
    assert cards["store#6"]["last_live_check"] == _NOW.isoformat()


# --- intake ---

def test_a_command_is_ingested_queued_and_dispatched_in_one_wake(tmp_path):
    port = FakePort(comments=[comment(1, "/claude fix the header")])
    outcome = run_wake(store_for(tmp_path), port, now=_NOW)

    assert outcome.report.commands_ingested == 1
    assert len(outcome.directives) == 1
    directive = outcome.directives[0]
    assert directive["action"] == "new"
    assert directive["ref"] == "store#5"
    assert payloads(directive) == ["fix the header"]
    assert directive["generation"] and directive["dispatch_id"]
    assert EffectKind.SET_STATUS in port.kinds()


def test_a_new_dispatch_ends_the_park_the_card_rested_in(tmp_path):
    """The release reason and the human-review marker describe a park; a dispatch ends it."""
    store = store_for(tmp_path, {"issues": {"store#5": {
        "status": "Needs More Info",
        "generation": "gen-0",
        "parked_as": "human_review_pending",
        "release_blocked": {"why": "checks failed", "status": "Blocked", "generation": "gen-0"},
    }}})
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Needs More Info")},
        comments=[comment(1, "/claude carry on")],
    )

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    rec = store.read()["issues"]["store#5"]
    assert "parked_as" not in rec and "release_blocked" not in rec


def test_fresh_work_forgets_the_engines_earlier_rebases(tmp_path):
    """#2453: a verdict on a pre-rebase head may not be bridged across a person's new work."""
    rec = {"status": "Needs More Info", "generation": "gen-0"}
    release.note_rebase_lineage(rec, ref="store#7", source="a" * 40, head="b" * 40, now=_NOW)
    store = store_for(tmp_path, {"issues": {"store#5": rec}})
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Needs More Info")},
        comments=[comment(1, "/claude carry on")],
    )

    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert "rebase_lineage" not in store.read()["issues"]["store#5"]


def test_the_same_comment_is_never_ingested_twice(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(comments=[comment(1, "/claude go")])
    run_wake(store, port, now=_NOW)
    port.snapshots["store#5"] = CardSnapshot(status="In progress")
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert second.report.commands_ingested == 0


def test_the_cursor_advances_only_after_a_complete_scan(tmp_path):
    store = store_for(tmp_path)
    run_wake(store, FakePort(complete=False), now=_NOW)
    assert queue.get_cursor(store.read()) is None

    run_wake(store, FakePort(complete=True, scanned_to=_NOW), now=_NOW)
    # An empty scan proves nothing newer than its own overlap horizon.
    horizon = _NOW - timedelta(seconds=queue.DEFAULT_CURSOR_OVERLAP_S)
    assert queue.get_cursor(store.read()) == horizon.isoformat()


def test_a_comment_lost_to_the_scan_lag_is_ingested_on_the_following_wake(tmp_path):
    """GitHub can answer a scan without a comment that already existed when it was asked.
    The next wake must still reach back past that comment — and ingest it once, not twice.

    Created 20s before the scan that missed it: inside that scan's requested window, but under
    the old 10s overlap the next floor started after it and no scan ever asked again."""
    store = store_for(tmp_path)
    lagging = comment(1, "/claude fix the header", created="2026-07-25T11:59:40Z")

    # Wake one: the comment exists, the list endpoint has not caught up.
    first = run_wake(store, FakePort(comments=[], scanned_to=_NOW), now=_NOW)
    assert first.report.commands_ingested == 0

    # Wake two: GitHub now returns it, and the floor still covers the moment it was created.
    port = FakePort(comments=[lagging], scanned_to=_NOW + timedelta(seconds=30))
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    created = datetime.fromisoformat(lagging["created_at"].replace("Z", "+00:00"))
    assert datetime.fromisoformat(port.since) < created
    assert second.report.commands_ingested == 1
    assert payloads(second.directives[0]) == ["fix the header"]

    # Wake three: the overlapping window returns it again; the ledger refuses the duplicate.
    port.snapshots["store#5"] = CardSnapshot(status="In progress")
    port.scanned_to = _NOW + timedelta(seconds=60)
    third = run_wake(store, port, now=_NOW + timedelta(seconds=60))
    assert third.report.commands_ingested == 0
    assert len(queue.controls(store.read())) == 1


def test_a_wake_never_asks_for_a_window_narrower_than_the_gap_between_wakes(tmp_path):
    """The whole defect: an overlap under one wake interval leaves a slice of time that no
    scan, before or after, ever asks GitHub about."""
    store = store_for(tmp_path)
    run_wake(store, FakePort(scanned_to=_NOW), now=_NOW)
    port = FakePort(scanned_to=_NOW + timedelta(seconds=30))
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    floor = datetime.fromisoformat(port.since)
    assert _NOW - floor > timedelta(seconds=queue.WAKE_INTERVAL_S)


def test_a_denied_command_is_recorded_but_never_dispatched(tmp_path):
    # A real collaborator, but below the work bar.
    port = FakePort(comments=[comment(1, "/claude go", author="drive-by")],
                    permissions={"drive-by": "read"})
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW)
    ledger = queue.controls(store.read())
    assert ledger["1"]["kind"] == "unauthorized"
    assert ledger["1"]["status"] == "rejected"
    assert outcome.directives == []


def test_uncertain_authorization_is_neither_permission_nor_a_silent_drop(tmp_path):
    # GitHub could not say what this login may do: that is uncertainty, not a denial.
    port = FakePort(comments=[comment(1, "/claude go", author="unknown")],
                    permissions={"unknown": None})
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW)
    state = store.read()
    # Left unseen so a later wake can decide, and surfaced in health.
    assert queue.is_seen(state, 1) is False
    assert outcome.directives == []
    assert outcome.report.errors
    assert health.record(state)["error_streak"] == 1


def _app_comment(cid, body):
    """What GitHub sends for a `/claude` written by an app a person runs (#2033).

    Exactly the shape of the comment that went missing: a `[bot]` login of type `Bot`, and no
    relationship to the repository, because an app has none to have.
    """
    return comment(cid, body, author="reviewer[bot]", user_type="Bot", association="NONE")


_BOT_CONTROLLER = auth.parse_policy({"work": {"allow": ["reviewer[bot]"]}, "bot": "claude-bot"})


def test_an_allowlisted_bots_claude_comment_steers_the_card(tmp_path):
    # The defect: the scan dropped this before the grammar, so there was no ledger record, no
    # refusal and no wake — the operator's own review app spoke and nothing happened.
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    # A card the engine has already taken — a record on its own is not a hand-over (#2056).
    store = store_for(
        tmp_path, {"issues": {"store#5": {"status": "Todo", "engine_claim": True}}}
    )
    outcome = run_wake(store, port, now=_NOW, policy=_BOT_CONTROLLER)

    assert queue.controls(store.read())["1"]["kind"] == "claude"
    assert payloads(outcome.directives[0]) == ["fix the header"]
    # Allowlisted, so no collaborator lookup was needed — GitHub has no answer for an app login.
    assert port.permission_calls == []


def _refusal_comments(outcome):
    """The comment bodies this wake posted."""
    return [
        str(effect.value.body)
        for effect in outcome.effects
        if effect.kind is EffectKind.COMMENT
    ]


def test_an_app_may_not_start_a_card_nobody_handed_the_engine(tmp_path):
    """The whole of #2056: the review app's `/claude` opened work on an issue carrying no
    ownership label, and since #2053 nothing labels one either — so an agent was working an
    issue the operator had never handed over, and the issue did not even look claimed."""
    port = FakePort(comments=[_app_comment(1, "/claude fix the header")])
    store = store_for(tmp_path)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.directives == []
    assert store.read()["issues"] == {}
    control = queue.controls(store.read())["1"]
    assert control["status"] == "rejected"
    assert control["rejection"] == "app-cannot-start"
    # Refused out loud, naming this board's own label, because the person who has to decide
    # whether the work happens is not the app being answered.
    assert "`claude` label" in _refusal_comments(outcome)[0]


def test_a_person_still_starts_an_unlabelled_card_by_asking(tmp_path):
    # The gate is about who asked, not about the label: a person typing `/claude` is the
    # instruction, and refusing it would break the ordinary way work is started here.
    port = FakePort(comments=[comment(1, "/claude fix the header")])
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW, ownership_label="claude")

    assert payloads(outcome.directives[0]) == ["fix the header"]
    assert _refusal_comments(outcome) == []


def test_an_app_may_start_a_card_the_label_already_put_in_play(tmp_path):
    # The label is a person handing the card over, and discovery seeds it two phases before
    # the commands are read — so the app's instruction lands on a card that already exists.
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
        }
    })
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert queue.controls(store.read())["1"]["status"] != "rejected"
    assert port.ownership_actor_requests == [{"store#5": "e1"}]
    assert _refusal_comments(outcome) == []


def test_an_unreadable_label_sweep_never_proves_the_card_is_unlabelled(tmp_path):
    """"The engine has no record of this card" only means "no ownership label" when the
    labelled sweep was actually read. An unreadable one seeds nothing, so refusing on it would
    permanently reject an app's instruction for a card a person really had handed over."""
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")], labelled_complete=False
    )
    store = store_for(tmp_path)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    # Undecided, not refused: no comment, no rejection, and the command survives for a wake
    # that can see the labels.
    assert queue.controls(store.read())["1"]["status"] == "new"
    assert _refusal_comments(outcome) == []
    assert outcome.directives == []


_LEDGER = {
    "ownership": {
        "initialized": True,
        "events": {},
        "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
    }
}


def test_a_label_nobody_authorized_does_not_let_an_app_start_the_card(tmp_path):
    """Discovery seeds a record for every labelled issue *before* the labeler is authorized, so
    the record's existence is not the hand-over. A denied labeler starts nothing — and must not
    start something for an app either, which would reopen this issue through a labelled card."""
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "drive-by"},
        permissions={"drive-by": "read"},
    )
    store = store_for(tmp_path, _LEDGER)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.directives == []
    assert queue.controls(store.read())["1"]["rejection"] == "app-cannot-start"
    assert _refusal_comments(outcome)


def test_a_labeler_still_being_checked_leaves_an_apps_instruction_undecided(tmp_path):
    # The ownership rules defer an unresolved labeler rather than trusting it; the app's
    # instruction waits with it instead of being refused on a question nobody has answered.
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "unknown"},
        permissions={"unknown": None},
    )
    store = store_for(tmp_path, _LEDGER)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert queue.controls(store.read())["1"]["status"] == "new"
    assert _refusal_comments(outcome) == []
    assert outcome.directives == []

    # Once the labeler proves out, the label starts the card and the app's instruction rides it.
    port.permissions["unknown"] = "write"
    second = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), policy=_BOT_CONTROLLER,
        ownership_label="claude",
    )
    assert payloads(second.directives[0]) == ["fix the header"]


def test_a_standing_label_a_rebuilt_ledger_adopts_is_not_a_hand_over_for_an_app(tmp_path):
    """On a fresh ledger nothing is a start: the world is adopted rather than re-run. The
    labeler's authority is recorded before that classification happens, so it must not stand in
    for the start the ownership path deliberately did not make."""
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("old", "2026-07-20T12:00:00Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.directives == []
    assert queue.controls(store.read())["1"]["rejection"] == "app-cannot-start"
    assert not store.read()["issues"]["store#5"].get("manual_start")


def test_a_label_adopted_on_first_sight_is_not_a_hand_over_for_an_app_either(tmp_path):
    # The other adoption path: an initialized ledger meeting a long-standing label. The engine
    # says "re-apply the label to start it", and an app's `/claude` is not that re-application.
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("old", "2026-07-20T12:00:00Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {"ownership": {"initialized": True, "events": {}}})
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.suppressed == ["store#5"]
    assert outcome.directives == []
    assert queue.controls(store.read())["1"]["rejection"] == "app-cannot-start"


def test_a_known_cards_unresolved_labeler_defers_an_apps_instruction_too(tmp_path):
    """A card the engine already knows gets no discovery marker of its own, so the deferral is
    only knowable from the ownership pass. The operator re-applies the label as instructed, the
    permission lookup happens to be unreadable, and the app's instruction must wait with it
    rather than be rejected on a question nobody has answered."""
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "unknown"},
        permissions={"unknown": None},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}, **_LEDGER})
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert queue.controls(store.read())["1"]["status"] == "new"
    assert _refusal_comments(outcome) == []
    assert outcome.directives == []

    # The labeler proves out, the label starts the card, and the instruction is still there.
    port.permissions["unknown"] = "write"
    second = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), policy=_BOT_CONTROLLER,
        ownership_label="claude",
    )
    assert payloads(second.directives[0]) == ["fix the header"]


def test_an_app_cannot_hand_itself_the_card_by_adding_the_label(tmp_path):
    """A label's actor clears the same Work policy a command does, and an allowlisted app clears
    it — so without this the app could label the issue, collect the classified start, and let its
    own `/claude` through as though a person had handed the card over."""
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")],
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "reviewer[bot]"},
    )
    store = store_for(tmp_path, _LEDGER)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.directives == []
    assert queue.controls(store.read())["1"]["rejection"] == "app-cannot-start"


def test_an_app_label_does_not_resume_a_blocked_card_nobody_started(tmp_path):
    """The app's label is left on the issue, and the blocked-card resume reads the live label
    rather than any start: it needs no dispatch and no `started` of its own. So a card resting
    in Blocked with its dependencies cleared would begin work off a label an app added."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={
            "store#5": CardSnapshot(
                status="Blocked", owned=True, dependencies=("store#9",), blockers=()
            )
        },
        actors={"store#5": "reviewer[bot]"},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Blocked"}}, **_LEDGER})
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    rec = store.read()["issues"]["store#5"]
    assert outcome.directives == []
    assert "recovery_action" not in rec
    assert rec["status"] == "Blocked"

    # A person putting the label there is a hand-over, and the same wake resumes the card.
    port.actors["store#5"] = "eero"
    port._label_events["store#5"] = [LabelEvent("e2", "2026-07-25T12:00:02Z")]
    second = run_wake(
        store, port, now=_NOW + timedelta(seconds=30), policy=_BOT_CONTROLLER,
        ownership_label="claude",
    )
    assert second.directives


def test_the_engines_own_account_labelling_a_card_starts_it(tmp_path):
    """An agent chat asked to "put the claude label on it" labels as the engine's own account.
    That account's comments authorize nothing, but its label is the operator's ask (#2390)."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e2", "2026-07-25T12:00:02Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "Claude-Bot"},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}, **_LEDGER})
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert any("label added -> start" in line for line in outcome.log), outcome.log
    assert not any("not authorized" in line for line in outcome.log)


_APP_ENGINE = auth.parse_policy(
    {"work": {"allow": ["reviewer[bot]"]}, "bot": "agentdeck-eerovil[bot]"}
)


def test_the_engines_own_app_label_resumes_a_blocked_card_but_another_apps_does_not(tmp_path):
    """On an organization the engine speaks as the AgentDeck App, a `[bot]` login. Its label is
    the operator's ask like the agent account's is (#2390); any other app's still is not
    (#2056)."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={
            "store#5": CardSnapshot(
                status="Blocked", owned=True, dependencies=("store#9",), blockers=()
            )
        },
        actors={"store#5": "agentdeck-eerovil[bot]"},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Blocked"}}, **_LEDGER})
    outcome = run_wake(store, port, now=_NOW, policy=_APP_ENGINE, ownership_label="claude")

    assert outcome.directives, outcome.log
    assert not any("labeled by the app" in line for line in outcome.log)

    other = FakePort(
        labelled=["store#6"],
        label_events={"store#6": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={
            "store#6": CardSnapshot(
                status="Blocked", owned=True, dependencies=("store#9",), blockers=()
            )
        },
        actors={"store#6": "reviewer[bot]"},
    )
    other_store = store_for(
        tmp_path / "other", {"issues": {"store#6": {"status": "Blocked"}}, **_LEDGER}
    )
    refused = run_wake(
        other_store, other, now=_NOW, policy=_APP_ENGINE, ownership_label="claude"
    )
    assert refused.directives == []
    assert any("labeled by the app reviewer[bot]" in line for line in refused.log)


def test_the_engines_own_app_labelling_a_new_issue_starts_it(tmp_path):
    """On bazaar, gallery and trailhead the `gh` shim files an agent's issue as the
    AgentDeck App and adds the ownership label in the same call (#2505). That label is the
    engine's own account's, so it starts the card (#2390)."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e2", "2026-07-25T12:00:02Z")]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
        actors={"store#5": "agentdeck-eerovil[bot]"},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}, **_LEDGER})
    outcome = run_wake(store, port, now=_NOW, policy=_APP_ENGINE, ownership_label="claude")

    assert any("label added -> start" in line for line in outcome.log), outcome.log
    assert not any("labeled by the app" in line for line in outcome.log)


def test_an_app_label_accepted_before_this_shipped_is_not_trusted_after_it(tmp_path):
    """The code this replaces accepted an app's label and consumed its event, leaving only the
    work authority it recorded. A consumed event is never fresh again, so the marker this engine
    writes for a refused bot label is never written for those cards — and the live label would
    go on reading as ownership straight through the upgrade."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": [LabelEvent("e1", "2026-07-20T12:00:00Z")]},
        snapshots={
            "store#5": CardSnapshot(
                status="Blocked", owned=True, dependencies=("store#9",), blockers=()
            )
        },
    )
    # Exactly what a pre-upgrade state.json holds: the event already in the ledger, and the app
    # named as this card's work authority by the label it added.
    store = store_for(tmp_path, {
        "issues": {
            "store#5": {
                "status": "Blocked",
                "work_authority": {"login": "reviewer[bot]", "source": "label", "at": ""},
            }
        },
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    })
    outcome = run_wake(store, port, now=_NOW, ownership_label="claude")

    assert outcome.directives == []
    assert "recovery_action" not in store.read()["issues"]["store#5"]


def test_an_app_command_checkpointed_before_this_shipped_is_still_refused(tmp_path):
    """A crash between the intake checkpoint and control processing leaves a durable `new`
    record written by the old code, which carries no `app` bit. Reading a missing bit as "not an
    app" would let exactly this instruction through, once, on the wake after the deploy."""
    legacy = {
        "controls": {
            "1": {
                "id": "1",
                "repo": "store",
                "number": 5,
                "kind": "claude",
                "payload": "fix the header",
                "body": "/claude fix the header",
                "url": "",
                "author": "reviewer[bot]",
                "created_at": "2026-07-25T11:59:00Z",
                "status": "new",
            }
        }
    }
    store = store_for(tmp_path, legacy)
    outcome = run_wake(
        store, FakePort(), now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert outcome.directives == []
    assert store.read()["issues"] == {}
    assert queue.controls(store.read())["1"]["rejection"] == "app-cannot-start"


def test_a_refusal_whose_comment_did_not_land_is_kept_for_the_next_wake(tmp_path):
    # Same rule a refused release follows: nothing is resolved until the answer is visible.
    port = FakePort(
        comments=[_app_comment(1, "/claude fix the header")], refuse={EffectKind.COMMENT}
    )
    store = store_for(tmp_path)
    outcome = run_wake(
        store, port, now=_NOW, policy=_BOT_CONTROLLER, ownership_label="claude"
    )

    assert queue.controls(store.read())["1"]["status"] == "new"
    assert outcome.directives == []


def test_a_bot_nobody_named_is_still_never_a_controller(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(comments=[_app_comment(1, "/claude ship it")]), now=_NOW)

    assert queue.controls(store.read()) == {}
    assert outcome.directives == []


def test_an_allowlisted_bot_holds_only_the_tier_it_was_named_for(tmp_path):
    # Work lets it ask for work; releasing is a different authority nobody gave it.
    port = FakePort(comments=[_app_comment(1, "/merge")])
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW, policy=_BOT_CONTROLLER)

    assert queue.controls(store.read())["1"]["kind"] == "unauthorized"
    assert outcome.directives == []


def test_the_engines_own_account_cannot_be_allowlisted_into_a_controller(tmp_path):
    policy = auth.parse_policy({"work": {"allow": ["claude-bot"]}, "bot": "claude-bot"})
    port = FakePort(comments=[comment(1, "/claude go", author="claude-bot", user_type="Bot")])
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW, policy=policy)

    assert queue.controls(store.read()) == {}
    assert outcome.directives == []


def test_an_invalid_command_is_rejected_without_a_worker(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(comments=[comment(1, "/claude")]), now=_NOW)
    assert queue.controls(store.read())["1"]["status"] == "rejected"
    assert outcome.directives == []


# --- durability ordering ---

def test_queued_work_and_the_send_intent_are_durable_before_the_directive(tmp_path):
    store = store_for(tmp_path)
    outcome = run_wake(store, FakePort(comments=[comment(1, "/claude go")]), now=_NOW)

    # The command was published before any dispatch existed...
    intake_publish = next(p for p in store.publishes if "1" in (p.get("controls") or {}))
    assert not (intake_publish.get("issues") or {}).get("store#5", {}).get("dispatch")
    # ...and the send intent was published while still inside the transaction, before the
    # directive was handed out, so a crash leaves a dispatch to await, not a lost delivery.
    assert any(
        (p.get("issues") or {}).get("store#5", {}).get("dispatch", {}).get("sent_at")
        for p in store.publishes
    )
    assert len(outcome.directives) == 1


# --- refusal to run ---

def test_drift_refuses_the_cycle_and_still_reports_health(tmp_path):
    port = FakePort(comments=[comment(1, "/claude go")])
    outcome = run_wake(store_for(tmp_path), port, now=_NOW, drift="engine")
    assert outcome.halted == "drift"
    assert outcome.verdict.state is HealthState.FAILING
    assert port.scans == 0
    assert outcome.directives == []


def test_a_paused_instance_does_no_work_but_stays_fresh(tmp_path):
    store = store_for(tmp_path)
    with store.transaction() as state:
        health.set_paused(state, True, note="release freeze")
    port = FakePort(comments=[comment(1, "/claude go")])
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.halted == "paused"
    assert outcome.verdict.state is HealthState.PAUSED
    assert port.scans == 0


# --- budget ---


def test_model_budget_signal_does_not_hold_dispatch(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Todo")},
        budget=False,
    )
    initial = {
        "issues": {"store#5": {"status": "Todo", "pending": ["9"]}},
        "controls": {"9": _instruction("9")},
    }
    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.held is False


def test_the_worker_cap_bounds_dispatch_and_the_rest_wait(tmp_path):
    controls = {}
    issues = {}
    for index, number in enumerate((5, 6), start=1):
        cid = str(index)
        controls[cid] = {
            "id": cid, "repo": "store", "number": number, "kind": "claude",
            "payload": f"go {number}", "status": "queued", "created_at": f"t{index}",
        }
        issues[f"store#{number}"] = {"status": "Todo", "pending": [cid]}
    port = FakePort(
        snapshots={key: CardSnapshot(status="Todo") for key in issues},
    )
    store = store_for(tmp_path, {"issues": issues, "controls": controls})
    outcome = run_wake(store, port, now=_NOW, cap=1)
    assert len(outcome.directives) == 1
    assert any("worker cap 1 reached" in line for line in outcome.log)


# --- dispatch settlement ---

def _dispatched(sent_at, *, attempts=1, action="new"):
    return {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen1",
                "started_at": _NOW.isoformat(),
                "dispatch": {
                    "id": "d1",
                    "action": action,
                    "generation": "gen1",
                    "attempts": attempts,
                    "sent_at": sent_at,
                },
                "processing": ["9"],
            }
        },
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "go", "status": "processing", "created_at": "x"}},
    }


def test_a_dispatch_inside_the_window_is_awaited_never_re_sent(tmp_path):
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    outcome = run_wake(store_for(tmp_path, _dispatched(sent)), port, now=_NOW)
    assert outcome.directives == []
    assert any("awaiting acknowledgement" in line for line in outcome.log)


def test_a_capacity_refusal_retries_without_spending_a_delivery_attempt(tmp_path):
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "at_capacity"},
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3))

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert len(outcome.directives) == 1
    assert outcome.directives[0]["dispatch_id"] == "d1"
    assert outcome.directives[0]["generation"] == "gen1"
    assert rec["dispatch"]["attempts"] == 3
    assert rec["status"] == "In progress"
    assert "dispatch_failed" not in rec
    assert any("deferred by host capacity" in line for line in outcome.log)


def test_an_active_worker_defers_a_fresh_dispatch_without_spending_an_attempt(tmp_path):
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "active_turn"},
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3, action="merge-review"))

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert len(outcome.directives) == 1
    assert outcome.directives[0]["dispatch_id"] == "d1"
    assert outcome.directives[0]["generation"] == "gen1"
    assert rec["dispatch"]["attempts"] == 3
    assert rec["status"] == "In progress"
    assert "dispatch_failed" not in rec
    assert any("deferred by an active card worker" in line for line in outcome.log)



@pytest.mark.parametrize(
    ("refusal", "phrase"),
    [
        ("prior_run_executing", "an older Run still stopping"),
        ("host_shutting_down", "a host shutdown"),
    ],
)
def test_every_same_delivery_refusal_defers_without_crashing_the_wake(
    tmp_path, refusal, phrase
):
    # These two once had no phrase, so the lookup raised KeyError and every later wake of the
    # instance died on the same parked dispatch, starting no card at all.
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": refusal},
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3, action="merge-review"))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives[0]["dispatch_id"] == "d1"
    assert store.read()["issues"]["store#5"]["dispatch"]["attempts"] == 3
    assert any(f"deferred by {phrase}" in line for line in outcome.log)


def test_every_same_delivery_refusal_has_a_phrase():
    from agentdeck.poller_engine import cycle
    from agentdeck.poller_engine.refusal_policy import (
        _REDECIDED_SAME_DELIVERY,
        _RESUMED_SAME_DELIVERY,
    )

    assert set(_REDECIDED_SAME_DELIVERY) | {_RESUMED_SAME_DELIVERY} <= set(
        cycle._SAME_DELIVERY_REASONS
    )

def test_an_acknowledged_dispatch_is_cleared(tmp_path):
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")}, acks=["d1"])
    store = store_for(tmp_path, _dispatched(sent))
    outcome = run_wake(store, port, now=_NOW)
    assert "dispatch" not in store.read()["issues"]["store#5"]
    assert any("acknowledged" in line for line in outcome.log)


def test_a_timed_out_dispatch_retries_under_the_same_lease_and_run(tmp_path):
    """The runtime froze this delivery id under its Run; a new Run is refused for good (#2824).

    In choir-tracks-cf#23 the first attempt landed but timed out unacknowledged. The retry
    under a fresh generation was refused as ``delivery_correlation_conflict``, parked the card
    in Blocked, and fenced the one live worker as stale. The same lease and id instead let the
    runtime replay what it did, which acknowledges a spawn that landed.
    """
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    state = _dispatched(sent)
    state["issues"]["store#5"]["dispatch"]["run_id"] = "gen1"
    store = store_for(tmp_path, state)
    outcome = run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert len(outcome.directives) == 1
    assert rec["dispatch"]["attempts"] == 2
    assert rec["generation"] == "gen1"
    directive = outcome.directives[0]
    assert (directive["generation"], directive["run_id"], directive["dispatch_id"]) == (
        "gen1", "gen1", "d1"
    )

    # The runtime replays the landed spawn's receipt: the card is acknowledged, not parked,
    # and the worker started by the first attempt still holds a live lease.
    port.acks.add("d1")
    later = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    rec = store.read()["issues"]["store#5"]
    assert "dispatch" not in rec
    assert rec["status"] == "In progress"
    assert rec["generation"] == "gen1"
    assert any("acknowledged" in line for line in later.log)


def test_a_dispatch_that_never_lands_hands_back_with_the_instructions_kept(tmp_path):
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, _dispatched(sent, attempts=3))
    outcome = run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["status"] == "Blocked"
    assert rec["dispatch_failed"] is True
    assert "dispatch" not in rec
    # The instruction is queued again, not lost.
    assert rec["pending"] == ["9"]
    assert state["controls"]["9"]["status"] == "queued"
    assert outcome.report.deliveries_failed == 1
    # And it is not immediately re-spawned: the spawn path is broken until the cooldown.
    assert outcome.directives == []


def _comments_of(port):
    return [str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT]


def test_a_dispatch_that_never_lands_says_so_on_the_card(tmp_path):
    """#1491: a card parked for a transport failure looked exactly like one blocked on work.

    The give-up wrote a log line nobody reads and posted nothing, so #1488 sat in *Blocked*
    for an hour with no worker, no comment, and no sign anything had gone wrong.
    """
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "codex_create_failed"},
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3))

    run_wake(store, port, now=_NOW)

    said = _comments_of(port)
    assert len(said) == 1
    notice = said[0]
    assert "No worker could be started" in notice
    assert "3 attempt(s)" in notice
    assert "none was ever acknowledged" in notice
    assert "queued instructions were kept" in notice
    assert "try once more on its own" in notice
    assert "`/claude`" in notice


def test_a_park_for_an_unprepared_worktree_names_it_instead_of_silence(tmp_path):
    """#2816: a hand-made worktree refused every attempt, and the card said nobody answered."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    why = "base `origin/main`: legacy: /c/.worktrees/issue-5 exists, and it has uncommitted changes"
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": f"workspace_unprepared: {why}"},
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3))

    run_wake(store, port, now=_NOW)

    notice = _comments_of(port)[0]
    assert "its worktree could not be prepared" in notice
    assert why in notice and "Nothing was sent to any worker" in notice
    assert "no answer at all" not in notice and "none was ever acknowledged" not in notice
    assert "try once more on its own" in notice


def test_a_park_that_cannot_be_announced_is_not_consumed(tmp_path):
    """The comment gates the park: a silent park is the defect, so retry the whole ending."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        refuse=[EffectKind.COMMENT],
    )
    store = store_for(tmp_path, _dispatched(sent, attempts=3))

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert "dispatch_failed" not in rec
    assert rec["dispatch"]["attempts"] == 3
    assert any("the notice did not land" in line for line in outcome.log)


def test_a_parked_spawn_retries_itself_once_after_the_cooldown(tmp_path):
    """#1491/#490: the transient thing that parked the card is gone minutes later.

    Driven from #1488's own shape: a label-driven spawn with no queued instruction, refused
    for host capacity and then never acknowledged. Before this the card waited for a person
    who had no way of knowing it was waiting.
    """
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _dispatched(sent, attempts=3)
    initial["issues"]["store#5"].update(started=True, processing=[], worktrees=["/w/5"])
    initial["controls"] = {}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "codex_create_failed"},
    )
    store = store_for(tmp_path, initial)

    parked = run_wake(store, port, now=_NOW)
    assert parked.directives == []
    rec = store.read()["issues"]["store#5"]
    assert rec["dispatch_failed"] is True
    assert rec["dispatch_failed_clean"] is True
    # Why it parked survives the dispatch being dropped, for the Run trace (#2141).
    assert rec["dispatch_failed_refusal"] == "codex_create_failed"
    assert rec["dispatch_failed_refusal_final"] is True

    port.snapshots["store#5"] = CardSnapshot(status="Blocked")
    early = run_wake(store, port, now=_NOW + timedelta(minutes=20))
    assert early.directives == []
    assert store.read()["issues"]["store#5"]["dispatch_failed"] is True

    due = run_wake(store, port, now=_NOW + timedelta(minutes=31))

    rec = store.read()["issues"]["store#5"]
    assert "dispatch_failed" not in rec
    assert rec["dispatch_rearmed"] is True
    assert len(due.directives) == 1
    # A fresh delivery id and generation: the parked one was never acknowledged and its
    # lease is invalid, so this is a first attempt, not a resend of an unknown outcome.
    assert due.directives[0]["dispatch_id"] != "d1"
    assert due.directives[0]["generation"] != "gen1"
    assert any("Trying to start a worker again" in said for said in _comments_of(port))


def test_the_automatic_retry_is_spent_once_and_not_repeated(tmp_path):
    """A card whose free retry also failed rests for a person rather than looping forever."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _dispatched(sent, attempts=3)
    initial["issues"]["store#5"].update(dispatch_rearmed=True)
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "codex_create_failed"},
    )
    store = store_for(tmp_path, initial)

    parked = run_wake(store, port, now=_NOW)

    notice = _comments_of(port)[0]
    assert "already the retry" in notice
    assert "try once more on its own" not in notice
    assert parked.directives == []

    port.snapshots["store#5"] = CardSnapshot(status="Blocked")
    later = run_wake(store, port, now=_NOW + timedelta(hours=3))

    assert later.directives == []
    assert store.read()["issues"]["store#5"]["dispatch_failed"] is True


def test_an_ack_lost_park_is_never_retried_automatically(tmp_path):
    """#1491 review: a dispatch that ended with no answer may already have spawned a worker.

    The host wrote no deferral, so nothing proves the send failed before the provider was
    touched — only that no acknowledgement came back. Re-arming that under a fresh delivery
    id is exactly the second worker at-most-once exists to prevent, so the card is fenced
    and waits for a person however long it sits there.
    """
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _dispatched(sent, attempts=3)
    initial["issues"]["store#5"].update(started=True, processing=[], worktrees=["/w/5"])
    initial["controls"] = {}
    # No deferral: the send timed out, and its outcome is unknown.
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, initial)

    parked = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["dispatch_failed"] is True
    assert "dispatch_failed_clean" not in rec
    notice = _comments_of(port)[0]
    assert "no answer at all" in notice
    assert "waits for a person" in notice
    assert "try once more on its own" not in notice
    assert parked.directives == []

    port.snapshots["store#5"] = CardSnapshot(status="Blocked")
    for minutes in (31, 60 * 6):
        later = run_wake(store, port, now=_NOW + timedelta(minutes=minutes))
        assert later.directives == [], f"a fresh delivery was emitted after {minutes} minutes"
        assert store.read()["issues"]["store#5"]["dispatch_failed"] is True


def test_one_unobserved_attempt_taints_a_dispatch_that_was_clean_before_it(tmp_path):
    """Cleanliness is ANDed across attempts, never reset: ambiguity is permanent."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _dispatched(sent, attempts=2)
    initial["issues"]["store#5"].update(started=True, processing=[], worktrees=["/w/5"])
    initial["controls"] = {}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"d1": "codex_create_failed"},
    )
    store = store_for(tmp_path, initial)

    # Attempt 2 is positively refused, so the retry is emitted with `clean` still true.
    run_wake(store, port, now=_NOW)
    assert store.read()["issues"]["store#5"]["dispatch"]["clean"] is True

    # Attempt 3 gets no answer at all, and that is what the park has to remember.
    port.deferrals = {}
    run_wake(store, port, now=_NOW + timedelta(minutes=5))

    parked = store.read()["issues"]["store#5"]
    assert parked["dispatch_failed"] is True
    assert "dispatch_failed_clean" not in parked

    port.snapshots["store#5"] = CardSnapshot(status="Blocked")
    resumed = run_wake(store, port, now=_NOW + timedelta(minutes=40))
    assert resumed.directives == []


def test_a_landed_dispatch_gives_the_card_its_free_retry_back(tmp_path):
    """The park is spent history once something lands; the next one earns its own retry."""
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    initial = _dispatched(sent)
    initial["issues"]["store#5"].update(dispatch_rearmed=True)
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")}, acks=["d1"])
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    assert "dispatch_rearmed" not in store.read()["issues"]["store#5"]


def _active_turn(extra=None):
    rec = {
        "status": "In progress",
        "generation": "live-gen",
        "started": True,
        "processing": ["9"],
    }
    rec.update(extra or {})
    return {
        "issues": {"store#5": rec},
        "controls": {"9": _instruction("9", status="processing")},
    }


def test_a_plain_followup_steers_the_live_generation_without_using_a_worker_slot(tmp_path):
    port = FakePort(
        comments=[comment(10, "/claude change direction")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, _active_turn())

    outcome = run_wake(store, port, now=_NOW, cap=1, proposal_worker_cap=0)

    assert len(outcome.directives) == 1
    directive = outcome.directives[0]
    assert directive["steer"] is True
    assert directive["generation"] == "live-gen"
    assert payloads(directive) == ["change direction"]
    rec = store.read()["issues"]["store#5"]
    assert rec["pending"] == ["10"]
    assert rec["processing"] == ["9"]
    assert store.read()["controls"]["10"]["status"] == "queued"
    assert EffectKind.ADD_OWNERSHIP not in port.kinds()
    assert EffectKind.SET_STATUS not in port.kinds()


def test_an_acknowledged_steer_joins_processing_in_comment_order(tmp_path):
    port = FakePort(
        comments=[comment(10, "/claude first"), comment(11, "/claude second")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, _active_turn())
    first = run_wake(store, port, now=_NOW)
    dispatch_id = first.directives[0]["dispatch_id"]
    assert payloads(first.directives[0]) == ["first", "second"]

    accepted = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        acks=[dispatch_id],
    )
    run_wake(store, accepted, now=_NOW + timedelta(seconds=30))

    state = store.read()
    assert state["issues"]["store#5"]["processing"] == ["9", "10", "11"]
    assert state["issues"]["store#5"]["pending"] == []
    assert state["controls"]["10"]["status"] == "processing"
    assert state["controls"]["11"]["status"] == "processing"


def test_a_refused_steer_stays_queued_until_the_live_generation_ends(tmp_path):
    port = FakePort(
        comments=[comment(10, "/claude change direction")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, _active_turn())
    first = run_wake(store, port, now=_NOW)
    dispatch_id = first.directives[0]["dispatch_id"]

    refused = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={dispatch_id: "worker_inactive"},
    )
    outcome = run_wake(store, refused, now=_NOW + timedelta(seconds=30))

    rec = store.read()["issues"]["store#5"]
    assert outcome.directives == []
    assert rec["pending"] == ["10"]
    assert rec["processing"] == ["9"]
    assert rec["steer_refused_generation"] == "live-gen"
    assert "dispatch" not in rec
    assert "dispatch_failed" not in rec


def test_an_uncertain_steer_retries_the_same_delivery_on_the_same_lease(tmp_path):
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = _instruction("10")
    initial["issues"]["store#5"]["dispatch"] = {
        "id": "steer-1",
        "action": "followup",
        "generation": "live-gen",
        "attempts": 3,
        "sent_at": sent,
        "steer": True,
        "control_ids": ["10"],
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["dispatch_id"] == "steer-1"
    assert outcome.directives[0]["generation"] == "live-gen"
    assert outcome.directives[0]["steer"] is True


def test_a_steer_that_can_never_be_acknowledged_parks_instead_of_retrying_forever(tmp_path):
    """#1886: a steer was exempt from the dispatch cap, so a permanently refused delivery
    re-established its outcome once a window for as long as the deck ran — `agentdeck#1840`
    reached attempt 797 — while the card still read `In progress` to anybody looking."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = _instruction("10")
    initial["issues"]["store#5"]["dispatch"] = {
        "id": "steer-1",
        "action": "followup",
        "generation": "live-gen",
        "attempts": dispatch.STEER_MAX_ATTEMPTS,
        "sent_at": sent,
        "steer": True,
        "control_ids": ["10"],
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert outcome.directives == []
    assert [
        effect.value for effect in port.applied if effect.kind is EffectKind.SET_STATUS
    ] == ["Blocked"]


def test_a_steer_one_attempt_short_of_the_bound_still_retries(tmp_path):
    """The exemption exists for a slow acknowledgement joining an active turn, so the bound
    must not shorten that case — only end it."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = _instruction("10")
    initial["issues"]["store#5"]["dispatch"] = {
        "id": "steer-1",
        "action": "followup",
        "generation": "live-gen",
        "attempts": dispatch.STEER_MAX_ATTEMPTS - 1,
        "sent_at": sent,
        "steer": True,
        "control_ids": ["10"],
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["dispatch_id"] == "steer-1"


def _boundary_dispatch(attempts: int, sent: str) -> dict:
    """A message into a live turn (a steer), which re-sends under its one delivery id."""
    return {
        "id": "phase-1",
        "action": "followup",
        "generation": "live-gen",
        "attempts": attempts,
        "sent_at": sent,
        "steer": True,
        "control_ids": ["10"],
    }


def test_a_parked_boundary_keeps_the_live_generations_receipts(tmp_path):
    """The message was aimed at a worker that may still be running, so only this delivery's
    receipts go. Taking the generation's too would blind the engine to that worker's turn
    ending, which is the one signal still capable of arriving."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = _instruction("10")
    initial["issues"]["store#5"]["dispatch"] = _boundary_dispatch(1, sent)
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"phase-1": "delivery_id_conflict"},
    )

    run_wake(store_for(tmp_path, initial), port, now=_NOW)

    discards = [
        effect.value for effect in port.applied
        if effect.kind is EffectKind.DISCARD_RECEIPTS
    ]
    assert discards
    assert all(discard["generation"] is None for discard in discards)
    assert all(discard["dispatch_id"] == "phase-1" for discard in discards)


def test_an_ordinary_dispatch_parks_on_the_same_permanent_refusal(tmp_path):
    """A retry mints a fresh generation but keeps the dispatch id, and the runtime's delivery
    id is built from that id — so an ordinary dispatch cannot escape the conflict either. It
    was already bounded at three attempts, so the loop was never visible here; what changes is
    that it stops at the first refusal and never gets the free re-arm."""
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = _instruction("10")
    initial["issues"]["store#5"]["dispatch"] = {
        "id": "spawn-1",
        "action": "start",
        "generation": "live-gen",
        "attempts": 1,
        "sent_at": sent,
        "control_ids": ["10"],
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        deferrals={"spawn-1": "delivery_id_conflict"},
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    rec = store.read()["issues"]["store#5"]
    assert rec["dispatch_failed"] is True
    assert "dispatch_failed_clean" not in rec
    said = " ".join(_comments_of(port))
    assert "delivery_id_conflict" in said


def test_a_landed_steer_resolves_with_the_turn_that_accepted_it(tmp_path):
    port = FakePort(
        comments=[comment(10, "/claude include this")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, _active_turn())
    first = run_wake(store, port, now=_NOW)

    run_wake(
        store,
        FakePort(
            snapshots={
                "store#5": CardSnapshot(
                    status="In review", terminal_marker="In review"
                )
            },
            acks=[first.directives[0]["dispatch_id"]],
        ),
        now=_NOW + timedelta(seconds=30),
    )

    state = store.read()
    assert state["issues"]["store#5"]["processing"] == []
    assert state["controls"]["9"]["status"] == "done"
    assert state["controls"]["10"]["status"] == "done"


def test_terminal_recovery_counts_the_round_the_lost_board_write_would_have(tmp_path):
    """The worker's report is durable in the marker; recovery is just who noticed (#1840).

    Without this, a round that came back through reconciliation stayed invisible to the
    head-watching reviewer, which is exactly the handback #1840 is about.
    """
    store = store_for(tmp_path, _active_turn({"worker_rounds": 1}))
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", terminal_marker="In review")}
    )

    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["worker_rounds"] == 2
    assert rec["counted_rounds"] == ["live-gen"]


def test_recovery_does_not_count_the_same_generation_twice(tmp_path):
    store = store_for(
        tmp_path,
        _active_turn({"worker_rounds": 2, "counted_rounds": ["live-gen"]}),
    )
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", terminal_marker="In review")}
    )

    run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["worker_rounds"] == 2


def test_an_extension_proposal_never_steers_an_active_worker(tmp_path):
    initial = _active_turn({"pending": ["10"]})
    initial["controls"]["10"] = {
        **_instruction("10"),
        "author": "extension:sentry",
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert outcome.directives == []


def test_a_new_instruction_re_arms_a_card_whose_spawn_was_given_up(tmp_path):
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, _dispatched(sent, attempts=3))
    run_wake(store, port, now=_NOW)

    port.comments = [comment(11, "/claude try again")]
    port.snapshots["store#5"] = CardSnapshot(status="Blocked")
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert "dispatch_failed" not in store.read()["issues"]["store#5"]
    assert len(outcome.directives) == 1
    assert payloads(outcome.directives[0]) == ["go", "try again"]


# --- titles nobody wrote (#338 placeholders, #595 pastes) ---

PLACEHOLDER = "Requested changes"
FILED = f"{PLACEHOLDER} fix the export encoding"
# GitHub names an issue after the first line of what was pasted into it, so a screenshot
# dropped in from a phone becomes the card's title everywhere it is read.
PASTED = '<img width="1080" height="2414" alt="Image"…'
PASTED_BODY = f'{PASTED} src="x.png" />\n\nThe calibration disagrees with the 5h samples'


def _retitles(port):
    return [
        (f"{effect.repo}#{effect.number}", effect.value)
        for effect in port.applied
        if effect.kind is EffectKind.SET_TITLE
    ]


def test_a_card_still_wearing_its_template_placeholder_is_retitled(tmp_path):
    """The wake that sees the card fixes its title, so nothing downstream ever reads the
    placeholder — not the board, not the issue list, not the deck's card for the worker."""
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(title=FILED)})
    outcome = run_wake(
        store_for(tmp_path, initial), port, now=_NOW, placeholder_titles=(PLACEHOLDER,)
    )
    assert _retitles(port) == [("store#5", "fix the export encoding")]
    assert any("renamed to" in line for line in outcome.log)


def test_a_project_that_names_no_placeholder_keeps_the_title_it_was_given(tmp_path):
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(title=FILED)})
    run_wake(store_for(tmp_path, initial), port, now=_NOW)
    assert _retitles(port) == []


def test_a_card_named_after_a_paste_is_retitled_without_any_placeholder_configured(tmp_path):
    """An `<img …` tag names no work in any project, so recognising it needs no manifest."""
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(title=PASTED, body=PASTED_BODY)})
    run_wake(store_for(tmp_path, initial), port, now=_NOW)
    assert _retitles(port) == [("store#5", "The calibration disagrees with the 5h samples")]


def test_a_card_is_retitled_once_and_a_human_who_renames_it_back_is_not_fought(tmp_path):
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    store = store_for(tmp_path, initial)
    port = FakePort(snapshots={"store#5": CardSnapshot(title=FILED)})
    run_wake(store, port, now=_NOW, placeholder_titles=(PLACEHOLDER,))
    assert _retitles(port) == [("store#5", "fix the export encoding")]
    assert store.read()["issues"]["store#5"]["title_repaired"] is True

    # Put a title that still matches back on the card — the engine's one attempt is spent, so
    # what a human calls their own issue stands.
    port.applied.clear()
    port.snapshots["store#5"] = CardSnapshot(title=FILED)
    run_wake(store, port, now=_NOW + timedelta(seconds=30), placeholder_titles=(PLACEHOLDER,))
    assert _retitles(port) == []


def test_a_refused_retitle_is_attempted_again_on_the_next_wake(tmp_path):
    """The one attempt is spent by a write that landed, not by one GitHub turned away."""
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    store = store_for(tmp_path, initial)
    port = FakePort(
        snapshots={"store#5": CardSnapshot(title=FILED)}, refuse=(EffectKind.SET_TITLE,)
    )
    run_wake(store, port, now=_NOW, placeholder_titles=(PLACEHOLDER,))
    assert store.read()["issues"]["store#5"].get("title_repaired") is None

    port.refuse = set()
    run_wake(store, port, now=_NOW + timedelta(seconds=30), placeholder_titles=(PLACEHOLDER,))
    assert _retitles(port) == [("store#5", "fix the export encoding")]


def test_a_closed_card_keeps_the_title_it_was_filed_with(tmp_path):
    """Renaming a finished thread helps nobody, and cleanup is what that wake is for."""
    initial = {"issues": {"store#5": {"status": "In progress"}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(open=False, title=FILED)})
    run_wake(store_for(tmp_path, initial), port, now=_NOW, placeholder_titles=(PLACEHOLDER,))
    assert _retitles(port) == []
    assert port.ownership_edits() == []  # the label stays on (#2497)


def test_a_refused_retitle_is_recorded_and_never_stalls_the_card(tmp_path):
    initial = {"issues": {"store#5": {"status": "Todo", "manual_start": True}}}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(title=FILED)}, refuse=(EffectKind.SET_TITLE,)
    )
    outcome = run_wake(
        store_for(tmp_path, initial), port, now=_NOW, placeholder_titles=(PLACEHOLDER,)
    )
    assert outcome.report.errors  # visible, and retried on the next wake
    assert len(outcome.directives) == 1  # the work itself went out regardless


def test_a_card_resting_in_done_is_released_rather_than_started_again(tmp_path):
    """The board said Done and the poller dispatched anyway — three times, on one card (#1581)."""
    initial = {
        "issues": {
            "store#5": {
                "status": "Done",
                "manual_start": True,
                "last_live_check": _NOW.isoformat(),
            }
        }
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="Done")})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert "store#5" not in store.read()["issues"]


def test_a_done_card_the_wake_could_not_re_read_is_still_not_dispatched(tmp_path):
    """The backstop: whatever went wrong upstream, Done is not a column work starts from."""
    initial = {
        "issues": {"store#5": {"status": "Done", "manual_start": True}}
    }
    port = FakePort(snapshots={})  # unreadable, so nothing releases the card this wake
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    # Nothing was consumed either, so the wake that does read the card sees the same start.
    assert store.read()["issues"]["store#5"]["manual_start"] is True


# --- reconciliation ---

def test_a_closed_card_is_cleaned_up_and_forgotten(tmp_path):
    initial = {"issues": {"store#5": {"status": "Todo", "worktrees": ["/w/issue-5"]}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    assert "store#5" not in store.read()["issues"]
    assert port.ownership_edits() == []  # the label stays on (#2497)
    assert EffectKind.REMOVE_WORKTREES in port.kinds()
    assert outcome.report.releases == 1


def test_a_closed_card_does_not_remove_a_live_workers_worktree(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen-1",
                "mode": "merge-review",
                "worktrees": ["/w/issue-5"],
            }
        }
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(open=False, status="In progress")}
    )
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["generation"] == "gen-1"
    assert rec["mode"] == "merge-review"
    assert rec["worktrees"] == ["/w/issue-5"]
    assert EffectKind.REMOVE_WORKTREES not in port.kinds()


def test_a_card_held_for_a_missing_worker_report_says_so_in_the_log(tmp_path):
    # #1091: the hold spends a worker slot while the wake otherwise prints "no directives",
    # so the engine reads as idle when it is one worker short. Name the card.
    initial = {"issues": {"store#5": {"status": "In progress", "generation": "gen-1"}}}
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In review", quiet_seconds=60)})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["status"] == "In progress"
    assert any("holding a worker slot" in line and "store#5" in line for line in outcome.log)


def test_a_closed_card_whose_worker_never_reported_is_released_once_its_run_ended(tmp_path):
    # #1091: the Trailhead card that started this — closed for days, board Done, no marker,
    # no dispatch. Held at await-terminal it kept a worker slot with no way out. The way out is
    # now the Runtime's report that the Run is over (#2159).
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "started": True,
                "generation": "gen-1",
                "worktrees": ["/w/issue-5"],
            }
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(
                open=False, status="Done", quiet_seconds=60, run_ended=True
            )
        }
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert "store#5" not in store.read()["issues"]
    assert EffectKind.REMOVE_WORKTREES in port.kinds()
    assert port.ownership_edits() == []  # the label stays on (#2497)
    assert any("the Runtime says its Run ended" in line for line in outcome.log)


def test_an_open_card_whose_worker_never_reported_ends_once_the_runtime_says_so(tmp_path):
    initial = {"issues": {"store#5": {"status": "In progress", "generation": "gen-1"}}}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In review", run_ended=True)}
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["status"] == "In review"
    assert any("the Runtime says its Run ended" in line for line in outcome.log)


def test_a_refused_worktree_removal_keeps_the_card_for_retry(tmp_path):
    initial = {"issues": {"store#5": {"status": "In progress", "worktrees": ["/w/issue-5"]}}}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(open=False)},
        refuse={EffectKind.REMOVE_WORKTREES},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec["forget_pending"] is True
    assert rec["worktrees"] == ["/w/issue-5"]

    # Next wake retries the cleanup and only then forgets the card.
    port.refuse.clear()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert "store#5" not in store.read()["issues"]


def test_partial_cleanup_forgets_successes_and_retries_only_failed_paths(tmp_path):
    class PartialCleanupPort(FakePort):
        failed = ("/repo-b/.worktrees/issue-5",)

        def apply(self, effect):
            if effect.kind is EffectKind.REMOVE_WORKTREES:
                self.applied.append(effect)
                return WorktreeCleanup(self.failed)
            return super().apply(effect)

    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "worktrees": [
                    "/repo-a/.worktrees/issue-5",
                    "/repo-b/.worktrees/issue-5",
                ],
            }
        }
    }
    port = PartialCleanupPort(snapshots={"store#5": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)
    assert store.read()["issues"]["store#5"]["worktrees"] == list(port.failed)

    port.failed = ()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert "store#5" not in store.read()["issues"]
    removals = [e.value for e in port.applied if e.kind is EffectKind.REMOVE_WORKTREES]
    assert removals[-1] == ["/repo-b/.worktrees/issue-5"]


def test_a_worktree_the_host_kept_releases_the_card_and_says_so(tmp_path):
    """#1718: cleanup that would destroy unfinished work keeps the directory instead.

    That is not a refusal, so it must not hold the card: a path reported pending is retried
    on every wake forever (#1189). The card is released and forgotten, and the release
    comment is the only thing that will ever mention the directory it left behind.
    """
    class KeepingPort(FakePort):
        def apply(self, effect):
            if effect.kind is EffectKind.REMOVE_WORKTREES:
                self.applied.append(effect)
                return WorktreeCleanup(
                    (), (("/repo/.worktrees/issue-5", "3 uncommitted file(s)"),)
                )
            return super().apply(effect)

    initial = {"issues": {"store#5": {"status": "In progress",
                                      "worktrees": ["/repo/.worktrees/issue-5"]}}}
    port = KeepingPort(snapshots={"store#5": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert "store#5" not in store.read()["issues"], "a deliberate keep is not retryable cleanup"
    assert outcome.report.releases == 1
    assert not outcome.report.errors, "keeping a worktree is not a failed effect"
    comments = [
        str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT
    ]
    assert any(
        "Kept this card's worktree" in body and "3 uncommitted file(s)" in body
        for body in comments
    ), comments
    assert any("kept /repo/.worktrees/issue-5" in line for line in outcome.log)


def test_an_unclaimed_leftover_is_named_as_that_not_as_uncommitted_work(tmp_path):
    """#2195 review: a clean directory no Work claims is kept for a different reason.

    The release comment must say which, not claim every kept tree holds uncommitted work.
    """
    reason = "no Work claims it, so it was left on disk for `reap-worktrees`"

    class KeepingPort(FakePort):
        def apply(self, effect):
            if effect.kind is EffectKind.REMOVE_WORKTREES:
                self.applied.append(effect)
                return WorktreeCleanup((), (("/repo/.worktrees/issue-5", reason),))
            return super().apply(effect)

    initial = {"issues": {"store#5": {"status": "In progress",
                                      "worktrees": ["/repo/.worktrees/issue-5"]}}}
    port = KeepingPort(snapshots={"store#5": CardSnapshot(open=False)})

    run_wake(store_for(tmp_path, initial), port, now=_NOW)

    (body,) = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert reason in body
    assert "uncommitted" not in body and "not committed" not in body


def test_a_card_saved_owing_a_label_removal_is_released_without_one(tmp_path):
    """Before #2497 a refused removal left `cleanup_label` on the record for a retry wake. The
    engine never removes the label now, so such a record finishes its release on the next wake
    and the label stays where the person put it."""
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "forget_pending": True, "cleanup_label": True}
        }
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    assert "store#5" not in store.read()["issues"]
    assert port.ownership_edits() == []
    assert EffectKind.DISCARD_RECEIPTS in port.kinds()
    assert outcome.report.releases == 1


def test_an_unreadable_card_is_skipped_without_stalling_the_wake(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress"},
            "store#6": {"status": "In progress"},
        }
    }
    port = FakePort(snapshots={"store#6": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.report.scan_failures == 1
    assert "store#5" in store.read()["issues"]   # skipped, untouched
    assert "store#6" not in store.read()["issues"]  # the other card still progressed


def test_a_quiet_card_is_neither_stopped_nor_reloaded_on_github_silence(tmp_path):
    """#2159 test 10: GitHub quiet age alone never declares a worker dead. The retired cold
    reload stopped and restarted cards on exactly this; now only the Runtime ends a Run."""
    stale_start = (_NOW - timedelta(hours=3)).isoformat()
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "started_at": stale_start,
                "kind": "issue",
                "generation": "gen-5",
                "worktrees": ["/repo/.worktrees/issue-5"],
            }
        }
    }
    quiet = reconcile.STALE_SECONDS * 10
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=quiet)}
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    rec = store.read()["issues"]["store#5"]
    assert "stopping" not in rec
    assert rec["generation"] == "gen-5"
    assert outcome.report.revives == 0


def test_a_revived_pr_card_refreshes_before_its_continuation(tmp_path):
    """A reportless ending on a PR card resumes in PR mode, after the branch is refreshed."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(observed="died", subtype=""),
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
    )
    store = store_for(tmp_path, _wedged({"kind": "pr", "started": True}))

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert port.updated_branches == ["store#7"]

    port.linked["store#5"] = LinkedPRs(open={"store#7": "bbb"})
    port._pr_states["store#7"] = PrState(ref="store#7", head="bbb", merge_state="CLEAN")
    resumed = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert [directive["action"] for directive in resumed.directives] == ["pr"]
    assert resumed.directives[0]["continuation"] is True


def test_stale_recovery_never_reuses_the_worktree_on_timeout_alone(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "restart_after_stop": True,
                "stop_waiting_generation": "gen-5",
                "stop_requested_at": (_NOW - timedelta(hours=3)).isoformat(),
                "generation": "replacement-lease",
                "worktrees": ["/repo/.worktrees/issue-5"],
            }
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", quiet_seconds=reconcile.STALE_SECONDS + 60
            )
        }
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    rec = store.read()["issues"]["store#5"]
    assert rec["stopping"] is True
    assert rec["worktrees"] == ["/repo/.worktrees/issue-5"]


def test_a_stop_the_runtime_has_not_proved_names_itself_and_what_it_holds(tmp_path):
    # #2073: the minecraft instance sat like this for two and a half hours. Every wake logged
    # the cards queued behind the cap and none of them logged the card holding it, because
    # STOP_WAITING was the one slot-holding decision with nothing to say. The wait is correct
    # — a timeout is not proof a provider stopped writing the worktree (#2159) — so what is
    # asserted here is that it is legible, not that it ends.
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "restart_after_stop": True,
                "stop_waiting_generation": "gen-5",
                "stop_requested_at": (_NOW - timedelta(hours=3)).isoformat(),
                "generation": "gen-5",
                "worktrees": ["/repo/.worktrees/issue-5"],
            }
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", quiet_seconds=reconcile.STALE_SECONDS + 60
            )
        }
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    line = next(line for line in outcome.log if "has been stopping" in line)
    assert "store#5" in line
    assert f"stopping {3 * 3600}s" in line
    assert "holding a worker slot" in line
    assert "waiting for the Runtime to stop Run gen-5" in line
    assert "stop-ack" not in line
    assert health.HealthReason.STOP_STALLED in outcome.verdict.reasons
    assert outcome.verdict.state is health.HealthState.DEGRADED
    # Published for an observer, and named rather than counted: which card, not how many.
    assert store.read()["health"]["last_report"]["stalled_stops"] == ["store#5"]


def test_a_stop_still_inside_its_window_is_logged_but_is_not_called_stalled(tmp_path):
    # An ordinary stop waiting its first minutes is normal operation. It still says it holds a
    # slot — that is #1091's rule, not a judgement about this stop — but it names the deadline
    # it actually has and must not degrade the instance's health.
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "stop_waiting_generation": "gen-5",
                "stop_requested_at": (_NOW - timedelta(minutes=4)).isoformat(),
                "generation": "gen-5",
            }
        }
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=240)}
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    line = next(line for line in outcome.log if "has been stopping" in line)
    assert f"stopping {4 * 60}s" in line
    assert "waiting for the Runtime to stop Run gen-5" in line
    assert health.HealthReason.STOP_STALLED not in outcome.verdict.reasons
    assert store.read()["health"]["last_report"]["stalled_stops"] == []


def test_a_stalled_stop_survives_the_quiet_wakes_between_two_full_ones(tmp_path):
    # #2073 review: a webhook-quiet wake defers the live card read entirely, and a wedged stop is
    # exactly the thing that keeps an instance quiet. The reason survives it for free now that it
    # is read from the card rather than tallied per wake — the quiet wake changed no record, so it
    # has nothing to say and says nothing. The Runtime's proof in the third wake is what ends
    # it, because that is what lets reconciliation clear the card.
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "stop_waiting_generation": "gen-5",
                "stop_requested_at": (_NOW - timedelta(hours=3)).isoformat(),
                "generation": "gen-5",
            }
        }
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=10)}
    )
    store = store_for(tmp_path, initial)

    seen = run_wake(store, port, now=_NOW)
    assert health.HealthReason.STOP_STALLED in seen.verdict.reasons

    port.graphql_deferred = True
    quiet = run_wake(store, port, now=_NOW + timedelta(minutes=5))
    # The quiet wake read nothing and so changed nothing — which is exactly why it has no
    # opinion to overwrite the stall with.
    assert store.read()["issues"]["store#5"]["stopping"] is True
    assert quiet.verdict.state is HealthState.DEGRADED
    assert health.HealthReason.STOP_STALLED in quiet.verdict.reasons

    port.graphql_deferred = False
    port.snapshots["store#5"] = CardSnapshot(
        status="In progress", quiet_seconds=10, execution_stopped=True
    )
    ended = run_wake(store, port, now=_NOW + timedelta(minutes=10))
    # Released, and then released all the way: no card left with an unresolved stop, so there is
    # nothing for the reason to be derived from.
    assert not store.read()["issues"].get("store#5", {}).get("stopping")
    assert health.HealthReason.STOP_STALLED not in ended.verdict.reasons


def test_a_stall_proven_this_wake_survives_an_unreadable_card_beside_it(tmp_path):
    # #2073 review, second round: one stopping card reads fine and is stalled, another is
    # unreadable, so the wake cannot speak for every stop. A wake-level tally had to be repaired
    # for this; reading the cards instead makes it fall out — store#5's record is late whether or
    # not store#6 could be read, and store#6's is not late whether or not it could.
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "stop_waiting_generation": "gen-5",
                "stop_requested_at": (_NOW - timedelta(hours=3)).isoformat(),
                "generation": "gen-5",
            },
            "store#6": {
                "status": "In progress",
                "stopping": True,
                "stop_waiting_generation": "gen-6",
                "stop_requested_at": (_NOW - timedelta(minutes=2)).isoformat(),
                "generation": "gen-6",
            },
        }
    }
    # store#6 is absent from the port's snapshots, which is how a card comes back unreadable.
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=10)}
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert any("not readable this wake" in line for line in outcome.log)
    assert outcome.verdict.state is HealthState.DEGRADED
    assert health.HealthReason.STOP_STALLED in outcome.verdict.reasons
    assert store.read()["health"]["last_report"]["stalled_stops"] == ["store#5"]


def test_an_explicit_stop_overrides_a_revive_in_the_same_wake(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen-5",
                "processing": ["9"],
            }
        },
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        comments=[comment(10, "/claude stop")],
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", quiet_seconds=reconcile.STALE_SECONDS + 60
            )
        },
        turn_ends=_killed(),
    )
    store = store_for(tmp_path, initial)

    first = run_wake(store, port, now=_NOW)

    assert first.directives == []
    rec = store.read()["issues"]["store#5"]
    assert rec["stopping"] is True
    assert "release_label" not in rec
    assert rec["stop_control"] == "10"
    assert "restart_after_stop" not in rec
    assert first.report.revives == 0
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "Restarting this card's worker" not in said

    port.comments = []
    port.snapshots["store#5"] = CardSnapshot(
        status="In progress", execution_stopped=True, quiet_seconds=60
    )
    second = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert second.directives == []
    assert "store#5" not in store.read()["issues"]


def test_a_stop_command_invalidates_the_lease_and_drops_the_dispatch(tmp_path):
    sent = (_NOW - timedelta(seconds=10)).isoformat()
    initial = _dispatched(sent)
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec["stopping"] is True
    assert rec["stop_waiting_generation"] == "gen1"
    assert rec["generation"] != "gen1"
    assert "dispatch" not in rec
    assert outcome.directives == []


def test_a_stop_the_runtime_proved_releases_the_card(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "release_label": True,
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
            }
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="In progress", execution_stopped=True, quiet_seconds=60)
        }
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    assert "store#5" not in store.read()["issues"]
    assert port.ownership_edits() == []  # the label stays on (#2497)
    assert outcome.report.releases == 1


def test_an_instruction_queued_during_a_stop_restarts_the_card(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "release_label": True,
                "restart_after_stop": True,
                "pending": ["9"],
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
            }
        },
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "and also this", "status": "queued", "created_at": "x"}},
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="In progress", execution_stopped=True, quiet_seconds=60)
        }
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    # The restart claims the card in state; the issue's labels are left exactly as the stop
    # left them (#2053).
    assert EffectKind.ADD_OWNERSHIP not in port.kinds()
    assert store.read()["issues"]["store#5"]["engine_claim"] is True
    assert len(outcome.directives) == 1
    assert payloads(outcome.directives[0]) == ["and also this"]


# --- ownership provenance ---

def test_a_hand_added_label_starts_a_card_only_once_adopted(tmp_path):
    initial = {"issues": {"store#5": {"status": "Todo"}}}
    port = FakePort(
        labelled=["store#5"],
        snapshots={"store#5": CardSnapshot(status="Todo")},
        label_events={"store#5": ["e1"]},
    )
    store = store_for(tmp_path, initial)
    # First wake adopts the existing world without treating a standing label as a start.
    first = run_wake(store, port, now=_NOW)
    assert first.directives == []

    # A remove + re-add produces a new labeled event, which is a genuine start.
    port._label_events["store#5"] = ["e1", "e2"]
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert len(second.directives) == 1
    assert payloads(second.directives[0]) == []


# --- release admission ---

def _in_review(extra=None):
    rec = {"status": "In review"}
    rec.update(extra or {})
    return {"issues": {"store#5": rec}}


def _merge_comment(cid=3, body="/merge"):
    return comment(cid, body)


def test_a_merge_command_on_a_ready_card_is_admitted(tmp_path):
    port = FakePort(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
    )
    store = store_for(tmp_path, _in_review())
    outcome = run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["mode"] == "merge"
    assert rec["status"] == "In progress"
    assert "merge_start_pending" not in rec
    assert state["controls"]["3"]["status"] == "processing"
    # A merge is poll-driven: no worker is spawned for it.
    assert outcome.directives == []


def test_a_merge_review_snapshots_the_heads_and_spawns_the_reviewer(tmp_path):
    port = FakePort(
        comments=[_merge_comment(body="/merge review")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={
            "store#5": LinkedPRs(open={"store#7": "aaa"})
        },
        review_diffs={"store#7": "diff-1"},
        pr_states={"store#7": PrState(ref="store#7", head="aaa")},
    )
    store = store_for(tmp_path, _in_review())
    outcome = run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec["mode"] == "merge-review"
    assert rec["review_heads"] == {"store#7": "aaa"}
    assert rec["review_diffs"] == {"store#7": "diff-1"}
    assert "review_trees" not in rec
    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "merge-review"


def test_a_merge_review_directive_names_every_pull_request_it_would_release(tmp_path):
    """#1338: the reviewer reviewed the one PR on its branch while a pass shipped three."""
    port = FakePort(
        comments=[_merge_comment(body="/merge review")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={
            "store#5": LinkedPRs(
                open={"store#7": "aaa", "docker#80": "bbb", "abacus#1810": "ccc"}
            )
        },
        review_diffs={"store#7": "d1", "docker#80": "d2", "abacus#1810": "d3"},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa"),
            "docker#80": PrState(ref="docker#80", head="bbb"),
            "abacus#1810": PrState(ref="abacus#1810", head="ccc"),
        },
    )
    store = store_for(tmp_path, _in_review())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives[0]["pull_requests"] == ["abacus#1810", "docker#80", "store#7"]
    assert store.read()["issues"]["store#5"]["review_prs"] == [
        "abacus#1810", "docker#80", "store#7"
    ]


def test_a_review_whose_pull_requests_cannot_be_read_names_none_of_them(tmp_path):
    """A stale set would send the reviewer at pull requests the engine can no longer confirm."""
    class Blind(FakePort):
        def linked_prs(self, repo, number):
            return None

    initial = _in_review()
    initial["issues"]["store#5"].update(
        mode="merge-review", review_prs=["store#7"], merge_control="3", started=False,
    )
    port = Blind(snapshots={"store#5": CardSnapshot(status="In review")})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    assert "review_prs" not in store.read()["issues"]["store#5"]


def test_a_merge_review_refreshes_a_behind_branch_before_dispatch(tmp_path):
    port = FakePort(
        comments=[_merge_comment(body="/merge review")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        review_diffs={"store#7": "diff-1"},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
    )
    store = store_for(tmp_path, _in_review())

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert port.updated_branches == ["store#7"]
    assert store.read()["issues"]["store#5"]["mode"] == "merge-review"

    port.linked["store#5"] = LinkedPRs(open={"store#7": "bbb"})
    port._pr_states["store#7"] = PrState(ref="store#7", head="bbb", merge_state="CLEAN")
    dispatched = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert [directive["action"] for directive in dispatched.directives] == ["merge-review"]


def test_a_conflicting_merge_review_is_dispatched_for_the_reviewer_to_resolve(tmp_path):
    port = FakePort(
        comments=[_merge_comment(body="/merge review")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        review_diffs={"store#7": "diff-1"},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
        refuse={EffectKind.UPDATE_BRANCH},
    )

    outcome = run_wake(store_for(tmp_path, _in_review()), port, now=_NOW)

    assert [directive["action"] for directive in outcome.directives] == ["merge-review"]
    assert any("worker will resolve it" in line for line in outcome.log)


def test_a_new_merge_review_does_not_consume_the_previous_reviewers_verdict(tmp_path):
    port = FakePort(
        comments=[_merge_comment(cid=4, body="/merge review")],
        snapshots={
            "store#5": CardSnapshot(
                status="In review",
                review_verdict="pass",
            )
        },
        linked={"store#5": LinkedPRs(open={"store#7": "bbb"})},
        release_trees={"store#7": "tree-2"},
        pr_states={"store#7": PrState(ref="store#7", head="bbb")},
    )
    store = store_for(
        tmp_path,
        _in_review({"generation": "previous-review", "started": True}),
    )

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["mode"] == "merge-review"
    assert rec["review_heads"] == {"store#7": "bbb"}
    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "merge-review"
    assert outcome.directives[0]["generation"] != "previous-review"


def test_a_merge_on_a_busy_card_is_refused_with_a_reason(tmp_path):
    port = FakePort(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
    )
    store = store_for(tmp_path, _in_review({"pending": ["8"]}))
    outcome = run_wake(store, port, now=_NOW)
    control = store.read()["controls"]["3"]
    assert control["status"] == "rejected"
    assert control["rejection"] == "reject-busy"
    assert EffectKind.REACT in port.kinds()
    refusal = next(effect for effect in outcome.effects if effect.kind is EffectKind.COMMENT)
    assert isinstance(refusal.value, CommentBody)
    assert "active or queued work" in refusal.value.body
    assert "run `/merge` again" in refusal.value.body


@pytest.mark.parametrize(
    ("snapshot", "linked", "body", "rejection", "detail"),
    [
        (CardSnapshot(open=False, status="In review"), LinkedPRs(open={"store#7": "aaa"}),
         "/merge", "reject-closed", "this card is closed"),
        (CardSnapshot(owned=False, status="In review"), LinkedPRs(open={"store#7": "aaa"}),
         "/merge", "reject-unowned", "poller no longer owns"),
        (CardSnapshot(status="Todo"), LinkedPRs(open={"store#7": "aaa"}),
         "/merge", "reject-status", "not in In review"),
        (CardSnapshot(status="In review"), LinkedPRs(),
         "/merge", "reject-no-linked-pr", "could not find a pull request linked"),
        (CardSnapshot(status="In review"), LinkedPRs(merged=["store#7"]),
         "/merge review", "reject-nothing-to-review", "open linked pull request to review"),
    ],
)
def test_a_final_release_refusal_explains_the_action(
    tmp_path, snapshot, linked, body, rejection, detail
):
    port = FakePort(
        comments=[_merge_comment(body=body)],
        snapshots={"store#5": snapshot},
        linked={"store#5": linked},
    )
    store = store_for(tmp_path, _in_review())

    outcome = run_wake(store, port, now=_NOW)

    control = store.read()["controls"]["3"]
    assert control["status"] == "rejected"
    assert control["rejection"] == rejection
    assert EffectKind.REACT in port.kinds()
    refusal = next(effect for effect in outcome.effects if effect.kind is EffectKind.COMMENT)
    assert detail in str(refusal.value)


def test_a_failed_release_refusal_comment_is_retried_before_rejecting(tmp_path):
    port = FakePort(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
        refuse={EffectKind.COMMENT},
    )
    store = store_for(tmp_path, _in_review())

    first = run_wake(store, port, now=_NOW)

    assert store.read()["controls"]["3"]["status"] == "new"
    assert "release refusal comment pending" in " ".join(first.log)

    port.refuse.clear()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    control = store.read()["controls"]["3"]
    assert control["status"] == "rejected"
    assert control["rejection"] == "reject-no-linked-pr"
    assert any(effect.kind is EffectKind.COMMENT for effect in port.applied)


def test_no_linked_pr_guidance_leads_with_the_cross_repository_safe_reference():
    message = _release_refusal_message(
        release.ReleaseAdmission.REJECT_NO_LINKED_PR,
        repo="issues",
        number=2898,
        review_first=False,
    )

    assert "`issues#2898` reference" in message
    assert "For a PR in this same repository" in message
    # #1734: the advice no longer names a closing keyword. It still links a same-repo pull
    # request, but recommending one is how AgentDeck manufactured a policy conflict for
    # repositories whose own guide bans it — and the qualified reference works everywhere.
    assert "Closes" not in message
    assert "needs no closing keyword" in message
    assert "`agent/issue-2898` branch with a bare `#2898` reference" in message


def test_incomplete_pr_discovery_defers_the_release_instead_of_refusing_it(tmp_path):
    class Blind(FakePort):
        def linked_prs(self, repo, number):
            return None

    port = Blind(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
    )
    store = store_for(tmp_path, _in_review())
    run_wake(store, port, now=_NOW)
    # Still 'new': the command waits for a wake that can see the PRs.
    assert store.read()["controls"]["3"]["status"] == "new"


def test_a_review_without_readable_heads_is_deferred(tmp_path):
    port = FakePort(
        comments=[_merge_comment(body="/merge review")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": None})},
    )
    store = store_for(tmp_path, _in_review())
    outcome = run_wake(store, port, now=_NOW)
    assert store.read()["controls"]["3"]["status"] == "new"
    assert "mode" not in store.read()["issues"]["store#5"]
    assert outcome.directives == []


# --- health ---

def test_every_wake_reports_queue_depth_and_worker_count(tmp_path):
    port = FakePort(comments=[comment(1, "/claude go")])
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.verdict.state is HealthState.OK
    current = health.record(store.read())
    assert current["cycles"] == 1
    assert current["last_report"]["active_workers"] == 1
    assert current["totals"]["commands_ingested"] == 1
    assert current["totals"]["dispatch_attempts"] == 1


# --- parity behaviors recovered from the reference suites (#176 slice 11) ---

def _instruction(cid="9", *, number=5, payload="go", status="queued"):
    return {
        "id": cid, "repo": "store", "number": number, "kind": "claude",
        "payload": payload, "url": f"https://example/{cid}", "author": "eero",
        "status": status, "created_at": f"t{cid}",
    }


def test_a_batched_directive_carries_the_command_records_not_bare_text(tmp_path):
    port = FakePort(comments=[comment(1, "/claude fix the header")])
    outcome = run_wake(store_for(tmp_path), port, now=_NOW)
    message = outcome.directives[0]["messages"][0]
    # A worker answers the requester, so it needs who asked and where.
    assert message["payload"] == "fix the header"
    assert message["author"] == "eero"
    assert message["url"].endswith("issuecomment-1")


def test_the_lease_is_durable_before_the_board_transition(tmp_path):
    port = FakePort(comments=[comment(1, "/claude go")], refuse={EffectKind.SET_STATUS})
    store = store_for(tmp_path)
    outcome = run_wake(store, port, now=_NOW)
    # The status write was refused, so no directive was handed out...
    assert outcome.directives == []
    # ...but the lease and spawn intent are already durable, so the next wake settles it
    # instead of finding a board transition with no record of why.
    rec = store.read()["issues"]["store#5"]
    assert rec["dispatch"]["sent_at"] is None
    assert rec["generation"]


def test_a_stop_on_an_idle_card_releases_it_instead_of_being_refused(tmp_path):
    initial = {"issues": {"store#5": {"status": "In review", "started": True}}}
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In review")},
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    state = store.read()
    assert "store#5" not in state["issues"]
    # The release completed inside the same wake, so the command is already done.
    assert state["controls"]["2"]["status"] == "done"
    assert port.ownership_edits() == []  # the label stays on (#2497)
    assert outcome.report.releases == 1


def test_a_stop_cancels_the_queued_commands_but_not_the_running_one(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "pending": ["9"], "processing": ["8"],
                        "generation": "gen1"}
        },
        "controls": {"8": _instruction("8", status="processing"), "9": _instruction("9")},
    }
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["stopping"] is True
    assert rec["pending"] == []
    assert rec["processing"] == ["8"]
    assert state["controls"]["9"]["status"] == "cancelled"
    assert state["controls"]["8"]["status"] == "processing"


def test_a_stop_while_already_stopping_is_refused(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "stopping": True, "generation": "gen2",
                        "stop_waiting_generation": "gen1",
                        "stop_requested_at": _NOW.isoformat()}
        }
    }
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=5)},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    control = store.read()["controls"]["2"]
    assert control["status"] == "rejected"
    assert control["rejection"] == "already-stopping"


def test_a_stop_cancels_the_restart_a_re_added_label_queued_behind_a_stop(tmp_path):
    """Re-adding the label mid-run stops the Run in order to restart it. A person's
    `/claude stop` then has to win over that restart, not be refused as a duplicate (#2448)."""
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "stopping": True, "generation": "gen2",
                        "stop_waiting_generation": "gen1", "release_label": False,
                        "stop_requested_at": _NOW.isoformat(), "pending": ["9"],
                        "restart_after_stop": True, "manual_restart_after_stop": True}
        },
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=5)},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert state["controls"]["2"]["status"] == "processing"
    assert rec["stop_control"] == "2"
    assert rec["stopping"] is True
    assert "restart_after_stop" not in rec
    assert "manual_restart_after_stop" not in rec
    assert rec["pending"] == []
    assert state["controls"]["9"]["status"] == "cancelled"


def test_a_stop_that_raced_the_runs_own_ending_leaves_reviewed_work_mergeable(tmp_path):
    """Northwind/issues#397: the `/claude stop` was written while a Run started by a
    label toggle was going, and that Run ended In review before the wake read the stop.
    Handing the idle card back then dropped its label and moved it to Todo, so `/merge` had
    nothing left to act on (#2448)."""
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "stopping": True, "generation": "gen2",
                        "stop_waiting_generation": "gen1", "release_label": False,
                        "stop_requested_at": _NOW.isoformat(),
                        "restart_after_stop": True, "manual_restart_after_stop": True}
        },
    }
    # The stop was written at 11:59:00; the Run posted its ending half a minute later, and one
    # wake at 12:00 reads both.
    port = FakePort(
        comments=[comment(2, "/claude stop", created="2026-07-25T11:59:00Z")],
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", stopped_terminal_marker="In review", quiet_seconds=60,
                stopped_terminal_at="2026-07-25T11:59:30Z",
            )
        },
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["status"] == "In review"
    assert rec["run_ended_at"] == "2026-07-25T11:59:30Z"
    assert state["controls"]["2"]["status"] == "done"
    assert port.ownership_edits() == []
    assert [e.value for e in port.applied if e.kind is EffectKind.SET_STATUS] == ["In review"]
    assert not release.is_busy(rec)


@pytest.mark.parametrize("ended", ["2026-07-25T11:58:00Z", "2026-07-25T11:59:00Z", ""])
def test_a_stop_posted_after_the_run_ended_hands_the_card_back_even_read_in_one_wake(
    tmp_path, ended
):
    """The inverse race: the Run ends, *then* the stop is posted, and a single later wake
    reads both. The stop was aimed at an idle card, so it hands it back as it always did. The
    Run's end is its terminal comment's own stamp, not the wake that read it — which would be
    12:00 and would swallow this stop (#2448). A tie, or no readable stamp, hands back too."""
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "stopping": True, "generation": "gen2",
                        "stop_waiting_generation": "gen1", "release_label": False,
                        "stop_requested_at": _NOW.isoformat(),
                        "restart_after_stop": True, "manual_restart_after_stop": True}
        },
    }
    port = FakePort(
        comments=[comment(2, "/claude stop", created="2026-07-25T11:59:00Z")],
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", stopped_terminal_marker="In review", quiet_seconds=60,
                stopped_terminal_at=ended,
            )
        },
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    assert "store#5" not in state["issues"]
    assert state["controls"]["2"]["status"] == "done"
    assert port.ownership_edits() == []  # the label stays on (#2497)


def test_a_stop_written_after_the_run_ended_still_hands_the_card_back(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In review", "started": True,
                        "run_ended_at": (_NOW - timedelta(minutes=10)).isoformat()}
        }
    }
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In review")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    assert "store#5" not in state["issues"]
    assert state["controls"]["2"]["status"] == "done"
    assert port.ownership_edits() == []  # the label stays on (#2497)


def test_a_stop_cannot_take_back_a_release_in_flight(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "mode": "merge",
                        "awaiting_merge": {"prs": ["store#7"], "premerged": {},
                                           "since": _NOW.isoformat()}}
        }
    }
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    assert store.read()["controls"]["2"]["rejection"] == "release-in-flight"


def test_a_worker_that_ended_resolves_the_commands_it_carried(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "In progress", "processing": ["9"], "started": True}},
        "controls": {"9": _instruction("9", status="processing")},
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In review", terminal_marker="In review")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    assert state["controls"]["9"]["status"] == "done"
    assert state["issues"]["store#5"]["processing"] == []


def _done_reactions(port, cid):
    """Every "+1" this port was asked to put on one comment."""
    return [
        effect
        for effect in port.applied
        if effect.kind is EffectKind.REACT
        and effect.value["comment"] == cid
        and effect.value["reaction"] == "+1"
    ]


def test_a_recovered_terminal_status_resolves_the_commands_it_carried(tmp_path):
    """#286: a card that ends through recovery ends the same way one that ends normally does.

    The worker reported, its own status write never landed, and the engine published it —
    the commands it was carrying are just as finished as under ``WORKER_ENDED``.
    """
    initial = {
        "issues": {"store#5": {"status": "In progress", "processing": ["9"], "started": True}},
        "controls": {"9": _instruction("9", status="processing")},
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", terminal_marker="In review")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    # The status write itself is unchanged.
    assert rec["status"] == "In review"
    assert [
        e.value for e in port.applied if e.kind is EffectKind.SET_STATUS
    ] == ["In review"]
    assert rec["processing"] == []
    assert state["controls"]["9"]["status"] == "done"
    assert len(_done_reactions(port, "9")) == 1


def test_a_recovered_terminal_resolves_nothing_until_its_status_write_lands(tmp_path):
    """The ending is one act: a refused status write retries whole, and never resolves twice."""
    initial = {
        "issues": {"store#5": {"status": "In progress", "processing": ["9"], "started": True}},
        "controls": {"9": _instruction("9", status="processing")},
    }
    snapshot = {"store#5": CardSnapshot(status="In progress", terminal_marker="In review")}
    store = store_for(tmp_path, initial)

    refusing = FakePort(snapshots=snapshot, refuse={EffectKind.SET_STATUS})
    run_wake(store, refusing, now=_NOW)
    held = store.read()
    assert held["issues"]["store#5"]["processing"] == ["9"]
    assert held["controls"]["9"]["status"] == "processing"
    assert _done_reactions(refusing, "9") == []

    port = FakePort(snapshots=snapshot, scanned_to=_NOW + timedelta(seconds=30))
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    later = FakePort(snapshots=snapshot, scanned_to=_NOW + timedelta(seconds=60))
    run_wake(store, later, now=_NOW + timedelta(seconds=60))
    state = store.read()
    assert state["issues"]["store#5"]["processing"] == []
    assert state["controls"]["9"]["status"] == "done"
    assert len(_done_reactions(port, "9") + _done_reactions(later, "9")) == 1


def test_an_instruction_after_a_recovered_terminal_does_not_queue_behind_it(tmp_path):
    """The finished command must not survive as work: it would be re-run, not merely linger.

    A control still recorded as ``processing`` is reattached to its card by every later
    state load (``ledger.recover``), which puts work the worker already did back on the
    queue behind the new instruction.
    """
    initial = {
        "issues": {"store#5": {"status": "In progress", "processing": ["9"], "started": True}},
        "controls": {"9": _instruction("9", status="processing")},
    }
    store = store_for(tmp_path, initial)
    run_wake(
        store,
        FakePort(
            snapshots={"store#5": CardSnapshot(status="In progress", terminal_marker="In review")}
        ),
        now=_NOW,
    )

    later = _NOW + timedelta(seconds=30)
    port = FakePort(
        comments=[comment(10, "/claude and now this", created="2026-07-25T11:59:50Z")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        scanned_to=later,
    )
    outcome = run_wake(store, port, now=later)
    assert [payloads(directive) for directive in outcome.directives] == [["and now this"]]
    rec = store.read()["issues"]["store#5"]
    assert rec["processing"] == ["10"]
    assert rec.get("pending") == []
    assert "recovery_action" not in rec


def test_a_newer_instruction_cancels_a_release_that_has_not_started(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "mode": "merge", "merge_control": "3",
                        "review_heads": {"store#7": "aaa"}, "started": True}
        },
        "controls": {"3": {"id": "3", "repo": "store", "number": 5, "kind": "merge",
                           "status": "processing", "created_at": "t1"}},
    }
    port = FakePort(
        comments=[comment(9, "/claude change this instead")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    assert "mode" not in rec and "review_heads" not in rec
    assert state["controls"]["3"]["status"] == "cancelled"
    assert state["controls"]["9"]["status"] in {"queued", "processing"}


def _releasing_card():
    return {
        "issues": {
            "store#5": {
                "status": "In progress", "mode": "merge", "merge_control": "3",
                "merged": ["store#7"], "started": True,
                "awaiting_deploy": {"builds": {"store": {"sha": "sha1", "status": "pending"}},
                                    "since": _NOW.isoformat()},
            }
        },
        "controls": {"3": {"id": "3", "repo": "store", "number": 5, "kind": "merge",
                           "status": "processing", "created_at": "t1"}},
    }


def test_a_newer_instruction_cannot_cancel_a_release_in_flight(tmp_path):
    port = FakePort(
        comments=[comment(9, "/claude change this instead")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, _releasing_card())
    outcome = run_wake(store, port, now=_NOW)
    state = store.read()
    rec = state["issues"]["store#5"]
    # The merge cannot be taken back, but the instruction waits for it instead of being
    # thrown away (#1440): the release stays authoritative and nothing is dispatched.
    assert rec["mode"] == "merge"
    assert state["controls"]["3"]["status"] == "processing"
    assert state["controls"]["9"]["status"] == "queued"
    assert "rejection" not in state["controls"]["9"]
    assert rec["pending"] == ["9"]
    assert outcome.directives == []
    assert any("queued behind a release in flight" in line for line in outcome.log)


def test_an_instruction_queued_behind_a_release_runs_once_it_hands_back(tmp_path):
    initial = _releasing_card()
    port = FakePort(
        comments=[comment(9, "/claude change this instead")],
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)

    # The release ends in a handback, which is what clears the mode and rests the card.
    with store.transaction() as state:
        rec = state["issues"]["store#5"]
        rec.pop("awaiting_deploy", None)
        rec.pop("merged", None)
        release.handback(rec)

    port.comments = []
    port.snapshots = {"store#5": CardSnapshot(status="In review")}
    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=60))
    state = store.read()
    assert state["issues"]["store#5"]["processing"] == ["9"]
    assert state["controls"]["9"]["status"] == "processing"
    payload = outcome.directives[0]["messages"][0]["payload"]
    assert payload == "change this instead"


def test_a_newer_instruction_cancels_a_review_that_has_not_started(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In review", "mode": "merge-review", "merge_control": "3",
                        "review_heads": {"store#7": "aaa"}, "started": True}
        },
        "controls": {"3": {"id": "3", "repo": "store", "number": 5, "kind": "merge-review",
                           "status": "queued", "created_at": "t1"}},
    }
    port = FakePort(
        comments=[comment(9, "/claude change this instead")],
        snapshots={"store#5": CardSnapshot(status="In review")},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    state = store.read()
    assert "mode" not in state["issues"]["store#5"]
    assert state["controls"]["3"]["status"] == "cancelled"


def test_commands_are_reacted_to_so_the_requester_sees_what_happened(tmp_path):
    port = FakePort(comments=[comment(1, "/claude go"), comment(2, "/claude")])
    store = store_for(tmp_path)
    run_wake(store, port, now=_NOW)
    reactions = [
        (e.value["comment"], e.value["reaction"])
        for e in port.applied
        if e.kind is EffectKind.REACT
    ]
    assert ("2", "confused") in reactions        # the invalid command was refused visibly
    assert ("1", "eyes") in reactions            # the valid one was received
    assert ("1", "rocket") in reactions          # ...and then started


def test_an_idle_release_is_not_finished_until_the_status_write_lands(tmp_path):
    initial = {"issues": {"store#5": {"status": "In review", "started": True}}}
    port = FakePort(
        comments=[comment(2, "/claude stop")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        refuse={EffectKind.SET_STATUS},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    # The card is still there with the release armed, so the next wake retries it.
    assert rec["release_pending"] is True

    port.refuse.clear()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert "store#5" not in store.read()["issues"]
    assert port.ownership_edits() == []  # the label stays on (#2497)


def test_a_refused_restart_status_keeps_a_retryable_dispatch_with_its_instruction(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress", "stopping": True, "release_label": True,
                "restart_after_stop": True, "pending": ["9"],
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
                "worktrees": ["/repo/.worktrees/issue-5"],
            }
        },
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="In progress", execution_stopped=True, quiet_seconds=60)
        },
        refuse={(EffectKind.SET_STATUS, "In progress")},
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert "stopping" not in rec
    assert rec["dispatch"]
    assert rec["processing"] == ["9"]
    assert rec["worktrees"] == ["/repo/.worktrees/issue-5"]
    assert outcome.directives == []

    port.refuse.clear()
    port.snapshots["store#5"] = CardSnapshot(status="Todo", owned=False)
    retried = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert len(retried.directives) == 1
    assert payloads(retried.directives[0]) == ["go"]
    assert store.read()["issues"]["store#5"]["worktrees"] == [
        "/repo/.worktrees/issue-5"
    ]


def test_a_new_instruction_cancels_a_merge_review_while_its_worker_is_stopping(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "mode": "merge-review",
                "merge_control": "8",
                "review_heads": {"store#7": "abc"},
                "stopping": True,
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
            }
        },
        "controls": {
            "8": {
                "id": "8", "repo": "store", "number": 5, "kind": "merge-review",
                "status": "processing", "created_at": "x",
            }
        },
    }
    port = FakePort(
        comments=[comment(9, "/claude do not merge; address the new requirement")],
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=60)},
    )
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["stopping"] is True
    assert rec["restart_after_stop"] is True
    assert rec["pending"] == ["9"]
    assert "mode" not in rec
    assert "merge_control" not in rec
    assert "review_heads" not in rec
    assert state["controls"]["8"]["status"] == "cancelled"


# --- the last two parity behaviors (#176 slice 13) ---

def test_re_adding_ownership_during_active_work_rolls_the_lease_and_restarts_once_stopped(
    tmp_path,
):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "started": True, "kind": "issue",
                        "generation": "old-lease"}
        },
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=5)},
        label_events={"store#5": ["e1", "e2"]},   # removed and re-added by hand
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    # A second worker is never spawned alongside the first: the old lease is rolled and the
    # card waits for the superseded worker to exit safely.
    assert rec["stopping"] is True
    assert rec["stop_waiting_generation"] == "old-lease"
    assert rec["generation"] != "old-lease"
    assert rec["manual_restart_after_stop"] is True

    # Once the Runtime has stopped it, the card restarts even with nothing queued. With no
    # durable work registered, that worker receives the issue itself as a fresh ask instead of
    # an empty follow-up brief.
    port.snapshots["store#5"] = CardSnapshot(
        status="In progress", execution_stopped=True, quiet_seconds=60
    )
    port._label_events["store#5"] = ["e1", "e2"]
    second = run_wake(store, port, now=_NOW + timedelta(minutes=1))
    assert len(second.directives) == 1
    assert second.directives[0]["action"] == "new"
    assert payloads(second.directives[0]) == []


def test_a_started_card_with_queued_instructions_is_still_a_followup(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "Todo",
                "started": True,
                "pending": ["9"],
            }
        },
        "controls": {"9": _instruction("9")},
    }

    outcome = run_wake(store_for(tmp_path, initial), FakePort(), now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "followup"
    assert payloads(outcome.directives[0]) == ["go"]


def test_a_followup_refreshes_a_behind_branch_before_dispatch(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo", "started": True, "pending": ["9"]}},
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
    )
    store = store_for(tmp_path, initial)

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert port.updated_branches == ["store#7"]
    assert store.read()["issues"]["store#5"]["pending"] == ["9"]

    port.linked["store#5"] = LinkedPRs(open={"store#7": "bbb"})
    port._pr_states["store#7"] = PrState(ref="store#7", head="bbb", merge_state="CLEAN")
    dispatched = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert [directive["action"] for directive in dispatched.directives] == ["followup"]
    assert payloads(dispatched.directives[0]) == ["go"]


def test_a_conflicting_followup_is_dispatched_for_the_worker_to_resolve(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo", "started": True, "pending": ["9"]}},
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
        refuse={EffectKind.UPDATE_BRANCH},
    )

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert [directive["action"] for directive in outcome.directives] == ["followup"]
    assert any("worker will resolve it" in line for line in outcome.log)


def test_a_followup_waits_when_github_cannot_tell_if_the_branch_is_current(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo", "started": True, "pending": ["9"]}},
        "controls": {"9": _instruction("9")},
    }
    port = FakePort(
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="UNKNOWN")
        },
    )

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert outcome.directives == []
    assert any("mergeability unknown" in line for line in outcome.log)


@pytest.mark.parametrize(
    "prior_work",
    [
        {"worktrees": ["/repo/.worktrees/issue-5"]},
        {"branch": "agent/issue-5"},
        {"branches": {"store": "agent/issue-5"}},
        {"kind": "pr"},
        {"comment_links": {"pull_request_url": "https://github.com/example/store/pull/5"}},
    ],
)
def test_an_empty_restart_with_prior_work_is_still_a_followup(tmp_path, prior_work):
    initial = {
        "issues": {
            "store#5": {
                "status": "Todo",
                "started": True,
                "manual_start": True,
                **prior_work,
            }
        }
    }

    outcome = run_wake(store_for(tmp_path, initial), FakePort(), now=_NOW)

    assert len(outcome.directives) == 1
    assert outcome.directives[0]["action"] == "followup"
    assert payloads(outcome.directives[0]) == []


def test_re_adding_ownership_to_an_idle_card_just_starts_it(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        snapshots={"store#5": CardSnapshot(status="Todo")},
        label_events={"store#5": ["e1", "e2"]},
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    assert "stopping" not in store.read()["issues"]["store#5"]
    assert len(outcome.directives) == 1


def test_a_cleaned_up_card_discards_its_acknowledgement_receipts(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In progress", "stop_waiting_generation": "old-lease"}
        }
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(open=False)})
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    # Every receipt for the card goes, so a later card at the same number cannot read a
    # stale acknowledgement as its own.
    assert ("store#5", None) in port.discarded


def test_an_abandoned_dispatch_discards_only_its_own_receipt(tmp_path):
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, _dispatched(sent))
    run_wake(store, port, now=_NOW)
    discarded = [value for card, value in port.discarded if card == "store#5"]
    assert {"dispatch_id": "d1", "generation": "gen1"} in discarded


def test_a_released_stop_discards_the_receipts_it_consumed(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress", "stopping": True, "release_label": True,
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
            }
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="In progress", execution_stopped=True, quiet_seconds=60)
        }
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    assert ("store#5", None) in port.discarded


# --- the ownership label is a work-level act (#180 slice 3) ---

def test_a_hand_added_label_starts_a_card_only_when_its_labeler_may_ask_for_work(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        snapshots={"store#5": CardSnapshot(status="Todo")},
        label_events={"store#5": ["e1", "e2"]},
        actors={"store#5": "drive-by"},
        permissions={"drive-by": "read"},
    )
    store = store_for(tmp_path, initial)
    outcome = run_wake(store, port, now=_NOW)
    # The label stays where the human put it; it simply starts nothing.
    assert outcome.directives == []
    assert any("not authorized" in line for line in outcome.log)


def test_a_labeler_the_engine_could_not_check_is_deferred_not_trusted(tmp_path):
    initial = {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        snapshots={"store#5": CardSnapshot(status="Todo")},
        label_events={"store#5": ["e1", "e2"]},
        actors={"store#5": "unknown"},
        permissions={"unknown": None},
    )
    store = store_for(tmp_path, initial)
    first = run_wake(store, port, now=_NOW)
    assert first.directives == []
    # The event was not consumed, so the start survives to be reconsidered.
    assert store.read()["ownership"]["events"]["store#5"] == "e1"

    port.permissions["unknown"] = "write"
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert len(second.directives) == 1


def test_a_snapshot_is_asked_about_the_leases_that_are_actually_current(tmp_path):
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen2",
                "stop_waiting_generation": "gen1",
                "stopping": True,
                "stop_requested_at": _NOW.isoformat(),
            }
        }
    }
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=1)})
    run_wake(store_for(tmp_path, initial), port, now=_NOW)
    # A marker from a superseded worker, or an ack for a lease nobody awaits, is not this
    # card's news — so the adapter has to be told which leases count.
    assert ("store#5", "gen2", "gen1", _NOW.isoformat()) in port.snapshot_leases


def test_re_owning_a_card_after_a_stop_writes_no_label_to_read_back(tmp_path):
    """Re-owning a card after a stop used to add the label with the engine's own credential. A
    label event carries no body, so the bot marker that keeps the engine from reading its own
    comments could not reach it — only the actor login could, and on a single-operator project
    that login is the operator, so the engine kept handing itself work it had already given
    itself. Since #2053 it writes no label at all, so there is no event to absorb and no way
    for its own claim to come back as an instruction."""
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "release_label": True,
                "restart_after_stop": True,
                "pending": ["9"],
                "stop_waiting_generation": "gen1",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
            }
        },
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "and also this", "status": "queued", "created_at": "x"}},
        # An initialized ledger: an existing label is part of the adopted world.
        "ownership": {"initialized": True, "events": {"store#5": "event-1"}},
    }
    port = FakePort(
        labelled=["store#5"],
        snapshots={
            "store#5": CardSnapshot(status="In progress", execution_stopped=True, quiet_seconds=60)
        },
        label_events={"store#5": ["event-1"]},
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)
    assert EffectKind.ADD_OWNERSHIP not in port.kinds()

    # GitHub reports no new label event, because the restart caused none.
    outcome = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    # The specific harm: the card is mid-work, so a "human re-added the label" reading rolls the
    # lease and abandons a live dispatch. Nothing about this wake may do that.
    assert not [line for line in outcome.log if "lease rolled" in line]
    assert not [line for line in outcome.log if "label added -> start" in line]
    assert not store.read()["issues"]["store#5"].get("manual_start")

    # One event is absorbed, not all of them: a human re-adding the label afterwards still counts.
    port._label_events["store#5"] = ["event-1", "event-2", "event-3"]
    later = run_wake(store, port, now=_NOW + timedelta(minutes=2))
    assert any("lease rolled" in line for line in later.log), later.log


def test_the_chat_the_host_reported_is_recorded_for_the_next_dispatch(tmp_path):
    """#2155: the worker no longer registers its session, so the receipt is where it comes from.

    The next dispatch continues that chat (#2127). Nothing is written to the comment navigation
    record: repeating the link in every issue comment was the ceremony being removed.
    """
    sent = (_NOW - timedelta(seconds=30)).isoformat()
    url = "https://deck.example/sessions/claude_code:main:s1"
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        acks=("d1",),
        ack_sessions={"d1": url},
    )
    store = store_for(tmp_path, _dispatched(sent))
    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["run_session"] == {"session_url": url, "generation": "gen1"}
    assert "comment_links" not in rec


def test_a_late_receipt_from_a_superseded_run_names_no_chat(tmp_path):
    """A receipt for a generation the card no longer holds must not steer its next dispatch."""
    sent = (_NOW - timedelta(seconds=30)).isoformat()
    state = _dispatched(sent)
    state["issues"]["store#5"]["generation"] = "gen2"
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        acks=("d1",),
        ack_sessions={"d1": "https://deck.example/sessions/claude_code:main:old"},
    )
    store = store_for(tmp_path, state)
    run_wake(store, port, now=_NOW)

    assert "run_session" not in store.read()["issues"]["store#5"]


def test_a_worktree_the_host_reported_is_registered_and_later_released(tmp_path):
    """The registry had no writer at all: `register_worktree` was called only by tests, so
    every card was released with an empty list and worker worktrees accumulated on the host
    forever. The engine never derives a path — it records what the dispatcher's receipt said,
    and it records it before the receipt is discarded, after which there is no other account
    of what was created on disk. It records the resolved path (#1095), so the entry is the
    directory's identity rather than whichever spelling the receipt happened to use.
    """
    reported = str(tmp_path / "store" / ".worktrees" / "issue-5")
    registered = engine_paths.resolve(reported)
    sent = (_NOW - timedelta(seconds=30)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        acks=("d1",),
        ack_worktrees={"d1": [reported]},
    )
    store = store_for(tmp_path, _dispatched(sent))
    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["worktrees"] == [registered]

    # Closing cannot erase the checkout while the lease may still be writing. Once the
    # terminal marker proves the turn ended, the following wake releases what was registered.
    closing = FakePort(snapshots={
        "store#5": CardSnapshot(
            open=False, status="In review", terminal_marker="In review"
        )
    })
    run_wake(store, closing, now=_NOW + timedelta(minutes=1))
    assert not [e for e in closing.applied if e.kind is EffectKind.REMOVE_WORKTREES]
    run_wake(store, closing, now=_NOW + timedelta(minutes=16))
    removals = [e for e in closing.applied if e.kind is EffectKind.REMOVE_WORKTREES]
    assert removals and removals[0].value == [registered]


def test_a_delivery_that_reported_no_worktree_leaves_nothing_to_remove(tmp_path):
    """A dispatcher that reports nothing must not have a path invented for it."""
    sent = (_NOW - timedelta(seconds=30)).isoformat()
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")}, acks=("d1",)
    )
    store = store_for(tmp_path, _dispatched(sent))
    run_wake(store, port, now=_NOW)
    assert store.read()["issues"]["store#5"].get("worktrees") in (None, [])


def test_a_label_a_card_already_carried_is_adopted_not_read_as_an_instruction(tmp_path):
    """Labelling an issue and then commanding it is the ordinary way work starts here. The card
    enters state on the command, and the label it already carried is first seen in that same
    wake — with no ledger entry, so it read as a human re-adding the label mid-work. The engine
    stopped the lease it had just given and waited for an acknowledgement from a worker that had
    never existed: `stopping: true`, forever.

    An ownership label present when a card is adopted is part of the world, not an instruction.
    """
    port = FakePort(
        comments=[comment("c1", "/claude do the thing", repo="store", number=5)],
        labelled=["store#5"],
        label_events={"store#5": ["event-1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {"ownership": {"initialized": True, "events": {}}})
    first = run_wake(store, port, now=_NOW)
    assert len(first.directives) == 1, "the command should have produced one dispatch"

    # Discovery and adoption happen before dispatch in the same wake. The label and command
    # collapse into one ready card, and the label must not be reconsidered on the next wake.
    assert any("label added -> start" in line for line in first.log), first.log
    port.comments = []
    outcome = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    rec = store.read()["issues"]["store#5"]
    assert not rec.get("stopping"), "the card stopped the lease it was just given"
    assert not rec.get("restart_after_stop")
    assert not any("adopted with the card" in line for line in outcome.log), outcome.log
    # The ledger has advanced, so the same label is not reconsidered again.
    assert store.read()["ownership"]["events"]["store#5"] == "event-1"


def test_a_label_added_after_the_card_is_known_still_starts_work(tmp_path):
    """Only the first sighting is adopted. A human labelling a card the engine already tracks
    is still an instruction, or the fix would have swallowed the primary way work is steered."""
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["event-1", "event-2"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "event-1"}},
    })
    outcome = run_wake(store, port, now=_NOW)
    assert any("label added -> start" in line for line in outcome.log), outcome.log


def test_unchanged_labelled_cards_need_no_per_card_event_reads(tmp_path):
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["event-1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "event-1"}},
    })

    run_wake(store, port, now=_NOW)
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert port.labelled_reads == 2
    assert store.read()["ownership"]["events"] == {"store#5": "event-1"}


def test_a_new_event_id_is_detected_without_a_timestamp_change_signal(tmp_path):
    port = FakePort(
        labelled=["store#5", "store#6"],
        label_events={"store#5": ["e1"], "store#6": ["e1"]},
        snapshots={
            "store#5": CardSnapshot(status="Todo"),
            "store#6": CardSnapshot(status="Todo"),
        },
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Todo"}, "store#6": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {"store#5": "e1", "store#6": "e1"}},
    })
    run_wake(store, port, now=_NOW)

    port._label_events["store#6"] = ["e1", "e2"]
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert any("store#6 label added -> start" in line for line in second.log)
    assert not any("store#5 label added -> start" in line for line in second.log)


def test_a_failed_labelled_sweep_holds_the_ledger_and_degrades_health(tmp_path):
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        unreadable_label_events={"store#5"},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Todo"}},
        "ownership": {"initialized": True, "events": {}},
    })
    run_wake(store, port, now=_NOW)
    assert "store#5" not in store.read()["ownership"]["events"]
    assert store.read()["health"]["error_streak"] == 1

    port.unreadable_label_events.clear()
    run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert store.read()["ownership"]["events"]["store#5"] == "e1"


def test_a_card_started_by_a_command_is_claimed_without_labelling_the_issue(tmp_path):
    """A `/claude` comment is the whole instruction: the card is claimed in the engine's own
    state and the issue's labels are left alone (#2053).

    The engine used to write the ownership label here, which is how a card nobody had labelled
    ended up wearing one — and, because the poller edits GitHub as its token's owner, wearing it
    under the operator's name, as if they had handed the work over themselves.
    """
    port = FakePort(
        comments=[comment("c1", "/claude do the thing", repo="store", number=5)],
        snapshots={"store#5": CardSnapshot(status="Todo", owned=False)},
    )
    store = store_for(tmp_path, {"ownership": {"initialized": True, "events": {}}})
    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1
    assert EffectKind.ADD_OWNERSHIP not in port.kinds()
    rec = store.read()["issues"]["store#5"]
    assert rec["engine_claim"] is True, "the card was announced without being claimed"
    assert not rec.get("label_claim"), "it never wore a label, so none can be taken off it"


def test_a_comment_started_worker_is_not_stopped_for_wearing_no_label(tmp_path):
    """The claim has to survive the next wake, or the engine safely stops the worker it just
    started — whose report is then bound to a lease that no longer exists.

    Observed on a real card when the claim failed to land: started 08:45:54, stopped 08:46:30,
    and the worker's terminal marker arrived at 08:46:46 carrying the superseded generation, so
    it could never be honoured. That is what the label write used to buy, and what
    `engine_claim` buys now without touching the issue.
    """
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen1",
                "started": True,
                "engine_claim": True,
            }
        }
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", owned=False, quiet_seconds=1)}
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert "stopping" not in rec
    assert rec["status"] == "In progress"


def test_a_label_a_person_adds_still_hands_the_card_back_when_they_remove_it(tmp_path):
    """The label keeps its human meaning: a card *seen wearing* one is safely stopped when it
    goes away. Only a card that never wore one is exempt (#2053)."""
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "generation": "gen1",
                "started": True,
                "engine_claim": True,
                # Recorded by an earlier wake that saw the label on the issue.
                "label_claim": True,
            }
        }
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", owned=False, quiet_seconds=1)}
    )
    store = store_for(tmp_path, initial)
    run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["stopping"] is True


def test_a_refused_board_claim_produces_no_directive(tmp_path):
    """If the claim will not land there is no point starting: better to keep the intent and
    retry than to spend a worker on a card the board does not show as in progress."""
    port = FakePort(
        comments=[comment("c1", "/claude do the thing", repo="store", number=5)],
        snapshots={"store#5": CardSnapshot(status="Todo", owned=False)},
        refuse={(EffectKind.SET_STATUS, "In progress")},
    )
    store = store_for(tmp_path, {"ownership": {"initialized": True, "events": {}}})
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.directives == []
    assert EffectKind.SET_STATUS not in port.kinds()


# --- discovery: how a labelled issue is found at all (#311) ---

def test_a_labelled_issue_nobody_commented_on_is_found_and_started(tmp_path):
    """The regression #311 is about: the old poller's entire intake was this query.

    Before this, `_adopt_ownership` read label events only for cards already in state, and only a
    command comment put them there — so labelling a fresh issue did nothing at all, forever.
    """
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
        }
    })
    wake = run_wake(store, port, now=_NOW)

    assert len(wake.directives) == 1
    assert wake.suppressed == []
    assert wake.directives[0]["ref"] == "store#7"
    # Started by its label, so it carries no instruction — the issue itself is the brief.
    assert payloads(wake.directives[0]) == []


def test_a_spec_only_snapshot_marks_the_dispatched_worker(tmp_path):
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo", spec_only=True)},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
        }
    })

    wake = run_wake(
        store, port, now=_NOW, command_output=CommandOutputPolicy(enabled=True)
    )

    assert wake.directives[0]["spec_only"] is True
    assert "phase" not in wake.directives[0]
    assert store.read()["issues"]["store#7"]["spec_only"] is True


def _spec_card(status: str = "Needs More Info") -> dict:
    """A card a spec worker already ran on under the spec-only label."""
    return {
        "status": status,
        "started": True,
        "spec_only": True,
        "engine_claim": True,
        "generation": "g1",
    }


def _approved_spec_port(status: str = "Needs More Info", **snapshot) -> FakePort:
    return FakePort(snapshots={"store#7": CardSnapshot(status=status, **snapshot)})


def test_removing_spec_only_after_the_spec_starts_the_build(tmp_path):
    """A person approves the spec by removing the label; the poller starts the build itself
    rather than waiting for a `/claude` comment (#2706), and says so on the issue."""
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card()}, **_LEDGER})

    wake = run_wake(store, _approved_spec_port(), now=_NOW)

    assert len(wake.directives) == 1, wake.log
    directive = wake.directives[0]
    assert directive["spec_approved"] is True
    assert "spec_only" not in directive
    assert any("Spec approved" in body for body in _refusal_comments(wake))
    rec = store.read()["issues"]["store#7"]
    assert not ({"spec_only", "spec_approved", "spec_build"} & set(rec))

    # Started once: the next wake arms nothing more.
    again = run_wake(store, _approved_spec_port("In progress"), now=_NOW + timedelta(minutes=1))
    assert again.directives == []


def test_a_spec_approved_in_chat_starts_the_build_once_its_worker_parks(tmp_path):
    """The chat flow: the resumed spec worker removes the label while it is still In progress.
    The removal alone marks nothing (#2773); the worker's `--spec-approved` finish does, and only
    the park starts the build, so two workers never share it."""
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card("In progress")}, **_LEDGER})

    working = run_wake(store, _approved_spec_port("In progress"), now=_NOW)
    assert working.directives == []
    rec = store.read()["issues"]["store#7"]
    assert "spec_approved" not in rec
    assert rec["spec_only"] is True

    finished = WorkerControl(store, _approved_spec_port("In progress")).finish(
        "store", 7, "g1", "needs_more_info", "Approved by Eero in chat.", spec_approved=True
    )
    assert finished.ok, finished.reason
    assert store.read()["issues"]["store#7"]["spec_approved"] is True

    parked = run_wake(
        store, _approved_spec_port(terminal_marker="Needs More Info"),
        now=_NOW + timedelta(minutes=1),
    )
    assert [d.get("spec_approved") for d in parked.directives] == [True], parked.log


def test_a_label_removed_mid_turn_without_approval_starts_nothing(tmp_path):
    """agentdeck#2769: the person removed the label and told the spec worker "no specs". The
    worker's plain needs_more_info finish drops the spec, and no wake reads the removal as an
    approval afterwards (#2773)."""
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card("In progress")}, **_LEDGER})

    run_wake(store, _approved_spec_port("In progress"), now=_NOW)
    finished = WorkerControl(store, _approved_spec_port("In progress")).finish(
        "store", 7, "g1", "needs_more_info", "Investigation done, no spec."
    )
    assert finished.ok, finished.reason

    for minutes in (1, 2):
        parked = run_wake(
            store, _approved_spec_port(terminal_marker="Needs More Info"),
            now=_NOW + timedelta(minutes=minutes),
        )
        assert parked.directives == [], parked.log
        assert not any("Spec approved" in body for body in _refusal_comments(parked))
    rec = store.read()["issues"]["store#7"]
    assert not ({"spec_only", "spec_approved", "spec_build"} & set(rec))


def test_a_spec_card_that_keeps_its_label_stays_parked(tmp_path):
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card()}, **_LEDGER})

    wake = run_wake(store, _approved_spec_port(spec_only=True), now=_NOW)

    assert wake.directives == []
    assert _refusal_comments(wake) == []


def test_a_spec_card_that_moved_on_another_way_is_not_started(tmp_path):
    """A card a person moved to In review, say, by hand drops the mark rather than starting
    a build later on a stale approval."""
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card("In review")}, **_LEDGER})

    wake = run_wake(store, _approved_spec_port("In review"), now=_NOW)

    assert wake.directives == []
    assert "spec_approved" not in store.read()["issues"]["store#7"]


def test_a_spec_only_label_removed_before_any_worker_ran_is_not_an_approval(tmp_path):
    card = {**_spec_card(), "started": False}
    store = store_for(tmp_path, {"issues": {"store#7": card}, **_LEDGER})

    wake = run_wake(store, _approved_spec_port(), now=_NOW)

    assert not any("Spec approved" in body for body in _refusal_comments(wake))
    assert "spec_approved" not in store.read()["issues"]["store#7"]


def test_a_spec_build_taken_over_in_its_chat_starts_no_second_worker(tmp_path):
    """The person approved in the spec worker's chat and asked it to build (#2716): that worker
    runs `start-implementation`, builds, and reports. No wake starts another worker for it."""
    store = store_for(tmp_path, {"issues": {"store#7": _spec_card("In progress")}, **_LEDGER})
    started = WorkerControl(store, _approved_spec_port("In progress")).start_implementation(
        "store", 7, "g1", "Approved in my chat: start implementing."
    )
    assert started.ok, started.reason

    building = run_wake(store, _approved_spec_port("In progress"), now=_NOW)
    assert building.directives == [], building.log
    reported = run_wake(
        store,
        _approved_spec_port("In review", terminal_marker="In review"),
        now=_NOW + timedelta(minutes=1),
    )
    assert reported.directives == [], reported.log
    assert not any("Spec approved" in body for body in _refusal_comments(reported))
    assert "spec_approved" not in store.read()["issues"]["store#7"]

    # Even a wake that still read the label on the card, and then saw it go, starts nothing:
    # the build is this Run's, so a later park of it is a question, not a fresh approval.
    relabelled = _approved_spec_port("In progress", spec_only=True)
    run_wake(store, relabelled, now=_NOW + timedelta(minutes=2))
    parked = run_wake(
        store,
        _approved_spec_port(terminal_marker="Needs More Info"),
        now=_NOW + timedelta(minutes=3),
    )
    assert parked.directives == [], parked.log
    assert "spec_approved" not in store.read()["issues"]["store#7"]


class _HoldsChatAnswers(FakePort):
    """A port on which the chat answer's comment does not land this wake."""

    hold = True

    def apply(self, effect):
        if self.hold and "> Build it" in str(getattr(effect.value, "body", "")):
            return False
        return super().apply(effect)


def test_an_approval_waits_for_a_chat_answer_still_owed_to_the_issue(tmp_path):
    """The label came off while the person's chat answer is not yet on the issue. That answer
    resumes the card In progress under the spec worker, so the removal is not an approval by
    itself: the worker's finish decides (#2773), and no other worker starts meanwhile."""
    from agentdeck.poller_engine import chat_answers

    card = {**_spec_card(), "run_session": {"session_url": "/chats/c1", "generation": "g1"}}
    store = store_for(tmp_path, {"issues": {"store#7": card}, **_LEDGER})
    chat_answers.write(
        store.paths.state,
        request_id="answer-0001",
        key="store#7",
        generation="g1",
        text="Build it",
        session_url="/chats/c1",
        now=_NOW - timedelta(seconds=5),
    )
    parked = CardSnapshot(status="Needs More Info", terminal_marker="Needs More Info")
    port = _HoldsChatAnswers(snapshots={"store#7": parked})

    held = run_wake(store, port, now=_NOW)

    assert held.directives == [], held.log
    assert not any("Spec approved" in body for body in _refusal_comments(held))
    rec = store.read()["issues"]["store#7"]
    assert "spec_approved" not in rec
    assert rec["spec_only"] is True

    port.hold = False
    port.snapshots = {"store#7": CardSnapshot(status="Needs More Info")}
    answered = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert answered.directives == [], answered.log
    rec = store.read()["issues"]["store#7"]
    assert rec["status"] == "In progress"
    assert "spec_approved" not in rec


def test_discovery_adopts_a_long_standing_label_missing_from_an_initialized_ledger(tmp_path):
    """A partial activation sweep may have initialized without baselining this card."""
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("old", "2026-07-20T12:00:00Z")]},
        snapshots={"store#7": CardSnapshot(status="In review")},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
        }
    })

    wake = run_wake(store, port, now=_NOW)

    assert wake.directives == []
    assert wake.suppressed == ["store#7"]
    assert any(
        "discovery: store#7 already carried the ownership label on first sight — "
        "not dispatched; post `/claude …`, or remove and re-add the label, to start it" in line
        for line in wake.log
    )
    assert not store.read()["issues"]["store#7"].get("manual_start")
    assert store.read()["ownership"]["events"]["store#7"] == "old"


def test_authorization_deferral_preserves_the_first_sight_suppression_notice(tmp_path):
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("old", "2026-07-20T12:00:00Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
        actors={"store#7": None},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T11:59:00+00:00",
        }
    })

    first = run_wake(store, port, now=_NOW)
    assert first.suppressed == []
    assert store.read()["issues"]["store#7"]["ownership_discovery_pending"] is True

    port.actors["store#7"] = "eero"
    second = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert second.directives == []
    assert second.suppressed == ["store#7"]
    assert "ownership_discovery_pending" not in store.read()["issues"]["store#7"]


def test_an_old_unresolved_label_history_does_not_block_another_start(tmp_path):
    port = FakePort(
        labelled=["store#7", "store#8"],
        label_events={"store#7": [], "store#8": ["e8"]},
        snapshots={
            "store#7": CardSnapshot(status="Todo"),
            "store#8": CardSnapshot(status="Todo"),
        },
    )
    store = store_for(
        tmp_path,
        {"ownership": {"initialized": True, "events": {}}},
    )

    wake = run_wake(store, port, now=_NOW)

    assert [directive["ref"] for directive in wake.directives] == ["store#8"]
    assert wake.suppressed == ["store#7"]


def test_discovery_seeds_the_card_but_the_ledger_decides_whether_it_starts(tmp_path):
    """Seeding must not manufacture a start the provenance rules would not have reached.

    On an uninitialized ledger nothing is a start, so a rebuilt state adopts the standing world
    rather than re-running every labelled issue on it.
    """
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": ["e1"]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {})  # no ledger yet: first sight of the world
    wake = run_wake(store, port, now=_NOW)

    assert wake.directives == []
    assert wake.suppressed == ["store#7"]
    assert "store#7" in store.publishes[-1]["issues"], "known even though it did not start"


def test_a_sweep_that_could_not_be_read_is_not_an_empty_board(tmp_path):
    """An unreadable sweep must hold the ledger, never be read as "nothing is labelled"."""
    port = FakePort(
        labelled_complete=False,
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="Todo")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}, "ownership": {}})
    wake = run_wake(store, port, now=_NOW)

    # The ledger cannot initialize off a partial view, so a later full sweep still gets to decide.
    assert not store.publishes[-1]["ownership"].get("initialized")
    assert wake.report.scan_failures == 1
    assert port.labelled_reads == 1


def test_a_card_already_known_is_not_reseeded_by_discovery(tmp_path):
    """Discovery introduces cards; it must not disturb one that is mid-flight."""
    in_flight = {"status": "In progress", "started": True, "generation": "g1"}
    port = FakePort(
        labelled=["store#5"],
        label_events={"store#5": ["e1"]},
        snapshots={"store#5": CardSnapshot(status="In progress")},
    )
    store = store_for(
        tmp_path,
        {"issues": {"store#5": in_flight}, "ownership": {"initialized": True,
                                                         "events": {"store#5": "e1"}}},
    )
    wake = run_wake(store, port, now=_NOW)

    assert store.publishes[-1]["issues"]["store#5"]["generation"] == "g1"
    assert wake.directives == []


def test_a_merge_on_a_card_with_no_record_waits_for_discovery_instead_of_refusing(tmp_path):
    """The refusal that could never clear (#294), now a wait that does.

    The first wake cannot read the labelled-issue sweep, so the card the `/merge` names
    is not in state at all. Calling that `reject-busy` retired the command permanently
    for something that was never true; the operator would retry forever. Left new, the
    next wake seeds the card and releases it.
    """
    port = FakePort(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        labelled_complete=False,
    )
    store = store_for(tmp_path)

    first = run_wake(store, port, now=_NOW)

    control = store.read()["controls"]["3"]
    assert control["status"] == "new", "a deferred command is not a rejected one"
    assert "rejection" not in control
    assert any("release deferred: store#5 (no record of this card yet)" in line
               for line in first.log)

    port._labelled = ["store#5"]
    port._labelled_complete = True
    port.comments = []
    run_wake(store, port, now=_NOW)

    state = store.read()
    assert state["issues"]["store#5"]["mode"] == "merge"
    assert state["controls"]["3"]["status"] == "processing"
def test_a_stop_whose_worker_already_finished_keeps_the_finished_work(tmp_path):
    """agentdeck#295 exactly: the worker had opened its PR before the stop landed (#299).

    Its terminal marker carries `stop_waiting_generation`, so the old code saw neither a
    terminal result (wrong generation) nor an ack (wrong prefix), and the card sat In
    progress with a completed PR for the two-hour stale window. Landing it as an ordinary
    stop would be no better: Todo plus a dropped label orphans the PR and invites a second
    worker onto finished work. The turn ended and said what it did, so that is published.
    """
    initial = {
        "issues": {
            "store#5": {
                "status": "In progress",
                "stopping": True,
                "release_label": True,
                "generation": "gen2",
                "stop_waiting_generation": "gen1",
                "stop_control": "7",
                "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat(),
                "restart_after_stop": True,
            }
        },
        "controls": {"7": {"id": "7", "repo": "store", "number": 5, "kind": "stop",
                           "status": "processing", "created_at": "x"}},
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(
                status="In progress", stopped_terminal_marker="In review", quiet_seconds=60
            )
        },
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In review"
    assert "stopping" not in rec
    assert "stop_waiting_generation" not in rec
    assert "restart_after_stop" not in rec
    assert [e.value for e in port.applied if e.kind is EffectKind.SET_STATUS] == ["In review"]
    assert port.ownership_edits() == [], (
        "the card ended like any other finished card; /merge still needs the label"
    )
    assert store.read()["controls"]["7"]["status"] == "done"
    assert any("stop met by a finished turn -> In review" in line for line in outcome.log)
    assert outcome.directives == [], "nothing is dispatched onto work that is done"


def _wedged(extra=None):
    """A card mid-turn: the engine dispatched, the host acknowledged, no marker yet."""
    rec = {"status": "In progress", "generation": "gen-1", "processing": ["9"]}
    rec.update(extra or {})
    return {
        "issues": {"store#5": rec},
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "do the thing", "status": "processing", "created_at": "x"}},
    }


# --- Run without phases (#2154) ----------------------------------------------
#
# Poller phases are retired: a Run investigates, edits, tests and reports in whatever turns it
# needs, verification is evidence a job attaches to the Run, and the general delivery and
# generation fences carry the safety the phase machinery used to duplicate.

_EMPTY = hashlib.sha256(b"").hexdigest()


def _tested(head: str, *, clean: bool = True) -> dict:
    """A tested-workspace record as the job queue writes it."""
    return {
        "root": "/w",
        "head": head,
        "staged_sha256": _EMPTY,
        "tracked_diff_sha256": _EMPTY if clean else "dirty",
        "untracked_sha256": _EMPTY,
        "untracked_count": 0,
    }


def _verification_job(**overrides) -> dict:
    job = {
        "id": "job-7",
        "purpose": "verification",
        "run_id": "gen-1",
        "work_id": "github_issue:org/store#5",
        "state": "done",
        "result_class": "success",
        "tested_workspace": _tested("head-1"),
        "verified_workspace": _tested("head-1"),
    }
    job.update(overrides)
    # The repository-keyed record every row since #2148 carries, mirroring the cwd reading.
    for singular, keyed in (("tested_workspace", "tested_workspaces"),
                            ("verified_workspace", "verified_workspaces")):
        if keyed not in job and isinstance(job.get(singular), dict):
            job[keyed] = {"unresolved:/w": job[singular]}
    return job


def _running_card(jobs: dict, extra=None):
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")}, jobs=jobs)
    return _wedged(extra), port


def _discovered_card_port() -> FakePort:
    return FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
    )


def _discovered_card_state() -> dict:
    return {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
        }
    }


def test_an_explicit_green_verification_job_credits_its_runs_exact_commit(tmp_path):
    """#2154 tests 2/3: the job runs with no verify phase, and its result attaches to the Run
    directly — nothing waits on it, nothing parks, and no worker is dispatched for it."""
    initial, port = _running_card({"job-7": _verification_job()})
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert "head-1" in rec["release_tests_pass"]
    assert "phases" not in rec
    assert outcome.directives == []
    assert (
        "release: store#5 tests green on head-1 (verification job job-7, tested "
        "unresolved:/w@head-1)"
    ) in outcome.log


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"purpose": ""}, "an ordinary job is never release evidence"),
        # An earlier Run of the *same* Work does count (#2306); one naming no Work, or another
        # Work, never does.
        ({"run_id": "gen-0", "work_id": ""}, "evidence for another Run of no known Work"),
        (
            {"run_id": "gen-0", "work_id": "github_issue:org/store#6"},
            "evidence for another Run of another Work",
        ),
        ({"work_id": "github_issue:org/store#6"}, "evidence for another Work"),
        ({"state": "failed", "result_class": "failure"}, "a red run"),
        ({"state": "running", "result_class": ""}, "a run still going"),
        ({"tested_workspace": _tested("head-1", clean=False)}, "an unclean tree"),
        ({"tested_workspace": {"head": "head-1"}}, "an unprovable tree"),
        ({"tested_workspace": None}, "no record of what ran"),
        # The tree moved while the suite ran: the second reading no longer matches the first.
        ({"verified_workspace": _tested("head-2")}, "a head moved mid-run"),
        ({"verified_workspace": _tested("head-1", clean=False)}, "an edit made mid-run"),
        ({"verified_workspace": None}, "no reading after the command (an older runtime)"),
        ({"tested_workspaces": None}, "a row from before #2148, with no repository-keyed record"),
    ],
)
def test_only_an_exact_clean_green_verification_of_this_run_credits_anything(
    tmp_path, overrides, why
):
    initial, port = _running_card({"job-7": _verification_job(**overrides)})
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    assert not store.read()["issues"]["store#5"].get("release_tests_pass"), why


def test_a_rebase_round_bound_by_its_own_run_records_its_lineage(tmp_path):
    """#2453: the head a rebase round's own suite ran on is written down as coming from the
    head the engine asked it to rebase, so a verdict on that older head can still be traced."""
    initial, port = _running_card(
        {"job-7": _verification_job()},
        {"rebase_round_pending": {"refs": ["store#12"], "round": "9", "from": {"store#12": "h0"}}},
    )
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["rebase_round"] == {"store#12": "head-1"}
    edges = [(e["ref"], e["from"], e["to"]) for e in release.rebase_lineage(rec)]
    assert edges == [("store#12", "h0", "head-1")]


def test_a_rebase_round_binds_the_head_its_own_verification_ran_on(tmp_path):
    """#2122 carries over unchanged: the rebase round's provenance is bound when its suite's
    commit is first credited, and only once."""
    initial, port = _running_card(
        {"job-7": _verification_job()},
        {"rebase_round_pending": {"refs": ["store#12"], "round": "9"}},
    )
    store = store_for(tmp_path, initial)

    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert "rebase_round_pending" not in rec
    assert rec["rebase_round"] == {"store#12": "head-1"}
    # Armed with no source, as every round before #2453 was: it records no lineage.
    assert "rebase_lineage" not in rec
    first = (rec["release_tests_pass"], rec["rebase_round"])

    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    rec = store.read()["issues"]["store#5"]
    assert (rec["release_tests_pass"], rec["rebase_round"]) == first
    assert not any("tests green" in line for line in second.log), "credited once"


def test_an_unreadable_job_queue_credits_nothing_and_breaks_nothing(tmp_path):
    initial, port = _running_card({})
    port.verification_jobs = lambda: None
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.errors == []
    assert not store.read()["issues"]["store#5"].get("release_tests_pass")


def test_an_ordinary_dispatch_carries_no_phase_machinery(tmp_path):
    """#2154 tests 1/5/12: a Run is dispatched as ordinary work — no phase envelope, packet,
    boundary or checkpoint id — and bounded command output rides it as a plain tool setting."""
    store = store_for(tmp_path, _discovered_card_state())

    wake = run_wake(
        store, _discovered_card_port(), now=_NOW,
        command_output=CommandOutputPolicy(enabled=True, no_archive_commands=("vault read",)),
    )

    (directive,) = wake.directives
    for key in ("phase", "phase_boundary", "expected_source_session_id"):
        assert key not in directive
    assert directive["action"] != "phase"
    assert directive["command_artifacts"] == {
        "mode": "enforce",
        "visible_tool_result_chars": 8000,
        "artifact_retention_days": 30,
        "artifact_slice_max_bytes": 64 * 1024,
        "artifact_slice_call_limit": 8,
        "no_archive_commands": ["vault read"],
        "card": "store#7",
        "generation": directive["generation"],
    }
    assert "phases" not in store.read()["issues"]["store#7"]


def test_a_project_without_command_output_dispatches_exactly_as_before(tmp_path):
    """#2154 test 11: the projects that never ran phases see no new field at all."""
    wake = run_wake(
        store_for(tmp_path, _discovered_card_state()), _discovered_card_port(), now=_NOW
    )

    (directive,) = wake.directives
    assert "command_artifacts" not in directive
    assert "phase" not in directive


def test_a_busy_run_is_never_sent_a_phase_pressure_steer(tmp_path):
    """#2154 test 4: context pressure is the provider's and the runtime's to manage. Even a card
    whose old phase block says it is mid-phase gets no poller directive for it."""
    initial, port = _running_card(
        {}, {"phases": {"generation": "gen-1", "phase": "execute", "status": "active"}}
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert store.read()["issues"]["store#5"]["phases"] == {
        "generation": "gen-1", "phase": "execute", "status": "active",
    }


def test_an_old_checkpointed_phase_is_continued_as_an_ordinary_revive(tmp_path):
    """#2154 tests 6/9: a turn that ended on a legacy checkpoint is just a turn that ended
    without a report. It continues through the ordinary revive in the same Work — no phase
    successor, no packet, no transfer record — and the old block is left exactly as it was."""
    old_block = {"generation": "gen-1", "phase": "orient", "status": "checkpointed",
                 "checkpoints": {"poller:d:phase:orient:1": {"phase": "orient"}}}
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(subtype="success"),
    )
    store = store_for(tmp_path, _wedged({"phases": dict(old_block)}))

    outcome = run_wake(store, port, now=_NOW)

    (directive,) = outcome.directives
    assert directive["action"] == "resume"
    assert directive["continuation"] is True
    assert "phase" not in directive and "phase_boundary" not in directive
    rec = store.read()["issues"]["store#5"]
    assert rec["phases"] == old_block
    assert directive["run_id"] == rec["generation"] != "gen-1"
    assert directive["work_id"] == "github_issue:org/store#5", "the same Work continues"


def test_an_account_refusal_moves_the_run_without_any_transfer_record(tmp_path):
    """#2154 test 6: a provider or account move is ordinary continuation, steered only by the
    host's `avoid_account` hint; nothing phase-shaped is reserved or frozen."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_refused(),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    (directive,) = outcome.directives
    assert directive["avoid_account"] == "codex:codex"
    assert "phase" not in directive
    assert "phases" not in store.read()["issues"]["store#5"]


def _legacy_transfer_store(path, transitions: dict | None):
    rec = {"dispatch": {"id": "d1", "action": "phase", "generation": "gen-1"}}
    if transitions is not None:
        rec["phases"] = {"transitions": transitions}
    return store_for(path, _wedged(rec))


def test_an_unacknowledged_delivery_is_awaited_never_resent(tmp_path):
    """Same delivery id is never written twice, and an ambiguous write is never re-routed:
    inside the acknowledgement window the dispatch is awaited under its own id, with no new
    directive to any account."""
    initial = _wedged({
        "dispatch": {"id": "d1", "action": "work", "generation": "gen-1", "attempts": 1,
                     "sent_at": _NOW.isoformat()},
    })
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW + timedelta(seconds=5))

    assert outcome.directives == []
    assert "dispatch awaiting acknowledgement: store#5" in outcome.log


def test_a_working_run_gets_no_second_worker(tmp_path):
    """An active worker cannot be joined by a second conflicting one: an in-progress card with
    no queued follow-up is left alone, whatever its old phase block says."""
    initial, port = _running_card({}, {"phases": {"status": "checkpointed"}})

    outcome = run_wake(store_for(tmp_path, initial), port, now=_NOW)

    assert outcome.directives == []


def test_a_stale_generation_cannot_move_the_runs_lifecycle(tmp_path):
    """Poller generation fencing is untouched: a superseded worker's terminal report is refused
    and writes nothing."""
    store = store_for(tmp_path, _wedged({"generation": "gen-2"}))
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    before = json.dumps(store.read(), sort_keys=True)

    result = WorkerControl(store, port).finish("store", 5, "gen-1", "in_review", "done")

    assert not result.ok
    assert port.applied == []
    assert json.dumps(store.read(), sort_keys=True) == before


def test_an_observed_turn_end_with_no_report_publishes_needs_more_info(tmp_path):
    """#279 step 3: the wedge resolves itself instead of waiting two hours for a human.

    A card only leaves In progress on the worker's own marker. Without one it looks like
    healthy work — `no directives this wake`, a follow-up stuck in `pending`, no degraded
    health — until the stale window cold-reloads a second worker onto finished work.

    Needs More Info rather than In review, because silence is not success: the engine has no
    statement of what the work reached, so it says so instead of guessing. Since #835 the
    engine restarts such a card first, so this is what it does once the restarts run out.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1"}},
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 1
    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "Needs More Info"
    # The commands it was carrying are resolved, which is what lets the queued follow-up run.
    assert rec["processing"] == []
    assert EffectKind.SET_STATUS in port.kinds()
    # And the card says plainly what was observed, rather than only a log line nobody reads.
    said = " ".join(
        str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT
    )
    assert "ended without reporting" in said and "Needs More Info" in said


def _awaited_suite(**overrides) -> dict:
    """This Run's own verification job, as the queue lists it while the worker waits on it."""
    job = {"id": "job-1", "purpose": "verification", "run_id": "gen-1",
           "work_id": "github_issue:org/store#5", "session_key": "claude_code:main:s1",
           "state": "running", "notified": False}
    job.update(overrides)
    return job


@pytest.mark.parametrize("revives", [0, 2])
@pytest.mark.parametrize(
    "job",
    [
        _awaited_suite(state="queued"),
        _awaited_suite(state="running"),
        # Finished, but the chat has not been told: the turn it wakes has not started.
        _awaited_suite(state="done", result_class="success"),
    ],
)
def test_a_turn_that_ended_waiting_on_its_own_suite_is_left_in_progress(tmp_path, revives, job):
    """#2300: the worker queued its suite and ended its turn, as AGENTS.md tells it to.

    That is a worker waiting on its own job, not one that ended without reporting: no revive is
    spent, the card stays In progress, and nothing is posted. The job's completion wakes it.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1"}},
        jobs={"job-1": job},
    )
    store = store_for(tmp_path, _wedged({"revives": revives}))

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress"
    assert rec["generation"] == "gen-1", "no revive minted a new lease"
    assert rec.get("revives", 0) == revives
    assert "reportless_resolved" not in rec
    assert rec["processing"] == ["9"]
    assert outcome.directives == []
    assert outcome.report.reportless_ends == 0
    assert EffectKind.SET_STATUS not in port.kinds()
    assert EffectKind.COMMENT not in port.kinds()
    assert (
        "reconcile: store#5 worker turn ended (success) waiting on its verification job "
        "job-1 -> left in progress"
    ) in outcome.log


@pytest.mark.parametrize(
    "job",
    [
        _awaited_suite(state="done", result_class="success", notified=True),
        _awaited_suite(run_id="gen-0"),  # an earlier Run's suite
        # An ordinary job holds the card now (#2622), but only its own Run's, and only until
        # the chat has been told. This list said a `purpose=""` job never holds before #2622.
        _awaited_suite(purpose="", run_id="gen-0"),
        _awaited_suite(purpose="", run_id=""),
        _awaited_suite(purpose="", state="done", result_class="success", notified=True),
        _awaited_suite(work_id="github_issue:org/store#6"),
    ],
)
def test_a_reportless_ending_with_no_job_of_its_own_to_wait_on_is_handled_as_today(
    tmp_path, job
):
    """Once the chat has been told — or when the job is not this Run's suite — the ending is
    the woken turn's own, and a silent one still revives and, past the cap, parks."""
    ended = {"observed": "ended", "subtype": "success", "generation": "gen-1"}

    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": ended}, jobs={"job-1": job},
    )
    store = store_for(tmp_path / "revive", _wedged())
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec["revives"] == 1
    assert rec["generation"] != "gen-1"

    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": ended}, jobs={"job-1": job},
    )
    store = store_for(tmp_path / "park", _wedged({"revives": 2}))
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.report.reportless_ends == 1
    assert store.read()["issues"]["store#5"]["status"] == "Needs More Info"


@pytest.mark.parametrize(
    "job",
    [
        _awaited_suite(purpose="", state="queued"),
        _awaited_suite(purpose="", state="running"),
        _awaited_suite(purpose="", state="failed", result_class="failure"),
        # The Work as the deck spelled it for the chat, in another case.
        _awaited_suite(purpose="", work_id="github_issue:Org/Store#5"),
    ],
)
def test_a_turn_that_ended_waiting_on_an_ordinary_job_of_its_run_is_left_in_progress(
    tmp_path, job
):
    """#2622: a worker that queued its suite without --verification is waiting just the same.
    Holding the card spends nothing; only crediting a green run needs a verification job."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1"}},
        jobs={"job-1": job},
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress"
    assert rec["generation"] == "gen-1"
    assert outcome.report.reportless_ends == 0
    assert EffectKind.COMMENT not in port.kinds()
    assert (
        "reconcile: store#5 worker turn ended (success) waiting on its job job-1 "
        "-> left in progress"
    ) in outcome.log


@pytest.mark.parametrize("state", ["queued", "running"])
def test_a_turn_waiting_on_a_job_its_chat_queued_without_a_run_is_left_in_progress(
    tmp_path, state
):
    """#2902: a job submitted without --session-token names no Run, but it still wakes the chat
    that queued it. On Northwind/issues#645 the card was restarted while its worker
    waited on exactly such a job; the Runtime names the chat the turn ended in, and that is
    enough to tell the worker is waiting."""
    job = _awaited_suite(purpose="", run_id="", work_id="", state=state)
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1", "session_key": "claude_code:main:s1"}},
        jobs={"job-1": job},
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["generation"] == "gen-1"
    assert rec.get("revives", 0) == 0
    assert EffectKind.COMMENT not in port.kinds()
    assert (
        "reconcile: store#5 worker turn ended (success) waiting on its job job-1 "
        "-> left in progress"
    ) in outcome.log


@pytest.mark.parametrize(
    "job",
    [
        # Another chat's job naming no Run.
        _awaited_suite(purpose="", run_id="", work_id="", session_key="claude_code:main:s2"),
        # The same chat, but the job names an earlier Run.
        _awaited_suite(purpose="", run_id="gen-0"),
        # The same chat's job, already told.
        _awaited_suite(purpose="", run_id="", work_id="", state="done", notified=True),
    ],
)
def test_a_chat_match_holds_only_this_chats_unannounced_job_naming_no_run(tmp_path, job):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1", "session_key": "claude_code:main:s1"}},
        jobs={"job-1": job},
    )
    store = store_for(tmp_path, _wedged())

    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["revives"] == 1
    assert rec["generation"] != "gen-1"


def test_the_queue_is_read_before_the_runs_ending(tmp_path):
    """#2300, #2200: the Runtime delivers a job's completion before it marks the job notified.
    Reading the queue first means a job read as notified already has the woken turn visible in
    the ending read after it; the other order could revive a card whose next turn is running."""
    order: list[str] = []

    class Ordered(FakePort):
        def verification_jobs(self):
            order.append("jobs")
            return super().verification_jobs()

        def turn_ended(self, repo, number, generation):
            order.append("outcome")
            return super().turn_ended(repo, number, generation)

    port = Ordered(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "generation": "gen-1"}},
    )
    run_wake(store_for(tmp_path, _wedged()), port, now=_NOW)

    assert "outcome" in order
    assert order[order.index("outcome") - 1] == "jobs"


@pytest.mark.parametrize("observed", ["died", "stopped", "abandoned"])
def test_a_turn_that_did_not_reach_its_own_end_is_not_waiting_on_anything(tmp_path, observed):
    """A death, a stop or a wedge is over whatever the queue holds: the job's completion has
    no turn left to wake."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": observed, "generation": "gen-1"}},
        jobs={"job-1": _awaited_suite()},
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["status"] == "Needs More Info"


def test_an_unreadable_job_queue_does_not_hide_a_reportless_ending(tmp_path):
    """#2300: an unreadable queue proves no wait. Holding the card until it could be read would
    hide a real reportless ending for as long as the queue stayed unreadable, so the ending is
    handled exactly as it was before the queue was consulted."""

    class Unreadable(FakePort):
        def verification_jobs(self):
            return None

    port = Unreadable(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "generation": "gen-1"}},
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 1
    assert store.read()["issues"]["store#5"]["status"] == "Needs More Info"


def test_a_worker_the_deck_cannot_find_is_reported_and_nothing_else(tmp_path):
    """`gone` is not an ending. A runtime restart makes every in-flight card `gone` at once.

    Publishing on it would move a whole board's live work to Needs More Info on one restart,
    so absence is reported — it proves nothing is running, not that anything finished.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "gone", "generation": "gen-1"}},
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 1
    assert any("nothing is running" in line for line in outcome.log)
    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress", "an absent worker is not an ending"
    assert rec["processing"] == ["9"]
    assert EffectKind.SET_STATUS not in port.kinds()


def test_the_report_degrades_health_so_it_is_not_only_in_a_log(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "gone"}},
    )
    store = store_for(tmp_path, _wedged())

    run_wake(store, port, now=_NOW)

    health = store.read()["health"]
    assert health["last_report"]["reportless_ends"] == 1
    assert health["totals"]["reportless_ends"] == 1


def test_a_worker_that_did_report_is_not_also_called_reportless(tmp_path):
    """The marker is the engine's own evidence and outranks the host's observation."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", terminal_marker="In review")},
        turn_ends={"store#5": {"observed": "ended"}},
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 0
    assert store.read()["issues"]["store#5"]["status"] == "In review", (
        "the reporting path is unchanged"
    )


def test_a_stopping_card_is_left_to_the_stop_protocol(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", quiet_seconds=60)},
        turn_ends={"store#5": {"observed": "ended"}},
    )
    store = store_for(
        tmp_path,
        _wedged({"stopping": True, "stop_waiting_generation": "gen-0",
                 "stop_requested_at": (_NOW - timedelta(minutes=1)).isoformat()}),
    )

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 0


def test_an_unacknowledged_dispatch_is_not_a_finished_turn(tmp_path):
    """No receipt means no worker was proven to exist; the dispatch path owns that case."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended"}},
    )
    store = store_for(
        tmp_path,
        _wedged({"dispatch": {
            "id": "d1", "generation": "gen-1", "at": _NOW.isoformat(), "action": "work",
        }}),
    )

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 0


def test_publishing_needs_more_info_happens_once_not_every_wake(tmp_path):
    """The receipt stays on disk, so nothing about it stops a second wake reading it again.

    If resolving did not close the lease, every later wake would re-publish and post another
    comment — a card commented on twice a minute, forever.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "subtype": "success",
                               "generation": "gen-1"}},
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    first = run_wake(store, port, now=_NOW)
    assert first.report.reportless_ends == 1

    # The board has NOT caught up: it still reports In progress, which is what makes
    # `reconcile` say ACTIVE again. That is the case that actually re-fired.
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert second.report.reportless_ends == 0, "the same observation must not resolve twice"
    comments = [e for e in port.applied if e.kind is EffectKind.COMMENT]
    assert len(comments) == 1, f"commented {len(comments)} times"


def test_the_park_reports_the_pr_and_dirty_worktree_the_silent_worker_left(tmp_path):
    """#787: the human the park hands the card to deserves more than the silence. A worker
    that hit its session limit mid-turn often left an open PR and uncommitted progress in
    its registered worktree; the engine can see both cheaply, so the comment says so."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "generation": "gen-1"}},
        linked={"store#5": LinkedPRs(open={"store#6": "abc123def456"})},
        pr_states={
            "store#6": PrState(ref="store#6", head="abc123def456", check_state="green")
        },
        dirty_counts={"/repo/.worktrees/issue-5": 3},
    )
    store = store_for(
        tmp_path, _wedged({"worktrees": ["/repo/.worktrees/issue-5"], "revives": 2})
    )

    run_wake(store, port, now=_NOW)

    said = " ".join(
        str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT
    )
    assert "store#6 is open at `abc123def`, checks green." in said
    assert "`/repo/.worktrees/issue-5` — 3 uncommitted file(s)" in said


def test_unreadable_evidence_never_blocks_the_park_itself(tmp_path):
    """Every extra read fails open: the card still lands on Needs More Info, and each line
    that could not be read says so instead of vanishing."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends={"store#5": {"observed": "ended", "generation": "gen-1"}},
        linked={"store#5": None},  # discovery incomplete this wake
        # No dirty_counts scripted: the registered worktree reads as unreadable.
    )
    store = store_for(
        tmp_path, _wedged({"worktrees": ["/repo/.worktrees/issue-5"], "revives": 2})
    )

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.reportless_ends == 1
    assert store.read()["issues"]["store#5"]["status"] == "Needs More Info"
    said = " ".join(
        str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT
    )
    assert "Linked pull requests could not be read this wake." in said
    assert "`/repo/.worktrees/issue-5` — state could not be read." in said


# --- reviving a worker that ended without reporting (#802, #835) ------------


def _killed(observed="ended", subtype="error_during_execution", account="claude_code:main"):
    """A turn-end receipt for a worker that was stopped rather than one that finished."""
    return {
        "store#5": {
            "observed": observed,
            "subtype": subtype,
            "generation": "gen-1",
            "account": account,
        }
    }


def test_a_worker_killed_mid_turn_is_restarted_instead_of_parked(tmp_path):
    """The failure this whole card exists for: five cards parked in one night because their
    worker hit a usage limit mid-turn, each waiting for a human to notice and retype it.

    An error result is not a decision anyone has to make — the work was interrupted — so the
    card is dispatched again under a fresh lease, keeping its instructions and its worktrees.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
    )
    store = store_for(tmp_path, _wedged({
        "worktrees": ["/repo/.worktrees/issue-5"],
        "run_session": {"session_url": "https://deck.example/sessions/dead", "generation": "gen-1"},
        "comment_links": {"pull_request_url": "https://github.com/example/store/pull/12"},
    }))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.revives == 1
    assert outcome.report.reportless_ends == 0, "a card the engine restarted is not a wedge"
    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress", "it is being worked again, not parked"
    assert rec["processing"] == ["9"], "the new worker is handed what the dead one was carrying"
    assert rec["worktrees"] == ["/repo/.worktrees/issue-5"], "its progress is still on disk"
    assert rec["revives"] == 1
    assert rec["generation"] != "gen-1", "a fresh lease; the dead one can resolve nothing"
    (directive,) = outcome.directives
    assert directive["action"] == "resume"
    assert directive["generation"] == rec["generation"]
    assert directive["continuation"] is True, "the new worker inherits an unreported turn"
    assert directive["previous_session_url"] == "https://deck.example/sessions/dead"
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "Restarting this card's worker** (1 of 2)" in said
    restart = next(e.value for e in port.applied if e.kind is EffectKind.COMMENT)
    assert isinstance(restart, CommentBody)
    assert restart.session_url == "", "the new generation must not link the dead worker"
    assert restart.pull_request_url.endswith("/pull/12"), "the card still owns the same PR"


def test_a_delayed_revive_still_records_which_worker_was_handed_off(tmp_path):
    """A refused board claim delays delivery, but the successor lease is already durable.

    The stale worker must therefore receive the same precise safe-stop answer as it would when
    the first board write landed. The ordinary dispatch retry owns delivery from here.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
        refuse={(EffectKind.SET_STATUS, "In progress")},
    )
    store = store_for(tmp_path, _wedged())

    refused = run_wake(store, port, now=_NOW)

    assert refused.directives == []
    rec = store.read()["issues"]["store#5"]
    assert rec["generation"] != "gen-1"
    assert rec["reportless_resolved"] == "gen-1"
    assert rec["dispatch"]["continuation"] is True

    port.refuse.clear()
    retried = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert [directive["action"] for directive in retried.directives] == ["resume"]
    assert store.read()["issues"]["store#5"]["reportless_resolved"] == "gen-1"


def _refused(account="codex:codex", why="seven_day window at 100% until 2026-09-11T04:48:00+00:00"):
    """A turn-end receipt for a delivery the account turned away before it ran (#1519)."""
    end = _killed(subtype="failed", account=account)["store#5"]
    end["refused"] = why
    return {"store#5": end}


def test_a_refused_turn_is_handed_out_again_without_spending_a_revive(tmp_path):
    """#1519: a turn the account would not run is not an attempt at the work.

    The card was parked In progress with an empty chat and no receipt at all. Once the
    receipt exists, the fix is that it must not burn the two revives a genuinely dying
    worker gets — nothing was tried, so nothing is any more likely to fail next time.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_refused(),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress", "handed out again, not parked with an empty chat"
    assert "refusals" not in rec, "there is no separate refusal count (#2159)"
    assert "revives" not in rec, "a refused turn is not an attempt at the work"
    (directive,) = outcome.directives
    assert directive["avoid_account"] == "codex:codex", (
        "the host walks the Overlay order without the account that refused it"
    )
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "turn was refused, not started**" in said
    assert "codex:codex could not run a turn at all" in said


def test_a_refused_turn_never_parks_the_card_even_past_the_old_refusal_bound(tmp_path):
    """#2159: one Run-attempt count, and a refusal is not an attempt. The retired refusal
    count is only cleared; the scheduler holds a card back while every account is blocked."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_refused(),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress"
    assert len(outcome.directives) == 1
    assert "revives" not in rec


def test_a_refused_turn_does_not_forgive_a_card_that_was_already_dying(tmp_path):
    """Attempts already spent stay spent: a refusal neither spends nor forgives one."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_refused(),
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["revives"] == 2, "untouched, not reset"
    assert "refusals" not in rec
    assert rec["status"] == "In progress"


def test_a_process_that_disappeared_without_any_result_is_a_death(tmp_path):
    """An OOM writes no result event at all, so the host reports `died` rather than `ended`."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(observed="died", subtype=""),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.revives == 1
    assert store.read()["issues"]["store#5"]["status"] == "In progress"


def test_a_reportless_revive_refreshes_a_behind_branch_before_dispatch(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
        pr_states={
            "store#7": PrState(ref="store#7", head="aaa", merge_state="BEHIND")
        },
    )
    store = store_for(tmp_path, _wedged())

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert held.report.revives == 0
    assert port.updated_branches == ["store#7"]
    assert store.read()["issues"]["store#5"].get("reportless_resolved") is None

    port.linked["store#5"] = LinkedPRs(open={"store#7": "bbb"})
    port._pr_states["store#7"] = PrState(ref="store#7", head="bbb", merge_state="CLEAN")
    revived = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert revived.report.revives == 1
    assert [directive["action"] for directive in revived.directives] == ["resume"]


@pytest.mark.parametrize("subtype", ["success", ""])
def test_a_turn_that_ended_quietly_is_continued_rather_than_parked(tmp_path, subtype):
    """#835: the ending #802 left behind. The turn reached its own end and said nothing, so
    nobody knows what it reached — but the worktree, the branch and any PR are still there,
    and a second worker can go and look at them. Parking meant a person did that looking.

    The card keeps everything the silent worker had, and the new dispatch is marked a
    continuation so the worker is told to check before it repeats any of the work.
    """
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(subtype=subtype),
    )
    store = store_for(tmp_path, _wedged({"worktrees": ["/repo/.worktrees/issue-5"]}))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.revives == 1
    assert outcome.report.reportless_ends == 0
    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "In progress"
    assert rec["processing"] == ["9"]
    assert rec["worktrees"] == ["/repo/.worktrees/issue-5"]
    assert rec["generation"] != "gen-1", "a fresh lease; the dead one can resolve nothing"
    (directive,) = outcome.directives
    assert directive["continuation"] is True
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "reached its own end" in said and "never reported" in said


def test_an_ordinary_dispatch_is_not_marked_a_continuation(tmp_path):
    """Telling a worker that is starting fresh work to expect somebody else's half-finished
    branch would be a lie about the card it was just handed."""
    from agentdeck.poller_engine import dispatch as dispatch_mod

    rec: dict = {}
    attempt = dispatch_mod.new_dispatch(rec, "work")
    directive = dispatch_mod.build_directive(rec, attempt, repo="store", number=5, ref="store#5")

    assert "continuation" not in attempt
    assert "continuation" not in directive


def test_a_usage_limit_death_keeps_the_configured_account_order(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
        full_accounts={"claude_code:main"},
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    (directive,) = outcome.directives
    assert "avoid_account" not in directive
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "usage ceiling" not in said


def test_an_oom_implicates_no_account_and_retries_where_it_is(tmp_path):
    """A kill says nothing about any account's budget, and moving off a healthy one would
    only spend the fallback's."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(observed="died", subtype=""),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    (directive,) = outcome.directives
    assert "avoid_account" not in directive


def test_a_card_that_keeps_dying_reaches_a_human(tmp_path):
    """Two revives, then the park — a card that cannot survive either account is not failing
    at random, and looping would burn both budgets on it forever."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
    )
    store = store_for(tmp_path, _wedged({"revives": 2}))

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.revives == 0
    assert outcome.report.reportless_ends == 1
    rec = store.read()["issues"]["store#5"]
    assert rec["status"] == "Needs More Info"
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "already restarted this card's work 2 time(s)" in said


def test_a_revive_cannot_fire_twice_on_the_same_dead_lease(tmp_path):
    """The receipt stays on disk and the board has not caught up, so the second wake reads
    the same ending again — and must see it already answered."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(),
    )
    store = store_for(tmp_path, _wedged())

    first = run_wake(store, port, now=_NOW)
    assert first.report.revives == 1

    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert second.report.revives == 0
    assert store.read()["issues"]["store#5"]["revives"] == 1


def test_an_ordinary_dispatch_forgets_the_revive_count(tmp_path):
    """A fresh instruction is fresh work, not a third attempt at the old one."""
    from agentdeck.poller_engine import revive

    rec = {"revives": 2}
    revive.reset(rec)

    assert revive.attempts(rec) == 0


def test_a_give_up_says_how_much_was_tried(tmp_path):
    """#482 follow-up: a bare "given up" reads like a decision, not a failure.

    #470 sat `Blocked` for four hours because a card parked for a transport failure
    looks exactly like one blocked on real work.
    """
    sent = (_NOW - timedelta(seconds=200)).isoformat()
    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    store = store_for(tmp_path, _dispatched(sent, attempts=3))

    outcome = run_wake(store, port, now=_NOW)

    line = next(entry for entry in outcome.log if "given up" in entry)
    assert "after 3 attempt(s), never acknowledged" in line, line
    assert "instructions kept" in line


# --- naming the pull request to release (#1445) ---

def test_a_merge_naming_a_pull_request_pins_the_release_to_it(tmp_path):
    """The recovery hatch: discovery linked two, the person meant one."""
    port = FakePort(
        comments=[_merge_comment(body="/merge store#7")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa", "store#9": "bbb"})},
    )
    store = store_for(tmp_path, _in_review())
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec["mode"] == "merge"
    assert rec["release_only"] == ["store#7"]


def test_a_bare_merge_after_a_named_one_goes_back_to_the_whole_set(tmp_path):
    """A pin outliving its command would quietly narrow every later release on the card."""
    port = FakePort(
        comments=[_merge_comment()],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa", "store#9": "bbb"})},
    )
    store = store_for(tmp_path, _in_review({"release_only": ["store#7"]}))
    run_wake(store, port, now=_NOW)
    assert "release_only" not in store.read()["issues"]["store#5"]


def test_a_merge_naming_a_pull_request_that_does_not_exist_is_refused(tmp_path):
    """Releasing the rest instead would ship less than was asked for and report success."""
    port = FakePort(
        comments=[_merge_comment(body="/merge store#404")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
    )
    store = store_for(tmp_path, _in_review())
    run_wake(store, port, now=_NOW)
    rec = store.read()["issues"]["store#5"]
    assert rec.get("mode") is None
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any("store#404" in body and "not an open pull request" in body for body in comments)


def test_a_merge_naming_a_withheld_pull_request_is_refused(tmp_path):
    """Pinning a draft would recreate the very loop this fixes: GitHub cannot merge one."""
    port = FakePort(
        comments=[_merge_comment(body="/merge store#9")],
        snapshots={"store#5": CardSnapshot(status="In review")},
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"}, withheld=["store#9"])},
    )
    store = store_for(tmp_path, _in_review())
    run_wake(store, port, now=_NOW)
    assert store.read()["issues"]["store#5"].get("mode") is None
    comments = [str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT]
    assert any("store#9" in body and "draft" in body for body in comments)


# --- a paused card (#2548) ---------------------------------------------------


def _paused_notices(port):
    return [
        effect for effect in port.applied
        if effect.kind is EffectKind.COMMENT and "**Paused.**" in str(effect.value)
    ]


def test_a_paused_card_starts_nothing_says_so_once_and_starts_when_resumed(tmp_path):
    port = FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo", paused=True)},
    )
    store = store_for(tmp_path, {
        "ownership": {
            "initialized": True,
            "events": {},
            "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
        }
    })

    first = run_wake(store, port, now=_NOW)
    second = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert first.directives == [] and second.directives == []
    assert len(_paused_notices(port)) == 1, "said once per pause, not once per wake"
    rec = store.read()["issues"]["store#7"]
    assert rec["paused"] is True
    assert rec["manual_start"] is True, "the start is kept for when the pause ends"

    port.snapshots["store#7"] = CardSnapshot(status="Todo")
    resumed = run_wake(store, port, now=_NOW + timedelta(minutes=2))

    assert [directive["ref"] for directive in resumed.directives] == ["store#7"]
    rec = store.read()["issues"]["store#7"]
    assert "paused" not in rec and "paused_notice" not in rec


def test_a_paused_card_holds_a_queued_instruction_until_resumed(tmp_path):
    port = FakePort(
        comments=[comment(2, "/claude do the thing")],
        snapshots={"store#5": CardSnapshot(status="In review", paused=True)},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "In review", "started": True}}})

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert store.read()["issues"]["store#5"]["pending"] == ["2"]
    port.comments = []
    port.snapshots["store#5"] = CardSnapshot(status="In review")
    resumed = run_wake(store, port, now=_NOW + timedelta(minutes=1))
    assert [payloads(directive) for directive in resumed.directives] == [["do the thing"]]


def test_a_paused_card_is_not_revived_and_is_once_resumed(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress", paused=True)},
        turn_ends=_killed(),
    )
    store = store_for(tmp_path, _wedged())

    held = run_wake(store, port, now=_NOW)

    assert held.directives == []
    assert held.report.revives == 0
    assert any("not revived, paused" in line for line in held.log)
    assert "revives" not in store.read()["issues"]["store#5"]

    port.snapshots["store#5"] = CardSnapshot(status="In progress")
    resumed = run_wake(store, port, now=_NOW + timedelta(minutes=1))
    assert resumed.report.revives == 1
    assert [directive["action"] for directive in resumed.directives] == ["resume"]


def test_a_paused_release_waits_but_one_already_armed_goes_on(tmp_path):
    initial = {
        "issues": {
            "store#5": {"status": "In review", "mode": "merge"},
            "store#6": {"status": "In progress", "mode": "merge",
                        "awaiting_merge": {"prs": ["store#8"], "premerged": {},
                                           "since": _NOW.isoformat()}},
        }
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="In review", paused=True),
            "store#6": CardSnapshot(status="In progress", paused=True),
        },
        linked={"store#5": LinkedPRs(open={"store#7": "aaa"})},
    )
    store = store_for(tmp_path, initial)

    outcome = run_wake(store, port, now=_NOW)

    assert "release: store#5 held, paused" in outcome.log
    assert "release: store#6 held, paused" not in outcome.log
    assert store.read()["issues"]["store#5"]["mode"] == "merge"


# --- a usage limit, a deck Continue, then a wake (#2862) -------------------------------------


def test_a_turn_the_usage_limit_cut_short_is_told_as_one(tmp_path):
    """The CLI calls that turn a success; the card must not say it reached its own end."""
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="In progress")},
        turn_ends=_killed(subtype=revive.USAGE_LIMIT),
    )
    store = store_for(tmp_path, _wedged())

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.report.revives == 1
    said = " ".join(str(e.value) for e in port.applied if e.kind is EffectKind.COMMENT)
    assert "its account hit its usage limit mid-turn" in said
    assert "reached its own end" not in said


def test_a_card_the_deck_continued_after_a_usage_limit_gets_no_second_worker(tmp_path):
    """#2861: the worker hit its usage limit, a person pressed Continue in new chat on another
    account, and the next wake read the old leg's "success" and revived the card beside the
    continued chat. The wake now reads the Runtime's real outcome, which follows the Continue."""
    rt = runtime_fakes
    db = rt._alt_db(tmp_path)
    alt, main = rt.Host({rt.KEY: rt._limited()}), rt.Host({})
    executions = rt._two_accounts(tmp_path, alt, main, db)

    asked: list[dict | None] = []

    class Runtime(dict):
        """``turn_ended`` through the real Runtime outcome and the engine's own reading of it."""

        def get(self, key, default=None):
            asked.append(revive.observation(executions.outcome(rt.WORK, "run-1").as_json()))
            return asked[-1]

    def running() -> list[str]:
        return [
            key
            for host in (alt, main)
            for key, row in host.workers.items()
            if row.get("turn_active")
        ]

    port = FakePort(snapshots={"store#5": CardSnapshot(status="In progress")})
    port.turn_ends = Runtime()
    store = store_for(tmp_path, _wedged())
    # The limit ended the turn, then the deck continued the chat on the other account.
    rt._continue(db)
    main.workers["deck-chat"] = rt._continuation()

    first = run_wake(store, port, now=_NOW)

    assert asked == [None], "the wake asked, and the Runtime proved no ending"
    assert first.directives == [] and first.report.revives == 0
    assert store.read()["issues"]["store#5"]["generation"] == "gen-1"
    assert running() == ["deck-chat"], "one worker: the continued chat, and nothing beside it"

    # The continued chat ends without reporting: now, and only now, the card is restarted.
    main.workers["deck-chat"].update(
        turn_active=False, effective_activity=False,
        last_result_at=rt.T0 + 200, last_result_subtype="success",
    )
    second = run_wake(store, port, now=_NOW + timedelta(minutes=1))

    assert second.report.revives == 1
    assert [directive["action"] for directive in second.directives] == ["resume"]
