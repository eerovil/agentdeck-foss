"""Work-owned repository workspace claims (#2149).

A worktree belongs to the Work, not to whichever worker, generation, provider or
account happened to be running when it was made. The same Work moves between Runs,
chats and providers, and every one of them must land in the one checkout that holds
its work; only the end of the Work may give that checkout back.

The durable fact is one **claim** per ``(work_id, repository, purpose)``, and nothing
more than the minimum a release needs:

``path, branch, base_ref, claimed_at, confirmed_at, release_requested_at,
released_at, last_error, reserved_at``

There is no workspace id and no workspace state machine. ``confirmed_at`` empty is the
single "reserved, but whether the host worktree exists is not settled" fact a crash
leaves behind, and ``reserved_at`` is when the attempt that holds it began. It is
resolved by looking at the host, never by making a second candidate beside it; only
the caller that wins the reservation (a compare-and-set on ``reserved_at``) may run
Git, so two processes never create the same worktree at once. A released row stays as
history, and the next claim for the same key (a reopened Work) is a new row.

Rules the tests pin:

* **One active claim per key, one per path.** Two partial unique indexes enforce it in
  SQLite, so two processes ensuring the same Work converge on one row.
* **Reuse, never replace.** A provider, account, chat or Run change asks for the same
  key and gets the same path back.
* **Legacy adoption only when the relation is authoritative.** An existing worktree at
  the requested path is adopted when Git registers it under that checkout, its remote
  is the claimed repository, and the card registered it — or, unregistered, it is clean
  and pushed, so nothing in it exists only there (#2816). When the Poller card holds
  another registered worktree of the same repository, or its registration could not be
  read, the answer is ``legacy`` and the caller stays on the old ownership.
* **Exactly one destructive owner.** Once a path is claimed, only ``release`` here may
  remove it; the Poller's cleanup hands such paths over instead of deleting them.
* **Release is Work-terminal only.** A Run ending releases nothing. The request is
  persisted before any deletion, a live worker in the tree defers it, and a dirty or
  unreadable tree keeps it pending with ``last_error`` for a person to see.
* **Verification evidence is not a workspace.** Releasing a claim never touches the
  Run evidence ledger (#2148); a removed worktree's A1/B4 evidence stays readable.

The persistent runtime owns the writes (``/work-workspaces/*`` on its socket); the web
only reads. The Poller engine never opens this database.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import github_identity
from .git_context import github_repository
from .poller_engine import paths as path_identity
from .poller_engine.github import GitHubAdapter, registered_worktree
from .sqlite_open import create_schema, use_wal

log = logging.getLogger(__name__)

# The runtime socket routes. The engine spells the release path out itself because it
# never imports the app; a test pins the two together.
ENSURE_PATH = "/work-workspaces/ensure"
RELEASE_PATH = "/work-workspaces/release"
CLAIMS_PATH = "/work-workspaces/claims"

#: The folder-name prefix of a worktree made ahead of a deck-started Work (#3590): the agent
#: starts in it before the issue exists, and the Work's claim adopts it once it does.
SPARE_PREFIX = "work-spare-"

PURPOSE_IMPLEMENTATION = "implementation"
PURPOSE_REVIEW = "review"
PURPOSE_ARTIFACT = "artifact"
PURPOSES = frozenset({PURPOSE_IMPLEMENTATION, PURPOSE_REVIEW, PURPOSE_ARTIFACT})

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_workspace_claims (
    work_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    purpose TEXT NOT NULL,
    path TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT '',
    base_ref TEXT NOT NULL DEFAULT '',
    claimed_at TEXT NOT NULL,
    confirmed_at TEXT,
    release_requested_at TEXT,
    released_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    reserved_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_workspace_claims_key
    ON work_workspace_claims(work_id, repository, purpose) WHERE released_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_workspace_claims_path
    ON work_workspace_claims(path) WHERE released_at IS NULL;
"""

_COLUMNS = (
    "work_id, repository, purpose, path, branch, base_ref, claimed_at, confirmed_at,"
    " release_requested_at, released_at, last_error, reserved_at"
)

# How long an unconfirmed reservation is left to the caller that made it. Worktree creation
# is bounded well inside this, so an older one was interrupted and may be taken over.
CREATION_STALE_S = 600


class ClaimError(ValueError):
    """A request this service refuses outright, with a reason a person can read."""


@dataclass(frozen=True)
class WorkspaceClaim:
    work_id: str
    repository: str  # lower-cased owner/repo
    purpose: str
    path: str  # resolved
    branch: str = ""
    base_ref: str = ""
    claimed_at: str = ""
    confirmed_at: str = ""
    release_requested_at: str = ""
    released_at: str = ""
    last_error: str = ""
    reserved_at: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.work_id, self.repository, self.purpose)

    @property
    def active(self) -> bool:
        return not self.released_at

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmed_at)

    @property
    def release_pending(self) -> bool:
        return self.active and bool(self.release_requested_at)

    def as_json(self) -> dict:
        return {
            "work_id": self.work_id,
            "repository": self.repository,
            "purpose": self.purpose,
            "path": self.path,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "claimed_at": self.claimed_at,
            "confirmed_at": self.confirmed_at,
            "release_requested_at": self.release_requested_at,
            "released_at": self.released_at,
            "last_error": self.last_error,
            "reserved_at": self.reserved_at,
        }

    @classmethod
    def from_json(cls, data: object) -> WorkspaceClaim | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(**{name: str(data.get(name) or "") for name in cls.__dataclass_fields__})
        except TypeError:
            return None


def _row(values: Sequence[object]) -> WorkspaceClaim:
    return WorkspaceClaim(*(str(value or "") for value in values))


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ClaimStore:
    """The claims table in the deck's SQLite database.

    Its own connection, so a claim write never waits on ``Db``'s lock, and every
    mutation is one ``BEGIN IMMEDIATE`` transaction: SQLite serializes writers across
    processes, and the unique indexes turn a lost race into the winner's row.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        use_wal(self._conn)
        with self._lock:
            create_schema(self._conn, SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _select(self, where: str, params: tuple = ()) -> list[WorkspaceClaim]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM work_workspace_claims WHERE {where}"
                " ORDER BY claimed_at, rowid",
                params,
            ).fetchall()
        return [_row(row) for row in rows]

    def active(self, work_id: str | None = None) -> list[WorkspaceClaim]:
        if work_id is None:
            return self._select("released_at IS NULL")
        return self._select("released_at IS NULL AND work_id = ?", (work_id,))

    def get(self, work_id: str, repository: str, purpose: str) -> WorkspaceClaim | None:
        found = self._select(
            "released_at IS NULL AND work_id = ? AND repository = ? AND purpose = ?",
            (work_id, repository, purpose),
        )
        return found[0] if found else None

    def holding(self, path: str) -> WorkspaceClaim | None:
        found = self._select("released_at IS NULL AND path = ?", (path,))
        return found[0] if found else None

    def reserve(self, claim: WorkspaceClaim) -> tuple[WorkspaceClaim, bool]:
        """The active claim for ``claim.key``, inserting ``claim`` unconfirmed when none.

        Answers ``(claim, inserted)``. A path already held under another key refuses:
        two Works must never be handed one directory.
        """
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM work_workspace_claims WHERE released_at IS NULL"
                    " AND work_id = ? AND repository = ? AND purpose = ?",
                    claim.key,
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return _row(row), False
                other = conn.execute(
                    f"SELECT {_COLUMNS} FROM work_workspace_claims"
                    " WHERE released_at IS NULL AND path = ?",
                    (claim.path,),
                ).fetchone()
                if other is not None:
                    conn.execute("ROLLBACK")
                    held = _row(other)
                    raise ClaimError(
                        f"{claim.path} is already claimed by {held.work_id}"
                        f" ({held.repository}, {held.purpose})"
                    )
                conn.execute(
                    f"INSERT INTO work_workspace_claims({_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, '', ?)",
                    (
                        *claim.key,
                        claim.path,
                        claim.branch,
                        claim.base_ref,
                        claim.claimed_at,
                        claim.reserved_at or claim.claimed_at,
                    ),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                # Another process inserted between our read and write; its row stands.
                conn.execute("ROLLBACK")
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM work_workspace_claims WHERE released_at IS NULL"
                    " AND work_id = ? AND repository = ? AND purpose = ?",
                    claim.key,
                ).fetchone()
                if row is None:
                    raise ClaimError(f"{claim.path} is already claimed") from None
                return _row(row), False
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return claim, True

    def _update(self, sql: str, params: tuple) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def take_creation(self, claim: WorkspaceClaim, *, stale_before: str) -> bool:
        """Win the right to (re)create this claim's worktree; True for exactly one caller.

        Taken from a confirmed claim whose worktree has gone, or from an unconfirmed one
        whose reservation is older than ``stale_before``; never from a fresh reservation,
        which belongs to whoever made it. The claim reads unconfirmed until it is settled.
        """
        return (
            self._update(
                "UPDATE work_workspace_claims SET reserved_at = ?, confirmed_at = NULL"
                " WHERE released_at IS NULL AND work_id = ? AND repository = ? AND purpose = ?"
                " AND reserved_at = ? AND (confirmed_at IS NOT NULL OR reserved_at < ?)",
                (_now(), *claim.key, claim.reserved_at, stale_before),
            )
            == 1
        )

    def confirm(self, claim: WorkspaceClaim) -> None:
        self._update(
            "UPDATE work_workspace_claims SET confirmed_at = COALESCE(confirmed_at, ?),"
            " last_error = '' WHERE released_at IS NULL AND work_id = ? AND repository = ?"
            " AND purpose = ?",
            (_now(), *claim.key),
        )

    def note_error(self, claim: WorkspaceClaim, error: str) -> None:
        self._update(
            "UPDATE work_workspace_claims SET last_error = ? WHERE released_at IS NULL"
            " AND work_id = ? AND repository = ? AND purpose = ?",
            (error[:500], *claim.key),
        )

    def withdraw_release(self, claim: WorkspaceClaim) -> None:
        """The Work is being executed again, so its terminal release no longer holds."""
        self._update(
            "UPDATE work_workspace_claims SET release_requested_at = NULL, last_error = ''"
            " WHERE released_at IS NULL AND work_id = ? AND repository = ? AND purpose = ?",
            claim.key,
        )

    def request_release(self, work_id: str) -> list[WorkspaceClaim]:
        """Persist the release request for every active claim of this Work, idempotently."""
        self._update(
            "UPDATE work_workspace_claims SET release_requested_at = ?"
            " WHERE released_at IS NULL AND work_id = ? AND release_requested_at IS NULL",
            (_now(), work_id),
        )
        return self.active(work_id)

    def mark_released(self, claim: WorkspaceClaim, *, error: str = "") -> None:
        self._update(
            "UPDATE work_workspace_claims SET released_at = ?, last_error = ?"
            " WHERE released_at IS NULL AND work_id = ? AND repository = ? AND purpose = ?",
            (_now(), error[:500], *claim.key),
        )


# --- the host side -----------------------------------------------------------


@dataclass(frozen=True)
class HostWorktree:
    """What the host says about one path."""

    exists: bool
    registered: bool = False  # Git lists it as a worktree of the checkout it sits in
    checkout: str = ""
    repository: str = ""  # lower-cased owner/repo of its origin remote, "" when unknown


class GitHost:
    """The host operations a claim needs, all through Git and all bounded."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self._runner = runner

    def _git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess | None:
        try:
            return self._runner(
                ["git", *args], capture_output=True, text=True, timeout=timeout, check=False,
                # A fetch reaches GitHub only through the owner rule (#2289).
                env=github_identity.github_git_environment(),
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def inspect(self, path: str) -> HostWorktree:
        target = Path(path)
        if not target.exists() and not target.is_symlink():
            return HostWorktree(exists=False)
        checkout, present = registered_worktree(path)
        if checkout is None or not present:
            return HostWorktree(exists=True)
        done = self._git("-C", path, "remote", "get-url", "origin", timeout=15)
        remote = done.stdout.strip() if done is not None and done.returncode == 0 else ""
        return HostWorktree(
            exists=True,
            registered=True,
            checkout=str(checkout),
            repository=(github_repository(remote) or "").lower() if remote else "",
        )

    def unsaved(self, path: str) -> str:
        """Why removing ``path`` could lose work, or "" when nothing there is only local.

        Clean (no changed or untracked files) and its commit already on an origin branch. An
        answer Git could not give is a reason too, never a pass.
        """
        status = self._git("-C", path, "status", "--porcelain", timeout=15)
        if status is None or status.returncode != 0:
            return "its status could not be read"
        if status.stdout.strip():
            return "it has uncommitted changes"
        # Only origin, refreshed and pruned now: a stale or deleted branch, or another
        # remote's, proves nothing about what the claimed repository holds (#2816 review).
        fetched = self._git("-C", path, "fetch", "--quiet", "--prune", "origin")
        if fetched is None or fetched.returncode != 0:
            return "origin could not be fetched to prove its commit is pushed"
        pushed = self._git(
            "-C", path, "for-each-ref", "--count=1", "--contains", "HEAD", "refs/remotes/origin/",
            timeout=15,
        )
        if pushed is None or pushed.returncode != 0:
            return "whether its commit is pushed could not be read"
        if not pushed.stdout.strip():
            return "its commit is not on any origin branch"
        return ""

    def current_branch(self, path: str) -> str:
        """The branch checked out at ``path``; "" when detached or unreadable."""
        done = self._git("-C", path, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=15)
        return done.stdout.strip() if done is not None and done.returncode == 0 else ""

    def switch(self, path: str, branch: str, base_ref: str) -> str:
        """Put the worktree at ``path`` on ``branch``; answers "" on success, else why not.

        Only for a clean, pushed worktree being taken over (#2816), so leaving its old branch
        loses nothing. A branch only origin has is checked out from there; one that exists
        nowhere yet starts from ``origin/<base_ref>``, exactly as ``create`` would make it.
        """
        if self.current_branch(path) == branch:
            return ""
        self._git("-C", path, "fetch", "--quiet", "origin", base_ref)

        def has(ref: str) -> bool:
            done = self._git("-C", path, "rev-parse", "--verify", "--quiet", ref, timeout=15)
            return done is not None and done.returncode == 0

        if has(f"refs/heads/{branch}"):
            done = self._git("-C", path, "switch", "--quiet", branch)
        elif has(f"refs/remotes/origin/{branch}"):
            # The card's branch is pushed but not checked out here: its commits, not a fresh
            # branch off the base that would leave the worker on the wrong history.
            done = self._git(
                "-C", path, "switch", "--quiet", "-c", branch, "--track", f"origin/{branch}"
            )
        else:
            done = self._git("-C", path, "switch", "--quiet", "-c", branch, f"origin/{base_ref}")
        if done is None:
            return f"git switch to {branch} did not run"
        if done.returncode != 0:
            return (done.stderr or done.stdout or f"git switch to {branch} failed").strip()[-300:]
        return ""

    def create(self, checkout: str, path: str, branch: str, base_ref: str) -> str:
        """Add the worktree; answers "" on success, else why not.

        A branch only origin has, as for a Work taken over from another deck (#3834), is
        checked out from there with its commits, never started again from the base.
        """
        self._git("-C", checkout, "fetch", "--quiet", "origin", base_ref)
        existing = self._git("-C", checkout, "rev-parse", "--verify", "--quiet", branch)
        # Fails quietly for a branch origin does not have, which is the usual start.
        self._git("-C", checkout, "fetch", "--quiet", "origin", branch)
        pushed = self._git(
            "-C", checkout, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"
        )
        if (
            existing is not None
            and existing.returncode == 0
            and pushed is not None
            and pushed.returncode == 0
        ):
            # A branch left here from an earlier stint, before another deck took the Work and
            # pushed more (#3834): moved up to origin's when that only adds commits. One with
            # commits origin lacks is kept as it is, and a branch checked out elsewhere is
            # refused by ``git branch -f``, so nothing local is lost.
            behind = self._git(
                "-C", checkout, "merge-base", "--is-ancestor", branch, f"origin/{branch}"
            )
            if behind is not None and behind.returncode == 0:
                self._git("-C", checkout, "branch", "--quiet", "-f", branch, f"origin/{branch}")
        if existing is not None and existing.returncode == 0:
            made = self._git("-C", checkout, "worktree", "add", path, branch)
        elif pushed is not None and pushed.returncode == 0:
            made = self._git(
                "-C", checkout, "worktree", "add", "--track", "-b", branch, path,
                f"origin/{branch}",
            )
        else:
            made = self._git(
                "-C", checkout, "worktree", "add", path, "-b", branch, f"origin/{base_ref}"
            )
        if made is None:
            return "git worktree add did not run"
        if made.returncode != 0:
            return (made.stderr or made.stdout or "git worktree add failed").strip()[-300:]
        return ""

    def create_spare(self, checkout: str, path: str, base_ref: str) -> str:
        """Add a detached worktree at a fresh ``origin/<base_ref>`` (#3590); "" or why not."""
        self._git("-C", checkout, "fetch", "--quiet", "origin", base_ref)
        made = self._git(
            "-C", checkout, "worktree", "add", "--quiet", "--detach", path, f"origin/{base_ref}"
        )
        if made is None:
            return "git worktree add did not run"
        if made.returncode != 0:
            return (made.stderr or made.stdout or "git worktree add failed").strip()[-300:]
        return ""

    def changed(self, path: str) -> bool:
        """Whether ``path`` has changed or untracked files; an unreadable status counts."""
        status = self._git("-C", path, "status", "--porcelain", timeout=15)
        return status is None or status.returncode != 0 or bool(status.stdout.strip())

    def branch_here(self, path: str, branch: str) -> str:
        """Start ``branch`` at the worktree's own commit, no fetch (#3590); "" or why not."""
        done = self._git("-C", path, "switch", "--quiet", "-c", branch, timeout=15)
        if done is None:
            return f"git switch to {branch} did not run"
        if done.returncode != 0:
            return (done.stderr or done.stdout or f"git switch to {branch} failed").strip()[-300:]
        return ""

    def remove(self, path: str) -> tuple[bool, str]:
        """``(released, why_not)`` through the engine's own dirty-safe removal (#1718)."""
        cleanup = GitHubAdapter.remove_worktrees([path])
        if cleanup.retained:
            return False, cleanup.retained[0][1]
        if cleanup.failed:
            return False, "the worktree could not be removed"
        return True, ""


# --- the service ---------------------------------------------------------------


@dataclass(frozen=True)
class EnsureRequest:
    work_id: str
    repository: str
    purpose: str
    checkout: str
    path: str
    branch: str
    base_ref: str
    # The worktrees the Poller card has registered, when the caller could read them.
    # None means unread, which is never permission to adopt.
    legacy_paths: tuple[str, ...] | None = ()
    run_id: str = ""
    # A spare worktree a deck-started Work's agent is already running in (#3590): adopted
    # only when clean and detached, and put on ``branch`` where it stands, with no fetch.
    spare: bool = False
    # A symlink beside it naming the Work's usual folder (``.worktrees/issue-<n>``).
    link: str = ""

    @classmethod
    def from_json(cls, data: dict) -> EnsureRequest:
        legacy = data.get("legacy_paths")
        return cls(
            work_id=str(data.get("work_id") or "").strip(),
            repository=str(data.get("repository") or "").strip().lower(),
            purpose=str(data.get("purpose") or "").strip(),
            checkout=str(data.get("checkout") or "").strip(),
            path=str(data.get("path") or "").strip(),
            branch=str(data.get("branch") or "").strip(),
            base_ref=str(data.get("base_ref") or "").strip(),
            legacy_paths=(
                tuple(str(item) for item in legacy if isinstance(item, str))
                if isinstance(legacy, list)
                else None
            ),
            run_id=str(data.get("run_id") or ""),
            spare=data.get("spare") is True,
            link=str(data.get("link") or "").strip(),
        )


# What an ensure answered. ``legacy`` means stay on the old ownership for this one;
# ``uncertain`` means an earlier creation's outcome is not settled and nothing was made.
CREATED, REUSED, ADOPTED, LEGACY, UNCERTAIN, FAILED = (
    "created",
    "reused",
    "adopted",
    "legacy",
    "uncertain",
    "failed",
)


@dataclass(frozen=True)
class EnsureResult:
    outcome: str
    claim: WorkspaceClaim | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.outcome in {CREATED, REUSED, ADOPTED} and self.claim is not None

    def as_json(self) -> dict:
        return {
            "outcome": self.outcome,
            "claim": self.claim.as_json() if self.claim else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReleaseOutcome:
    path: str
    released: bool
    pending: str = ""  # why it is still held, when it is

    def as_json(self) -> dict:
        return {"path": self.path, "released": self.released, "pending": self.pending}


def _validate(request: EnsureRequest) -> tuple[str, str]:
    """The resolved checkout and path, or ClaimError."""
    if not request.work_id:
        raise ClaimError("a claim needs a work_id")
    if "/" not in request.repository:
        raise ClaimError(f"repository {request.repository!r} is not owner/repo")
    if request.purpose not in PURPOSES:
        raise ClaimError(f"unknown purpose {request.purpose!r}")
    if not request.branch or not request.base_ref:
        raise ClaimError("a claim needs a branch and a base ref")
    checkout = Path(request.checkout)
    path = Path(request.path)
    if not checkout.is_absolute() or not path.is_absolute():
        raise ClaimError("checkout and path must be absolute")
    # The same eligibility rule every removal applies: a direct child of the checkout's
    # own .worktrees, so a claim can never name a directory a release must not touch.
    resolved_checkout = path_identity.resolve(str(checkout))
    resolved_path = path_identity.resolve(str(path))
    if (
        path.parent.name != ".worktrees"
        or path_identity.resolve(str(path.parent.parent)) != resolved_checkout
    ):
        raise ClaimError(f"{request.path} is not a direct child of {request.checkout}/.worktrees")
    return resolved_checkout, resolved_path


class WorkWorkspaceService:
    """Ensure, reuse and release Work claims; the runtime's workspace service.

    ``execution_active(path)`` answers whether a live worker is mid-turn inside the
    path: True, False, or None when that could not be read. Anything but False defers a
    release. All mutations run under one lock, so this process never races itself; the
    database indexes cover every other process.
    """

    def __init__(
        self,
        store: ClaimStore,
        *,
        host: GitHost | None = None,
        execution_active: Callable[[str], bool | None] = lambda _path: False,
    ) -> None:
        self.store = store
        self.host = host or GitHost()
        self.execution_active = execution_active
        self._lock = threading.Lock()

    def ensure(self, request: EnsureRequest) -> EnsureResult:
        checkout, path = _validate(request)
        with self._lock:
            return self._ensure(request, checkout, path)

    def _ensure(self, request: EnsureRequest, checkout: str, path: str) -> EnsureResult:
        key = (request.work_id, request.repository, request.purpose)
        existing = self.store.get(*key)
        if existing is not None:
            return self._resume(existing, request)

        held = self.store.holding(path)
        if held is not None:
            # Another Work's claim: neither adoption nor a legacy fallback may hand it over.
            raise ClaimError(
                f"{path} is already claimed by {held.work_id} ({held.repository}, {held.purpose})"
            )
        if request.spare:
            return self._adopt_spare(request, key, checkout, path)
        seen = self.host.inspect(path)
        adopt = False
        if seen.exists:
            if not seen.registered or path_identity.resolve(seen.checkout) != checkout:
                return EnsureResult(
                    FAILED, detail=f"{path} exists but is not a worktree of {checkout}"
                )
            if seen.repository != request.repository:
                return EnsureResult(
                    FAILED,
                    detail=f"{path} is a {seen.repository or 'unknown'} checkout,"
                    f" not {request.repository}",
                )
            ambiguity = self._legacy_ambiguity(request, path)
            if ambiguity:
                return EnsureResult(LEGACY, detail=ambiguity)
            adopt = True

        claim = WorkspaceClaim(
            *key,
            path=path,
            branch=request.branch,
            base_ref=request.base_ref,
            claimed_at=_now(),
        )
        # The claim is durable before anything touches the host, so a crash between the
        # two leaves an unconfirmed claim a later ensure resolves, never an orphan.
        claim, inserted = self.store.reserve(claim)
        if not inserted:  # another process won the key between our read and reserve
            return self._resume(claim, request)
        if adopt:
            registered = {path_identity.resolve(raw) for raw in request.legacy_paths or ()}
            return self._settle_existing(
                claim, ADOPTED, "existing worktree adopted", takeover=path not in registered
            )
        return self._create(claim, checkout)

    def _adopt_spare(
        self, request: EnsureRequest, key: tuple[str, str, str], checkout: str, path: str
    ) -> EnsureResult:
        """Claim the spare worktree a Work's agent already runs in (#3590).

        Fast on purpose, since the agent is working while this runs: no fetch, no pushed
        check. A spare holds nothing of anyone's: it was made detached at the base for this
        start, so clean and still detached proves nothing was done in it yet.
        """
        if not Path(path).name.startswith(SPARE_PREFIX):
            return EnsureResult(FAILED, detail=f"{path} is not a spare worktree")
        seen = self.host.inspect(path)
        if (
            not seen.registered
            or path_identity.resolve(seen.checkout) != checkout
            or seen.repository != request.repository
        ):
            return EnsureResult(FAILED, detail=f"{path} is not a {request.repository} worktree")
        if self.host.current_branch(path):
            return EnsureResult(FAILED, detail=f"{path} is already on a branch")
        if self.host.changed(path):
            return EnsureResult(FAILED, detail=f"{path} has changes")
        claim = WorkspaceClaim(
            *key,
            path=path,
            branch=request.branch,
            base_ref=request.base_ref,
            claimed_at=_now(),
        )
        claim, inserted = self.store.reserve(claim)
        if not inserted:
            return self._resume(claim, request)
        error = self.host.branch_here(path, request.branch)
        if error:
            self.store.mark_released(claim, error=error)
            return EnsureResult(FAILED, None, error)
        self.store.confirm(claim)
        _link(path, request.link)
        return EnsureResult(ADOPTED, self.store.get(*key), "spare worktree adopted")

    def _legacy_ambiguity(self, request: EnsureRequest, path: str) -> str:
        """Why adopting ``path`` would guess, or "" when the legacy relation is exact.

        The target should be one of the card's own registrations: a healthy worktree of the
        right repository at the expected path proves nothing about who owns it. One the card
        never registered is still taken over when it is clean and its commit is pushed, so
        nothing in it exists only there (#2816); otherwise it stays on legacy ownership.

        Every other path the card registered must be *proven* not to compete: gone from the
        host, or a registered worktree whose origin names a different repository. One that
        is present but unregistered, whose origin cannot be read or is not a GitHub remote,
        or that is the same repository leaves the card on legacy ownership. An unreadable
        sibling is unknown, never "some other repository".
        """
        if request.legacy_paths is None:
            return "the Poller card's registered worktrees could not be read"
        registered = {path_identity.resolve(raw) for raw in request.legacy_paths}
        if path not in registered:
            # Made by hand, most often by this card's own earlier chat after its Work ended
            # (#2816). Taking it over is safe only when it holds nothing that exists nowhere
            # else, since the Work's end removes what it claims.
            unsaved = self.host.unsaved(path)
            if unsaved:
                return (
                    f"{path} exists, the Poller card never registered it, and {unsaved};"
                    " commit and push it, or remove it, then post `/claude` again"
                )
        competing: list[str] = []
        unproven: list[str] = []
        for raw in request.legacy_paths:
            resolved = path_identity.resolve(raw)
            if resolved == path:
                continue
            seen = self.host.inspect(resolved)
            if not seen.exists:
                continue
            if not seen.registered or not seen.repository:
                unproven.append(resolved)
            elif seen.repository == request.repository:
                competing.append(resolved)
        if competing:
            return (
                f"the Poller card also registers {', '.join(sorted(competing))} for"
                f" {request.repository}; left on legacy ownership until reconciled"
            )
        if unproven:
            return (
                f"the Poller card also registers {', '.join(sorted(unproven))}, whose"
                " repository could not be proven; left on legacy ownership until reconciled"
            )
        return ""

    def _resume(self, claim: WorkspaceClaim, request: EnsureRequest) -> EnsureResult:
        """Hand back the Work's existing claim, settling it against the host first."""
        if claim.release_pending:
            # A new Run is starting, so the Work is not finished after all.
            self.store.withdraw_release(claim)
        seen = self.host.inspect(claim.path)
        healthy = seen.registered and seen.repository == claim.repository
        if healthy:
            if claim.confirmed:
                return EnsureResult(REUSED, self.store.get(*claim.key))
            # An unconfirmed claim over a worktree is a creation or a takeover a crash cut
            # short (#2816 review). Clean, pushed and on its branch, it is confirmed as it
            # stands; anything else is settled by the one caller that wins the reservation,
            # under the same check a first takeover makes.
            if (
                self.host.current_branch(claim.path) == claim.branch
                and not self.host.unsaved(claim.path)
            ):
                self.store.confirm(claim)
                return EnsureResult(REUSED, self.store.get(*claim.key))
            stale = datetime.fromtimestamp(time.time() - CREATION_STALE_S, UTC).isoformat()
            if not self.store.take_creation(claim, stale_before=stale):
                detail = "another caller is taking this worktree over, or just was"
                return EnsureResult(UNCERTAIN, self.store.get(*claim.key), detail)
            return self._settle_existing(self.store.get(*claim.key) or claim, REUSED, takeover=True)
        if seen.exists:
            # Something is at the claimed path that is not this claim's worktree. Making
            # another one elsewhere is exactly the second candidate this must never create.
            detail = f"{claim.path} is present but is not a {claim.repository} worktree"
            self.store.note_error(claim, detail)
            return EnsureResult(UNCERTAIN, self.store.get(*claim.key), detail)
        # Nothing on disk at the claimed path: an interrupted creation, or a worktree
        # someone removed by hand. The same claim is re-created at the same path, by the
        # one caller that wins it; a creation still in flight elsewhere is left alone.
        stale = datetime.fromtimestamp(time.time() - CREATION_STALE_S, UTC).isoformat()
        if not self.store.take_creation(claim, stale_before=stale):
            detail = "another caller is creating this worktree, or just was"
            return EnsureResult(UNCERTAIN, self.store.get(*claim.key), detail)
        checkout = str(Path(claim.path).parent.parent)
        return self._create(claim, path_identity.resolve(checkout), fresh=False)

    def _settle_existing(
        self, claim: WorkspaceClaim, outcome: str, detail: str = "", *, takeover: bool = False
    ) -> EnsureResult:
        """Confirm an existing worktree for a claim this caller holds, on the claim's branch.

        The one step every unconfirmed claim over an existing worktree goes through, first
        try or after a crash (#2816 review). A ``takeover`` (a worktree the card never
        registered, or a crash left unsettled) must hold nothing that exists solely there,
        checked now, under the reservation; so must any worktree on another branch, which
        is then switched. Otherwise, or when Git will not switch, the reservation is given
        up and the card is told why, since the worktree is still exactly what it was.
        """
        on_branch = self.host.current_branch(claim.path) == claim.branch
        if takeover or not on_branch:
            # Rechecked here, under the reservation: whatever was checked before it may have
            # changed since (#2816 review).
            unsaved = self.host.unsaved(claim.path)
            if unsaved:
                why = f"{claim.path} cannot be taken over: {unsaved}"
            elif on_branch:
                why = ""
            else:
                switched = self.host.switch(claim.path, claim.branch, claim.base_ref)
                why = (
                    f"{claim.path} could not be put on {claim.branch}: {switched}"
                    if switched
                    else ""
                )
            if why:
                self.store.mark_released(claim, error=why)
                return EnsureResult(LEGACY, detail=why)
        self.store.confirm(claim)
        return EnsureResult(outcome, self.store.get(*claim.key), detail)

    def _create(self, claim: WorkspaceClaim, checkout: str, *, fresh: bool = True) -> EnsureResult:
        """Run Git for a claim this caller holds; ``fresh`` when it was reserved just now."""
        try:
            error = self.host.create(checkout, claim.path, claim.branch, claim.base_ref)
        except Exception as exc:  # noqa: BLE001 -- outcome unknown; keep the claim unconfirmed
            detail = f"worktree creation outcome unknown: {exc}"
            self.store.note_error(claim, detail)
            return EnsureResult(UNCERTAIN, self.store.get(*claim.key), detail)
        if error:
            if self.host.inspect(claim.path).exists:
                # Git failed but left something behind: unsettled, not free to retry blind.
                self.store.note_error(claim, error)
                return EnsureResult(UNCERTAIN, self.store.get(*claim.key), error)
            if not fresh:
                # The Work keeps its claim; the next attempt waits out the reservation.
                self.store.note_error(claim, error)
                return EnsureResult(FAILED, self.store.get(*claim.key), error)
            # A clean failure left nothing on the host, so the reservation is history.
            self.store.mark_released(claim, error=error)
            return EnsureResult(FAILED, None, error)
        self.store.confirm(claim)
        return EnsureResult(
            CREATED if fresh else REUSED,
            self.store.get(*claim.key),
            "" if fresh else "recreated at the claimed path",
        )

    # --- release ---------------------------------------------------------------

    def release(
        self,
        work_id: str,
        paths: Iterable[str] | None = None,
        *,
        execution_active: Callable[[str], bool | None] | None = None,
    ) -> tuple[list[ReleaseOutcome], list[str]]:
        """The Work finished: release every claim it holds (#2149).

        Answers (per-claim outcomes, the ``paths`` no active claim holds). ``paths`` is what
        the caller (the Poller's cleanup) would otherwise delete itself; the ones returned
        stay its own to remove. A path another Work claims is neither released nor returned.
        Every claim of this Work is released, not only the named ones: the Work is over
        whichever of its worktrees the card happened to record.
        """
        with self._lock:
            unclaimed: list[str] = []
            for raw in paths or ():
                # Only a path no active claim holds goes back to its legacy owner. One another
                # Work claims is that Work's alone: returned as unclaimed, the caller's cleanup
                # would delete a live worktree out from under it.
                if self.store.holding(path_identity.resolve(raw)) is None:
                    unclaimed.append(raw)
            # Persisted first: a crash from here on still leaves the Work release-eligible.
            claims = self.store.request_release(work_id)
            return [self._release_one(claim, execution_active) for claim in claims], unclaimed

    def release_pending(
        self, *, execution_active: Callable[[str], bool | None] | None = None
    ) -> list[ReleaseOutcome]:
        """Retry every claim whose release was requested and has not finished."""
        with self._lock:
            return [
                self._release_one(claim, execution_active)
                for claim in self.store.active()
                if claim.release_pending
            ]

    def _release_one(
        self,
        claim: WorkspaceClaim,
        execution_active: Callable[[str], bool | None] | None = None,
    ) -> ReleaseOutcome:
        stale = datetime.fromtimestamp(time.time() - CREATION_STALE_S, UTC).isoformat()
        if not claim.confirmed and claim.reserved_at >= stale:
            # Its creation may still be running; removing under it would race Git.
            reason = "its worktree is still being created"
            self.store.note_error(claim, reason)
            return ReleaseOutcome(claim.path, False, reason)
        active = (execution_active or self.execution_active)(claim.path)
        if active is not False:
            reason = (
                "a live worker is running in it"
                if active
                else "whether a worker is running in it could not be read"
            )
            self.store.note_error(claim, reason)
            return ReleaseOutcome(claim.path, False, reason)
        seen = self.host.inspect(claim.path)
        if not seen.exists:
            _unlink_links(claim.path)
            self.store.mark_released(claim)
            return ReleaseOutcome(claim.path, True)
        released, why = self.host.remove(claim.path)
        if released:
            _unlink_links(claim.path)
            self.store.mark_released(claim)
            return ReleaseOutcome(claim.path, True)
        self.store.note_error(claim, why)
        return ReleaseOutcome(claim.path, False, why)

    def claimed_paths(self) -> list[str]:
        return [claim.path for claim in self.store.active()]


def _link(path: str, link: str) -> None:
    """Make ``link`` a symlink to ``path``, beside it in the same ``.worktrees`` (#3590).

    Only a convenience for a person looking for the Work by its usual folder name, so a
    failure (something already there) is logged, never an error.
    """
    if not link:
        return
    target = Path(link)
    beside = path_identity.resolve(str(target.parent)) == path_identity.resolve(
        str(Path(path).parent)
    )
    if not target.is_absolute() or not beside:
        log.warning("work workspace: %s is not beside %s; no link made", link, path)
        return
    try:
        os.symlink(path, target, target_is_directory=True)
    except OSError as exc:
        log.warning("work workspace: could not link %s to %s: %s", link, path, exc)


def _unlink_links(path: str) -> None:
    """Remove every symlink beside ``path`` that names it: the released Work's links."""
    folder = Path(path).parent
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink() and os.readlink(entry) == path:
                entry.unlink()
        except OSError:
            log.warning("work workspace: could not remove the link %s", entry, exc_info=True)


def read_claims(db_path: Path | str, work_id: str) -> list[WorkspaceClaim] | None:
    """One Work's active claims, read-only, for the web's read model.

    ``[]`` when there are none yet (no database, no table); None when the store exists
    and could not be read, which the read model reports as unknown rather than absent.
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM work_workspace_claims"
            " WHERE released_at IS NULL AND work_id = ? ORDER BY claimed_at, rowid",
            (work_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [] if "no such table" in str(exc) else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [_row(row) for row in rows]
