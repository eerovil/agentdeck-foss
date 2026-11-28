"""The pull request's paperwork, checked by a script before its review starts (#4261).

About a quarter of review findings were paperwork the agent forgot, and each cost a whole
review round although the pre-review checklist named it. So when the agent says it went
through the checklist (``pre-review-done``), the deck reads the pull request and its Work's
issue and refuses, naming each gap, before any review starts. No model is asked.

What is checked:

- the pull request's body names the Work's issue with ``Refs #<n>`` (or ``Closes``,
  ``Fixes``, ``Resolves``, a full ``owner/repo#n`` or the issue's URL);
- the issue has a comment holding the ``Use-case walkthrough:`` line;
- when the pull request adds a ``docs/diagrams/`` file, or changes the part of one that the
  diagram comment shows (its title, Mermaid block or ``## Colours`` list), the issue has that
  comment, ending ``<!-- agentdeck-diagram -->``. An edit to the ``## Boxes`` notes or the
  introduction, or to a plain note under the block, alone needs none: the comment shows none
  of them (#4598, #4647);
- a pull request of ``BIG_PULL_LINES`` changed lines or more (additions plus deletions) has a
  ``Reading guide:`` line in its body, outside a code block or a hidden comment (#4786). Only
  that the section is there is checked, not what it says.

Before the pull request's first review only (no review asked at any head yet, read by the
router and passed in as ``helpers``), one more point is checked in every repository (#4437):

- the issue has a comment holding the ``Helper checks:`` line: what the three Haiku helpers
  of ``agentdeck pre-review-helpers`` found, each find fixed
  or dropped, or ``did not run (<why>)``. A Work already in a fix round when this shipped is
  never asked for it.

A Work draws or updates a diagram only when the owner asks (#4721), and the reviewer does
not read diagrams, so nothing checks that a diagram exists or matches the code. The comment
check above stays: it is what shows an asked-for diagram on the issue.

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

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from . import code_lines, gh_cli

log = logging.getLogger(__name__)

WALKTHROUGH = "Use-case walkthrough:"
HELPER_CHECKS = "Helper checks:"
DIAGRAM_MARK = "<!-- agentdeck-diagram -->"
DIAGRAM_DIR = "docs/diagrams/"

NO_TEST = "No test:"
READING_GUIDE = "Reading guide:"
#: Changed lines (additions plus deletions) from which a pull request's body owes the owner a
#: reading guide (#4786, assumed: 1,000, in every repository). Looked at again two weeks after
#: it is live: dropped if the owner does not use it.
BIG_PULL_LINES = 1000

_TIMEOUT_S = 20.0
_TEXTS_QUERY = (
    "query($owner:String!,$name:String!,$old:String!,$new:String!){"
    "repository(owner:$owner,name:$name){"
    "old:object(expression:$old){...on Blob{text}}"
    "new:object(expression:$new){...on Blob{text}}}}"
)
_GUIDE_RE = re.compile(r"(?i)^[\s*_#]*" + re.escape(READING_GUIDE))
_NO_TEST_RE = re.compile(r"(?i)^[\s*_+-]*" + re.escape(NO_TEST) + r"(?P<why>.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_OPEN_RE = re.compile(r"`{3,}(?!.*`)|~{3,}")
# Under a block, a line that may be the drawing's legend and not a plain note: a heading or
# its underline, a list item, a table's row or ruler, a quote or HTML. ``#12``, ``<3 s``
# and a link in angle brackets are text.
_NOT_A_NOTE_RE = re.compile(
    r"#{1,6}(\s|$)|=+$|[-*+](\s|$)|\d+[.)](\s|$)|\||[-:|\s]*-[-:|\s]*$|>"
    r"|<[!?]|</?[A-Za-z][A-Za-z0-9-]*(\s|/?>|$)"
)
# An inline code span on one line: a ``<!--`` or ``-->`` in it is shown, so it opens or closes
# no HTML comment (#4786 review). A comment left open hides the lines after it until ``-->``
# or the end of the body (GitHub hides that too), but not inside a code block (#4786 helpers).
_CODE_SPAN_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
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
    """What the check reads: the pull request's body and changed paths, the bodies of the
    Work issue's comments, the diagrams whose drawing the pull request changes, and its
    changed lines (additions plus deletions; 0 when unread)."""

    body: str
    files: tuple[str, ...]
    comments: tuple[str, ...]
    drawn: tuple[str, ...] = ()
    changed: int = 0


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


def _is_diagram(path: str) -> bool:
    return path.startswith(DIAGRAM_DIR) and path.endswith(".md")


def has_diagram_comment(comments: tuple[str, ...]) -> bool:
    """Whether a comment ends with the mark; a mention of it in prose is not one."""
    return any((comment or "").rstrip().endswith(DIAGRAM_MARK) for comment in comments)


def _closes(raw: str, fence: str) -> bool:
    """Whether ``raw`` closes a block opened by ``fence``, by Markdown's rule: the same marker,
    at least as long, alone on its line and indented at most three spaces."""
    line = raw.strip()
    indent = len(raw.expandtabs(4)) - len(raw.expandtabs(4).lstrip())
    return indent <= 3 and len(line) >= len(fence) and not line.strip(fence[0])


def shown_part(text: str) -> list[str]:
    """A diagram file's lines without what its issue comment leaves out: the plain paragraphs
    between the title and the first section, above the first block (the introduction) or under
    one (a note, #4647), and the ``## Boxes`` notes. Blank lines and trailing space are
    dropped. Anything else counts as shown, so a file of an unusual shape still asks for its
    comment (#4598): a heading, list, table, quote, indented code or HTML under a block, with
    the paragraph it stands in and all that follows it there, and everything after a block
    left open."""
    shown: list[str] = []
    part = "start"
    fence = ""
    note: list[str] = []  # the paragraph under a block being read, kept if it is no plain note
    for raw in text.splitlines():
        line = raw.strip()
        if fence:
            if line:
                shown.append(raw.rstrip())
            if _closes(raw, fence):
                part, fence, note = "note", "", []
            continue
        if line.startswith("## "):
            part = "boxes" if line[3:].strip().lower() == "boxes" else "shown"
        elif part in {"intro", "note"} and (opened := _OPEN_RE.match(line)):
            part, fence = "shown", opened.group()
        elif part == "note":
            indented = not note and raw.expandtabs(4).startswith("    ")
            if line and (indented or _NOT_A_NOTE_RE.match(line)):
                part = "shown"
                shown += note
            else:
                note = [*note, raw.rstrip()] if line else []
        if line and part in {"start", "shown"}:
            shown.append(raw.rstrip())
        if part == "start" and line.startswith("# "):
            part = "intro"
    return shown


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
    if paper.drawn and not has_diagram_comment(paper.comments):
        found.append(
            f"the pull request adds or redraws (title, Mermaid block or `## Colours`)"
            f" {', '.join(f'`{path}`' for path in paper.drawn)} but the issue has no"
            f" diagram comment ending `{DIAGRAM_MARK}`"
        )
    if paper.changed >= BIG_PULL_LINES and not has_reading_guide(paper.body):
        found.append(
            f"the pull request changes {paper.changed:,} lines but its body has no"
            f" `{READING_GUIDE}` section: say in a line or two why it needs this many, then the"
            " few places that carry the risk in reading order with what to check in each, then"
            " what is safe to skip (tests, migrations, generated files, docs) with their line"
            " counts"
        )
    if tests:
        untested = untested_gap(paper)
        if untested:
            found.append(untested)
    return found


def _shown_lines(body: str) -> list[str]:
    """The lines of ``body`` the page shows as text: not in a code block, a quote, an indented
    code line or an HTML comment. A ``<!--`` shown inside a code block opens no comment."""
    fenced = hidden = False
    lines: list[str] = []
    for line in (body or "").splitlines():
        if hidden:
            end = line.find("-->")
            if end < 0:
                continue
            hidden, line = False, line[end + 3 :]
        elif _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line, hidden = _uncomment(line)
        if line.lstrip().startswith(">") or line.startswith(("    ", "\t")):
            continue
        lines.append(line)
    return lines


def _uncomment(line: str) -> tuple[str, bool]:
    """``line`` without its HTML comments, and whether the last one is left open. Delimiters
    inside an inline code span are text (#4786 review)."""
    masked = _CODE_SPAN_RE.sub(lambda match: "x" * len(match.group(0)), line)
    kept: list[str] = []
    at = 0
    while True:
        start = masked.find("<!--", at)
        if start < 0:
            kept.append(line[at:])
            return "".join(kept), False
        kept.append(line[at:start])
        end = masked.find("-->", start + 4)
        if end < 0:
            return "".join(kept), True
        at = end + 3


def has_reading_guide(body: str) -> bool:
    """Whether ``body`` shows a line opening ``Reading guide:`` (a heading or bold allowed),
    the big pull request's guide for the owner (#4786)."""
    return any(_GUIDE_RE.match(line) for line in _shown_lines(body))


def says_no_test(body: str) -> bool:
    """Whether ``body`` gives a reason for no test: a ``No test: <why>`` line of its own that
    the page shows as text -- not in a quote, a code block or an HTML comment -- whose reason
    is words and not the ``<why>`` placeholder (#4327 review)."""
    # A hidden HTML comment is not shown, so a line in it gives no reason (#4327 review).
    for line in _shown_lines(body):
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


async def _read(argv: list[str], what: str, *, repo: str | None = None) -> dict | None:
    try:
        code, stdout = await gh_cli.run(argv, timeout=_TIMEOUT_S, repo=repo)
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


async def read_drawn(repository: str, base: str, head: str, paths: list[str]) -> tuple[str, ...]:
    """Those of ``paths`` whose shown part differs between ``head`` and its merge base with
    ``base``: an added diagram, or a changed drawing. A deleted one has nothing to show, and
    one that cannot be read is left to the reviewer (#4598)."""
    if not paths:
        return ()
    owner, _, name = repository.partition("/")
    compare = await _read(
        ["gh", "api", f"repos/{repository}/compare/{base}...{head}?per_page=1"],
        f"{repository} {base[:12]}...{head[:12]}",
    )
    merge_base = (compare or {}).get("merge_base_commit")
    sha = str(merge_base.get("sha") or "") if isinstance(merge_base, dict) else ""
    if not sha:
        return ()

    async def texts(path: str) -> dict | None:
        value = await _read(
            [
                *("gh", "api", "graphql", "-f", f"query={_TEXTS_QUERY}"),
                *("-f", f"owner={owner}", "-f", f"name={name}"),
                *("-f", f"old={sha}:{path}", "-f", f"new={head}:{path}"),
            ],
            f"{repository} {path}",
            repo=repository,
        )
        data = (value or {}).get("data")
        found = data.get("repository") if isinstance(data, dict) else None
        return found if isinstance(found, dict) else None

    drawn = []
    for path, pair in zip(paths, await asyncio.gather(*map(texts, paths)), strict=True):
        new = (pair or {}).get("new")
        if not isinstance(new, dict) or not isinstance(new.get("text"), str):
            continue
        old = pair.get("old")
        if old is None:
            drawn.append(path)
        elif isinstance(old, dict) and isinstance(old.get("text"), str):
            if shown_part(old["text"]) != shown_part(new["text"]):
                drawn.append(path)
    return tuple(drawn)


async def read(pull_repository: str, number: int, repository: str, issue: int) -> Paperwork | None:
    """The pull request's and its Work issue's paperwork read from GitHub, ``None`` if
    either read failed."""
    pull = await _read(
        [
            *("gh", "pr", "view", str(number), "--repo", pull_repository),
            *("--json", "body,files,baseRefOid,headRefOid,additions,deletions"),
        ],
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
    drawn: tuple[str, ...] = ()
    diagrams = sorted(filter(_is_diagram, files))
    if diagrams and not has_diagram_comment(comments):
        added = {
            str(item.get("path") or "")
            for item in pull.get("files") or ()
            if isinstance(item, dict) and item.get("changeType") == "ADDED"
        }
        edited = [path for path in diagrams if path not in added]
        drawn = await read_drawn(
            pull_repository,
            str(pull.get("baseRefOid") or ""),
            str(pull.get("headRefOid") or ""),
            edited,
        )
        drawn = tuple(path for path in diagrams if path in added or path in drawn)
    # Code lines without whole-line comments when a local checkout holds both commits
    # (#4812), else GitHub's count of every line.
    counted = await code_lines.COUNTER.count(
        pull_repository, str(pull.get("baseRefOid") or ""), str(pull.get("headRefOid") or "")
    )
    changed = (
        counted.total
        if counted is not None
        else sum(
            value
            for value in (pull.get("additions"), pull.get("deletions"))
            if isinstance(value, int) and not isinstance(value, bool)
        )
    )
    return Paperwork(str(pull.get("body") or ""), files, comments, drawn, changed)


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
