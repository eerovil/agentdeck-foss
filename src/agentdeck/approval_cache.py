"""Reuse one narrowly scoped approval instead of paying for a second guardian run.

An AgentDeck-owned Codex chat escalates out of its workspace-write sandbox many
times per turn — the sandbox re-marks each workspace root's own ``.git`` as
read-only, so every ``git commit`` and ``git fetch`` in the worker's own checkout
asks permission. A 24-hour audit found 403 escalations adjudicated by 44 guardian
sessions costing ~15.5M tokens, of which 400 were allowed and 3 denied (#1738).
The guard earns its keep on those 3; what it does not earn is re-deciding the same
``git commit`` in the same worktree forty times.

This module turns an adjudicated escalation into a *capability grant* and answers
the next identical escalation from it. Three properties do the safety work, and
all three are structural rather than advisory:

Exact keys only
    A grant is stored under a frozen :class:`CapabilityKey` and retrieved by
    ``==``. There is no prefix match, no subset match, no "nearby" lookup
    anywhere in this module, so a grant cannot widen into a path, repository,
    remote or command it did not name. A materially different argument produces a
    different ``shape`` and therefore a different key, which is a miss.

Fail closed
    :func:`capability_key` returns ``None`` — meaning "ask the guardian" — for
    anything it does not positively understand: an unparseable command, shell
    metacharacters, an unknown program, an operand that resolves outside the
    worktree, an unexpanded ``$``. Uncertainty is never resolved in favour of the
    cache.

Only two classes are cacheable
    :data:`CACHEABLE_CLASSES` is ``READ`` and ``LOCAL_WRITE``. A remote mutation
    or a destructive/credential-bearing command is classified as such by an
    explicit table and can never be granted, so no amount of local low-risk
    traffic adds up to an approved ``git push``.

Repository policy is part of the key: editing the ``AGENTS.md``/``CLAUDE.md`` that
governs a worktree changes its fingerprint, so every grant taken under the old
policy stops matching and is pruned.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

# Action classes, coarsest-to-finest in the order the issue names them.
READ = "read"
LOCAL_WRITE = "local_write"
REMOTE_MUTATION = "remote_mutation"
SENSITIVE = "sensitive"

#: The only classes a grant may ever be stored for. Everything else keeps going
#: to the guardian however often it is asked.
CACHEABLE_CLASSES = frozenset({READ, LOCAL_WRITE})

ALLOW = "allow"
DENY = "deny"

# A grant is deliberately short-lived: it is an optimisation for one worker's
# burst of repeated escalations, not standing trust that outlives the work.
DEFAULT_TTL_S = 3600.0
DEFAULT_MAX_GRANTS = 512

# Anything here means the string is not a single command we can reason about.
# `sh -c "a && b"` has to reach the guardian, not be guessed at.
_SHELL_METACHARACTERS = frozenset("|&;<>()`\n\r*?[]{}!#")

# Files whose contents are the repository policy a worker runs under. Changing
# any of them must invalidate the grants taken while it said something else.
POLICY_FILES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", ".agents/policy.md")


class _Unclassifiable(Exception):
    """Raised internally the moment anything is uncertain, to fail closed."""


@dataclass(frozen=True)
class WorkerScope:
    """The enforceable identity a grant is confined to.

    Every field is something AgentDeck knows first-hand rather than something the
    primary agent reported about itself.
    """

    worker: str
    """Logical worker/session identity — the owned Codex thread id."""

    repository: str
    """Repository identity, stable across worktrees of the same repo."""

    worktree: str
    """Absolute worktree root. Nothing outside it is ever cacheable."""

    policy_fingerprint: str
    """Digest of the repository policy in force when the grant was taken."""


@dataclass(frozen=True)
class CapabilityKey:
    """What exactly was approved. Compared only by equality, never by containment."""

    worker: str
    repository: str
    worktree: str
    policy_fingerprint: str
    action_class: str
    cwd: str
    """Worktree-relative directory the command runs in.

    In the key because a bare operand like ``src`` names a different file from a
    different directory, so two identical command strings are only the same
    capability when they run in the same place.
    """

    shape: tuple[str, ...]
    """Normalized argv. Verbatim except for resolved paths and declared-inert text."""

    path_scope: tuple[str, ...]
    """Worktree-relative paths the command names, sorted. Empty means none named."""

    def describe(self) -> str:
        """One auditable line naming the capability, for the hit record."""
        scope = ", ".join(self.path_scope) if self.path_scope else "no named path"
        return f"{self.action_class} {' '.join(self.shape)} in {self.cwd} [{scope}]"


@dataclass(frozen=True)
class Decision:
    """A real adjudication — a guardian verdict or a human click."""

    key: CapabilityKey
    outcome: str
    approval_id: str
    decided_at: float


@dataclass(frozen=True)
class CacheHit:
    """Why this request was answered without a guardian.

    Carries the originating approval so a reader can go and check the decision
    that is being reused, which is what makes a hit auditable rather than magic.
    """

    key: CapabilityKey
    origin_approval_id: str
    origin_decided_at: float
    matched_at: float
    reason: str

    @property
    def age_s(self) -> float:
        return self.matched_at - self.origin_decided_at

    def audit_line(self) -> str:
        return (
            f"approval-cache hit: reused {self.origin_approval_id} "
            f"({self.age_s:.0f}s old) for {self.key.describe()}; {self.reason}"
        )


def policy_fingerprint(worktree: Path | str, *, files: Sequence[str] = POLICY_FILES) -> str:
    """Digest the repository policy governing ``worktree``.

    A missing file contributes its absence, so adding an ``AGENTS.md`` where
    there was none invalidates prior grants exactly as editing one does.
    """
    root = Path(worktree)
    digest = hashlib.sha256()
    for name in files:
        candidate = root / name
        digest.update(name.encode())
        try:
            digest.update(b"\x01")
            digest.update(candidate.read_bytes())
        except OSError:
            digest.update(b"\x00")
    return digest.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Classification
#
# Every table below is an allowlist. A program that appears in none of them is
# unclassifiable and therefore uncacheable — that is the intended outcome for
# `bash`, `python`, `make`, `npm` and anything else that runs arbitrary code.
# ---------------------------------------------------------------------------

# Read-only programs, plus the flags that would make them stop being read-only.
_READ_PROGRAMS: dict[str, frozenset[str]] = {
    "ls": frozenset(),
    "cat": frozenset(),
    "head": frozenset(),
    "tail": frozenset({"-f", "--follow"}),
    "wc": frozenset(),
    "stat": frozenset(),
    "file": frozenset(),
    "pwd": frozenset(),
    "basename": frozenset(),
    "dirname": frozenset(),
    "realpath": frozenset(),
    "du": frozenset(),
    "tree": frozenset(),
    "diff": frozenset(),
    "rg": frozenset({"--pre", "--hostname-bin"}),
    "grep": frozenset(),
    "jq": frozenset(),
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
    ),
}

_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "rev-parse",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
        "blame",
        "shortlog",
        "rev-list",
    }
)

_GIT_LOCAL_WRITE_SUBCOMMANDS = frozenset(
    {
        "add",
        "commit",
        "fetch",
        "switch",
        "restore",
        "stash",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "tag",
        "branch",
        "checkout",
        "worktree",
        "reset",
    }
)

_GIT_REMOTE_SUBCOMMANDS = frozenset({"push", "remote"})

# Second-level forms that throw work away even though their first-level
# subcommand is an ordinary local write. `git stash` is a local write, but
# `git stash drop` destroys a stash; `git worktree` is a local write, but
# `git worktree remove` deletes a checkout. Classification that stopped at the
# first subcommand would have made these cacheable, so each first-level
# subcommand that has destructive children lists them here. A first-level
# subcommand absent from this map has no second level we distinguish.
_GIT_DESTRUCTIVE_SECOND_LEVEL: dict[str, frozenset[str]] = {
    "stash": frozenset({"drop", "clear", "pop"}),
    "worktree": frozenset({"remove", "prune"}),
    "branch": frozenset({"delete"}),
    "tag": frozenset({"delete"}),
    "remote": frozenset({"remove", "rm", "prune", "set-url"}),
    "reflog": frozenset({"delete", "expire"}),
}

# Flags that turn an otherwise ordinary local git write into a destructive one.
# Their presence demotes the command to SENSITIVE, which is never cacheable.
_GIT_DESTRUCTIVE_FLAGS = frozenset(
    {
        "--hard",
        "--force",
        "-f",
        "--force-with-lease",
        # Both spellings of delete: `-D` force-deletes an unmerged branch, and the
        # lowercase `-d` still deletes one (and a tag, and a stash entry).
        "-D",
        "-d",
        "--delete",
        "--prune",
        "-X",
    }
)

# Programs that are always sensitive, listed explicitly so they can never be
# reached by a future widening of the read/local-write tables.
_SENSITIVE_PROGRAMS = frozenset(
    {
        "rm",
        "sudo",
        "su",
        "doas",
        "chmod",
        "chown",
        "chgrp",
        "kill",
        "pkill",
        "killall",
        "systemctl",
        "journalctl",
        "mount",
        "umount",
        "dd",
        "mkfs",
        "ssh",
        "scp",
        "rsync",
        "curl",
        "wget",
        "nc",
        "docker",
        "podman",
        "kubectl",
        "helm",
        "terraform",
        "aws",
        "gcloud",
        "az",
        "npm",
        "pip",
        "uv",
        "cargo",
        "bash",
        "sh",
        "zsh",
        "python",
        "python3",
        "node",
        "make",
        "eval",
        "exec",
        "env",
        "xargs",
    }
)

_GH_READ_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "pr": frozenset({"view", "list", "checks", "diff", "status"}),
    "issue": frozenset({"view", "list", "status"}),
    "repo": frozenset({"view"}),
    "run": frozenset({"view", "list"}),
    "release": frozenset({"view", "list"}),
    "auth": frozenset(),  # nothing under `gh auth` is a plain read we will cache
}

# Local file operations that stay local as long as every path is in the worktree.
_LOCAL_WRITE_PROGRAMS: dict[str, frozenset[str]] = {
    "mkdir": frozenset(),
    "touch": frozenset(),
    "cp": frozenset({"-r", "-R", "--recursive"}),
    "mv": frozenset(),
}

# Argument values that provably cannot change a command's effect, so two
# otherwise identical commands differing only here share a key. Keyed by
# (program, subcommand or "", flag). Nothing is inert unless it is listed.
_INERT_FLAG_VALUES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("git", "commit", "-m"),
        ("git", "commit", "--message"),
        ("git", "tag", "-m"),
        ("git", "tag", "--message"),
        ("git", "stash", "-m"),
        ("git", "stash", "--message"),
    }
)

_INERT = "<text>"


def _looks_like_path(token: str, *, cwd: Path) -> bool:
    """Is this operand something we must resolve before we dare cache it?

    Deliberately generous: a false positive costs a cache miss, while a false
    negative would let an out-of-tree path ride along as an opaque literal. A
    bare name is included when it actually exists, so ``git add src`` records the
    directory it touches instead of passing ``src`` through as a literal.
    """
    if "/" in token or token.startswith("~") or "$" in token or token in (".", ".."):
        return True
    try:
        return (cwd / token).exists()
    except OSError:
        return False


def _resolve_in_worktree(token: str, *, cwd: Path, worktree: Path) -> str:
    """Return the worktree-relative form of ``token`` or fail closed."""
    if "$" in token or "~" in token:
        # Unexpanded by us and already expanded or not by the shell we never saw.
        raise _Unclassifiable("path contains an unresolved expansion")
    candidate = Path(token)
    resolved = candidate if candidate.is_absolute() else cwd / candidate
    normalized = Path(_normalize(resolved))
    root = Path(_normalize(worktree))
    try:
        relative = normalized.relative_to(root)
    except ValueError as exc:
        raise _Unclassifiable("path is outside the worktree") from exc
    # The lexical check above is not enough on its own: a path that spells out as
    # in-worktree can still be a symlink, or sit under one, whose real target is
    # somewhere else entirely, and the command would follow the link. Resolve
    # both sides for real and require containment again. `realpath` resolves the
    # ancestors that exist and leaves the rest alone, so a file about to be
    # created still gets its parent directories checked.
    try:
        real_target = os.path.realpath(normalized)
        real_root = os.path.realpath(root)
    except OSError as exc:
        raise _Unclassifiable("path could not be resolved") from exc
    if real_target != real_root and not real_target.startswith(real_root + os.sep):
        raise _Unclassifiable("path resolves outside the worktree through a link")
    return str(PurePosixPath(relative)) or "."


def _normalize(path: Path) -> str:
    """Lexically normalize without touching the filesystem.

    ``Path.resolve`` would follow symlinks and hide a traversal behind a link the
    guardian would have wanted to see, so ``..`` is collapsed by hand instead.
    """
    parts: list[str] = []
    for part in path.parts:
        if part == ".":
            continue
        if part == "..":
            if not parts or parts[-1] in ("", "/"):
                raise _Unclassifiable("path escapes above the filesystem root")
            parts.pop()
            continue
        parts.append(part)
    return str(Path(*parts)) if parts else "/"


def _is_flag(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _classify_git(argv: Sequence[str]) -> str:
    operands = [token for token in argv[1:] if not _is_flag(token)]
    subcommand = operands[0] if operands else None
    if subcommand is None:
        raise _Unclassifiable("git with no subcommand")
    flags = {token.split("=", 1)[0] for token in argv if _is_flag(token)}
    # Second level before first: `git stash drop` must not be read as the
    # ordinary local write `git stash`, and the check has to come before the
    # remote and local-write tables for `git remote remove` to reach it too.
    second_level = operands[1] if len(operands) > 1 else None
    if second_level in _GIT_DESTRUCTIVE_SECOND_LEVEL.get(subcommand, frozenset()):
        return SENSITIVE
    if subcommand in _GIT_REMOTE_SUBCOMMANDS:
        return REMOTE_MUTATION
    if flags & _GIT_DESTRUCTIVE_FLAGS:
        return SENSITIVE
    if subcommand in _GIT_READ_SUBCOMMANDS:
        return READ
    if subcommand in _GIT_LOCAL_WRITE_SUBCOMMANDS:
        # `git clean` is not in the table at all; `reset` without --hard is.
        return LOCAL_WRITE
    raise _Unclassifiable(f"unknown git subcommand {subcommand!r}")


def _classify_gh(argv: Sequence[str]) -> str:
    operands = [token for token in argv[1:] if not _is_flag(token)]
    if len(operands) < 2:
        raise _Unclassifiable("gh with no subcommand pair")
    group, action = operands[0], operands[1]
    if group == "api":
        raise _Unclassifiable("gh api effect depends on flags we will not guess")
    if action in _GH_READ_SUBCOMMANDS.get(group, frozenset()):
        return READ
    return REMOTE_MUTATION


def _classify(argv: Sequence[str]) -> str:
    program = PurePosixPath(argv[0]).name
    if program in _SENSITIVE_PROGRAMS:
        return SENSITIVE
    if program == "git":
        return _classify_git(argv)
    if program == "gh":
        return _classify_gh(argv)
    if program in _READ_PROGRAMS:
        unsafe = _READ_PROGRAMS[program]
        if any(token.split("=", 1)[0] in unsafe for token in argv[1:]):
            raise _Unclassifiable(f"{program} used with an effectful flag")
        return READ
    if program in _LOCAL_WRITE_PROGRAMS:
        unsafe = _LOCAL_WRITE_PROGRAMS[program]
        if any(token.split("=", 1)[0] in unsafe for token in argv[1:]):
            raise _Unclassifiable(f"{program} used with a flag we will not reason about")
        return LOCAL_WRITE
    raise _Unclassifiable(f"unknown program {program!r}")


def _normalize_argv(
    argv: Sequence[str], *, cwd: Path, worktree: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the cache shape and the worktree-relative paths it names."""
    program = PurePosixPath(argv[0]).name
    subcommand = next((token for token in argv[1:] if not _is_flag(token)), "") or ""
    shape: list[str] = [program]
    paths: set[str] = set()
    inert_next = False
    for token in argv[1:]:
        if inert_next:
            shape.append(_INERT)
            inert_next = False
            continue
        if _is_flag(token):
            flag, separator, value = token.partition("=")
            if separator and (program, subcommand, flag) in _INERT_FLAG_VALUES:
                shape.append(f"{flag}={_INERT}")
                continue
            if not separator and (program, subcommand, flag) in _INERT_FLAG_VALUES:
                inert_next = True
            shape.append(token)
            continue
        if _looks_like_path(token, cwd=cwd):
            relative = _resolve_in_worktree(token, cwd=cwd, worktree=worktree)
            paths.add(relative)
            shape.append(f"<path:{relative}>")
            continue
        shape.append(token)
    if inert_next:
        raise _Unclassifiable("flag expecting a value had none")
    return tuple(shape), tuple(sorted(paths))


def capability_key(
    command: str,
    *,
    cwd: str | Path,
    scope: WorkerScope,
) -> CapabilityKey | None:
    """Normalize one escalating command into a cache key, or ``None`` to ask.

    ``None`` is returned for every command this module cannot positively account
    for, and for every command whose class is not in :data:`CACHEABLE_CLASSES`.
    Callers must treat ``None`` as "run the full adjudication".
    """
    if not command or not command.strip():
        return None
    if any(character in _SHELL_METACHARACTERS for character in command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    worktree = Path(scope.worktree)
    try:
        relative_cwd = _resolve_in_worktree(str(cwd), cwd=worktree, worktree=worktree)
        action_class = _classify(argv)
        if action_class not in CACHEABLE_CLASSES:
            return None
        shape, paths = _normalize_argv(argv, cwd=Path(cwd), worktree=worktree)
    except _Unclassifiable:
        return None
    return CapabilityKey(
        worker=scope.worker,
        repository=scope.repository,
        worktree=str(worktree),
        policy_fingerprint=scope.policy_fingerprint,
        action_class=action_class,
        cwd=relative_cwd,
        shape=shape,
        path_scope=paths,
    )


def file_change_key(grant_root: str | Path | None, *, scope: WorkerScope) -> CapabilityKey | None:
    """Key a file-change escalation, which names a root rather than a command.

    Only a root inside the worktree is cacheable; a patch that wants to write
    somewhere else is a different capability and goes to the guardian.
    """
    if grant_root is None:
        return None
    worktree = Path(scope.worktree)
    try:
        relative = _resolve_in_worktree(str(grant_root), cwd=worktree, worktree=worktree)
    except _Unclassifiable:
        return None
    return CapabilityKey(
        worker=scope.worker,
        repository=scope.repository,
        worktree=str(worktree),
        policy_fingerprint=scope.policy_fingerprint,
        action_class=LOCAL_WRITE,
        cwd=relative,
        shape=("<file-change>",),
        path_scope=(relative,),
    )


def resolve_scope(worker: str, cwd: str | Path) -> WorkerScope | None:
    """Derive the enforceable scope for a request, or ``None`` to fail closed.

    Filesystem reads only — no ``git`` subprocess — because this runs inside a
    per-request hook where a process spawn per escalation would eat the saving
    the cache exists to make. The worktree is the nearest ancestor holding a
    ``.git``; the repository identity is the common git directory, so two
    worktrees of one repository share it while two clones do not.
    """
    if not worker:
        return None
    try:
        start = Path(_normalize(Path(cwd)))
    except _Unclassifiable:
        return None
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            if not (marker / "HEAD").exists():
                # An empty `.git` directory is litter, not a repository, and
                # treating it as the worktree root would scope grants to a
                # boundary git itself would not recognise.
                continue
            return WorkerScope(
                worker=worker,
                repository=str(marker),
                worktree=str(candidate),
                policy_fingerprint=policy_fingerprint(candidate),
            )
        if marker.is_file():
            # A linked worktree: `.git` holds `gitdir: <repo>/.git/worktrees/<name>`.
            try:
                pointer = marker.read_text().strip()
            except OSError:
                return None
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = Path(pointer.split(":", 1)[1].strip())
            if gitdir.parent.name != "worktrees":
                return None
            return WorkerScope(
                worker=worker,
                repository=str(gitdir.parent.parent),
                worktree=str(candidate),
                policy_fingerprint=policy_fingerprint(candidate),
            )
    return None


class ApprovalCache:
    """Grants and denials for one runtime, looked up by exact key only."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        ttl_s: float = DEFAULT_TTL_S,
        max_grants: int = DEFAULT_MAX_GRANTS,
    ) -> None:
        self._clock = clock
        self._ttl_s = ttl_s
        self._max_grants = max_grants
        self._grants: dict[CapabilityKey, Decision] = {}
        self._denials: dict[CapabilityKey, Decision] = {}
        self._hits: list[CacheHit] = []

    def record(self, key: CapabilityKey | None, *, outcome: str, approval_id: str) -> None:
        """Remember one real adjudication.

        A denial is recorded even for an unclassifiable command's key of ``None``
        being absent — there is simply nothing to remember then. A denial that
        *does* have a key is remembered permanently for that key, so a later
        approval of something adjacent cannot quietly re-enable it.
        """
        if key is None:
            return
        now = self._clock()
        if outcome == DENY:
            self._denials[key] = Decision(key, DENY, approval_id, now)
            self._grants.pop(key, None)
            return
        if outcome != ALLOW:
            return
        if key.action_class not in CACHEABLE_CLASSES:
            return
        if key in self._denials:
            # The same capability was refused before. Re-deciding it is the
            # guardian's job every time; we do not get to upgrade it here.
            return
        self._prune_stale_policy(key)
        self._grants[key] = Decision(key, ALLOW, approval_id, now)
        self._evict()

    def lookup(self, key: CapabilityKey | None) -> CacheHit | None:
        """Return an auditable hit, or ``None`` to run the full adjudication."""
        if key is None:
            return None
        if key.action_class not in CACHEABLE_CLASSES:
            return None
        if key in self._denials:
            return None
        grant = self._grants.get(key)
        if grant is None:
            return None
        now = self._clock()
        if now - grant.decided_at > self._ttl_s:
            del self._grants[key]
            return None
        hit = CacheHit(
            key=key,
            origin_approval_id=grant.approval_id,
            origin_decided_at=grant.decided_at,
            matched_at=now,
            reason=(
                "same worker, repository, worktree, policy fingerprint, action class, "
                "normalized command and path scope as the approval it reuses"
            ),
        )
        self._hits.append(hit)
        del self._hits[:-200]
        return hit

    def hits(self) -> tuple[CacheHit, ...]:
        """Recent hits, newest last, for the audit surface."""
        return tuple(self._hits)

    def forget_worker(self, worker: str) -> None:
        """Drop everything a finished worker established."""
        for store in (self._grants, self._denials):
            for key in [key for key in store if key.worker == worker]:
                del store[key]

    def _prune_stale_policy(self, key: CapabilityKey) -> None:
        """Drop grants for this worktree taken under a different policy."""
        stale = [
            existing
            for existing in self._grants
            if existing.worktree == key.worktree
            and existing.repository == key.repository
            and existing.policy_fingerprint != key.policy_fingerprint
        ]
        for existing in stale:
            del self._grants[existing]

    def _evict(self) -> None:
        if len(self._grants) <= self._max_grants:
            return
        oldest: Iterable[CapabilityKey] = sorted(
            self._grants, key=lambda key: self._grants[key].decided_at
        )
        for key in list(oldest)[: len(self._grants) - self._max_grants]:
            del self._grants[key]


# ---------------------------------------------------------------------------
# Durable ledger
#
# The hook that consults this cache is a short-lived process spawned by Codex,
# so the cache cannot live in the runtime's memory. It is a small JSON file held
# under `flock` for the whole read-modify-write, which also makes the audit trail
# durable rather than something only a running process could show you.
# ---------------------------------------------------------------------------

LEDGER_PATH_ENV = "AGENTDECK_APPROVAL_CACHE"
LEDGER_VERSION = 1

#: How long a deferred request stays claimable by the execution that proves it
#: was approved. Long enough for a guardian to think, short enough that a much
#: later identical command cannot harvest a grant from an abandoned turn.
PENDING_TTL_S = 900.0


def _key_to_json(key: CapabilityKey) -> dict[str, object]:
    return asdict(key)


def _key_from_json(raw: object) -> CapabilityKey | None:
    if not isinstance(raw, dict):
        return None
    try:
        return CapabilityKey(
            worker=str(raw["worker"]),
            repository=str(raw["repository"]),
            worktree=str(raw["worktree"]),
            policy_fingerprint=str(raw["policy_fingerprint"]),
            action_class=str(raw["action_class"]),
            cwd=str(raw["cwd"]),
            shape=tuple(str(item) for item in raw["shape"]),
            path_scope=tuple(str(item) for item in raw["path_scope"]),
        )
    except (KeyError, TypeError):
        return None


class CapabilityLedger:
    """The on-disk form of :class:`ApprovalCache`, plus the deferred-request book.

    A grant is never written by adjudicating anything here. It is written only
    when AgentDeck observes the two halves of a real approval it deferred: a
    ``PermissionRequest`` this cache declined to answer, followed by the matching
    tool actually running. A command the guardian denied never runs, so a denial
    cannot become a grant by this route — which is the structural version of the
    issue's "a denial is never converted into a reusable approval".
    """

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], float] = time.time,
        ttl_s: float = DEFAULT_TTL_S,
        pending_ttl_s: float = PENDING_TTL_S,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._ttl_s = ttl_s
        self._pending_ttl_s = pending_ttl_s

    @classmethod
    def from_env(
        cls, environ: dict[str, str] | None = None, **kwargs: object
    ) -> CapabilityLedger | None:
        """Build the ledger the hook was told to use, or ``None`` if unconfigured."""
        source = os.environ if environ is None else environ
        configured = source.get(LEDGER_PATH_ENV)
        if not configured:
            return None
        return cls(Path(configured), **kwargs)  # type: ignore[arg-type]

    @contextmanager
    def _locked(self):
        """Hold the file exclusively across the whole read-modify-write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            raw = os.read(descriptor, 8 * 1024 * 1024)
            try:
                state = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                # A truncated ledger is a cache, not a source of truth. Losing it
                # costs guardian calls; trusting half of it could cost more.
                state = {}
            if not isinstance(state, dict) or state.get("version") != LEDGER_VERSION:
                state = {}
            state.setdefault("version", LEDGER_VERSION)
            state.setdefault("grants", [])
            state.setdefault("pending", {})
            state.setdefault("hits", [])
            yield state
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, json.dumps(state, separators=(",", ":")).encode())
        finally:
            os.close(descriptor)

    def _live_grants(self, state: dict, now: float) -> dict[CapabilityKey, Decision]:
        grants: dict[CapabilityKey, Decision] = {}
        for entry in state.get("grants", []):
            if not isinstance(entry, dict):
                continue
            key = _key_from_json(entry.get("key"))
            decided_at = entry.get("decided_at")
            if key is None or not isinstance(decided_at, (int, float)):
                continue
            if now - decided_at > self._ttl_s:
                continue
            grants[key] = Decision(key, ALLOW, str(entry.get("approval_id") or ""), decided_at)
        return grants

    @staticmethod
    def _store_grants(state: dict, grants: dict[CapabilityKey, Decision]) -> None:
        state["grants"] = [
            {
                "key": _key_to_json(key),
                "approval_id": decision.approval_id,
                "decided_at": decision.decided_at,
            }
            for key, decision in grants.items()
        ]

    def consult(self, key: CapabilityKey | None, *, request_id: str) -> CacheHit | None:
        """Answer one ``PermissionRequest``: a hit, or ``None`` meaning "ask".

        A miss with a usable key is remembered as deferred under ``request_id``,
        so that if the guardian goes on to allow it and the tool runs, the
        execution can be recognised as this request's outcome.
        """
        if key is None:
            return None
        now = self._clock()
        with self._locked() as state:
            grants = self._live_grants(state, now)
            grant = grants.get(key)
            if grant is not None and key.action_class in CACHEABLE_CLASSES:
                hit = CacheHit(
                    key=key,
                    origin_approval_id=grant.approval_id,
                    origin_decided_at=grant.decided_at,
                    matched_at=now,
                    reason=(
                        "same worker, repository, worktree, policy fingerprint, action "
                        "class, normalized command and path scope as the approval it reuses"
                    ),
                )
                hits = [entry for entry in state.get("hits", []) if isinstance(entry, dict)]
                hits.append(
                    {
                        "at": now,
                        "request_id": request_id,
                        "origin_approval_id": hit.origin_approval_id,
                        "capability": key.describe(),
                        "key": _key_to_json(key),
                        "reason": hit.reason,
                    }
                )
                state["hits"] = hits[-200:]
                self._store_grants(state, grants)
                return hit
            pending = {
                token: entry
                for token, entry in state.get("pending", {}).items()
                if isinstance(entry, dict)
                and isinstance(entry.get("asked_at"), (int, float))
                and now - entry["asked_at"] <= self._pending_ttl_s
            }
            pending[request_id] = {"key": _key_to_json(key), "asked_at": now}
            state["pending"] = pending
            self._store_grants(state, grants)
            return None

    def observe_execution(self, request_id: str) -> CapabilityKey | None:
        """Turn a deferred request into a grant because its tool actually ran.

        Returns the granted key, or ``None`` when this execution was not one this
        cache deferred — an unescalated command, a second run, or a stale turn.
        """
        now = self._clock()
        with self._locked() as state:
            pending = state.get("pending", {})
            entry = pending.pop(request_id, None) if isinstance(pending, dict) else None
            if not isinstance(entry, dict):
                return None
            key = _key_from_json(entry.get("key"))
            asked_at = entry.get("asked_at")
            if key is None or not isinstance(asked_at, (int, float)):
                return None
            if now - asked_at > self._pending_ttl_s:
                return None
            if key.action_class not in CACHEABLE_CLASSES:
                return None
            grants = self._live_grants(state, now)
            # A policy edit between the request and the execution must not be
            # papered over, and neither must a stale grant under the old one.
            for existing in [
                candidate
                for candidate in grants
                if candidate.worktree == key.worktree
                and candidate.repository == key.repository
                and candidate.policy_fingerprint != key.policy_fingerprint
            ]:
                del grants[existing]
            grants[key] = Decision(key, ALLOW, request_id, now)
            if len(grants) > DEFAULT_MAX_GRANTS:
                for stale in sorted(grants, key=lambda item: grants[item].decided_at)[
                    : len(grants) - DEFAULT_MAX_GRANTS
                ]:
                    del grants[stale]
            self._store_grants(state, grants)
            state["pending"] = pending
            return key

    def audit(self) -> tuple[dict, ...]:
        """Recent cache hits, newest last, each naming the approval it reused."""
        try:
            raw = self.path.read_text()
        except OSError:
            return ()
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(state, dict):
            return ()
        return tuple(entry for entry in state.get("hits", []) if isinstance(entry, dict))
