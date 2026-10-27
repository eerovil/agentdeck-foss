"""Whether a failed verification failed only on tests its commit did not break (#2382).

On 2026-09-28 a flaky browser test and a test already broken on ``master`` failed most
full-suite runs whatever the branch, and parked cards for about twenty card-hours.

When a verification fails on a few tests, the job queue runs follow-ups by itself and records
on each one exactly what it is: which verification it follows (``follows``), what for
(``follow_kind``) and which tests it ran (``follow_tests``). Nothing here parses a command or
reads another card's runs; the evidence is the verification and its own follow-ups.

``rerun``
    The failing tests again, on the same commit, at most ``MAX_RERUNS`` times. A test a rerun
    passes is ``flaky``: nothing about the tree changed, so the failure was not the commit's.
    Every run here, the verification included, counts only if its tree held still while it ran.
``base``
    What still fails, once, on the verification's own merge base. A test that fails there
    too is ``base_broken``: the branch did not introduce it.
``resubmit``
    A verification that ended with no test result at all (no room on the cluster, killed,
    cut off by a restart), submitted again once.

A failure on a test the merge base does not have is the branch's own and is never forgiven,
not even when a rerun passes it (#2552): a racy test a branch adds is that branch's bug. The
queue records which failures exist on the merge base as ``base_present``; a row without that
record, or with ``None`` because the queue could not tell, proves nothing and forgives nothing.

A verification is forgiven only when every one of its failures is; anything else is the
branch's regression and stays red. ``forgiveness`` is the one place that says so, worked out
each time from the verification and its follow-ups -- nothing writes a verdict back onto the
failed row. Pure: the queue carries the plan out and records results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .workspace import WorkspaceSnapshot

__all__ = [
    "BASE",
    "BASE_BROKEN",
    "DIRTY",
    "FLAKY",
    "MOVED",
    "MAX_RERUNS",
    "RERUN",
    "RESUBMIT",
    "FollowUp",
    "describe",
    "follow_up",
    "forgiveness",
    "no_test_result",
    "unfollowable_tree",
]

RERUN = "rerun"
BASE = "base"
RESUBMIT = "resubmit"
FLAKY = "flaky"
BASE_BROKEN = "base_broken"
# Why a failed verification's tree rules out a same-commit rerun (#2625).
DIRTY = "dirty"
MOVED = "moved"

MAX_RERUNS = 2
# Past this many failures the branch most likely broke something; rerunning them would only
# spend a lane to learn it.
MAX_FOLLOWED_FAILURES = 10
# Exit codes of a run that ended without a test result: no room on the cluster (75), the
# connection to the pod dropped before pytest started (69, #2475), or killed (143 SIGTERM,
# 137 SIGKILL, as a shell or as a signal).
_INCOMPLETE_EXITS = frozenset({69, 75, 137, 143, -9, -15})
# Options that let pytest stop before it reached every test, so its failure list does not
# say the rest passed.
_STOPS_EARLY = frozenset({"-x", "--exitfirst", "--maxfail", "--sw", "--stepwise"})


@dataclass(frozen=True)
class FollowUp:
    """One job the queue should submit for a verification."""

    kind: str  # RERUN | BASE | RESUBMIT
    verification_id: str
    tests: tuple[str, ...] = ()


def _tested(job: dict) -> WorkspaceSnapshot:
    return WorkspaceSnapshot.from_record(job.get("tested_workspace"))


def _result(job: dict) -> bool:
    """The job finished with a complete test result over a clean tree that held still.

    The tree is read once before the command starts and again after it exits. A rerun of
    #2493's verification read a clean commit, then its worker fixed the failing test while the
    pod was still copying the checkout, so the rerun passed the fix and forgave the commit
    (#2495). Only a matching second reading says the command saw the tree the first one names;
    a row with none (an older runtime) proves nothing.
    """
    tested = _tested(job)
    after = WorkspaceSnapshot.from_record(job.get("verified_workspace"))
    return (
        str(job.get("result_class") or "") in {"success", "failure"}
        and bool(job.get("failures_complete"))
        and tested.clean
        and after.clean
        and after.snapshot_id == tested.snapshot_id
    )


def _followable(verification: dict) -> bool:
    """A failed verification whose failures can be attributed at all."""
    return _result(verification) and _followable_but_for_tree(verification)


def _followable_but_for_tree(verification: dict) -> bool:
    failures = verification.get("failures") or ()
    return (
        verification.get("purpose") == "verification"
        and str(verification.get("state") or "") == "failed"
        and str(verification.get("result_class") or "") == "failure"
        and bool(verification.get("failures_complete"))
        and 0 < len(failures) <= MAX_FOLLOWED_FAILURES
        and not any(
            str(word).split("=", 1)[0] in _STOPS_EARLY for word in verification.get("command") or ()
        )
    )


def unfollowable_tree(verification: object) -> str | None:
    """``DIRTY`` or ``MOVED`` when only the tree it tested keeps this verification's failures
    from a same-commit rerun, else None (#2625).

    A rerun runs in a checkout of the verified head, which is only the tree that failed when
    that tree was clean and held still while the verification ran. Each reason is named only
    when a snapshot that was actually taken shows it: a missing or partial one proves nothing
    either way, so it gets no reason rather than a wrong one.
    """
    if not isinstance(verification, dict) or not _followable_but_for_tree(verification):
        return None
    if _result(verification):
        return None
    tested = _tested(verification)
    if not tested.provable:
        return None
    if not tested.clean:
        return DIRTY
    after = WorkspaceSnapshot.from_record(verification.get("verified_workspace"))
    if after.provable and after.snapshot_id != tested.snapshot_id:
        return MOVED
    return None


def _follow_ups(verification: dict, jobs: Iterable[dict], kind: str) -> list[dict]:
    wanted = str(verification.get("id") or "")
    return [
        job
        for job in jobs
        if wanted and str(job.get("follows") or "") == wanted and job.get("follow_kind") == kind
    ]


def _passed_on_rerun(verification: dict, reruns: Iterable[dict]) -> dict[str, str]:
    """Each failure a same-commit rerun passed, with that rerun's id."""
    head = _tested(verification).head
    passed: dict[str, str] = {}
    for rerun in reruns:
        if not _result(rerun) or _tested(rerun).head != head:
            continue
        requested = {str(test) for test in rerun.get("follow_tests") or ()}
        failures = {str(test) for test in rerun.get("failures") or ()}
        # A failed rerun proves the others passed only when every failure it reported is one
        # of the tests it was asked to run. A collection or setup error names a file or module
        # instead, and then none of the requested tests may have run at all.
        if not failures <= requested:
            continue
        for test in sorted(requested - failures):
            passed.setdefault(test, str(rerun.get("id") or ""))
    return passed


def _failed_on_base(verification: dict, bases: Iterable[dict]) -> dict[str, str]:
    """Each failure the merge-base run failed too, with that run's id."""
    base = _tested(verification).base_head
    failed: dict[str, str] = {}
    for run in bases:
        if not base or not _result(run) or _tested(run).head != base:
            continue
        ran = {str(test) for test in run.get("follow_tests") or ()}
        for test in run.get("failures") or ():
            if str(test) in ran:
                failed.setdefault(str(test), str(run.get("id") or ""))
    return failed


def _all_on_base(verification: dict) -> bool:
    """Whether the queue recorded every failure as a test the merge base already has."""
    present = verification.get("base_present")
    if not isinstance(present, (list, tuple)):
        return False
    have = {str(test) for test in present}
    return all(str(test) in have for test in verification.get("failures") or ())


def forgiveness(verification: dict, jobs: Iterable[dict]) -> tuple[dict, ...] | None:
    """Every failure of this verification with the follow-up that forgives it -- or None."""
    if not isinstance(verification, dict) or not _followable(verification):
        return None
    if not _all_on_base(verification):
        return None
    jobs = [job for job in jobs if isinstance(job, dict)]
    tested = _tested(verification)
    passed = _passed_on_rerun(verification, _follow_ups(verification, jobs, RERUN))
    broken = _failed_on_base(verification, _follow_ups(verification, jobs, BASE))
    forgiven: list[dict] = []
    for test in verification.get("failures") or ():
        if test in passed:
            forgiven.append(_forgiven(test, FLAKY, tested.head, passed[test]))
        elif test in broken:
            forgiven.append(_forgiven(test, BASE_BROKEN, tested.base_head, broken[test]))
        else:
            return None
    return tuple(forgiven)


def _forgiven(test: str, kind: str, commit: str, job_id: str) -> dict:
    return {"test": str(test), "kind": kind, "commit": commit, "job_id": job_id}


def follow_up(finished: dict, jobs: Sequence[dict]) -> FollowUp | None:
    """What the queue should run next because ``finished`` just finished, if anything.

    One follow-up at a time, bounded per verification: ``MAX_RERUNS`` reruns of the failures
    no rerun has passed yet, then one run of those on the merge base; and a verification that
    ended with no test result is resubmitted once, never a resubmission again.
    """
    if not isinstance(finished, dict):
        return None
    jobs = [job for job in jobs if isinstance(job, dict)]
    if finished.get("purpose") == "verification" and _incomplete(finished):
        if finished.get("follow_kind") == RESUBMIT or _follow_ups(finished, jobs, RESUBMIT):
            return None
        return FollowUp(RESUBMIT, str(finished.get("id") or ""))
    if finished.get("follow_kind") in {RERUN, BASE}:
        wanted = str(finished.get("follows") or "")
        verification = next((job for job in jobs if job.get("id") == wanted), None)
    else:
        verification = finished
    if verification is None or not _followable(verification):
        return None
    if not _all_on_base(verification):
        return None  # nothing a follow-up finds could forgive it, so spend no lane on it
    if forgiveness(verification, jobs):
        return None
    mine = [job for job in jobs if str(job.get("follows") or "") == verification.get("id")]
    if any(str(job.get("state") or "") in {"queued", "running"} for job in mine):
        return None
    reruns = _follow_ups(verification, mine, RERUN)
    passed = _passed_on_rerun(verification, reruns)
    remaining = tuple(test for test in verification["failures"] if test not in passed)
    if not remaining:
        return None
    if len(reruns) < MAX_RERUNS:
        return FollowUp(RERUN, str(verification["id"]), remaining)
    if _tested(verification).base_head and not _follow_ups(verification, mine, BASE):
        return FollowUp(BASE, str(verification["id"]), remaining)
    return None


def no_test_result(job: object) -> bool:
    """Whether this finished job ended before it had a test result to give (#2475)."""
    return isinstance(job, dict) and _incomplete(job)


def _incomplete(job: dict) -> bool:
    if bool(job.get("failures_complete")):
        return False
    if str(job.get("state") or "") == "interrupted":
        return True
    return str(job.get("state") or "") == "failed" and job.get("exit_code") in _INCOMPLETE_EXITS


_MAX_LISTED = 10


def describe(forgiven: Sequence[dict]) -> str:
    """One line per forgiven test, saying why."""
    lines = []
    for item in list(forgiven)[:_MAX_LISTED]:
        commit = str(item.get("commit") or "")[:12]
        job = str(item.get("job_id") or "")
        if item.get("kind") == BASE_BROKEN:
            why = f"also fails on the merge base {commit} (job {job})"
        else:
            why = f"flaky: a rerun on the same commit {commit} passed it (job {job})"
        lines.append(f"- `{item.get('test')}` — {why}")
    omitted = len(forgiven) - len(lines)
    if omitted > 0:
        lines.append(f"- … and {omitted} more")
    return "\n".join(lines)
