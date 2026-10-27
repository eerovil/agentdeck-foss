"""How the web process invokes the ``gh`` CLI.

Every ``gh`` call names the repository or owner it is about — read off the argv
it already wrote (``--repo``, an ``api repos/owner/name/...`` path, a URL), or
passed as ``repo=`` / ``owner=`` — and runs as exactly the non-human authority
``github_identity`` picks for that owner (#2289). A call that names no owner, or
an owner with no authority, is refused with ``GitHubRefusal`` before anything
runs. There is no second attempt as anybody else: the old retry on the host's
own ``gh`` login spoke as a person, which is the one thing this must never do.

One module owns this because one caller solving a credential condition privately
leaves every other caller to rediscover it (#544).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

from . import github_identity, github_suspension

log = logging.getLogger(__name__)

# Spawning is not free, and ``asyncio.create_subprocess_exec`` does the fork and
# the pipe setup inline on the event-loop thread. Against a web process holding
# a few thousand sessions that showed up as real event-loop time under Deckhand's
# git and PR lookups, on the one thread that also serves every SSE tick (#1310).
#
# Its own pool rather than the default executor: the provider scans already run
# their file reads through ``asyncio.to_thread``, and a burst of ``gh`` lookups
# sharing that pool would queue a scan behind network calls. Small on purpose —
# ``git_context`` already caps its own fan-out at six, and the point is to move
# the spawn off the loop, not to run more of them at once.
_SPAWN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gh-spawn")

# Each distinct refusal is logged once per process: Deckhand re-reads the same card
# on every render, and a WARNING per render would bury everything else.
_REFUSALS_LOGGED: set[str] = set()


class GitHubRefusal(OSError):  # noqa: N818 — callers already treat OSError as "gh unavailable"
    """No non-human authority may make this ``gh`` call; ``str()`` says why."""


def _environment(
    argv: Sequence[str], repo: object, owner: str | None
) -> tuple[dict[str, str], github_identity.Authority | None]:
    """The child environment for ``argv``, and the authority it speaks as.

    Raises ``GitHubRefusal``. Blocking — minting an App installation token is a
    network call — so the async entry points run it on the spawn pool, never on the
    event loop.

    ``git`` keeps this process's environment minus any credential variable, with
    AgentDeck's helper as its only GitHub credential (#2289); it names no authority.
    """
    parts = [str(part) for part in argv]
    if not parts or os.path.basename(parts[0]) not in ("gh", "gh.exe"):
        environment = dict(os.environ)
        github_identity.configure_github_git(environment)
        return environment, None
    if repo is None and not owner:
        owner = github_identity.owner_in_argv(parts)
    authority = None
    try:
        authority = github_identity.authority_for(repo, owner=owner)
        return github_identity.environment_for(repo, owner=owner), authority
    except github_identity.Refusal as refusal:
        # A suspended App installation is refused its token before `gh` ever runs.
        github_suspension.observe(authority, ok=False, text=str(refusal))
        shown = " ".join(parts[1:3])
        reason = str(refusal)
        if reason.startswith("no target owner"):
            reason = f"no target owner for gh {shown}"
        if reason not in _REFUSALS_LOGGED:
            _REFUSALS_LOGGED.add(reason)
            log.warning("github: %s", reason)
        raise GitHubRefusal(f"github: {reason}") from refusal


async def _environment_off_loop(
    argv: Sequence[str], repo: object, owner: str | None
) -> tuple[dict[str, str], github_identity.Authority | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SPAWN_POOL, lambda: _environment(argv, repo, owner))


def _spawn(
    argv: Sequence[str],
    environment: dict[str, str] | None,
    timeout: float,
    *,
    stdin: bytes | None,
    keep_stderr: bool,
) -> subprocess.CompletedProcess[bytes]:
    """Run one command to completion, off the event loop. Blocking on purpose."""
    return subprocess.run(  # noqa: S603 — argv is built by callers, never a shell
        list(argv),
        input=stdin if stdin is not None else b"",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if keep_stderr else subprocess.DEVNULL,
        env=environment,
        timeout=timeout,
        check=False,
    )


async def _run_off_loop(
    argv: Sequence[str],
    environment: dict[str, str] | None,
    timeout: float,
    *,
    stdin: bytes | None = None,
    keep_stderr: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            _SPAWN_POOL,
            lambda: _spawn(
                argv, environment, timeout, stdin=stdin, keep_stderr=keep_stderr
            ),
        )
    except subprocess.TimeoutExpired as exc:
        # The callers were written against ``asyncio.wait_for``; keep raising
        # what they already handle. ``subprocess.run`` has killed the child.
        raise TimeoutError(str(exc)) from exc


def _observe(
    authority: github_identity.Authority | None, done: subprocess.CompletedProcess[bytes]
) -> None:
    """Tell ``github_suspension`` how this call went, so a suspended account is named
    on the deck rather than surfacing as unrelated read failures (#2682)."""
    github_suspension.observe(authority, ok=not done.returncode, text=done.stderr)


async def run_once(
    argv: Sequence[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    repo: object = None,
    owner: str | None = None,
) -> tuple[int, bytes, bytes]:
    """Run one command exactly once, keeping stderr. Every write goes through here, and so
    does a read that needs to see why it failed (`review_refs.target_digest`, #2478).

    A non-zero exit does not prove nothing landed, so a write is never retried.
    Raises ``GitHubRefusal`` when no authority may make the call.
    """
    environment, authority = await _environment_off_loop(argv, repo, owner)
    done = await _run_off_loop(argv, environment, timeout, stdin=stdin, keep_stderr=True)
    _observe(authority, done)
    return (done.returncode or 0, done.stdout, done.stderr)


async def run(
    argv: Sequence[str],
    *,
    timeout: float,
    repo: object = None,
    owner: str | None = None,
) -> tuple[int, bytes]:
    """Run a read, once, as the authority for its owner.

    Raises ``OSError``/``TimeoutError`` like the subprocess call it wraps —
    ``GitHubRefusal`` is an ``OSError`` — so callers decide what an unreachable
    ``gh`` means for them. There is no retry: under the same authority a failed
    read fails the same way, and under any other it would speak as someone else.
    """
    environment, authority = await _environment_off_loop(argv, repo, owner)
    # stderr is kept only to read GitHub's suspension words off it; callers still get
    # the two-part answer they always had.
    done = await _run_off_loop(argv, environment, timeout, keep_stderr=authority is not None)
    _observe(authority, done)
    return (done.returncode or 0, done.stdout)
