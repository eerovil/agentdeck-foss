"""Slice 12 of #176: the independent command ledger — accepted commands survive state loss."""

from __future__ import annotations

import json
from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, payloads, run_wake, store_for

from agentdeck.poller_engine import ledger, queue
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.state import EnginePaths, StateStore


def _control(cid="9", *, kind="claude", status="queued", number=5, **extra):
    record = {
        "id": cid, "repo": "store", "number": number, "kind": kind, "payload": "go",
        "url": f"https://example/{cid}", "author": "eero", "status": status,
        "created_at": f"t{cid}",
    }
    record.update(extra)
    return record


# --- the ledger payload ---

def test_the_ledger_carries_commands_the_cursor_and_enough_card_context():
    state = {
        "controls": {"9": _control()},
        "control_since": "2026-07-25T11:00:00+00:00",
        "issues": {
            "store#5": {
                "status": "In progress", "kind": "pr", "branch": "fix/x",
                "worktrees": ["/w/5"], "review_heads": {"store#7": "aaa"},
                "review_diffs": {"store#7": "diff-1"},
                "review_trees": {"store#7": "tree-1"},
                "generation": "gen1", "dispatch": {"id": "d1"},
            }
        },
    }
    snapshot = ledger.snapshot(state)
    assert snapshot["controls"]["9"]["id"] == "9"
    assert snapshot["control_since"] == "2026-07-25T11:00:00+00:00"
    context = snapshot["issue_context"]["store#5"]
    assert context["kind"] == "pr" and context["branch"] == "fix/x"
    assert context["review_heads"] == {"store#7": "aaa"}
    assert context["review_diffs"] == {"store#7": "diff-1"}
    assert context["review_trees"] == {"store#7": "tree-1"}
    # The volatile lease and dispatch are deliberately not carried: they are re-derived.
    assert "generation" not in context and "dispatch" not in context


def test_the_ledger_wins_over_a_rolled_back_state():
    state = {"controls": {"9": _control(status="new")}, "control_since": "old"}
    stored = {"controls": {"9": _control(status="processing")}, "control_since": "newer"}
    ledger.merge(state, stored)
    assert state["controls"]["9"]["status"] == "processing"
    assert state["control_since"] == "newer"


def test_the_ledger_carries_manual_proposals_over_a_state_rollback():
    proposal = {
        "extension": "sentry-autofix",
        "kind": "open-work",
        "payload": {"repo": "issues", "key": "WEB-7"},
    }
    snapshot = ledger.snapshot({"proposal_inbox": [proposal]})
    rolled_back = {}

    ledger.merge(rolled_back, snapshot)

    assert rolled_back["proposal_inbox"] == [proposal]


def test_an_empty_current_ledger_clears_a_stale_inbox_from_state_backup():
    rolled_back = {"proposal_inbox": [{"extension": "stale"}]}

    ledger.merge(rolled_back, ledger.snapshot({}))

    assert "proposal_inbox" not in rolled_back


def test_merging_an_empty_ledger_adds_nothing():
    state: dict = {}
    assert ledger.merge(state, {}) == {}
    assert state == {}


# --- recovery ---

def test_a_queued_instruction_is_reattached_to_a_lost_card():
    state = {"controls": {"9": _control(status="queued")}}
    recovered = ledger.recover(state, {}, now=_NOW)
    assert recovered == ["store#5"]
    rec = state["issues"]["store#5"]
    assert rec["pending"] == ["9"]
    assert rec["status"] == "Todo"


def test_an_in_flight_instruction_is_restarted_rather_than_left_to_age_out():
    state = {"controls": {"9": _control(status="processing")}}
    ledger.recover(state, {"store#5": {"kind": "pr", "branch": "fix/x"}}, now=_NOW)
    rec = state["issues"]["store#5"]
    assert rec["pending"] == ["9"]
    assert rec["recovery_action"] == "pr"      # its PR context survived
    assert rec["branch"] == "fix/x"
    assert rec["started"] is True


def test_recovery_keeps_the_card_context_the_ledger_carried():
    state = {"controls": {"9": _control(status="processing")}}
    context = {"store#5": {"kind": "pr", "branch": "fix/x", "worktrees": ["/w/5"],
                           "session_id": "s1", "repos": ["store"]}}
    ledger.recover(state, context, now=_NOW)
    rec = state["issues"]["store#5"]
    assert rec["worktrees"] == ["/w/5"]
    assert rec["session_id"] == "s1"


def test_recovery_is_idempotent_across_wakes():
    state = {"controls": {"9": _control(status="queued")}}
    ledger.recover(state, {}, now=_NOW)
    ledger.recover(state, {}, now=_NOW)
    assert state["issues"]["store#5"]["pending"] == ["9"]


def test_a_command_already_on_its_card_is_left_alone():
    state = {
        "controls": {"9": _control(status="processing")},
        "issues": {"store#5": {"status": "In progress", "processing": ["9"]}},
    }
    assert ledger.recover(state, {}, now=_NOW) == []
    rec = state["issues"]["store#5"]
    assert rec["processing"] == ["9"]
    assert rec.get("pending") in (None, [])


def test_finished_commands_are_never_resurrected():
    for status in ("new", "done", "cancelled", "rejected"):
        state = {"controls": {"9": _control(status=status)}}
        assert ledger.recover(state, {}, now=_NOW) == []
        assert state.get("issues") in (None, {})


def test_a_review_pass_with_its_snapshot_resumes_the_release():
    state = {"controls": {"31": _control("31", kind="merge-review", status="processing",
                                         review_passed=True)}}
    context = {"store#5": {"review_heads": {"store#7": "aaa"}}}
    ledger.recover(state, context, now=_NOW)
    rec = state["issues"]["store#5"]
    assert rec["mode"] == "merge"
    assert rec["merge_control"] == "31"
    assert rec["review_heads"] == {"store#7": "aaa"}


def test_a_review_pass_without_its_snapshot_is_re_run_not_trusted():
    state = {"controls": {"31": _control("31", kind="merge-review", status="processing",
                                         review_passed=True)}}
    ledger.recover(state, {}, now=_NOW)
    rec = state["issues"]["store#5"]
    control = state["controls"]["31"]
    # Merging code nobody can prove was approved is the one unrecoverable mistake.
    assert rec["mode"] == "merge-review"
    assert rec["recovery_action"] == "merge-review"
    assert "review_passed" not in control
    assert control["status"] == "queued"


def test_stale_review_recovery_does_not_overwrite_an_active_merge_watch():
    watch = {"prs": ["store#7"], "premerged": {}, "since": _NOW.isoformat()}
    state = {
        "controls": {
            "31": _control("31", kind="merge-review", status="processing"),
        },
        "issues": {
            "store#5": {
                # The stale handback already cleared these release fields, but failed to clear
                # the watch. Recovery must trust the later watch, not rerun the old review.
                "status": "In review",
                "awaiting_merge": watch,
            }
        },
    }

    ledger.recover(state, {}, now=_NOW)

    rec = state["issues"]["store#5"]
    assert rec["mode"] == "merge"
    assert rec["status"] == "In progress"
    assert rec["merge_control"] == "31"
    assert rec["awaiting_merge"] == watch
    assert state["controls"]["31"]["status"] == "processing"


def test_release_and_stop_commands_are_re_gated_from_the_live_world():
    for kind in ("merge", "stop"):
        state = {"controls": {"7": _control("7", kind=kind, status="processing")}}
        ledger.recover(state, {}, now=_NOW)
        assert state["controls"]["7"]["status"] == "new"


# --- the store publishes the ledger first and reads it back ---

def _store(tmp_path) -> StateStore:
    return StateStore(EnginePaths.from_base(tmp_path / "state.json"))


def test_the_ledger_is_published_alongside_the_state(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as state:
        state["controls"] = {"9": _control()}
        state["issues"] = {"store#5": {"status": "Todo", "pending": ["9"]}}
    stored = json.loads(store.paths.controls.read_text())
    assert stored["controls"]["9"]["id"] == "9"
    assert stored["issue_context"]["store#5"]["status"] == "Todo"


def test_a_lost_state_file_still_yields_the_accepted_command(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as state:
        state["controls"] = {"9": _control(status="processing")}
        state["control_since"] = "2026-07-25T11:00:00+00:00"
        state["issues"] = {"store#5": {"status": "In progress", "kind": "pr",
                                       "processing": ["9"], "branch": "fix/x"}}
    store.paths.state.unlink()
    store.paths.backup.unlink(missing_ok=True)

    recovered = store.read()
    assert recovered["controls"]["9"]["status"] == "queued"
    assert queue.get_cursor(recovered) == "2026-07-25T11:00:00+00:00"
    rec = recovered["issues"]["store#5"]
    assert rec["pending"] == ["9"]
    assert rec["kind"] == "pr" and rec["branch"] == "fix/x"
    assert store.last_load.recovered == ("store#5",)
    assert store.last_load.used_backup is True


def test_a_healthy_load_reports_no_recovery(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as state:
        state["controls"] = {"9": _control(status="queued")}
        state["issues"] = {"store#5": {"status": "Todo", "pending": ["9"]}}
    store.read()
    assert store.last_load.recovered == ()
    assert store.last_load.used_backup is False


# --- through a whole wake ---

def test_state_loss_restarts_the_lost_work_on_the_next_wake(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(comments=[comment(1, "/claude fix the header")])
    first = run_wake(store, port, now=_NOW)
    assert len(first.directives) == 1

    # The worker-state generation is gone; only the command ledger survives.
    store.paths.state.unlink()
    store.paths.backup.unlink(missing_ok=True)

    # The board still says In progress: the worker was mid-flight when the state was lost.
    port.comments = []
    port.snapshots["store#5"] = CardSnapshot(status="In progress")
    second = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    # The instruction was never lost and restarts immediately under a fresh lease.
    assert len(second.directives) == 1
    assert payloads(second.directives[0]) == ["fix the header"]
    assert second.directives[0]["generation"] != first.directives[0]["generation"]
    assert second.report.state_fallback is True
    assert any("recovered from the command ledger" in line for line in second.log)


def test_a_released_card_leaves_nothing_the_ledger_can_resurrect(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(comments=[comment(1, "/claude go")])
    run_wake(store, port, now=_NOW)

    # The card is released while its instruction is still in flight.
    port.comments = []
    port.snapshots["store#5"] = CardSnapshot(open=False)
    run_wake(store, port, now=_NOW + timedelta(seconds=30))
    state = store.read()
    assert "store#5" not in state.get("issues", {})
    assert state["controls"]["1"]["status"] == "cancelled"
    assert port.ownership_edits() == []  # the label stays on (#2497)

    # A third wake must not resurrect it from the ledger.
    third = run_wake(store, port, now=_NOW + timedelta(seconds=60))
    assert "store#5" not in store.read().get("issues", {})
    assert third.directives == []
