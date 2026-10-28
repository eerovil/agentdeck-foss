"""What a person is asked for on a Work, in one of eight words-first categories (#2236).

A small reading laid over the technical status (``work_story.work_status``), never a
replacement for it: the status says what the Work is doing and why, and this says which of
the operator's few decisions it is waiting on. The front page's Needs you rows and the Work
page read the same ``operator_attention``, so the two cannot name a Work's wait differently.

Nothing is stored and nothing schedules on it. Whether a Work needs a person at all is still
``work_story``'s answer (``_needs_you``); this only sorts the Works it already put there, so
automatic recovery, a fix round the deck runs itself and a card waiting on the issues it names
stay out by construction (#2235). The category is picked from facts, never from a label:
``WorkStatus.kind`` for the kind of human attention and ``ReviewSummary.cause`` for why a
review or release needs one.

Actions are data here. Each category lists the action ids it allows, in order; the Work
action endpoint (``work_actions``, #2237) is what may perform the ones that write, and only
while the Work still lists them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from . import review_refs
from .poller_engine.revive import MAX_REVIVES
from .run_trace import CAPACITY_REFUSALS
from .work_overview import (
    CAUSE_CHANGES_ASKED,
    CAUSE_CLOSED_UNMERGED,
    CAUSE_CONFLICT,
    CAUSE_HUMAN_REVIEW,
    CAUSE_MERGED_OPEN,
    CAUSE_NO_OUTPUT,
    CAUSE_NOT_LANDED,
    CAUSE_RELEASE_PARKED,
    CAUSE_REVIEWED_CLEAN,
    CAUSE_REVIEWER,
    DECIDE_CLARIFICATION,
    REVIEW_FAILED,
    REVIEW_PASSED,
    WorkCard,
    _reviewer_label,
    ci_state,
    clean_at_head,
    conflicts,
    failed_verification_holds,
    verification_covers,
)
from .work_story import (
    CHAT_ACTION,
    CHAT_QUESTION,
    CHAT_STOPPED,
    CLARIFICATION,
    DISPATCH_FAILED,
    DONE,
    MERGE_BLOCKED,
    MERGE_DECISION,
    NOT_STARTED,
    RELEASE_DECISION,
    REVIEW_DECISION,
    WorkStatus,
    _needs_you,
    waiting_attention,
)

# The categories, in the operator's words.
READY_FOR_MERGE = "ready_for_merge"
NEEDS_ANSWER = "needs_answer"
NEEDS_REVIEW = "needs_review"
NEEDS_RETRY = "needs_retry"
RELEASE_BLOCKED = "release_blocked"
WORK_ERROR = "work_error"
MERGED_ISSUE_OPEN = "merged_issue_open"
CLOSED_UNMERGED = "closed_unmerged"
# The issue closed, by a merge, the Poller or a person on GitHub, and nobody has finished the
# Work on the deck yet (#2510): a Work is over only when a person says so.
ISSUE_CLOSED = "issue_closed"
# An open issue no agent works on (#3558): a person starts one, or handles it themselves.
UNSTARTED = "unstarted"

LABELS = {
    READY_FOR_MERGE: "Ready for merge",
    NEEDS_ANSWER: "Needs answer",
    NEEDS_REVIEW: "Needs review",
    NEEDS_RETRY: "Needs retry",
    RELEASE_BLOCKED: "Release blocked",
    WORK_ERROR: "Work error",
    MERGED_ISSUE_OPEN: "Merged, issue still open",
    CLOSED_UNMERGED: "PR closed without merge",
    ISSUE_CLOSED: "Issue closed — finish it?",
    UNSTARTED: "Not started",
}

# Action ids. The link ones only open a page; the rest write, and none of them is performed
# here.
ANSWER = "answer"
OPEN = "open"
INSPECT = "inspect"
REVIEW = "review"
MERGE = "merge"
CLOSE = "close"
AGENT_REVIEW = "agent_review"
CHATGPT_REVIEW = "chatgpt_review"
HIDE = "hide"
FINISH = "finish"
# A person's pause on the Work, and the end of it (#2548): the paused label on its issue,
# offered from the Work's own state rather than from a category (``work_actions.pausable``).
PAUSE = "pause"
RESUME = "resume"
# Adds the repository's ownership label to a Work's issue, so an agent starts on it (#3558).
START = "start"

LINKS = {ANSWER: "Answer", OPEN: "Open", INSPECT: "Inspect", REVIEW: "Review"}

# The front page's Needs you groups, in the order a Work moves through them (#3578, replacing
# #2254's): not started, a question, a stopped Run, a review, its release, then clean-up. Each
# has a glyph so the category never rests on colour alone.
FRONT_ORDER = (
    UNSTARTED,
    NEEDS_ANSWER,
    NEEDS_RETRY,
    WORK_ERROR,
    NEEDS_REVIEW,
    RELEASE_BLOCKED,
    READY_FOR_MERGE,
    MERGED_ISSUE_OPEN,
    CLOSED_UNMERGED,
    ISSUE_CLOSED,
)
# Where each group sits among the front page's other groups (#3578), one keyed part each:
# ``start`` above Working now, ``stuck`` between it and Waiting, ``release`` after Waiting and
# before Live checking, ``cleanup`` after that. A category not named here is clean-up.
FRONT_PLACES = {
    UNSTARTED: "start",
    NEEDS_ANSWER: "start",
    NEEDS_RETRY: "stuck",
    WORK_ERROR: "stuck",
    NEEDS_REVIEW: "release",
    RELEASE_BLOCKED: "release",
    READY_FOR_MERGE: "release",
}
GLYPHS = {
    READY_FOR_MERGE: "↑",
    NEEDS_ANSWER: "?",
    NEEDS_REVIEW: "◐",
    NEEDS_RETRY: "↻",
    RELEASE_BLOCKED: "⊘",
    WORK_ERROR: "!",
    MERGED_ISSUE_OPEN: "✓",
    CLOSED_UNMERGED: "×",
    ISSUE_CLOSED: "☐",
    UNSTARTED: "○",
}
# A short line beside a group's header, where the label alone does not say why a Work is there.
NOTES = {
    NEEDS_RETRY: "stopped; nothing restarts it on its own",
    UNSTARTED: "no agent label; start one or handle it yourself",
}

# Why a Work in Needs retry is there because AgentDeck broke, not because the work did (#2888).
# Red CI, a failed test run, a conflict, asked-for changes, a missing verification and a host
# with no room are ordinary outcomes and carry none of these.
BUG_WORKER_DIED = "worker_died"
BUG_RUN_FAILED = "run_failed"
BUG_REVIEW_RUN_FAILED = "review_run_failed"
BUG_START_REFUSED = "start_refused"
BUG_CHAT_STOPPED = "chat_stopped"
# A deck-run Work's agent ended a turn without a next step even after the deck asked (#2960).
BUG_ENDED_WITHOUT_NEXT_STEP = "ended_without_next_step"
# A ready pull request's review chat went quiet with no reviewer running (#2943, #2960).
BUG_REVIEW_STUCK = "review_stuck"
# The Deckhand causes that mean a chat's turn died or failed, rather than ended quietly.
_CHAT_ERRORS = frozenset({"died_mid_turn", "turn_failed"})


@dataclass(frozen=True)
class OperatorAttention:
    """The one decision a Work waits on, and what the operator may do about it.

    ``reason`` is one factual line, "" when the category says it all. ``actions`` are the
    allowed action ids, strongest first, always ending in ``hide``. ``target`` names the
    branch its pull requests merge into, only when that is not ``master``, and
    ``review_url`` is the pull request the ``review`` action opens.
    """

    category: str
    reason: str
    actions: tuple[str, ...]
    target: str = ""
    review_url: str = ""
    # Set only when the Work waits here because AgentDeck broke (#2888): the cause it is
    # reported under, one of the ``BUG_*`` names, a refused start's carrying the refusal.
    bug: str = ""

    @property
    def label(self) -> str:
        return LABELS[self.category]


def _asked(card: WorkCard, column: str) -> str:
    """A parked Work's question, as its worker last put it; else the column it rests in."""
    return card.question or f"The card is in {column}"


def _blocked(card: WorkCard) -> bool:
    """Parked in Blocked rather than Needs More Info: nothing may have been asked (#2332)."""
    return "blocked" in {card.resting, card.parked}


def _ask(card: WorkCard, column: str, made) -> OperatorAttention:
    """A parked Work the worker handed to a person: its question when it asked one, else, in
    Blocked, what it said blocks it, with closing the issue as the other way out (#2332)."""
    if card.question or not _blocked(card):
        return made(NEEDS_ANSWER, _asked(card, column), ANSWER)
    reason = (
        f"Blocked: {card.park_reason}"
        if card.park_reason
        else "Blocked; the worker recorded no reason"
    )
    return made(NEEDS_ANSWER, reason, CLOSE, ANSWER)


def _open_outputs(card: WorkCard) -> list:
    return [output for output in card.outputs if not output.settled]


def _review_actions(card: WorkCard) -> tuple[str, ...]:
    """The review actions a Work with an open pull request supports.

    Agent review used to be a ``/claude`` follow-up to the card's own worker, withheld where the
    card releases itself because the worker would review its own work and merge on a pass
    (#2234). It is now a separate Claude reviewer on the Work's review chat (#2736), as
    independent of the worker as the ChatGPT reviewer, so it is offered wherever that one is.
    """
    if not _open_outputs(card):
        return ()
    if not review_refs.reviewer_available():
        # An edition with no reviewer (the public copy, #3623): a person reviews and presses
        # Merge, so the two review buttons, which would only refuse, are not offered.
        return (REVIEW,)
    return (AGENT_REVIEW, CHATGPT_REVIEW, REVIEW)


def _review_url(card: WorkCard) -> str:
    review = card.review
    if review is not None and review.pull_request:
        return review.pull_request
    return next((output.url for output in _open_outputs(card)), "")


def operator_attention(card: WorkCard, story: WorkStatus) -> OperatorAttention | None:
    """Which decision the Work waits on, or None when it needs nobody.

    ``story`` is ``work_status(card)``. Every Work ``work_story`` puts in Needs you gets
    exactly one category; one that fits none of the known shapes is a Work error rather than
    a guess.
    """
    row_reason, _, rank = _needs_you(card, story)
    if rank < 0:
        return None
    review = card.review
    target = ", ".join(card.targets)
    url = _review_url(card)

    def made(category: str, reason: str, *actions: str) -> OperatorAttention:
        if category == READY_FOR_MERGE and not verification_covers(
            card.verification, card.outputs, card.ci
        ):
            # Never a merge before a green run covers every open head (#2364).
            return OperatorAttention(
                NEEDS_RETRY, _unverified(card), (INSPECT, HIDE), target, url
            )
        return OperatorAttention(category, reason, (*actions, HIDE), target, url)

    if story.kind == NOT_STARTED:
        # Close issue for one a person handled themselves or does not want done (#3562).
        return made(UNSTARTED, row_reason, START, OPEN, CLOSE)
    if card.derived is not None:
        return _derived(card, row_reason, made)
    if (
        story.tone == DONE
        and card.awaiting_finish
        and not any(output.is_open for output in card.outputs)
    ):
        # Its issue closed; only a person's Finish lets it go (#2510). With a pull request
        # still open, that pull request's state reads first (#3187).
        return made(ISSUE_CLOSED, "Merged" if _merged_any(card) else "", FINISH, ANSWER)

    waiting = waiting_attention(card)

    def needs_more_info() -> OperatorAttention:
        # A Needs More Info park stands for three different waits: a review the board had
        # no column for, restarts the engine ran out of, and a real question (#2235).
        if card.parked_as == "human_review_pending":
            return made(NEEDS_REVIEW, "", *_review_actions(card))
        if card.revives >= MAX_REVIVES:
            reason = f"The worker stopped without a report after {card.revives} restarts"
            return _bug(made(NEEDS_RETRY, reason, INSPECT), BUG_WORKER_DIED)
        return made(NEEDS_ANSWER, _asked(card, card.work_state), ANSWER)

    # A chat's wait keeps the row's own reason, which never names the chat (#2224).
    if story.kind == CHAT_QUESTION:
        first = next(item for item in waiting if item.asks)
        return made(NEEDS_ANSWER, row_reason, ANSWER if first.answered_by_message else OPEN)
    if story.kind == CHAT_ACTION:
        return made(NEEDS_ANSWER, row_reason, OPEN)
    if story.kind == CHAT_STOPPED:
        failed = any(item.cause in _CHAT_ERRORS for item in waiting)
        return _bug(made(NEEDS_RETRY, row_reason, INSPECT), BUG_CHAT_STOPPED if failed else "")
    if story.kind == MERGE_BLOCKED:
        # Nothing in the deck can start another team's CI; the pull request is where to look.
        return made(RELEASE_BLOCKED, row_reason, REVIEW, INSPECT)
    if story.kind == DISPATCH_FAILED:
        reason = card.run_reason or "The worker never started"
        return _bug(made(NEEDS_RETRY, reason, INSPECT), _refused(card))
    if story.kind in {REVIEW_DECISION, RELEASE_DECISION, MERGE_DECISION, CLARIFICATION} and (
        review is not None and review.needs_human
    ):
        return _from_review(card, made, needs_more_info)
    if story.kind == CLARIFICATION:
        return needs_more_info()
    if card.parked:
        # The worker handed the card back without naming what it waits on: a person decides.
        return _ask(card, card.work_state or card.parked, made)
    reviewer_failed = (
        review is not None and review.state == REVIEW_FAILED and review.run_id == card.run_id
    )
    if card.stuck and not reviewer_failed:
        reason = f"The current Run is {card.run_label}"
        reason = f"{reason}: {card.run_reason}" if card.run_reason else reason
        if card.run_refusal in CAPACITY_REFUSALS:
            bug = ""  # the host had no room: not AgentDeck's fault
        else:
            bug = BUG_WORKER_DIED if card.revives >= MAX_REVIVES else BUG_RUN_FAILED
        return _bug(made(NEEDS_RETRY, reason, INSPECT), bug)
    if review is not None and review.state == REVIEW_FAILED:
        attention = made(NEEDS_RETRY, review.label, INSPECT, *_review_actions(card))
        return _bug(attention, BUG_REVIEW_RUN_FAILED)
    verification = card.verification
    if failed_verification_holds(verification, card.run_id, card.ci):
        # A red final suite on a clean review: the Verify step's, never a merge (#2352).
        return made(NEEDS_RETRY, verification.label, INSPECT)
    if ci_state(card.ci) == "failing":
        # Red CI on a head whose CI is its verification (#2364): the worker's repair to run.
        # The front page names no pull request (#3720); the Work page lists which failed.
        return made(NEEDS_RETRY, "CI failed", INSPECT)
    if review is not None and review.state == REVIEW_PASSED and not verification_covers(
        verification, card.outputs, card.ci
    ):
        # A clean review no green run covers, on a project that does not ask for one (#2364).
        return made(NEEDS_RETRY, _unverified(card), INSPECT)
    return made(WORK_ERROR, story.summary.rstrip("."), INSPECT)


def _derived(card: WorkCard, row_reason: str, made) -> OperatorAttention:
    """The category of a Work the deck runs itself, from its derived status alone (#2944).

    A question waits on an answer; a died or stalled turn on a person to start it again, a
    merged or closed issue on a person to finish it; a reviewed and tested pull request on a
    person's Merge. A turn that ended without a next step after the deck asked for one, and a
    review no reviewer ran, are AgentDeck bugs (#2960): the deck asks for a next step and starts
    every review by itself.
    """
    kind = card.derived.kind
    if kind in {"question", "review_escalated", "spec_wait"}:
        # The reviewer's escalation is a question for the owner too (#3216), and so is a
        # posted spec waiting for approval (#3247).
        return made(NEEDS_ANSWER, row_reason, OPEN)
    if kind == "ready":
        # The release checked the review and the tests at these heads itself (#2960), which is
        # what ``made``'s verification gate stands in for on a Poller card.
        return OperatorAttention(
            READY_FOR_MERGE, row_reason, (MERGE, OPEN, HIDE), ", ".join(card.targets),
            _review_url(card),
        )
    if kind == "unfinished":
        cause = BUG_ENDED_WITHOUT_NEXT_STEP if card.derived.bug else ""
        return _bug(made(NEEDS_RETRY, row_reason, ANSWER, OPEN), cause)
    if kind == "died":
        return _bug(made(NEEDS_RETRY, row_reason, ANSWER, OPEN), BUG_CHAT_STOPPED)
    if kind == "login_expired":
        # The account's sign-in expired (#3354): a person signs in again, not AgentDeck's bug.
        return made(NEEDS_RETRY, row_reason, ANSWER, OPEN)
    if kind == "cut_off":
        # Its process died mid-turn and no revival is coming (#3008): a person starts it
        # again. Not filed as a bug: a restart or a memory kill is not the deck's defect.
        return made(NEEDS_RETRY, row_reason, ANSWER, OPEN)
    if kind == "stalled":
        return made(NEEDS_RETRY, row_reason, OPEN)
    if kind == "closed":
        # With what the agent's live check found, which the status line carries (#3141): its
        # result words only, and no pull request names (#3720). The Work page has the note.
        live = card.derived.summary.partition(" · ")[2].partition(": ")[0]
        said = " · ".join(part for part in ("Merged" if _merged_any(card) else "", live) if part)
        return made(ISSUE_CLOSED, said, FINISH, ANSWER)
    if kind == "live_failed":
        # The merged change failed its live check (#3141): its agent reopens the issue, and
        # until it does a person reads why, and may still finish it.
        return made(NEEDS_ANSWER, row_reason, OPEN, FINISH)
    if kind == "not_live":
        # Merged, but part of it is not running yet (#3220): someone deploys it, then the
        # agent checks again; no Finish until then.
        return made(NEEDS_ANSWER, row_reason, OPEN)
    if kind == "pr_held":
        # The spec-only label (#3097): nothing in the deck merges it, a person decides.
        return made(RELEASE_BLOCKED, row_reason, OPEN, INSPECT)
    if kind == "pr_stuck":
        # A pull request whose hold nothing moved for hours (#3097): a lost pass, findings that
        # never arrived, tests nobody ran. Not filed as a bug: a slow lane is not the deck's.
        return made(NEEDS_RETRY, row_reason, OPEN, INSPECT)
    if kind == "review_stuck":
        # Every ready head is reviewed by itself, so a review nobody ran is the deck's (#2960).
        return _bug(
            made(NEEDS_RETRY, row_reason, OPEN, *_review_actions(card)), BUG_REVIEW_STUCK
        )
    return made(WORK_ERROR, row_reason, INSPECT)


def _bug(attention: OperatorAttention, cause: str) -> OperatorAttention:
    """``attention`` carrying the bug it is reported under, if any (#2888)."""
    if not cause or attention.category != NEEDS_RETRY:
        return attention
    return replace(attention, bug=cause)


def _refused(card: WorkCard) -> str:
    """A start that never landed is a bug unless the host only had no room for it (#2888)."""
    refusal = card.run_refusal
    if refusal in CAPACITY_REFUSALS:
        return ""
    named = re.sub(r"[^A-Za-z0-9_.-]", "_", refusal)[:60] or "unknown"
    return f"{BUG_START_REFUSED}:{named}"


def _unverified(card: WorkCard) -> str:
    """Why a clean review is not a merge yet: no green run covers its head (#2364)."""
    return "Verification not run"


def _merged_any(card: WorkCard) -> bool:
    return any(output.state == "merged" for output in card.outputs)


def _pull_requests(count: int) -> str:
    """``1 pull request`` or ``3 pull requests``: a card names none (#3720)."""
    return f"{count} pull request" + ("" if count == 1 else "s")


def _from_review(card: WorkCard, made, needs_more_info) -> OperatorAttention:
    """A review or release that needs a person, by ``ReviewSummary.cause``."""
    review = card.review
    assert review is not None
    cause = review.cause
    if review.blocker:
        # The engine's own words for why it stopped the release (#2235). One whose work all
        # merged is the issue left to close.
        if not _open_outputs(card) and any(o.state == "merged" for o in card.outputs):
            again = "; AgentDeck is re-checking the deploy" if card.deploy_recheck else ""
            return made(RELEASE_BLOCKED, review.blocker + again, CLOSE, INSPECT)
        return made(RELEASE_BLOCKED, review.blocker, INSPECT)
    if cause == CAUSE_RELEASE_PARKED:
        if review.decision == DECIDE_CLARIFICATION:
            return needs_more_info()
        if review.heads:
            # A passed release that stopped in Blocked with nothing saying why.
            reason = f"The release stopped in {card.work_state} without a recorded reason"
            return made(WORK_ERROR, reason, INSPECT)
        return _ask(card, card.work_state, made)
    if cause == CAUSE_MERGED_OPEN:
        # The label says it merged; which pull requests is the Work page's (#3720).
        if card.resting in {"blocked", "needs_more_info"}:
            # Parked with a question the merge may have answered: the question still shows,
            # and can still be answered (#2332).
            asked = f"asked: {card.question}" if card.question else ""
            return made(MERGED_ISSUE_OPEN, asked, CLOSE, ANSWER)
        return made(MERGED_ISSUE_OPEN, "", CLOSE)
    if cause == CAUSE_CLOSED_UNMERGED:
        return made(CLOSED_UNMERGED, "", CLOSE)
    if cause == CAUSE_NOT_LANDED:
        outputs = _open_outputs(card)
        if not outputs:
            return made(WORK_ERROR, "Ready to merge, but no open pull request is known", INSPECT)
        if all(_passed(output.head, review.heads) for output in outputs):
            return made(READY_FOR_MERGE, "", MERGE)
        reason = "The review passed an older head"
        return made(NEEDS_REVIEW, reason, *_review_actions(card))
    if cause == CAUSE_CONFLICT:
        # Nothing can merge it until the base is brought in.
        conflicted = [output for output in _open_outputs(card) if conflicts(output)]
        base = next((output.base for output in conflicted if output.base), "its base branch")
        if len(conflicted) > 1:
            said = f"{_pull_requests(len(conflicted))} conflict with {base}"
        else:
            said = f"Conflicts with {base}"
        return made(NEEDS_RETRY, said, REVIEW, INSPECT)
    if cause == CAUSE_CHANGES_ASKED:
        return made(NEEDS_RETRY, review.label, INSPECT, REVIEW)
    if cause == CAUSE_HUMAN_REVIEW:
        return made(NEEDS_REVIEW, "", *_review_actions(card))
    if cause == CAUSE_REVIEWED_CLEAN:
        # Clean, but the deck does not merge it from here: it is merged on GitHub (#2327).
        return made(
            READY_FOR_MERGE, "Clean review at the current head; merge it on GitHub", REVIEW
        )
    if cause == CAUSE_REVIEWER:
        return made(NEEDS_REVIEW, _reviewer_reason(card, review.label), *_review_actions(card))
    if cause == CAUSE_NO_OUTPUT:
        return made(WORK_ERROR, "In review, but no pull request is known", INSPECT)
    return made(WORK_ERROR, review.label, INSPECT)


# The review's own test, so the Work page and the category never read a head differently.
_clean_output = clean_at_head


def _reviewer_reason(card: WorkCard, label: str) -> str:
    """One line for why the reviewer leaves a Work to a person, never the same label twice.

    One open output keeps the review's own label. With several, the ones not reviewed clean
    are counted under their label, never named (#3720): the Work page lists which.
    """
    outputs = _open_outputs(card)
    if len(outputs) < 2:
        return label
    waiting = [output for output in outputs if not _clean_output(output)]
    if not waiting:
        # Every output is clean, but the card cannot take a merge (off the board, or in a
        # column the release does not accept): the review's own label says why.
        return label
    labels = [
        _reviewer_label(item.url, item.review, item.review_detail, item.review_held)
        for item in waiting
    ]
    if len(set(labels)) == 1:
        return f"{labels[0]} on {len(waiting)} of {_pull_requests(len(outputs))}"
    return f"{labels[0]} on 1 of {_pull_requests(len(outputs))} (+{len(waiting) - 1} more)"


def _passed(head: str, passed: tuple[str, ...]) -> bool:
    """The output's current head is one a review passed; either side may be abbreviated."""
    head = head.strip().lower()
    if len(head) < 7:
        return False
    return any(
        len(item) >= 7 and (head.startswith(item) or item.startswith(head))
        for item in (value.strip().lower() for value in passed)
    )

