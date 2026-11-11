"""The issue a Work's agent starts on, pasted into its first message (#4254).

Every Work's agent opened by reading its issue, and a wayfinder ticket's agent then its map:
a model call or two each, on every Work. The deck reads them once at the start instead, with
the issue's comments (its own start comment left out), and puts them below the brief.

Best effort: a read that fails or runs long leaves the text out, and the agent reads the issue
itself as before. The text is capped, saying so when it was cut, so one huge issue cannot fill
the first message.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Sequence

from . import gh_cli
from .work_signals import MARKER as START_MARKER

log = logging.getLogger(__name__)

GH_TIMEOUT_S = 15.0
#: The most of the issue (and of a map) the first message carries.
MAX_CHARS = 12_000
#: At most this many comments, the first ones, are read.
MAX_COMMENTS = 30
#: The line a wayfinder ticket names its map with (``skills/wayfinder/WAYFINDER.md``).
MAP_LINE = re.compile(
    r"^Map: \[[^\]\n]*\]\(https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)\)\s*$",
    re.MULTILINE,
)

#: ``(argv, repository) -> (exit code, stdout, stderr)``: one ``gh`` call.
Run = Callable[[Sequence[str], str], Awaitable[tuple[int, bytes, bytes]]]


async def _gh(argv: Sequence[str], repository: str) -> tuple[int, bytes, bytes]:
    gh = shutil.which("gh")
    if gh is None:
        return 1, b"", b"gh is not available"
    return await gh_cli.run_once([gh, *argv], timeout=GH_TIMEOUT_S, repo=repository)


async def _read(run: Run, repository: str, path: str) -> object:
    try:
        code, out, err = await run(["api", path], repository)
    except Exception:  # noqa: BLE001 - a missing issue text must never stop the start
        log.warning("work start: could not read %s", path, exc_info=True)
        return None
    if code != 0:
        log.warning("work start: reading %s exited %s: %s", path, code, err[:200])
        return None
    try:
        return json.loads(out or b"null")
    except ValueError:
        return None


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + "\n\n[cut here: read the rest on GitHub]"


async def _issue_block(run: Run, repository: str, number: int, heading: str) -> tuple[str, str]:
    """``(block, body)`` for one issue: its title, body and comments; ``("", "")`` unread."""
    issue = await _read(run, repository, f"repos/{repository}/issues/{number}")
    if not isinstance(issue, dict):
        return "", ""
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    parts = [f"{heading} {repository}#{number}: {issue.get('title') or ''}".rstrip(), body.strip()]
    if isinstance(issue.get("comments"), int) and issue["comments"] > 0:
        comments = await _read(
            run,
            repository,
            f"repos/{repository}/issues/{number}/comments?per_page={MAX_COMMENTS}",
        )
        if not isinstance(comments, list):
            parts.append(f"(Its {issue['comments']} comments could not be read: read them.)")
        else:
            for comment in comments:
                text = comment.get("body") if isinstance(comment, dict) else None
                if not isinstance(text, str) or START_MARKER in text:
                    continue
                who = (comment.get("user") or {}).get("login") or "someone"
                parts.append(f"--- comment by {who} ---\n{text.strip()}")
            if issue["comments"] > MAX_COMMENTS:
                parts.append(f"(Only the first {MAX_COMMENTS} comments are here: read the rest.)")
    return _cap("\n\n".join(part for part in parts if part)), body


async def issue_text(repository: str, number: int, *, run: Run | None = None) -> str:
    """The issue, and a wayfinder ticket's map, as the first message shows them; "" unread.

    ``run`` defaults to ``_gh``, looked up at call time so the test suite can stub it.
    """
    run = run or _gh
    block, body = await _issue_block(run, repository, number, "Issue")
    if not block:
        return ""
    found = MAP_LINE.search(body)
    if found is not None:
        map_block, _ = await _issue_block(run, found.group(1), int(found.group(2)), "Its map")
        if map_block:
            block += "\n\n" + map_block
        else:
            block += (
                f"\n\n(Its map {found.group(1)}#{found.group(2)} could not be read: read it.)"
            )
    return (
        "The issue as it reads now is below, so you need not read it again; its later "
        "comments reach you as messages.\n\n<issue>\n" + block + "\n</issue>"
    )
