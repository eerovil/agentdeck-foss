"""A Work's one status line, derived from what the deck already holds (#2895 step 3, D7).

For a Work the deck runs itself (a ``works_router`` project, no Poller card), the status is
not a column a Poller wrote: it is read, first match wins, from the owning chat and the
Work's own facts. Nothing here is stored and nothing here asks the agent to call a tool.

1. the agent asked through its question box, or its provider waits on a permission
   (**Needs you — Allow Bash?**, #3138): **Needs you** — the question;
2. a turn is running: **Working** — its latest progress line; one the deck marks stalled:
   **Needs you — no progress since 13:02**; a message the deck handed to the chat after its
   turn ended (a wake-on firing) counts as a turn starting for ``HANDOFF_GRACE`` from its due
   time, claim or delivery: **Working — starting on the message it was just sent** (#3084);
3. the turn died and the runtime's revival did not bring it back:
   **Needs you — the agent stopped unexpectedly · 13:02**; one whose last line is the CLI's
   expired sign-in reads **Needs you — the alt account's sign-in expired · 13:02**, a
   person's to sign in again and never a bug (#3354). A turn a runtime restart or crash
   cut off is not that (#3008). While the runtime still owes it a revival (its boot's scan
   has not run, or a full account turned the revival away and it keeps trying) it reads
   **Waiting on the runtime to revive the agent · cut off 13:02**, never asked for a next
   step and never a bug. Any other cut-off turn — a worker that died alone while the
   runtime stayed up, one the loop guard stopped — gets nothing more from the runtime:
   **Needs you — the agent's process stopped mid-turn · 13:02**, a person's to start
   again, and not reported as a bug. Past those, a turn that ended with the chat marked
   waiting for a person (``PUT …/attention``, as the Mac runner's wrapper does) reads as a
   question, never as an ending (#3106): **Needs you — approve the Mac command** when its
   last message links the Mac's approval page, which the Work's Open then opens, else
   **Needs you —** the question in its last message;
4. the issue closed: **Issue closed — finish it?** (#2944, #2956), with what the agent's live
   check of the merged change found (**finish it? · checked live: the card shows the line**,
   #3141). While the deck has asked for that check and no result came, it reads **Live
   checking — waiting on …, until 14:20** on the agent's wake-on; with no wait its turn and the
   deck's ask for a next step read **Live checking — …** too (#3578), and an ending nobody answered
   **Needs you — no live check recorded · …**; a failed check is **Needs you — live check
   failed: …**, and one with part of the merge not running yet **Needs you — not live yet: …**
   (#3220), offering no Finish. Only once no registered pull request is open: with one still
   open the rules below read it as usual and the line ends **· issue closed** (#3187), since
   another chat may close the issue while this Work's pull request still waits for its review
   or Merge;
5. a registered pull request is ready and a review chat about its head is still open:
   **In review — since 20:17**, from the chat's opening; one whose reviewer gave up
   while the deck opens the Work's next chat reads **In review — the reviewer gave
   up at 08:32, opening the next review** (#3192); once no reviewer has run on
   it for longer than the fallback reviewer's own retry window, the deck starts a reviewer on
   it once (**In review — waiting for a reviewer since 20:37**, #2960); quiet again
   after that, **Needs you — no reviewer since 20:37** (#2943), an AgentDeck bug,
   since every ready head is reviewed by itself; **the reviewer stopped since 20:37**
   when the review chat is labelled blocked, so a reviewer ran and says why there (#2974).
   Before all of these, an open pull request whose newest review chat ended with the reviewer
   escalating its current head to the owner reads **Needs you — the reviewer escalated it
   to you (review-chats#5914) · 20:19** (#3216): the agent was told to ask the reviewer's
   question in its question box, which rule 1 shows instead once it does;
6. the deck's merge queue holds one of its pull requests (#3022), behind another one:
   **In merge queue — 3rd of 5**; first in line and waiting on its re-test:
   **In merge queue — first of 5 · re-testing after master moved, round 2**. First in
   line with nothing running is not a rest: the agent owes that re-test, so the rules below
   apply;
7. a wake-on or a queued test run is open: **Waiting on …, until 14:20**; a wake-on for
   the Work's own pull request merging is left out once rule 8 holds, since that merge is
   what Ready for merge asks a person for (#3003). When the agent's only wait is its own pull
   request merging, the line says what that pull request waits for, from the release's last
   answer (#3097): **Waiting on its pull request — no test run started, until 14:20**,
   **— review asked for changes at 15:11, sent to the agent** or **… not sent: no chat owns
   it**. A hold only a person lifts (the spec-only label) is **Needs you — its pull
   request held: spec-only label** at once; any other that nothing moved for ``PR_STALE`` (no
   push, no review, no turn) is **Needs you — … · nothing since 13:02**, Needs retry;
8. every open pull request is reviewed and tested and only a person's Merge is missing (the
   deck's release says so): **Ready for merge — reviewed clean 13:26
   (review-chats#5166)** (#2960, #2992), linking that review chat. No line names a pull
   request (#3720): the Work page lists them;
9. otherwise the agent ended its turn without a next step: no question, no wait, no review,
   no closed issue. Its pull requests all merged with the issue still open is one of these:
   the agent decides whether work is left and closes the issue if not. That is never a
   resting state (#2960). Once the ending has rested ``ENDING_SETTLE`` (a person who stopped
   the turn to type gets to send their message first, #2965), the deck asks the agent once
   for its next step (**Working — asked
   the agent for its next step**); if the next turn ends the same way, or nothing ran within
   ``NUDGE_GRACE``, **Needs you — the agent ended without a next step · last said "…" ·
   13:02**, which is reported as a bug. Where the deck does not ask (``asks`` off, as on
   staging), the same line stands without a request or a report. A schedule definition (an
   issue labelled ``scheduled``, which stays open on purpose) is never asked: it reads
   **Scheduled — files a new card on its schedule** (#2998). Nor is a spec-only Work whose
   agent posted its spec on the issue, with no pull request open and nobody commenting since:
   building waits on a person's approval, so it reads **Needs you — approve the spec · posted
   06:35**, linking the spec comment (#3247). Nor is a Work whose issue carries the ``parked``
   label with no pull request open: a person asked its agent to leave it until they write, so
   it reads **Parked — waits for your message** (#3560).

Since #2944 this is the one status of such a Work: the front page's section, the operator's
category and the buttons follow it too (``work_story.work_status``). A time not on today's
date carries its date (``Oct 11 20:49``).

``deadline`` is the one moment the line changes by itself (a wait's "until"), for the
refresher's deadline rather than a timer (ADR 0007).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

QUESTION = "question"
WORKING = "working"
STALLED = "stalled"
DIED = "died"
CLOSED = "closed"
REVIEW = "review"
REVIEW_STUCK = "review_stuck"
REVIEW_QUEUED = "review_queued"  # quiet, and the deck's one reviewer start is still to come
WAITING = "waiting"
QUEUED = "queued"  # in the deck's merge queue (#3022)
READY = "ready"  # reviewed and tested, a person's Merge is all that is missing (#2960)
ENDED = "ended"  # the turn ended without a next step; the deck is about to ask once (#2960)
NUDGED = "nudged"  # the deck asked; the agent's answering turn has not started yet
UNFINISHED = "unfinished"  # it ended without a next step again, or nothing ran
FINISHED = "finished"
REVIVING = "reviving"  # a restart cut the turn off; the runtime's revival answers it (#3008)
CUT_OFF = "cut_off"  # the process died mid-turn and no revival is coming
LOGIN_EXPIRED = "login_expired"  # the turn failed because its account's sign-in expired
SCHEDULED = "scheduled"  # a schedule definition, open on purpose: no next step (#2998)
PR_HELD = "pr_held"  # its own pull request is held by something only a person lifts (#3097)
PR_STUCK = "pr_stuck"  # its own pull request's hold has not moved for ``PR_STALE`` (#3097)
LIVE_FAILED = "live_failed"  # the agent's live check of its merged change failed (#3141)
NOT_LIVE = "not_live"  # merged, but part of it is not running yet (#3220)
REVIEW_ESCALATED = "review_escalated"  # the reviewer handed the review to the owner (#3216)
SPEC_WAIT = "spec_wait"  # a spec-only Work's spec is posted; a person approves it (#3247)
PARKED = "parked"  # a person asked the agent to leave it until they write (#3560)

#: The issue label an agent puts on a Work a person asked it to leave until they write.
PARKED_LABEL = "parked"

#: How long an asked agent may take to start answering before the Work needs a person.
NUDGE_GRACE = timedelta(minutes=10)

#: How long an ending rests before the deck asks about it (#2965). A person who stops a
#: turn to type a message of their own ends it too, and asking at once raced their message
#: and then judged their turn's ending as the answer to the deck's request.
ENDING_SETTLE = timedelta(minutes=3)

#: How long a message just delivered to the chat counts as its next turn starting (#3084). The
#: turn it starts shows as running a few seconds later, and a fired wake-on is already gone.
HANDOFF_GRACE = timedelta(minutes=2)

#: How long a wait on the Work's own pull request may go with nothing moving (no push, no
#: review, no test run, no turn) before it is stuck rather than waiting (#3097). A review takes
#: minutes and a test run under an hour.
PR_STALE = timedelta(hours=3)

#: The longest a quoted last sentence runs before it is cut.
MAX_QUOTE = 140

_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\n")
#: A markdown link, shown as its text alone: a status line is plain words (#2944).
_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+\)")
#: What a provider says while it works, which says nothing about the work (#2944).
_GENERIC_PROGRESS = frozenset({"using tools", "working", "thinking"})


@dataclass(frozen=True)
class DerivedInputs:
    """What the status reads, gathered by the caller from the owning chat and the Work."""

    question: str = ""  # the open question box's text, "" when none
    running: bool = False  # a turn is in flight (working or stalled)
    stalled: bool = False
    progress: str = ""  # the running turn's latest progress line
    #: A message was handed to the chat after its turn ended, counting until ``handed_until``
    #: (#3084).
    handed: bool = False
    handed_until: datetime | None = None
    last_progress: datetime | None = None
    died: bool = False  # the turn died and revival did not bring it back
    died_at: datetime | None = None
    #: When the deck's own retry of a turn that died on a full account or a lost connection is
    #: due, while that retry is still scheduled (#3305): the death then reads as a wait.
    retry_at: datetime | None = None
    #: The account a died turn failed to sign in to ("alt"), else "" (#3354): the CLI answered
    #: "Failed to authenticate: OAuth session expired", which a person's sign-in fixes.
    login_expired: str = ""
    #: The turn's process died mid-turn with nothing running since (#3008), when the turn
    #: was last fed, and whether the runtime still owes it a revival.
    cut_off: bool = False
    cut_off_at: datetime | None = None
    reviving: bool = False
    #: The ended turn's last text when the chat marked itself waiting for a person (#3106),
    #: and the Mac approval page it links, if any.
    waiting_on_you: str = ""
    approval_href: str = ""
    closed: bool = False  # the issue closed and nobody finished the Work yet
    #: A registered pull request is still open: a closed issue then reads through the pull
    #: request's rules rather than as Issue closed (#3187).
    pr_open: bool = False
    #: The deck asked the agent to check its merged change live and no result came yet; else
    #: the result it recorded (``verified``, ``failed``, ``nothing_to_check``, ``not_live``) and
    #: its line (#3141).
    live_pending: bool = False
    #: A registered pull request merged and none is open (#3578): the Work is in its live
    #: check until the agent records one, asked or not.
    merged: bool = False
    live_result: str = ""
    live_line: str = ""
    finished: bool = False  # a person finished the Work (D15)
    review_pr: int | None = None  # a ready pull request an open review chat is about
    review_since: datetime | None = None  # when that review chat opened
    #: The last sign of a reviewer on it (the chat opening, a reviewer start, the chat's own
    #: activity), and the moment after which its quiet means nobody is coming.
    review_active: datetime | None = None
    review_stuck_at: datetime | None = None
    review_chat: int | None = None  # that review chat's number, for the deck's one retry
    review_retried: bool = False  # the deck already started its one reviewer on that chat
    #: That chat is labelled ``blocked`` or ``needs-more-info``: a reviewer ran and stopped,
    #: rather than none coming (#2974).
    review_held: bool = False
    #: Why the reviewer stopped, from its ``Blocked:`` comment (#3356); "" when none said.
    review_held_why: str = ""
    #: The deck's own reviewer confirmed it failed: a ``Blocked:`` reason written after it
    #: started (#3356). A person's hold (``needs-more-info``, a bare ``blocked``) is not one.
    review_stopped_by_ours: bool = False
    #: The review chat gave up and the deck opens the Work's next one, trying until then
    #: (#3192): still in review, never the agent's ending.
    review_reopen_until: datetime | None = None
    #: ChatGPT was not started on that review chat, and why (#3326): the deck hands it to its
    #: Claude or Codex reviewer, and the owner learns the ChatGPT browser needs them.
    review_start_failed: str = ""
    #: The reviewer escalated an open pull request's current head to the owner (#3216): that
    #: pull request, when, and the review chat.
    escalated_pr: int | None = None
    escalated_at: datetime | None = None
    escalated_chat: str = ""
    escalated_url: str = ""  # the escalation comment itself, where its question is
    waits: tuple[tuple[str, datetime | None], ...] = ()  # (what, until)
    #: Wake-ons on the Work's own pull requests merging (#3097), apart from ``waits``: what the
    #: pull request itself waits for, as the release last said it (``hold``), whether only a
    #: person lifts that, a reviewer's findings since (when, and whether its chat heard them),
    #: and the last time anything moved.
    pr_waits: tuple[tuple[str, datetime | None], ...] = ()
    pr_hold: str = ""
    pr_hold_person: bool = False
    pr_findings_at: datetime | None = None
    pr_findings_told: bool = False
    pr_quiet_since: datetime | None = None
    last_text: str = ""  # the ended turn's final text
    ended_at: datetime | None = None
    #: The pull request a Ready for merge line names, when the release says only a person's
    #: Merge is missing (#2960). A line never names it (#3720).
    ready_pr: int | None = None
    #: The clean review behind it (#2992): when the pass was recorded, and the review chat
    #: (``owner/repo#n``) that passed it, ``""`` when none is known.
    ready_reviewed_at: datetime | None = None
    ready_review_chat: str = ""
    #: The deck's merge queue (#3022): the Work's pull request in it, its repository when it
    #: is not the issue's own, its place (1 is first), the line's length, and how many times
    #: the deck brought it up to its base while first.
    queue_pr: int | None = None
    queue_place: int = 0
    queue_size: int = 0
    queue_round: int = 0
    #: The deck's one request for a next step (#2960): when it was sent, and the turn end it
    #: answered. ``asks`` says the deck asks at all (off on staging).
    nudged_at: datetime | None = None
    nudged_for: datetime | None = None
    asks: bool = False
    pr: int | None = None  # the Work's pull request, if any; a line never names it (#3720)
    chat_href: str = ""  # the owning chat's page, where its question is answered
    #: The issue is a schedule definition (``scheduled`` label): rule 9 never applies (#2998).
    scheduled: bool = False
    #: A spec-only Work's agent posted its spec on the issue, with no pull request open and no
    #: comment since (#3247): when, and the comment. Rule 9 never applies; a person approves.
    spec_posted_at: datetime | None = None
    spec_url: str = ""
    #: The issue carries ``PARKED_LABEL`` (#3560): with no pull request open, rule 9 never
    #: applies; the next person's message starts the agent again.
    parked: bool = False


@dataclass(frozen=True)
class DerivedStatus:
    kind: str
    line: str
    at: datetime | None = None
    #: The line without its headline, built from the same parts rather than cut back out of
    #: ``line`` (#2956): a Stopped line naming a pull request is not only a time.
    summary: str = ""
    deadline: datetime | None = None
    pr: int | None = None
    chat_href: str = ""  # the owning chat's page, "" when none is known
    review_chat: int | None = None  # the quiet review chat the deck starts a reviewer on
    review_held: bool = False  # a stuck review whose reviewer stopped in the chat (#2974)
    #: A stuck review the reviewers failed (#3356): ours confirmed it with ``Blocked:``, or no
    #: reviewer came even after the deck's retry. Not a chat held for a person's answer. The
    #: one signal the Work chat is told on.
    review_failed: bool = False
    #: The review chat (``owner/repo#n``) a Ready for merge line links (#2992).
    review_ref: str = ""
    #: An ending AgentDeck reports as a bug: the agent ended without a next step twice (#2960).
    bug: bool = False
    #: The Mac approval page a chat waiting on the boss's tap linked (#3106), "" otherwise.
    approval_href: str = ""
    #: Where the line itself links, ahead of ``review_ref``'s chat: the reviewer's escalation
    #: comment, which holds its question and options (#3216).
    summary_url: str = ""
    #: Merged, and the agent checks it live (#3578): the line reads **Live checking — …** and
    #: the Work has a status and a front-page group of its own. ``kind`` stays the rule that
    #: matched, so the nudge and the bug report read the same Work as before.
    live_checking: bool = False


def clock(value: datetime | None) -> str:
    """``13:02`` in the deck's local time, ``Oct 11 20:49`` on another day; ``""`` for none."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    if local.date() == datetime.now(local.tzinfo).date():
        return local.strftime("%H:%M")
    return f"{local.strftime('%b')} {local.day} {local.strftime('%H:%M')}"


def first_sentence(text: str) -> str:
    """The first sentence of ``text``, on one line, cut at ``MAX_QUOTE`` characters."""
    text = _LINK.sub(r"\1", (text or "").strip())
    if not text:
        return ""
    head = _SENTENCE_END.split(text, maxsplit=1)[0].strip()
    head = " ".join(head.split())
    if len(head) > MAX_QUOTE:
        head = head[: MAX_QUOTE - 1].rstrip() + "…"
    return head


def _sentences(text: str) -> list[str]:
    text = _LINK.sub(r"\1", (text or "").strip())
    return [" ".join(part.split()) for part in _SENTENCE_END.split(text) if part.strip()]


def _cut(head: str) -> str:
    return head if len(head) <= MAX_QUOTE else head[: MAX_QUOTE - 1].rstrip() + "…"


def question_sentence(text: str) -> str:
    """The question itself: the first sentence that asks one, else the first (#2944).

    A question form often opens with what the agent found ("The alert is real evening load,
    not a broken rule."), which is not what it asks.
    """
    sentences = _sentences(text)
    asked = next((sentence for sentence in sentences if sentence.endswith("?")), "")
    return _cut(asked) if asked else first_sentence(text)


def progress_sentence(text: str) -> str:
    """A running turn's latest progress sentence, or "" for a generic label (#2944)."""
    sentences = _sentences(text)
    if not sentences or sentences[-1].casefold().rstrip(".…") in _GENERIC_PROGRESS:
        return ""
    return _cut(sentences[-1])


#: The statuses a merged Work's pending live check speaks over (#3141, D6, #3578): the agent's
#: own turn, the deck asking it for a next step, and a wait it registered.
_LIVE_WORKING = frozenset({WORKING, ENDED, NUDGED, WAITING})
#: The words each of those lines opens with, cut so the line reads **Live checking — …**.
_LIVE_PREFIXES = ("Working — ", "Working", "Waiting on ")


def _live_rest(line: str) -> str:
    """What a working or waiting line says after its headline: ``waiting on …`` keeps its verb."""
    for prefix in _LIVE_PREFIXES:
        if line.startswith(prefix):
            rest = line.removeprefix(prefix)
            return f"waiting on {rest}" if prefix == "Waiting on " else rest
    return line


def _checking_live(inputs: DerivedInputs) -> bool:
    """Merged, no pull request open and no live check recorded for it yet (#3578).

    The deck's ask is the sure sign; a merge with no result is one too while the issue is
    open. A merge the agent never registered is unknown here, so it does not count.
    """
    if inputs.pr_open:
        return False
    return inputs.live_pending or (inputs.merged and not inputs.live_result and not inputs.closed)


def derived_status(
    inputs: DerivedInputs, *, now: datetime, clock: Callable[[datetime | None], str] = clock
) -> DerivedStatus:
    """The Work's status line: the first rule of D7 that matches.

    While a merged Work's live check is pending (#3141, D6, #3578), a turn, the ask for a next
    step or a wait reads **Live checking — …** rather than **Working — …** or **Waiting on …**,
    and an ending nobody answered on a closed issue says the check was never recorded. The
    rules, nudge and bug report are unchanged: they read ``kind``.
    """
    status = _first_rule(inputs, now=now, clock=clock)
    if inputs.closed and inputs.pr_open and status.kind != FINISHED:
        # Its pull request still has a step ahead of it (#3187): say the issue closed after.
        return replace(
            status,
            line=f"{status.line} · issue closed",
            summary=" · ".join(part for part in (status.summary, "issue closed") if part),
        )
    if status.live_checking:
        return status
    if _checking_live(inputs) and status.kind in _LIVE_WORKING:
        rest = _live_rest(status.line)
        return replace(
            status,
            line="Live checking" + (f" — {rest}" if rest else ""),
            summary=rest,
            live_checking=True,
        )
    if not (inputs.closed and inputs.live_pending):
        return status
    if status.kind == UNFINISHED:
        summary = "no live check recorded · " + status.summary
        return replace(status, line=f"Needs you — {summary}", summary=summary)
    return status


def _first_rule(
    inputs: DerivedInputs, *, now: datetime, clock: Callable[[datetime | None], str]
) -> DerivedStatus:
    """The first rule of D7 that matches."""
    if inputs.finished and not (inputs.question or inputs.running):
        # A finished Work stays finished until a message wakes it (D15): an old failed turn
        # or its last words never bring it back.
        return DerivedStatus(FINISHED, "Finished")
    href = inputs.chat_href
    # A closed issue with a pull request still open follows the pull request (#3187).
    closed = inputs.closed and not inputs.pr_open
    if inputs.question:
        summary = question_sentence(inputs.question)
        return DerivedStatus(
            QUESTION, f"Needs you — {summary}", summary=summary, chat_href=href
        )
    if inputs.running and inputs.stalled:
        since = clock(inputs.last_progress)
        summary = f"no progress since {since}" if since else "no progress"
        return DerivedStatus(
            STALLED, f"Needs you — {summary}", at=inputs.last_progress, summary=summary,
            chat_href=href,
        )
    if inputs.running:
        progress = progress_sentence(inputs.progress)
        line = f"Working — {progress}" if progress else "Working"
        return DerivedStatus(
            WORKING, line, at=inputs.last_progress, summary=progress, chat_href=href
        )
    if inputs.handed and inputs.handed_until is not None and now < inputs.handed_until:
        summary = "starting on the message it was just sent"
        return DerivedStatus(
            WORKING, f"Working — {summary}", summary=summary, deadline=inputs.handed_until,
            chat_href=href,
        )
    if inputs.cut_off and not inputs.died:
        return _cut_off(inputs, clock=clock)
    if inputs.died and inputs.retry_at is not None:
        # The account's limit or the connection stopped it, and the deck retries it at the
        # reset by itself (#3305): nobody needs to start it again, and it is not a bug.
        until = _aware(inputs.retry_at)
        summary = f"the usage limit to reset, until {clock(until)}"
        return DerivedStatus(
            WAITING, f"Waiting on {summary}", deadline=until, summary=summary, chat_href=href
        )
    if inputs.died and inputs.login_expired:
        # The account's sign-in expired, not the deck (#3354): a person signs in again.
        when = clock(inputs.died_at)
        summary = f"the {inputs.login_expired} account's sign-in expired" + (
            f" · {when}" if when else ""
        )
        return DerivedStatus(
            LOGIN_EXPIRED, f"Needs you — {summary}", at=inputs.died_at, summary=summary,
            chat_href=href,
        )
    if inputs.died:
        when = clock(inputs.died_at)
        summary = "the agent stopped unexpectedly" + (f" · {when}" if when else "")
        return DerivedStatus(
            DIED, f"Needs you — {summary}", at=inputs.died_at, summary=summary, chat_href=href
        )
    if inputs.approval_href:
        summary = "approve the Mac command"
        return DerivedStatus(
            QUESTION, f"Needs you — {summary}", summary=summary, chat_href=href,
            approval_href=inputs.approval_href,
        )
    if inputs.waiting_on_you:
        summary = question_sentence(inputs.waiting_on_you)
        return DerivedStatus(
            QUESTION, f"Needs you — {summary}", summary=summary, chat_href=href
        )
    if closed and inputs.live_result == "failed":
        # The agent reopens the issue and fixes it; still closed, a person decides (#3141).
        summary = inputs.live_line
        return DerivedStatus(LIVE_FAILED, f"Needs you — {summary}", summary=summary, chat_href=href)
    if closed and (inputs.live_pending or inputs.live_result == "not_live") and inputs.waits:
        # The agent waits on the deploy to check its merge live (#3141, D6): its own words,
        # not a Finish nobody can judge yet. With no wait the rules below ask it for a step.
        # A ``not_live`` result with a wait it registered is the same wait, not a person's
        # (#3297): the deploy runs by itself and the wake-on brings the agent back.
        timed = [(what, until) for what, until in inputs.waits if until is not None]
        what, until = min(timed, key=lambda item: item[1]) if timed else inputs.waits[0]
        summary = f"waiting on {what}" + (f", until {clock(until)}" if until else "")
        return DerivedStatus(
            WAITING, f"Live checking — {summary}", deadline=until, summary=summary,
            chat_href=href, live_checking=True,
        )
    if closed and inputs.live_result == "not_live":
        # Part of the merge runs nowhere yet and nothing will wake the agent (#3220): someone
        # deploys it by hand. Not a Finish until a later check says so.
        summary = inputs.live_line
        return DerivedStatus(NOT_LIVE, f"Needs you — {summary}", summary=summary, chat_href=href)
    if closed and not inputs.live_pending:
        summary = "finish it?" + (f" · {inputs.live_line}" if inputs.live_line else "")
        return DerivedStatus(
            CLOSED, f"Issue closed — {summary}", summary=summary, chat_href=href
        )
    if inputs.escalated_pr is not None:
        # Neither findings nor a pass: the reviewer asks the owner and reviews nothing more by
        # itself, so a review wait or an ending is not what the Work waits on (#3216). The
        # agent is asked to put the question in its question box, which reads first above.
        when = clock(inputs.escalated_at)
        summary = (
            "the reviewer escalated it to you"
            + (f" ({inputs.escalated_chat.rpartition('/')[2]})" if inputs.escalated_chat else "")
            + (f" · {when}" if when else "")
        )
        return DerivedStatus(
            REVIEW_ESCALATED, f"Needs you — {summary}", at=inputs.escalated_at, summary=summary,
            pr=inputs.escalated_pr, chat_href=href, review_ref=inputs.escalated_chat,
            summary_url=inputs.escalated_url,
        )
    reopen_until = _aware(inputs.review_reopen_until)
    if reopen_until is not None and inputs.review_pr is not None and now < reopen_until:
        when = clock(inputs.review_active)
        summary = "the reviewer gave up" + (
            f" at {when}" if when else ""
        ) + ", opening the next review"
        return DerivedStatus(
            REVIEW, f"In review — {summary}", at=inputs.review_active, deadline=reopen_until,
            summary=summary, pr=inputs.review_pr,
        )
    if inputs.review_pr is not None and reopen_until is None:
        pr = inputs.review_pr
        stuck_at = inputs.review_stuck_at
        if stuck_at is not None and now >= stuck_at:
            last = clock(inputs.review_active)
            if not inputs.review_retried:
                # Not a person's yet: the deck starts a reviewer on it first (#2960).
                summary = "waiting for a reviewer" + (f" since {last}" if last else "")
                return DerivedStatus(
                    REVIEW_QUEUED, f"In review — {summary}", at=inputs.review_active,
                    summary=summary, pr=pr, review_chat=inputs.review_chat,
                )
            # A reviewer that ran and gave up says why in its chat (#2974): a verdict the deck
            # never answered, say. Only quiet with no such stop means nobody came.
            what = "the reviewer stopped" if inputs.review_held else "no reviewer"
            summary = what + (f" since {last}" if last else "")
            if inputs.review_held and inputs.review_held_why:
                summary += f": {inputs.review_held_why}"
            return DerivedStatus(
                REVIEW_STUCK, f"Needs you — {summary}", at=inputs.review_active,
                summary=summary, pr=pr, review_chat=inputs.review_chat,
                review_held=inputs.review_held,
                review_failed=inputs.review_stopped_by_ours or not inputs.review_held,
            )
        since = clock(inputs.review_since)
        parts = [f"since {since}"] if since else []
        if inputs.review_start_failed:
            parts.append(f"ChatGPT was not started: {inputs.review_start_failed}")
        elif inputs.review_held and inputs.review_held_why:
            # Not stuck yet: the deck hands the chat to its own reviewer (#3356).
            parts.append(f"the last reviewer stopped: {inputs.review_held_why}")
        summary = " · ".join(parts)
        return DerivedStatus(
            REVIEW, _headed("In review", summary), at=inputs.review_since, deadline=stuck_at,
            summary=summary, pr=pr,
        )
    if inputs.queue_pr is not None and (
        inputs.queue_place > 1 or inputs.waits or inputs.pr_waits
    ):
        summary = f"{_ordinal(inputs.queue_place)} of {inputs.queue_size}"
        if inputs.queue_place <= 1 and inputs.queue_round:
            summary += f" · re-testing after master moved, round {inputs.queue_round}"
        return DerivedStatus(
            QUEUED, f"In merge queue — {summary}", summary=summary, pr=inputs.queue_pr,
            chat_href=href,
        )
    if inputs.waits:
        timed = [(what, until) for what, until in inputs.waits if until is not None]
        what, until = min(timed, key=lambda item: item[1]) if timed else inputs.waits[0]
        # A wait keeps its whole line as its summary ("Waiting on its tests").
        line = f"Waiting on {what}" + (f", until {clock(until)}" if until else "")
        return DerivedStatus(WAITING, line, deadline=until, summary=line)
    if inputs.pr_waits:
        return _pr_wait(inputs, now=now, clock=clock)
    if inputs.ready_pr is not None:
        summary = ""
        if inputs.ready_reviewed_at is not None or inputs.ready_review_chat:
            # What made it ready, so a reviewed Ready reads apart from any other (#2992).
            when = clock(inputs.ready_reviewed_at)
            summary = "reviewed clean" + (f" {when}" if when else "")
            if inputs.ready_review_chat:
                summary += f" ({inputs.ready_review_chat.rpartition('/')[2]})"
        return DerivedStatus(
            READY, _headed("Ready for merge", summary), summary=summary, pr=inputs.ready_pr,
            chat_href=href, review_ref=inputs.ready_review_chat,
        )
    if inputs.spec_posted_at is not None:
        when = clock(inputs.spec_posted_at)
        summary = "approve the spec" + (f" · posted {when}" if when else "")
        return DerivedStatus(
            SPEC_WAIT, f"Needs you — {summary}", at=inputs.spec_posted_at, summary=summary,
            chat_href=href, summary_url=inputs.spec_url,
        )
    if inputs.scheduled:
        summary = "files a new card on its schedule"
        return DerivedStatus(
            SCHEDULED, f"Scheduled — {summary}", summary=summary, chat_href=href
        )
    if inputs.parked and not inputs.pr_open:
        summary = "waits for your message"
        return DerivedStatus(PARKED, f"Parked — {summary}", summary=summary, chat_href=href)
    return _ended(inputs, now=now, clock=clock)


def _pr_wait(
    inputs: DerivedInputs, *, now: datetime, clock: Callable[[datetime | None], str]
) -> DerivedStatus:
    """Rule 7 for a Work whose only wait is its own pull request merging (#3097)."""
    timed = [(what, until) for what, until in inputs.pr_waits if until is not None]
    what, until = min(timed, key=lambda item: item[1]) if timed else inputs.pr_waits[0]
    href = inputs.chat_href
    hold = inputs.pr_hold
    if inputs.pr_findings_at is not None:
        when = clock(inputs.pr_findings_at)
        hold = "review asked for changes" + (f" at {when}" if when else "") + (
            ", sent to the agent" if inputs.pr_findings_told else ", not sent: no chat owns it"
        )
    if inputs.pr_hold_person and hold:
        summary = f"{what} {hold}"
        return DerivedStatus(PR_HELD, f"Needs you — {summary}", summary=summary, chat_href=href)
    said = f"{what} — {hold}" if hold else what
    quiet = _aware(inputs.pr_quiet_since)
    stale = quiet + PR_STALE if quiet is not None else None
    if stale is not None and now >= stale:
        summary = f"{said} · nothing since {clock(quiet)}"
        return DerivedStatus(
            PR_STUCK, f"Needs you — {summary}", at=quiet, summary=summary, chat_href=href
        )
    line = f"Waiting on {said}" + (f", until {clock(until)}" if until else "")
    deadline = min((t for t in (until, stale) if t is not None), default=None)
    return DerivedStatus(WAITING, line, deadline=deadline, summary=line)


def _ordinal(place: int) -> str:
    """``first``, ``2nd``, ``3rd``, ``11th``: a place in the merge queue."""
    if place <= 1:
        return "first"
    suffix = "th" if 10 <= place % 100 <= 20 else {2: "nd", 3: "rd"}.get(place % 10, "th")
    if place % 10 == 1 and not 10 <= place % 100 <= 20:
        suffix = "st"
    return f"{place}{suffix}"


def _cut_off(inputs: DerivedInputs, *, clock: Callable[[datetime | None], str]) -> DerivedStatus:
    """Rule 3 for a turn whose process died mid-turn (#3008): the revival's, else a person's."""
    at = _aware(inputs.cut_off_at)
    when = clock(at)
    if inputs.reviving:
        summary = "the runtime to revive the agent" + (f" · cut off {when}" if when else "")
        return DerivedStatus(
            REVIVING, f"Waiting on {summary}", at=at, summary=summary, chat_href=inputs.chat_href
        )
    summary = "the agent's process stopped mid-turn" + (f" · {when}" if when else "")
    return DerivedStatus(
        CUT_OFF, f"Needs you — {summary}", at=at, summary=summary, chat_href=inputs.chat_href
    )


def _ended(
    inputs: DerivedInputs, *, now: datetime, clock: Callable[[datetime | None], str]
) -> DerivedStatus:
    """D7 rule 9: the turn ended without a next step (#2960)."""
    href = inputs.chat_href
    ended = _aware(inputs.ended_at)
    nudged = _aware(inputs.nudged_at)
    if nudged is not None and (ended is None or ended <= nudged):
        if now < nudged + NUDGE_GRACE:
            when = clock(nudged)
            summary = "asked the agent for its next step" + (f" · {when}" if when else "")
            return DerivedStatus(
                NUDGED, f"Working — {summary}", at=nudged, summary=summary,
                deadline=nudged + NUDGE_GRACE, chat_href=href,
            )
        return _unfinished(inputs, clock, bug=True)
    if nudged is not None:
        return _unfinished(inputs, clock, bug=True)  # asked once, and it ended the same way
    if ended is None or not inputs.asks:
        # No chat to ask, or a deck that does not ask: a person's, not a bug.
        return _unfinished(inputs, clock, bug=False)
    return DerivedStatus(
        ENDED, "Working — asking the agent for its next step", at=ended,
        summary="asking the agent for its next step", chat_href=href,
        # The deck asks once the ending has settled; the pass has to run again then.
        deadline=ended + ENDING_SETTLE if now < ended + ENDING_SETTLE else None,
    )


def _unfinished(
    inputs: DerivedInputs, clock: Callable[[datetime | None], str], *, bug: bool
) -> DerivedStatus:
    quote = first_sentence(inputs.last_text)
    when = clock(inputs.ended_at)
    parts = ["the agent ended without a next step"]
    parts += [f'last said "{quote}"'] if quote else []
    parts += [when] if when else []
    summary = " · ".join(parts)
    return DerivedStatus(
        UNFINISHED, f"Needs you — {summary}", at=inputs.ended_at, summary=summary,
        pr=inputs.pr, chat_href=inputs.chat_href, bug=bug,
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _headed(headline: str, summary: str) -> str:
    """``Headline — summary``, or the headline alone when there is nothing to add."""
    return f"{headline} — {summary}" if summary else headline
