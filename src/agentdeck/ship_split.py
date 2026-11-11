"""Ship a reviewed commit whose pull request has moved on (#4212).

The owner pressed Ship now on a head the reviewer answered with findings, but the agent had
already pushed a fix on top. The owner chose not to force-push: the deck opens a new pull
request from a branch at the reviewed commit, which ships through the normal merge queue, and
turns the original pull request back into a draft that carries only the fix. Every step here
can be repeated: a press cut off half way finds the branch and the pull request it made.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

#: ``(argv, repository) -> (exit code, stdout, stderr)``, one ``gh`` call run exactly once.
Run = Callable[[Sequence[str], str], Awaitable[tuple[int, bytes, bytes]]]


async def _gh(argv: Sequence[str], repository: str) -> tuple[int, bytes, bytes]:
    from . import gh_cli

    return await gh_cli.run_once(argv, timeout=60, repo=repository)


def branch_name(number: int, head: str) -> str:
    """The branch the deck opens at ``head`` for pull request ``number``."""
    return f"ship/{number}-{head[:12].lower()}"


class SplitFailed(RuntimeError):
    """The reviewed commit could not be put in a pull request of its own; the reason."""


async def _json(run: Run, argv: Sequence[str], repository: str) -> object:
    code, out, _err = await run(argv, repository)
    if code != 0:
        return None
    try:
        return json.loads(out or b"null")
    except ValueError:
        return None


async def open_ship_pull(
    repository: str, number: int, head: str, issue: int, *, run: Run = _gh
) -> int:
    """The number of the open pull request that ships ``head``, ``number``'s reviewed commit,
    opened as a draft by this call or an earlier one; ``number`` is then a draft too.

    Raises ``SplitFailed`` with the reason. A pull request from a fork is refused: its
    commits are not this repository's to branch from.
    """
    head = head.lower()
    facts = await _json(
        run,
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--repo",
            repository,
            "--json",
            "baseRefName,isCrossRepository,state",
        ],
        repository,
    )
    if not isinstance(facts, dict) or str(facts.get("state") or "").upper() != "OPEN":
        raise SplitFailed(f"{repository}#{number} is not open, or could not be read now")
    if facts.get("isCrossRepository"):
        raise SplitFailed(
            f"{repository}#{number} comes from a fork, so its commit has no branch here"
        )
    base = str(facts.get("baseRefName") or "")
    if not base:
        raise SplitFailed(f"{repository}#{number}'s base branch could not be read")
    branch = branch_name(number, head)
    await _branch_at(run, repository, branch, head)
    found = await _open_pull_from(run, repository, branch)
    if not found:
        found = await _create_pull(run, repository, number, head, issue, branch, base)
    code, _out, err = await run(
        ["gh", "pr", "ready", str(number), "--undo", "--repo", repository], repository
    )
    if code != 0:
        why = err.decode("utf-8", "replace").strip() or f"gh exited {code}"
        raise SplitFailed(f"{repository}#{number} could not be turned back into a draft ({why})")
    return found


async def _branch_at(run: Run, repository: str, branch: str, head: str) -> None:
    """Make ``branch`` point at ``head``; one made earlier at ``head`` is kept."""
    await run(
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repository}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={head}",
        ],
        repository,
    )
    # Read back rather than trust the exit code: the branch may exist from an earlier press.
    sha = await _json(
        run,
        ["gh", "api", f"repos/{repository}/git/ref/heads/{branch}", "--jq", ".object.sha | tojson"],
        repository,
    )
    if str(sha or "").lower() != head:
        raise SplitFailed(f"branch {branch} could not be made at {head[:12]}")


async def _open_pull_from(run: Run, repository: str, branch: str) -> int:
    """The open pull request from ``branch``, or 0."""
    owner = repository.partition("/")[0]
    found = await _json(
        run,
        [
            "gh",
            "api",
            f"repos/{repository}/pulls?state=open&head={owner}:{branch}",
            "--jq",
            "[.[].number]",
        ],
        repository,
    )
    numbers = [n for n in found if isinstance(n, int)] if isinstance(found, list) else []
    return min(numbers, default=0)


async def _create_pull(
    run: Run, repository: str, number: int, head: str, issue: int, branch: str, base: str
) -> int:
    body = (
        f"Refs #{issue}\n\n"
        f"The owner chose Ship now on #{number} at {head}: its review came back with findings,"
        f" and a fix was pushed on top of it after. This pull request ships that reviewed commit"
        f" as it is, and #{number} goes on with the fix only (#4212).\n\nOpened by AgentDeck."
    )
    created = await _json(
        run,
        [
            "gh",
            "api",
            "-X",
            "POST",
            f"repos/{repository}/pulls",
            "-f",
            f"title=Ship #{number} at {head[:12]}, its findings fixed in #{number}",
            "-f",
            f"head={branch}",
            "-f",
            f"base={base}",
            "-f",
            f"body={body}",
            "-F",
            "draft=true",
            "--jq",
            ".number",
        ],
        repository,
    )
    if isinstance(created, int) and created > 0:
        return created
    # The create may have landed and only its answer been lost.
    found = await _open_pull_from(run, repository, branch)
    if not found:
        raise SplitFailed(f"no pull request could be opened from {branch}")
    return found
