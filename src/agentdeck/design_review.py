"""A risky Work's design, reviewed before its code (#4332).

A reviewer that reads only code finds a design gap through the bugs it causes, one round at a
time: on one robot build about nine rounds went on one question one paragraph
could have settled. So a Work whose agent marks its spec as big gets a design round
before any code:

- **The design comment.** The Work's agent posts a comment on its issue opening with
  ``**Design:**``: what runs at the same time, which row, lock or file decides, and what a
  crash, a timeout or a retry does; for a large change, its parts and what each owns. Posting
  it is the agent's own mark that its spec is big (owner, 13 Nov).
- **The design review.** ``POST /api/sessions/<key>/design-review`` starts a deck reviewer
  chat on a throwaway checkout of the repository's default branch. It reads the issue and the
  design, and sends one message through ``agentdeck deck-request`` whose first line is
  ``/claude Design review of <owner/repo#n>: pass <id>`` or ``…: gap <id>``, ``<id>`` being
  the design comment's number. The deck takes it only from that reviewer, known by the session
  token it was started with (``agent_review.reviews_design``), so the Work's agent cannot send
  one itself. A verdict about a design the agent has since replaced is dropped. Otherwise the
  deck posts it on the issue, so the owner sees it, records it in its own file
  (``Verdicts``), and hands it to the Work's agent (``WorkRelease.on_design_review``).
  On a gap the agent fixes the design, posts it again as a new ``**Design:**`` comment, and asks
  again, until a review passes or the second round ends it with notes (#4463, below).
- **The gate.** No code review of the Work's pull requests starts while its issue's newest
  ``**Design:**`` comment has no passing verdict in that record (``DesignRound.missing``,
  checked by ``WorkRouterService._design_gap`` at every start; ``pre-review-done`` and
  ``review-request`` refuse with it). Only the deck writes the record, so no comment on
  GitHub can pass a design.
- **A withdrawn design (#4762).** When the owner picks a narrow fix over a redesign, the agent
  edits the design comment's first line to open with ``**Design (withdrawn`` instead. The
  newest design comment or withdrawn one decides: a withdrawn one ends its series, so no older
  design holds the code review any more, and the next review start records it as withdrawn so
  a later design's rounds count from one. Waiting for the owner's pick is the agent's rule, as
  waiting for their approval is (#4476); the edit stays on the issue for the owner to see.

Owner's choices (#4332): a deck step, not only a guide rule, with an independent reviewer;
started only by the agent's own mark (a check of the diff at ready would come after the code
is written); each verdict names the design comment it reviewed.

Assumed (#4332), looked at again after the first ten design rounds: the marker
``**Design:**``; the deck's own design reviewer is Claude (or Codex, when that is the code
reviewer). The design reviewer shares the code reviewer's
places (``review.max_reviewers``) and ledger.

**When every place is taken (#4342)** the request is not refused: ``ask`` raises ``Busy``, and
the Work router keeps it in its saved retry list (``work_router.PendingActions``, one
``design:<owner/repo#n>`` entry per issue), the same route as a code review that finds every
place taken: tried again with the same growing wait, dropped after a day. ``GET
…/design-review`` shows it as ``queued``. Chosen by the owner on PR #4344: no order of its
own, no place number.

**ChatGPT reads the design when it reviews the code (#4722).** Owner's choice (26 Nov): the
design review moves to ChatGPT, as the code review did. With ChatGPT as the code reviewer and
its timed switch off, the Work router opens one chat issue per design comment
(``chat_title``, ``chat_brief``) instead of starting a deck reviewer. ChatGPT answers with a
comment on the Work's own issue whose first line is the bare
``Design review of <owner/repo#n>: pass|gap|notes <id>`` (``chat_verdict_of``): no ``/claude``,
so no command reader takes it. The router takes it only while its pending entry names a chat
for that design comment, and only from the deck's reviewer account or ChatGPT's own GitHub
connector under the login that owns that chat's repository; it records it (``DesignRound.take``)
and tells the agent, as above. So a comment can now pass a design, but only that one, asked
for by the deck. Assumed: the brief lives here, not in the chat repository's policy; a chat
with no verdict ``CHAT_LIMIT_S`` after it opened is closed and the deck's own reviewer takes
the design (a worker not started now is owed to the run in flight, so it gets the same
time); a request keeps the route it started on, whatever the switch does later.

**A closed issue gets no design review (#4675)**: ``ask`` refuses, a waiting request is
dropped, and a verdict that arrives after the close asks for no new design and no code. A
reviewer already running goes on: its findings can still help the Work that took the design.

**At most two rounds (#4463, built in #4496).** Rounds piled up to about six a Work, nearly
half of their gaps holes in the previous round's fix. So a design series (the designs since
the issue's last passing one) gets two reviews. Round 1 lists every gap at once, worked
through a fixed list (``CHECKLIST``), and skips small details; round 2 rechecks only round 1's
gaps and their fixes. A gap still open after round 2 does not hold the code: the deck records
the design as passed with notes (``NOTES``), the Work may start coding, and the code review
checks the code against each note. The round is read from the deck's own record
(``round_of``): one more than the gap verdicts on earlier design comments since the last pass.
Owner's choice: the cap, round 2's scope, the notes, and telling the reviewer it has two
rounds so it skips small details. Assumed: the fixed list, design-level designs, a new series
after a pass counting from one again, and the deck (not the reviewer) deciding the round.

**At most three series a Work, then the owner (#4765).** The two-round cap bounds one series,
not a Work: one Work had eight series (16 reviews), each started by a new design after a pass.
So the first round of a fourth series (``MAX_SERIES``) starts no review: ``target`` refuses
(``LIMIT``) and tells the agent to ask the owner in its question box: one more series, code
without a design review, or pause the Work. The agent sends the answer as ``POST
…/design-review`` with ``{"owner": "more"}`` (the review starts, and the next series asks
again) or ``{"owner": "skip"}`` (the deck records the design as ``OWNER``, which holds no
code); a pause is the ``parked`` label, no deck step. The count reads the deck's own record:
the earlier designs whose series ended (``PASS``, ``NOTES`` or ``OWNER``). While the limit
holds, the deck notes the design comment (``Verdicts.limited``, #4800), so the release and the
turn-end nudge, which read no GitHub, do not ask for the ``pre-review-done`` the gate refuses
then. Owner's choice: a
limit per Work, then ask. Assumed: three series; the three answers; that the owner's answer
reaches the deck through the agent, as a redesign's approval does (#4476).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .design_wait import OWNER_WAIT

if TYPE_CHECKING:
    from .agent_review import IssueCopy

log = logging.getLogger(__name__)

#: How a design comment opens. A quote of it, or a mention mid-line, is not one.
DESIGN_MARK = "**Design:**"

PASS = "pass"
GAP = "gap"
#: The design passed with notes: round 2 still found gaps, which go to the code review as
#: notes to check instead of holding the code (#4463).
NOTES = "notes"
#: A design series gets this many reviews (#4463, the owner's choice).
MAX_ROUNDS = 2
#: The owner let the Work code without a review of this design (#4765): it holds no code.
OWNER = "owner"
#: A Work gets this many design series before the owner is asked (#4765, assumed).
MAX_SERIES = 3
#: The owner's answers at the limit (#4765): one more series, or code with no review.
MORE = "more"
SKIP = "skip"
#: The issue has no design comment, or its newest one was withdrawn (#4762).
NONE = "none"
#: How a withdrawn design comment opens (#4762): the owner picked a narrow fix instead.
WITHDRAWN_MARK = "**Design (withdrawn"
#: What the deck records for a withdrawn design: it ends its series as a pass does.
WITHDRAWN = "withdrawn"
#: The verdicts that end a series (#4765): a withdrawn design's series ended too.
_ENDS = (PASS, NOTES, OWNER, WITHDRAWN)
#: Its newest design comment has no verdict after it.
UNREVIEWED = "unreviewed"
#: A reviewer the deck started is reading its newest design comment (``status`` only).
REVIEWING = "reviewing"
#: The request waits in the router's retry list for a reviewer place (#4342, ``status`` only).
QUEUED = "queued"

_DESIGN_RE = re.compile(r"(?m)^[ \t]*" + re.escape(DESIGN_MARK))
#: The verdict line as the reviewer sends it. The deck's own reviewer opens it with `/claude`;
#: ChatGPT, which posts its verdict on the issue itself, leaves that out (#4722), so neither
#: the poller nor the router's comment intake reads its comment as a command.
_VERDICT_RE = re.compile(
    r"(?i)^\s*(?P<command>/claude\s+)?Design review of\s+(?P<ref>[\w.-]+/[\w.-]+#\d+)\s*:\s*"
    r"(?P<verdict>pass|gap|notes)\b(?:[ \t]+(?P<comment>\d+)\b)?"
)
_COMMENT_ID_RE = re.compile(r"#issuecomment-(?P<id>\d+)$")


@dataclass(frozen=True)
class Verdict:
    """A design review's verdict, read from the reviewer's message."""

    issue: str
    verdict: str
    #: The design comment's number (its URL's ``#issuecomment-<n>``), "" when not named.
    comment: str = ""
    #: Whether its line opened with `/claude`: the deck's own reviewer's, not ChatGPT's.
    command: bool = True


def verdict_of(text: str) -> Verdict | None:
    """The verdict a reviewer's message opens with, or ``None`` when it is no design verdict.

    Only the first non-empty line counts: findings that quote a verdict further down are not
    one.
    """
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        match = _VERDICT_RE.match(line)
        if match is None:
            return None
        return Verdict(
            match.group("ref").lower(),
            match.group("verdict").lower(),
            match.group("comment") or "",
            match.group("command") is not None,
        )
    return None


def chat_verdict_of(text: str) -> Verdict | None:
    """The verdict of a comment ChatGPT posted on the issue itself (#4722): the same line
    without `/claude`, naming the design comment. ``None`` for anything else, the deck's own
    reviewer's `/claude` line included."""
    found = verdict_of(text)
    return found if found is not None and not found.command and found.comment else None


def restated(text: str, verdict: str) -> str:
    """``text`` with its verdict line's word replaced by ``verdict`` (#4463): the deck posts
    the verdict it records, so the issue shows ``notes`` where a last-round reviewer wrote
    ``gap``, and ``gap`` for a ``notes`` before the last round. ``text`` unchanged when its
    first non-empty line is no verdict."""
    lines = (text or "").split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = _VERDICT_RE.match(line)
        if match is None:
            return text
        start, end = match.span("verdict")
        lines[index] = line[:start] + verdict + line[end:]
        return "\n".join(lines)
    return text


def comment_id(url: str) -> str:
    """The number of an issue comment from its URL, or ""."""
    match = _COMMENT_ID_RE.search((url or "").strip())
    return match.group("id") if match else ""


def is_design(comment: str) -> bool:
    """Whether ``comment`` is a design comment: a line of its own opens with the mark. A
    verdict is never one, whatever it quotes (#4722: ChatGPT's is posted unquoted)."""
    return _DESIGN_RE.search(comment or "") is not None and verdict_of(comment) is None


def is_withdrawn(comment: str) -> bool:
    """Whether ``comment`` is a withdrawn design comment (#4762): its first non-empty line
    opens with ``WITHDRAWN_MARK``, where the agent puts it. The mark quoted further down, in
    a live design or a reply, withdraws nothing."""
    first = next((line for line in (comment or "").splitlines() if line.strip()), "")
    return first.lstrip(" \t").startswith(WITHDRAWN_MARK)


def newest_design(comments: list[dict]) -> dict | None:
    """The newest design comment of ``comments`` (GitHub's comment objects, oldest first), or
    ``None`` when there is none or a withdrawn one is newer (#4762). The deck's post of a
    verdict quotes every line, so it is never one."""
    for comment in reversed(comments):
        body = str(comment.get("body") or "")
        if is_withdrawn(body):
            return None
        if is_design(body):
            return comment
    return None


def withdrawn_numbers(comments: list[dict]) -> list[str]:
    """The comment numbers of the withdrawn design comments in ``comments`` (#4762)."""
    found = (comment_id(str(c.get("url") or "")) for c in comments)
    return [
        number
        for number, comment in zip(found, comments, strict=True)
        if number and is_withdrawn(str(comment.get("body") or ""))
    ]


def _ordered(verdicts: dict[str, str], before: str) -> list[tuple[int, str, str]]:
    """The recorded verdicts on design comments older than ``before``, newest first. Comment
    numbers grow with time on GitHub, so their order is the designs' order."""
    if not before.isdigit():
        return []
    found = [(int(k), k, v) for k, v in verdicts.items() if k.isdigit() and int(k) < int(before)]
    return sorted(found, reverse=True)


def round_of(verdicts: dict[str, str], comment: str) -> int:
    """Which review of its series the design comment ``comment`` gets (#4463): one more than
    the gap verdicts on the designs before it since the last pass (``verdicts`` as
    ``Verdicts.of`` gives them). A new design after a pass starts again at one."""
    gaps = 0
    for _number, _key, verdict in _ordered(verdicts, comment):
        if verdict != GAP:
            break
        gaps += 1
    return gaps + 1


def series_before(verdicts: dict[str, str], comment: str) -> int:
    """How many design series of the issue ended before design ``comment`` (#4765)."""
    return sum(1 for _n, _k, verdict in _ordered(verdicts, comment) if verdict in _ENDS)


def at_limit(verdicts: dict[str, str], comment: str, allowed: set[str]) -> bool:
    """Whether a review of design ``comment`` waits on the owner (#4765): it opens a series
    after ``MAX_SERIES`` ended ones, and the owner has not allowed one more for it."""
    return (
        comment not in allowed
        and round_of(verdicts, comment) == 1
        and series_before(verdicts, comment) >= MAX_SERIES
    )


def limit_text(issue: str) -> str:
    """What the agent does when the series limit stops a review (#4765)."""
    return (
        f"{issue} has had {MAX_SERIES} design-review series already, so this design gets no"
        " review until the owner answers (#4765). Ask the owner in your question box, with the"
        " three options: start one more review series, let the Work code without a design"
        " review, or pause the Work. On their answer: `POST …/design-review` with"
        f' `{{"owner": "{MORE}"}}` starts the review, `{{"owner": "{SKIP}"}}` lets you build'
        " to this design with no review, and a pause is the `parked` label on the issue."
        " Send an answer only the owner gave"
    )


def earlier_gap(verdicts: dict[str, str], comment: str) -> str:
    """The number of the design before ``comment`` whose review found the gaps it answers,
    or "" when ``comment`` opens its series (#4463)."""
    for _number, key, verdict in _ordered(verdicts, comment):
        return key if verdict == GAP else ""
    return ""


def kept(verdicts: dict[str, str], verdict: Verdict) -> str:
    """The verdict the deck records for ``verdict`` (#4463): a gap in the last round becomes
    notes, and notes before it are a gap; a pass stays a pass."""
    if verdict.verdict == PASS:
        return PASS
    return NOTES if round_of(verdicts, verdict.comment) >= MAX_ROUNDS else GAP


def state(
    comments: list[str] | tuple[str, ...],
    urls: list[str] | tuple[str, ...],
    verdicts: dict[str, str],
) -> str:
    """The issue's design state: ``none``, ``unreviewed``, ``gap``, ``pass`` or ``notes``.

    ``comments`` are the issue's comment bodies, oldest first, and ``urls`` their URLs, from
    which each comment's number is read. ``verdicts`` is the deck's own record of the issue
    (``Verdicts.of``), by design comment number. The newest design counts, and only a verdict
    recorded for its number: a design whose number is unread stays unreviewed. A withdrawn
    design newer than any design holds nothing: ``none`` (#4762).
    """
    for index in range(len(comments) - 1, -1, -1):
        if is_withdrawn(comments[index]):
            return NONE
        if is_design(comments[index]):
            number = comment_id(urls[index]) if index < len(urls) else ""
            return verdicts.get(number, UNREVIEWED) if number else UNREVIEWED
    return NONE


#: Where ``Verdicts`` keeps the owner's "one more series" answers: no issue has this name.
_ALLOWED = "owner-allowed"
#: Where it keeps the design comments the series limit stopped (#4800): no issue has this name.
_LIMITED = "series-limited"


class Verdicts:
    """The design verdicts the deck took from its reviewers, by issue and design comment
    number: the gate's only evidence. One JSON file, written whole."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def beside(cls, ledger: Path) -> Verdicts:
        """The record next to the reviewers' ledger, where the services process writes it
        and the web reads it (``GET …/design-review``)."""
        return cls(ledger.with_name("design-verdicts.json"))

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def of(self, issue: str) -> dict[str, str]:
        found = self._read().get(issue.lower())
        return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}

    def record(self, issue: str, comment: str, verdict: str) -> None:
        data = self._read()
        data.setdefault(issue.lower(), {})[comment] = verdict
        self._write(data)

    def allowed(self, issue: str) -> set[str]:
        """The design comments the owner allowed one more series for (#4765)."""
        found = self._read().get(_ALLOWED, {})
        numbers = found.get(issue.lower()) if isinstance(found, dict) else None
        return {str(n) for n in numbers} if isinstance(numbers, list) else set()

    def allow(self, issue: str, comment: str) -> None:
        data = self._read()
        found = data.get(_ALLOWED)
        found = found if isinstance(found, dict) else {}
        numbers = found.get(issue.lower())
        numbers = [str(n) for n in numbers] if isinstance(numbers, list) else []
        if comment not in numbers:
            found[issue.lower()] = [*numbers, comment]
        data[_ALLOWED] = found
        self._write(data)

    def limited(self, issue: str) -> set[str]:
        """The design comments of ``issue`` whose review the series limit stopped (#4800) and
        that the owner has not answered for: no verdict, no "one more series"."""
        data = self._read()
        found = data.get(_LIMITED, {})
        numbers = found.get(issue.lower()) if isinstance(found, dict) else None
        if not isinstance(numbers, list):
            return set()
        decided = self.of(issue)
        allowed = self.allowed(issue)
        return {str(n) for n in numbers if str(n) not in decided and str(n) not in allowed}

    def mark_limited(self, issue: str, comment: str | None) -> None:
        """Note that the series limit stops ``comment``'s review (#4800), or with ``None`` that
        it stops none of ``issue``'s now. Written only on a change: the gate calls it often."""
        data = self._read()
        found = data.get(_LIMITED)
        found = found if isinstance(found, dict) else {}
        numbers = found.get(issue.lower())
        numbers = [str(n) for n in numbers] if isinstance(numbers, list) else []
        wanted = [comment] if comment else []
        if numbers == wanted:
            return
        if wanted:
            found[issue.lower()] = wanted
        else:
            found.pop(issue.lower(), None)
        data[_LIMITED] = found
        self._write(data)

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1))
        temporary.replace(self._path)


def gap(state_now: str) -> str:
    """What the design round still needs before a code review, or "" (``DesignRound.missing``):
    a design passed with notes holds nothing (#4463)."""
    how = (
        "call `POST $AGENTDECK_URL/api/sessions/<session-key>/design-review`, and fix the"
        " design until a review passes; a design gets two rounds, and a gap left after the"
        " second is a note for the code review"
    )
    if state_now == GAP:
        return (
            "the newest design review found a gap: post the fixed design as a new comment"
            f" opening with `{DESIGN_MARK}`, then {how}"
        )
    if state_now == UNREVIEWED:
        return f"the issue's newest `{DESIGN_MARK}` comment has no design review after it: {how}"
    return ""


#: The design gap while the review of the newest design was asked and has not answered
#: (#4781): an agent told to call ``design-review`` again while one ran thought the deck lost it.
WAITING_GAP = (
    f"the review of the issue's newest `{DESIGN_MARK}` comment was asked for and its verdict"
    " has not come yet: wait for it, as it comes to this chat as a message, and write no code"
    " until it passes; then call this again. Asking for the design review again adds nothing"
)

#: What a round-1 reviewer works through (#4463, assumed): a case outside it is no gap.
CHECKLIST = (
    "first, each lock, state or queue the design adds: does the case it answers need it"
    " (#4757)? One it does not need is a gap: say it can go, and do not review its races",
    "what runs at the same time (robots, processes, requests, timers) and which row, lock or"
    " file decides between them",
    "what a crash, a timeout and a retry do at each step: nothing left half done, nothing run"
    " twice",
    "for a large change, its parts and which one owns each row, file or step",
    "parts or lines of the design that contradict each other",
)


#: Both rounds (#4802): on #1607 four of six round-1 gaps were guards the existing receive step
#: already had, added and then removed again at the owner's question.
COVERED_RULE = (
    "- Before you name a gap, check that the code the design would change does not already"
    " refuse or handle that case (an existing check, status test or step it reuses), and name"
    " the `file:line` you checked in the gap's line. A case the existing code already handles"
    " is no gap."
)


def _rules(issue: str, number: str, earlier: str, line: str) -> tuple[str, str]:
    """What a round checks and how its verdict is written, as two lists of the brief: the
    same for the deck's own reviewer and for ChatGPT (#4722). ``line`` is how the verdict
    line opens."""
    listed = "\n".join(f"  {n}. {item}" for n, item in enumerate(CHECKLIST, 1))
    if earlier:
        scope = f"""- This is the last round: a design gets only two reviews (#4463). Read \
the design before it, {earlier}, and the gap verdict after it. Check only those gaps: \
is each one fixed here, and does its fix open a hole of its own? Raise nothing else, and no \
small details.
- Raise only what everyday use or money reaches (#4760), both a round-1 gap still open and a \
hole a fix opens. A case that needs an unlikely ordering, a crash at one exact moment or a \
rare input is left out, unless it loses money, and not written as a note either: a round-1 \
gap open only in such a case counts as fixed.
- A gap you still find does not hold the code back. The deck records the design as passed \
with notes, the Work starts coding (on a redesign answering a design finding, only what \
the owner picked), and the code review checks the code for each note. So \
write each one so a code reviewer can check it in code: the case, and what must hold.
{COVERED_RULE}"""
        lines = f"""  - If every gap from round 1 is fixed, its first line is exactly \
`{line} {issue}: pass {number}`, and the next lines say in a few sentences \
what you checked.
  - Otherwise its first line is exactly `{line} {issue}: notes {number}`, \
and each next line is one gap still open: which round-1 gap it is, and the case that shows \
it."""
    else:
        scope = f"""- A design gets only two reviews (#4463): this one, and one more that \
rechecks only the gaps you list now. Whatever is still open after that goes to the code \
review as a note. So list every gap now, in this one message, and spend no time on small \
details: only what would break the change.
- Work through this list, and only this list:
{listed}
  A gap is a case on that list the design leaves open or gets wrong: two things racing for \
one row, a step a crash leaves half done, a retry that runs something twice, a part nobody \
owns. Wording, tests, names, code style and function-level details are not gaps: a design \
stays at the level of parts, rows, locks and failure paths, and the code review sees the \
rest in real code.
{COVERED_RULE}"""
        lines = f"""  - If the design holds, its first line is exactly \
`{line} {issue}: pass {number}`, and the next lines say in a few sentences \
what you checked.
  - Otherwise its first line is exactly `{line} {issue}: gap {number}`, and \
each next line is one gap: the line of the design it breaks, or that the design missed it, \
and the case that shows it."""
    return scope, lines


def brief(
    *,
    name: str,
    issue: str,
    design_url: str,
    base: str,
    request: str,
    token: str,
    earlier: str = "",
    copy: IssueCopy | None = None,
) -> str:
    """The first message of one design reviewer's chat. It opens like every reviewer's, so
    the chat list leaves it out (``agent_review.is_reviewer_prompt``). ``request`` is the
    reviewer's ``deck-request`` command (``agent_review.DECK_REQUEST``); ``token`` its own
    session token, which the deck asks of a verdict (a Codex shell has no variable for it).
    Its message file is named by the token's first 12 characters (#4468): a name no other
    reviewer uses, that does not leave the whole token in a listable ``/var/tmp``.

    ``earlier`` is the URL of the design whose review found the gaps this one answers: this
    is then the last round (#4463), which rechecks only those gaps and their fixes.

    ``copy`` is the issue with its comments, which then ends the message (#4726); the
    reviewer is sent to ``gh`` only when the design, or the one before it, is not in it."""
    scope, lines = _rules(issue, comment_id(design_url), earlier, "/claude Design review of")
    needed = {comment_id(url) for url in (design_url, earlier) if url}
    if copy is not None and needed <= {comment_id(url) for url in copy.comments}:
        reading = (
            "Read the issue and that comment at the end of this message, where the deck put"
            " the issue and its comments: do not fetch them again."
        )
        ending = f"\n{copy.text}"
    else:
        reading = (
            f"Read the issue and that comment: `gh issue view {issue.partition('#')[2]} --repo"
            f" {issue.partition('#')[0]} --comments`."
        )
        ending = ""
    return f"""You are AgentDeck's {name} reviewer. The deck started you to review the design \
of the Work {issue} before any code is written for it: the comment {design_url}. This \
directory is a throwaway checkout of `{base}`, the code as it is now, made for you; the deck \
removes it when you stop.

- {reading} Read the code here only as far as the design needs. \
Take about 10 to 15 minutes. This review is read-only: do not edit, commit or push, and do not \
comment on GitHub.
{scope}
- Send one message through the deck, which posts it on the issue and hands it to the Work's \
agent. Write it to `/var/tmp/design-review-{token[:12]}.md`, a name no other reviewer uses (a \
reviewer before you may have left its own verdict under another name, #4468), and send it with
    `{request} {issue} --session-token {token} --body-file /var/tmp/design-review-{token[:12]}.md`
  with a 10-minute timeout, never in the background. The deck's answer is the delivery. If the \
command fails or its answer is unclear, run the same command once more: a repeat is the same \
request.
{lines}
{ending}"""


#: How long ChatGPT has for a design verdict before the deck's own reviewer takes the design
#: (#4722, assumed; looked at again with the first week's cost reading).
CHAT_LIMIT_S = 20 * 60.0


def chat_title(issue: str, comment: str) -> str:
    """The title of the ChatGPT chat reviewing design comment ``comment`` of ``issue``
    (#4722). Not a code review's ``Review <owner/repo#n>``, so nothing that handles review
    chats (the board's review rows, the timed switch's take-over) takes it for one."""
    return f"Design review of {issue} ({comment})"


def chat_brief(*, issue: str, design_url: str, earlier: str = "") -> str:
    """The body of the ChatGPT chat that reviews a design (#4722): the same round rules as
    ``brief``, with the verdict posted by ChatGPT itself as a comment on the Work's issue. Its
    verdict line has no `/claude`, and no line of it may open with a command: a comment that
    opens with one is an instruction to the Work's agent (``chat_verdict_of``)."""
    repository, _, number = issue.partition("#")
    scope, lines = _rules(issue, comment_id(design_url), earlier, "Design review of")
    return f"""Review the design of the Work {issue} before any code is written for it: the \
comment {design_url}.

This is a design review, not a code review: there is no pull request, and \
`skills/github-repo-reviewer` does not apply. This issue is the whole task.

- Read issue #{number} of `{repository}` with all its comments, and that comment. Read the \
code of `{repository}` only as far as the design needs. Take about 10 to 15 minutes. Change \
nothing there: no edits, commits, pull requests or labels.
{scope}
- Post one comment on issue #{number} of `{repository}` (not on this issue), with the repo \
MCP connector's `add_issue_comment` or your GitHub connector:
{lines}
  Start no line of that comment with `/claude` or `/reviewed`. Post it once: read the issue's \
comments back before posting again after an unclear answer.
- The deck reads that comment and tells the Work's agent, so tell nobody else. Then finish \
this chat as done. With no verdict {int(CHAT_LIMIT_S // 60)} minutes after this chat opened, \
the deck closes it and has its own reviewer read the design: a verdict posted after that \
counts for nothing.
"""


@dataclass(frozen=True)
class ChatRoute:
    """ChatGPT as the design reviewer (#4722): the code review's chat route, reused."""

    #: Whether a design goes to ChatGPT now: it reviews this deck's code, and the timed
    #: switch to the deck's own reviewers is off.
    wanted: Callable[[], bool]
    #: Open a chat with this title and body: ``(its "owner/repo#n", whether a worker was
    #: started for it)``. Raises when the chat could not be opened.
    open: Callable[[str, str], Awaitable[tuple[str, bool]]]
    #: Close the chat ``owner/repo#n`` with a comment saying why; whether it closed.
    close: Callable[[str, str], Awaitable[bool]]


#: Why ``Refused`` (#4342): the router drops a waiting request only on ``DECIDED``,
#: ``NO_DESIGN`` or ``CLOSED``, and tries again on every other.
DECIDED = "decided"
NO_DESIGN = "none"
#: The issue is closed (#4675): no code follows on it, so no review of its design.
CLOSED = "closed"
#: The Work's design series reached ``MAX_SERIES`` (#4765): the owner decides.
LIMIT = "limit"
UNREAD = "unread"
OTHER = "other"


class Refused(Exception):
    """Why no design review started, with the HTTP status the deck answers and ``why`` it
    was refused (``DECIDED``, ``NO_DESIGN``, ``CLOSED``, ``UNREAD`` or ``OTHER``)."""

    def __init__(self, detail: str, status: int = 409, why: str = OTHER, comment: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.why = why
        #: The design comment it is about, when known (``LIMIT``).
        self.comment = comment


class Busy(Exception):
    """Every reviewer place is taken (#4342): the router keeps the request and tries again."""


async def read_comments(issue: str) -> list[dict] | None:
    """The issue's comments from GitHub, oldest first, or ``None`` when unread."""
    from . import gh_cli

    repository, _, number = issue.partition("#")
    argv = ["gh", "issue", "view", number, "--repo", repository, "--json", "comments"]
    try:
        code, stdout = await gh_cli.run(argv, timeout=20.0)
        found = json.loads(stdout or b"{}").get("comments") if code == 0 else None
    except (OSError, TimeoutError, ValueError, AttributeError):
        return None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else None


async def read_closed(issue: str) -> bool:
    """Whether the issue is closed on GitHub (#4675). An unread state counts as open: an
    extra review costs less than a Work held back by one failed read."""
    from .review_refs import target_closed

    repository, _, number = issue.partition("#")
    try:
        return number.isdigit() and await target_closed(repository, int(number)) is True
    except (OSError, TimeoutError):  # ``gh_cli.run`` could not start, or got no answer
        return False


Read = Callable[[str], Awaitable[list[dict] | None]]


async def _never_closed(issue: str) -> bool:
    return False


#: What an agent may still do about the design of a closed issue (#4675).
CLOSED_NEXT = (
    "the issue is closed, so its design gets no review (#4675): post no new design here. If"
    " another Work took the design over, that Work posts it on its own issue and asks there. If"
    " the work goes on here, reopen the issue, then read `GET …/design-review` for the next"
    " step. A reviewer already reading the design still gives its verdict"
)

#: What ``status`` tells the agent to do next, by state (#4337).
_NEXT = {
    NONE: f"post a comment opening with `{DESIGN_MARK}` on its own line, then"
    " `POST …/design-review`",
    UNREVIEWED: "no reviewer is on it: `POST …/design-review`. Asking again is safe: while a"
    " reviewer is starting or running on this design comment, it answers that one and starts"
    " no second",
    QUEUED: "it waits for a reviewer place and starts by itself when one frees up: end the"
    " turn; the verdict comes to this chat as a message. Asking again adds nothing",
    REVIEWING: "a reviewer is on it (or still starting): the verdict comes to this chat as a"
    " message; write no code until it passes",
    PASS: f"the design passed. {OWNER_WAIT} For any other design, build to it",
    NOTES: "the design passed with notes after its two review rounds (#4463). "
    f"{OWNER_WAIT} For any other design, build to it, and make the code answer each note; the"
    " code review checks the code against them",
    OWNER: f"the owner let this design go without a review (#4765). {OWNER_WAIT} For any other"
    " design, build to it",
    GAP: f"the review found a gap: post the fixed design as a new `{DESIGN_MARK}` comment,"
    " then `POST …/design-review`",
}


async def status(
    issue: str,
    verdicts: Verdicts,
    reviewer: Callable[[str], dict | None],
    read: Read = read_comments,
    queued: dict | None = None,
    closed: Callable[[str], Awaitable[bool]] = _never_closed,
) -> dict:
    """Where the design round of ``issue`` stands, for ``GET …/design-review`` (#4337): an
    agent told "not confirmed" reads here whether its review started, without asking again.

    ``reviewer(comment)`` is the reviewer the deck started on that design comment, as
    ``{"running": bool, ...}``, or ``None``. ``queued`` is the request's waiting entry
    (``work_router.design_queue``), or ``None`` (#4342). Raises ``Refused`` (503) when
    the comments are unread.
    """
    comments = await read(issue)
    if comments is None:
        raise Refused(f"the comments of {issue} could not be read", 503)
    design = newest_design(comments)
    url = str((design or {}).get("url") or "")
    number = comment_id(url)
    found = reviewer(number) if number else None
    if design is None:
        now = NONE
    else:
        now = verdicts.of(issue).get(number, UNREVIEWED) if number else UNREVIEWED
        if now == UNREVIEWED and found and found.get("running"):
            now = REVIEWING
        elif now == UNREVIEWED and queued and queued.get("comment") == number:
            # ChatGPT was asked for this design (#4722): the entry is the record of it.
            now, found = REVIEWING, {"running": True, "chat": queued.get("chat") or ""}
        elif now == UNREVIEWED and queued and not queued.get("comment"):
            now = QUEUED
    step = _NEXT[now]
    if now != REVIEWING and await closed(issue):
        answer = {"issue": issue, "design": url, "state": now, "reviewer": found}
        return {**answer, "closed": True, "next": CLOSED_NEXT}
    if now == UNREVIEWED and found:
        step = (
            "the reviewer started on this design stopped without a verdict:"
            " `POST …/design-review` starts a new one"
        )
    if now == UNREVIEWED and at_limit(verdicts.of(issue), number, verdicts.allowed(issue)):
        step = limit_text(issue)  # even after a reviewer stopped: the POST would refuse
    answer = {"issue": issue, "design": url, "state": now, "reviewer": found, "next": step}
    if now == QUEUED:
        answer["queue"] = queued
    return answer


class DesignRound:
    """The design review of a Work's issue (#4332): starting it, and taking its verdict.
    ``start`` is ``AgentReviewer.start_design``; ``verdicts`` the deck's record."""

    def __init__(
        self,
        start,
        verdicts: Verdicts,
        read: Read = read_comments,
        reviews: Callable[[str, str, str], bool] = lambda token, issue, comment: False,
        closed: Callable[[str], Awaitable[bool]] = _never_closed,
        reviewing: Callable[[str, str], bool] = lambda issue, comment: False,
    ) -> None:
        self._start = start
        self.verdicts = verdicts
        self._read = read
        # ``reviews(token, issue, comment)``: whether ``token`` is the reviewer the deck
        # started on that design (``agent_review.reviews_design``).
        self._reviews = reviews
        # Whether the issue is closed (``read_closed``, #4675).
        self.closed = closed
        # ``reviewing(issue, comment)``: whether the deck's own reviewer is on that design
        # now (``agent_review.design_reviewer``): ChatGPT is then not asked too (#4722).
        self.reviewing = reviewing

    async def _newest(self, issue: str) -> tuple[dict | None, list[dict]]:
        comments = await self._read(issue)
        if comments is None:
            raise Refused(f"the comments of {issue} could not be read", 503, UNREAD)
        return newest_design(comments), comments

    async def missing(self, issue: str, queued: dict | None = None) -> str:
        """What holds a code review of ``issue``'s pull requests back (#4332 review): the
        design gap, or "" when the newest design passed or there is none. Unread comments
        hold it too, so no start skips the design gate (``WorkRouterService._design_gap``).

        ``queued`` is the router's waiting entry for this issue's design review, or ``None``.
        While a review of the newest design runs or waits (``waits_for``), the gap says to wait
        for its verdict, not to ask for it again (#4781)."""
        comments = await self._read(issue)
        if comments is None:
            return f"the comments of {issue} could not be read to check its design review"
        bodies = [str(item.get("body") or "") for item in comments]
        urls = [str(item.get("url") or "") for item in comments]
        recorded = self.verdicts.of(issue)
        now = state(bodies, urls, recorded)
        found = gap(now)
        number = comment_id(str((newest_design(comments) or {}).get("url") or ""))
        limited = False
        if now == UNREVIEWED and self.waits_for(issue, number, queued):
            found = WAITING_GAP  # a review asked for waits on its verdict, not on the owner
        elif now == UNREVIEWED and at_limit(recorded, number, self.verdicts.allowed(issue)):
            limited = True
            found = limit_text(issue)  # one next step, the one the POST would give (#4765)
        # The release and the turn-end nudge read it with no GitHub call (#4800).
        self.verdicts.mark_limited(issue, number if limited else None)
        if found and await self.closed(issue):
            return f"the issue is closed, so reopen it first (#4675); {found}"
        return found

    def observe_comment(self, issue: str, number: str, body: str, deleted: bool = False) -> None:
        """Keep the series-limit note in step with the issue's comments (#4800 review): the
        release and the turn-end nudge read it with no GitHub call, so the note follows each
        design comment created, edited or deleted, by the same local rule the gate uses.

        ``number`` and ``body`` are a comment of ``issue`` from its webhook; ``deleted`` when it
        was deleted. The noted design deleted, withdrawn or edited into no design (or a withdrawn
        comment after it) clears the note; a newer reply deleted leaves it. A live design after
        the noted one, created or edited, is noted when the limit holds for it (``at_limit`` on
        the deck's own record) and clears the note otherwise; the noted design edited and still
        a design stays noted. An older comment changes nothing. It
        records no verdict: an event is not the gate's evidence, and a withdrawal recorded here
        would outlive the comment being edited back to a live design. Owner's choice (4800):
        this narrow fix; a missed webhook, or an older unreviewed design left newest by a
        delete, waits for the next gate call or ``POST …/design-review`` to note it."""
        if not number.isdigit():
            return
        noted = [int(n) for n in self.verdicts.limited(issue) if n.isdigit()]
        newest = max(noted, default=0)
        if int(number) < newest:
            return
        stopped = bool(noted) and int(number) == newest  # the noted design itself
        if deleted or not is_design(body):
            # Gone, or edited into no design: only the noted design's own comment clears it.
            if stopped or (noted and not deleted and is_withdrawn(body)):
                self.verdicts.mark_limited(issue, None)
            return
        if is_withdrawn(body):
            if noted:
                self.verdicts.mark_limited(issue, None)
            return
        if stopped:
            return  # the noted design edited, still a design
        recorded = self.verdicts.of(issue)
        limited = number not in recorded and at_limit(
            recorded, number, self.verdicts.allowed(issue)
        )
        if limited or noted:
            self.verdicts.mark_limited(issue, number if limited else None)

    def waits_for(self, issue: str, number: str, queued: dict | None) -> bool:
        """Whether a review of ``issue``'s design comment ``number`` was asked and its verdict
        is still to come (#4781): the deck's own reviewer is on it, the request waits for a
        reviewer place (``queued`` names no comment), or ChatGPT was asked for this comment."""
        if not number:
            return False
        if queued is not None:
            comment = str(queued.get("comment") or "")
            if not comment or comment == number:
                return True
        try:
            return bool(self.reviewing(issue, number))
        except Exception:  # noqa: BLE001 - unread: the plain gap, which asks again safely
            log.warning("design-review: reviewer of %s unread", issue, exc_info=True)
            return False

    async def ask(self, issue: str, checkout: str = "") -> dict:
        """Start the review of ``issue``'s newest design (``POST
        /api/sessions/<key>/design-review``); ``checkout`` is the asking chat's directory: its
        repository is the code reviewed, whichever repository holds the issue. Raises
        ``Refused`` when there is nothing to review or no reviewer could start, and ``Busy``
        when every reviewer place is taken (the router then keeps it and tries again, #4342)."""
        from .review_refs import ReviewBusy, ReviewError

        url, _number, earlier = await self.target(issue)
        asks = {"earlier": earlier} if earlier else {}
        try:
            started = await self._start(issue, url, checkout=checkout, **asks)
        except ReviewBusy as exc:
            raise Busy(str(exc)) from exc
        except ReviewError as exc:
            raise Refused(str(exc)) from exc
        return {
            "issue": issue,
            "design": url,
            "started": started.summary,
            "next": "the verdict comes to this chat as a message, and is posted on the issue;"
            " write no code until it passes",
        }

    async def target(self, issue: str) -> tuple[str, str, str]:
        """What a review of ``issue`` would read now: its newest design comment's URL and
        number, and the URL of the design whose gaps it answers ("" in round 1). Raises
        ``Refused`` when there is nothing to review; starts nothing."""
        design, comments = await self._newest(issue)
        if await self.closed(issue):
            raise Refused(
                f"no review of {issue}'s design: {CLOSED_NEXT}",
                why=CLOSED,
            )
        if design is None:
            self.verdicts.mark_limited(issue, None)  # no design, no limit (#4800 review)
            raise Refused(
                f"{issue} has no design comment: post one opening with `{DESIGN_MARK}` on its"
                " own line, then call this again",
                why=NO_DESIGN,
            )
        url = str(design.get("url") or "")
        number = comment_id(url)
        if not number:
            raise Refused(f"the newest design comment of {issue} has no readable URL", 503)
        # A withdrawn design ends its series (#4762): this design's rounds count from one.
        recorded = self.verdicts.of(issue)
        for gone in withdrawn_numbers(comments):
            if recorded.get(gone) != WITHDRAWN:
                self.verdicts.record(issue, gone, WITHDRAWN)
                recorded[gone] = WITHDRAWN
        now = recorded.get(number)
        # Noted for the release and the turn-end nudge, which read no GitHub (#4800); any other
        # answer here clears it, so an owner's answer never meets a stale note.
        limited = now is None and at_limit(recorded, number, self.verdicts.allowed(issue))
        self.verdicts.mark_limited(issue, number if limited else None)
        if now == NOTES:
            raise Refused(
                f"the newest design of {issue} passed with notes after its two review rounds"
                f" (#4463), so post no new design for the notes. {OWNER_WAIT} For any other"
                " design, build to it and make the code answer each note",
                why=DECIDED,
            )
        if now == PASS:
            raise Refused(
                f"the newest design of {issue} already passed its review. {OWNER_WAIT}"
                " For any other design, build to it, or post a changed design as a new comment"
                " and call this again",
                why=DECIDED,
            )
        if now == GAP:
            raise Refused(
                f"the newest design of {issue} already has a gap verdict: post the fixed design"
                f" as a new comment opening with `{DESIGN_MARK}`, then call this again",
                why=DECIDED,
            )
        if now == OWNER:
            raise Refused(
                f"the owner let the newest design of {issue} go without a review (#4765)."
                f" {OWNER_WAIT} For any other design, build to it",
                why=DECIDED,
            )
        if limited:
            raise Refused(limit_text(issue), why=LIMIT, comment=number)
        # The last round names the design whose gaps it rechecks (#4463).
        before = earlier_gap(recorded, number)
        earlier = next(
            (
                str(item.get("url") or "")
                for item in comments
                if before and comment_id(str(item.get("url") or "")) == before
            ),
            "",
        )
        if before and not earlier:
            earlier = url.rsplit("#issuecomment-", 1)[0] + f"#issuecomment-{before}"
        return url, number, earlier

    async def owner_answer(self, issue: str, answer: str) -> dict | None:
        """Take the owner's answer at the series limit (#4765), as the agent sends it.
        ``more`` allows one more series for the newest design and answers ``None``: the
        review then starts as asked. ``skip`` records the design as ``OWNER`` and answers what
        to tell the agent. Raises ``Refused`` for an unknown answer, or a ``skip`` while no
        limit holds: outside it a design is reviewed."""
        if answer not in (MORE, SKIP):
            raise Refused(f'the owner\'s answer is "{MORE}" or "{SKIP}", not "{answer}"', 400)
        try:
            await self.target(issue)
        except Refused as exc:
            if exc.why != LIMIT:
                raise
            if answer == MORE:
                self.verdicts.allow(issue, exc.comment)
                return None
            self.verdicts.record(issue, exc.comment, OWNER)
            return {
                "issue": issue,
                "design": exc.comment,
                "recorded": OWNER,
                "next": _NEXT[OWNER],
            }
        if answer == SKIP:
            raise Refused(
                f"no series limit holds {issue}'s newest design now, so it is reviewed: call"
                " this again without the owner's answer",
            )
        return None

    def _sent_by_its_reviewer(self, issue: str, verdict: Verdict, token: str) -> None:
        if not self._reviews(token, issue, verdict.comment):
            raise Refused(
                f"only the reviewer the deck started on design comment {verdict.comment} of"
                f" {issue} gives its verdict; ask for one with `POST …/design-review`",
                403,
            )

    async def accept(self, issue: str, verdict: Verdict, token: str) -> str:
        """Whether the deck takes ``verdict``, sent by the chat whose session token is
        ``token``, before posting it: "" when it does, else why it drops it (about a design
        the agent has since replaced). Raises ``Refused`` (403) when the sender is not the
        reviewer the deck started on that design, or (503) when the comments are unread."""
        self._sent_by_its_reviewer(issue, verdict, token)
        return await self.stale(issue, verdict)

    def kept(self, issue: str, verdict: Verdict) -> str:
        """What the deck would record for ``verdict`` now (#4463, ``kept``): read before it is
        posted, so the post says the same."""
        return kept(self.verdicts.of(issue), verdict)

    def record(self, issue: str, verdict: Verdict, token: str) -> str:
        """Record ``verdict`` once posted: the gate's evidence. The same sender rule as
        ``accept``, so no path records a verdict ``accept`` would refuse. Returns what it
        recorded: a gap in the last round is notes (#4463, ``kept``)."""
        self._sent_by_its_reviewer(issue, verdict, token)
        recorded = kept(self.verdicts.of(issue), verdict)
        self.verdicts.record(issue, verdict.comment, recorded)
        return recorded

    def take(self, issue: str, verdict: Verdict) -> str:
        """Record the verdict ChatGPT posted on the issue itself (#4722), once: what is
        already recorded for that design comment stands, so a second run after a crash
        records, and answers, the same, and it never replaces the deck's own reviewer's.
        Who may send it is the router's check, against the chat it opened; here is no
        session token to ask. The deck's own reviewer never meets a ChatGPT verdict on its
        design: no chat opens on a design it is on (``reviewing``)."""
        recorded = self.verdicts.of(issue).get(verdict.comment)
        if not recorded:
            recorded = kept(self.verdicts.of(issue), verdict)
            self.verdicts.record(issue, verdict.comment, recorded)
        return recorded

    async def stale(self, issue: str, verdict: Verdict) -> str:
        """Why ``verdict`` is about no design the agent still stands by, or "". A verdict
        naming no design comment is refused before this. Raises ``Refused`` when unread."""
        design, comments = await self._newest(issue)
        number = comment_id(str((design or {}).get("url") or ""))
        if number == verdict.comment:
            return ""
        if verdict.comment in withdrawn_numbers(comments):
            return f"{issue}'s design comment {verdict.comment} was withdrawn for a narrow fix"
        return f"{issue}'s newest design is comment {number or 'unknown'}, not {verdict.comment}"
