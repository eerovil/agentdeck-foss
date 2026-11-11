"""The pull request's paperwork, checked by a script before its review starts (#4261).

About a quarter of review findings were paperwork the agent forgot, and each cost a whole
review round although the pre-review checklist named it. So when the agent says it went
through the checklist (``pre-review-done``), the deck reads the pull request and its Work's
issue and refuses, naming each gap, before any review starts. No model is asked.

What is checked:

- the pull request's body names the Work's issue with ``Refs #<n>`` (or ``Closes``,
  ``Fixes``, ``Resolves``, a full ``owner/repo#n`` or the issue's URL);
- the issue has a comment holding the ``Use-case walkthrough:`` line;
- when the pull request adds or changes a ``docs/diagrams/`` file, the issue has the
  diagram comment ending ``<!-- agentdeck-diagram -->``.

Assumed (#4261): the older rule that a diagram whose ``## Boxes`` names a changed file must
be edited too is not checked, because #4254 drops that duty (a diagram is drawn only for a
new feature). Whether a change is a new feature is a judgement no script makes, so a
missing diagram is left to the reviewer.

A read that fails checks nothing: the review starts as before, and the reviewer still
checks each point.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from . import gh_cli

log = logging.getLogger(__name__)

WALKTHROUGH = "Use-case walkthrough:"
DIAGRAM_MARK = "<!-- agentdeck-diagram -->"
DIAGRAM_DIR = "docs/diagrams/"

_TIMEOUT_S = 20.0
# The line opens the comment's walkthrough; a quote of it inside backticks or mid-sentence
# (an issue asking for it, a reviewer naming it) is not one.
_WALKTHROUGH_RE = re.compile(r"(?m)^[\s>*_#]*" + re.escape(WALKTHROUGH))
_REFERENCE_RE = re.compile(
    r"(?i)\b(?:refs?|close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"(?:https://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/(?:issues|pull)/(?P<url_n>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<n>\d+))\b"
)


@dataclass(frozen=True)
class Paperwork:
    """What the check reads: the pull request's body and changed paths, and the bodies of
    the Work issue's comments."""

    body: str
    files: tuple[str, ...]
    comments: tuple[str, ...]


def names_issue(body: str, pull_repository: str, repository: str, issue: int) -> bool:
    """Whether ``body`` says ``Refs``/``Closes``/… the Work's issue."""
    for match in _REFERENCE_RE.finditer(body or ""):
        named = match.group("url_repo") or match.group("repo") or pull_repository
        number = match.group("url_n") or match.group("n")
        if named.lower() == repository.lower() and int(number) == issue:
            return True
    return False


def gaps(paper: Paperwork, pull_repository: str, repository: str, issue: int) -> list[str]:
    """Each piece of paperwork missing, as a sentence the agent can act on; ``[]`` if none."""
    found: list[str] = []
    ref = f"#{issue}" if repository.lower() == pull_repository.lower() else f"{repository}#{issue}"
    if not names_issue(paper.body, pull_repository, repository, issue):
        found.append(
            f"the pull request body does not say `Refs {ref}` (`Closes` only if the issue asks)"
        )
    if not any(_WALKTHROUGH_RE.search(comment or "") for comment in paper.comments):
        found.append(
            f"the issue has no comment with the `{WALKTHROUGH}` line: post the numbered steps"
            " (or `not applicable — <reason>` for a refactor, docs or tests only)"
        )
    diagrams = sorted(
        path for path in paper.files if path.startswith(DIAGRAM_DIR) and path.endswith(".md")
    )
    # The diagram comment ends with the mark; a mention of it in prose is not one.
    if diagrams and not any(
        (comment or "").rstrip().endswith(DIAGRAM_MARK) for comment in paper.comments
    ):
        found.append(
            f"the pull request changes {', '.join(f'`{path}`' for path in diagrams)} but the"
            f" issue has no diagram comment ending `{DIAGRAM_MARK}`"
        )
    return found


async def _read(argv: list[str], what: str) -> dict | None:
    try:
        code, stdout = await gh_cli.run(argv, timeout=_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        log.warning("review paperwork: %s unreadable: %s", what, exc)
        return None
    if code != 0:
        log.warning("review paperwork: %s unreadable (exit %s)", what, code)
        return None
    try:
        value = json.loads(stdout or b"null")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def read(pull_repository: str, number: int, repository: str, issue: int) -> Paperwork | None:
    """The pull request's and its Work issue's paperwork read from GitHub, ``None`` if
    either read failed."""
    pull = await _read(
        ["gh", "pr", "view", str(number), "--repo", pull_repository, "--json", "body,files"],
        f"{pull_repository}#{number}",
    )
    thread = await _read(
        ["gh", "issue", "view", str(issue), "--repo", repository, "--json", "comments"],
        f"{repository}#{issue}",
    )
    if pull is None or thread is None:
        return None
    files = tuple(
        str(item.get("path") or "") for item in pull.get("files") or () if isinstance(item, dict)
    )
    comments = tuple(
        str(item.get("body") or "")
        for item in thread.get("comments") or ()
        if isinstance(item, dict)
    )
    return Paperwork(str(pull.get("body") or ""), files, comments)


async def check(pull_repository: str, number: int, work: str) -> list[str]:
    """The paperwork gaps of ``pull_repository#number`` for the Work issue ``work``
    (``owner/repo#n``); ``[]`` when complete or unreadable."""
    repository, _, issue = work.rpartition("#")
    if not repository or not issue.isdigit():
        return []
    paper = await read(pull_repository, number, repository, int(issue))
    if paper is None:
        return []
    return gaps(paper, pull_repository, repository, int(issue))
