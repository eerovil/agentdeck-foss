"""The Work page's story: one status, four milestones, and where a message goes (#2218).

The Work page leads with a few lines an operator reads at a glance — what is happening, what
happens next, whether they have to do anything — and a way to send the Work an instruction
without first picking a chat. Everything here is a *reading* of facts the Work card already
carries (`work_overview.WorkCard`: the derived status of a Work the deck runs, verification
from explicit jobs, workspace from claims or the inspected checkouts). Nothing is stored and
nothing schedules on it: it is presentation, not a second lifecycle.

The tones are deliberately few. Healthy automation — a turn working, a job running — is
``active`` and calm. ``attention`` is reserved for a genuine human dependency (a chat asking
a question); ``problem`` is something that failed or cannot be confirmed; ``done`` is
settled.

Attention keeps its kind (#2224). Only a chat that really asks — a question-style pending
interaction or an unanswered open question — reads as **Needs your answer**, and only a
question in the chat's text, which a message answers, turns the message box into **Answer
question**; an interaction form, an approval or a browser step is settled on the chat's page.
A chat that merely stopped is a chat to look at, not a question.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from .work_overview import (
    HANDOFF_STATES,
    RECENT_WORK,
    WorkAttention,
    WorkCard,
    WorkChat,
    WorkspaceLine,
    continued_at,
)
from .work_workspace import WorkWorkspace

ACTIVE = "active"
ATTENTION = "attention"
PROBLEM = "problem"
DONE = "done"
IDLE = "idle"

# Milestone states. ``waiting`` is "not yet, and nothing is wrong".
STEP_DONE = "done"
STEP_ACTIVE = "active"
STEP_WAITING = "waiting"
STEP_PROBLEM = "problem"
STEP_ATTENTION = "attention"
STEP_IDLE = "idle"

# A text mark for every state, so no state is told by colour alone.
MARKS = {
    STEP_DONE: "✓",
    STEP_ACTIVE: "●",
    STEP_WAITING: "○",
    STEP_PROBLEM: "!",
    STEP_ATTENTION: "!",
    STEP_IDLE: "–",
}

# What kind of human attention the status is about (``WorkStatus.kind``), so the page never
# has to guess a question from "needs a person".
CHAT_QUESTION = "chat_question"
CHAT_STOPPED = "chat_stopped"
# A chat blocked on an approval or a browser step: settled on its own page, not answered.
CHAT_ACTION = "chat_action"
# Auto-merge waits on a required check that never reported (#2275).
MERGE_BLOCKED = "merge_blocked"
# What an automatic wait is on (#2573). Nothing needs a person, but the front page lists these
# Works under Waiting rather than Working now: a job a chat queued, or the deck's own wait.
WAITING_JOB = "waiting_job"
_WAITS = frozenset({WAITING_JOB})
# Merged, and the agent checks it live before the Work finishes (#3578): its own front-page
# group.
LIVE_CHECKING = "live_checking"
_LIVE = frozenset({LIVE_CHECKING})
# An open issue no agent works on (#3558): a person starts one or handles it themselves.
NOT_STARTED = "not_started"
# The readings that say only that nothing is going (#2590): a Work Working over one has
# nothing more to say about it.
_TURN_ONLY = frozenset({"Idle"})

# How much of a question the status repeats; the chat has the rest.
_QUESTION_SHOWN = 140

NOTHING_NEEDED = "Nothing needed"


@dataclass(frozen=True)
class Milestone:
    key: str  # implement | review | verify | merge
    label: str
    state: str
    note: str

    @property
    def mark(self) -> str:
        return MARKS.get(self.state, "")


@dataclass(frozen=True)
class WorkStatus:
    """The top of the Work page: one headline and the lines under it."""

    tone: str  # active | attention | problem | done | idle
    headline: str  # a few words, shown in capitals
    summary: str
    detail: str
    next: str  # what the deck waits for, or ""
    you: str  # what the operator has to do
    milestones: tuple[Milestone, ...]
    # The kind of human attention, or of automatic wait (``_WAITS``); "" for neither.
    kind: str = ""
    where: str = ""  # where the operator acts: a chat, a pull request or the issue
    where_href: str = ""
    summary_href: str = ""  # what the summary names, linked: a Ready's review chat (#2992)

    @property
    def mark(self) -> str:
        return {ACTIVE: "●", ATTENTION: "!", PROBLEM: "!", DONE: "✓"}.get(self.tone, "–")


_HANDOFF_STATES = HANDOFF_STATES


def waiting_attention(card: WorkCard) -> tuple[WorkAttention, ...]:
    """The Work's Deckhand cards that wait on a person, in their ranked order."""
    return tuple(item for item in card.attention if item.state not in _HANDOFF_STATES)


def _merged(card: WorkCard) -> bool:
    return any(output.state == "merged" for output in card.outputs)


def _all_landed(card: WorkCard) -> bool:
    """Something merged and nothing is still open: the merge step has finished (#2316)."""
    return _merged(card) and all(output.settled for output in card.outputs)


def _milestones(card: WorkCard) -> tuple[Milestone, ...]:
    verification = card.verification
    done = card.work_state == "done"

    # Implement: settled once verification exists.
    if done:
        implement = (STEP_DONE, "Done")
    elif verification is not None and verification.status != "earlier":
        implement = (STEP_DONE, "Done")
    elif _busy(card):
        # A turn on the Work is running (#2487).
        implement = (STEP_ACTIVE, "Working")
    else:
        implement = (STEP_IDLE, "Not started")

    # Verify: only the explicit verification jobs speak here (#2148). The final suite runs on
    # the head a review passed (#2253), so a run that no longer covers the checkout is simply
    # not due yet rather than a problem.
    status = verification.status if verification is not None else ""
    if status == "verified" and verification.superseded and verification.tested_at:
        # Green, on a commit GitHub has since moved the pull request past (#2327).
        verify = (STEP_DONE, f"Passed at {verification.tested_at}")
    elif status == "verified":
        verify = (STEP_DONE, "Passed")
    elif status in {"running", "queued"}:
        verify = (STEP_ACTIVE, status.capitalize())
    elif status == "failed" and not verification.superseded:
        # A red run on a head the pull request has moved past falls through to what its
        # current head is waiting on (#2460).
        verify = (STEP_PROBLEM, "Failed")
    elif status == "stale" and not done:
        verify = (STEP_WAITING, "After review")
    elif status in {"stale", "unproven"}:
        verify = (STEP_PROBLEM, status.capitalize())
    elif status == "earlier":
        verify = (STEP_WAITING, "Earlier Run")
    elif status == "failed":
        # Only a red run the pull request has moved past reaches here (#2460).
        at = verification.tested_at
        verify = (STEP_WAITING, f"Failed at {at}" if at else "Failed earlier")
    elif status:
        verify = (STEP_WAITING, status.capitalize())
    else:
        verify = (STEP_IDLE, "Not run")

    if card.chatgpt_reviewed:
        # A review chat on the chat board covered the result (#2411). Its verdict is in the
        # chat, not here, so this says a review ran, never that it passed.
        check = (STEP_DONE, "ChatGPT reviewed")
    else:
        check = (STEP_IDLE, "None") if done else (STEP_IDLE, "Not yet")

    if done or _all_landed(card):
        merge = (STEP_DONE, "Merged" if _merged(card) else "Done")
    else:
        merge = (STEP_WAITING, "Waiting")

    return (
        Milestone("implement", "Implement", *implement),
        Milestone("review", "Review", *check),
        Milestone("verify", "Verify", *verify),
        Milestone("merge", "Merge", *merge),
    )


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _QUESTION_SHOWN else text[: _QUESTION_SHOWN - 1].rstrip() + "…"


def work_status(card: WorkCard) -> WorkStatus:
    """What the Work is doing, in the order an operator asks: now, next, and me?

    While a chat on the Work is mid-turn, it is Working, whatever else is true (#2590): a
    question or a stopped chat elsewhere on it included. What it
    would otherwise say is in the detail and shows again when the turn ends. Only a finished
    Work and a paused one are not Working.

    A Work the deck runs itself reads its derived status instead (``derived_story``, #2944):
    the page's status box, its category, its buttons and the front page all read this one.
    """
    if card.start_label:
        return _not_started(card)
    if card.derived is not None:
        story = derived_story(card)
        if card.paused and story.tone != DONE:
            return _paused(card, story)
        return story
    story = _read_status(card)
    if card.paused and story.tone != DONE:
        return _paused(card, story)
    if story.tone == DONE or not _busy(card):
        return story
    return _working(card, story)


def _not_started(card: WorkCard) -> WorkStatus:
    """An open issue without its repository's ownership label (#3558): no agent starts on it
    until a person adds the label, here with Start agent, or handles it themselves."""
    return WorkStatus(
        tone=ATTENTION,
        headline="Not started",
        summary="No agent works on this issue.",
        detail=f"It has no {card.start_label} label, so no agent starts on it.",
        next="",
        you="Start an agent, or handle it yourself",
        milestones=(),
        kind=NOT_STARTED,
        where="the issue",
        where_href=card.source_url,
    )


# The derived kinds (``work_derived``) as the page's tones: what is a person's is attention,
# a died turn a problem, a closed issue done, and every automatic wait a waiting job.
_DERIVED_TONES = {
    "question": ATTENTION,
    "working": ACTIVE,
    "stalled": ATTENTION,
    "died": PROBLEM,
    "reviving": ACTIVE,  # the runtime revives a turn a restart cut off by itself (#3008)
    "cut_off": ATTENTION,  # its revival window passed: a person starts it again
    "login_expired": ATTENTION,  # its account's sign-in expired: a person signs in (#3354)
    "closed": ATTENTION,  # a person finishes it (D14); "finished" is done
    "finished": DONE,
    "review": ACTIVE,
    "review_stuck": ATTENTION,
    "review_queued": ACTIVE,  # the deck starts a reviewer on it itself (#2960)
    "waiting": ACTIVE,
    "queued": ACTIVE,  # the deck's merge queue moves it on by itself (#3022)
    "ready": ATTENTION,  # a person's Merge (#2960)
    "ended": ACTIVE,  # the deck is asking the agent for its next step (#2960)
    "nudged": ACTIVE,
    "unfinished": PROBLEM,
    "scheduled": ACTIVE,  # a schedule definition waits on its next run (#2998)
    "parked": ACTIVE,  # waits for a person's message, which they asked for (#3560)
    "pr_held": ATTENTION,  # only a person lifts its pull request's hold (#3097)
    "pr_stuck": ATTENTION,  # its pull request's hold has not moved for hours (#3097)
    "live_failed": ATTENTION,  # its merged change failed the agent's live check (#3141)
    "not_live": ATTENTION,  # part of its merge is not running yet (#3220)
    "review_escalated": ATTENTION,  # the reviewer asks the owner (#3216)
    "spec_wait": ATTENTION,  # a person approves the posted spec (#3247)
}
_DERIVED_WAITS = frozenset(
    {"review", "review_queued", "waiting", "queued", "scheduled", "parked", "reviving"}
)
_DERIVED_HEADLINES = {
    "question": "Needs you",
    "working": "Working",
    "stalled": "Needs you",
    "died": "Needs you",
    "reviving": "Waiting",
    "cut_off": "Needs you",
    "login_expired": "Needs you",
    "closed": "Issue closed",
    "finished": "Finished",
    "review": "In review",
    "review_stuck": "Needs you",
    "review_queued": "In review",
    "waiting": "Waiting",
    "queued": "In merge queue",
    "ready": "Ready for merge",
    "ended": "Working",
    "nudged": "Working",
    "unfinished": "Needs you",
    "scheduled": "Scheduled",
    "parked": "Parked",
    "pr_held": "Needs you",
    "pr_stuck": "Needs you",
    "live_failed": "Needs you",
    "not_live": "Needs you",
    "review_escalated": "Needs you",
    "spec_wait": "Needs you",
}


def derived_story(card: WorkCard) -> WorkStatus:
    """The status of a Work the deck runs itself (#2895 step 3, D1, D7), as ``work_status``
    gives it to every surface (#2944)."""
    derived = card.derived
    live = getattr(derived, "live_checking", False)
    # Merged and checked live (#3578): its own status over the turn or wait underneath.
    headline = "Live checking" if live else _DERIVED_HEADLINES.get(derived.kind, "Needs you")
    # Built with the line, never cut back out of it (#2956): a Stopped line naming a pull
    # request read "ended at PR #37 · 13:02".
    summary = derived.summary
    you = {
        "question": (
            "Approve or deny the command on the Mac's page"
            if derived.approval_href
            else "Answer the question in the chat"
        ),
        "stalled": "Open the chat and see why it stopped making progress",
        "died": "Open the chat and send it a message to start again",
        "cut_off": "Open the chat and send it a message to start again",
        "login_expired": (
            "Sign the account in again from the usage panel, then send the chat a message"
        ),
        "closed": "Finish the Work, or send a follow-up",
        "review_stuck": (
            "Open the review chat and read why the reviewer stopped"
            if derived.review_held
            else "Open the review chat and see why no reviewer is running"
        ),
        "ready": "Merge it, or send a follow-up",
        "unfinished": "Read its last message and tell it what to do next",
        "pr_held": "Lift the hold, merge it by hand, or finish the Work",
        "pr_stuck": "Open the pull request and see why nothing moved it",
        "live_failed": "Read what failed live, then reopen the issue or finish the Work",
        "not_live": "Deploy what is not running yet, then tell the agent to check it again",
        "review_escalated": "Read the reviewer's question and answer it in the chat",
        "spec_wait": "Read the spec, then approve it or say what to change",
    }.get(derived.kind, NOTHING_NEEDED)
    return WorkStatus(
        tone=ACTIVE if live else _DERIVED_TONES.get(derived.kind, IDLE),
        headline=headline,
        summary=summary,
        detail="",
        next="",
        you=you,
        milestones=(),
        kind=(
            LIVE_CHECKING if live else WAITING_JOB if derived.kind in _DERIVED_WAITS else ""
        ),
        # A Mac command waiting on the boss's tap opens its approval page (#3106).
        where=(
            "the Mac's approval page"
            if derived.approval_href
            else "the chat" if derived.chat_href else ""
        ),
        where_href=derived.approval_href or derived.chat_href,
        summary_href=derived.summary_url or _issue_url(derived.review_ref),
    )


def _issue_url(ref: str) -> str:
    """``https://github.com/owner/repo/issues/n`` for ``owner/repo#n``, ``""`` otherwise."""
    repository, _, number = ref.partition("#")
    if "/" not in repository or not number.isdigit():
        return ""
    return f"https://github.com/{repository}/issues/{number}"


def _working(card: WorkCard, story: WorkStatus) -> WorkStatus:
    """``story`` with a turn running on the Work: Working, and ``story`` in the detail."""
    if story.headline in _TURN_ONLY:
        detail = story.detail
    else:
        said = story.summary.rstrip(" .")
        detail = f"When it finishes: {story.headline}" + (f": {said}" if said else "")
        detail += "" if detail.endswith("…") else "."
        if story.detail:
            detail = f"{detail} {story.detail}"
    summary, where, where_href = _working_chats(card)
    # Nothing asks for a person while the turn runs, the step strip included.
    steps = tuple(
        replace(step, state=STEP_WAITING, note="Waiting")
        if step.state == STEP_ATTENTION
        else step
        for step in story.milestones
    )
    return WorkStatus(
        tone=ACTIVE,
        headline="Working",
        summary=summary,
        detail=detail,
        # No next step, so the front page's row names who is working.
        next="",
        you=NOTHING_NEEDED,
        milestones=steps,
        where=where,
        where_href=where_href,
    )


def _paused(card: WorkCard, story: WorkStatus) -> WorkStatus:
    """A paused Work (#2548): nothing new starts on it and it asks nobody anything, so it is
    not Needs you whatever it was waiting on. A turn already running still finishes."""
    summary = (
        "The current turn will finish first; nothing new starts after it."
        if _busy(card)
        else "Nothing new starts until you resume it."
    )
    if card.deck_paused and not card.label_paused:
        # Paused on this deck (#2559): the deck's refusal is all.
        summary = (
            "The current turn will finish first; after it the deck sends nothing to its chats."
            if _busy(card)
            else "The deck sends nothing to its chats until you resume it."
        )
    return WorkStatus(
        tone=IDLE,
        headline="Paused",
        summary=summary,
        detail=f"Before the pause: {story.headline}." if story.headline else "",
        next="",
        you="Resume it when the work should go on",
        milestones=story.milestones,
    )


# How much of a job's command a status line shows.
_JOB_LABEL = 40


def _job_line(job: Any, now: datetime | None = None) -> str:
    """A job in a few words (#2573): its label, else its command, cut short, and how long it
    has queued or run, e.g. ``pytest -q · running 6 min``."""
    label = " ".join(job.label.split())
    if len(label) > _JOB_LABEL:
        label = label[: _JOB_LABEL - 1].rstrip() + "…"
    if job.since is None:
        return label
    minutes = max(0, int(((now or datetime.now(UTC)) - job.since).total_seconds() // 60))
    hours, rest = divmod(minutes, 60)
    took = (
        "under a minute" if not minutes
        else f"{minutes} min" if not hours
        else f"{hours} h {rest} min" if rest
        else f"{hours} h"
    )
    return f"{label} · {job.state} {took}"


def _busy(card: WorkCard) -> bool:
    """A turn is running on the Work: a chat on it is thinking (#2590)."""
    return bool(card.working_count)


def _working_chats(card: WorkCard) -> tuple[str, str, str]:
    """The summary naming the Work's working chats, and where the first one is."""
    chats = card.working_chats
    if not chats:
        chats_word = "chat is" if card.working_count == 1 else "chats are"
        return f"{card.working_count} {chats_word} working.", "", ""
    title, where_href = chats[0]
    others = len(chats) - 1
    summary = (
        f"{title} and {others} more {'chat' if others == 1 else 'chats'} are working."
        if others
        else f"{title} is working."
    )
    return summary, title, where_href


def _read_status(card: WorkCard) -> WorkStatus:
    verification = card.verification
    steps = _milestones(card)

    def status(tone: str, headline: str, summary: str, detail: str = "", **fields: str):
        return WorkStatus(
            tone=tone,
            headline=headline,
            summary=summary,
            detail=detail,
            next=fields.get("next", ""),
            you=fields.get("you", NOTHING_NEEDED),
            milestones=steps,
            kind=fields.get("kind", ""),
            where=fields.get("where", ""),
            where_href=fields.get("where_href", ""),
        )

    if card.work_state == "done":
        if card.awaiting_finish and any(output.is_open for output in card.outputs):
            # Another chat closed the issue with a pull request still open (#3187): that pull
            # request's state reads, and the issue's closing is said after it.
            story = _read_status(replace(card, work_state=""))
            return replace(story, summary=story.summary.rstrip(".") + " · issue closed.")
        pulls = ", ".join(
            output.label for output in card.outputs if output.state == "merged"
        )
        detail = f"Merged {pulls}." if pulls else ""
        if card.awaiting_finish:
            # The issue closed; the Work is over only when a person says so (#2510).
            summary = "The issue closed. Finish the Work when you are done with it."
            return status(DONE, "Issue closed", summary, detail, next="", you="Finish it")
        return status(DONE, "Done", "This Work is finished.", detail, next="")
    # Each kind of chat attention is counted on its own and keeps its own action (#2225): a
    # question never stands for an approval or a stopped chat, and the ones that are not the
    # headline still show in the detail line as their own follow-up.
    waiting = waiting_attention(card)
    asking = [item for item in waiting if item.asks]
    acting = [item for item in waiting if item.interaction and not item.asks]
    # A chat that restarts by itself is not a chat for a person to look at.
    stopped = [
        item for item in waiting if not (item.asks or item.interaction or item.recovering)
    ]

    def also(*, acting_from: int = 0, stopped_from: int = 0) -> str:
        """What else waits on a person, each in its own words."""
        parts = []
        if acting[acting_from:]:
            names = ", ".join(item.chat_title for item in acting[acting_from:])
            parts.append(f"Also waiting on its own page: {names}.")
        if stopped[stopped_from:]:
            names = ", ".join(item.chat_title for item in stopped[stopped_from:])
            parts.append(f"Also stopped: {names}.")
        return " ".join(parts)

    if asking:
        # A real question comes first. A question asked through an interaction form is
        # answered on the chat's page; only a question in the chat's text by a message.
        first = asking[0]
        more = len(asking) - 1
        who = first.chat_title + (
            f" (and {more} more {'question' if more == 1 else 'questions'})" if more else ""
        )
        return status(
            ATTENTION,
            "Needs your answer",
            (
                f"{who} asked: {_short(first.question)}"
                if first.question
                else f"{who} is waiting for your answer."
            ),
            also(),
            next="your answer",
            you=(
                "Answer the question"
                if first.answered_by_message
                else "Answer the question on the chat's page"
            ),
            kind=CHAT_QUESTION,
            where=first.chat_title,
            where_href=first.href,
        )
    if acting:
        # An approval or a browser step: a decision the chat's own page takes, not an answer.
        first = acting[0]
        what = _short(first.question) if first.question else "a pending request"
        return status(
            ATTENTION,
            "Chat is waiting on you",
            f"{first.chat_title} is waiting on: {what}",
            also(acting_from=1),
            next="your approval",
            you=(
                "Continue in the browser from the chat's page"
                if first.interaction == "mcp_url"
                else "Approve or decline on the chat's page"
            ),
            kind=CHAT_ACTION,
            where=first.chat_title,
            where_href=first.href,
        )
    if stopped:
        first = stopped[0]
        what = first.state.replace("_", " ")
        return status(
            ATTENTION,
            "Chat needs a look",
            f"{first.chat_title} stopped; it has not asked you anything.",
            " ".join(filter(None, (f"Deckhand says: {what}.", also(stopped_from=1)))),
            you="Look at the chat",
            kind=CHAT_STOPPED,
            where=first.chat_title,
            where_href=first.href,
        )
    blocked = [output for output in card.outputs if output.never_ran]
    if blocked:
        # Auto-merge is on and waits on a required check that never started (#2275): nothing
        # is working, and nothing will until a person starts that check or merges by hand.
        first = blocked[0]
        names = ", ".join(first.never_ran)
        # Its summary names no pull request (#3720); ``where`` links the first.
        return status(
            ATTENTION,
            "Merge blocked",
            f"Waits for {names}, which never ran.",
            "GitHub merges it by itself once that check passes.",
            next="a required check",
            you="Start the missing check, or merge by hand",
            kind=MERGE_BLOCKED,
            where=first.label,
            where_href=first.url,
        )
    if verification is not None and verification.status in {"running", "queued"}:
        return status(ACTIVE, "Verifying", verification.label + ".")
    if card.jobs:
        # A chat queued a job and ended its turn (#2573): the job is what moves next.
        job = card.jobs[0]
        more = f" (+{len(card.jobs) - 1} more)" if len(card.jobs) > 1 else ""
        return status(
            ACTIVE,
            "Job running" if job.state == "running" else "Job queued",
            f"{_job_line(job)}{more}.",
            next="the job to finish",
            kind=WAITING_JOB,
        )
    # Below, nothing but the turn itself (``_TURN_ONLY``): a chat mid-turn says Working (#2590).
    return status(IDLE, "Idle", "Nothing is running.")


# --- the front page (#2219) ----------------------------------------------------------------------

# The front page's sections. A Work is in exactly one, picked from its status above, so the
# front page and the Work page can never disagree about whether a Work needs someone.
NEEDS_YOU = "needs_you"
IN_PROGRESS = "active"
FINISHED = "finished"
QUIET = "idle"  # recent, nothing current and not finished: counted, not listed
PAUSED = "paused"  # a person paused it (#2548): listed apart until they resume it

# How many finished Works the front page lists; the rest are on /works.
MAX_FINISHED = 5
# How many Works Recently stopped lists (#2283): a shortcut to where they landed, not a list.
MAX_STOPPED = 5


@dataclass(frozen=True)
class FrontRow:
    """One Work on the front page, in a line or two: never the Work page's detail."""

    card: WorkCard
    status: WorkStatus
    section: str
    reason: str  # what the row says under the title
    action: str = ""  # Needs you only: "Answer" or "Open"
    rank: int = 0  # Needs you only: the stronger dependency first
    # Needs you only: the operator's category and actions (``work_attention``, #2236), which
    # the row shows in place of the technical headline and reason above.
    attention: Any = None
    # Needs you only (#2283): when the Work entered its current category, and whether that was
    # after the operator last opened the Work's page (``work_stops``).
    stopped_at: datetime | None = None
    unseen: bool = False
    # A parent issue's row with its sub-issues' Works folded in (#3622, ``work_tree.fold``):
    # the children shown under it, its own row as built, the child whose state the row shows
    # when that is not its own, and the progress line ("3/7 done · 1 waits for you").
    children: tuple[FrontRow, ...] = ()
    own: FrontRow | None = None
    lead: FrontRow | None = None
    progress: str = ""
    # A wayfinder map's row (#3696, ``wayfinder_map.Progress``): its title, counts and the
    # questions its tickets wait on. None for every other row.
    map: Any = None
    # A map's children that wait on another of its tickets, not on the owner (#3736,
    # ``work_tree._held_tickets``): folded in, but never a Needs you of their own.
    held: frozenset[str] = frozenset()

    @property
    def revision(self) -> str:
        stopped = self.stopped_at.isoformat() if self.stopped_at is not None else ""
        base = f"{self.card.revision}:{stopped}:{int(self.unseen)}"
        if not self.children and not self.progress:
            return base
        # A parent row redraws when any child it shows, or its sub-issue count, changes (#3622).
        kids = "|".join(child.revision for child in self.children)
        tree = hashlib.sha1(f"{kids}|{self.progress}".encode(), usedforsecurity=False)
        return f"{base}:{tree.hexdigest()[:12]}"

    @property
    def actor(self) -> WorkCard:
        """The Work the row's controls act on: the leading child's, when the row shows one."""
        return self.lead.card if self.lead is not None else self.card

    @property
    def buttons(self) -> tuple:
        """Needs you only: the controls for the operator's current actions (#2237)."""
        from .work_actions import buttons

        return buttons(self.actor, self.attention, self.status.where_href)

    @property
    def continued(self) -> datetime | None:
        """Finished only: when a chat carried on in the done Work (#2357), else None."""
        if self.section != FINISHED:
            return None
        return continued_at(self.card.finished_at, self.card.last_activity)

    @property
    def finished_order(self) -> datetime | None:
        """A finished row's place in time: the continued chat, else when it finished."""
        return self.continued or self.card.finished_at

    @property
    def archive(self) -> Any:
        """A finished row's one control: take it off Recently finished (#2254)."""
        from .work_actions import ARCHIVE_BUTTON

        return ARCHIVE_BUTTON

    @property
    def finish(self) -> Any:
        """A row waiting for Finish (#2510): that one press, where the row shows its age."""
        from .work_actions import finish_button

        return finish_button(self.actor)

    @property
    def start(self) -> Any:
        """A Not started row's one control (#3558): Start agent, one tap from the list."""
        from .work_actions import START_BUTTON

        return START_BUTTON

    @property
    def bring_back(self) -> Any:
        """The Hidden list's one control (#2238)."""
        from .work_actions import BRING_BACK

        return BRING_BACK

    @property
    def resume(self) -> Any:
        """The Paused list's one control (#2548)."""
        from .work_actions import RESUME_BUTTON

        return RESUME_BUTTON

    @property
    def source(self) -> str:
        """``repo#n``: the owner is the same on nearly every row and costs a phone its width."""
        repository, _, number = self.card.source_label.rpartition("#")
        return f"{repository.rsplit('/', 1)[-1]}#{number}" if number else self.card.source_label

    @property
    def number(self) -> str:
        """``#n``, for a page whose rows all share one repository."""
        _, mark, number = self.card.source_label.rpartition("#")
        return f"#{number}" if mark and number else self.source

    @property
    def line(self) -> str:
        """The row's one line (#2254): a Needs you reason in plain words, else ``reason``.

        A parent row showing a child's state names that child first (#3622)."""
        from .work_wording import plain_reason

        if self.lead is not None:
            lead = self.lead
            same = lead.card.source_label.rpartition("#")[0].casefold() == (
                self.card.source_label.rpartition("#")[0].casefold()
            )
            said = lead.line or lead.status.headline
            return f"{lead.number if same else lead.source}: {said}"
        if self.attention is not None:
            from .work_attention import NEEDS_REVIEW

            line = plain_reason(self.attention.reason)
            if not line and self.attention.category == NEEDS_REVIEW:
                return "Waiting for your review"
            return line
        return self.reason

    @property
    def glyph(self) -> str:
        """Needs you only: the category's glyph, so it never rests on colour alone."""
        from .work_attention import GLYPHS

        return GLYPHS.get(self.attention.category, "") if self.attention is not None else ""

    @property
    def live(self) -> bool:
        """Listed under Live checking: merged, before its live check is recorded (#3578)."""
        return self.section == IN_PROGRESS and self.status.kind in _LIVE

    @property
    def waits(self) -> bool:
        """Listed under Waiting: an automatic wait such as a job (#2573)."""
        return self.section == IN_PROGRESS and self.status.kind in _WAITS


@dataclass(frozen=True)
class FrontGroup:
    """One category of Needs you on the front page, in ``work_attention.FRONT_ORDER``."""

    category: str
    label: str
    glyph: str
    rows: tuple[FrontRow, ...]
    # What the group means, beside its header, when the label alone does not say (#2888).
    note: str = ""

    @property
    def new(self) -> int:
        """Rows that stopped here since the operator last opened them (#2283)."""
        return sum(1 for row in self.rows if row.unseen)


def _needs_you(card: WorkCard, story: WorkStatus) -> tuple[str, str, int]:
    """(reason, action, rank) of a Work waiting on a person, or ("", "", -1) if it is not.

    A question a chat asks is the strongest, then anything else that waits on a person, then
    a problem. A Work whose issue closed waits on a person too, until someone finishes it (#2510).
    """
    if story.kind == NOT_STARTED:
        return "No agent works on this issue", "Open", 3
    if card.derived is not None:
        return _derived_needs(card, story)
    if story.tone == DONE and card.awaiting_finish:
        return "The issue closed; finish the Work when you are done with it", "Open", 3
    asking = [item for item in waiting_attention(card) if item.asks]
    if story.kind == CHAT_QUESTION and asking:
        # The headline already says it waits for an answer; say only what it adds.
        count = len(asking)
        state = asking[0].state
        what = state.replace("_", " ") if state != "waiting" else ""
        more = f"{count} questions" if count > 1 else ""
        # Only a question in the chat's text is answered by a message; a form is filled in on
        # the chat's own page (#2235).
        action = "Answer" if asking[0].answered_by_message else "Open"
        return " · ".join(part for part in (what, more) if part), action, 0
    if story.kind in {CHAT_ACTION, CHAT_STOPPED}:
        # A chat waits on a person, but not for an answer (#2224); the row never names it.
        reason = "Waiting on a chat's own page" if story.kind == CHAT_ACTION else "A chat stopped"
        return reason, "Open", 0
    if story.tone == ATTENTION:
        return story.summary.rstrip("."), "Open", 1
    if story.tone == PROBLEM:
        reason = story.summary.rstrip(".")
        return (f"{reason}: {story.detail}" if story.detail else reason), "Open", 2
    return "", "", -1


# A derived status that waits on a person (#2944): its rank among Needs you, as ``_needs_you``
# ranks the rest. A question first, then a stopped or stuck chat, then a Work to finish.
_DERIVED_RANKS = {
    "question": 0, "died": 0, "cut_off": 0, "login_expired": 0, "stalled": 0,
    "unfinished": 1, "review_stuck": 1,
    "ready": 1, "closed": 3, "pr_stuck": 1, "pr_held": 2, "live_failed": 2,
    "not_live": 2, "review_escalated": 0, "spec_wait": 0,
}


def _derived_needs(card: WorkCard, story: WorkStatus) -> tuple[str, str, int]:
    """``_needs_you`` for a Work the deck runs itself: read from its derived status alone."""
    kind = card.derived.kind
    rank = _DERIVED_RANKS.get(kind, -1)
    if rank < 0 or story.tone not in {ATTENTION, PROBLEM}:
        return "", "", -1  # paused, finished, working or waiting
    if kind == "closed":
        return "The issue closed; finish the Work when you are done with it", "Open", rank
    # The headline already says it waits for an answer; the question itself is the reason.
    return story.summary.rstrip("."), "Open", rank


def waiting_story(card: WorkCard, story: WorkStatus | None = None) -> WorkStatus:
    """The story a Work's Needs you is judged on: ``work_status(card)``, except while only a
    background notification woke its chats (#2760).

    Then the headline still says Working (#2590), but nobody answered, so the wait under it
    stands: the front page keeps the Work in Needs you rather than dropping it for a few
    seconds, and the Work's own page offers the same actions its row does.
    """
    story = story if story is not None else work_status(card)
    if card.derived is not None:
        return story  # one status: a woken chat's wait is in it already (#2944)
    if not card.woke_itself or card.paused or story.tone == DONE:
        return story
    if _needs_you(card, story)[2] >= 0:
        return story
    waiting = _read_status(card)
    return waiting if _needs_you(card, waiting)[2] >= 0 else story


def front_row(card: WorkCard, *, current: bool) -> FrontRow:
    """Which front-page section a Work belongs in, and the one line it says there.

    ``current`` is the Work's Active classification (#2201): current evidence, never age.
    """
    story = work_status(card)
    if story.tone == DONE and not card.awaiting_finish:
        # The section and its mark already say it finished; only a merge adds anything, and
        # never which pull requests (#3720).
        merged = any(output.state == "merged" for output in card.outputs)
        reason = "Merged" if merged else ""
        return FrontRow(card, story, FINISHED, reason)
    if card.paused and story.tone != DONE:
        return FrontRow(card, story, PAUSED, story.summary.rstrip("."))
    waiting = waiting_story(card, story)
    reason, action, rank = _needs_you(card, waiting)
    if rank >= 0:
        from .work_attention import operator_attention

        attention = operator_attention(card, waiting)
        return FrontRow(card, story, NEEDS_YOU, reason, action, rank, attention)
    if story.kind in _WAITS:
        # What it waits on says more than its next step (#2573): the blockers, the job, the merge.
        line = story.summary.rstrip(".")
    else:
        line = f"Next: {story.next}" if story.next else story.summary.rstrip(".")
    # A derived Work that is working or waiting is in progress, whatever its age (#2944).
    current = current or card.derived is not None
    return FrontRow(card, story, IN_PROGRESS if current else QUIET, line)


# The counts-line keys whose words change with the count: (one, several).
SUMMARY_PLURALS = {"answer": ("question", "questions"), "review": ("review", "reviews")}


def summary_words(key: str, count: int) -> str:
    one, many = SUMMARY_PLURALS[key]
    return one if count == 1 else many


@dataclass(frozen=True)
class FrontSections:
    needs_you: tuple[FrontRow, ...] = ()
    active: tuple[FrontRow, ...] = ()
    finished: tuple[FrontRow, ...] = ()
    active_more: int = 0  # Active Works past the section's display limit
    finished_more: int = 0  # finished Works past ``MAX_FINISHED``
    quiet: int = 0  # recent Works with nothing current and nothing finished
    # Every listed Work is in one repository, so a row can say ``#n`` alone.
    one_repository: bool = True
    # Needs you rows the operator hid (#2238), in Needs you's own order: the Hidden list.
    hidden: tuple[FrontRow, ...] = ()
    # Current Works waiting only on something automatic, kept out of ``active``: a job or the
    # deck's own wait (#2573).
    waiting: tuple[FrontRow, ...] = ()
    # The Needs you rows that most recently stopped, newest first (#2283): each is also in
    # its category, so this is a shortcut to where it landed, never a move.
    stopped: tuple[FrontRow, ...] = ()
    # Works a person paused (#2548), most recent activity first: the Paused list.
    paused: tuple[FrontRow, ...] = ()
    # Current Works merged and checked live now (#3578), kept out of ``active`` and ``waiting``.
    live: tuple[FrontRow, ...] = ()

    @property
    def new(self) -> int:
        """Needs you rows that stopped since the operator last opened them (#2283)."""
        return sum(1 for row in self.needs_you if row.unseen)

    @property
    def stopped_new(self) -> int:
        return sum(1 for row in self.stopped if row.unseen)

    @property
    def needs_you_works(self) -> tuple[FrontRow, ...]:
        """Every Work in Needs you on its own row, a parent's folded children unfolded (#3622).

        What tells a person per Work (the top bar's list, a needs-you wake-on) reads this, so a
        child's question is still its own Work's there, under its own id."""
        found: list[FrontRow] = []
        for row in self.needs_you:
            if row.own is None:
                found.append(row)
                continue
            found.extend(
                item
                for item in (row.own, *row.children)
                if item.section == NEEDS_YOU and item.card.work_id not in row.held
            )
        return tuple(found)

    @property
    def groups(self) -> tuple[FrontGroup, ...]:
        """Needs you by category, in the front page's order; each keeps Needs you's order."""
        from .work_attention import FRONT_ORDER, GLYPHS, LABELS, NOTES

        by: dict[str, list[FrontRow]] = {}
        for row in self.needs_you:
            category = row.attention.category if row.attention is not None else ""
            by.setdefault(category, []).append(row)
        known = [category for category in FRONT_ORDER if category in by]
        # A category the order does not know yet is listed last rather than dropped.
        known += [category for category in by if category not in FRONT_ORDER]
        return tuple(
            FrontGroup(category, LABELS.get(category, "Needs you"), GLYPHS.get(category, "!"),
                       tuple(by[category]), NOTES.get(category, ""))
            for category in known
        )

    def groups_in(self, place: str) -> tuple[FrontGroup, ...]:
        """The groups listed at one place in the front page's flow (``FRONT_PLACES``)."""
        from .work_attention import FRONT_PLACES

        return tuple(
            group for group in self.groups
            if FRONT_PLACES.get(group.category, "cleanup") == place
        )

    @property
    def summary(self) -> tuple[tuple[str, int, str], ...]:
        """``(key, count, words)`` for the counts line at the top, zero counts left out."""
        from .work_attention import (
            ISSUE_CLOSED,
            NEEDS_ANSWER,
            NEEDS_RETRY,
            NEEDS_REVIEW,
            READY_FOR_MERGE,
            RELEASE_BLOCKED,
            WORK_ERROR,
        )

        counts = {group.category: len(group.rows) for group in self.groups}
        items = (
            ("new", self.new, "new"),
            ("running", len(self.active), "running"),
            ("merge", counts.get(READY_FOR_MERGE, 0), "to merge"),
            ("stuck", sum(counts.get(c, 0) for c in (NEEDS_RETRY, RELEASE_BLOCKED, WORK_ERROR)),
             "stuck"),
            ("cleanup", counts.get(ISSUE_CLOSED, 0), "cleanup"),
            ("answer", counts.get(NEEDS_ANSWER, 0),
             summary_words("answer", counts.get(NEEDS_ANSWER, 0))),
            ("review", counts.get(NEEDS_REVIEW, 0),
             summary_words("review", counts.get(NEEDS_REVIEW, 0))),
        )
        return tuple(item for item in items if item[1])

    @property
    def summary_plurals(self) -> dict[str, tuple[str, str]]:
        return SUMMARY_PLURALS

    @property
    def summary_by_project(self) -> dict[str, dict[str, int]]:
        """Each counts-line key's count per project (#2311), zero counts left out, so the
        page can take a project it hides out of the counts line too."""
        projects = {row.card.project for row in (*self.needs_you, *self.active)}
        by: dict[str, dict[str, int]] = {}
        for project in sorted(projects):
            part = replace(
                self,
                needs_you=tuple(row for row in self.needs_you if row.card.project == project),
                active=tuple(row for row in self.active if row.card.project == project),
            )
            for key, count, _ in part.summary:
                by.setdefault(key, {})[project] = count
        return by


def front_sections(
    rows: Sequence[FrontRow],
    *,
    now: datetime | None = None,
    max_active: int = 20,
    hidden: Collection[str] = frozenset(),
) -> FrontSections:
    """The rows sorted into sections, each Work once, and only then bounded.

    Needs you: the most recent Work activity first, never cut; the page groups it by
    category, and each category keeps that order (#2267). Active: the most recent Work
    activity, the first ``max_active`` listed and the rest counted. Live checking (#3578): the
    in-progress Works merged and checked live, never cut.
    Finished: the newest ``MAX_FINISHED`` by when the Work finished (its issue's
    ``closed_at``), within ``RECENT_WORK`` of ``now``; one that finished before that is not
    called recent and is only counted as quiet. A done Work whose chat carried on well after
    it finished goes by that chat's activity instead, for both (#2357). A done Work whose
    finish time is not known yet is counted among the finished ones past the list, never as
    quiet (#2226).
    The Work id breaks every tie, so a refresh with nothing new renders the same order.
    A Work in ``hidden`` leaves Needs you for the Hidden list; every other section is
    unaffected by it (#2238). A paused Work is on the Paused list only (#2548).
    A starred Work goes first in Needs you (so in its category) and in Active (#3116).
    Recently stopped is the ``MAX_STOPPED`` shown Needs you rows with the newest
    ``stopped_at`` (#2283); a row with none yet, or whose issue closed (#3388), is not
    listed there.
    """

    def stamp(row: FrontRow) -> float:
        when = row.card.last_activity
        return when.timestamp() if when is not None else 0.0

    by: dict[str, list[FrontRow]] = {
        NEEDS_YOU: [], IN_PROGRESS: [], FINISHED: [], QUIET: [], PAUSED: []
    }
    unknown = 0  # finished, but when is not known yet: counted as finished, never as quiet
    for row in rows:
        section = row.section
        ended = row.finished_order
        if section == FINISHED and ended is None:
            unknown += 1
            continue
        if section == FINISHED and now is not None and now - ended > RECENT_WORK:
            section = QUIET
        by[section].append(row)
    def order(row: FrontRow) -> tuple[bool, float, str]:
        # A starred Work first in its section (#3116), then the most recent activity.
        return not getattr(row.card, "starred", False), -stamp(row), row.card.work_id

    ranked = sorted(by[NEEDS_YOU], key=order)
    needs = [row for row in ranked if row.card.work_id not in hidden]
    put_away = tuple(row for row in ranked if row.card.work_id in hidden)
    active = sorted(by[IN_PROGRESS], key=order)
    live = [row for row in active if row.live]
    active = [row for row in active if not row.live]
    waiting = [row for row in active if row.waits]
    active = [row for row in active if not row.waits]
    finished = sorted(
        by[FINISHED],
        key=lambda row: (-row.finished_order.timestamp(), row.card.work_id),
    )
    # Archive on a finished row is the same deck-only hide (#2254): it leaves the list too.
    put_away += tuple(row for row in finished if row.card.work_id in hidden)
    finished = [row for row in finished if row.card.work_id not in hidden]
    from .work_attention import ISSUE_CLOSED, UNSTARTED

    # A closed issue only waits for Finish: it is not a stop worth a shortcut (#3388). Nor is
    # an issue no agent ever started on (#3558).
    stopped = sorted(
        (
            row for row in needs
            if row.stopped_at is not None
            and getattr(row.attention, "category", "") not in {ISSUE_CLOSED, UNSTARTED}
        ),
        key=lambda row: (-row.stopped_at.timestamp(), row.card.work_id),
    )[:MAX_STOPPED]
    paused = sorted(by[PAUSED], key=lambda row: (-stamp(row), row.card.work_id))
    shown = (
        *needs, *put_away, *active[:max_active], *waiting, *live, *finished[:MAX_FINISHED],
        *paused,
    )
    repositories = {row.card.source_label.rpartition("#")[0].casefold() for row in shown}
    return FrontSections(
        needs_you=tuple(needs),
        active=tuple(active[:max_active]),
        finished=tuple(finished[:MAX_FINISHED]),
        active_more=max(0, len(active) - max_active),
        finished_more=max(0, len(finished) - MAX_FINISHED) + unknown,
        quiet=len(by[QUIET]),
        one_repository=len(repositories) <= 1,
        hidden=put_away,
        waiting=tuple(waiting),
        live=tuple(live),
        stopped=tuple(stopped),
        paused=tuple(paused),
    )


# --- workspace, in a line per repository ----------------------------------------------------


@dataclass(frozen=True)
class WorkspaceRow:
    """One repository of the Work, as ``name · branch · state``."""

    repository: str
    name: str
    branch: str
    state: str  # clean | dirty | N problems | a claim's state | ""
    problem: bool = False


def _short_names(repositories: Sequence[str]) -> dict[str, str]:
    """``owner/repo`` → ``repo``, unless two different repositories share that name.

    Compared case-folded, as GitHub does: ``Eerovil/Shop`` and ``example-org/shop``
    keep their owners, and one repository listed twice is still one repository.
    """
    distinct = {name.casefold() for name in repositories}
    tails = [name.rsplit("/", 1)[-1].casefold() for name in distinct]
    return {
        name: (
            name.rsplit("/", 1)[-1]
            if tails.count(name.rsplit("/", 1)[-1].casefold()) == 1
            else name
        )
        for name in repositories
    }


def workspace_rows(
    workspace: WorkWorkspace | None, claims: Sequence[WorkspaceLine] = ()
) -> tuple[WorkspaceRow, ...]:
    """The Work's repositories in a line each: the inspected workspace, else its claims."""
    if workspace is None:
        names = _short_names([line.repository for line in claims])
        return tuple(
            WorkspaceRow(
                line.repository, names[line.repository], line.branch, line.state, line.problem
            )
            for line in claims
        )
    names = _short_names([repo.repository for repo in workspace.repositories])
    rows = []
    for repo in workspace.repositories:
        problems = len(repo.problems)
        dirty = [cwd.dirty for cwd in repo.cwds if cwd.dirty is not None]
        if problems:
            state = f"{problems} {'problem' if problems == 1 else 'problems'}"
        elif any(dirty):
            state = "dirty"
        elif dirty:
            state = "clean"
        else:
            state = ""
        branch = repo.branches[0] if repo.branches else ""
        rows.append(
            WorkspaceRow(repo.repository, names[repo.repository], branch, state, bool(problems))
        )
    return tuple(rows)


# --- where a message to the Work goes -------------------------------------------------------


@dataclass(frozen=True)
class MessageTarget:
    """The chat a Work-level message is delivered to, chosen by the deck, not the operator.

    ``answer`` is true only when a message to that chat is the answer to its question
    (``WorkAttention.answered_by_message``), so the page offers **Answer question** rather
    than a plain message. An interaction form or approval on the chat's own page never sets
    it (#2224).
    """

    session_key: str
    title: str
    href: str
    answer: bool = False
    why: str = ""


def message_target(
    card: WorkCard,
    chats: Sequence[WorkChat],
    can_send: Callable[[str], bool],
) -> MessageTarget | None:
    """Which chat takes a message sent to this Work, or None when none can.

    In order: a chat whose question is the Work's attention; a chat waiting on its own page;
    a chat that stopped and is the Work's attention; a working chat; the most recently
    active chat. Only a chat that can take a message now (``can_send``) is chosen.
    """

    def chat_of(session_key: str) -> WorkChat | None:
        return next(
            (
                chat
                for chat in chats
                if chat.session_key == session_key
                or session_key in chat.legs
            ),
            None,
        )

    def target(chat: WorkChat | None, key: str, title: str, href: str, **fields: Any):
        if chat is not None:
            key, title, href = chat.session_key, chat.title, chat.href
        return MessageTarget(key, title, href, **fields) if can_send(key) else None

    def why(item: WorkAttention) -> str:
        if item.answered_by_message:
            return "its question is what this Work waits on"
        if item.interaction:
            return "it waits on you on its own page, and a message does not settle that"
        return "it stopped and is waiting for a look"

    answering = [item for item in card.attention if item.answered_by_message]
    waiting = [item for item in card.attention if item.interaction]
    # The status's own test (#2327): a chat that handed off, or one that restarts by itself,
    # has not stopped for a look.
    stopped = [
        item
        for item in waiting_attention(card)
        if not (item.asks or item.interaction or item.recovering)
    ]
    for item in (*answering, *waiting, *stopped):
        found = target(
            chat_of(item.session_key),
            item.session_key,
            item.chat_title,
            item.href,
            answer=item.answered_by_message,
            why=why(item),
        )
        if found is not None:
            return found
    ordered = sorted(
        chats,
        key=lambda chat: (
            chat.working,
            chat.last_activity.timestamp() if chat.last_activity else 0.0,
        ),
        reverse=True,
    )
    for chat in ordered:
        why = "it is working on this Work" if chat.working else "it is this Work's latest chat"
        found = target(chat, chat.session_key, chat.title, chat.href, why=why)
        if found is not None:
            return found
    return None


def opening_chat(
    target: MessageTarget | None, chats: Sequence[WorkChat]
) -> tuple[str, str] | None:
    """``(session key, path)`` of the chat a Work opens into: the one a message would go to,
    else the first of the Work's chats, else None. A desktop opens it beside the Work
    (#2345) and a phone's front-page row opens it instead of the Work (#2512), so both
    layouts read this one choice."""
    if target is not None:
        return target.session_key, target.href
    if chats:
        return chats[0].session_key, chats[0].href
    return None
