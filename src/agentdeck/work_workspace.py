"""A read-only, multi-repository workspace model for one Work (#2143).

A Work is not one cwd. One Work can be worked in a borrowed code repository,
commit its screenshots to an artifact repository, and have several chats whose
directories disagree with each other and with what the poller registered. This
module gathers what the deck already knows about those places into one
collection-shaped read model:

``WorkWorkspace(work_id, repositories=(WorkRepositoryContext, ...),
unresolved_observations=(...), issues=(...))``

Evidence, strongest first:

0. the Work's own workspace claims (#2149): the durable fact of which checkout the
   Work owns, whichever Run, provider or account is working in it. They are the only
   source of the Work's worktrees (#2195);
1. its project's declared repository roles;
2. existing ``GitContext`` facts and any ``WorkspaceSnapshot`` a caller holds;
3. the Work's own source repository;
4. the cwd of a session bound to a Run of this Work (``session_run_bindings``);
5. the cwd of any other leg of a member chat, which is weak and says so.

Rules this model keeps, and which the tests pin:

* Repository identity comes only from a checkout's remote, a pull request or
  the Work source, never from a directory name.
* Nothing is chosen to simplify the picture. Several repositories, several paths
  for one repository, a missing registered path, a cwd outside every registered
  worktree, a remote that disagrees and unreadable git are all kept and reported
  as ``WorkspaceIssue`` rows. ``primary`` is only a hint and may be empty.
* Read-only. Nothing here creates, deletes, repairs or reassigns a worktree; the
  claims are written by the runtime's workspace service
  (``work_workspace_claims``), and only read here.
* Bounded. Every unique path is inspected at most once per projection pass by
  one ``WorkspaceProbe``, with a per-command timeout and a cap on how many paths
  one pass inspects. Templates only render the result.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .git_context import GitContext, github_repository
from .poller_engine import paths as path_identity
from .poller_engine import verification
from .poller_engine.workspace import WorkspaceSnapshot, _digest, _normalize_remote
from .work import WorkRef
from .work_workspace_claims import WorkspaceClaim

if TYPE_CHECKING:
    from .poller_ops import BoardView
    from .state import AppState


class WorkspaceEvidence(StrEnum):
    """Where one workspace fact came from, strongest first."""

    WORK_CLAIM = "work_claim"  # the Work's own workspace claim (#2149)
    POLLER_MANIFEST = "poller_manifest"  # a repository the project declares, checkout or not
    GIT_CONTEXT = "git_context"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    WORK_SOURCE = "work_source"
    RUN_SESSION = "run_session"
    CHAT_SESSION = "chat_session"


EVIDENCE_RANK = {evidence: rank for rank, evidence in enumerate(WorkspaceEvidence)}


def _ranked(items: Iterable[WorkspaceProvenance]) -> tuple[WorkspaceProvenance, ...]:
    """Strongest evidence first, then a stable order within one kind."""
    return tuple(
        sorted(
            set(items), key=lambda item: (EVIDENCE_RANK[item.evidence], item.source, item.run_id)
        )
    )


class RepositoryRole(StrEnum):
    SOURCE = "source"  # the repository the Work's issue lives in
    IMPLEMENTATION = "implementation"  # code was observed being worked here
    BORROWED = "borrowed"  # the code repository the source borrows a checkout from
    ARTIFACT = "artifact"  # where the project commits evidence such as screenshots


class WorkspaceIssueKind(StrEnum):
    MISSING_WORKTREE = "missing_worktree"
    MISSING_CWD = "missing_cwd"
    MULTIPLE_WORKTREES = "multiple_worktrees"
    CWD_OUTSIDE_WORKTREE = "cwd_outside_worktree"
    NESTED_CHECKOUT = "nested_checkout"  # a cwd in another repository inside a registered one
    REMOTE_MISMATCH = "remote_mismatch"
    UNREADABLE_GIT = "unreadable_git"
    UNRESOLVED_REPOSITORY = "unresolved_repository"
    DETACHED_HEAD = "detached_head"
    AMBIGUOUS_PRIMARY = "ambiguous_primary"
    NOT_INSPECTED = "not_inspected"
    PULL_REQUEST_CONFLICT = "pull_request_conflict"
    POLLER_STATE_UNAVAILABLE = "poller_state_unavailable"
    CLAIMS_UNAVAILABLE = "claims_unavailable"
    CLAIM_UNSETTLED = "claim_unsettled"  # reserved, creation outcome not yet confirmed
    RELEASE_PENDING = "release_pending"  # the Work finished, its checkout is still held


# Informational kinds describe a valid state worth seeing; the rest are problems.
# A vanished session cwd is ordinary history (a merged Work's worktree is removed); a
# missing *registered* worktree is not.
_INFO_KINDS = frozenset(
    {
        WorkspaceIssueKind.DETACHED_HEAD,
        WorkspaceIssueKind.AMBIGUOUS_PRIMARY,
        WorkspaceIssueKind.MISSING_CWD,
    }
)


@dataclass(frozen=True)
class WorkspaceProvenance:
    """One piece of evidence: its kind, what carried it, and the Run when one is known."""

    evidence: WorkspaceEvidence
    source: str = ""  # poller instance, session key, or the Work id
    run_id: str = ""

    def as_json(self) -> dict:
        return {"evidence": str(self.evidence), "source": self.source, "run_id": self.run_id}


@dataclass(frozen=True)
class PathObservation:
    """What one bounded git read found at one resolved path."""

    path: str
    exists: bool = False
    inspected: bool = True
    toplevel: str = ""
    branch: str = ""  # empty when detached or unknown
    detached: bool = False
    head: str = ""
    remote: str = ""
    repository: str = ""  # lower-cased owner/repo from a GitHub remote, else empty
    error: str = ""
    # The read ran out of time (pass budget or command timeout) rather than failing.
    timed_out: bool = False

    @property
    def unfinished(self) -> bool:
        """Nothing was learned for want of time, which says nothing about the checkout."""
        return self.exists and (not self.inspected or self.timed_out)

    @property
    def readable(self) -> bool:
        return self.exists and self.inspected and bool(self.toplevel) and not self.error

    def as_json(self) -> dict:
        return {
            "path": self.path,
            "exists": self.exists,
            "inspected": self.inspected,
            "toplevel": self.toplevel,
            "branch": self.branch,
            "detached": self.detached,
            "head": self.head,
            "remote": self.remote,
            "repository": self.repository,
            "error": self.error,
        }


# (path, git arguments, timeout in seconds) -> (exit code, output). ``TIMED_OUT`` is
# the exit code of a command that ran out of time.
Runner = Callable[[str, Sequence[str], float], "tuple[int, str]"]
TIMED_OUT = -1

_COMMAND_TIMEOUT_S = 2.0
DEFAULT_PATH_LIMIT = 24
DEFAULT_PASS_BUDGET_S = 8.0


def _git(path: str, args: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TIMED_OUT, f"git {args[0]} timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr


class WorkspaceProbe:
    """Inspects each unique resolved path once, at most ``limit`` paths per pass.

    Two git commands per path: one ``rev-parse`` for top level, HEAD and branch,
    and one ``remote get-url origin``. A path past the limit, or reached after the
    pass's time budget ran out, is reported as not inspected rather than dropped.
    Every command's timeout is capped to what is left of the pass budget, so the
    whole pass never outlasts it by more than process start-up.
    """

    def __init__(
        self,
        runner: Runner = _git,
        *,
        limit: int = DEFAULT_PATH_LIMIT,
        budget_s: float = DEFAULT_PASS_BUDGET_S,
    ) -> None:
        self._runner = runner
        self._limit = limit
        self._deadline = time.monotonic() + budget_s
        self._seen: dict[str, PathObservation] = {}

    def _remaining(self) -> float:
        return min(_COMMAND_TIMEOUT_S, self._deadline - time.monotonic())

    @property
    def inspected(self) -> int:
        return sum(1 for item in self._seen.values() if item.inspected and item.exists)

    def observe(self, raw: str) -> PathObservation:
        # A relative or empty spelling would resolve against the deck's own cwd and
        # inspect a checkout nobody named, so it is reported as absent instead.
        if not raw.strip() or not Path(raw.strip()).expanduser().is_absolute():
            return PathObservation(raw, exists=False, error="not an absolute path")
        path = path_identity.resolve(raw)
        found = self._seen.get(path)
        if found is None:
            found = self._seen[path] = self._inspect(path)
        return found

    def _inspect(self, path: str) -> PathObservation:
        if not Path(path).exists():
            return PathObservation(path, exists=False)
        if self.inspected >= self._limit or self._remaining() <= 0:
            return PathObservation(path, exists=True, inspected=False)
        code, out = self._runner(
            path,
            ("rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD"),
            self._remaining(),
        )
        lines = out.strip().splitlines()
        if code != 0 or len(lines) < 3:
            error = (out.strip().splitlines() or ["not a git repository"])[-1][:200]
            return PathObservation(path, exists=True, error=error, timed_out=code == TIMED_OUT)
        toplevel, head, ref = lines[0], lines[1], lines[2]
        # Without the remote there is no repository identity, and "no remote" must not
        # be claimed for a read that was never finished.
        if self._remaining() <= 0:
            return PathObservation(path, exists=True, inspected=False)
        code, remote = self._runner(path, ("remote", "get-url", "origin"), self._remaining())
        if code == TIMED_OUT:
            return PathObservation(
                path, exists=True, error=remote.strip() or "timed out", timed_out=True
            )
        remote = remote.strip() if code == 0 else ""
        return PathObservation(
            path,
            exists=True,
            toplevel=path_identity.resolve(toplevel),
            branch="" if ref == "HEAD" else ref,
            detached=ref == "HEAD",
            head=head,
            remote=remote,
            repository=(github_repository(remote) or "").lower() if remote else "",
        )


@dataclass(frozen=True)
class WorktreeObservation:
    """A checkout this Work touches: registered by the poller, or where a session ran."""

    path: str
    registered: bool
    observation: PathObservation
    provenance: tuple[WorkspaceProvenance, ...]

    def as_json(self) -> dict:
        return {
            "path": self.path,
            "registered": self.registered,
            "observation": self.observation.as_json(),
            "provenance": [item.as_json() for item in self.provenance],
        }


@dataclass(frozen=True)
class CwdObservation:
    """Where one session leg executes, and which checkout that is inside."""

    session_key: str
    chat_id: str
    cwd: str
    observation: PathObservation
    provenance: WorkspaceProvenance
    # The registered worktree this cwd sits inside, if any.
    registered_worktree: str = ""
    git_repository: str = ""  # what the leg's GitContext said, for disagreement
    dirty: bool | None = None

    def as_json(self) -> dict:
        return {
            "session_key": self.session_key,
            "chat_id": self.chat_id,
            "cwd": self.cwd,
            "observation": self.observation.as_json(),
            "provenance": self.provenance.as_json(),
            "registered_worktree": self.registered_worktree,
            "git_repository": self.git_repository,
            "dirty": self.dirty,
        }


@dataclass(frozen=True)
class WorkPullRequest:
    repository: str
    number: int
    url: str
    status: str = ""
    head_branch: str = ""
    base_branch: str = ""
    provenance: tuple[WorkspaceProvenance, ...] = ()

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
            "status": self.status,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
            "provenance": [item.as_json() for item in self.provenance],
        }


@dataclass(frozen=True)
class WorkspaceIssue:
    kind: WorkspaceIssueKind
    detail: str
    repository: str = ""
    path: str = ""

    @property
    def problem(self) -> bool:
        return self.kind not in _INFO_KINDS

    def as_json(self) -> dict:
        return {
            "kind": str(self.kind),
            "detail": self.detail,
            "repository": self.repository,
            "path": self.path,
            "problem": self.problem,
        }


@dataclass(frozen=True)
class WorkRepositoryContext:
    repository: str  # lower-cased owner/repo
    roles: tuple[RepositoryRole, ...]
    branches: tuple[str, ...] = ()
    heads: tuple[str, ...] = ()
    base_refs: tuple[str, ...] = ()
    worktrees: tuple[WorktreeObservation, ...] = ()
    cwds: tuple[CwdObservation, ...] = ()
    pull_requests: tuple[WorkPullRequest, ...] = ()
    provenance: tuple[WorkspaceProvenance, ...] = ()
    issues: tuple[WorkspaceIssue, ...] = ()

    @property
    def problems(self) -> tuple[WorkspaceIssue, ...]:
        return tuple(issue for issue in self.issues if issue.problem)

    def as_json(self) -> dict:
        return {
            "repository": self.repository,
            "roles": [str(role) for role in self.roles],
            "branches": list(self.branches),
            "heads": list(self.heads),
            "base_refs": list(self.base_refs),
            "worktrees": [item.as_json() for item in self.worktrees],
            "cwds": [item.as_json() for item in self.cwds],
            "pull_requests": [item.as_json() for item in self.pull_requests],
            "provenance": [item.as_json() for item in self.provenance],
            "issues": [item.as_json() for item in self.issues],
        }


@dataclass(frozen=True)
class RunVerification:
    """One explicit verification job of a Run of this Work, and what it tested (#2148).

    ``tested`` is the job's own record, so it still says what ran after the worktree is
    removed or the Work moves on. ``verified`` is the release rule's answer and nothing
    looser: a green run over a tree that was unprovable, dirty or moved is not verified.
    """

    job_id: str
    run_id: str
    state: str  # the queue's own word: queued, running, done, failed, cancelled
    verified: bool
    tested: tuple[tuple[str, WorkspaceSnapshot], ...]  # (repository as shown, snapshot)
    # The failing tests a verified run was forgiven for (#2382): it failed only on tests a rerun
    # of the same commit passed or that also fail on its merge base. Empty for a green run.
    forgiven: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.verified:
            return "verified"
        if self.state == "done":
            return "unproven"  # green, but the evidence does not hold up
        return self.state or "unknown"

    def as_json(self) -> dict:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state,
            "status": self.status,
            "verified": self.verified,
            "tested": {name: snapshot.as_record() for name, snapshot in self.tested},
            "forgiven": list(self.forgiven),
        }


# The newest few are what a Work card has room for; the job queue keeps the rest.
_VERIFICATIONS_SHOWN = 5


def run_verifications(
    jobs: Iterable[object], work_id: str, *, limit: int | None = _VERIFICATIONS_SHOWN
) -> tuple[RunVerification, ...]:
    """This Work's explicit verification jobs, newest first (#2148).

    Only a job submitted as verification that names this exact Work counts; a job that
    merely ran in one of its checkouts is not evidence for it. ``limit`` is the Work card's
    room; the Run trace passes None, because it shows every retained Run (#2141).
    """
    jobs = list(jobs)
    found: list[tuple[float, RunVerification]] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("purpose") != verification.PURPOSE:
            continue
        run_id = str(job.get("run_id") or "")
        if not work_id or str(job.get("work_id") or "") != work_id or not run_id:
            continue
        tested = verification.snapshots_tested(job)
        head = verification.credited_head(job, run_id=run_id, work_id=work_id, jobs=jobs)
        submitted = job.get("submitted_at")
        found.append(
            (
                float(submitted) if isinstance(submitted, int | float) else 0.0,
                RunVerification(
                    job_id=str(job.get("id") or ""),
                    run_id=run_id,
                    state=str(job.get("state") or ""),
                    verified=bool(head),
                    tested=tuple((key, tested[key]) for key in sorted(tested)),
                    forgiven=tuple(
                        str(item.get("test") or "")
                        for item in verification.forgiven_tests(job, jobs)
                    )
                    if head
                    else (),
                ),
            )
        )
    found.sort(key=lambda item: item[0], reverse=True)
    return tuple(item for _, item in (found if limit is None else found[:limit]))


@dataclass(frozen=True)
class WorkWorkspace:
    work_id: str
    repositories: tuple[WorkRepositoryContext, ...]
    # Worktrees and cwds whose repository could not be established.
    unresolved_observations: tuple[WorktreeObservation | CwdObservation, ...] = ()
    issues: tuple[WorkspaceIssue, ...] = ()  # Work-level: not about one repository
    primary: str = ""  # optional UI hint, never identity
    inspected_paths: int = 0
    verifications: tuple[RunVerification, ...] = ()

    @property
    def problems(self) -> tuple[WorkspaceIssue, ...]:
        found = [issue for issue in self.issues if issue.problem]
        for repository in self.repositories:
            found.extend(repository.problems)
        return tuple(found)

    @property
    def unresolved_problem(self) -> bool:
        """Whether any unresolved observation carries a problem, not just information."""
        paths = {
            item.cwd if isinstance(item, CwdObservation) else item.path
            for item in self.unresolved_observations
        }
        return any(issue.problem for issue in self.issues if issue.path in paths)

    def repository(self, name: str) -> WorkRepositoryContext | None:
        wanted = name.lower()
        return next((item for item in self.repositories if item.repository == wanted), None)

    def as_json(self) -> dict:
        return {
            "work_id": self.work_id,
            "primary": self.primary,
            "repositories": [item.as_json() for item in self.repositories],
            "unresolved_observations": [
                {
                    "kind": "cwd" if isinstance(item, CwdObservation) else "worktree",
                    **item.as_json(),
                }
                for item in self.unresolved_observations
            ],
            "issues": [item.as_json() for item in self.issues],
            "inspected_paths": self.inspected_paths,
            "verifications": [item.as_json() for item in self.verifications],
        }


# --- inputs ------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectWorkspaceFacts:
    """The project whose manifest declares this Work's repository, as the board reads it."""

    instance: str
    artifact_repositories: tuple[str, ...] = ()
    borrowed_repository: str = ""  # code repository the source borrows, if declared


@dataclass(frozen=True)
class SessionWorkspaceFacts:
    """One session leg that may have run this Work."""

    session_key: str
    chat_id: str
    cwd: str | None
    run_ids: tuple[str, ...] = ()  # Runs of this Work the leg is bound to
    git: GitContext | None = None
    # The leg's chat belongs to this Work alone. A chat's registered pull requests
    # are per chat, so they only count for a Work when the chat has no other one.
    exclusive: bool = False


_PULL_FIELDS = ("status", "head", "base")

_WORK_ID_RE = re.compile(r"github_issue:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")


def parse_work_id(work_id: str) -> WorkRef | None:
    """The WorkRef a ``github_issue:<owner/repo>#<n>`` id names, or None."""
    match = _WORK_ID_RE.fullmatch((work_id or "").strip())
    if match is None:
        return None
    return WorkRef(match.group(1).lower(), int(match.group(2)))


# --- the builder -------------------------------------------------------------


@dataclass
class _Repo:
    roles: set[RepositoryRole] = field(default_factory=set)
    worktrees: dict[str, WorktreeObservation] = field(default_factory=dict)
    cwds: list[CwdObservation] = field(default_factory=list)
    pulls: dict[int, WorkPullRequest] = field(default_factory=dict)
    provenance: set[WorkspaceProvenance] = field(default_factory=set)
    base_refs: list[str] = field(default_factory=list)
    issues: list[WorkspaceIssue] = field(default_factory=list)
    # Branch and HEAD a GitContext held for a cwd this pass could not read.
    known_branches: list[str] = field(default_factory=list)
    known_heads: list[str] = field(default_factory=list)


def build_work_workspace(
    work: WorkRef,
    *,
    projects: Iterable[ProjectWorkspaceFacts] = (),
    sessions: Iterable[SessionWorkspaceFacts] = (),
    probe: WorkspaceProbe | None = None,
    snapshots: Mapping[str, WorkspaceSnapshot] | None = None,
    poller_unavailable: str = "",
    verifications: Sequence[RunVerification] = (),
    claims: Iterable[WorkspaceClaim] = (),
    claims_unavailable: str = "",
) -> WorkWorkspace:
    """Assemble the Work's workspace from explicit evidence; never mutates anything.

    ``claims`` are this Work's active workspace claims (#2149), the strongest evidence of
    which checkout it owns; ``claims_unavailable`` says why they could not be read.

    ``snapshots`` maps a resolved checkout root to a ``WorkspaceSnapshot`` the
    caller already holds (#2148 attaches Run-bound ones); none are collected here.
    """
    probe = probe or WorkspaceProbe()
    snapshots = snapshots or {}
    repos: dict[str, _Repo] = {}
    unresolved: list[WorktreeObservation | CwdObservation] = []
    work_issues: list[WorkspaceIssue] = []

    def repo(name: str) -> _Repo:
        return repos.setdefault(name.lower(), _Repo())

    source = repo(work.repository)
    source.roles.add(RepositoryRole.SOURCE)
    source.provenance.add(WorkspaceProvenance(WorkspaceEvidence.WORK_SOURCE, work.work_id))

    def unresolved_issue(observation: PathObservation, what: str) -> WorkspaceIssue:
        if not observation.exists:
            return WorkspaceIssue(
                WorkspaceIssueKind.MISSING_CWD
                if what == "session cwd"
                else WorkspaceIssueKind.MISSING_WORKTREE,
                f"{what} does not exist on this host",
                path=observation.path,
            )
        if not observation.inspected:
            return WorkspaceIssue(
                WorkspaceIssueKind.NOT_INSPECTED,
                f"{what} was past this pass's inspection limit",
                path=observation.path,
            )
        if not observation.readable:
            return WorkspaceIssue(
                WorkspaceIssueKind.UNREADABLE_GIT,
                f"git could not read {what}: {observation.error or 'unknown error'}",
                path=observation.path,
            )
        return WorkspaceIssue(
            WorkspaceIssueKind.UNRESOLVED_REPOSITORY,
            f"{what} has no GitHub remote"
            + (f" ({observation.remote})" if observation.remote else ""),
            path=observation.path,
        )

    artifacts: set[str] = set()
    borrowed: set[str] = set()
    registered_roots: dict[str, str] = {}  # resolved root -> repository ("" when unknown)

    # 0. The Work's own claims. The claim names its repository: the runtime checked the
    # checkout's remote when it made or adopted it, and a disagreement now is reported.
    if claims_unavailable:
        work_issues.append(
            WorkspaceIssue(
                WorkspaceIssueKind.CLAIMS_UNAVAILABLE,
                f"Work workspace claims could not be read ({claims_unavailable}); the"
                " worktrees this Work owns are unknown, not absent",
            )
        )
    for claim in claims:
        if claim.work_id != work.work_id or not claim.active:
            continue
        name = claim.repository.lower()
        target = repo(name)
        if claim.purpose == "artifact":
            artifacts.add(name)
        provenance = WorkspaceProvenance(WorkspaceEvidence.WORK_CLAIM, claim.purpose)
        observation = probe.observe(claim.path)
        path = observation.path
        target.worktrees[path] = WorktreeObservation(path, True, observation, (provenance,))
        target.provenance.add(provenance)
        registered_roots[observation.toplevel or path] = name
        if claim.base_ref:
            target.base_refs.append(claim.base_ref)
        if observation.repository and observation.repository != name:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.REMOTE_MISMATCH,
                    f"the Work claims this for {name}, the checkout's remote is"
                    f" {observation.repository}",
                    repository=name,
                    path=path,
                )
            )
        if not claim.confirmed:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.CLAIM_UNSETTLED,
                    "claimed, but whether the worktree was created is not settled yet"
                    + (f": {claim.last_error}" if claim.last_error else ""),
                    repository=name,
                    path=path,
                )
            )
        elif not observation.exists and not claim.release_pending:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.MISSING_WORKTREE,
                    "the Work's claimed worktree does not exist on this host",
                    repository=name,
                    path=path,
                )
            )
        elif observation.exists and not (observation.inspected and observation.readable):
            # The claims are the only source of the Work's worktrees (#2195), so a path this
            # pass could not look at is said to be unknown here, never passed over quietly.
            issue = unresolved_issue(observation, "claimed worktree")
            target.issues.append(
                WorkspaceIssue(issue.kind, issue.detail, repository=name, path=path)
            )
        if claim.release_pending:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.RELEASE_PENDING,
                    "the Work finished but its worktree is still held"
                    + (f": {claim.last_error}" if claim.last_error else ""),
                    repository=name,
                    path=path,
                )
            )

    # 1. The project's repository roles.
    if poller_unavailable:
        # An unreadable manifest is not an empty one: the project's roles may exist and
        # simply could not be read.
        work_issues.append(
            WorkspaceIssue(
                WorkspaceIssueKind.POLLER_STATE_UNAVAILABLE,
                f"poller state could not be read ({poller_unavailable}); the project's"
                " repositories are unknown, not absent",
            )
        )
    for project in projects:
        declared = WorkspaceProvenance(WorkspaceEvidence.POLLER_MANIFEST, project.instance)
        # Declared repositories are part of the Work whether or not a checkout exists yet.
        for name in project.artifact_repositories:
            artifacts.add(name.lower())
            repo(name).provenance.add(declared)
        if project.borrowed_repository:
            borrowed.add(project.borrowed_repository.lower())
            repo(project.borrowed_repository).provenance.add(declared)

    # 2-5. Session legs: Run-bound first, then the weaker chat members.
    # Per pull request, the strength that set each of (status, head, base); None = unknown.
    pull_strength: dict[tuple[str, int], list[int | None]] = {}
    for facts in sorted(sessions, key=lambda item: (not item.run_ids, item.session_key)):
        if facts.run_ids:
            provenance = WorkspaceProvenance(
                WorkspaceEvidence.RUN_SESSION, facts.session_key, facts.run_ids[-1]
            )
        else:
            provenance = WorkspaceProvenance(WorkspaceEvidence.CHAT_SESSION, facts.session_key)
        git = facts.git
        if git is not None and (facts.run_ids or facts.exclusive):
            git_prov = WorkspaceProvenance(WorkspaceEvidence.GIT_CONTEXT, facts.session_key)
            strength = 0 if facts.run_ids else 1  # lower is stronger
            for pull in git.pull_requests:
                name = pull.repository.lower()
                target = repo(name)
                existing = target.pulls.get(pull.number)
                offered = (pull.display_status, pull.head_branch or "", pull.base_branch or "")
                # Each field merges on its own: an empty field is unknown, not a claim, so
                # weaker evidence may fill it; a known field is never overwritten by weaker
                # evidence, and two equals that disagree are shown, not settled by sort order.
                strengths = pull_strength.setdefault((name, pull.number), [None, None, None])
                current = (
                    [existing.status, existing.head_branch, existing.base_branch]
                    if existing
                    else ["", "", ""]
                )
                values = list(current)
                disagreements: list[str] = []
                for index, value in enumerate(offered):
                    prior = strengths[index]
                    if not value:
                        continue
                    if prior is None or strength < prior:
                        values[index] = value
                        strengths[index] = strength
                    elif strength == prior and value != current[index]:
                        disagreements.append(f"{_PULL_FIELDS[index]} {current[index]} vs {value}")
                if disagreements:
                    target.issues.append(
                        WorkspaceIssue(
                            WorkspaceIssueKind.PULL_REQUEST_CONFLICT,
                            f"#{pull.number}: chats disagree about this pull request ("
                            + "; ".join(disagreements)
                            + ")",
                            repository=name,
                        )
                    )
                target.pulls[pull.number] = WorkPullRequest(
                    name,
                    pull.number,
                    existing.url if existing else pull.url,
                    status=values[0],
                    head_branch=values[1],
                    base_branch=values[2],
                    provenance=_ranked((git_prov, *(existing.provenance if existing else ()))),
                )
        if not facts.cwd:
            continue
        observation = probe.observe(facts.cwd)
        repository = observation.repository
        # A read that learned nothing (cut short, timed out, or unreadable) says nothing
        # about the checkout; the leg's GitContext already holds what an earlier read found.
        # It is weaker than a fresh read, so it is used only then, and the failed read is
        # still reported. A readable checkout with no remote never borrows it: GitContext
        # falls back to the issue's repository there, which is inference, not observation.
        borrowed_context = bool(
            not repository
            and observation.exists
            and not observation.readable
            and git is not None
            and git.repository
        )
        if borrowed_context:
            repository = git.repository.lower()
        container, container_repository = _registered_container(observation, registered_roots)
        same_repository = bool(container_repository) and container_repository == repository
        inside = container if same_repository else ""
        cwd = CwdObservation(
            session_key=facts.session_key,
            chat_id=facts.chat_id,
            cwd=observation.path,
            observation=observation,
            provenance=provenance,
            registered_worktree=inside,
            git_repository=(git.repository or "").lower() if git else "",
            dirty=git.dirty if git is not None and git.branch is not None else None,
        )
        if not repository:
            unresolved.append(cwd)
            work_issues.append(unresolved_issue(observation, "session cwd"))
            continue
        target = repo(repository)
        target.cwds.append(cwd)
        target.provenance.add(provenance)
        if borrowed_context:
            work_issues.append(unresolved_issue(observation, "session cwd"))
            target.provenance.add(
                WorkspaceProvenance(WorkspaceEvidence.GIT_CONTEXT, facts.session_key)
            )
            if git.branch:
                target.known_branches.append(git.branch)
            if git.head_sha:
                target.known_heads.append(git.head_sha)
        if container and container_repository and not same_repository:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.NESTED_CHECKOUT,
                    f"a session runs in a {repository} checkout nested inside"
                    f" the registered {container_repository} worktree {container}",
                    repository=repository,
                    path=observation.path,
                )
            )
        if cwd.git_repository and cwd.git_repository != repository:
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.REMOTE_MISMATCH,
                    f"git context names {cwd.git_repository}, the checkout's remote is"
                    f" {observation.repository}",
                    repository=observation.repository,
                    path=observation.path,
                )
            )

    # Snapshot facts, keyed by resolved checkout root.
    observations_by_root: dict[str, PathObservation] = {}
    for context in repos.values():
        for tree in context.worktrees.values():
            if tree.observation.toplevel:
                observations_by_root[tree.observation.toplevel] = tree.observation
        for cwd in context.cwds:
            if cwd.observation.toplevel:
                observations_by_root.setdefault(cwd.observation.toplevel, cwd.observation)
    for root, snapshot in snapshots.items():
        if not root.strip():
            continue
        observation = observations_by_root.get(path_identity.resolve(root))
        if observation is None or not observation.repository:
            continue
        target = repo(observation.repository)
        target.provenance.add(
            WorkspaceProvenance(WorkspaceEvidence.WORKSPACE_SNAPSHOT, snapshot.snapshot_id)
        )
        if snapshot.base_ref:
            target.base_refs.append(snapshot.base_ref)
        if (
            snapshot.repo_identity
            and observation.remote
            and _digest(_normalize_remote(observation.remote)) != snapshot.repo_identity
        ):
            target.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.REMOTE_MISMATCH,
                    "the recorded workspace snapshot names a different remote",
                    repository=observation.repository,
                    path=observation.path,
                )
            )

    # Per-repository roles and diagnostics.
    contexts: list[WorkRepositoryContext] = []
    for name, context in repos.items():
        if name in artifacts:
            context.roles.add(RepositoryRole.ARTIFACT)
        elif context.worktrees or context.cwds or context.pulls:
            context.roles.add(RepositoryRole.IMPLEMENTATION)
        if name in borrowed and name != work.repository:
            context.roles.add(RepositoryRole.BORROWED)
        observations = [tree.observation for tree in context.worktrees.values()] + [
            cwd.observation for cwd in context.cwds
        ]
        roots = sorted({item.toplevel for item in observations if item.toplevel})
        if len(roots) > 1:
            context.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.MULTIPLE_WORKTREES,
                    f"{len(roots)} different checkouts: " + ", ".join(roots),
                    repository=name,
                )
            )
        # Once every registered worktree is gone, a chat that ran in one resumes in the
        # repository instead (#2524); running outside them is then where it belongs.
        if any(tree.observation.exists for tree in context.worktrees.values()):
            for cwd in context.cwds:
                if not cwd.registered_worktree:
                    context.issues.append(
                        WorkspaceIssue(
                            WorkspaceIssueKind.CWD_OUTSIDE_WORKTREE,
                            f"a session runs outside every registered worktree, in {cwd.cwd}",
                            repository=name,
                            path=cwd.cwd,
                        )
                    )
        for item in {obs.toplevel: obs for obs in observations if obs.detached}.values():
            context.issues.append(
                WorkspaceIssue(
                    WorkspaceIssueKind.DETACHED_HEAD,
                    f"detached at {item.head[:12]}",
                    repository=name,
                    path=item.toplevel,
                )
            )
        pulls = tuple(context.pulls[number] for number in sorted(context.pulls))
        context.base_refs.extend(pull.base_branch for pull in pulls if pull.base_branch)
        contexts.append(
            WorkRepositoryContext(
                repository=name,
                roles=tuple(role for role in RepositoryRole if role in context.roles),
                branches=_unique(
                    [item.branch for item in observations if item.branch] + context.known_branches
                ),
                heads=_unique(
                    [item.head for item in observations if item.head] + context.known_heads
                ),
                base_refs=_unique(context.base_refs),
                worktrees=tuple(context.worktrees[path] for path in sorted(context.worktrees)),
                cwds=tuple(context.cwds),
                pull_requests=pulls,
                provenance=_ranked(context.provenance),
                issues=tuple(context.issues),
            )
        )

    contexts.sort(key=lambda item: (RepositoryRole.SOURCE not in item.roles, item.repository))
    primary, ambiguous = _primary(contexts)
    if ambiguous:
        work_issues.append(
            WorkspaceIssue(
                WorkspaceIssueKind.AMBIGUOUS_PRIMARY,
                "no single primary repository: " + ", ".join(ambiguous),
            )
        )
    return WorkWorkspace(
        work_id=work.work_id,
        repositories=tuple(contexts),
        unresolved_observations=tuple(unresolved),
        issues=tuple(work_issues),
        primary=primary,
        inspected_paths=probe.inspected,
        verifications=tuple(verifications),
    )


def _registered_container(
    observation: PathObservation, roots: Mapping[str, str]
) -> tuple[str, str]:
    """The innermost registered worktree root this observation sits in, and its repository.

    Containment alone is not membership: a checkout of another repository nested inside
    a registered worktree is inside that directory but not inside that worktree, so the
    caller compares the repository too.
    """
    best = ""
    for root in roots:
        prefix = root.rstrip("/") + "/"
        for candidate in (observation.toplevel, observation.path):
            if candidate and (candidate == root or candidate.startswith(prefix)):
                if len(root) > len(best):
                    best = root
    return best, roots.get(best, "")


def _primary(contexts: Sequence[WorkRepositoryContext]) -> tuple[str, tuple[str, ...]]:
    """A default repository to lead with, or the tied candidates when there is none."""

    def code(context: WorkRepositoryContext) -> bool:
        return RepositoryRole.ARTIFACT not in context.roles

    for pick in (
        lambda item: code(item) and bool(item.worktrees),
        lambda item: code(item) and bool(item.cwds),
    ):
        candidates = [item.repository for item in contexts if pick(item)]
        if len(candidates) == 1:
            return candidates[0], ()
        if len(candidates) > 1:
            return "", tuple(candidates)
    source = next((item for item in contexts if RepositoryRole.SOURCE in item.roles), None)
    return (source.repository if source else ""), ()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)


# --- gathering from the running deck -----------------------------------------


@dataclass(frozen=True)
class WorkWorkspaceFacts:
    """Everything the builder needs, gathered from one snapshot of the deck."""

    work: WorkRef
    projects: tuple[ProjectWorkspaceFacts, ...] = ()
    sessions: tuple[SessionWorkspaceFacts, ...] = ()
    poller_unavailable: str = ""  # why poller state that may name this Work was unreadable
    verifications: tuple[RunVerification, ...] = ()
    claims: tuple[WorkspaceClaim, ...] = ()
    claims_unavailable: str = ""  # why the Work's workspace claims could not be read

    @property
    def known(self) -> bool:
        """Whether anything ties this Work to the deck, or could and was unreadable."""
        return bool(self.sessions or self.poller_unavailable or self.claims)

    def build(self, probe: WorkspaceProbe | None = None) -> WorkWorkspace:
        return build_work_workspace(
            self.work,
            projects=self.projects,
            sessions=self.sessions,
            probe=probe,
            poller_unavailable=self.poller_unavailable,
            verifications=self.verifications,
            claims=self.claims,
            claims_unavailable=self.claims_unavailable,
        )


def gather_work_workspace(
    state: AppState,
    work: WorkRef,
    *,
    board: BoardView | None = None,
    git_contexts: Mapping[str, GitContext] | None = None,
    jobs: Iterable[object] = (),
    claims: Iterable[WorkspaceClaim] | None = (),
) -> WorkWorkspaceFacts:
    """Read this Work's facts out of the deck; no git and no network.

    Legs come from two places: every leg of a chat that is a member of this Work,
    and every leg bound to a Run of it. A leg bound only to *other* Works' Runs is
    left out, so a chat shared by two Works does not lend one Work's checkout to
    the other. The git reads happen later, in ``WorkWorkspaceFacts.build``, so a
    caller can move them off the event loop.
    """
    git_contexts = git_contexts or {}
    projection = state.work_projection()
    found = projection.get(work)
    members = set(found.chat_ids) if found is not None else set()
    bindings = getattr(state.db, "session_run_bindings", None)
    runs: dict[str, list[str]] = {}
    for row in bindings(work_id=work.work_id) if bindings is not None else ():
        runs.setdefault(row.session_key, []).append(row.run_id)

    def bound_elsewhere(key: str) -> bool:
        rows = bindings(session_key=key) if bindings is not None else ()
        return any(row.work_id != work.work_id for row in rows)

    sessions = dict(state.sessions)
    chats = state.chat_snapshot()
    legs: dict[str, str] = {}
    for key in sessions:
        chat_id = state.chat_identity_key(key, chats)
        if chat_id in members and (key in runs or not bound_elsewhere(key)):
            legs[key] = chat_id
    for key in runs:
        legs.setdefault(key, state.chat_identity_key(key, chats))

    facts = tuple(
        SessionWorkspaceFacts(
            session_key=key,
            chat_id=chat_id,
            cwd=str(sessions[key].cwd) if key in sessions and sessions[key].cwd else None,
            run_ids=tuple(runs.get(key, ())),
            git=git_contexts.get(key),
            exclusive=projection.works_for_chat(chat_id) == (work,),
        )
        for key, chat_id in sorted(legs.items())
    )

    # The project that declares this repository names its roles; the Work's claims above are
    # the only source of its worktrees (#2195).
    projects: list[ProjectWorkspaceFacts] = []
    unavailable = ""
    if board is not None:
        scope = next(
            (
                item
                for item in board.scopes
                if any(repo.lower() == work.repository for repo in item.issue_repositories)
            ),
            None,
        )
        unavailable = _unreadable_scope(board, work)
        if scope is not None:
            projects.append(
                ProjectWorkspaceFacts(
                    instance=scope.instance,
                    artifact_repositories=scope.artifact_repositories,
                    borrowed_repository=next(
                        (
                            code
                            for repo, code in scope.borrowed_workspaces
                            if repo.lower() == work.repository
                        ),
                        "",
                    ),
                )
            )
    return WorkWorkspaceFacts(
        work,
        tuple(projects),
        facts,
        unavailable,
        run_verifications(jobs, work.work_id),
        claims=tuple(claims or ()),
        claims_unavailable="" if claims is not None else "the claims store was unreadable",
    )


def _unreadable_scope(board: BoardView, work: WorkRef) -> str:
    """Why an instance that might declare this Work's repository could not be read, else "".

    ``BoardView`` keeps "an incomplete lookup is not absence": no project only means none
    when every instance that could declare it was read. One that could is one that declares
    the repository, or one whose manifest was unreadable and so declares nothing we can rule
    out.
    """
    owner = work.repository.partition("/")[0]
    for scope in board.scopes:
        if scope.readable and scope.state_readable:
            continue
        if scope.readable:
            # The manifest says which repositories this instance holds issues for.
            declared = {repo.lower() for repo in scope.issue_repositories}
            if work.repository in declared:
                return f"instance {scope.instance}: state unreadable"
        elif scope.org.lower() in ("", owner):
            return f"instance {scope.instance}: manifest unreadable"
    return ""
