"""#1122: what each merge review decided about one commit outlives the card that forgot it."""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import reviews
from agentdeck.poller_engine.ports import LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
_PR = "store#7"


def merge_review_state(extra=None):
    rec = {
        "status": "In progress",
        "mode": "merge-review",
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
                "id": "3", "repo": "store", "number": 5, "kind": "merge-review",
                "payload": None, "status": "processing", "created_at": "t1",
            }
        },
    }


def port_for(*, open_heads=None, verdict="pass", **kwargs):
    return FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={_KEY: LinkedPRs(open=dict(open_heads or {}))},
        review_verdicts={_KEY: verdict},
        **kwargs,
    )


def recorded(tmp_path):
    return {body["sha"]: body for _, body in reviews.pending(tmp_path / "review-verdicts")}


# --- the record the release progression leaves behind ---

def test_a_passing_review_records_the_commit_it_released(tmp_path):
    store = store_for(tmp_path, merge_review_state({"review_heads": {_PR: "aaa"}}))
    port = port_for(
        open_heads={_PR: "aaa"},
        pr_states={_PR: PrState(ref=_PR, state="OPEN", head="aaa", merge_state="CLEAN")},
    )

    run_wake(store, port, now=_NOW)

    entry = recorded(tmp_path)["aaa"]
    assert entry["repo"] == "store"
    assert entry["pull_request"] == _PR
    assert entry["issue_repo"] == "store"
    assert entry["issue_number"] == 5
    assert entry["verdict"] == "pass"
    assert entry["result"] == "release"
    assert "auto" not in entry  # the label-driven review it once marked is gone (#2502)
    assert entry["generation"] == "gen1"


def test_a_failed_review_records_the_hand_back(tmp_path):
    store = store_for(tmp_path, merge_review_state({"review_heads": {_PR: "aaa"}}))
    port = port_for(open_heads={_PR: "aaa"}, verdict="changes-needed")

    run_wake(store, port, now=_NOW)

    entry = recorded(tmp_path)["aaa"]
    assert (entry["verdict"], entry["result"]) == ("changes-needed", "handback")


def test_a_deferred_verdict_records_nothing_because_it_decided_nothing(tmp_path):
    """`/merge review` whose PR set could not be read defers; nothing has happened yet."""
    store = store_for(tmp_path, merge_review_state({"review_heads": {_PR: "aaa"}}))
    port = FakePort(
        snapshots={_KEY: CardSnapshot(status="In progress")},
        linked={_KEY: None},
        review_verdicts={_KEY: "pass"},
    )

    run_wake(store, port, now=_NOW)

    assert recorded(tmp_path) == {}
    assert store.read()["issues"][_KEY]["mode"] == "merge-review"


def test_an_unreadable_head_is_not_a_commit_anybody_reviewed(tmp_path):
    store = store_for(tmp_path, merge_review_state({"review_heads": {_PR: "aaa"}}))
    port = port_for(open_heads={_PR: None})

    run_wake(store, port, now=_NOW)

    assert recorded(tmp_path) == {}


def test_reviewing_the_same_commit_again_replaces_its_record(tmp_path):
    """The key is the commit, so a second round on an unchanged head is one row, not two."""
    store = store_for(tmp_path, merge_review_state({"review_heads": {_PR: "ccc"}}))
    run_wake(store, port_for(open_heads={_PR: "ccc"}, verdict="changes-needed"), now=_NOW)

    rec = store.read()["issues"][_KEY]
    assert rec["status"] == "In review"
    later = _NOW + timedelta(minutes=5)
    with store.transaction() as state:
        state["issues"][_KEY].update(
            status="In progress", mode="merge-review", review_heads={_PR: "ccc"},
            started=True, started_at=later.isoformat(), merge_control="3",
        )
        state["controls"]["3"]["status"] = "processing"
    run_wake(
        store,
        port_for(
            open_heads={_PR: "ccc"},
            pr_states={_PR: PrState(ref=_PR, state="OPEN", head="ccc", merge_state="CLEAN")},
        ),
        now=later,
    )

    entries = recorded(tmp_path)
    assert list(entries) == ["ccc"]
    assert entries["ccc"]["verdict"] == "pass"


# --- the ledger itself ---

def test_a_record_is_addressed_by_repo_and_commit(tmp_path):
    first = reviews.record(
        tmp_path, repo="org/one", sha="a" * 40, pull_request="org/one#1",
        issue_repo="org/one", issue_number=1, verdict="pass", result="release",
        generation="g", now=_NOW,
    )
    same = reviews.record(
        tmp_path, repo="org/one", sha="a" * 40, pull_request="org/one#1",
        issue_repo="org/one", issue_number=1, verdict="changes-needed",
        result="handback", generation="g", now=_NOW,
    )
    other = reviews.record(
        tmp_path, repo="org/two", sha="a" * 40, pull_request="org/two#2",
        issue_repo="org/one", issue_number=1, verdict="pass", result="release",
        generation="g", now=_NOW,
    )

    assert first == same != other
    assert len(reviews.pending(tmp_path)) == 2


def test_a_record_without_a_commit_is_not_written(tmp_path):
    assert reviews.record(
        tmp_path, repo="org/one", sha="", pull_request="", issue_repo="org/one",
        issue_number=1, verdict="pass", result="release",
        generation="g", now=_NOW,
    ) is None
    assert reviews.pending(tmp_path) == []


def test_a_damaged_record_is_skipped_rather_than_stopping_the_rest(tmp_path):
    reviews.record(
        tmp_path, repo="org/one", sha="a" * 40, pull_request="org/one#1",
        issue_repo="org/one", issue_number=1, verdict="pass", result="release",
        generation="g", now=_NOW,
    )
    (tmp_path / f"broken{reviews.SUFFIX}").write_text("{not json")

    assert [body["repo"] for _, body in reviews.pending(tmp_path)] == ["org/one"]


def test_clearing_a_record_that_was_never_there_is_not_a_failure(tmp_path):
    assert reviews.clear(tmp_path / f"missing{reviews.SUFFIX}") is True


def test_pending_on_a_directory_that_does_not_exist_is_empty(tmp_path):
    assert reviews.pending(tmp_path / "nope") == []
