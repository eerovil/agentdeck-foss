"""The optional *Ready to merge* column: where a release attempt that did not merge rests (#1099).

Before this, an approved release that failed went to *Blocked* and a review that asked for
changes went back to *In review*. Neither says "this was approved, it just did not land", and
the two of them split one pile of work across two columns that already mean other things. The
sharper cost was that ``/merge`` was admitted only from *In review*, so a card the engine had
parked in Blocked could not be retried at all — even though the engine's own repair-exhausted
comment tells the reader to "fix it and `/merge` again".

Every test here is paired: the same wake with the column configured and without it. A project
that has not made the column must see the old destinations exactly, because the column is
optional and adding a role to the engine cannot move other projects' cards.
"""

from __future__ import annotations

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import config, release
from agentdeck.poller_engine.ports import ArmOutcome, EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"
_READY = "Ready to merge"


def rec_of(store, key=_KEY):
    return store.read()["issues"].get(key)


def merge_state(extra=None, *, mode="merge"):
    """A card mid-release, exactly as ``/merge`` left it."""
    rec = {
        "status": "In progress",
        "mode": mode,
        "started": True,
        "started_at": _NOW.isoformat(),
        "generation": "gen1",
        "merge_control": "3",
    }
    rec.update(extra or {})
    return {
        "issues": {_KEY: rec},
        "controls": {
            "3": {
                "id": "3", "repo": "store", "number": 5, "kind": "merge",
                "payload": None, "status": "processing", "created_at": "t1",
            }
        },
    }


# --- a failed release parks in the new column instead of Blocked ---

def _failed_arm_port():
    """Auto-merge is switched off on the repository, which ends the release with nothing merged."""
    return FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: PrState(ref=_PR, state="OPEN", head="aaa",
                                check_state="green", merge_state="CLEAN")},
        arm={_PR: ArmOutcome.UNAVAILABLE},
    )


def test_a_failed_release_parks_in_ready_to_merge_when_the_project_has_the_column(tmp_path):
    store = store_for(tmp_path, merge_state())

    run_wake(store, _failed_arm_port(), now=_NOW, ready_to_merge=_READY)

    rec = rec_of(store)
    assert rec["status"] == _READY
    assert "mode" not in rec  # the release really is over, not merely relocated


def test_the_same_failure_still_parks_in_blocked_without_the_column(tmp_path):
    store = store_for(tmp_path, merge_state())

    run_wake(store, _failed_arm_port(), now=_NOW)

    assert rec_of(store)["status"] == "Blocked"


def test_the_parked_card_is_still_reopened_and_still_explains_itself(tmp_path):
    """Moving the column must not quietly drop the other two halves of parking a release."""
    port = _failed_arm_port()
    store = store_for(tmp_path, merge_state())

    run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    assert EffectKind.REOPEN_ISSUE in port.kinds()
    assert any("auto-merge is disabled" in str(e.value).lower() for e in port.applied)


# --- a review handback rests in the new column instead of In review ---

def _handback_port():
    """The reviewer asked for changes: approved work, nothing merged."""
    return FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: PrState(ref=_PR, head="aaa")},
        review_verdicts={_KEY: "changes-needed"},
    )


def _review_state():
    state = merge_state({"review_heads": {_PR: "aaa"}}, mode="merge-review")
    state["controls"]["3"]["kind"] = "merge-review"
    return state


def test_a_review_handback_rests_in_ready_to_merge_when_the_project_has_the_column(tmp_path):
    store = store_for(tmp_path, _review_state())

    run_wake(store, _handback_port(), now=_NOW, ready_to_merge=_READY)

    rec = rec_of(store)
    assert rec["status"] == _READY
    assert "mode" not in rec


def test_the_same_handback_still_rests_in_in_review_without_the_column(tmp_path):
    store = store_for(tmp_path, _review_state())

    run_wake(store, _handback_port(), now=_NOW)

    assert rec_of(store)["status"] == release.IN_REVIEW


# --- the point of the column: /merge can be tried again from it ---

def test_merge_is_admitted_from_ready_to_merge(tmp_path):
    """The retry the column exists for. Without it this is ``reject-status``."""
    port = FakePort(
        comments=[comment(3, "/merge")],
        snapshots={_KEY: CardSnapshot(status=_READY)},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
    )
    store = store_for(tmp_path, {"issues": {_KEY: {"status": _READY}}})

    run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    state = store.read()
    assert state["issues"][_KEY]["mode"] == "merge"
    assert state["controls"]["3"]["status"] == "processing"


def test_merge_review_is_admitted_from_ready_to_merge(tmp_path):
    port = FakePort(
        comments=[comment(3, "/merge review")],
        snapshots={_KEY: CardSnapshot(status=_READY)},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        review_diffs={_PR: "diff-1"},
        pr_states={_PR: PrState(ref=_PR, head="aaa")},
    )
    store = store_for(tmp_path, {"issues": {_KEY: {"status": _READY}}})

    outcome = run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    assert rec_of(store)["mode"] == "merge-review"
    assert [d["action"] for d in outcome.directives] == ["merge-review"]


def test_merge_from_ready_to_merge_is_refused_where_the_project_has_no_such_column(tmp_path):
    """Nothing is admitted from a column the project never made — the old rule, unchanged."""
    port = FakePort(
        comments=[comment(3, "/merge")],
        snapshots={_KEY: CardSnapshot(status=_READY)},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
    )
    store = store_for(tmp_path, {"issues": {_KEY: {"status": _READY}}})

    run_wake(store, port, now=_NOW)

    assert store.read()["controls"]["3"]["rejection"] == "reject-status"


def test_merge_is_still_refused_from_a_column_that_is_not_a_resting_place(tmp_path):
    port = FakePort(
        comments=[comment(3, "/merge")],
        snapshots={_KEY: CardSnapshot(status="Todo")},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
    )
    store = store_for(tmp_path, {"issues": {_KEY: {"status": "Todo"}}})

    outcome = run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    assert store.read()["controls"]["3"]["rejection"] == "reject-status"
    refusal = next(e for e in outcome.effects if e.kind is EffectKind.COMMENT)
    # The refusal names both admissible columns rather than sending the reader to one of them.
    assert "not in In review or Ready to merge" in str(refusal.value)


# --- nothing reviews a resting card unasked (#2502) ---

def test_a_card_resting_in_ready_to_merge_is_not_reviewed_unasked(tmp_path):
    """The retired automerge label used to arm a review here with no instruction typed."""
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status=_READY)},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        permissions={"eero": "maintain"},
        pr_states={_PR: PrState(ref=_PR, head="aaa")},
    )
    store = store_for(
        tmp_path,
        {"issues": {_KEY: {"status": _READY, "started": True, "generation": "gen1",
                           "automerge": True}}},
    )

    outcome = run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    assert "mode" not in rec_of(store)
    assert "automerge" not in rec_of(store)
    assert outcome.directives == []


def test_a_review_the_retired_label_armed_before_the_upgrade_goes_back_to_rest(tmp_path):
    """It approved no diff, so its verdict could only defer; it must not sit in review mode."""
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status=_READY)},
        linked={_KEY: LinkedPRs(open={_PR: "aaa"})},
        pr_states={_PR: PrState(ref=_PR, head="aaa")},
    )
    store = store_for(
        tmp_path,
        {"issues": {_KEY: {
            "status": _READY, "started": True, "generation": "gen1", "mode": "merge-review",
            "automerge": True, "auto_review": True, "automerge_spent": True,
        }}},
    )

    outcome = run_wake(store, port, now=_NOW, ready_to_merge=_READY)

    rec = rec_of(store)
    assert "mode" not in rec and "auto_review" not in rec and "automerge_spent" not in rec
    assert outcome.directives == []
    assert any("automerge retired" in line for line in outcome.log)


def test_a_person_s_review_keeps_its_mode_through_the_retirement(tmp_path):
    """Only the label's own arm is dropped; a `/merge review` bound to heads is untouched."""
    from agentdeck.poller_engine.cycle import CycleOutcome, CycleReport, _retire_standing_approvals

    state = {"issues": {_KEY: {
        "mode": "merge-review", "merge_control": "3", "review_heads": {_PR: "aaa"},
        "auto_review": True,
    }}}
    _retire_standing_approvals(state, CycleOutcome(CycleReport()))

    assert state["issues"][_KEY] == {
        "mode": "merge-review", "merge_control": "3", "review_heads": {_PR: "aaa"},
    }


# --- the column is optional, so a board without it still activates ---

def test_ready_to_merge_is_not_a_required_role():
    assert config.Role.READY_TO_MERGE in config.OPTIONAL_ROLES
    assert config.Role.READY_TO_MERGE not in config.REQUIRED_ROLES


def test_the_column_is_used_only_when_activation_bound_a_board_option():
    """A status write to an unbound role fails and retries forever, so this must be exact."""
    from agentdeck.poller_engine.__main__ import _ready_to_merge

    unbound = config.ResolvedBinding(project_id="p", status_field_id="f", status_option_ids={})
    bound = config.ResolvedBinding(
        project_id="p", status_field_id="f",
        status_option_ids={config.Role.READY_TO_MERGE.value: "opt-1"},
    )
    assert _ready_to_merge(unbound) is None
    assert _ready_to_merge(bound) == _READY
