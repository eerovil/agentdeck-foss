"""Slice 7 of #176: guarded release — admission, approval binding, repair policy, deploy gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.poller_engine import release
from agentdeck.poller_engine.release import (
    DeployOutcome,
    FixOutcome,
    PrBlocker,
    ReleaseAdmission,
    ReleaseStep,
)

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_IDLE = {"status": "In review"}


def _admit(rec=None, *, review_first=False, **kwargs) -> ReleaseAdmission:
    defaults = {
        "issue_open": True,
        "owned": True,
        "live_status": "In review",
        "linked_open": 1,
        "linked_merged": 0,
    }
    defaults.update(kwargs)
    return release.admit(dict(_IDLE if rec is None else rec), review_first=review_first, **defaults)


# --- admission ---

def test_idle_owned_in_review_card_is_admitted():
    assert _admit() is ReleaseAdmission.ADMIT_MERGE
    assert _admit(review_first=True) is ReleaseAdmission.ADMIT_REVIEW


def test_closed_issue_is_rejected_first():
    assert _admit(issue_open=False, owned=False) is ReleaseAdmission.REJECT_CLOSED


def test_unowned_card_is_rejected():
    assert _admit(owned=False) is ReleaseAdmission.REJECT_UNOWNED


def test_busy_card_is_rejected():
    for rec in (
        {"status": "In progress"},
        {"status": "In review", "mode": "merge"},
        {"status": "In review", "stopping": True},
        {"status": "In review", "pending": ["c1"]},
        {"status": "In review", "processing": ["c1"]},
    ):
        assert _admit(rec) is ReleaseAdmission.REJECT_BUSY


def test_a_card_the_engine_has_no_record_of_is_deferred_not_called_busy():
    """`rec is None` is nothing happening; busy is something happening (#294).

    Told `busy`, an operator waits and retries — reasonable, and wrong: no worker is
    going to finish. The engine simply has not met this card yet, which is a later
    wake's answer once discovery seeds it, not a verdict to publish now.
    """
    verdict = release.admit(
        None,
        review_first=False,
        issue_open=True,
        owned=True,
        live_status="In review",
        linked_open=1,
        linked_merged=0,
    )
    assert verdict is ReleaseAdmission.DEFER_NO_RECORD
    assert verdict is not ReleaseAdmission.REJECT_BUSY


def test_wrong_status_is_rejected():
    assert _admit(live_status="Todo") is ReleaseAdmission.REJECT_STATUS
    assert _admit(live_status=None) is ReleaseAdmission.REJECT_STATUS


def test_no_linked_pr_is_rejected():
    assert _admit(linked_open=0, linked_merged=0) is ReleaseAdmission.REJECT_NO_LINKED_PR


def test_review_needs_an_open_pr_but_plain_merge_publishes_merged_work():
    assert _admit(review_first=True, linked_open=0, linked_merged=2) is (
        ReleaseAdmission.REJECT_NOTHING_TO_REVIEW
    )
    # A plain /merge still deploy-verifies and publishes already-merged work.
    assert _admit(linked_open=0, linked_merged=2) is ReleaseAdmission.ADMIT_MERGE


# --- approval bound to the approved diff ---

def test_arm_review_snapshots_the_approved_prs():
    rec: dict = {"status": "In review"}
    release.arm_review(rec, heads={"store#5": "aaa"}, control_id="c1", now=_NOW)
    assert rec["mode"] == "merge-review"
    assert rec["merge_control"] == "c1"
    assert rec["review_heads"] == {"store#5": "aaa"}
    assert rec["started_at"] == _NOW.isoformat()


def test_unchanged_heads_confirm_the_approval():
    rec = {"review_heads": {"store#5": "aaa", "abacus#7": "bbb"}}
    assert release.heads_unchanged(
        rec, open_refs={"store#5", "abacus#7"}, heads={"store#5": "aaa", "abacus#7": "bbb"}
    ) is True


def test_a_new_commit_invalidates_the_approval():
    rec = {"review_heads": {"store#5": "aaa"}}
    assert release.heads_unchanged(rec, open_refs={"store#5"}, heads={"store#5": "ccc"}) is False


def test_an_unchanged_effective_diff_survives_head_and_base_updates():
    """#1436, kept by provenance: the engine's sync recorded the head it produced (#2122)."""
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_diffs": {"store#5": "diff-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        review_diffs={"store#5": "diff-1"},
        rebase_moved={"store#5": "ccc"},
    ) is True


def test_an_approval_does_not_survive_a_push_that_relocated_the_same_lines():
    """The paired case: identical normalized payload, somewhere else, nobody's provenance.

    The digest says what a change contains and not where it sits, so on its own it cannot tell
    a relocated block from an unmoved one. Without a recorded engine transition naming this
    head, a moved head is a push the approving reviewer has not seen.
    """
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_diffs": {"store#5": "diff-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        review_diffs={"store#5": "diff-1"},
    ) is False
    # And a transition naming a *different* head is not this head's provenance either.
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        review_diffs={"store#5": "diff-1"},
        rebase_moved={"store#5": "ddd"},
    ) is False


def test_a_changed_effective_diff_invalidates_even_an_unchanged_head():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_diffs": {"store#5": "diff-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "aaa"},
        review_diffs={"store#5": "diff-2"},
    ) is False


def test_an_unreadable_current_diff_defers_the_approval():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_diffs": {"store#5": "diff-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "aaa"},
        review_diffs={"store#5": None},
    ) is None


def test_a_base_only_head_update_keeps_the_same_release_content_approved():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_trees": {"store#5": "tree-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        release_trees={"store#5": "tree-1"},
    ) is True


def test_a_changed_release_tree_invalidates_a_changed_head():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_trees": {"store#5": "tree-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        release_trees={"store#5": "tree-2"},
    ) is False


def test_an_unreadable_current_tree_defers_a_changed_head():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_trees": {"store#5": "tree-1"},
    }
    assert release.heads_unchanged(
        rec,
        open_refs={"store#5"},
        heads={"store#5": "ccc"},
        release_trees={"store#5": None},
    ) is None


def test_a_changed_pr_set_invalidates_the_approval():
    rec = {"review_heads": {"store#5": "aaa"}}
    assert release.heads_unchanged(
        rec, open_refs={"store#5", "abacus#9"}, heads={"store#5": "aaa"}
    ) is False


def test_unverifiable_freshness_is_neither_approved_nor_rejected():
    rec = {"review_heads": {"store#5": "aaa"}}
    # Discovery failed.
    assert release.heads_unchanged(rec, open_refs=None, heads={}) is None
    # A head SHA could not be read.
    assert release.heads_unchanged(rec, open_refs={"store#5"}, heads={"store#5": None}) is None
    # No snapshot at all.
    assert release.heads_unchanged({}, open_refs=set(), heads={}) is None


def test_approval_is_consumed_only_when_asked():
    rec = {
        "review_heads": {"store#5": "aaa"},
        "review_diffs": {"store#5": "diff-1"},
        "review_trees": {"store#5": "tree-1"},
    }
    release.consume_approval(rec)
    assert not {"review_heads", "review_diffs", "review_trees"} & rec.keys()


# --- blocker classification ---

def test_behind_is_judged_before_the_ci_verdict():
    assert release.classify_pr(check_state="failing", merge_state="BEHIND") is PrBlocker.BEHIND
    # The up-to-date check failing means behind too, even when the merge state lags.
    assert release.classify_pr(
        check_state="failing", merge_state="BLOCKED", failed_steps=[release.UPTODATE_STEP]
    ) is PrBlocker.BEHIND


def test_conflicts_are_a_rebase_job():
    assert release.classify_pr(check_state="green", merge_state="DIRTY") is PrBlocker.DIRTY


def test_lint_only_failures_are_distinguished_from_code_failures():
    assert release.classify_pr(
        check_state="failing", merge_state="CLEAN", failed_steps=["Lint_backend", "lint_js"]
    ) is PrBlocker.RED_LINT
    assert release.classify_pr(
        check_state="failing", merge_state="CLEAN", failed_steps=["Lint_backend", "Tests"]
    ) is PrBlocker.RED_CODE
    # Red with no step detail is not assumed to be lint.
    assert release.classify_pr(check_state="failing", merge_state="CLEAN") is PrBlocker.RED_CODE


def test_pending_and_ready():
    assert release.classify_pr(check_state="pending", merge_state="CLEAN") is PrBlocker.PENDING
    assert release.classify_pr(check_state="green", merge_state="CLEAN") is PrBlocker.READY


# --- release progression ---

def test_plan_release_orders_the_steps():
    assert release.plan_release({"a#1": PrBlocker.BEHIND, "b#2": PrBlocker.RED_CODE}) is (
        ReleaseStep.UPDATE_BEHIND
    )
    assert release.plan_release({"a#1": PrBlocker.RED_CODE, "b#2": PrBlocker.PENDING}) is (
        ReleaseStep.REPAIR
    )
    assert release.plan_release({"a#1": PrBlocker.PENDING, "b#2": PrBlocker.READY}) is (
        ReleaseStep.WAIT_CHECKS
    )
    assert release.plan_release({"a#1": PrBlocker.READY}) is ReleaseStep.ARM_MERGE
    # No open PRs at all: nothing to arm, the caller publishes the already-merged work.
    assert release.plan_release({}) is ReleaseStep.ARM_MERGE


def test_lint_only_blockers_may_keep_merging_after_a_repair():
    assert release.fix_keeps_merge([PrBlocker.RED_LINT, PrBlocker.RED_LINT]) is True
    assert release.fix_keeps_merge([PrBlocker.RED_LINT, PrBlocker.DIRTY]) is False
    assert release.fix_keeps_merge([]) is False


# --- the single repair attempt ---

def _fix(keep=False, heads=None, diffs=None):
    rec: dict = {}
    release.arm_fix(
        rec, heads=heads or {"a#1": "aaa"}, keep=keep, now=_NOW, diffs=diffs
    )
    return rec["fix"]


def test_going_behind_mid_repair_updates_and_keeps_waiting():
    outcome = release.assess_fix(
        _fix(), blockers={"a#1": PrBlocker.BEHIND}, heads={"a#1": "aaa"}
    )
    assert outcome is FixOutcome.UPDATE_BEHIND


def test_repair_still_running_waits():
    outcome = release.assess_fix(
        _fix(), blockers={"a#1": PrBlocker.RED_CODE}, heads={"a#1": "aaa"}
    )
    assert outcome is FixOutcome.WAITING


def test_pushed_but_still_broken_spends_the_single_attempt():
    outcome = release.assess_fix(
        _fix(), blockers={"a#1": PrBlocker.RED_CODE}, heads={"a#1": "zzz"}
    )
    assert outcome is FixOutcome.EXHAUSTED


def test_checks_rerunning_after_a_push_waits():
    outcome = release.assess_fix(
        _fix(), blockers={"a#1": PrBlocker.PENDING}, heads={"a#1": "zzz"}
    )
    assert outcome is FixOutcome.WAITING


def test_a_code_repair_that_went_green_is_handed_back():
    outcome = release.assess_fix(
        _fix(keep=False), blockers={"a#1": PrBlocker.READY}, heads={"a#1": "zzz"}
    )
    assert outcome is FixOutcome.HANDBACK


def test_the_dispatch_promise_and_the_assessment_read_one_predicate():
    # The comment the repair dispatch posts and the outcome `assess_fix` reaches must not be
    # able to disagree about whether a code change comes back for review.
    assert release.fix_may_hand_back(keep=False) is True
    assert release.fix_may_hand_back(keep=True) is False


def test_a_lint_repair_that_went_green_keeps_merging():
    outcome = release.assess_fix(
        _fix(keep=True), blockers={"a#1": PrBlocker.READY}, heads={"a#1": "zzz"}
    )
    assert outcome is FixOutcome.KEEP_MERGING


def test_a_repair_that_only_merged_the_base_keeps_merging():
    # #1436: the worker's whole push was `Merge branch 'master'`. The head moved, the PR's own
    # diff did not, so there is nothing new for the approver to read.
    outcome = release.assess_fix(
        _fix(keep=False, diffs={"a#1": "diff-1"}),
        blockers={"a#1": PrBlocker.READY},
        heads={"a#1": "zzz"},
        diffs={"a#1": "diff-1"},
    )
    assert outcome is FixOutcome.KEEP_MERGING


def test_a_repair_that_changed_the_diff_is_still_handed_back():
    outcome = release.assess_fix(
        _fix(keep=False, diffs={"a#1": "diff-1"}),
        blockers={"a#1": PrBlocker.READY},
        heads={"a#1": "zzz"},
        diffs={"a#1": "diff-2"},
    )
    assert outcome is FixOutcome.HANDBACK


def test_an_unreadable_diff_falls_back_to_the_head_fence():
    # Unknown on either side is stricter, never looser: the head moved, so a human looks.
    for armed, current in (({"a#1": "diff-1"}, None), (None, "diff-1"), (None, None)):
        outcome = release.assess_fix(
            _fix(keep=False, diffs=armed),
            blockers={"a#1": PrBlocker.READY},
            heads={"a#1": "zzz"},
            diffs={"a#1": current},
        )
        assert outcome is FixOutcome.HANDBACK


def test_arm_fix_stores_only_the_diffs_it_could_read():
    assert _fix(diffs={"a#1": "diff-1", "a#2": None})["diffs"] == {"a#1": "diff-1"}
    assert "diffs" not in _fix(diffs={"a#1": None})


def test_a_base_sync_that_left_the_pr_broken_still_spends_the_attempt():
    # "Changed code" softens the hand-back, not the spend: the worker acted and failed.
    outcome = release.assess_fix(
        _fix(diffs={"a#1": "diff-1"}),
        blockers={"a#1": PrBlocker.RED_CODE},
        heads={"a#1": "zzz"},
        diffs={"a#1": "diff-1"},
    )
    assert outcome is FixOutcome.EXHAUSTED


def test_transient_red_that_cleared_without_a_push_keeps_merging():
    outcome = release.assess_fix(
        _fix(keep=False), blockers={"a#1": PrBlocker.READY}, heads={"a#1": "aaa"}
    )
    assert outcome is FixOutcome.KEEP_MERGING


# --- bounded waiting ---

def test_waiting_is_bounded():
    fresh = (_NOW.replace(hour=11, minute=59)).isoformat()
    old = (_NOW.replace(hour=10, minute=0)).isoformat()
    assert release.is_timed_out(fresh, _NOW) is False
    assert release.is_timed_out(old, _NOW) is True
    # An unreadable timestamp must not silently block a release.
    assert release.is_timed_out(None, _NOW) is False
    assert release.is_timed_out("not-a-time", _NOW) is False


# --- watching merges and the production deploy ---

def test_merge_watch_records_the_armed_and_premerged_sets():
    rec: dict = {}
    release.begin_merge_watch(
        rec, armed=["store#5"], premerged={"abacus": {"sha": "abc", "status": "pending"}}, now=_NOW
    )
    assert rec["awaiting_merge"]["prs"] == ["store#5"]
    assert rec["awaiting_merge"]["premerged"] == {"abacus": {"sha": "abc", "status": "pending"}}
    assert rec["awaiting_merge"]["since"] == _NOW.isoformat()


def test_deploy_watch_replaces_the_merge_watch():
    rec: dict = {}
    release.begin_merge_watch(rec, armed=["store#5"], premerged={}, now=_NOW)
    release.begin_deploy_watch(rec, builds={"store": {"sha": "abc"}}, now=_NOW)
    assert "awaiting_merge" not in rec
    assert rec["awaiting_deploy"]["builds"] == {"store": {"sha": "abc"}}


def test_the_deploy_watch_anchors_at_the_merge_not_at_the_wake_that_noticed_it():
    # #815: the gate accepts deploy evidence only from at/after `since`. Anchoring that at
    # the wake meant a deploy that fired and finished before the engine looked left its
    # only evidence in the past, and two healthy releases were reported as failed deploys.
    rec: dict = {}
    merged_at = (_NOW - timedelta(seconds=47)).astimezone(UTC).isoformat()
    release.begin_deploy_watch(
        rec, builds={"store": {"sha": "abc"}}, now=_NOW, merged_at=merged_at
    )
    assert rec["awaiting_deploy"]["since"] == merged_at


def test_a_missing_or_unreadable_merge_time_still_anchors_at_the_wake():
    # Every port that does not report one — the reference suites included — keeps the old
    # behaviour rather than losing its watch to a None.
    for merged_at in (None, "", "   ", "not-a-timestamp"):
        rec: dict = {}
        release.begin_deploy_watch(
            rec, builds={"store": {"sha": "abc"}}, now=_NOW, merged_at=merged_at
        )
        assert rec["awaiting_deploy"]["since"] == _NOW.isoformat(), merged_at


def test_a_merge_time_in_the_future_never_widens_the_window_it_exists_to_close():
    rec: dict = {}
    release.begin_deploy_watch(
        rec,
        builds={"store": {"sha": "abc"}},
        now=_NOW,
        merged_at=(_NOW + timedelta(minutes=5)).isoformat(),
    )
    assert rec["awaiting_deploy"]["since"] == _NOW.isoformat()


def test_deploy_gate_outcomes():
    since = _NOW.replace(hour=11, minute=59).isoformat()
    assert release.gate_deploy({"store": "ok"}, since=since, now=_NOW) is DeployOutcome.DONE
    assert release.gate_deploy(
        {"store": "ok", "abacus": "bad"}, since=since, now=_NOW
    ) is DeployOutcome.FAILED
    assert release.gate_deploy(
        {"store": "pending"}, since=since, now=_NOW
    ) is DeployOutcome.WAITING


def test_deploy_gate_times_out_rather_than_waiting_forever():
    old = _NOW.replace(hour=10, minute=0).isoformat()
    assert release.gate_deploy({"store": "pending"}, since=old, now=_NOW) is (
        DeployOutcome.TIMED_OUT
    )
    # A failure still reads as a failure, not a timeout.
    assert release.gate_deploy({"store": "bad"}, since=old, now=_NOW) is DeployOutcome.FAILED


# --- durable terminal intents ---

def test_terminal_transitions_are_intended_before_they_are_visible():
    rec: dict = {}
    release.arm_done(rec, merged=["store#5"], deploy_links=["[store #12](url)"])
    assert release.pending_finalization(rec) == "done_pending"
    release.clear_finalization(rec, "done_pending")
    assert release.pending_finalization(rec) is None

    release.arm_blocked(rec, "prod deploy failed", merged=["store#5"])
    intent = rec["blocked_pending"]
    assert intent["why"] == "prod deploy failed"
    assert intent["merged"] == ["store#5"]
    assert release.pending_finalization(rec) == "blocked_pending"


def test_each_step_of_a_terminal_transition_is_recorded_as_it_lands():
    rec: dict = {}
    intent = release.arm_done(rec, merged=["store#5"])
    assert all(release.needs_step(intent, step) for step in release.DONE_STEPS)
    release.mark_step(intent, "status")
    assert release.needs_step(intent, "status") is False
    # Re-arming an interrupted transition keeps the steps that already landed.
    again = release.arm_done(rec, merged=["store#5"])
    assert again is intent
    assert release.needs_step(again, "status") is False
    assert release.needs_step(again, "comment") is True


def test_a_done_intent_is_resumed_before_a_start_transition():
    rec = {"merge_start_pending": True, "done_pending": {"merged": []}}
    assert release.pending_finalization(rec) == "done_pending"
    release.clear_finalization(rec, "done_pending")
    assert release.pending_finalization(rec) == "merge_start_pending"


def test_a_repair_worker_that_ended_without_pushing_has_finished():
    """#2771: nothing pushed and still red is an answer once the worker's turn is over."""
    fix = {"heads": {"r#1": "aaa"}}
    red = {"r#1": release.PrBlocker.RED_CODE}
    assert release.assess_fix(fix, blockers=red, heads={"r#1": "aaa"}) is (
        release.FixOutcome.WAITING
    )
    assert release.assess_fix(fix, blockers=red, heads={"r#1": "aaa"}, worker_ended=True) is (
        release.FixOutcome.UNCHANGED
    )
    # A push still spends the attempt the ordinary way, ended or not.
    assert release.assess_fix(fix, blockers=red, heads={"r#1": "bbb"}, worker_ended=True) is (
        release.FixOutcome.EXHAUSTED
    )
    # Checks still running after the worker ended are waited on, not judged.
    pending = {"r#1": release.PrBlocker.PENDING}
    assert release.assess_fix(
        fix, blockers=pending, heads={"r#1": "aaa"}, worker_ended=True
    ) is release.FixOutcome.WAITING


def test_a_park_moves_on_only_when_a_head_it_recorded_changed():
    parked = {"why": "x", "heads": {"r#1": "aaa", "r#2": "ccc"}}
    assert release.park_head_moved(parked, {"r#1": "bbb", "r#2": "ccc"})
    assert not release.park_head_moved(parked, {"r#1": "aaa", "r#2": "ccc"})
    # An unread head, or a pull request the park never saw, says nothing.
    assert not release.park_head_moved(parked, {"r#1": None, "r#3": "ddd"})
    assert not release.park_head_moved(parked, {})
    assert not release.park_head_moved({"why": "x"}, {"r#1": "bbb"})
    assert not release.park_head_moved(None, {"r#1": "bbb"})
    assert not release.park_head_moved({"heads": {"r#1": ""}}, {"r#1": "bbb"})
