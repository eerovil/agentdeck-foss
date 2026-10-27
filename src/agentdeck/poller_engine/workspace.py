"""What a job actually verified: an immutable workspace snapshot (#1473).

``HEAD`` is not enough to identify what was tested.  A worker can submit a full suite, edit
three tracked files and add an untracked one while it runs, and the result comes back green
for a tree nobody ran.  A snapshot therefore covers the repository identity, the branch and
``HEAD``, the staged difference, the tracked working-tree difference, a bounded manifest of
untracked content, submodule state where there is any, the command that was run and the
runtime identity it ran under.

Two properties matter more than completeness:

*Bounded.*  Every command has a timeout, the whole collection has a deadline, and output is
spooled to a temporary file rather than read into memory -- a repository with an enormous
diff must not make snapshotting cost more than the job it describes.

*Never optimistic.*  Anything that could not be read is named in ``unprovable`` and makes
``provable`` false.  Unknown workspace state is not verified state, and a caller may not
credit a result against it.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "UNRESOLVED_REPOSITORY",
    "WorkspaceSnapshot",
    "collect_workspace_snapshot",
    "command_fingerprint",
    "github_repository",
    "redact",
    "repository_snapshots",
]

_COMMAND_TIMEOUT_S = 2.0
_AGGREGATE_TIMEOUT_S = 8.0
# An untracked file larger than this is named but not digested; the snapshot says so rather
# than reading a multi-gigabyte artifact into the hash.
_MAX_UNTRACKED_BYTES = 1 << 20
_MAX_UNTRACKED_FILES = 200
_HASH_CHUNK = 1 << 16
_MAX_GIT_PATH_BYTES = 1 << 16
_MAX_GIT_TEXT_BYTES = 4 << 10


_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*\S+"
)
_TOKEN_SECRET = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9]{8,})"
)
_REDACTED = "[redacted]"

# Prefix of the key a checkout with no remote takes among repository-keyed snapshots.
UNRESOLVED_REPOSITORY = "unresolved:"


def redact(text: str) -> str:
    """Blank known credential shapes before anything is persisted or shown."""
    without_assignments = _ASSIGNED_SECRET.sub(_REDACTED, text)
    return _TOKEN_SECRET.sub(_REDACTED, without_assignments)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def command_fingerprint(argv: tuple[str, ...] | list[str]) -> str:
    """A stable identity for the command a job ran, with no secrets in it.

    The words of the command are what identify it; a value that looks like a credential is
    dropped rather than hashed in, because a fingerprint is stored and shown.
    """
    words = [redact(str(word)) for word in argv]
    return _digest("\x00".join(words))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """An immutable identity for one workspace at one moment."""

    root: str = ""
    branch: str = ""
    head: str = ""
    staged_sha256: str = ""
    tracked_diff_sha256: str = ""
    untracked_sha256: str = ""
    untracked_count: int = 0
    untracked_omitted: int = 0
    submodules_sha256: str = ""
    command_fingerprint: str = ""
    runtime_identity: str = ""
    # Where this checkout came from and what it branched off (#1737). A path cannot say
    # whether two runs tested the same project -- the live checkout and a card's worktree are
    # different paths of one repository -- so the remote is what identifies it, and the merge
    # base is the base commit a failure can be attributed to.
    repo_identity: str = ""
    base_ref: str = ""
    base_head: str = ""
    # ``owner/repo`` of a GitHub ``origin``, lower-cased, or empty when it cannot be proven
    # (#2148): what repository-keyed evidence is keyed by, and what a person reads.
    repository: str = ""
    unprovable: tuple[str, ...] = ()

    @property
    def provable(self) -> bool:
        """Whether every part of this snapshot was actually observed."""
        return not self.unprovable and bool(self.root and self.head)

    @property
    def clean(self) -> bool:
        """Whether ``head`` alone describes this tree, with nothing uncommitted on top.

        Only a clean tree's result is reusable as a base result: a run over an edited tree
        verified a state no other card can ever be at.
        """
        empty = hashlib.sha256(b"").hexdigest()
        return (
            self.provable
            and self.staged_sha256 == empty
            and self.tracked_diff_sha256 == empty
            and self.untracked_sha256 == empty
            and self.untracked_count == 0
        )

    @property
    def snapshot_id(self) -> str:
        """The immutable id a job result is fenced against.

        An unprovable snapshot still gets an id -- it has to be recorded and compared like any
        other -- but callers gate on :attr:`provable` before treating it as evidence.

        The base fields are deliberately absent: results already recorded carry ids computed
        without them, and adding a field here would make every one of them read as a different
        workspace.
        """
        return _digest(
            json.dumps(
                {
                    "root": self.root,
                    "branch": self.branch,
                    "head": self.head,
                    "staged": self.staged_sha256,
                    "tracked_diff": self.tracked_diff_sha256,
                    "untracked": self.untracked_sha256,
                    "untracked_count": self.untracked_count,
                    "submodules": self.submodules_sha256,
                    "command": self.command_fingerprint,
                    "runtime": self.runtime_identity,
                    "unprovable": list(self.unprovable),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @property
    def comparison(self) -> tuple:
        """What the tree held, as a plain value, for comparing two readings (#2148).

        ``snapshot_id`` names one reading on one host -- root, command and runtime included --
        and stays byte-for-byte what recorded rows carry. Whether two checkouts (a worktree
        here, the same commit on another host) hold the same code is a different question that
        a path must not answer, so this is those same fields without the host ones. It is a
        derived value, never stored: the job row is what identifies a piece of evidence. The
        command is left out too, because a workspace read now has none and still has to match
        the one a job read.
        """
        return (
            self.repository,
            self.head,
            self.staged_sha256,
            self.tracked_diff_sha256,
            self.untracked_sha256,
            self.untracked_count,
            self.submodules_sha256,
            self.unprovable,
        )

    @property
    def repository_key(self) -> str:
        """The key this snapshot takes in a repository-keyed collection.

        A checkout with no remote still needs a distinct key, but it is not a repository
        anyone can name on another host, so the key says so and carries the path.
        """
        return self.repository or f"{UNRESOLVED_REPOSITORY}{self.root}"

    def as_record(self) -> dict:
        record = {
            "snapshot_id": self.snapshot_id,
            "root": self.root,
            "branch": self.branch,
            "head": self.head,
            "staged_sha256": self.staged_sha256,
            "tracked_diff_sha256": self.tracked_diff_sha256,
            "untracked_sha256": self.untracked_sha256,
            "untracked_count": self.untracked_count,
            "untracked_omitted": self.untracked_omitted,
            "submodules_sha256": self.submodules_sha256,
            "command_fingerprint": self.command_fingerprint,
            "runtime_identity": self.runtime_identity,
            "repo_identity": self.repo_identity,
            "base_ref": self.base_ref,
            "base_head": self.base_head,
            "repository": self.repository,
            "provable": self.provable,
            "clean": self.clean,
        }
        if self.unprovable:
            record["unprovable"] = list(self.unprovable)
        return record

    @classmethod
    def from_record(cls, record: object) -> WorkspaceSnapshot:
        if not isinstance(record, dict):
            return cls(unprovable=("no recorded snapshot",))
        unprovable = record.get("unprovable")
        return cls(
            root=str(record.get("root") or ""),
            branch=str(record.get("branch") or ""),
            head=str(record.get("head") or ""),
            staged_sha256=str(record.get("staged_sha256") or ""),
            tracked_diff_sha256=str(record.get("tracked_diff_sha256") or ""),
            untracked_sha256=str(record.get("untracked_sha256") or ""),
            untracked_count=int(record.get("untracked_count") or 0),
            untracked_omitted=int(record.get("untracked_omitted") or 0),
            submodules_sha256=str(record.get("submodules_sha256") or ""),
            command_fingerprint=str(record.get("command_fingerprint") or ""),
            runtime_identity=str(record.get("runtime_identity") or ""),
            repo_identity=str(record.get("repo_identity") or ""),
            base_ref=str(record.get("base_ref") or ""),
            base_head=str(record.get("base_head") or ""),
            repository=str(record.get("repository") or ""),
            unprovable=tuple(str(item) for item in unprovable)
            if isinstance(unprovable, list | tuple)
            else (),
        )


def repository_snapshots(
    snapshots: Iterable[WorkspaceSnapshot],
) -> dict[str, WorkspaceSnapshot]:
    """Key snapshots by repository, the canonical shape of multi-repository evidence (#2148).

    Two checkouts of one repository holding the same content are one entry. Two holding
    different content cannot both be what "this repository" was, so the entry is kept and
    made unprovable rather than one of them being picked.
    """
    keyed: dict[str, WorkspaceSnapshot] = {}
    for snapshot in snapshots:
        key = snapshot.repository_key
        held = keyed.get(key)
        if held is None:
            keyed[key] = snapshot
        elif held.comparison != snapshot.comparison:
            keyed[key] = replace(
                held,
                unprovable=(
                    *held.unprovable,
                    f"another checkout of this repository differs ({snapshot.root})",
                ),
            )
    return keyed


class _Deadline:
    def __init__(self, budget: float) -> None:
        self._end = time.monotonic() + budget

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._end

    @property
    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())


def collect_workspace_snapshot(
    cwd: str,
    *,
    command: tuple[str, ...] | list[str] = (),
    runtime_identity: str = "",
    aggregate_timeout_s: float = _AGGREGATE_TIMEOUT_S,
) -> WorkspaceSnapshot:
    """Observe one workspace within a bounded time budget."""
    from . import paths as path_identity

    deadline = _Deadline(max(0.5, float(aggregate_timeout_s)))
    unprovable: list[str] = []
    fingerprint = command_fingerprint(command) if command else ""

    root = _text(cwd, ("rev-parse", "--show-toplevel"), deadline, unprovable, "root")
    if not root:
        return WorkspaceSnapshot(
            root=path_identity.resolve(cwd),
            command_fingerprint=fingerprint,
            runtime_identity=runtime_identity,
            unprovable=tuple(unprovable or ["not a git repository"]),
        )

    branch = _text(cwd, ("branch", "--show-current"), deadline, unprovable, "branch")
    head = _text(cwd, ("rev-parse", "HEAD"), deadline, unprovable, "head")
    staged = _hashed(
        cwd, ("diff-index", "--cached", "--raw", "HEAD"), deadline, unprovable, "staged"
    )
    tracked = _hashed(cwd, ("diff", "--no-color"), deadline, unprovable, "tracked diff")
    submodules = _hashed(cwd, ("submodule", "status"), deadline, unprovable, "submodules")
    untracked, count, omitted = _untracked(cwd, deadline, unprovable)
    repo_identity, repository, base_ref, base_head = _origin(cwd, deadline)

    return WorkspaceSnapshot(
        root=path_identity.resolve(root),
        branch=branch,
        head=head,
        staged_sha256=staged,
        tracked_diff_sha256=tracked,
        untracked_sha256=untracked,
        untracked_count=count,
        untracked_omitted=omitted,
        submodules_sha256=submodules,
        command_fingerprint=fingerprint,
        runtime_identity=runtime_identity,
        repo_identity=repo_identity,
        base_ref=base_ref,
        base_head=base_head,
        repository=repository,
        unprovable=tuple(unprovable),
    )


def _origin(cwd: str, deadline: _Deadline) -> tuple[str, str, str, str]:
    """The repository's remote identity and name, and the base commit it branched off.

    None of it is ever ``unprovable``. A repository with no remote, or one whose base branch
    cannot be resolved, is a repository whose results simply cannot be compared across
    checkouts -- which is a missing comparison, not an unverified workspace, and a workspace
    that stopped being verifiable here would park cards that have nothing to do with this.
    """
    remote = _optional_text(cwd, ("remote", "get-url", "origin"), deadline)
    identity = _digest(_normalize_remote(remote)) if remote else ""
    repository = (github_repository(remote) or "").lower() if remote else ""
    base_ref = _optional_text(cwd, ("rev-parse", "--abbrev-ref", "origin/HEAD"), deadline)
    if not base_ref:
        for candidate in ("origin/master", "origin/main"):
            if _optional_text(cwd, ("rev-parse", "--verify", "--quiet", candidate), deadline):
                base_ref = candidate
                break
    if not base_ref:
        return identity, repository, "", ""
    base_head = _optional_text(cwd, ("merge-base", "HEAD", base_ref), deadline)
    return identity, repository, base_ref if base_head else "", base_head


_GITHUB_REMOTE_RE = re.compile(
    r"(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def github_repository(remote: str) -> str | None:
    """Return ``owner/repo`` for a github.com remote."""
    match = _GITHUB_REMOTE_RE.search(remote.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _normalize_remote(url: str) -> str:
    """One spelling for the several ways the same remote is written.

    ``git@host:owner/repo.git`` and ``https://host/owner/repo`` are the same repository, and a
    baseline recorded under one spelling has to be found under the other.
    """
    text = url.strip().removesuffix(".git")
    for prefix in ("ssh://", "https://", "http://", "git://"):
        text = text.removeprefix(prefix)
    text = text.split("@", 1)[-1]
    return text.replace(":", "/").strip("/").lower()


def _optional_text(cwd: str, args: tuple[str, ...], deadline: _Deadline) -> str:
    """Read one short git answer, treating anything unreadable as simply absent."""
    result = _run(cwd, args, deadline, _bounded_text)
    if result is None or result[0] != 0:
        return ""
    text, truncated = result[1]
    return "" if truncated else text.decode("utf-8", "replace").strip()


def _run[T](
    cwd: str,
    args: tuple[str, ...],
    deadline: _Deadline,
    consume: Callable[[BinaryIO], T],
) -> tuple[int, T] | None:
    """Run one git command and consume its file-backed output while the spool is open."""
    if deadline.expired:
        return None
    timeout = min(_COMMAND_TIMEOUT_S, deadline.remaining) or 0.01
    try:
        with tempfile.TemporaryFile() as spool:
            completed = subprocess.run(  # noqa: S603  (fixed argv, no shell)
                ["git", "-C", str(cwd), *args],
                stdout=spool,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            spool.seek(0)
            return completed.returncode, consume(spool)
    except (OSError, subprocess.SubprocessError):
        return None


def _text(
    cwd: str,
    args: tuple[str, ...],
    deadline: _Deadline,
    unprovable: list[str],
    what: str,
) -> str:
    result = _run(cwd, args, deadline, _bounded_text)
    if result is None or result[0] != 0:
        # A branchless detached HEAD legitimately answers empty with status 0; only a failed
        # or timed-out read is unprovable.
        if result is None or what != "branch":
            unprovable.append(what)
        return ""
    text, truncated = result[1]
    if truncated:
        unprovable.append(f"{what} exceeds the text bound")
        return ""
    return text.decode("utf-8", "replace").strip()


def _hashed(
    cwd: str,
    args: tuple[str, ...],
    deadline: _Deadline,
    unprovable: list[str],
    what: str,
) -> str:
    result = _run(cwd, args, deadline, _stream_sha256)
    if result is None:
        unprovable.append(what)
        return ""
    if result[0] != 0:
        # `submodule status` in a repository without submodules is the common benign case.
        if what != "submodules":
            unprovable.append(what)
        return hashlib.sha256(b"").hexdigest()
    return result[1]


def _untracked(cwd: str, deadline: _Deadline, unprovable: list[str]) -> tuple[str, int, int]:
    """A bounded digest over untracked content: names, sizes and file digests."""
    result = _run(
        cwd,
        ("ls-files", "--others", "--exclude-standard", "-z"),
        deadline,
        _bounded_nul_names,
    )
    if result is None or result[0] != 0:
        unprovable.append("untracked files")
        return "", 0, 0
    kept, count, oversized = result[1]
    omitted = count - len(kept)
    if oversized:
        unprovable.append(f"{oversized} untracked paths exceed the snapshot bound")
    if omitted:
        unprovable.append(f"{omitted} untracked files beyond the snapshot bound")
    digest = hashlib.sha256()
    for name in kept:
        digest.update(name.encode("utf-8", "surrogatepass"))
        path = Path(cwd) / name
        try:
            size = path.stat().st_size
        except OSError:
            unprovable.append(f"untracked file {name}")
            continue
        digest.update(str(size).encode())
        if size > _MAX_UNTRACKED_BYTES:
            unprovable.append(f"untracked file {name} exceeds the snapshot digest bound")
            continue
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_HASH_CHUNK):
                    digest.update(chunk)
        except OSError:
            unprovable.append(f"untracked file {name}")
    return digest.hexdigest(), count, omitted


def _stream_sha256(handle: BinaryIO) -> str:
    """Hash a spool incrementally; command output size never becomes memory size."""
    digest = hashlib.sha256()
    while chunk := handle.read(_HASH_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _bounded_text(handle: BinaryIO) -> tuple[bytes, bool]:
    """Keep only the tiny textual answers used for root, branch and HEAD."""
    text = handle.read(_MAX_GIT_TEXT_BYTES + 1)
    return text[:_MAX_GIT_TEXT_BYTES], len(text) > _MAX_GIT_TEXT_BYTES


def _bounded_nul_names(handle: BinaryIO) -> tuple[list[str], int, int]:
    """Count every NUL record while retaining only the first bounded path list.

    ``git ls-files`` emits paths in index order, so its first bounded records are stable.  The
    current record buffer and retained list have explicit bounds. Oversized records are counted
    but discarded so malformed output cannot turn one record into an unbounded allocation.
    """
    kept: list[str] = []
    count = 0
    oversized = 0
    pending = bytearray()
    record_overflowed = False
    while chunk := handle.read(_HASH_CHUNK):
        start = 0
        while True:
            end = chunk.find(b"\0", start)
            part = chunk[start:] if end < 0 else chunk[start:end]
            if not record_overflowed:
                remaining = _MAX_GIT_PATH_BYTES - len(pending)
                pending.extend(part[:remaining])
                if len(part) > remaining:
                    pending.clear()
                    record_overflowed = True
            if end < 0:
                break
            if pending or record_overflowed:
                count += 1
                if record_overflowed:
                    oversized += 1
                elif len(kept) < _MAX_UNTRACKED_FILES:
                    kept.append(pending.decode("utf-8", "replace"))
            pending.clear()
            record_overflowed = False
            start = end + 1
    if pending or record_overflowed:
        count += 1
        if record_overflowed:
            oversized += 1
        elif len(kept) < _MAX_UNTRACKED_FILES:
            kept.append(pending.decode("utf-8", "replace"))
    return kept, count, oversized
