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

Owner's choices (#4332): a deck step, not only a guide rule, with an independent reviewer;
started only by the agent's own mark (a check of the diff at ready would come after the code
is written); each verdict names the design comment it reviewed.

Assumed (#4332), looked at again after the first ten design rounds: the marker
``**Design:**``; the reviewer is the deck's Claude (or Codex, when that is the code reviewer)
whichever reviewer reviews the code. The design reviewer shares the code reviewer's
places (``review.max_reviewers``) and ledger.

**When every place is taken (#4342)** the request is not refused: ``ask`` raises ``Busy``, and
the Work router keeps it in its saved retry list (``work_router.PendingActions``, one
``design:<owner/repo#n>`` entry per issue), the same route as a code review that finds every
place taken: tried again with the same growing wait, dropped after a day. ``GET
…/design-review`` shows it as ``queued``. Chosen by the owner on PR #4344: no order of its
own, no place number.

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
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

#: How a design comment opens. A quote of it, or a mention mid-line, is not one.
DESIGN_MARK = "**Design:**"

PASS = "pass"
GAP = "gap"
#: The design passed with notes: round 2 still found gaps, which go to the code review as
#: notes to check instead of holding the code (#4463).
NOTES = "notes"
#: A design series gets this many reviews (#4463, the owner's choice).
MAX_ROUNDS = 2
#: The issue has no design comment.
NONE = "none"
#: Its newest design comment has no verdict after it.
UNREVIEWED = "unreviewed"
#: A reviewer the deck started is reading its newest design comment (``status`` only).
REVIEWING = "reviewing"
#: The request waits in the router's retry list for a reviewer place (#4342, ``status`` only).
QUEUED = "queued"

_DESIGN_RE = re.compile(r"(?m)^[ \t]*" + re.escape(DESIGN_MARK))
#: The verdict line as the reviewer sends it.
_VERDICT_RE = re.compile(
    r"(?i)^\s*/claude\s+Design review of\s+(?P<ref>[\w.-]+/[\w.-]+#\d+)\s*:\s*"
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
        )
    return None


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
    """Whether ``comment`` is a design comment: a line of its own opens with the mark."""
    return _DESIGN_RE.search(comment or "") is not None


def newest_design(comments: list[dict]) -> dict | None:
    """The newest design comment of ``comments`` (GitHub's comment objects, oldest first).
    The deck's post of a verdict quotes every line, so it is never one."""
    for comment in reversed(comments):
        if is_design(str(comment.get("body") or "")):
            return comment
    return None


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
    recorded for its number: a design whose number is unread stays unreviewed.
    """
    for index in range(len(comments) - 1, -1, -1):
        if is_design(comments[index]):
            number = comment_id(urls[index]) if index < len(urls) else ""
            return verdicts.get(number, UNREVIEWED) if number else UNREVIEWED
    return NONE


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


#: What a round-1 reviewer works through (#4463, assumed): a case outside it is no gap.
CHECKLIST = (
    "what runs at the same time (robots, processes, requests, timers) and which row, lock or"
    " file decides between them",
    "what a crash, a timeout and a retry do at each step: nothing left half done, nothing run"
    " twice",
    "for a large change, its parts and which one owns each row, file or step",
    "parts or lines of the design that contradict each other",
)


def brief(
    *,
    name: str,
    issue: str,
    design_url: str,
    base: str,
    request: str,
    token: str,
    earlier: str = "",
) -> str:
    """The first message of one design reviewer's chat. It opens like every reviewer's, so
    the chat list leaves it out (``agent_review.is_reviewer_prompt``). ``request`` is the
    reviewer's ``deck-request`` command (``agent_review.DECK_REQUEST``); ``token`` its own
    session token, which the deck asks of a verdict (a Codex shell has no variable for it).
    Its message file is named by the token's first 12 characters (#4468): a name no other
    reviewer uses, that does not leave the whole token in a listable ``/var/tmp``.

    ``earlier`` is the URL of the design whose review found the gaps this one answers: this
    is then the last round (#4463), which rechecks only those gaps and their fixes."""
    number = comment_id(design_url)
    listed = "\n".join(f"  {n}. {item}" for n, item in enumerate(CHECKLIST, 1))
    if earlier:
        scope = f"""- This is the last round: a design gets only two reviews (#4463). Read \
the design before it, {earlier}, and the deck's gap verdict after it. Check only those gaps: \
is each one fixed here, and does its fix open a hole of its own? Raise nothing else, and no \
small details.
- A gap you still find does not hold the code back. The deck records the design as passed \
with notes, the Work starts coding, and the code review checks the code for each note. So \
write each one so a code reviewer can check it in code: the case, and what must hold."""
        lines = f"""  - If every gap from round 1 is fixed, its first line is exactly \
`/claude Design review of {issue}: pass {number}`, and the next lines say in a few sentences \
what you checked.
  - Otherwise its first line is exactly `/claude Design review of {issue}: notes {number}`, \
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
rest in real code."""
        lines = f"""  - If the design holds, its first line is exactly \
`/claude Design review of {issue}: pass {number}`, and the next lines say in a few sentences \
what you checked.
  - Otherwise its first line is exactly `/claude Design review of {issue}: gap {number}`, and \
each next line is one gap: the line of the design it breaks, or that the design missed it, \
and the case that shows it."""
    return f"""You are AgentDeck's {name} reviewer. The deck started you to review the design \
of the Work {issue} before any code is written for it: the comment {design_url}. This \
directory is a throwaway checkout of `{base}`, the code as it is now, made for you; the deck \
removes it when you stop.

- Read the issue and that comment: `gh issue view {issue.partition("#")[2]} --repo \
{issue.partition("#")[0]} --comments`. Read the code here only as far as the design needs. \
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
- Finish by saying in a few sentences what you found and why you gave that verdict.
"""


#: Why ``Refused`` (#4342): the router drops a waiting request only on ``DECIDED`` or
#: ``NO_DESIGN``, and tries again on every other.
DECIDED = "decided"
NO_DESIGN = "none"
UNREAD = "unread"
OTHER = "other"


class Refused(Exception):
    """Why no design review started, with the HTTP status the deck answers and ``why`` it
    was refused (``DECIDED``, ``NO_DESIGN``, ``UNREAD`` or ``OTHER``)."""

    def __init__(self, detail: str, status: int = 409, why: str = OTHER) -> None:
        super().__init__(detail)
        self.status = status
        self.why = why


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


Read = Callable[[str], Awaitable[list[dict] | None]]


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
    PASS: "the design passed: build to it (a redesign answering a design finding, once the"
    " owner approved it, #4476)",
    NOTES: "the design passed with notes after its two review rounds (#4463): build to it,"
    " and make the code answer each note; the code review checks the code against them",
    GAP: f"the review found a gap: post the fixed design as a new `{DESIGN_MARK}` comment,"
    " then `POST …/design-review`",
}


async def status(
    issue: str,
    verdicts: Verdicts,
    reviewer: Callable[[str], dict | None],
    read: Read = read_comments,
    queued: dict | None = None,
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
        elif now == UNREVIEWED and queued:
            now = QUEUED
    step = _NEXT[now]
    if now == UNREVIEWED and found:
        step = (
            "the reviewer started on this design stopped without a verdict:"
            " `POST …/design-review` starts a new one"
        )
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
    ) -> None:
        self._start = start
        self.verdicts = verdicts
        self._read = read
        # ``reviews(token, issue, comment)``: whether ``token`` is the reviewer the deck
        # started on that design (``agent_review.reviews_design``).
        self._reviews = reviews

    async def _newest(self, issue: str) -> tuple[dict | None, list[dict]]:
        comments = await self._read(issue)
        if comments is None:
            raise Refused(f"the comments of {issue} could not be read", 503, UNREAD)
        return newest_design(comments), comments

    async def missing(self, issue: str) -> str:
        """What holds a code review of ``issue``'s pull requests back (#4332 review): the
        design gap, or "" when the newest design passed or there is none. Unread comments
        hold it too, so no start skips the design gate (``WorkRouterService._design_gap``)."""
        comments = await self._read(issue)
        if comments is None:
            return f"the comments of {issue} could not be read to check its design review"
        bodies = [str(item.get("body") or "") for item in comments]
        urls = [str(item.get("url") or "") for item in comments]
        return gap(state(bodies, urls, self.verdicts.of(issue)))

    async def ask(self, issue: str, checkout: str = "") -> dict:
        """Start the review of ``issue``'s newest design (``POST
        /api/sessions/<key>/design-review``); ``checkout`` is the asking chat's directory: its
        repository is the code reviewed, whichever repository holds the issue. Raises
        ``Refused`` when there is nothing to review or no reviewer could start, and ``Busy``
        when every reviewer place is taken (the router then keeps it and tries again, #4342)."""
        from .review_refs import ReviewBusy, ReviewError

        design, comments = await self._newest(issue)
        if design is None:
            raise Refused(
                f"{issue} has no design comment: post one opening with `{DESIGN_MARK}` on its"
                " own line, then call this again",
                why=NO_DESIGN,
            )
        url = str(design.get("url") or "")
        number = comment_id(url)
        if not number:
            raise Refused(f"the newest design comment of {issue} has no readable URL", 503)
        recorded = self.verdicts.of(issue)
        now = recorded.get(number)
        if now == NOTES:
            raise Refused(
                f"the newest design of {issue} passed with notes after its two review rounds"
                " (#4463): build to it and make the code answer each note; post no new design"
                " for the notes",
                why=DECIDED,
            )
        if now == PASS:
            raise Refused(
                f"the newest design of {issue} already passed its review: build to it, or post"
                " a changed design as a new comment and call this again",
                why=DECIDED,
            )
        if now == GAP:
            raise Refused(
                f"the newest design of {issue} already has a gap verdict: post the fixed design"
                f" as a new comment opening with `{DESIGN_MARK}`, then call this again",
                why=DECIDED,
            )
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

    async def stale(self, issue: str, verdict: Verdict) -> str:
        """Why ``verdict`` is about no design the agent still stands by, or "". A verdict
        naming no design comment is refused before this. Raises ``Refused`` when unread."""
        design, _comments = await self._newest(issue)
        number = comment_id(str((design or {}).get("url") or ""))
        if number == verdict.comment:
            return ""
        return f"{issue}'s newest design is comment {number or 'unknown'}, not {verdict.comment}"
