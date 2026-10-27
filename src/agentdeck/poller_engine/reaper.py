"""Find and release host worktrees no Work claims (#1094).

Cleanup everywhere else runs from the *record* side: the runtime removes a worktree only
because a Work claimed that exact path and the Work has ended (#2149, #2195). That is the
right rule for a wake, because guessing a path from a naming convention is how an engine
deletes a directory it never created. Its cost is that a directory nothing ever claimed —
left by a pre-claims card, a refused registration, or a person — has no owner, and nothing
looks at it again. They only accumulate.

This module is the other direction, as a command rather than a wake: walk what is
actually on disk under each configured checkout's ``.worktrees``, subtract every path a Work
still claims, and judge the remainder. It is deliberately not wired into a
wake. Deleting a worker's unfinished work is a failure this project has already paid for.
It began operator-only, with a person reading the list first; since #3394 a daily timer
(``scripts/reap_all_worktrees.sh``) applies it to every instance, because worktrees filled
the disk, so the rules below are the whole of the safety.

Two rules carry that safety:

* **Paths are compared resolved, never as strings** (``paths.resolve``). On an ostree host
  ``/home`` is a symlink to ``var/home``, and the same directory can be recorded both
  spellings in one ``state.json`` (#1095). A string comparison would read a live, tracked
  worktree as an orphan and delete a worker's uncommitted work. Records are now written
  resolved too, but this side never relies on that: a state file written by an older engine,
  or a candidate path spelled differently by the filesystem walk, must still compare equal.
* **Every unknown keeps the directory.** A fact that cannot be read — an unreadable status,
  a checkout with no base branch, a missing timestamp — is never treated as permission. Only
  a candidate positively proved unclaimed, old, clean and fully merged is reapable.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .github import registered_worktree
from .paths import resolve

# How old a directory must be before it is even considered. A card lives for hours, so days
# of quiet is a generous floor: it costs nothing to leave a recent worktree alone for another
# run, and it is the difference between reaping a forgotten card and racing a live one.
DEFAULT_MIN_AGE_DAYS = 7

# Why a candidate was kept. These are shown to an operator verbatim, so they read as reasons
# rather than codes.
CLAIMED = "claimed"
UNREGISTERED = "unregistered"
TOO_NEW = "too-new"
DIRTY = "dirty"
UNMERGED = "unmerged"
UNREADABLE = "unreadable"
# ...and the one verdict that releases anything.
ORPHAN = "orphan"

# Remote-tracking branches to try when a checkout has no ``origin/HEAD`` to ask.
_BASE_FALLBACKS = ("origin/master", "origin/main")

_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Candidate:
    """One directory found under a checkout's ``.worktrees``, and what could be read about it.

    Every optional fact is ``None`` when it could not be read rather than a guessed default,
    because the judgement below turns any unknown into "keep".
    """

    path: str
    checkout: str
    registered: bool
    age_days: float | None = None
    dirty: int | None = None
    merged: bool | None = None
    branch: str = ""


@dataclass(frozen=True)
class Verdict:
    """What the reaper decided about one candidate, and in one word why."""

    candidate: Candidate
    reap: bool
    reason: str

    @property
    def path(self) -> str:
        return self.candidate.path


def plan(
    candidates: Iterable[Candidate],
    claimed: Iterable[str],
    *,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> list[Verdict]:
    """Judge every candidate against the claimed set. Pure: no disk, no Git, no clock."""
    owned = {resolve(path) for path in claimed}
    return [Verdict(item, *_judge(item, owned, min_age_days)) for item in candidates]


def _judge(item: Candidate, owned: set[str], min_age_days: float) -> tuple[bool, str]:
    """The order is the safety argument: ownership first, then every way of not knowing."""
    if resolve(item.path) in owned:
        return False, CLAIMED
    # Not a worktree this checkout's Git registers. The engine has no validated way to remove
    # it, and a bare `rm -rf` from a poller is not something this module is willing to own.
    if not item.registered:
        return False, UNREGISTERED
    if item.age_days is None or item.dirty is None or item.merged is None:
        return False, UNREADABLE
    if item.age_days < min_age_days:
        return False, TOO_NEW
    # Uncommitted work is somebody's unfinished card, whatever the records say about it.
    if item.dirty > 0:
        return False, DIRTY
    if not item.merged:
        return False, UNMERGED
    return True, ORPHAN


def scan(
    checkouts: Iterable[str],
    *,
    now: datetime,
    runner=subprocess.run,
) -> list[Candidate]:
    """Read every ``.worktrees`` child of every configured checkout, and what Git says about it.

    A checkout with no ``.worktrees`` contributes nothing; an unreadable one contributes
    nothing rather than failing the sweep, because one bad checkout must not hide the other
    four from the operator.
    """
    found: list[Candidate] = []
    for checkout in checkouts:
        root = Path(str(checkout)).expanduser() / ".worktrees"
        try:
            entries = sorted(entry for entry in root.iterdir() if entry.is_dir())
        except OSError:
            continue
        base = _base_ref(root.parent, runner)
        for entry in entries:
            found.append(_inspect(entry, root.parent, base, now=now, runner=runner))
    return found


def _inspect(path: Path, checkout: Path, base: str, *, now: datetime, runner) -> Candidate:
    target = str(path)
    repo, present = registered_worktree(target, runner=runner)
    if repo is None or not present:
        return Candidate(path=target, checkout=str(checkout), registered=False)
    return Candidate(
        path=target,
        checkout=str(checkout),
        registered=True,
        age_days=_age_days(path, now=now),
        dirty=_dirty_count(target, runner),
        merged=_merged(repo, target, base, runner),
        branch=_branch(target, runner),
    )


def _git(runner, *args: str, cwd: str) -> tuple[int, str]:
    """One bounded, non-raising Git read. Returns ``(-1, "")`` when it could not run at all."""
    try:
        done = runner(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return done.returncode, done.stdout


def _age_days(path: Path, *, now: datetime) -> float | None:
    """Days since the directory last showed any sign of life.

    The *newest* of the directory's own timestamp and its ``.git`` link is used, so anything
    recently touched reads as young. That is the conservative direction: a worktree looking
    younger than it is gets kept for another run, while one looking older than it is would be
    reaped out from under whoever touched it.
    """
    newest: float | None = None
    for entry in (path, path / ".git"):
        try:
            stamp = entry.stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    if newest is None:
        return None
    return max(0.0, (now.timestamp() - newest) / _SECONDS_PER_DAY)


def _dirty_count(target: str, runner) -> int | None:
    """Uncommitted and untracked files in the worktree; ``None`` when the status is unreadable."""
    code, out = _git(runner, "status", "--porcelain", cwd=target)
    if code != 0:
        return None
    return sum(1 for line in out.splitlines() if line.strip())


def _branch(target: str, runner) -> str:
    code, out = _git(runner, "rev-parse", "--abbrev-ref", "HEAD", cwd=target)
    return out.strip() if code == 0 else ""


def _base_ref(checkout: Path, runner) -> str:
    """The checkout's base branch, as a remote-tracking ref, or ``""`` when there is none.

    No fetch happens here — a reaper has no business reaching the network. A stale
    remote-tracking ref therefore makes recent work look unmerged, which keeps the directory.
    """
    root = str(checkout)
    code, out = _git(runner, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=root)
    if code == 0 and out.strip():
        return out.strip().removeprefix("refs/remotes/")
    for candidate in _BASE_FALLBACKS:
        code, _out = _git(runner, "rev-parse", "--verify", "--quiet", candidate, cwd=root)
        if code == 0:
            return candidate
    return ""


def _merged(repo: Path, target: str, base: str, runner) -> bool | None:
    """Is everything this worktree holds already contained in the checkout's base branch?

    This single question stands in for "the branch is merged or gone". A worktree whose HEAD
    is an ancestor of the base has nothing that would be lost — whether its branch was merged
    and deleted, or never had a commit of its own. Anything else, including a base branch that
    cannot be resolved, is unknown and keeps the directory.
    """
    if not base:
        return None
    code, out = _git(runner, "rev-parse", "HEAD", cwd=target)
    head = out.strip()
    if code != 0 or not head:
        return None
    code, _out = _git(runner, "merge-base", "--is-ancestor", head, base, cwd=str(repo))
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def directory_size(path: str) -> int | None:
    """Bytes held under one worktree, or ``None`` when the walk fails.

    This is what an operator actually wants to know before approving a sweep, and it is the
    only reason to walk a candidate's contents at all — so it stays opt-in at the command
    line rather than being paid on every scan.
    """
    total = 0
    try:
        for parent, _dirs, files in os.walk(path, onerror=_raise):
            for name in files:
                try:
                    total += os.lstat(os.path.join(parent, name)).st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _raise(error: OSError) -> None:
    raise error
