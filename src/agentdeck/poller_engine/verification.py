"""Which finished jobs are verification evidence for one Run (#2154).

A Run proves its work by submitting an explicit verification job — ``agentdeck job submit
--verification`` — and the queue records what that job actually tested. There is no verify
phase, no checkpoint and nothing to wait on: the result is evidence attached to the Run, read
whenever the engine looks.

A job credits the release tests bit only when every one of these is authoritative:

* it was submitted *as verification*, not merely something that looks like a suite;
* it names this exact Run (``run_id`` is the poller generation, #2142), and the same Work
  when both sides know one;
* it finished successfully -- or it failed only on tests its own follow-ups showed were not
  the commit's doing, as ``flaky.forgiveness`` works out from it and them (#2382);
* the workspace it tested is provable and clean, so its commit describes the tree that ran;
* the same workspace, read again after the command exited, is identical — so nothing edited,
  committed or checked out underneath the suite while it ran;
* the commit credited is the tested head itself.

Anything unproved credits nothing, which only ever leaves the bit red.

A job can test more than one repository (#2148). It then records one snapshot per
repository, keyed by the repository and not by a path, and every one of them is held to the
same rules: resolved to a named repository, clean, and unchanged across the run. The head it
credits is still the one in the directory the job ran in, because the release bit is kept per
commit of the pull request being merged; the other repositories make the evidence stricter,
never looser, and ``compare`` is what says whether a recorded set still describes a workspace.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import flaky
from .workspace import UNRESOLVED_REPOSITORY, WorkspaceSnapshot, repository_snapshots

__all__ = [
    "PURPOSE",
    "EvidenceCheck",
    "awaited_run_job",
    "compare",
    "canonical",
    "credited_head",
    "credited_head_for_work",
    "describe",
    "ended_untested",
    "failed_head_for_work_alone",
    "forgiven_tests",
    "snapshots_after",
    "snapshots_tested",
    "unfinished_for_heads",
    "unfinished_jobs",
]

# The one purpose a job may carry that makes it release evidence.
PURPOSE = "verification"

# The queue's own words for a finished, successful run (``agentdeck.jobs``).
_DONE = "done"
_SUCCESS = "success"
# And for a run that has not finished yet.
_UNFINISHED = frozenset({"queued", "running"})


def credited_head(
    job: object, *, run_id: str, work_id: str = "", jobs: Iterable[object] = ()
) -> str:
    """The commit this job proves green for the Run, or ``""`` when it proves nothing.

    ``jobs`` are the rows around it; a failed verification's follow-ups among them can make
    it count (#2382).
    """
    if not isinstance(job, dict) or not run_id:
        return ""
    if not _belongs_to_run(job, run_id=run_id, work_id=work_id):
        return ""
    return _proven_head(job, jobs)


def _proven_head(job: dict, jobs: Iterable[object]) -> str:
    """The clean, held commit a verification job that belongs proves green, or ""."""
    if not _passed(job) and not forgiven_tests(job, jobs):
        return ""
    tested = WorkspaceSnapshot.from_record(job.get("tested_workspace"))
    if not tested.clean:
        return ""
    # The first reading is taken before the command starts. Without a matching second one
    # after it exits, a tree that moved mid-run would still credit the clean commit it started
    # from; a job with no second reading (an older runtime) proves nothing either.
    after = WorkspaceSnapshot.from_record(job.get("verified_workspace"))
    if not after.clean or after.snapshot_id != tested.snapshot_id:
        return ""
    if not _every_repository_held(job, tested):
        return ""
    return tested.head


def _passed(job: dict) -> bool:
    return str(job.get("state") or "") == _DONE and str(job.get("result_class") or "") == _SUCCESS


def forgiven_tests(job: object, jobs: Iterable[object] = ()) -> tuple[dict, ...]:
    """Each failure of a failed verification with the follow-up that forgives it (#2382).

    Empty for a green run, and for a failed one not forgiven for every failure.
    """
    if not isinstance(job, dict):
        return ()
    return flaky.forgiveness(job, [row for row in jobs if isinstance(row, dict)]) or ()


def credited_head_for_work(job: object, *, work_id: str, jobs: Iterable[object] = ()) -> str:
    """The commit a verification job of *any* Run of this Work proves green (#2306).

    A Run's own job is the ordinary case. This is the handoff: the worker that submitted the
    suite was replaced by a newer generation of the same card while the suite was still
    running, so the job names a Run the card no longer holds. What it proves is still a fact
    about one commit, held to every rule ``credited_head`` applies, so the card's tests bit may
    take it. A rebase round's provenance (#2122) is still bound only by that round's own Run's
    job: the current Run's, or an ended rebase round's kept under its Run (#2449).

    Both sides must name the Work, compared as the canonical lower-case id. A job with no Work,
    or a card whose Work is unknown, credits nothing here.
    """
    if not isinstance(job, dict):
        return ""
    wanted = str(work_id or "").strip().lower()
    named = str(job.get("work_id") or "").strip().lower()
    run_id = str(job.get("run_id") or "")
    if not wanted or named != wanted or not run_id:
        return ""
    # The Work was matched above, case-folded; ``credited_head`` would compare it exactly.
    return credited_head(job, run_id=run_id, jobs=jobs)


def credited_head_for_work_alone(job: object, *, work_id: str, jobs: Iterable[object] = ()) -> str:
    """The commit a verification job of this Work proves green, with or without a Run (#2895).

    For the deck's own release, which merges only Works no Poller card holds: such a Work's
    chat is bound to the Work alone, so its jobs name no Run. Held to the same rules about what
    a job proves as ``credited_head``. The Poller never reads this: a card credits only its
    Runs' jobs (``credited_head_for_work``), so a Run-less job is never its evidence.
    """
    if not isinstance(job, dict):
        return ""
    wanted = str(work_id or "").strip().lower()
    named = str(job.get("work_id") or "").strip().lower()
    if not wanted or named != wanted or str(job.get("purpose") or "") != PURPOSE:
        return ""
    return _proven_head(job, jobs)


def moved_head_for_work_alone(job: object, *, work_id: str) -> str:
    """The commit a passing verification of this Work ran on but cannot prove, or "" (#3097).

    The command passed on a clean tree, and the tree was not that same clean tree when it
    exited: a test or a tool wrote to the checkout. ``_proven_head`` rightly credits nothing,
    and the Work's line says why rather than "no test run started".
    """
    if not isinstance(job, dict) or not _passed(job):
        return ""
    wanted = str(work_id or "").strip().lower()
    if not wanted or str(job.get("work_id") or "").strip().lower() != wanted:
        return ""
    if str(job.get("purpose") or "") != PURPOSE:
        return ""
    tested = WorkspaceSnapshot.from_record(job.get("tested_workspace"))
    after = WorkspaceSnapshot.from_record(job.get("verified_workspace"))
    if not tested.clean or (after.clean and after.snapshot_id == tested.snapshot_id):
        return ""
    # A second reading that failed says nothing about whether the tree changed (#3397 review).
    if not after.provable:
        return ""
    return tested.head


def unclean_head_for_work_alone(job: object, *, work_id: str) -> str:
    """The commit a passing verification of this Work ran on from an unclean tree, or "" (#3397).

    The checkout had uncommitted or untracked files before the command started, so
    ``_proven_head`` credits nothing; the Work's line says why rather than "no test run
    started". One card sat six hours on empty ``*.pyc.<pid>`` files a
    disk-full pytest left behind.
    """
    if not isinstance(job, dict) or not _passed(job):
        return ""
    wanted = str(work_id or "").strip().lower()
    if not wanted or str(job.get("work_id") or "").strip().lower() != wanted:
        return ""
    if str(job.get("purpose") or "") != PURPOSE:
        return ""
    tested = WorkspaceSnapshot.from_record(job.get("tested_workspace"))
    # Only a tree read in full is called unclean: an unprovable one may have been clean.
    return tested.head if tested.provable and not tested.clean else ""


def failed_head_for_work_alone(job: object, *, work_id: str, jobs: Iterable[object] = ()) -> str:
    """The commit a verification job of this Work proves red, settled, or "" (#3022).

    The deck's merge queue drops a pull request whose re-test failed. Only a settled failure
    counts: the command ran and failed (not the runner), on a clean tree that held still, no
    job of this Work is still queued or running (a rerun or merge-base follow-up may yet
    forgive it, #2382), and the follow-ups that did run forgave nothing.
    """
    if not isinstance(job, dict):
        return ""
    wanted = str(work_id or "").strip().lower()
    if not wanted or str(job.get("work_id") or "").strip().lower() != wanted:
        return ""
    if str(job.get("purpose") or "") != PURPOSE:
        return ""
    if str(job.get("state") or "") != "failed" or job.get("result_class") != "failure":
        return ""
    rows = [row for row in jobs if isinstance(row, dict)]
    if any(
        str(row.get("work_id") or "").strip().lower() == wanted
        and str(row.get("state") or "") in {"queued", "running"}
        for row in rows
    ):
        return ""
    if forgiven_tests(job, rows):
        return ""
    tested = WorkspaceSnapshot.from_record(job.get("tested_workspace"))
    after = WorkspaceSnapshot.from_record(job.get("verified_workspace"))
    if not tested.clean or not after.clean or after.snapshot_id != tested.snapshot_id:
        return ""
    return tested.head


def awaited_run_job(
    jobs: Iterable[object] | None, *, run_id: str, work_id: str = "", session_key: str = ""
) -> tuple[str, str]:
    """The id and purpose of a job this Run's worker is still waiting on, or ``("", "")``.

    A worker that queues its suite ends its turn and is woken by the job's completion message,
    so a turn that ends while that message has not been delivered yet is a worker waiting, not
    a worker that ended without reporting (#2300). Waiting means the job is queued or running,
    or it has finished and its completion is still owed to the chat that submitted it. Once the
    queue marks it notified — delivered, or given up on — the worker's next turn is the one
    that must report.

    Any job of the Run counts, ``--verification`` or not (#2622): holding a card spends nothing,
    and a worker waiting on an ordinary suite is just as much waiting. Only crediting a head
    and cancelling a rejected head's suite stay with verification jobs.

    A job naming no Run counts too when ``session_key``, the chat the Run's turn ended in,
    queued it (#2902). The CLI learns the Run only from ``--session-token``; a job submitted
    without one still tells its chat when it finishes, so that chat is just as much waiting.
    On one tracker card such a job was missed, and the card was restarted while
    its worker was waiting on it.

    ``None`` is an unreadable queue, and it proves no wait: a queue that cannot be read must not
    hide a real reportless ending for as long as it stays unreadable.
    """
    if not jobs or not run_id:
        return "", ""
    rows = [job for job in jobs if isinstance(job, dict)]
    wanted = work_id.strip().casefold()
    chat = session_key.strip()

    def same_work(job: dict) -> bool:
        named = str(job.get("work_id") or "").strip().casefold()
        return not (wanted and named and named != wanted)

    def ours(job: dict) -> bool:
        named = str(job.get("run_id") or "")
        if named:
            return named == run_id
        return bool(chat) and str(job.get("session_key") or "").strip() == chat

    own = [job for job in rows if ours(job) and same_work(job)]
    ids = {str(job.get("id") or "") for job in own} - {""}
    follow_ups = [
        job
        for job in rows
        if str(job.get("follows") or "") in ids and str(job.get("id") or "") not in ids
    ]
    for job in own + follow_ups:
        state = str(job.get("state") or "")
        owed = bool(job.get("session_key")) and not job.get("notified")
        if state in _UNFINISHED or owed:
            return str(job.get("id") or "unknown"), str(job.get("purpose") or "")
    return "", ""


def unfinished_jobs(jobs: Iterable[object] | None, *, run_id: str, work_id: str = "") -> list[str]:
    """The ids of this Run's verification jobs still queued or running (#2350).

    What a rejected review cancels: a suite of code the reviewer sent back cannot be the green
    run any release uses, so letting it finish only spends a lane. A finished job is left alone,
    whatever its result.
    """
    if not jobs or not run_id:
        return []
    return [
        str(job.get("id"))
        for job in _with_follow_ups(jobs, run_id=run_id, work_id=work_id)
        if job.get("id") and str(job.get("state") or "") in _UNFINISHED
    ]


def unfinished_for_heads(jobs: Iterable[object] | None, *, work_id: str, heads: set[str]) -> str:
    """A queued or running verification of any Run of this Work that may be testing ``heads``.

    Work-wide rather than per Run, because the job that matters is usually an ended rebase
    round's suite, still running under a Run the card no longer holds (#2449). A running job
    counts only on a head it is testing; a queued one has not read its tree yet, so it counts
    too. Waiting on it can only delay a verify turn, never skip one: once it ends, it is no
    longer unfinished, and a run that proved nothing leaves the tests bit red.
    """
    wanted = str(work_id or "").strip().lower()
    if not jobs or not wanted or not heads:
        return ""
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("purpose") or "") != PURPOSE:
            continue
        if str(job.get("work_id") or "").strip().lower() != wanted:
            continue
        state = str(job.get("state") or "")
        if state not in _UNFINISHED:
            continue
        tested = job.get("tested_workspace")
        head = str(tested.get("head") or "") if isinstance(tested, dict) else ""
        if head in heads or (state == "queued" and not head):
            return str(job.get("id") or "unknown")
    return ""


def ended_untested(jobs: Iterable[object] | None, *, work_id: str, heads: set[str]) -> str:
    """The id of a finished verification that tested nothing, latest on one of ``heads``.

    A run the cluster cut off before pytest started is not a result about the head (#2475):
    the queue resubmits it once by itself, and when that one is cut off too, this is what
    lets the engine ask for the verify turn once more rather than leave the card waiting for
    a person. Judged per head, as ``unfinished_for_heads`` is: one pull request's later real
    run says nothing about another's. ``""`` when every head's latest run ran, or none has one.
    """
    wanted = str(work_id or "").strip().lower()
    if not jobs or not wanted or not heads:
        return ""
    latest: dict[str, dict] = {}
    for job in jobs:
        if not isinstance(job, dict) or str(job.get("purpose") or "") != PURPOSE:
            continue
        if str(job.get("work_id") or "").strip().lower() != wanted:
            continue
        if str(job.get("state") or "") in _UNFINISHED:
            continue
        tested = job.get("tested_workspace")
        head = str(tested.get("head") or "") if isinstance(tested, dict) else ""
        if head not in heads:
            continue
        if head not in latest or _finished_at(job) >= _finished_at(latest[head]):
            latest[head] = job
    for head in sorted(latest):
        if flaky.no_test_result(latest[head]):
            return str(latest[head].get("id") or "unknown")
    return ""


def _finished_at(job: dict) -> float:
    try:
        return float(job.get("finished_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _with_follow_ups(jobs: Iterable[object], *, run_id: str, work_id: str) -> list[dict]:
    """This Run's verification jobs, and the jobs the queue submitted for them (#2382).

    A rerun of a verification's failing tests, or a run of them on its merge base, is part of
    that verification: the worker is still waiting on it, and a rejected head cancels it too.
    """
    rows = [job for job in jobs or () if isinstance(job, dict)]
    own = [job for job in rows if _belongs_to_run(job, run_id=run_id, work_id=work_id)]
    ids = {str(job.get("id") or "") for job in own} - {""}
    return own + [
        job
        for job in rows
        if str(job.get("follows") or "") in ids
        and not _belongs_to_run(job, run_id=run_id, work_id=work_id)
    ]


def _every_repository_held(job: dict, primary: WorkspaceSnapshot) -> bool:
    """Whether every repository the job tested was as provable as the one it credits (#2148).

    A row with no repository-keyed record proves nothing: every row since #2148 has one, so a
    row without it is malformed (#2214). The record must list the credited checkout itself,
    and every entry must be clean and read the same after the command as before it; with more
    than one repository, each must also name its repository, because a path is nothing
    another host can compare.
    """
    if not isinstance(job.get("tested_workspaces"), dict) or not job["tested_workspaces"]:
        return False
    tested, after = snapshots_tested(job), snapshots_after(job)
    held = tested.get(primary.repository_key)
    if held is None or held.snapshot_id != primary.snapshot_id or set(after) != set(tested):
        return False
    return all(
        (len(tested) == 1 or not key.startswith(UNRESOLVED_REPOSITORY))
        and snapshot.clean
        and after[key].snapshot_id == snapshot.snapshot_id
        for key, snapshot in tested.items()
    )


def snapshots_tested(job: object) -> dict[str, WorkspaceSnapshot]:
    """What the job tested, one snapshot per repository, read before the command started."""
    return _workspaces(job, "tested_workspaces")


def snapshots_after(job: object) -> dict[str, WorkspaceSnapshot]:
    """The same repositories read again after a successful verification command exited."""
    return _workspaces(job, "verified_workspaces")


def _workspaces(job: object, field: str) -> dict[str, WorkspaceSnapshot]:
    keyed = job.get(field) if isinstance(job, dict) else None
    if not isinstance(keyed, dict) or not keyed:
        return {}
    return repository_snapshots(WorkspaceSnapshot.from_record(item) for item in keyed.values())


def canonical(snapshots: Mapping[str, WorkspaceSnapshot]) -> tuple[tuple[str, tuple], ...]:
    """A repository-keyed set as one plain value: repository to tree, in no order.

    A comparison value, not an identity: nothing stores it, and the job row that recorded the
    snapshots is what names a piece of evidence.
    """
    return tuple((key, snapshots[key].comparison) for key in sorted(snapshots))


@dataclass(frozen=True)
class EvidenceCheck:
    """Whether recorded evidence still describes a workspace, and per repository why not.

    ``unprovable`` keeps each repository's own reasons rather than one verdict for the set,
    so a person can see which repository could not be read and what about it.
    """

    stale: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unprovable: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def verified(self) -> bool:
        return not (self.stale or self.missing or self.unprovable)


def compare(
    tested: Mapping[str, WorkspaceSnapshot],
    current: Mapping[str, WorkspaceSnapshot],
    *,
    required: Iterable[str] | None = None,
) -> EvidenceCheck:
    """Does ``tested`` still describe ``current`` for every ``required`` repository?

    ``required`` defaults to every repository either side names. A repository only one side
    has is ``missing``; one whose content moved is ``stale``; one that could not be read on
    either side, or never resolved to a named repository, is ``unprovable`` -- and none of
    those is verified. Content is compared, not paths, so a pod's or another host's checkout
    of the same commit matches.
    """
    wanted = (
        {key if key.startswith(UNRESOLVED_REPOSITORY) else key.lower() for key in required}
        if required is not None
        else set(tested) | set(current)
    )
    keys = sorted(wanted)
    if not keys:
        return EvidenceCheck(missing=("no repository",))
    stale: list[str] = []
    missing: list[str] = []
    unprovable: list[tuple[str, tuple[str, ...]]] = []
    for key in keys:
        before, now = tested.get(key), current.get(key)
        if before is None or now is None:
            missing.append(key)
            continue
        reasons = [*(f"tested: {item}" for item in before.unprovable)]
        reasons += [f"current: {item}" for item in now.unprovable]
        if key.startswith(UNRESOLVED_REPOSITORY):
            reasons.append("no repository remote")
        for side, snapshot in (("tested", before), ("current", now)):
            if not snapshot.provable and not snapshot.unprovable:
                reasons.append(f"{side}: not observed")
        if reasons:
            unprovable.append((key, tuple(reasons)))
        elif before.comparison != now.comparison:
            stale.append(key)
    return EvidenceCheck(tuple(stale), tuple(missing), tuple(unprovable))


def describe(snapshots: Mapping[str, WorkspaceSnapshot]) -> str:
    """``owner/repo@1a2b3c4`` per repository, compact enough for a log line or a CLI row."""
    parts = []
    for key in sorted(snapshots):
        snapshot = snapshots[key]
        state = "" if snapshot.clean else (" dirty" if snapshot.provable else " unprovable")
        parts.append(f"{key}@{snapshot.head[:7] or '?'}{state}")
    return ", ".join(parts)


def _belongs_to_run(job: dict, *, run_id: str, work_id: str) -> bool:
    if str(job.get("purpose") or "") != PURPOSE:
        return False
    if str(job.get("run_id") or "") != run_id:
        return False
    job_work = str(job.get("work_id") or "")
    return not (work_id and job_work and job_work != work_id)
