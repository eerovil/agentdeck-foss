"""Bring a project's shared code checkouts up to GitHub's default branch (#4233).

A Work's own worktree starts from GitHub's branch, but its agent also reads the project's
other code in the shared main checkouts a Poller overlay names. Those drift: one was left
detached 35 commits behind, others on a feature branch holding nothing master lacks, and an
agent read the old code as current. The owner wants every Poller project's checkouts on
master, refreshed whenever a new Work starts, so each start moves every one it safely can
to its default branch at ``origin``'s tip.

Safely means nothing is lost or disturbed: a checkout with uncommitted tracked changes, with
commits its default branch on ``origin`` lacks, or mid-merge or rebase stays where it is,
and so does an AgentDeck checkout, whose own deploy timer moves it (moving it here would
make that timer think it had already deployed). Runs Git, so callers keep it off the event
loop; every step is bounded by a timeout, and nothing here raises.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

#: One Git step's limit; a fetch over a slow link is the longest.
GIT_TIMEOUT_S = 60.0

#: A folder only an AgentDeck checkout holds: its deploy timer moves it, never this. A folder,
#: not a script's name, so `scripts/select_suite.py` does not read this module as running
#: that script (which would tie the poller engine's tests to every Work start test).
DECK_MARKER = Path("src", "agentdeck")

#: Checkouts being refreshed now, so two Work starts never move one at once.
_BUSY: set[str] = set()
_BUSY_LOCK = threading.Lock()


def _git(path: str, *args: str) -> tuple[int, str]:
    # The deck's GitHub rule for host-side git (#2289), with no password prompt (#3632): a
    # refused fetch must fail here, not open a box on the host's screen at every Work start.
    from .github_identity import github_git_environment

    try:
        done = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=github_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout if done.returncode == 0 else done.stderr).strip()


def refresh_checkout(raw: str) -> str:
    """Move one main checkout to its default branch at ``origin``'s tip; what happened.

    ``updated``, ``current``, or why it was left alone (``dirty``, ``own commits`` ...).
    """
    try:
        path = Path(raw).expanduser().resolve()
        git_dir = path / ".git"
        if not git_dir.is_dir():
            return "not a main checkout"
        if (path / DECK_MARKER).is_dir():
            return "an AgentDeck checkout"
        busy = any(
            (git_dir / name).exists() for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply")
        )
    except (OSError, RuntimeError):
        return "unreadable"
    if busy:
        return "mid-merge"
    where = str(path)
    with _BUSY_LOCK:
        if where in _BUSY:
            return "already refreshing"
        _BUSY.add(where)
    try:
        return _refresh(where)
    finally:
        with _BUSY_LOCK:
            _BUSY.discard(where)


def _refresh(path: str) -> str:
    code, ref = _git(path, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code or not ref.startswith("origin/"):
        return "no default branch"
    base = ref.removeprefix("origin/")
    code, why = _git(path, "fetch", "--quiet", "origin", base)
    if code:
        return f"fetch failed: {why}"
    code, changed = _git(path, "status", "--porcelain", "--untracked-files=no")
    if code or changed:
        return "dirty"
    code, own = _git(path, "rev-list", "--count", f"{ref}..HEAD")
    if code or own != "0":
        return "own commits"
    code, branch = _git(path, "branch", "--show-current")
    if code:
        return "unreadable"
    if branch != base:
        code, local = _git(path, "rev-list", "--count", f"{ref}..refs/heads/{base}")
        if code == 0 and local != "0":
            return f"local {base} has own commits"
        # ``-C`` puts the local branch at origin's tip: it held nothing origin lacks.
        code, why = _git(path, "switch", "--quiet", "-C", base, "--track", ref)
        return "updated" if code == 0 else f"switch failed: {why}"
    code, behind = _git(path, "rev-list", "--count", f"HEAD..{ref}")
    if code == 0 and behind == "0":
        return "current"
    code, why = _git(path, "merge", "--quiet", "--ff-only", ref)
    return "updated" if code == 0 else f"fast-forward failed: {why}"


def refresh_checkouts(paths: Iterable[str]) -> dict[str, str]:
    """Refresh each checkout once, in order; what happened to each, logged."""
    results: dict[str, str] = {}
    for raw in dict.fromkeys(path for path in paths if path):
        results[raw] = outcome = refresh_checkout(raw)
        log.info("checkout refresh: %s %s", raw, outcome)
    return results
