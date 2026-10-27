"""Slice 6 of #176: lifecycle reconciliation, stale detection, cold-reload, retryable cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.poller_engine import leases, reconcile
from agentdeck.poller_engine.reconcile import CardSnapshot, ReconcileDecision

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_STALE = reconcile.STALE_SECONDS


def _decide(rec: dict, snapshot: CardSnapshot, **kwargs) -> ReconcileDecision:
    return reconcile.reconcile_card(rec, snapshot, now=_NOW, **kwargs)


# --- closed / done / release ---

def test_closed_issue_is_cleaned_up():
    rec = {"status": "Todo"}
    assert _decide(rec, CardSnapshot(open=False)) is ReconcileDecision.CLEANUP


def test_closed_issue_keeps_a_running_worker_alive_until_it_reports():
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(open=False, status="In progress")
    assert _decide(rec, snapshot) is ReconcileDecision.ACTIVE


def test_closed_issue_is_cleaned_up_once_its_workers_turn_is_over():
    """A card closed by hand under a resumed chat stayed In progress for good (#2822)."""
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(open=False, status="In progress", run_ended=True)
    assert _decide(rec, snapshot) is ReconcileDecision.CLEANUP


def test_closed_issue_accepts_a_running_workers_terminal_marker():
    rec = {"status": "In progress"}
    snapshot = CardSnapshot(
        open=False, status="In progress", terminal_marker="In review"
    )
    assert _decide(rec, snapshot) is ReconcileDecision.TERMINAL_RECOVERED


def _resolved(at: datetime = _NOW, **extra) -> dict:
    """A card its own worker closed with ``resolved`` (#2522)."""
    return {
        "status": "Done",
        "generation": "gen-1",
        "resolved_finish": {"generation": "gen-1", "at": at.isoformat()},
        **extra,
    }


def test_a_card_closed_by_its_own_resolved_is_held_for_reopen():
    """Its worker may still be asked, in its chat, to carry on (#2522)."""
    closed = CardSnapshot(open=False, status="Done", terminal_marker="Done")
    assert _decide(_resolved(), closed) is ReconcileDecision.IDLE
    almost = _resolved(_NOW - reconcile.REOPEN_HOLD + timedelta(seconds=1))
    assert _decide(almost, closed) is ReconcileDecision.IDLE


def test_the_reopen_hold_ends_and_the_card_is_released():
    closed = CardSnapshot(open=False, status="Done", terminal_marker="Done")
    late = _resolved(_NOW - reconcile.REOPEN_HOLD)
    assert _decide(late, closed) is ReconcileDecision.CLEANUP


def test_the_reopen_hold_is_only_for_the_lease_that_closed_it_and_only_in_done():
    closed = CardSnapshot(open=False, status="Done")
    other = _resolved(generation="gen-2")
    assert _decide(other, closed) is ReconcileDecision.CLEANUP
    moved = CardSnapshot(open=False, status="Todo")
    assert _decide(_resolved(), moved) is ReconcileDecision.CLEANUP
    garbled = _resolved()
    garbled["resolved_finish"]["at"] = "not a time"
    assert _decide(garbled, closed) is ReconcileDecision.CLEANUP


def test_a_removed_label_or_a_pending_forget_still_releases_a_held_card():
    unowned = CardSnapshot(open=False, status="Done", owned=False)
    assert _decide(_resolved(label_claim=True), unowned) is ReconcileDecision.CLEANUP
    closed = CardSnapshot(open=False, status="Done")
    assert (
        _decide(_resolved(forget_pending=True), closed) is ReconcileDecision.CLEANUP_RETRY
    )


def test_a_persons_own_instruction_or_start_releases_a_held_card():
    """A new Run is theirs to start, and a held Done card would refuse to start it."""
    closed = CardSnapshot(open=False, status="Done")
    for extra in ({"pending": ["c1"]}, {"manual_start": True}, {"recovery_action": "x"}):
        assert _decide(_resolved(**extra), closed) is ReconcileDecision.CLEANUP, extra


def test_a_held_card_reopened_by_hand_is_released_as_before():
    """A person reopening the issue leaves the ``resolved`` marker newest: released."""
    reopened = CardSnapshot(open=True, status="Done", terminal_marker="Done")
    assert _decide(_resolved(), reopened) is ReconcileDecision.RELEASED_DONE


def test_a_reopen_caught_between_the_issue_and_the_board_is_held_for_its_repeat():
    """``reopen`` reopens the issue before it moves the board (#2522 D3). Between the two, its
    ``resumed`` comment is the newest marker, and the card waits for the worker's repeat."""
    between = CardSnapshot(open=True, status="Done", terminal_marker=None)
    assert _decide(_resolved(), between) is ReconcileDecision.IDLE
    late = _resolved(_NOW - reconcile.REOPEN_HOLD)
    assert _decide(late, between) is ReconcileDecision.RELEASED_DONE
    unowned = CardSnapshot(open=True, status="Done", owned=False)
    assert _decide(_resolved(label_claim=True), unowned) is not ReconcileDecision.IDLE


def test_done_status_releases_the_card():
    rec = {"status": "Todo"}
    assert _decide(rec, CardSnapshot(status="Done")) is ReconcileDecision.RELEASED_DONE


def test_armed_release_is_retried_before_anything_else():
    rec = {"status": "Todo", "release_pending": True}
    assert _decide(rec, CardSnapshot(owned=False)) is ReconcileDecision.IDLE_RELEASE


# --- worker end / terminal marker ---

def test_status_moved_without_terminal_marker_holds_the_card():
    rec = {"status": "In progress"}
    decision = _decide(rec, CardSnapshot(status="In review"))
    assert decision is ReconcileDecision.AWAIT_TERMINAL
    # The record is untouched: the worker may still be writing.
    assert rec["status"] == "In progress"


def test_status_moved_without_terminal_marker_keeps_holding_inside_the_stale_window():
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(status="In review", quiet_seconds=_STALE)
    assert _decide(rec, snapshot) is ReconcileDecision.AWAIT_TERMINAL
    assert rec["status"] == "In progress"


def test_await_terminal_releases_an_open_card_once_the_runtime_says_its_run_ended():
    # #1091: holding is the right first read, but it lasts only until the Runtime reports
    # the Run's execution over (#2159) — not until the issue has been quiet for long enough.
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(status="In review", run_ended=True)
    assert _decide(rec, snapshot) is ReconcileDecision.WORKER_ENDED
    assert rec["status"] == "In review"


def test_github_quiet_age_alone_never_declares_the_worker_gone():
    """#2159 test 10: however long the issue is silent, only the Runtime ends a Run."""
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(status="In review", quiet_seconds=_STALE * 10)
    assert _decide(rec, snapshot) is ReconcileDecision.AWAIT_TERMINAL
    running = {"status": "In progress", "generation": "gen-1"}
    busy = CardSnapshot(status="In progress", quiet_seconds=_STALE * 10)
    assert _decide(running, busy) is ReconcileDecision.ACTIVE


def test_await_terminal_cleans_up_a_closed_card_once_its_run_ended():
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(open=False, status="Done", run_ended=True)
    assert _decide(rec, snapshot) is ReconcileDecision.CLEANUP


def test_await_terminal_still_holds_a_closed_card_that_moved_moments_ago():
    rec = {"status": "In progress", "generation": "gen-1"}
    snapshot = CardSnapshot(open=False, status="Done", quiet_seconds=60.0)
    assert _decide(rec, snapshot) is ReconcileDecision.AWAIT_TERMINAL
    assert rec["status"] == "In progress"


def test_status_moved_with_terminal_marker_finishes_the_card():
    rec = {"status": "In progress"}
    snapshot = CardSnapshot(status="In review", terminal_marker="In review")
    assert _decide(rec, snapshot) is ReconcileDecision.WORKER_ENDED
    assert rec["status"] == "In review"


def test_terminal_marker_while_still_in_progress_recovers_the_lost_status_write():
    rec = {"status": "In progress"}
    snapshot = CardSnapshot(status="In progress", terminal_marker="In review")
    assert _decide(rec, snapshot) is ReconcileDecision.TERMINAL_RECOVERED
    # The caller publishes the marker's status; nothing is adopted blindly here.
    assert rec["status"] == "In progress"


# --- stop protocol integration ---

def test_stopping_card_released_on_the_runtimes_proof():
    rec = {"status": "In progress", "stopping": True, "stop_waiting_generation": "g1"}
    snapshot = CardSnapshot(status="In progress", execution_stopped=True)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_RELEASED


def test_stopping_card_waits_until_the_runtime_proves_the_stop():
    rec = {"status": "In progress", "stopping": True, "stop_waiting_generation": "g1"}
    snapshot = CardSnapshot(status="In progress", quiet_seconds=_STALE - 1)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_WAITING


def test_a_dead_worker_is_not_released_by_the_clock_but_by_the_runtime():
    """#2159: a dead worker needs no stop-ack and no stale window. The Runtime stops it and
    the host records that; until then, however quiet the card, the stop waits."""
    rec = {"status": "In progress", "stopping": True, "stop_waiting_generation": "g1"}
    quiet = CardSnapshot(status="In progress", quiet_seconds=_STALE * 10)
    assert _decide(rec, quiet) is ReconcileDecision.STOP_WAITING
    proved = CardSnapshot(status="In progress", execution_stopped=True)
    assert _decide(rec, proved) is ReconcileDecision.STOP_RELEASED


def test_stopping_card_whose_status_already_moved_is_released():
    rec = {"status": "In progress", "stopping": True, "stop_waiting_generation": "g1"}
    snapshot = CardSnapshot(status="Todo", execution_stopped=True)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_RELEASED
    assert rec["status"] == "Todo"


# --- ownership label removed by hand ---

def test_seeing_the_label_records_the_claim_it_can_later_be_handed_back_by():
    # The engine no longer writes the label, so what makes its absence meaningful is having
    # seen it there (#2053).
    rec = {"status": "Todo"}
    reconcile.reconcile_card(rec, CardSnapshot(owned=True), now=_NOW)
    assert rec["label_claim"] is True


def test_a_card_that_never_wore_the_label_is_not_handed_back_by_its_absence():
    rec = {"status": "In progress", "started": True}
    decision = reconcile.reconcile_card(
        rec, CardSnapshot(owned=False, status="In progress", quiet_seconds=1), now=_NOW
    )
    assert decision is ReconcileDecision.ACTIVE
    assert "stopping" not in rec


def test_label_removed_on_active_card_arms_a_safe_stop():
    # `label_claim` is what reconciliation recorded when it last saw the label on the issue,
    # so its absence now is a person taking the card back rather than a card that never wore
    # one (#2053).
    rec = {"status": "In progress", "label_claim": True}
    old = leases.bump(rec)
    snapshot = CardSnapshot(owned=False, status="In progress", quiet_seconds=1)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_WAITING
    assert rec["stopping"] is True
    assert not leases.holds(rec, old)  # the worker is invalidated immediately


def test_label_removed_card_waits_for_the_runtime_however_quiet_it_is():
    rec = {"status": "In progress", "label_claim": True, "generation": "g1"}
    snapshot = CardSnapshot(owned=False, status="In progress", quiet_seconds=_STALE + 1)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_WAITING


def test_a_stop_awaiting_no_generation_releases_instead_of_holding_forever():
    """The un-awaitable class (#2111): a stop armed while the record held no generation
    awaits the empty string: no Run ever started, so there is nothing for the Runtime to
    stop. It held a worker slot for thirteen days on #1608/#1749."""
    rec = {
        "status": "In progress",
        "stopping": True,
        "stop_waiting_generation": "",
    }
    snapshot = CardSnapshot(status="In progress", quiet_seconds=5)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_RELEASED


def test_stopping_card_drops_its_outstanding_dispatch():
    rec = {"status": "In progress", "stopping": True, "dispatch": {"id": "d1"}}
    assert reconcile.abandon_dispatch(rec) == {"id": "d1"}
    assert "dispatch" not in rec


def test_a_running_card_keeps_its_dispatch():
    rec = {"status": "In progress", "dispatch": {"id": "d1"}}
    assert reconcile.abandon_dispatch(rec) is None
    assert rec["dispatch"] == {"id": "d1"}


def test_label_removed_on_idle_card_arms_an_idle_release():
    rec = {"status": "Todo", "started": True, "label_claim": True}
    assert _decide(rec, CardSnapshot(owned=False)) is ReconcileDecision.IDLE_RELEASE
    assert rec["release_pending"] is True


# --- a card that has not started yet has never worn the label (#1663) ---

def test_unlabelled_card_held_by_a_blocker_is_kept_rather_than_released():
    # A card the blocker gate is holding has never worn the label. Releasing it cancelled the
    # queued brief and forgot what was blocking it.
    rec = {"status": "Todo", "pending": ["engine:ready-review:agentdeck#1649"]}
    decision = _decide(rec, CardSnapshot(owned=False, blockers=("agentdeck#1637",)))
    assert decision is ReconcileDecision.IDLE
    assert "release_pending" not in rec
    assert rec["pending"] == ["engine:ready-review:agentdeck#1649"]


def test_unlabelled_card_deferred_by_the_worker_cap_is_kept_too():
    rec = {"status": "Todo", "pending": ["c1"]}
    assert _decide(rec, CardSnapshot(owned=False)) is ReconcileDecision.IDLE


def test_unlabelled_card_with_nothing_queued_is_still_released():
    rec = {"status": "Todo", "manual_start": True, "label_claim": True}
    assert _decide(rec, CardSnapshot(owned=False)) is ReconcileDecision.IDLE_RELEASE


def test_label_removed_after_the_card_started_still_releases_it():
    # A person really did hand this one back; a queue left over from its run is not a reason
    # to keep it.
    rec = {"status": "Todo", "started": True, "pending": ["c1"], "label_claim": True}
    assert _decide(rec, CardSnapshot(owned=False)) is ReconcileDecision.IDLE_RELEASE


# --- recovered command with no worker ---

def test_recovered_command_without_dispatch_restarts_immediately():
    rec = {"status": "In progress", "recovery_action": "resume"}
    snapshot = CardSnapshot(status="In progress", quiet_seconds=10)
    assert _decide(rec, snapshot) is ReconcileDecision.RECOVER_LOST_WORKER
    # Reset to Todo so it re-dispatches now instead of ageing out the stale window.
    assert rec["status"] == "Todo"


def test_recovered_command_with_a_live_dispatch_is_left_alone():
    rec = {"status": "In progress", "recovery_action": "resume", "dispatch": {"id": "d1"}}
    snapshot = CardSnapshot(status="In progress", quiet_seconds=10)
    assert _decide(rec, snapshot) is ReconcileDecision.ACTIVE


# --- stale detection ---

def test_in_progress_card_is_active_however_long_it_has_been_quiet():
    """No cold reload: a quiet issue is not a dead worker (#2159). A wedged worker is the
    Runtime's to report, and that report arrives through the reportless path."""
    for quiet in (_STALE - 1, _STALE + 1):
        rec = {"status": "In progress"}
        snapshot = CardSnapshot(status="In progress", quiet_seconds=quiet)
        assert _decide(rec, snapshot) is ReconcileDecision.ACTIVE
    assert not hasattr(ReconcileDecision, "COLD_RELOAD")


def test_idle_owned_card_is_idle():
    assert _decide({"status": "Todo"}, CardSnapshot()) is ReconcileDecision.IDLE


# --- the release state machine's cards are not this module's business ---

def test_merge_modes_are_gated_to_the_release_state_machine():
    for mode in ("merge", "merge-review"):
        rec = {"status": "In progress", "mode": mode}
        snapshot = CardSnapshot(status="In progress", quiet_seconds=_STALE + 1)
        assert _decide(rec, snapshot) is ReconcileDecision.RELEASE_GATED


def test_a_stopping_merge_card_still_follows_the_stop_protocol():
    rec = {
        "status": "In progress",
        "mode": "merge-review",
        "stopping": True,
        "stop_waiting_generation": "g1",
    }
    snapshot = CardSnapshot(status="In progress", execution_stopped=True)
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_RELEASED


# --- retryable worktree cleanup (#106 gap 2) ---

def test_worktrees_are_registered_once_and_returned_for_cleanup():
    rec: dict = {}
    reconcile.register_worktree(rec, "/repo/.worktrees/issue-7")
    reconcile.register_worktree(rec, "/repo/.worktrees/issue-7")
    reconcile.register_worktree(rec, "/other/.worktrees/issue-7")
    assert reconcile.worktrees(rec) == ["/repo/.worktrees/issue-7", "/other/.worktrees/issue-7"]
    assert reconcile.begin_cleanup(rec) == reconcile.worktrees(rec)
    assert rec["forget_pending"] is True


def _ostree_worktree(tmp_path):
    """The ostree host layout the card is about: `home` is a symlink to `var/home`.

    On a plain Linux box `/home` is a real directory, so the two spellings are identical and
    the defect cannot be reproduced. Building the symlink here is what makes the test real.
    """
    real = tmp_path / "var" / "home" / "eero" / "repo" / ".worktrees" / "issue-7"
    real.mkdir(parents=True)
    (tmp_path / "home").symlink_to(tmp_path / "var" / "home")
    alias = tmp_path / "home" / "eero" / "repo" / ".worktrees" / "issue-7"
    assert str(alias) != str(real)
    return str(real), str(alias)


def test_one_worktree_handed_over_twice_is_recorded_once_however_it_is_spelled(tmp_path):
    """#1095: `/home/...` and `/var/home/...` are the same directory on an ostree host.

    Recorded verbatim they became two entries for one tree, and anything comparing those
    strings later — a dedup, a reaper's claimed set — reads the spelling it was not given as
    unclaimed. That is how a live worker's uncommitted work gets deleted.
    """
    real, alias = _ostree_worktree(tmp_path)
    rec: dict = {}

    reconcile.register_worktree(rec, alias)
    reconcile.register_worktree(rec, real)

    assert rec["worktrees"] == [real]
    assert reconcile.worktrees(rec) == [real]


def test_a_record_already_holding_both_spellings_reads_back_as_one_path(tmp_path):
    """The entries a live state.json already carries are fixed on the way out, not migrated."""
    real, alias = _ostree_worktree(tmp_path)
    rec = {"worktrees": [alias, real]}

    assert reconcile.worktrees(rec) == [real]
    assert reconcile.begin_cleanup(rec) == [real]


def test_registering_a_worktree_normalises_the_spellings_already_on_the_record(tmp_path):
    real, alias = _ostree_worktree(tmp_path)
    rec = {"worktrees": [alias]}

    reconcile.register_worktree(rec, real)

    assert rec["worktrees"] == [real]


def test_a_late_worktree_registration_is_bound_to_the_active_generation():
    state = {"issues": {"store#7": {"generation": "current"}}}

    assert reconcile.register_generation_worktrees(
        state, "store", 7, "current", ["/repo/.worktrees/issue-7"]
    ) is True
    assert reconcile.worktrees(state["issues"]["store#7"]) == [
        "/repo/.worktrees/issue-7"
    ]


def test_a_superseded_generation_cannot_register_a_worktree():
    state = {"issues": {"store#7": {"generation": "current"}}}

    assert reconcile.register_generation_worktrees(
        state, "store", 7, "stale", ["/repo/.worktrees/issue-7-stale"]
    ) is False
    assert "worktrees" not in state["issues"]["store#7"]


def test_a_delivery_registers_all_of_its_worktrees_or_none_of_them():
    """A partial registration is the worst outcome available: the engine would own some of a
    card's worktrees while the rest sat on disk owned by nobody, and the refusal the host sees
    would not say which is which. The refused card here keeps no path at all."""
    state = {"issues": {"store#7": {"generation": "current"}}}
    paths = ["/repo/.worktrees/issue-7", "/other/.worktrees/issue-7"]

    assert reconcile.register_generation_worktrees(state, "store", 7, "stale", paths) is False
    assert "worktrees" not in state["issues"]["store#7"]
    assert reconcile.register_generation_worktrees(state, "store", 7, "current", paths) is True
    assert reconcile.worktrees(state["issues"]["store#7"]) == paths


def test_a_card_the_engine_has_forgotten_cannot_be_given_worktrees():
    """Registration is how a path becomes cleanable. Attaching one to a record that no longer
    exists would silently create the leak this seam is meant to close."""
    assert reconcile.register_generation_worktrees(
        {"issues": {}}, "store", 7, "current", ["/repo/.worktrees/issue-7"]
    ) is False


def test_failed_worktree_removal_keeps_the_card_for_retry():
    rec: dict = {}
    reconcile.register_worktree(rec, "/a/.worktrees/issue-7")
    reconcile.register_worktree(rec, "/b/.worktrees/issue-7")
    reconcile.begin_cleanup(rec)
    assert reconcile.record_cleanup(rec, failed=["/b/.worktrees/issue-7"]) is False
    # Only the unreleased path remains, and the record stays alive to retry it.
    assert reconcile.worktrees(rec) == ["/b/.worktrees/issue-7"]
    assert rec["cleanup_attempts"] == 1
    assert _decide(rec, CardSnapshot(open=False)) is ReconcileDecision.CLEANUP_RETRY


def test_a_label_removal_owed_before_2497_no_longer_holds_the_card():
    """A record saved while the engine still removed the label may carry `cleanup_label`. The
    label stays on now (#2497), so nothing is owed and the card is clean enough to forget."""
    rec: dict = {"cleanup_label": True}
    reconcile.begin_cleanup(rec)
    assert reconcile.record_cleanup(rec, failed=[]) is True
    assert "cleanup_label" not in rec
    assert "forget_pending" not in rec


def test_successful_cleanup_clears_the_card_for_forgetting():
    rec: dict = {}
    reconcile.register_worktree(rec, "/a/.worktrees/issue-7")
    reconcile.begin_cleanup(rec)
    reconcile.record_cleanup(rec, failed=["/a/.worktrees/issue-7"])
    assert reconcile.record_cleanup(rec, failed=[]) is True
    assert "worktrees" not in rec
    assert "cleanup_attempts" not in rec
    assert "forget_pending" not in rec


# --- worker slots ---

def test_active_workers_counts_cards_a_worker_may_still_be_on():
    decisions = {
        "r#1": ReconcileDecision.ACTIVE,
        "r#2": ReconcileDecision.AWAIT_TERMINAL,
        "r#3": ReconcileDecision.STOP_WAITING,
        "r#4": ReconcileDecision.IDLE,
        "r#6": ReconcileDecision.STOP_RELEASED,
    }
    assert reconcile.active_workers(decisions) == 3


def test_reload_action_matches_the_card_kind():
    assert reconcile.reload_action({"kind": "issue"}) == "resume"
    assert reconcile.reload_action({"kind": "pr"}) == "pr"
    assert reconcile.reload_action({"mode": "merge-review", "kind": "pr"}) == "merge-review"


def test_beginning_a_revived_run_restarts_the_cards_clock():
    rec = {"status": "In progress", "started_at": "2026-07-25T09:00:00+00:00"}
    reconcile.begin_cold_reload(rec, now=_NOW)
    assert rec["started_at"] == _NOW.isoformat()


def test_stopping_card_whose_turn_already_finished_is_ended_not_left_waiting():
    """#299 on agentdeck#295: a finished worker's marker satisfies the stop by itself."""
    rec = {"status": "In progress", "stopping": True}
    snapshot = CardSnapshot(status="In progress", stopped_terminal_marker="In review")
    assert _decide(rec, snapshot) is ReconcileDecision.STOP_ENDED


def test_an_ended_stop_does_not_hold_a_worker_slot():
    """The turn is over, so the card does not hold a worker slot."""
    assert ReconcileDecision.STOP_ENDED not in reconcile.HOLDS_A_WORKER
    assert ReconcileDecision.STOP_WAITING in reconcile.HOLDS_A_WORKER
