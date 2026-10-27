"""A Blocked card picks itself back up when its verification lands green (#2306).

The #2289 shape: a worker stopped in Blocked saying its own suite was the only thing left. The
suite had been submitted by the generation before it, which reportless recovery had already
replaced, so the old worker's own ``finish`` was — correctly — refused as handed off. The suite
then finished green on the pull request's current head, and nothing acted on it: the job named a
Run the card no longer held, so it was not even credited, and a Blocked card with no dependency
is never read again. A person had to move it.

The stale-generation fence stays exactly as it was. What changes is that the arrival of the
evidence itself is enough: it is credited to the card's commit whichever Run of the same Work
submitted it, and a card resting in Blocked when it arrives gets one ordinary ``resume``.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import blockers, verification
from agentdeck.poller_engine.ports import EffectKind, LinkedPRs, PrState
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl

_KEY = "store#5"
_PR = "store#12"
_WORK = "github_issue:org/store#5"
_EMPTY = hashlib.sha256(b"").hexdigest()


def _tested(head: str) -> dict:
    return {
        "root": "/w",
        "head": head,
        "staged_sha256": _EMPTY,
        "tracked_diff_sha256": _EMPTY,
        "untracked_sha256": _EMPTY,
        "untracked_count": 0,
    }


def _job(**overrides) -> dict:
    """The suite the replaced generation submitted, finished green on the PR head."""
    job = {
        "id": "9b37727aa748",
        "purpose": "verification",
        "run_id": "gen-old",
        "work_id": _WORK,
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


def _blocked(**extra) -> dict:
    """#2289 after the second revive: the current generation finished Blocked on the suite."""
    rec = {
        "status": "Blocked",
        "started": True,
        "label_claim": True,
        "generation": "gen-new",
        "reportless_resolved": "gen-old",
        "worktrees": ["/w"],
    }
    rec.update(extra)
    return {"issues": {_KEY: rec}}


def _port(*, jobs=None, head="head-1", snapshot=None, **extra) -> FakePort:
    return FakePort(
        snapshots={_KEY: snapshot or CardSnapshot(status="Blocked")},
        linked={_KEY: LinkedPRs(open={_PR: head})},
        pr_states={_PR: PrState(ref=_PR, head=head)},
        jobs=jobs if jobs is not None else {"9b37727aa748": _job()},
        **extra,
    )


def _comments(port) -> list[str]:
    return [str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT]


def _rec(store) -> dict:
    return store.read()["issues"][_KEY]


def test_the_2289_shape_resumes_by_itself_and_the_stale_finish_stays_refused(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port()

    # The replaced generation cannot move the card; that fence is not what this changes.
    refused = WorkerControl(store, port).finish("store", 5, "gen-old", "in_review", "done")
    assert not refused.ok
    assert "handed to a new worker" in refused.reason

    outcome = run_wake(store, port, now=_NOW)

    assert [d["action"] for d in outcome.directives] == ["resume"]
    (said,) = _comments(port)
    assert "Verification finished green" in said
    assert "9b37727aa748" in said and "head-1" in said and _PR in said
    rec = _rec(store)
    assert "head-1" in rec["release_tests_pass"]
    assert rec["status"] == "In progress"
    assert "blocked_verified" not in rec
    assert any("from an earlier Run of this Work" in line for line in outcome.log)


def test_it_resumes_once_however_many_wakes_follow(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port()

    outcomes = [
        run_wake(store, port, now=_NOW + timedelta(seconds=seconds))
        for seconds in (0, 30, 60, 1000)
    ]

    # Counted by the arm, not by directives: an unacknowledged delivery is retried on its own.
    armed = [
        line for outcome in outcomes for line in outcome.log if "verified green on" in line
        and line.endswith("-> resuming")
    ]
    assert len(armed) == 1
    assert len(_comments(port)) == 1


def test_a_red_verification_leaves_the_card_blocked(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port(jobs={"9b37727aa748": _job(state="failed", result_class="failure")})

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert _comments(port) == []
    rec = _rec(store)
    assert rec["status"] == "Blocked"
    assert not rec.get("release_tests_pass") and "blocked_verified" not in rec


def test_evidence_for_an_old_head_does_not_unblock_the_current_one(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port(head="head-2")  # somebody pushed after the suite started

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert _comments(port) == []
    rec = _rec(store)
    assert rec["status"] == "Blocked"
    # The commit it tested is still proven; it just is not the one that would merge.
    assert "head-1" in rec["release_tests_pass"]
    assert "blocked_verified" not in rec, "a result that can never apply is not kept"
    assert any("stays Blocked" in line for line in outcome.log)


def test_a_draft_pull_request_head_is_checked_too(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port()
    port.linked[_KEY] = LinkedPRs(withheld=[_PR])
    port._pr_states[_PR] = PrState(ref=_PR, head="head-2")

    assert run_wake(store, port, now=_NOW).directives == []
    assert _rec(store)["status"] == "Blocked"


def test_a_second_pull_request_on_another_head_holds_the_card_even_if_green(tmp_path):
    """The verified commit must be the head of *every* linked PR; the other head's own green
    evidence is not the result that arrived (review of #2307)."""
    store = store_for(
        tmp_path, _blocked(release_tests_pass={"head-9": "2026-07-25T11:00:00+00:00"})
    )
    port = _port()
    port.linked[_KEY] = LinkedPRs(open={_PR: "head-1", "store#13": "head-9"})
    port._pr_states["store#13"] = PrState(ref="store#13", head="head-9")

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert _comments(port) == []
    rec = _rec(store)
    assert rec["status"] == "Blocked"
    assert "blocked_verified" not in rec
    assert any("stays Blocked" in line for line in outcome.log)


def test_a_worker_reported_blocker_still_holds_the_card(tmp_path):
    """A genuinely blocked card stays blocked: green tests do not outrank a named dependency."""
    store = store_for(tmp_path, _blocked(blocked_until=["store#122"]))
    port = _port(ref_states={"store#122": "OPEN"})

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    rec = _rec(store)
    assert rec["status"] == "Blocked" and rec["blocked_until"] == ["store#122"]

    # When that lands too, the one resume it arms carries both away.
    port._ref_states["store#122"] = "MERGED"
    resumed = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert [d["action"] for d in resumed.directives] == ["resume"]
    assert "blocked_verified" not in _rec(store)


def test_an_open_dependency_still_holds_the_card(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port(
        snapshot=CardSnapshot(
            status="Blocked", blockers=("store#4",), dependencies=("store#4",)
        )
    )

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.directives == []
    assert _rec(store)["status"] == "Blocked"
    assert any("still waiting on store#4" in line for line in outcome.log)


def test_green_first_and_blocked_second_still_resumes(tmp_path):
    """Review of #2307: the suite is credited while the worker still runs, and its Blocked
    finish lands a wake later. The later Blocked transition must still pick the card up."""
    store = store_for(tmp_path, _blocked(status="In progress", processing=["1"]))
    port = _port(snapshot=CardSnapshot(status="In progress"))

    first = run_wake(store, port, now=_NOW)
    assert first.directives == []
    rec = _rec(store)
    assert "head-1" in rec["release_tests_pass"]
    assert rec["blocked_verified"] == {"head": "head-1", "job": "9b37727aa748"}

    with store.transaction() as state:
        state["issues"][_KEY].update(status="Blocked", processing=[])
    port.snapshots[_KEY] = CardSnapshot(status="Blocked")

    resumed = run_wake(store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=900)

    assert [d["action"] for d in resumed.directives] == ["resume"]
    assert "blocked_verified" not in _rec(store)
    assert len(_comments(port)) == 1


def test_a_worker_finishing_in_review_is_left_alone_and_its_next_start_forgets_the_mark(
    tmp_path,
):
    store = store_for(tmp_path, _blocked(status="In progress", processing=["1"]))
    port = _port(snapshot=CardSnapshot(status="In progress"))
    run_wake(store, port, now=_NOW)

    with store.transaction() as state:
        state["issues"][_KEY].update(status="In review", processing=[])
    port.snapshots[_KEY] = CardSnapshot(status="In review")

    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30))
    assert outcome.directives == [] and _comments(port) == []

    # Any start means a worker is on the card again; the old result has nothing left to lift.
    from poller_engine_fakes import comment

    port.comments.append(comment("1", "/claude carry on"))
    started = run_wake(store, port, now=_NOW + timedelta(seconds=60))
    assert len(started.directives) == 1
    assert "blocked_verified" not in _rec(store)


def test_a_restart_between_the_result_and_the_card_read_keeps_the_resume(tmp_path):
    """The mark is on the card record, so a fresh process reading the same state still acts."""
    store = store_for(tmp_path, _blocked())
    port = _port()
    port._pr_states = None  # the wake credits the job but cannot prove the PR head yet

    assert run_wake(store, port, now=_NOW).directives == []
    assert _rec(store)["blocked_verified"] == {"head": "head-1", "job": "9b37727aa748"}

    restarted = store_for(tmp_path)  # same state file, new process
    port._pr_states = {_PR: PrState(ref=_PR, head="head-1")}
    resumed = run_wake(restarted, port, now=_NOW + timedelta(seconds=30))

    assert [d["action"] for d in resumed.directives] == ["resume"]


def test_the_card_is_read_on_the_wake_the_result_lands_not_the_recheck_window(tmp_path):
    """A Blocked card with no hold cools down; the arriving result makes it due at once."""
    store = store_for(tmp_path, _blocked(last_live_check=_NOW.isoformat()))
    port = _port()

    outcome = run_wake(store, port, now=_NOW + timedelta(seconds=30), terminal_recheck_s=900)

    assert [d["action"] for d in outcome.directives] == ["resume"]


def test_a_notice_that_did_not_land_arms_nothing_and_is_retried(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port(refuse={EffectKind.COMMENT})

    assert run_wake(store, port, now=_NOW).directives == []
    assert _rec(store)["blocked_verified"]["head"] == "head-1"

    port.refuse.clear()
    assert len(run_wake(store, port, now=_NOW + timedelta(seconds=30)).directives) == 1


def test_unreadable_pull_requests_defer_rather_than_resume_or_forget(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port()
    port._pr_states = None

    assert run_wake(store, port, now=_NOW).directives == []
    assert _rec(store)["blocked_verified"]["head"] == "head-1"


def test_a_card_whose_label_was_removed_is_left_alone(tmp_path):
    store = store_for(tmp_path, _blocked())
    port = _port(snapshot=CardSnapshot(status="Blocked", owned=False))

    assert run_wake(store, port, now=_NOW).directives == []
    assert _comments(port) == []


def test_an_earlier_runs_job_never_binds_this_rounds_rebase(tmp_path):
    """#2122: a rebase round's head is what *its own* suite ran on, not a late older one."""
    store = store_for(
        tmp_path,
        _blocked(
            status="In progress",
            processing=["1"],
            rebase_round_pending={"refs": [_PR], "round": "1"},
        ),
    )
    port = _port(snapshot=CardSnapshot(status="In progress"))

    run_wake(store, port, now=_NOW)

    rec = _rec(store)
    assert "head-1" in rec["release_tests_pass"]
    assert rec["rebase_round_pending"] == {"refs": [_PR], "round": "1"}
    assert "rebase_round" not in rec


@pytest.mark.parametrize("order", ["old-first", "own-first"])
def test_this_runs_own_job_binds_the_rebase_round_whatever_order_the_jobs_arrive_in(
    tmp_path, order
):
    """Review of #2307: an earlier Run's green job for the same head, listed first, credits
    the tests bit but must not take the rebase-round binding from this Run's own job."""
    old = ("job-old", _job(id="job-old"))
    own = ("job-own", _job(id="job-own", run_id="gen-new"))
    jobs = dict([old, own] if order == "old-first" else [own, old])
    store = store_for(
        tmp_path,
        _blocked(
            status="In progress",
            processing=["1"],
            rebase_round_pending={"refs": [_PR], "round": "1"},
        ),
    )
    port = _port(jobs=jobs, snapshot=CardSnapshot(status="In progress"))

    run_wake(store, port, now=_NOW)

    rec = _rec(store)
    assert "head-1" in rec["release_tests_pass"]
    assert "rebase_round_pending" not in rec
    assert rec["rebase_round"] == {_PR: "head-1"}


def test_a_head_already_credited_binds_nothing_without_this_runs_own_job(tmp_path):
    store = store_for(
        tmp_path,
        _blocked(
            status="In progress",
            processing=["1"],
            release_tests_pass={"head-1": "2026-07-25T11:00:00+00:00"},
            rebase_round_pending={"refs": [_PR], "round": "1"},
        ),
    )
    port = _port(snapshot=CardSnapshot(status="In progress"))

    run_wake(store, port, now=_NOW)

    rec = _rec(store)
    assert rec["rebase_round_pending"] == {"refs": [_PR], "round": "1"}
    assert "rebase_round" not in rec


# --- which jobs count for the Work ---

def test_any_run_of_the_same_work_is_credited_case_folded():
    assert verification.credited_head_for_work(_job(), work_id=_WORK) == "head-1"
    assert (
        verification.credited_head_for_work(
            _job(work_id="github_issue:Org/Store#5"), work_id=_WORK.upper()
        )
        == "head-1"
    )


def test_no_work_on_either_side_or_another_work_credits_nothing():
    assert verification.credited_head_for_work(_job(), work_id="") == ""
    assert verification.credited_head_for_work(_job(work_id=""), work_id=_WORK) == ""
    assert (
        verification.credited_head_for_work(_job(work_id="github_issue:org/store#6"), work_id=_WORK)
        == ""
    )
    assert verification.credited_head_for_work(_job(run_id=""), work_id=_WORK) == ""
    assert verification.credited_head_for_work(None, work_id=_WORK) == ""


def test_every_other_evidence_rule_still_applies_to_an_earlier_run():
    assert verification.credited_head_for_work(_job(purpose=""), work_id=_WORK) == ""
    assert (
        verification.credited_head_for_work(
            _job(verified_workspace=_tested("head-2")), work_id=_WORK
        )
        == ""
    )


def test_the_mark_ignores_an_empty_head():
    rec: dict = {}
    blockers.note_verified(rec, head="  ")
    assert blockers.verified_while_blocked(rec) == {}
    rec["blocked_verified"] = "junk"
    assert blockers.verified_while_blocked(rec) == {}
