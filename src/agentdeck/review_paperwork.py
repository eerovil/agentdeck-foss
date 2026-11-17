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

Before the pull request's first review only (no review asked at any head yet, read by the
router and passed in as ``helpers``), one more point is checked in every repository (#4437):

- the issue has a comment holding the ``Helper checks:`` line: what the three Haiku helpers
  of ``agentdeck pre-review-helpers`` found, each find fixed or dropped, or ``did not run
  (<why>)``. A Work already in a fix round when this shipped is never asked for it.

Assumed (#4261): the older rule that a diagram whose ``## Boxes`` names a changed file must
be edited too is not checked, because #4254 drops that duty (a diagram is drawn only for a
new feature). Whether a change is a new feature is a judgement no script makes, so a
missing diagram is left to the reviewer.

In a repository whose manifest entry says ``check_tests = true`` (#4326, read as
``Policy.tests_checked`` and passed in as ``tests``) two more points are checked:

- the head has a verification job of the Work queued, running or green, and its newest run at
  the head is not red (the router reads the jobs, ``verification_gap``);
- a diff that changes Python code outside the tests also touches a ``test_*.py``, unless the
  body says ``No test: <why>`` with a real reason on a line of its own, outside a quote or a
  code block.

Assumed (#4326): the wording, the ``No test:`` escape, and the robot repository only at first;
looked at again after its next 10 pull requests that reach review (issues#1297).

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
HELPER_CHECKS = "Helper checks:"
DIAGRAM_MARK = "<!-- agentdeck-diagram -->"
DIAGRAM_DIR = "docs/diagrams/"

NO_TEST = "No test:"

_TIMEOUT_S = 20.0
_NO_TEST_RE = re.compile(r"(?i)^[\s*_+-]*" + re.escape(NO_TEST) + r"(?P<why>.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# An HTML comment, or one left open to the end of the body (GitHub hides that too).
_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
# The line opens the comment's walkthrough; a quote of it inside backticks or mid-sentence
# (an issue asking for it, a reviewer naming it) is not one.
_WALKTHROUGH_RE = re.compile(r"(?m)^[\s>*_#]*" + re.escape(WALKTHROUGH))
_HELPER_LINE_RE = re.compile(r"(?m)^[\s>*_#]*" + re.escape(HELPER_CHECKS) + r"(.*)$")
#: The pull request a ``Helper checks:`` line opens with, and nothing after it but a space,
#: punctuation or the line's end.
_RECEIPT_PULL_RE = re.compile(
    r"\s*(?:(?:https?://)?(?:www\.)?github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/pull/(?P<url_n>\d+)"
    r"|(?P<repo>[\w.-]+/[\w.-]+)?#(?P<n>\d+))(?![\w/#])",
    re.IGNORECASE,
)
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


def helper_receipt(comment: str, pull_repository: str, number: int, repository: str) -> bool:
    """Whether ``comment`` has a ``Helper checks:`` line whose first word is this pull
    request: its ``owner/repo#n``, its URL, or ``#n`` read in the issue's ``repository``. Only
    that leading name counts, never a reference later on the line, so a receipt is for the one
    pull request it opens with and a Work's second pull request runs its own (#4437 review)."""
    for line in _HELPER_LINE_RE.finditer(comment):
        lead = _RECEIPT_PULL_RE.match(line.group(1))
        if lead is None:
            continue
        named = lead.group("url_repo") or lead.group("repo") or repository
        if (
            named.lower() == pull_repository.lower()
            and int(lead.group("url_n") or lead.group("n")) == number
        ):
            return True
    return False


def gaps(
    paper: Paperwork,
    pull_repository: str,
    repository: str,
    issue: int,
    *,
    tests: bool = False,
    helpers: bool = False,
    number: int = 0,
) -> list[str]:
    """Each piece of paperwork missing, as a sentence the agent can act on; ``[]`` if none.
    ``helpers`` asks for the ``Helper checks:`` comment: before the first review (#4437)."""
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
    if helpers and not any(
        helper_receipt(comment or "", pull_repository, number, repository)
        for comment in paper.comments
    ):
        pull = (
            f"#{number}"
            if repository.lower() == pull_repository.lower()
            else (f"{pull_repository}#{number}")
        )
        found.append(
            f"the issue has no comment with a `{HELPER_CHECKS} {pull}` line: run `agentdeck"
            " pre-review-helpers`, fix each real find, and post each find as fixed or dropped"
            f" (or `Helper checks: {pull} did not run (<why>)`)"
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
    if tests:
        untested = untested_gap(paper)
        if untested:
            found.append(untested)
    return found


def says_no_test(body: str) -> bool:
    """Whether ``body`` gives a reason for no test: a ``No test: <why>`` line of its own that
    the page shows as text -- not in a quote, a code block or an HTML comment -- whose reason
    is words and not the ``<why>`` placeholder (#4327 review)."""
    fenced = False
    # A hidden HTML comment is not shown, so a line in it gives no reason (#4327 review).
    shown = _COMMENT_RE.sub("", body or "")
    for line in shown.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith(">") or line.startswith(("    ", "\t")):
            continue
        match = _NO_TEST_RE.match(line)
        if match is None:
            continue
        why = match.group("why").strip(" \t*_`")
        if re.search(r"\w", why) and not re.fullmatch(r"<[^>]*>\.?", why):
            return True
    return False


def _is_test(path: str) -> bool:
    name = path.rpartition("/")[2]
    return name.startswith("test_") and name.endswith(".py")


def _is_code(path: str) -> bool:
    """A Python file that is not a test: not ``test_*.py``, ``*_test.py``, ``conftest.py``,
    nor under a ``tests``/``test`` directory."""
    if not path.endswith(".py") or _is_test(path):
        return False
    *dirs, name = path.split("/")
    if name == "conftest.py" or name.endswith("_test.py"):
        return False
    return not any(part in {"tests", "test"} for part in dirs)


def untested_gap(paper: Paperwork) -> str:
    """The gap when the diff changes Python code and no ``test_*.py``, or "" (#4326)."""
    code = sorted(path for path in paper.files if _is_code(path))
    if not code or any(_is_test(path) for path in paper.files):
        return ""
    if says_no_test(paper.body):
        return ""
    shown = ", ".join(f"`{path}`" for path in code[:3]) + (" and more" if len(code) > 3 else "")
    return (
        f"the pull request changes Python code ({shown}) but no `test_*.py`: add or extend a"
        f" test, or say why none fits on a line `{NO_TEST} <why>` in the body"
    )


def verification_gap(rows: list[object], work: str, head: str) -> str:
    """The gap when ``head`` has no verification job of ``work`` queued, running or green, or
    its newest run there is red, or "" (#4326). ``rows`` are the Work's jobs: the retained
    evidence and the live queue.

    One rule, in submission order (#4327 review): the newest job of the Work that speaks for
    ``head`` decides. A queued or running one speaks for it unless it already read another
    head (a queued job has not read its head yet), and the merge still waits on a green run.
    A finished one speaks only when it ran at ``head``: green there clears it, and so does a
    failure its reruns or merge-base run forgive, or one still being followed up; any other
    failure there is named. A job that proves nothing either way (cancelled, a pass on a tree
    that moved) is passed over for the one before it.
    """
    from .jobs import QUEUED, RUNNING, VERIFICATION
    from .poller_engine.verification import credited_head_for_work_alone
    from .poller_engine.workspace import WorkspaceSnapshot

    wanted = (work or "").strip().lower()
    head = (head or "").strip().lower()
    # Every job of the Work, follow-ups included: a rerun or a merge-base run carries no
    # verification purpose, yet it is what forgives a failure the release credits (#4327).
    work_rows = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("work_id") or "").strip().lower() == wanted
    ]
    mine = [row for row in work_rows if row.get("purpose") == VERIFICATION]
    for row in sorted(mine, key=lambda row: float(row.get("submitted_at") or 0), reverse=True):
        tested = WorkspaceSnapshot.from_record(row.get("tested_workspace")).head.lower()
        if row.get("state") in {QUEUED, RUNNING}:
            if not tested or tested == head:
                return ""
            continue
        if tested != head:
            continue
        if credited_head_for_work_alone(row, work_id=work, jobs=work_rows).lower() == head:
            return ""
        # A rerun or merge-base run of it still going on may yet forgive it.
        if any(
            str(other.get("follows") or "") == str(row.get("id") or "")
            and other.get("state") in {QUEUED, RUNNING}
            for other in work_rows
        ):
            return ""
        if row.get("state") == "failed":
            return (
                f"verification job {row.get('id')} failed at {head[:12]}: fix the tests, push,"
                " and run this Work's verification job on the new head"
            )
    return (
        f"no verification job of this Work is queued, running or green at {head[:12]}: run"
        " this Work's verification job on it (`agentdeck job submit --verification`)"
    )


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


async def check(
    pull_repository: str, number: int, work: str, *, tests: bool = False, helpers: bool = False
) -> list[str]:
    """The paperwork gaps of ``pull_repository#number`` for the Work issue ``work``
    (``owner/repo#n``); ``[]`` when complete or unreadable. ``tests`` adds the changed-test
    point (#4326), ``helpers`` the helper checks' comment (#4437)."""
    repository, _, issue = work.rpartition("#")
    if not repository or not issue.isdigit():
        return []
    paper = await read(pull_repository, number, repository, int(issue))
    if paper is None:
        return []
    return gaps(
        paper, pull_repository, repository, int(issue), tests=tests, helpers=helpers, number=number
    )
