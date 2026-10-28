"""Pure deterministic attention policy for Deckhand."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .git_context import GitContext, PullRequestContext
from .models import Capability, PendingInteraction, Session, SessionStatus
from .providers.claude_code.auth import looks_like_auth_failure

_BLOCKED_ISSUE_RE = re.compile(r"(?<![\w-])claude:blocked(?![\w-])", re.IGNORECASE)

KIND_WAITING = "waiting"
KIND_FINISHED = "finished"
_PRIORITY = {KIND_WAITING: 0, KIND_FINISHED: 1}

# Why a chat stopped working. The kind says what the deck shows; the cause says
# which branch of ``stopped_card`` named it, so two branches sharing a kind no
# longer collapse into one another. The cause never decides *whether* there is a
# card — only the working → not-working edge does that (#1450).
CAUSE_PENDING_INTERACTION = "pending_interaction"
CAUSE_SESSION_WAITING = "session_waiting"
CAUSE_BLOCKED_ISSUE = "blocked_issue"
CAUSE_STALLED = "stalled"
CAUSE_REPORTED_STATE = "reported_state"
CAUSE_AGENT_HANDOFF = "agent_handoff"
# A deck-owned session whose process is gone while its turn was still open (#426).
CAUSE_DIED_MID_TURN = "died_mid_turn"
# A deck-owned session whose latest turn ended in a runtime-reported failure
# instead of a reply (#778).
CAUSE_TURN_FAILED = "turn_failed"
# A turn that failed because its account's sign-in expired (#3354): a person signs in again,
# so it is not counted with the failures that mean AgentDeck broke.
CAUSE_LOGIN_EXPIRED = "login_expired"

CAUSES = frozenset(
    {
        CAUSE_PENDING_INTERACTION,
        CAUSE_SESSION_WAITING,
        CAUSE_BLOCKED_ISSUE,
        CAUSE_STALLED,
        CAUSE_REPORTED_STATE,
        CAUSE_AGENT_HANDOFF,
        CAUSE_DIED_MID_TURN,
        CAUSE_TURN_FAILED,
        CAUSE_LOGIN_EXPIRED,
    }
)


@dataclass(frozen=True)
class AssistantInsight:
    session_key: str
    kind: str
    # Half of the push identity ``(session_key, headline)``, and what an owed push
    # is checkpointed under. The UI identifies a card by the session title.
    headline: str
    detail: str = ""
    # Deck-internal: which branch of ``stopped_card`` raised this card. Outside
    # the push identity, so a cause change never splits one attention into two
    # pushes. Excluded from equality for the same reason: the rendered card is
    # unchanged, so a cause-only difference must not read as a view change and
    # swap live fragments. Empty for cards that did not come from triage.
    cause: str = field(default="", compare=False)


@dataclass(frozen=True)
class AssistantView:
    """Every chat that needs attention, as of one pass. ``state`` is "idle" until
    the deck's first pass and "ready" after it."""

    state: str = "idle"
    insights: tuple[AssistantInsight, ...] = ()
    # The scan clock is not a material view change by itself. Otherwise every
    # quiet Deckhand pass publishes an ``assistant`` event and every SSE
    # connection reruns the same Jinja render.
    analyzed_at: datetime | None = field(default=None, compare=False)
    error: str | None = None


def attention_card(
    session_key: str, state: str, cause: str, detail: str = ""
) -> AssistantInsight:
    """Build one card. ``cause`` is required so no branch can omit it."""
    return AssistantInsight(session_key, state, state, detail=detail, cause=cause)


def card_priority(insight: AssistantInsight) -> int:
    """Waiting sessions sort before finished handoffs."""
    return _PRIORITY.get(insight.kind, 0)


def blocked_issue_handoff(session: Session) -> bool:
    """Whether an open kanban worker has emitted its structured block marker."""
    return bool(
        session.worker_type == "kanban"
        and session.issue_status_kind == "open"
        and _BLOCKED_ISSUE_RE.search(session.last_text or "")
    )


def stopped_card(
    session: Session,
    interaction: PendingInteraction | None,
    *,
    reported_state: str | None = None,
    reported_detail: str = "",
) -> AssistantInsight:
    """The card for a chat that has stopped working.

    Whether there is attention at all is decided elsewhere: the chat is at rest
    and has moved since it was last opened (#2599). This function never decides
    that, so it always returns a card. Every branch here only chooses the
    words, picking the most specific true reason the chat stopped.

    Nothing about GitHub appears below, deliberately. A merged pull request, a
    closed issue, a fresh review and a red build all leave the chat exactly as it
    was, so under the rule none of them is attention. They remain decoration on a
    card that exists for its own reason.

    There is no clock here either. Attention is not earned by resting and not
    retired by resting; opening the chat clears it, and so does the chat working
    again.
    """
    key = session.key

    if interaction is not None:
        return attention_card(key, KIND_WAITING, CAUSE_PENDING_INTERACTION)

    if session.is_waiting:
        return attention_card(key, KIND_WAITING, CAUSE_SESSION_WAITING)

    if blocked_issue_handoff(session):
        return attention_card(key, KIND_WAITING, CAUSE_BLOCKED_ISSUE)

    # A stall is not a separate kind of attention any more. ``Session.thinking``
    # is already false while a turn is stalled (``models.py`` ``turn_state``), so
    # stalling *is* the working → not-working edge that produced this card; all
    # that is left is to say so.
    if session.stalled:
        return attention_card(key, KIND_WAITING, CAUSE_STALLED)

    # A deck-owned session whose process is gone while its turn was still open
    # (#426). `stalled` cannot catch this: `turn_stalled` returns False the moment
    # liveness is false. Without this branch the worker falls through to the
    # ordinary handoff below and reads as `finished`, so a death looks like
    # completion.
    #
    # Gated on INJECT so it only fires for sessions the deck owns and the operator
    # could actually act on, not for read-only transcripts that happen to end on a
    # user or tool event.
    if (
        session.status is not SessionStatus.LIVE
        and Capability.INJECT in session.capabilities
        and session.last_role in ("tool", "user")
    ):
        return attention_card(key, KIND_WAITING, CAUSE_DIED_MID_TURN)

    # A turn that failed instead of replying (#778).
    if session.turn_failed:
        expired = session.last_role == "agent" and looks_like_auth_failure(session.last_text)
        return attention_card(
            key, KIND_WAITING, CAUSE_LOGIN_EXPIRED if expired else CAUSE_TURN_FAILED
        )

    if reported_state == KIND_WAITING:
        return attention_card(
            key, KIND_WAITING, CAUSE_REPORTED_STATE, detail=reported_detail
        )

    if reported_state == KIND_FINISHED:
        return attention_card(key, KIND_FINISHED, CAUSE_REPORTED_STATE)

    # Nothing more specific is true, so the chat simply stopped. That is still
    # attention: a finished chat waits until it is opened, rather than being
    # quietly forgotten.
    return attention_card(key, KIND_FINISHED, CAUSE_AGENT_HANDOFF)


@dataclass(frozen=True)
class DeckhandStatus:
    """The resolved Deckhand status pill for one session."""

    state: str  # waiting|finished|merged|closed
    label: str
    title: str


# The issue badge kinds that mean the work item is closed: an issue closed as
# completed or as not-planned, and a pull request closed without merging. A
# merged pull request is terminal too but has its own presented kind below.
_CLOSED_ISSUE_KINDS = frozenset({"done", "dropped", "closed"})


def issue_is_closed(session: Session) -> bool:
    """The GitHub item this session was dispatched for is closed, unmerged."""
    return session.issue_status_kind in _CLOSED_ISSUE_KINDS


def issue_is_merged(session: Session) -> bool:
    """The GitHub item this session was dispatched for merged (#1744).

    A chat dispatched straight at a pull request, or at an issue GitHub resolved
    as a merge, carries the merge here and nowhere else: it has no *registered*
    pull request, so ``has_merged_pr`` is false for it and its row used to keep
    the green ``finished`` colour while its own pill read *issue merged*.
    """
    return session.issue_status_kind == "merged"


def presented_attention_kind(
    kind: str, *, merged: bool = False, issue_closed: bool = False
) -> str:
    """Render lifecycle context without changing the durable attention kind.

    A finished handoff whose pull request merged reads ``merged``; one whose
    issue is closed without a merge reads ``closed`` (#1113). A waiting handoff
    whose issue closed also reads ``closed`` (#1270): the work-item lifecycle is
    more useful than a stale request for attention. Merging wins over closure
    for finished handoffs because it is the more specific fact about the work.
    """
    if kind == KIND_FINISHED and merged:
        return "merged"
    if kind in (KIND_WAITING, KIND_FINISHED) and issue_closed:
        return "closed"
    return kind


def resolve_deckhand_status(
    session: Session, live_insight: AssistantInsight, *, merged: bool = False
) -> DeckhandStatus:
    """The colour of a row whose chat needs attention.

    A merged pull request or work item recolours it ``merged``, a closed one
    ``closed`` (#1113, #1744); otherwise it says why the chat stopped.
    """
    # A registered merged pull request and a merged work item are the same fact
    # about the same piece of work; either one colours the row merged (#1744).
    state = presented_attention_kind(
        live_insight.kind,
        merged=merged or issue_is_merged(session),
        issue_closed=issue_is_closed(session),
    )
    return DeckhandStatus(state, state, f"Deckhand: {state}")


@dataclass(frozen=True)
class WorkItemPill:
    """The GitHub work item one session row wears — its pull request or its issue.

    Never both (#1690). A chat whose issue is still an open card on the poller's
    board is read through that card's column, because the column is what says how
    far the work has got (#1696); once the issue is no longer an open card, the
    chat is read through its pull request, the later stage of the same piece of
    work, and failing that through the issue's own GitHub state. The row's
    Deckhand state is no longer spelled out beside this — it is this pill's
    colour, which is why the pill carries no attention wording of its own.
    """

    label: str
    title: str
    # Whether the work item still wants something. A draft pull request counts as
    # open. This is what the list's "hide closed" filter reads, so it must stay a
    # fact about the item and not about how the pill happens to be coloured.
    is_open: bool
    # The short commit a review passed on, when that commit is still the one this
    # row speaks for (#2121). Empty means the row claims nothing: no verdict, a
    # verdict that was not a pass, or a verdict about a commit the head has left.
    # It is deliberately a commit and not a boolean — a claim about review has to
    # name which commit it covers, or it cannot be checked.
    reviewed_sha: str = ""

    @property
    def review_note(self) -> str:
        """The reviewed-commit clause for a title or a screen reader, or ""."""
        if not self.reviewed_sha:
            return ""
        return f"latest commit {self.reviewed_sha} passed review"


# Which pull request a row speaks for when a chat has several. Open outranks
# merged outranks closed: the one still needing something is the one worth a
# row, and an unrecognised status never silently wins by ordering alone.
_PULL_PREFERENCE = ("open", "merged", "closed")


def _preferred_pull(git: GitContext | None) -> PullRequestContext | None:
    """The one pull request this row speaks for, or None when there is none."""
    if git is None or not git.pull_requests:
        return None
    for status in _PULL_PREFERENCE:
        for pull in git.pull_requests:
            if pull.status == status:
                return pull
    # An unrecognised status is still a real pull request; resolver ordering
    # (highest number first) picks which one.
    return git.pull_requests[0]


def pill_review_commit(git: GitContext | None) -> tuple[str, str] | None:
    """The repository and commit this row's review claim would be about, or None.

    The pull request's head when the row has one and we know it, because that is
    the commit that would merge and therefore the only one worth claiming on a
    row. Failing that the checkout's own HEAD — the commit the chat page's own
    review chip means (#1127) — so the row and the page never name different
    commits for the same chat.

    The repository is the pull request's own, not the checkout's, because that is
    where the review ran and how the verdict is keyed. None whenever the deck
    cannot name both halves, which is the same "no subject" case the chip has.
    """
    if git is None:
        return None
    pull = _preferred_pull(git)
    if pull is not None and pull.head_sha:
        repository = pull.repository or git.repository
        if repository:
            return (repository, pull.head_sha)
    if git.repository and git.head_sha:
        return (git.repository, git.head_sha)
    return None


def resolve_work_item_pill(
    session: Session,
    git: GitContext | None = None,
    reviewed_sha: str = "",
) -> WorkItemPill | None:
    """Resolve one row's work-item pill from its pull requests and its issue.

    Every half comes from state the deck has already resolved — the git context
    the webhook-fed status cache keeps warm, and the same ``issue_status`` field
    the row's badge read before — so a row never costs a GitHub call to draw.

    ``reviewed_sha`` is the short commit a review passed on, when that commit is
    still the one this row speaks for (#2121). It only ever adds a tick and a
    clause to what the pill already said: which work item the row wears, and
    whether that item is open, are the same with it and without it.
    """
    pull = _preferred_pull(git)
    if pull is not None:
        state = "draft" if pull.is_open and pull.draft else pull.status
        return _work_item_pill(
            label=f"pr {state}",
            title=f"pull request #{pull.number} is {state}",
            is_open=pull.is_open,
            reviewed_sha=reviewed_sha,
        )
    if session.issue_status:
        return _work_item_pill(
            label=f"issue {session.issue_status}",
            title=f"the issue this chat was dispatched for is {session.issue_status}",
            is_open=session.issue_status_kind == "open",
            reviewed_sha=reviewed_sha,
        )
    return None


def _work_item_pill(
    *, label: str, title: str, is_open: bool, reviewed_sha: str
) -> WorkItemPill:
    """One pill, wearing the reviewed-commit tick when there is a commit to wear.

    The tick is appended to the words rather than drawn as a second chip: a row
    must not grow to say this, and the pill is already the row's one place for
    what the work item is. The words behind it live in the title, because a tick
    on its own does not say which commit passed.

    That clause is parenthetical rather than a third ` · ` segment: the rendered
    title is "work half · Deckhand state", and inside brackets it reads as part
    of the half it belongs to.
    """
    sha = reviewed_sha.strip()
    if not sha:
        return WorkItemPill(label=label, title=title, is_open=is_open)
    return WorkItemPill(
        label=f"{label} ✓",
        title=f"{title} (latest commit {sha} passed review)",
        is_open=is_open,
        reviewed_sha=sha,
    )
