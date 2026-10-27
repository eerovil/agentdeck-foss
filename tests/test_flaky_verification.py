"""A verification that failed only on tests the commit did not break counts as green (#2382).

On 2026-09-28 a flaky browser test and a test already broken on ``master`` failed most
full-suite runs, whatever the branch, and parked cards for about twenty card-hours. When a
verification fails on a few tests, the job queue now reruns them on the same commit (twice at
most) and runs what still fails once on the merge base, recording on each follow-up which
verification and which tests it is for. A test a rerun passed is flaky, one that fails on the
merge base too is broken on master; anything else keeps the verification red. Each forgiven
test gets one labelled fix issue.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.jobs import VERIFICATION
from agentdeck.poller_engine import flaky
from agentdeck.poller_engine.cycle import _forgiven_on
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.verification import credited_head, forgiven_tests
from agentdeck.run_evidence import RunEvidenceLedger
from agentdeck.work_overview import summarize_verification
from agentdeck.work_workspace import run_verifications

_EMPTY = hashlib.sha256(b"").hexdigest()
REPO = "eerovil/agentdeck"
FLAKE = "tests/test_work_stops.py::test_a_folded_group_stays_folded"
BROKEN = "tests/test_work_resolution.py::test_rename_a_work_on_a_phone_and_back"
REAL = "tests/test_cycle.py::test_the_branch_broke_this"


def _tested(head: str, *, base: str = "base-1", clean: bool = True) -> dict:
    return {
        "root": "/w",
        "head": head,
        "base_head": base,
        "repository": REPO,
        "staged_sha256": _EMPTY,
        "tracked_diff_sha256": _EMPTY if clean else "dirty",
        "untracked_sha256": _EMPTY,
        "untracked_count": 0,
    }


def _verif(job_id="v", *, failures=(FLAKE,), head="h", base="base-1", **extra) -> dict:
    """A finished verification row as the queue keeps it."""
    row = {
        "id": job_id,
        "purpose": VERIFICATION,
        "run_id": "gen-1",
        "command": ["pytest", "-q"],
        "state": "failed" if failures else "done",
        "result_class": "failure" if failures else "success",
        "failures": list(failures),
        "failures_complete": True,
        "tested_workspace": _tested(head, base=base),
        "verified_workspace": _tested(head, base=base),
        # Every failure is a test the merge base has, unless a case says otherwise (#2552).
        "base_present": list(failures),
    }
    row.update(extra)
    return row


def _follow(job_id, kind, tests, *, failures=(), head="h", of="v", state=None, **extra) -> dict:
    """A follow-up the queue submitted, with what it ran and how it ended."""
    row = {
        "id": job_id,
        "follows": of,
        "follow_kind": kind,
        "follow_tests": list(tests),
        "command": ["pytest", "-q", *tests],
        "state": state or ("failed" if failures else "done"),
        "result_class": "failure" if failures else "success",
        "failures": list(failures),
        "failures_complete": True,
        "tested_workspace": _tested(head),
        "verified_workspace": _tested(head),
    }
    row.update(extra)
    return row


# --- the rule -------------------------------------------------------------------------


def test_a_failure_a_same_commit_rerun_passed_is_forgiven_as_flaky():
    rerun = _follow("r", flaky.RERUN, [FLAKE])

    assert flaky.forgiveness(_verif(), [rerun]) == (
        {"test": FLAKE, "kind": "flaky", "commit": "h", "job_id": "r"},
    )


def test_a_failure_the_merge_base_run_also_failed_is_forgiven_as_broken_on_master():
    verification = _verif(failures=(BROKEN,))
    reruns = [_follow(f"r{n}", flaky.RERUN, [BROKEN], failures=[BROKEN]) for n in (1, 2)]
    base = _follow("b", flaky.BASE, [BROKEN], failures=[BROKEN], head="base-1")

    assert flaky.forgiveness(verification, [*reruns, base]) == (
        {"test": BROKEN, "kind": "base_broken", "commit": "base-1", "job_id": "b"},
    )


def test_a_pass_on_another_commit_or_a_failure_on_another_base_forgives_nothing():
    """A production change can break a test without touching it, so only this commit and
    this commit's own merge base are evidence."""
    elsewhere = _follow("r", flaky.RERUN, [FLAKE], head="other-branch")
    old_base = _follow("b", flaky.BASE, [FLAKE], failures=[FLAKE], head="base-0")
    not_mine = _follow("x", flaky.RERUN, [FLAKE], of="someone-else")

    assert flaky.forgiveness(_verif(), [elsewhere, old_base, not_mine]) is None


def test_only_a_tree_that_was_dirty_or_moved_is_named_as_why_there_is_no_rerun():
    """#2625: a rerun runs on a checkout of the verified head, which is the tree that failed
    only when that tree was clean and held still, so anything else gets no rerun and says so."""
    dirty = _verif(tested_workspace=_tested("h", clean=False))
    moved = _verif(verified_workspace=_tested("h", clean=False))

    assert flaky.follow_up(dirty, [dirty]) is None
    assert flaky.follow_up(moved, [moved]) is None
    assert flaky.unfollowable_tree(dirty) == flaky.DIRTY
    assert flaky.unfollowable_tree(moved) == flaky.MOVED
    assert flaky.unfollowable_tree(_verif()) is None, "a clean one is rerun instead"
    # A snapshot that was never taken proves nothing, so it names no reason (#2630 review).
    unread_after = _verif(verified_workspace=None)
    assert flaky.follow_up(unread_after, [unread_after]) is None
    assert flaky.unfollowable_tree(unread_after) is None
    assert flaky.unfollowable_tree(_verif(tested_workspace=None)) is None
    partial = {**_tested("h"), "root": "", "tracked_diff_sha256": "dirty"}
    assert flaky.unfollowable_tree(_verif(verified_workspace=partial)) is None
    assert flaky.unfollowable_tree(_verif(tested_workspace=partial)) is None
    # Something other than the tree keeps it from a rerun, so the tree is not the reason.
    many = tuple(f"tests/test_x.py::test_{n}" for n in range(flaky.MAX_FOLLOWED_FAILURES + 1))
    assert flaky.unfollowable_tree(_verif(failures=many, tested_workspace=_tested(
        "h", clean=False))) is None
    assert flaky.unfollowable_tree({**dirty, "failures_complete": False}) is None
    assert flaky.unfollowable_tree({**dirty, "purpose": ""}) is None
    assert flaky.unfollowable_tree(None) is None


def test_one_unexplained_failure_keeps_the_whole_verification_red():
    verification = _verif(failures=(FLAKE, REAL))
    rerun = _follow("r", flaky.RERUN, [FLAKE, REAL], failures=[REAL])
    base = _follow("b", flaky.BASE, [REAL], head="base-1")  # passes there: the branch broke it

    assert flaky.forgiveness(verification, [rerun, base]) is None


def test_a_rerun_whose_collection_failed_is_no_evidence_for_the_tests_it_never_ran():
    """Review of #2386: a module that fails to import reports the file, not the requested
    tests, so treating every unreported test as passed would forgive a real regression."""
    verification = _verif(failures=(FLAKE, REAL))
    module = FLAKE.split("::", 1)[0]
    broken = _follow("r", flaky.RERUN, [FLAKE, REAL], failures=[module])

    assert flaky.forgiveness(verification, [broken]) is None
    assert flaky.follow_up(broken, [verification, broken]) == flaky.FollowUp(
        flaky.RERUN, "v", (FLAKE, REAL)
    ), "no test counts as passed, so the next rerun takes both again"


def test_a_failed_rerun_whose_failures_are_all_requested_tests_still_proves_the_rest_passed():
    verification = _verif(failures=(FLAKE, REAL))
    rerun = _follow("r", flaky.RERUN, [FLAKE, REAL], failures=[REAL])

    assert flaky.follow_up(rerun, [verification, rerun]) == flaky.FollowUp(
        flaky.RERUN, "v", (REAL,)
    )


def test_a_rerun_that_failed_again_or_has_no_complete_result_is_not_a_pass():
    again = _follow("r1", flaky.RERUN, [FLAKE], failures=[FLAKE])
    cut = _follow("r2", flaky.RERUN, [FLAKE], failures_complete=False)
    dirty = _follow("r3", flaky.RERUN, [FLAKE], tested_workspace=_tested("h", clean=False))

    assert flaky.forgiveness(_verif(), [again, cut, dirty]) is None


def test_a_rerun_whose_tree_moved_while_it_ran_is_not_a_pass():
    """#2495: #2493's rerun read a clean commit, then its worker fixed the failing test while
    the pod was still copying the checkout. The rerun passed the fix, not the commit, and the
    commit's real failure was forgiven as flaky."""
    edited = _follow("r1", flaky.RERUN, [FLAKE], verified_workspace=_tested("h", clean=False))
    recommitted = _follow("r2", flaky.RERUN, [FLAKE], verified_workspace=_tested("fix"))
    unread = _follow("r3", flaky.RERUN, [FLAKE], verified_workspace=None)

    assert flaky.forgiveness(_verif(), [edited, recommitted, unread]) is None
    assert flaky.forgiveness(_verif(), [_follow("r4", flaky.RERUN, [FLAKE])]) is not None


def test_a_merge_base_run_whose_tree_moved_while_it_ran_is_no_evidence():
    verification = _verif(failures=(BROKEN,))
    reruns = [_follow(f"r{n}", flaky.RERUN, [BROKEN], failures=[BROKEN]) for n in (1, 2)]
    base = _follow(
        "b", flaky.BASE, [BROKEN], failures=[BROKEN], head="base-1",
        verified_workspace=_tested("base-1", clean=False),
    )

    assert flaky.forgiveness(verification, [*reruns, base]) is None


@pytest.mark.parametrize(
    "change",
    [
        {"verified_workspace": _tested("h", clean=False)},
        {"verified_workspace": None},
        {"failures_complete": False},
        {"command": ["pytest", "-x"]},
        {"command": ["pytest", "--maxfail=1"]},
        {"tested_workspace": _tested("h", clean=False)},
        {"failures": []},
        {"state": "done", "result_class": "success"},
        {"purpose": ""},
    ],
)
def test_a_verification_nobody_can_account_for_is_never_forgiven(change):
    assert flaky.forgiveness({**_verif(), **change}, [_follow("r", flaky.RERUN, [FLAKE])]) is None


# --- a test the branch added is its own (#2552) ----------------------------------------


def test_a_test_missing_on_the_merge_base_is_never_forgiven_even_when_a_rerun_passes():
    rerun = _follow("r", flaky.RERUN, [FLAKE])

    assert flaky.forgiveness(_verif(base_present=[]), [rerun]) is None


def test_no_follow_up_is_spent_on_a_verification_failing_on_the_branchs_own_test():
    assert flaky.follow_up(_verif(failures=(FLAKE, REAL), base_present=[FLAKE]), []) is None


def test_a_row_whose_base_could_not_be_read_forgives_nothing():
    row = _verif(base_present=None)

    assert flaky.forgiveness(row, [_follow("r", flaky.RERUN, [FLAKE])]) is None
    assert flaky.follow_up(row, []) is None


def test_a_row_that_never_recorded_the_base_forgives_nothing():
    row = _verif()
    del row["base_present"]

    assert flaky.forgiveness(row, [_follow("r", flaky.RERUN, [FLAKE])]) is None
    assert flaky.follow_up(row, []) is None


def test_a_merge_base_run_that_could_not_find_the_test_does_not_call_it_broken_there():
    """pytest exits 4 with no summary for an id the base does not have."""
    base = _follow(
        "b", flaky.BASE, [FLAKE], head="base-1", state="failed",
        result_class="failure", failures=(), failures_complete=False, exit_code=4,
    )
    reruns = [_follow(f"r{n}", flaky.RERUN, [FLAKE], failures=(FLAKE,)) for n in (1, 2)]

    assert flaky.forgiveness(_verif(), [*reruns, base]) is None


# --- the release rule reads the queue's answer ---------------------------------------


WORK = "github_issue:org/store#5"


def _verification_row(**overrides) -> dict:
    row = {**_verif(), "work_id": WORK, "verified_workspace": _tested("h")}
    row.update(overrides)
    row["tested_workspaces"] = {REPO: row["tested_workspace"]}
    row["verified_workspaces"] = {REPO: row["verified_workspace"]}
    return row


def _rerun_row() -> dict:
    """The queue's rerun that passed the flaky test on the same commit."""
    return _follow("r", flaky.RERUN, [FLAKE], work_id=WORK)


def test_a_verification_its_follow_ups_forgive_credits_its_commit():
    row = _verification_row()

    assert credited_head(row, run_id="gen-1", jobs=[row, _rerun_row()]) == "h"
    assert [item["test"] for item in forgiven_tests(row, [row, _rerun_row()])] == [FLAKE]


@pytest.mark.parametrize(
    ("overrides", "follow_ups"),
    [
        ({}, []),  # no follow-up has spoken yet: nothing is written back, nothing credits
        ({"failures": [FLAKE, REAL]}, [_follow("r", flaky.RERUN, [FLAKE])]),
        ({"failures_complete": False}, [_follow("r", flaky.RERUN, [FLAKE])]),
        ({"verified_workspace": _tested("h", clean=False)}, [_follow("r", flaky.RERUN, [FLAKE])]),
        ({"result_class": "infrastructure"}, [_follow("r", flaky.RERUN, [FLAKE])]),
    ],
)
def test_a_verification_not_forgiven_for_everything_credits_nothing(overrides, follow_ups):
    row = _verification_row(**overrides)

    assert credited_head(row, run_id="gen-1", jobs=[row, *follow_ups]) == ""


def test_the_work_names_the_forgiven_tests_beside_its_verified_commit():
    checks = run_verifications([_verification_row(), _rerun_row()], WORK)

    summary = summarize_verification(checks, "gen-1")

    assert summary.status == "verified"
    assert "after forgiving 1 failing test" in summary.label
    assert "test_a_folded_group_stays_folded" in summary.label


def test_the_verdict_survives_the_queue_forgetting_the_rows(tmp_path):
    """The follow-up is kept in the Work's evidence beside the verification, so the verdict is
    worked out the same way from the ledger alone -- nothing is amended onto the failed row."""
    ledger = RunEvidenceLedger(tmp_path)
    assert ledger.record(_verification_row())
    assert ledger.record(_rerun_row())

    rows = ledger.rows(work_id=WORK)

    [row] = [item for item in rows if item["id"] == "v"]
    assert "forgiven" not in row
    assert credited_head(row, run_id="gen-1", jobs=rows) == "h"


# --- the queue runs the follow-ups by itself ------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "master"], check=True)
    # Written rather than set one `git config` at a time: every git run is a process.
    with (path / ".git" / "config").open("a") as config:
        config.write(
            "[user]\n\temail = t@example.com\n\tname = t\n"
            '[remote "origin"]\n\turl = https://github.com/eerovil/agentdeck.git\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        )
    (path / "a.txt").write_text("one\n")
    # The merge base has the flaky test, so a failure on it is not the branch's own (#2552).
    flake_file, _, flake_name = FLAKE.partition("::")
    (path / flake_file).parent.mkdir(parents=True, exist_ok=True)
    (path / flake_file).write_text(f"def {flake_name}():\n    pass\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt", flake_file], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "one"], check=True)
    # `update-ref`, by hand: origin/master is the commit just made.
    head = (path / ".git" / "refs" / "heads" / "master").read_text()
    (path / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    (path / ".git" / "refs" / "remotes" / "origin" / "master").write_text(head)
    return path


def test_the_queue_reads_which_failing_tests_the_merge_base_already_has(tmp_path):
    """Present: an existing def, a parametrized case of it, a method. Absent: a new def in an
    old file, and anything in a file the branch added (#2552)."""
    from types import SimpleNamespace

    from agentdeck.jobs import _failures_present_on_base

    repo = _git_repo(tmp_path / "repo")
    flake_file = FLAKE.partition("::")[0]
    (repo / flake_file).write_text(
        (repo / flake_file).read_text()
        + "class TestGroup:\n    async def test_method(self):\n        pass\n"
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "base 2"], check=True)
    base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    with (repo / flake_file).open("a") as handle:
        handle.write("def test_added_here():\n    pass\n")
    (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n")
    failing = [
        FLAKE,
        f"{FLAKE}[case-2]",
        f"{FLAKE}[a::b]",
        f"{flake_file}::TestGroup::test_method",
        f"{flake_file}::test_added_here",
        "tests/test_new.py::test_new",
    ]
    log = tmp_path / "log"
    log.write_text(
        "=== short test summary info ===\n"
        + "".join(f"FAILED {test} - AssertionError\n" for test in failing)
    )
    job = SimpleNamespace(log_path=str(log), cwd=str(repo), tested_workspace={"base_head": base})

    assert set(_failures_present_on_base(job)) == set(failing[:4])
    assert _failures_present_on_base(
        SimpleNamespace(log_path=str(log), cwd=str(repo), tested_workspace={})
    ) is None


# --- the poller credits it; the runtime quarantines and files (#3055) ------------------


def _card_port(jobs: dict, **kwargs) -> FakePort:
    return FakePort(snapshots={"store#5": CardSnapshot(status="In progress")}, jobs=jobs,
                    **kwargs)


def _card_state(extra=None) -> dict:
    rec = {"status": "In progress", "generation": "gen-1", "processing": ["9"]}
    rec.update(extra or {})
    return {
        "issues": {"store#5": rec},
        "controls": {"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                           "payload": "do the thing", "status": "processing",
                           "created_at": "x"}},
    }


def test_the_poller_credits_a_forgiven_run_and_files_no_issue_itself(tmp_path):
    port = _card_port({"v": _verification_row(), "r": _rerun_row()}, opened=[77])
    store = store_for(tmp_path, _card_state())

    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    rec = state["issues"]["store#5"]
    assert "h" in rec["release_tests_pass"]
    assert _forgiven_on(port, rec, key="store#5", heads={"store#6": "h"})[0]["test"] == FLAKE
    assert any("forgiven 1 failing test(s)" in line for line in outcome.log)
    # The runtime's quarantine files the fix issue now, for Works and cards alike.
    assert port.opened_calls == []
    assert "test_fix_issues" not in state


# --- a card parked on an issue nobody works on says so --------------------------------


def test_a_card_blocked_on_an_unlabelled_open_issue_says_nothing_is_working_on_it(tmp_path):
    state = {
        "issues": {"store#5": {"status": "Blocked", "blocked_until": ["store#9"]}},
    }
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Blocked")},
        ref_states={"store#9": "OPEN"},
    )
    store = store_for(tmp_path, state)

    run_wake(store, port, now=_NOW)
    run_wake(store, port, now=_NOW)

    notes = [
        effect for effect in port.applied
        if effect.kind is EffectKind.COMMENT and "Nothing is working on" in str(effect.value)
    ]
    assert len(notes) == 1, "said once, not every wake"
    assert "`store#9`" in str(notes[0].value)
    labels = [effect for effect in port.applied if effect.kind is EffectKind.ADD_OWNERSHIP]
    assert labels == [], "the engine never labels someone else's issue"


def test_a_card_blocked_on_a_card_the_poller_holds_gets_no_such_note(tmp_path):
    state = {
        "issues": {
            "store#5": {"status": "Blocked", "blocked_until": ["store#9"]},
            "store#9": {"status": "In progress"},
        },
    }
    port = FakePort(
        snapshots={
            "store#5": CardSnapshot(status="Blocked"),
            "store#9": CardSnapshot(status="In progress"),
        },
        ref_states={"store#9": "OPEN"},
    )
    store = store_for(tmp_path, state)

    run_wake(store, port, now=_NOW)

    assert not any(
        "Nothing is working on" in str(effect.value)
        for effect in port.applied
        if effect.kind is EffectKind.COMMENT
    )


# --- the queue brings the evidence itself (review of #2386) ---------------------------


def test_a_failed_verification_gets_its_failing_tests_rerun_on_the_same_commit():
    assert flaky.follow_up(_verif(), [_verif()]) == flaky.FollowUp(flaky.RERUN, "v", (FLAKE,))


def test_the_second_rerun_only_takes_the_tests_no_rerun_has_passed():
    verification = _verif(failures=(FLAKE, BROKEN))
    first = _follow("r1", flaky.RERUN, [FLAKE, BROKEN], failures=[BROKEN])

    plan = flaky.follow_up(first, [verification, first])

    assert plan == flaky.FollowUp(flaky.RERUN, "v", (BROKEN,))


def test_after_two_reruns_what_still_fails_runs_once_on_the_merge_base_and_then_nothing():
    verification = _verif(failures=(BROKEN,))
    reruns = [_follow(f"r{n}", flaky.RERUN, [BROKEN], failures=[BROKEN]) for n in (1, 2)]
    jobs = [verification, *reruns]

    assert flaky.follow_up(reruns[1], jobs) == flaky.FollowUp(flaky.BASE, "v", (BROKEN,))

    base = _follow("b", flaky.BASE, [BROKEN], head="base-1")
    assert flaky.follow_up(base, [*jobs, base]) is None, (
        "bounded: two reruns and one merge-base run per verification"
    )


def test_no_merge_base_run_without_a_known_merge_base():
    verification = _verif(failures=(BROKEN,), base="")
    reruns = [_follow(f"r{n}", flaky.RERUN, [BROKEN], failures=[BROKEN]) for n in (1, 2)]

    assert flaky.follow_up(reruns[1], [verification, *reruns]) is None


@pytest.mark.parametrize(
    "verification",
    [
        _verif(failures=tuple(f"tests/t.py::test_{n}" for n in range(11))),
        _verif(command=["pytest", "-x"]),
        _verif(failures_complete=False),
        {**_verif(), "purpose": ""},
    ],
)
def test_nothing_is_followed_up_that_could_not_be_forgiven_or_is_already(verification):
    assert flaky.follow_up(verification, [verification]) is None


def test_nothing_more_is_run_once_the_verification_is_forgiven():
    rerun = _follow("r1", flaky.RERUN, [FLAKE])

    assert flaky.follow_up(rerun, [_verif(), rerun]) is None


def test_one_follow_up_at_a_time():
    running = _follow("r1", flaky.RERUN, [FLAKE], state="running")

    assert flaky.follow_up(_verif(), [_verif(), running]) is None


@pytest.mark.parametrize(
    "ended",
    [
        {"state": "failed", "exit_code": 143},
        {"state": "failed", "exit_code": 75},
        # The connection to the pod dropped before pytest started (#2475).
        {"state": "failed", "exit_code": 69},
        {"state": "interrupted", "exit_code": None},
    ],
)
def test_a_verification_with_no_test_result_is_resubmitted_exactly_once(ended):
    cut = {"id": "v", "purpose": VERIFICATION, "failures_complete": False, **ended}

    assert flaky.follow_up(cut, [cut]) == flaky.FollowUp(flaky.RESUBMIT, "v")
    again = {**cut, "id": "v2", "follows": "v", "follow_kind": flaky.RESUBMIT}
    assert flaky.follow_up(cut, [cut, again]) is None
    assert flaky.follow_up(again, [cut, again]) is None


def test_an_ordinary_failure_or_a_crash_on_start_is_not_resubmitted():
    assert flaky.follow_up(
        {"id": "v", "purpose": VERIFICATION, "state": "failed", "exit_code": 1}, []
    ) is None
    assert flaky.follow_up(
        {"id": "v", "purpose": VERIFICATION, "state": "failed", "exit_code": None}, []
    ) is None


def test_the_run_is_still_waiting_while_a_follow_up_runs_and_a_rejection_cancels_it():
    from agentdeck.poller_engine.verification import awaited_run_job, unfinished_jobs

    done = {**_verif(), "notified": True, "session_key": "s"}
    rerun = _follow("r1", flaky.RERUN, [FLAKE], state="running", session_key="s")
    elsewhere = _follow("x", flaky.RERUN, [FLAKE], of="someone-else", state="running")

    assert awaited_run_job([done, rerun, elsewhere], run_id="gen-1") == ("r1", "")
    assert unfinished_jobs([done, rerun, elsewhere], run_id="gen-1") == ["r1"]


def _once_flaky_pytest(tmp_path: Path) -> Path:
    """A `pytest` that fails one test the first time it runs, and passes after that, noting
    the directory and commit each later run had."""
    ran = tmp_path / "ran-once"
    script = tmp_path / "bin" / "pytest"
    script.parent.mkdir(exist_ok=True)
    # A shell script, not Python: a fresh interpreter per run is most of its cost.
    script.write_text(
        "#!/bin/sh\n"
        f"if [ ! -e '{ran}' ]; then\n"
        f"    : >'{ran}'\n"
        "    echo '=== short test summary info ==='\n"
        f"    echo 'FAILED {FLAKE} - AssertionError'\n"
        "    exit 1\n"
        "fi\n"
        f"echo \"$(pwd -P) $(git rev-parse HEAD)\" >>'{tmp_path / 'reran-at'}'\n"
        "echo \"ran with $* ${AGENTDECK_QUARANTINED_TESTS-None}\"\n"
        "echo '1 passed'\n"
    )
    script.chmod(0o755)
    return script


async def _until(queue, predicate, what):
    from test_jobs import _wait_for

    return await _wait_for(predicate, timeout=120.0, what=what)


def _rerun_with(monkeypatch, pytest_bin: Path) -> None:
    """A pinned checkout runs `uv run pytest`; these fakes stand in for it."""
    from agentdeck import jobs

    monkeypatch.setattr(jobs, "_checkout_pytest", lambda: [str(pytest_bin)])


async def test_the_queue_reruns_a_failed_verification_by_itself_and_forgives_it(
    tmp_path, monkeypatch
):
    """...and quarantines the flaky test, so the next verification leaves it out (#3055)."""
    from test_jobs import _queue

    from agentdeck.flaky_quarantine import Quarantine

    repo = _git_repo(tmp_path / "repo")
    pytest_bin = _once_flaky_pytest(tmp_path)
    _rerun_with(monkeypatch, pytest_bin)
    queue = _queue(tmp_path)
    quarantine = Quarantine(
        tmp_path / "quarantine.json",
        labels=lambda repository: "agent" if repository == "eerovil/agentdeck" else "",
    )
    queue._quarantine = quarantine
    await queue.start()
    try:
        failed = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
            lane_defaulted=True,
        )
        def rows():
            return [job.to_dict() for job in queue.list()]

        await _until(
            queue, lambda: forgiven_tests(queue.get(failed.id).to_dict(), rows()),
            "the verification to be forgiven",
        )
        forgiven = queue.get(failed.id)
        [rerun] = [job for job in queue.list() if job.follows == failed.id]
        rerun = await _until(queue, lambda: queue.get(rerun.id).is_final and queue.get(rerun.id),
                             "the rerun to finish")
        later = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-2", work_id="github_issue:org/store#6",
        )
        later = await _until(queue, lambda: queue.get(later.id).is_final and queue.get(later.id),
                             "the next verification to finish")
    finally:
        await queue.stop()

    # Quarantined on its first forgiveness: the next verification leaves it out and says so,
    # and the fix issue waits for the runtime's sweep to file it.
    assert quarantine.tests("EeroVil/AgentDeck") == (FLAKE,)
    assert "Quarantined until a fix issue for each closes" in rerun.note
    assert later.quarantined == (FLAKE,)
    ran = Path(later.log_path).read_text()
    assert f'ran with -q -p agentdeck_quarantine ["{FLAKE}"]' in ran
    assert "Left out 1 quarantined test" in later.note
    [record] = quarantine.records()
    assert (record["label"], record["verification"], record["issue"]) == ("agent", failed.id, None)
    assert rerun.follow_kind == flaky.RERUN
    assert rerun.command == [str(pytest_bin), "-q", FLAKE]
    # #3040: as free to move as the verification was, so a full slot here sends it to a pod.
    assert (rerun.lane, rerun.lane_defaulted) == (failed.lane, True)
    assert f"as job {rerun.id}" in queue.get(failed.id).note
    head = forgiven.tested_workspace["head"]
    assert credited_head(queue.get(failed.id).to_dict(), run_id="gen-1", jobs=rows()) == head
    assert f"Verification job {failed.id} on {head[:12]} now counts as green" in rerun.note
    # Kept beside the verification in the Work's evidence, with what it was for.
    evidence = RunEvidenceLedger(tmp_path / "jobs" / "run_evidence").rows(work_id=WORK)
    [kept] = [row for row in evidence if row["id"] == rerun.id]
    assert (kept["follows"], kept["follow_tests"]) == (failed.id, [FLAKE])
    assert len([job for job in queue.list() if job.follows]) == 1, "nothing more was needed"
    assert rerun.cwd == str(repo / ".worktrees" / f"verify-rerun-{head[:12]}")


async def test_a_rerun_of_a_verification_that_tested_another_checkout_stays_here(
    tmp_path, monkeypatch
):
    """#3040 review: such a verification runs here only because a pod receives just its own
    checkout, and its rerun does not carry the other one, so the rerun must not spill."""
    from test_jobs import _queue

    repo = _git_repo(tmp_path / "repo")
    other = tmp_path / "other"
    other.mkdir()
    pytest_bin = _once_flaky_pytest(tmp_path)
    _rerun_with(monkeypatch, pytest_bin)
    queue = _queue(tmp_path)
    await queue.start()
    try:
        failed = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
            lane_defaulted=True, workspaces=[str(other)],
        )
        rerun = await _until(
            queue,
            lambda: next((job for job in queue.list() if job.follows == failed.id), None),
            "the rerun to be submitted",
        )
        await _until(queue, lambda: queue.get(rerun.id).is_final, "the rerun to finish")
    finally:
        await queue.stop()

    assert queue.get(failed.id).lane_defaulted
    assert (rerun.follow_kind, rerun.lane_defaulted) == (flaky.RERUN, False)


async def test_a_rerun_tests_the_verified_commit_after_the_worktree_moves_on(
    tmp_path, monkeypatch
):
    """#2625: the worker's next commit and edits land in its worktree before the rerun
    starts; the rerun still runs the verified commit, and its checkout goes once it is done."""
    from test_jobs import _queue

    from agentdeck import jobs

    repo = _git_repo(tmp_path / "repo")
    pytest_bin = _once_flaky_pytest(tmp_path)
    _rerun_with(monkeypatch, pytest_bin)
    planned = flaky.follow_up

    def worker_moves_on(finished, rows):
        plan = planned(finished, rows)
        if plan is not None and plan.kind == flaky.RERUN:
            (repo / "a.txt").write_text("next commit\n")
            subprocess.run(["git", "-C", str(repo), "commit", "-qam", "next"], check=True)
            (repo / "a.txt").write_text("still editing\n")
        return plan

    monkeypatch.setattr(jobs.flaky, "follow_up", worker_moves_on)
    queue = _queue(tmp_path)
    await queue.start()
    try:
        failed = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
        )
        rows = lambda: [job.to_dict() for job in queue.list()]  # noqa: E731
        await _until(
            queue, lambda: forgiven_tests(queue.get(failed.id).to_dict(), rows()),
            "the verification to be forgiven",
        )
        [rerun] = [job for job in queue.list() if job.follows == failed.id]
        checkout = Path(rerun.cwd)
        await _until(queue, lambda: not checkout.exists(), "the rerun's checkout to go")
    finally:
        await queue.stop()

    head = queue.get(failed.id).tested_workspace["head"]
    assert (tmp_path / "reran-at").read_text().split() == [str(checkout), head]
    assert queue.get(rerun.id).tested_workspace["head"] == head
    assert f"the same commit {head[:12]}" in queue.get(failed.id).note
    assert (repo / "a.txt").read_text() == "still editing\n", "the worktree is left alone"


async def test_a_verification_of_an_uncommitted_tree_says_why_it_gets_no_rerun(tmp_path):
    from test_jobs import _queue

    repo = _git_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("uncommitted\n")
    pytest_bin = _once_flaky_pytest(tmp_path)
    queue = _queue(tmp_path)
    await queue.start()
    try:
        failed = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
        )
        await _until(queue, lambda: queue.get(failed.id).is_final, "the verification")
    finally:
        await queue.stop()

    assert [job for job in queue.list() if job.follows] == []
    assert (
        "The verified tree had uncommitted changes, so its failures cannot be rerun on the "
        "same commit." in queue.get(failed.id).note
    )


def _fixing_pytest(tmp_path: Path) -> Path:
    """A `pytest` that fails one test, then on the rerun edits the checkout it runs in and
    passes, as #2493's worker did by fixing the test while the rerun's pod was copying it."""
    ran = tmp_path / "ran-once"
    script = tmp_path / "bin" / "pytest"
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"ran = pathlib.Path({str(ran)!r})\n"
        "if not ran.exists():\n"
        "    ran.touch()\n"
        "    print('=== short test summary info ===')\n"
        f"    print('FAILED {FLAKE} - AssertionError')\n"
        "    sys.exit(1)\n"
        "pathlib.Path('a.txt').write_text('fixed\\n')\n"
        "print('1 passed')\n"
    )
    script.chmod(0o755)
    return script


async def test_the_queue_does_not_forgive_a_failure_its_rerun_passed_on_an_edited_tree(
    tmp_path, monkeypatch
):
    """#2495: the queue reads a follow-up's tree again after it ran, so a rerun that passed
    because the checkout changed underneath it forgives nothing."""
    from test_jobs import _queue

    repo = _git_repo(tmp_path / "repo")
    pytest_bin = _fixing_pytest(tmp_path)
    _rerun_with(monkeypatch, pytest_bin)
    queue = _queue(tmp_path)
    await queue.start()
    try:
        failed = queue.submit(
            command=[str(pytest_bin), "-q"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
        )

        def first_rerun():
            reruns = [job for job in queue.list() if job.follows == failed.id]
            return reruns and reruns[0].is_final and reruns[0]

        rerun = await _until(queue, first_rerun, "the first rerun to finish")
        rows = [job.to_dict() for job in queue.list()]
    finally:
        await queue.stop()

    assert rerun.state == "done", "the rerun itself passed"
    assert rerun.tested_workspace["tracked_diff_sha256"] == _EMPTY
    assert rerun.verified_workspace["tracked_diff_sha256"] != _EMPTY
    assert forgiven_tests(queue.get(failed.id).to_dict(), rows) == ()
    assert credited_head(queue.get(failed.id).to_dict(), run_id="gen-1", jobs=rows) == ""


async def test_a_killed_verification_is_resubmitted_once_by_the_queue(tmp_path):
    from test_jobs import _queue

    repo = _git_repo(tmp_path / "repo")
    queue = _queue(tmp_path)
    await queue.start()
    try:
        killed = queue.submit(
            command=[sys.executable, "-c", "import sys; sys.exit(143)"], cwd=str(repo),
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:org/store#5",
            max_minutes=0,
        )
        resubmitted = await _until(
            queue,
            lambda: [job for job in queue.list() if job.follows == killed.id and job.is_final],
            "the resubmission to finish",
        )
        await _until(queue, lambda: queue.get(killed.id).is_final, "the first run")
    finally:
        await queue.stop()

    [again] = resubmitted
    assert (again.follow_kind, again.purpose, again.run_id) == (
        flaky.RESUBMIT, VERIFICATION, "gen-1"
    )
    assert again.exit_code == 143
    # The retry keeps the submitter's time limit (#3038): here, none at all.
    assert (again.max_minutes, again.time_limit_s) == (0, None)
    assert [job.id for job in queue.list()] == [killed.id, again.id], "resubmitted once only"


@pytest.mark.parametrize("prefix", ["verify-base-", "verify-rerun-"])
def test_a_pinned_checkout_is_made_detached_and_removed_again(tmp_path, prefix):
    from agentdeck.jobs import JobError, _pinned_checkout, _remove_pinned_checkout

    repo = _git_repo(tmp_path / "repo")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    path = _pinned_checkout(str(repo), sha, prefix)

    assert Path(path) == repo / ".worktrees" / f"{prefix}{sha[:12]}"
    assert _pinned_checkout(str(repo), sha, prefix) == path, "reused while it is still clean"
    (Path(path) / "a.txt").write_text("changed\n")
    assert _pinned_checkout(str(repo), sha, prefix) == path
    assert (Path(path) / "a.txt").read_text() != "changed\n", "a dirty one is made again"
    _remove_pinned_checkout(path)
    assert not Path(path).exists()
    _remove_pinned_checkout(str(repo))  # anything it did not make is left alone
    assert repo.exists()
    with pytest.raises(JobError):
        _pinned_checkout(str(repo), sha, "issue-")


def test_the_release_comment_reads_the_forgiven_tests_off_the_verification_that_made_it_green():
    port = FakePort(jobs={
        "v": _verification_row(),
        "r": _rerun_row(),
        "other": _verification_row(id="other", run_id="gen-0", work_id="github_issue:org/x#1"),
    })
    rec = {"generation": "gen-1"}

    forgiven = _forgiven_on(port, rec, key="store#5", heads={"store#6": "h"})

    assert [item["test"] for item in forgiven] == [FLAKE]
    assert "flaky: a rerun on the same commit h passed it (job r)" in flaky.describe(forgiven)
    assert _forgiven_on(port, rec, key="store#5", heads={"store#6": "moved"}) == []
