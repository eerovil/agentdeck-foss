"""The Work card: what one Work is, where it has got to and what it needs (#2147).

This is the read model the Work-first UI renders. A **Work** is the human task (#2138), and
this module turns what the deck already knows about one into the handful of facts an
operator reads without opening a chat:

* what it is: its source issue and title, and the Work's own state (its board column, or the
  issue's state when no Poller card tracks it);
* what is happening: the current Run from the Run trace (#2141), never the newest session;
* where it is stuck: the current Run's reason and what the deck expects next;
* what needs attention: the Deckhand cards this Work owns (see ``attribute_insights``);
* which workspace: the Work's repository claims (#2149), one line per repository;
* what exists: the newest explicit verification (#2148), where review has got to (#2216),
  and the pull requests attributed to the Work itself.

Everything here is derived on every read and nothing is stored. Chats and sessions stay
secondary: they are listed, never used to pick the current Run or to name an output.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .chat_cwd import cannot_resume
from .models import BACKGROUND_TASK_ACTIVITY, IssueRef
from .poller_engine.schedules import LABEL as SCHEDULE_LABEL
from .review_fallback import is_reviewer_prompt
from .run_trace import (
    FAILED,
    FINISHED,
    NEEDS_ATTENTION,
    NEXT_HUMAN,
    NEXT_RETRY,
    NEXT_REVIEW,
    NEXT_VERIFICATION,
    RUNNING,
    SUPERSEDED,
    UNCERTAIN,
    RunTrace,
    waits_on_person,
)
from .triage import KIND_FINISHED
from .work import Work, WorkEvidence, WorkProjection, WorkRef
from .work_derived import PARKED_LABEL
from .work_facts import (
    DiffStat,
    OutputFact,
    WorkFacts,
    _stamp,
    diffstat,
    pull_identity,
    target_branches,
    work_facts,
)
from .work_hidden import card_mark, issue_comments
from .work_wording import blocked_reason, question
from .work_workspace import RunVerification, WorkWorkspace, parse_work_id
from .work_workspace_claims import WorkspaceClaim

# How long a Work with nothing current stays Recent after its chats last moved (#2201). Past
# this it is Archived: kept whole and addressable, but out of the default views.
RECENT_WORK = timedelta(days=7)
# How long after a Work finished its chats may still move without the Work counting as
# continued (#2357): a worker's own wrap-up after the merge is not someone picking it up again.
CONTINUED_GRACE = timedelta(minutes=15)
# How long a chat outside every Work stays in the dashboard's "Other activity".
RECENT_CHAT = timedelta(hours=24)
# How many Active Works the front page lists before linking the rest (#2219). A display
# limit inside the section, applied after every Work has been sectioned.
MAX_DASHBOARD_WORKS = 20
# How many chats outside every Work the dashboard lists before pointing at the chat list.
MAX_OTHER_CHATS = 12

# Run states that mean the Work is stuck rather than progressing or done.
_STUCK_STATES = frozenset({NEEDS_ATTENTION, FAILED, UNCERTAIN})

def work_href(work_id: str) -> str:
    """The canonical Work page (#2147). The id's ``/`` and ``#`` are escaped, ``:`` kept."""
    return "/works/" + quote(work_id, safe=":")


def chat_href_in_work(href: str, work_id: str) -> str:
    """A shared chat's address that names the Work it was opened from (#2567).

    The chat page links up to that Work, which it cannot tell on its own when several own
    the chat; it is in the address so a reload or Back keeps it.
    """
    return f"{href}{'&' if '?' in href else '?'}work={quote(work_id, safe=':')}"


# --- attention ------------------------------------------------------------------------------


def attribute_insights(
    insights: Iterable[Any],
    projection: WorkProjection,
    chat_of: Mapping[str, str],
) -> dict[str, list[Any]]:
    """Deckhand cards a Work owns, keyed by ``work_id``, in the order they were ranked.

    A card belongs to a Work only when its chat is a member of exactly that one Work. A chat
    working on two Works cannot say which of them its problem is about, so its card stays a
    chat-level card rather than being copied onto both (M2M, #2138).
    """
    owned: dict[str, list[Any]] = {}
    works_by_chat: dict[str, tuple[WorkRef, ...]] = {}
    for insight in insights:
        key = getattr(insight, "session_key", "")
        chat_id = chat_of.get(key, key)
        if chat_id not in works_by_chat:
            works_by_chat[chat_id] = projection.works_for_chat(chat_id)
        works = works_by_chat[chat_id]
        if len(works) == 1:
            owned.setdefault(works[0].work_id, []).append(insight)
    return owned


@dataclass(frozen=True)
class WorkAttention:
    """One Deckhand card this Work owns: the causing chat and its state.

    ``asks`` is true only while the chat really has an unanswered question for the operator
    (its open question, or a question-style pending interaction), and ``question`` is that
    question's text when it has one. ``interaction`` is the kind of the chat's pending
    interaction, if any: that is answered on the chat's own page (``answer_interaction``),
    never by a message, and an approval or a browser step there is not a question at all. A
    Deckhand card alone is not a question either: a chat that finished, stalled or died is
    attention too, but nothing in it can be *answered* (#2224).
    """

    session_key: str
    chat_title: str
    state: str
    href: str
    asks: bool = False
    question: str = ""
    interaction: str = ""
    # The chat is one of this Work's own worker Runs, and the card's current Run ended in a
    # way the engine restarts by itself: a stop there is the engine's to recover (#2235).
    recovering: bool = False
    # Why Deckhand says the chat stopped (``triage.CAUSE_*``): only a turn that died or failed
    # makes a stopped chat an AgentDeck bug (#2888).
    cause: str = ""

    @property
    def answered_by_message(self) -> bool:
        """A message to the chat is its answer: a question with no interaction form."""
        return self.asks and not self.interaction


# --- verification ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationSummary:
    """The newest explicit verification, and whether it speaks for the current Run.

    ``status`` is ``verified`` only for the release rule's own answer on a job of the current
    Run, and only while nothing shows the checkout has moved since. A green job of an earlier
    Run is ``earlier``; a green job whose tested commit is no longer the checkout's is
    ``stale``. Everything names the commit it tested, never "the workspace".
    """

    status: str  # verified | stale | earlier | unproven | running | queued | failed | ...
    label: str
    job_id: str
    run_id: str
    tested: tuple[tuple[str, str], ...]  # (repository, short head)
    moved: tuple[tuple[str, str], ...] = ()  # (repository, current short head) that differ
    # (repository, short head) of an open pull request whose head is no longer the tested
    # commit, though the checkout still is: GitHub moved the branch (#2327).
    superseded: tuple[tuple[str, str], ...] = ()

    @property
    def tested_at(self) -> str:
        """The one short head the job tested, or "" when it tested none or several."""
        return self.tested[0][1] if len(self.tested) == 1 else ""


def _different_commit(one: str, other: str) -> bool:
    """Two heads, either possibly shortened, surely name different commits.

    Too short to compare (or unknown) is never a difference: nothing is claimed on a guess.
    """
    one, other = one.casefold(), other.casefold()
    size = min(len(one), len(other))
    return size >= 7 and one[:size] != other[:size]


def summarize_verification(
    verifications: Sequence[RunVerification],
    current_run_id: str,
    workspace: WorkWorkspace | None = None,
    outputs: Sequence[OutputFact] = (),
) -> VerificationSummary | None:
    """The one line a Work card shows for verification, or None when there is none.

    ``verifications`` is newest first. The current Run's newest job wins over any earlier
    Run's, so a Run that has not verified yet is never covered by one that did. ``outputs``
    are the Work's pull requests: a repository is named as its open pull request spells it,
    and a green job whose commit is no longer that pull request's head says so (#2327).
    """
    if not verifications:
        return None
    ours = [check for check in verifications if current_run_id and check.run_id == current_run_id]
    check = ours[0] if ours else verifications[0]
    current = bool(ours)
    open_by_repository: dict[str, list[OutputFact]] = {}
    for output in outputs:
        if output.repository and not output.settled:
            open_by_repository.setdefault(output.repository.casefold(), []).append(output)
    tested_list: list[tuple[str, str]] = []
    superseded: list[tuple[str, str]] = []
    for name, snapshot in check.tested:
        pulls = open_by_repository.get((snapshot.repository or name).casefold(), [])
        shown = pulls[0].repository if pulls else name
        tested_list.append((shown, snapshot.head[:7] or "?"))
        # Moved past only when every open pull request there is surely on another commit.
        if pulls and all(_different_commit(pull.head, snapshot.head) for pull in pulls):
            superseded.append((shown, pulls[0].head[:7]))
    tested = tuple(tested_list)
    moved: list[tuple[str, str]] = []
    if workspace is not None:
        for name, snapshot in check.tested:
            repo = workspace.repository(snapshot.repository or name)
            if repo is None or not repo.heads or not snapshot.head:
                continue
            if snapshot.head not in repo.heads:
                moved.append((name, repo.heads[0][:7]))
    commits = ", ".join(f"{name}@{head}" for name, head in tested) or "no recorded commit"
    status = check.status
    if check.verified and not current:
        # No Run of the Work's own is current, or the current one has not verified yet:
        # either way the green job speaks only for the commit it tested.
        tail = "; the current Run has not" if current_run_id else ""
        status, label = "earlier", f"An earlier Run verified {commits}{tail}"
    elif check.verified and moved:
        now = ", ".join(f"{name}@{head}" for name, head in moved)
        status, label = "stale", f"Verified {commits}, but the checkout has moved to {now}"
    elif check.verified and superseded:
        heads = ", ".join(f"{name}@{head}" for name, head in superseded)
        label = f"Verified {commits}; the pull request's head is now {heads}"
    elif check.verified:
        label = f"Verified {commits}"
    elif status == "unproven":
        label = f"Tests passed on {commits}, but the tree could not be proven clean"
    elif status in {"queued", "running"}:
        label = f"Verification {status}"
    elif status == "failed" and superseded and current:
        # A red run on a commit the pull request has moved past says nothing about its head
        # now (#2460).
        heads = ", ".join(f"{name}@{head}" for name, head in superseded)
        label = f"Verification failed on {commits}; the pull request's head is now {heads}"
    elif status == "failed":
        label = f"Verification failed on {commits}"
    else:
        label = f"Verification {status}"
    if check.verified and check.forgiven:
        # Said on the Work itself, not only in the thread: a run that was green only after
        # forgiving failures is a different fact from a clean one (#2382).
        shown = ", ".join(test.rsplit("::", 1)[-1] for test in check.forgiven[:3])
        more = f" and {len(check.forgiven) - 3} more" if len(check.forgiven) > 3 else ""
        one = len(check.forgiven) == 1
        label += (
            f". Counted green after forgiving {len(check.forgiven)} failing "
            f"test{'' if one else 's'} that {'was' if one else 'were'} not this commit's doing "
            f"(flaky, or broken on master): {shown}{more}"
        )
    if not current and status not in {"earlier"}:
        label += " (earlier Run)"
    return VerificationSummary(
        status,
        label,
        check.job_id,
        check.run_id,
        tested,
        tuple(moved),
        superseded=(
            tuple(superseded)
            if current and ((check.verified and not moved) or status == "failed")
            else ()
        ),
    )


# --- workspace ------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceLine:
    """One repository the Work claims, in a few words."""

    repository: str
    branch: str
    state: str  # claimed | reserved | release pending | released | error
    problem: bool = False


def compact_workspace(
    claims: Sequence[WorkspaceClaim] | None,
) -> tuple[tuple[WorkspaceLine, ...], str]:
    """The Work's repositories from its claims (#2149), plus a note; no git is read.

    The dashboard shows this; the Work page shows the full, inspected workspace. The claims
    are the whole answer: a card's own worktree list is not read (#2195).
    """
    if claims is None:
        return (), "workspace claims could not be read"
    lines: dict[tuple[str, str], WorkspaceLine] = {}
    for claim in claims:
        if claim.last_error:
            state, problem = "error", True
        elif claim.released_at:
            state, problem = "released", False
        elif claim.release_pending:
            state, problem = "release pending", False
        elif not claim.confirmed:
            state, problem = "reserved", False
        else:
            state, problem = "claimed", False
        key = (claim.repository, claim.branch)
        # Several purposes in one repository read as one line; the worst state wins.
        existing = lines.get(key)
        if existing is None or (problem and not existing.problem) or existing.state == "released":
            lines[key] = WorkspaceLine(claim.repository, claim.branch, state, problem)
    ordered = sorted(lines.values(), key=lambda line: (line.state == "released", line.repository))
    return tuple(ordered), ""


# --- chats ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkChat:
    """A chat taking part in the Work; secondary, for reaching the conversation."""

    chat_id: str
    session_key: str  # the chat's current leg
    title: str
    href: str
    working: bool
    last_activity: datetime | None
    evidence: tuple[str, ...]
    other_works: int = 0  # how many other Works this chat also belongs to
    pull_requests: tuple[str, ...] = ()  # registered on the chat, not attributed to the Work
    legs: tuple[str, ...] = ()
    last_text: str = ""  # the chat's most recent response text, "" unknown (#2254)
    # Working only on a turn a background notification opened, not a message (#2760).
    background: bool = False


@dataclass(frozen=True)
class WorkJob:
    """A queued or running ``agentdeck job`` one of the Work's chats submitted (#2573)."""

    state: str  # ``queued`` or ``running``
    label: str  # the job's own label, else its command
    since: datetime | None  # when it was queued, or when it started running


# The queue's states for a job that has not finished yet.
_PENDING_JOBS = ("running", "queued")


def work_jobs(queue: Sequence[Mapping[str, Any]], chats: Sequence[WorkChat]) -> tuple[WorkJob, ...]:
    """The Work's chats' unfinished jobs, running first, then oldest (#2573).

    A job belongs to the chat whose leg submitted it. A verification job is left out: it
    already reads as Verifying (``summarize_verification``).
    """
    keys = {key for chat in chats for key in (chat.session_key, *chat.legs) if key}
    found: list[tuple[int, float, WorkJob]] = []
    for job in queue:
        state = str(job.get("state") or "")
        if state not in _PENDING_JOBS or str(job.get("session_key") or "") not in keys:
            continue
        if str(job.get("purpose") or ""):
            continue
        stamp = job.get("started_at") if state == "running" else job.get("submitted_at")
        try:
            since = datetime.fromtimestamp(float(stamp), UTC) if stamp else None
        except (TypeError, ValueError, OverflowError):
            since = None
        command = job.get("command") or ()
        label = str(job.get("label") or "") or (
            " ".join(str(part) for part in command) if isinstance(command, list) else ""
        )
        rank = (_PENDING_JOBS.index(state), since.timestamp() if since else 0.0)
        found.append((*rank, WorkJob(state, label or "a job", since)))
    return tuple(job for _, _, job in sorted(found, key=lambda item: item[:2]))


def _last_word(chats: Sequence[WorkChat]) -> str:
    """The most recently active chat's last response, "" when none said anything (#2254)."""
    floor = datetime.min.replace(tzinfo=UTC)
    said = [chat for chat in chats if chat.last_text]
    latest = max(said, key=lambda chat: chat.last_activity or floor, default=None)
    return latest.last_text if latest is not None else ""


def chat_legs(state: Any, chats: Any = None) -> dict[str, tuple[str, ...]]:
    """Every chat the deck holds, keyed by chat identity, with its legs oldest first."""
    legs: dict[str, tuple[str, ...]] = {}
    for key in state.sessions:
        chat_id = state.chat_identity_key(key, chats)
        if chat_id not in legs:
            legs[chat_id] = tuple(state.chat_session_keys(key, chats)) or (key,)
    return legs


def _chat_row(state: Any, chat_id: str, legs: Sequence[str], chats: Any) -> tuple[Any, ...]:
    """(current session, leg keys) of one chat, or (None, legs) when no leg is loaded."""
    sessions = state.sessions
    current_key = state.chat_current_session_key(legs[0], chats) if legs else chat_id
    session = sessions.get(current_key)
    if session is None:
        loaded = [sessions[key] for key in legs if key in sessions]
        session = max(loaded, key=_activity_of, default=None)
    return session, tuple(legs)


def _activity_of(session: Any) -> float:
    stamp = getattr(session, "last_progress", None) or getattr(session, "last_activity", None)
    return stamp.timestamp() if stamp else 0.0


def _when(session: Any) -> datetime | None:
    return getattr(session, "last_progress", None) or getattr(session, "last_activity", None)


def _latest(state: Any, legs: Sequence[str]) -> datetime | None:
    """When any leg of the chat last moved."""
    stamps = [_when(state.sessions[leg]) for leg in legs if leg in state.sessions]
    return max((stamp for stamp in stamps if stamp), default=None)


def _leg_working(session: Any, presentation: Any = None) -> bool:
    """Taking a turn, or running a subagent; not merely holding a background command.

    A chat whose turn ended with a command still running in the background stays
    ``thinking`` so it is not read as finished (#683), but for the Work that is not work:
    a dev server never reports back, and the poller already took the ended turn as the
    end of its Run (#2783). A helper agent the chat started still counts, even when the
    chat's own label names only the background command.
    """
    if not getattr(session, "thinking", False):
        return False
    if getattr(session, "activity", None) != BACKGROUND_TASK_ACTIVITY:
        return True
    return presentation is not None and presentation.has_working_subagent(session)


def _chat_working(state: Any, legs: Sequence[str], presentation: Any = None) -> bool:
    return any(_leg_working(state.sessions.get(key), presentation) for key in legs)


def _chat_background(state: Any, legs: Sequence[str], presentation: Any = None) -> bool:
    """Working, and only on turns a background notification opened (#2760)."""
    working = [
        session
        for key in legs
        if (session := state.sessions.get(key)) is not None
        and _leg_working(session, presentation)
    ]
    return bool(working) and all(getattr(s, "background_turn", False) for s in working)


# --- the card -------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkCard:
    work_id: str
    href: str
    source_label: str  # owner/repo#number
    source_url: str
    title: str
    work_state: str  # the board column, or the issue's own state
    run_id: str
    run_state: str  # a Run-trace display state, "preflight", or "" for no Run
    run_label: str
    run_reason: str
    next_expected: str
    stuck: bool
    attention: tuple[WorkAttention, ...]
    workspace: tuple[WorkspaceLine, ...]
    workspace_note: str
    verification: VerificationSummary | None
    review: ReviewSummary | None
    outputs: tuple[OutputFact, ...]  # the Work's own facts' (``work_facts``), never a page's
    chat_count: int
    working_count: int
    last_activity: datetime | None
    unreadable: tuple[str, ...] = ()
    # When the Work finished (#2219): its issue's closing, never chat activity.
    finished_at: datetime | None = None
    # The column role when the card is parked for a person (``blocked`` or
    # ``needs_more_info``) and no review or dependency explains it (#2219); else "".
    parked: str = ""
    # The engine's spawn never landed, so no worker ever ran (#2226): whatever column the
    # card rests in, a person has to post a new instruction.
    dispatch_failed: bool = False
    # ...and the park's one free retry is still due, so the engine starts it again by itself
    # (``dispatch.rearmable``); only a park without it waits for a person (#2235).
    dispatch_rearms: bool = False
    # The host's name for the refusal a failed start or the current Run rests on
    # (``at_capacity`` ...), or "": a refusal for room is not a bug (#2888).
    run_refusal: str = ""
    # How many times the engine already restarted this card's worker (``BoardCard.revives``).
    revives: int = 0
    # Why the engine parked the release, while that park is current (#2235).
    release_parked: str = ""
    # ``human_review_pending`` while a Needs More Info park stands in for that column.
    parked_as: str = ""
    # A Poller card not yet done tracks this Work and releases itself (``auto_release``), so a
    # follow-up there would review and merge rather than only review (#2234).
    self_releasing: bool = False
    # The card's project asks for a missing verification itself (``BoardCard.verifies_itself``,
    # #2364): a clean review with no green run waits on the deck rather than on a person.
    verifies_itself: bool = False
    # The open outputs whose CI is their verification, at their heads (#2364).
    ci: tuple[CiCheck, ...] = ()
    # ``label@head`` of each open pull request no green run covers, on a Work shipping several
    # (#2718): the release waits on these.
    untested: tuple[str, ...] = ()
    # A card waiting on a blocker has no review summary, yet its review is still known (#2367):
    # ``passed_heads`` are the heads the engine's review bit was last read green for, which it
    # judged against the content that would merge; ``kept_pass_heads`` are the commits its
    # kept pass was recorded at, which says nothing about whether the base has moved since.
    passed_heads: tuple[str, ...] = ()
    kept_pass_heads: tuple[str, ...] = ()
    # The role of the column that card rests in (``BoardCard.status_role``), "" without one:
    # what the Resolve action posts depends on it (#2237).
    resting: str = ""
    # What a hide is compared against (#2238, ``work_hidden``): the source issue's comment
    # count and the Poller card's fingerprint, each None when not known.
    comments: int | None = None
    card_mark: str | None = None
    # What the Work's most recently active chat last asked, in one line (``work_wording``),
    # from the deck's own copy of its transcript: a parked Work's question (#2254).
    question: str = ""
    # Which project the Work belongs to (#2311, ``work_project``): the front page's filter.
    project: str = ""
    # The engine merged the Work and is watching for its deploy (``awaiting_deploy``) with no
    # release block: whatever the review said, what happens next is the deploy (#2332).
    deploying: bool = False
    # Why the worker said a card resting in Blocked is blocked, from its terminal comment
    # (``BoardCard.park_note``), "" when it gave no "Blocked…" line (#2332).
    park_reason: str = ""
    # The engine re-reads a deploy that failed, and may yet find it green (#2332).
    deploy_recheck: bool = False
    # The issue closed and no person has finished the Work yet (``work_facts.awaiting_finish``,
    # #2510): the front page keeps it in Needs you rather than Recently finished.
    awaiting_finish: bool = False
    # Its issue closed on a merge whose deploy the engine has not started watching yet
    # (``work_facts.deploy_due``, #2760): it reads as Deploying, not as a Finish to give.
    deploy_due: bool = False
    # A finished ChatGPT review chat on the chat board covered the head of every pull request
    # that is, or was merged as, the Work's result (``chat_reviewed``, #2411): the review the
    # chat's own ChatGPT chip shows, which the instance's reviewer may never have recorded.
    chatgpt_reviewed: bool = False
    # The chats mid-turn on this Work, as (title, link), in ``chats`` order: what holds a
    # merge back while they work (#2481).
    working_chats: tuple[tuple[str, str], ...] = ()
    # Every chat mid-turn on this Work is on a turn only a background notification opened — a
    # task it started finishing, a queued job reporting back — with no message from anyone
    # (#2760). Such a turn does not take the Work out of Needs you.
    woke_itself: bool = False
    # The chat this Work opens into (``work_story.opening_chat``), "" without one: a phone's
    # front-page row goes straight to it, and Back returns to the front page (#2512).
    chat_href: str = ""
    chat_key: str = ""  # that chat's session key: a phone push skips a muted one (#3348)
    # A person paused the Work (#2548): its issue carries its instance's ``paused_label``,
    # which is the label Pause adds and Resume removes; "" when that instance names none.
    paused: bool = False
    pause_label: str = ""
    # ``paused`` is either of: the label (``label_paused``), or paused on this deck instead
    # (#2559): a Work no live card with a paused label tracks, so the
    # deck is all that could start anything on it, and it sends nothing to its chats.
    label_paused: bool = False
    deck_paused: bool = False
    # A person starred the Work (#3116): first in its front-page section, with a star mark.
    starred: bool = False
    # No agent works on it (#3558): an open issue without its repository's ownership label.
    # The label the Start agent action adds; "" for every Work an agent has or had.
    start_label: str = ""
    # The branch staging copy already running every open pull request's head
    # (``WorkFacts.staging``, #3180): the change can be tried there while review and
    # verification still run. None otherwise.
    staging: Any = None
    # The open issues the Poller card waits on before it starts or resumes
    # (``BoardCard.blocked_by``, #2573); the engine moves it on by itself when they close.
    blocked_by: tuple[str, ...] = ()
    # Its chats' queued or running jobs (``work_jobs``, #2573).
    jobs: tuple[WorkJob, ...] = ()
    # The issue is a schedule definition (``scheduled`` label, ``poller_engine.schedules``):
    # it stays open on purpose and never has a next step of its own (#2998).
    scheduled: bool = False
    # The issue carries the ``parked`` label (``work_derived.PARKED_LABEL``): a person asked
    # its agent to leave it until they write (#3560). Not ``parked``, the poller's column.
    parked_label: bool = False
    # The labels on its cached issue, as GitHub spells them: a deck-run Work reads its
    # project's spec-only label from them (#3247).
    labels: tuple[str, ...] = ()
    # The one status line of a Work the deck runs itself (``work_derived``, #2895 step 3): a
    # ``works_router`` project's Work no Poller card holds. ``None`` keeps today's status.
    derived: Any = None

    @property
    def diffstat(self) -> DiffStat | None:
        """The size of the Work's pull requests, or None while any is unknown (#2236)."""
        return diffstat(self.outputs)

    @property
    def targets(self) -> tuple[str, ...]:
        """The branches its pull requests merge into, only when one is not ``master``."""
        return target_branches(self.outputs)

    @property
    def needs_attention(self) -> bool:
        return (
            bool(self.attention)
            or self.stuck
            or bool(self.review and self.review.needs_human)
            or bool(self.parked)
        )

    @property
    def revision(self) -> str:
        """Changes exactly when the card's facts do, so an unchanged card is never swapped."""
        return hashlib.sha1(repr(self).encode(), usedforsecurity=False).hexdigest()[:12]


def _run_label(state: str) -> str:
    return state.replace("_", " ")


# --- review ---------------------------------------------------------------------------------

# Where review has got to (#2216). Derived from the Poller card's own release facts on every
# read; it is a way of reading the engine's lifecycle, not a second one.
REVIEW_PENDING = "pending"  # expected or queued, not yet running
REVIEW_RUNNING = "running"
REVIEW_PASSED = "passed"
REVIEW_CHANGES = "changes"  # asked for changes; the deck starts the next round itself
REVIEW_FAILED = "failed"  # the review's own Run failed or cannot be confirmed
REVIEW_BLOCKED = "blocked"  # the release is parked for a person
REVIEW_HUMAN = "needs_human"  # nothing automatic will move it: a person reviews or decides

# Run states of the reviewer's own Run that mean it is not getting anywhere by itself.
_REVIEW_RUN_FAILED = frozenset({NEEDS_ATTENTION, FAILED, UNCERTAIN})
# Columns a review can leave a card resting in (``BoardCard.status_role``).
_REVIEW_RESTING = frozenset({"in_review", "ready_to_merge", "human_review_pending"})
NEEDS_MORE_INFO = "needs_more_info"
_PARKED = frozenset({"blocked", NEEDS_MORE_INFO})


@dataclass(frozen=True)
class ReviewSummary:
    """The Work's review, in one line, with what the deck expects next.

    ``implemented`` says the work under review exists: false only while the turn that writes
    it is still going. ``needs_human`` is the only part that is attention; a review queued,
    running, passed or asking for another automatic round is progress. ``run_id`` is the
    reviewer's Run when the review is one, and ``pull_request`` the output it is about.
    ``decision`` says what a person is asked for when ``needs_human`` is: ``review`` (a
    review nobody automatic will give), ``release`` (a release that stopped or is blocked) or
    ``clarification`` (the card is parked in Needs More Info) (#2224).
    """

    state: str
    label: str
    next_expected: str
    needs_human: bool = False
    implemented: bool = True
    run_id: str = ""
    pull_request: str = ""
    heads: tuple[str, ...] = ()
    decision: str = ""
    # Why a person is needed, for a reader that must not parse ``label`` (#2236): one of the
    # ``CAUSE_*`` below while ``needs_human`` is, else "". ``blocker`` is the engine's own
    # reason for stopping the release, while that stop is current.
    cause: str = ""
    blocker: str = ""


# Why a review needs a person (``ReviewSummary.cause``).
CAUSE_RELEASE_BLOCKED = "release_blocked"  # the engine is about to park the release, and why
CAUSE_RELEASE_PARKED = "release_parked"  # the card rests in Blocked or Needs More Info
CAUSE_MERGED_OPEN = "merged_open"  # every output settled, one merged, the issue still open
CAUSE_CLOSED_UNMERGED = "closed_unmerged"  # every output closed without merging
CAUSE_NOT_LANDED = "release_not_landed"  # in Ready to merge with a passed review
CAUSE_HUMAN_REVIEW = "human_review"  # a person's review is what is left
CAUSE_REVIEWER = "reviewer"  # the instance's reviewer will not act on it
CAUSE_NO_OUTPUT = "no_output"  # in review, but no pull request is known
# Every open output is reviewed clean at its head: what is left is a person's merge (#2327).
CAUSE_REVIEWED_CLEAN = "reviewed_clean"
# ...except that GitHub says one conflicts with its base, so nothing can merge it (#2332).
CAUSE_CONFLICT = "conflict"
# The reviewer sent an open output's current head back, and no fix round has started (#2332).
CAUSE_CHANGES_ASKED = "changes_asked"

# What a person is asked for when a review needs one (``ReviewSummary.decision``).
DECIDE_REVIEW = "review"
DECIDE_RELEASE = "release"
DECIDE_CLARIFICATION = "clarification"
DECIDE_MERGE = "merge"
DECIDE_FIX = "fix"  # the reviewer sent the head back and no fix round started (#2332)
DECIDE_REBASE = "rebase"  # GitHub says the head conflicts with its base (#2332)

# How long a head the reviewer sent back may wait for its automatic fix round before a person
# is asked to start one: the same grace the reviewer gives a parked head (``chatgpt_nudge_core``).
CHANGES_GRACE = timedelta(minutes=15)

# The reviewer's own words for a branch GitHub would not bring up to date with its base: a
# conflict, which a plain ``/merge`` cannot get past either (``chatgpt_nudge_core``).
_CONFLICT = "would not merge the base in"
# The reviewer's note for a branch it brought up to date and nobody merged.
_BROUGHT_FORWARD = re.compile(r"^brought forward (\d+) times? and still not merged\b")


def conflicts(output: OutputFact) -> bool:
    """Nothing can merge this output as it is: GitHub says its head conflicts with its base,
    or the reviewer found it would not merge the base in (#2332)."""
    return output.conflicting or (output.review == "stalled" and _CONFLICT in output.review_detail)


def reviewed_clean(output: OutputFact) -> bool:
    """The reviewer recorded a clean review of this open output's current head (#2254)."""
    return bool(output.head) and output.review_clean and not output.draft


def clean_at_head(output: OutputFact) -> bool:
    """Reviewed clean at its current head, and nothing stops a merge of it (#2254, #2332)."""
    return reviewed_clean(output) and not conflicts(output)


def _merge_wait(output: OutputFact) -> str:
    """Why a clean output is still open, from the reviewer's stall note, in plain words."""
    if output.review != "stalled" or not output.review_detail:
        return ""
    detail = output.review_detail
    rounds = _BROUGHT_FORWARD.match(detail)
    if rounds:
        return (
            f"not merged after {rounds[1]} update rounds, "
            "and AgentDeck no longer updates the branch"
        )
    if detail.startswith("reviewed, up to date with its base, and still not merged"):
        return "up to date with its base and still not merged"
    return detail



def summarize_review(
    card: Any,
    run_id: str = "",
    run_state: str = "",
    *,
    outputs: Sequence[OutputFact] | None = None,
    now: datetime | None = None,
    verifying: bool = False,
) -> ReviewSummary | None:
    """Where the Work's review is, from the Poller card; None when no review is in play.

    ``run_id``/``run_state`` are the current Run's, from the Run trace: while the card is armed
    for review its current Run is the reviewer's, so that Run says queued, running or stuck.
    A done card has nothing left to review, and a card the engine has not handed to review
    says nothing here — the Run line already says what it is doing.

    ``outputs`` are the Work's outputs from its facts (``work_facts``), each with its cached
    state and its reviewer's verdict for its current head; by default, the card's own pull
    requests with nothing known about them. A card resting in review is judged by every one
    (#2226): one merged or closed says so, and a review is "queued" only when the instance's
    reviewer will really ask about an open one. ``now`` is the clock a head sent back for
    changes is measured against (``CHANGES_GRACE``). ``verifying`` says the current Run's own
    verification is queued or running: a Needs More Info park is then read as In progress
    (``stale_park``, #2777).
    """
    if card is None or getattr(card, "done", False):
        return None
    mode = str(getattr(card, "review_mode", "") or "")
    role = str(getattr(card, "status_role", "") or "") or (
        "in_review" if getattr(card, "in_review", False) else ""
    )
    if (verifying or prompt_park(card)) and role == NEEDS_MORE_INFO:
        role = "in_progress"
    passed = tuple(getattr(card, "review_passed_heads", ()) or ())
    pull = str(getattr(card, "pull_request_url", "") or "")
    blocked = str(getattr(card, "release_blocked", "") or "")

    def summary(state: str, label: str, next_expected: str, **fields: Any) -> ReviewSummary:
        fields.setdefault("pull_request", pull)
        fields.setdefault("heads", passed)
        return ReviewSummary(state, label, next_expected, **fields)

    # A merge follows a review only when one of these says so; a plain ``/merge`` has none.
    reviewed = bool(
        passed
        or getattr(card, "auto_release", False)
        or getattr(card, "review_approved", False)
    )
    status = str(getattr(card, "status", "") or "")

    # Issues the card names as what it waits on: the engine picks it up again when they
    # close, so a review or release bit it kept is nobody's decision meanwhile (#2235).
    waits = bool(getattr(card, "blocked_by", ()))

    def human(
        state: str, label: str, decision: str = DECIDE_REVIEW, **fields: Any
    ) -> ReviewSummary | None:
        if waits:
            return None
        return summary(state, label, NEXT_HUMAN, needs_human=True, decision=decision, **fields)

    if blocked:
        return human(
            REVIEW_BLOCKED, f"Release blocked: {blocked}", DECIDE_RELEASE,
            cause=CAUSE_RELEASE_BLOCKED, blocker=blocked,
        )
    if role in _PARKED and reviewed:
        # The engine parks a failed release and keeps its pass (``_block_release``): the
        # column, not the old pass, says what happens next.
        label = f"Automatic review or release stopped: the card is {status}"
        parked = DECIDE_CLARIFICATION if role == NEEDS_MORE_INFO else DECIDE_RELEASE
        return human(
            REVIEW_BLOCKED,
            label,
            parked,
            cause=CAUSE_RELEASE_PARKED,
            blocker=str(getattr(card, "release_parked", "") or ""),
        )
    parked_why = str(getattr(card, "release_parked", "") or "")
    if role == "ready_to_merge" and parked_why:
        # The engine parked the release where a failed one comes to rest, and said why (#2332).
        return human(
            REVIEW_BLOCKED, f"Release blocked: {parked_why}", DECIDE_RELEASE,
            cause=CAUSE_RELEASE_PARKED, blocker=parked_why,
        )
    if mode == "merge":
        if not reviewed:
            return None
        at = f" at {passed[0][:7]}" if passed else ""
        # The card stays in merge mode until it closes, after the pull request merged (#2316).
        landed = outputs if outputs is not None else work_facts(None, card=card).outputs
        merged = any(output.state == "merged" for output in landed) and all(
            output.settled for output in landed
        )
        label = f"Review passed{at}; " + ("merged" if merged else "merging")
        return summary(REVIEW_PASSED, label, "merge", run_id=run_id)
    if mode == "merge-review":
        if run_state in _REVIEW_RUN_FAILED:
            label = f"Review {_run_label(run_state)}"
            return summary(REVIEW_FAILED, label, NEXT_RETRY, run_id=run_id)
        if run_state == RUNNING:
            return summary(REVIEW_RUNNING, "Review running", NEXT_REVIEW, run_id=run_id)
        if run_state == FINISHED:
            label = "Review ended; the Poller applies its verdict next"
            return summary(REVIEW_RUNNING, label, "verdict to apply", run_id=run_id)
        return summary(REVIEW_PENDING, "Review queued", "reviewer to start", run_id=run_id)
    if role in _PARKED and _continuing_after_step(
        card, outputs if outputs is not None else work_facts(None, card=card).outputs
    ) is None:
        # A card parked for a person whose pull requests all merged or closed is not waiting
        # on an answer any more: what is left is closing the issue (#2332). Not when those were
        # steps the card carried on past: then the park is about the next part (#2730).
        settled = _all_settled(
            outputs if outputs is not None else work_facts(None, card=card).outputs, human
        )
        if settled is not None:
            return settled
    offer = str(getattr(card, "review_offer", "") or "")
    if offer and role not in _REVIEW_RESTING:
        # The live worker offered its head and is verifying it while the review runs (#2350).
        if offer == "rejected":
            label = "Review asked for changes; the worker is fixing them"
            return summary(REVIEW_CHANGES, label, "the worker's fix round")
        return summary(
            REVIEW_RUNNING, "Reviewing while the worker verifies", "the reviewer's verdict"
        )
    if role not in _REVIEW_RESTING:
        if passed:
            return summary(REVIEW_PASSED, f"Review passed at {passed[0][:7]}", "release")
        return None
    if outputs is None:
        outputs = work_facts(None, card=card).outputs
    open_outputs = [output for output in outputs if not output.settled]
    continuing = _continuing_after_step(card, outputs)
    if continuing is not None:
        return summary(*continuing)
    settled = _all_settled(outputs, human)
    if settled is not None:
        # Nothing is left to review: the pull requests were merged or closed, yet the card is
        # still open (#2226).
        return settled
    if role == "ready_to_merge" and passed:
        # Where a failed release comes to rest; a pass kept from before the failure does not
        # make it clean, but a later pull request conflict is the reason when there is one.
        conflicted = [output for output in open_outputs if conflicts(output)]
        if conflicted:
            return human(
                REVIEW_HUMAN, _conflict_label(conflicted, passed=True), DECIDE_REBASE,
                pull_request=conflicted[0].url, cause=CAUSE_CONFLICT,
            )
        return human(
            REVIEW_HUMAN,
            f"Review passed at {passed[0][:7]}, but the release did not land; waiting for a person",
            DECIDE_RELEASE,
            cause=CAUSE_NOT_LANDED,
        )
    # From here a card resting in Ready to merge with no pass is read like one in review: the
    # reviewer's verdict at each head says what it waits on, never a guess (#2332).
    if passed and not waits and not getattr(card, "releases_itself", True):
        # Nothing on this project merges a pass (#2399): every open head the pass covers is a
        # person's ``/merge``, which ``_verifying`` holds back until a green run covers it.
        covered = bool(open_outputs) and all(
            any(_same_commit(sha, output.head) for sha in passed) for output in open_outputs
        )
        if covered:
            conflicted = [output for output in open_outputs if conflicts(output)]
            if conflicted:
                return human(
                    REVIEW_HUMAN, _conflict_label(conflicted, passed=True), DECIDE_REBASE,
                    pull_request=conflicted[0].url, cause=CAUSE_CONFLICT,
                )
            return human(
                REVIEW_PASSED,
                f"Review passed at {passed[0][:7]}; this project merges only by hand",
                DECIDE_MERGE,
                pull_request=open_outputs[0].url,
                cause=CAUSE_REVIEWED_CLEAN,
            )
    if passed:
        return summary(REVIEW_PASSED, f"Review passed at {passed[0][:7]}", "release")
    if role == "human_review_pending":
        return human(REVIEW_HUMAN, "Waiting for a human review", cause=CAUSE_HUMAN_REVIEW)
    # The reviewer's verdict for each open output's current head, read once into the facts.
    verdicts = [
        (output.url, output.review, output.review_detail)
        for output in open_outputs
        if output.review
    ]
    held = next((output for output in open_outputs if output.review == "held"), None)
    if held is not None:
        # The reviewer still awaits its chat, but the chat waits on a person (#2442): a chat
        # held elsewhere outranks one still running, since only a person moves it.
        label = _reviewer_label(held.url, held.review, held.review_detail, held.review_held)
        return human(REVIEW_HUMAN, label, pull_request=held.url, cause=CAUSE_REVIEWER)
    running = next((item for item in verdicts if item[1] == "awaiting"), None)
    if running is not None:
        chat = running[2]
        label = f"External review running in {chat}" if chat else "External review running"
        return summary(REVIEW_RUNNING, label, "review verdict", pull_request=running[0])
    queued = next((item for item in verdicts if item[1] == "configured"), None)
    if queued is not None:
        # The reviewer watches this repository, and nothing it recorded holds this head
        # back: it asks for a review of every ready head, and none is open yet.
        return summary(
            REVIEW_PENDING, "External review queued", "reviewer to start",
            pull_request=queued[0],
        )
    sent_back = [output for output in open_outputs if output.review_changes_at is not None]
    if sent_back:
        return _changes_asked(sent_back, open_outputs, run_state, now, human, summary)
    if verdicts and all(reviewed_clean(output) for output in open_outputs) and any(
        conflicts(output) for output in open_outputs
    ):
        conflicted = [output for output in open_outputs if conflicts(output)]
        return human(
            REVIEW_HUMAN, _conflict_label(conflicted), DECIDE_REBASE,
            pull_request=conflicted[0].url, cause=CAUSE_CONFLICT,
        )
    if verdicts and all(clean_at_head(output) for output in open_outputs):
        # A clean review of every open head is a passed review: a stall note on top says why
        # nobody merged it, which is a person's merge, not a review decision (#2327).
        if len(open_outputs) == 1:
            only = open_outputs[0]
            wait = _merge_wait(only)
            label = f"Reviewed clean at {only.head[:7]}" + (f"; {wait}" if wait else "")
        else:
            label = "Every pull request is reviewed clean at its current head"
        return human(
            REVIEW_PASSED,
            label,
            DECIDE_MERGE,
            pull_request=open_outputs[0].url,
            cause=CAUSE_REVIEWED_CLEAN,
        )
    sent = tuple(getattr(card, "verdicts_on_the_way", ()) or ())
    answered = [
        output for output in open_outputs if any(_same_commit(sha, output.head) for sha in sent)
    ]
    if answered and all(
        output in answered or clean_at_head(output) for output in open_outputs if output.review
    ):
        # The reviewer sent its verdict on these heads and the engine records it on its next
        # wake (#2760): until then it waits on the poller, not on a person.
        first = answered[0]
        return summary(
            REVIEW_PENDING,
            f"Reviewed at {first.head[:7]}; the poller records the verdict next",
            "review verdict",
            pull_request=first.url,
        )
    if verdicts:
        labels = list(dict.fromkeys(_reviewer_label(*item) for item in verdicts))
        # The decision is made on the pull request the reviewer judged, found or named.
        return human(
            REVIEW_HUMAN, "; ".join(labels), pull_request=verdicts[0][0], cause=CAUSE_REVIEWER
        )
    if role == "ready_to_merge":
        return human(
            REVIEW_HUMAN,
            "In Ready to merge, but nothing reviewed the current head",
            cause=CAUSE_HUMAN_REVIEW if outputs else CAUSE_NO_OUTPUT,
        )
    return human(
        REVIEW_HUMAN,
        "In review; waiting for a person",
        cause=CAUSE_HUMAN_REVIEW if outputs else CAUSE_NO_OUTPUT,
    )


@dataclass(frozen=True)
class CiCheck:
    """One open pull request whose CI is its verification, at its current head (#2364).

    ``state`` is the engine's reading for exactly ``head``: ``green``, ``pending``,
    ``failing``, ``none`` (no check reported), or ``unread`` when the engine has not read this
    head yet — a reading of an earlier head is about a commit that would not merge.
    """

    label: str
    repository: str
    number: int
    head: str
    state: str


def ci_checks(card: Any, outputs: Sequence[OutputFact]) -> tuple[CiCheck, ...]:
    """Every open output in a ``github_checks`` repository, with its CI at its head."""
    if card is None:
        return ()
    wanted = {name.casefold() for name in getattr(card, "ci_repos", ()) or ()}
    if not wanted:
        return ()
    readings = tuple(getattr(card, "ci_checks", ()) or ())
    linked = {url.casefold() for url, _ in getattr(card, "release_reading", ()) or ()}
    found = []
    for output in outputs:
        if output.settled or output.repository.casefold() not in wanted:
            continue
        state = next(
            (
                reading_state
                for repository, number, head, reading_state in readings
                if repository.casefold() == output.repository.casefold()
                and number == output.number
                and _same_commit(head, output.head)
            ),
            "unread",
        )
        if state == "unread" and linked and output.url.casefold() not in linked:
            # The engine reads CI only on the pull requests it links to the card, and its last
            # reading did not name this one, so it will never read it here (#2405).
            state = CI_UNTRACKED
        found.append(CiCheck(output.label, output.repository, output.number, output.head, state))
    return tuple(found)


# A pull request in a CI repository the engine does not link to the card, so no CI reading of
# it will ever come from the engine (#2405). It is left out of ``ci_state``: waiting on it would
# wait forever, and it is never green, so it never covers a head either.
CI_UNTRACKED = "untracked"


def ci_state(checks: Sequence[CiCheck]) -> str:
    """The one word for a Work's CI: failing, pending, waiting, green, or "" with no CI."""
    states = {check.state for check in checks if check.state != CI_UNTRACKED}
    if not states:
        return ""
    if "failing" in states:
        return "failing"
    if "pending" in states:
        return "pending"
    return "green" if states == {"green"} else "waiting"


def _same_commit(one: str, other: str) -> bool:
    """Two heads, either possibly shortened, surely name the same commit."""
    one, other = one.strip().casefold(), other.strip().casefold()
    return len(one) >= 7 and len(other) >= 7 and (one.startswith(other) or other.startswith(one))


def verification_covers(
    verification: VerificationSummary | None,
    outputs: Sequence[OutputFact],
    ci: Sequence[CiCheck] = (),
) -> bool:
    """Green verification covers every open pull request's current head (#2364).

    A pull request in a ``github_checks`` repository is covered only by green CI on its exact
    head. Any other is covered by a green verification job that tested that commit: the engine
    credits a job by the commit it tested, whichever Run submitted it, so an earlier Run's green
    job covers a head it tested and a checkout that moved on without a push changes nothing
    that would merge. What never covers a head is a job or CI on another commit, one still
    running, a red one, or none at all. A pull request in a ``review_only`` repository has no
    tests, so a clean review of its exact head covers it too (#2729, #2832).
    """
    opened = [output for output in outputs if not output.settled]
    if not opened:
        return False
    by_ci = {(check.repository.casefold(), check.number): check.state for check in ci}
    green = verification is not None and verification.status in {"verified", "stale", "earlier"}
    tested = [(name.casefold(), head) for name, head in verification.tested] if green else []
    return all(
        (output.review_only and reviewed_clean(output))
        or (
            by_ci[(output.repository.casefold(), output.number)] == "green"
            if (output.repository.casefold(), output.number) in by_ci
            else any(
                name == output.repository.casefold() and _same_commit(head, output.head)
                for name, head in tested
            )
        )
        for output in opened
    )


# The CI readings that answer for a head: anything else is no reading at all.
_CI_READ = frozenset({"green", "pending", "failing"})


def failed_verification_holds(
    verification: VerificationSummary | None,
    run_id: str,
    ci: Sequence[CiCheck] = (),
) -> bool:
    """A red verification job still decides the Work: the one test for it (#2352, #2460).

    Only the current Run's own failed job, and only while it speaks for what would merge. It
    does not once an open pull request it tested has moved to another commit: a red run on an
    old head says nothing about the new one. Nor when every repository it tested verifies by
    CI and CI has read the pull request's current head there: the engine credits only CI in a
    ``github_checks`` repository (#2364), so a job a worker ran itself is not its release gate.
    """
    if verification is None or verification.status != "failed":
        return False
    if not run_id or verification.run_id != run_id or verification.superseded:
        return False
    by_repository: dict[str, list[str]] = {}
    for check in ci:
        by_repository.setdefault(check.repository.casefold(), []).append(check.state)
    read = {name for name, states in by_repository.items() if set(states) <= _CI_READ}
    return not verification.tested or any(
        name.casefold() not in read for name, _ in verification.tested
    )


def _deck_verifies(card: Any, outputs: Sequence[OutputFact], run_state: str) -> bool:
    """The engine will still ask for this Work's missing verification itself (#2364).

    Only on a project that asks at all, and not once it has asked for exactly these heads and
    that turn is over: it never asks twice for one head, so a turn that ended with no green run
    is a person's to follow up. Nor while its own review bit reads red at these heads, however
    clean the reviewer's verdicts look from here (#2405).
    """
    if card is None or not getattr(card, "verifies_itself", False):
        return False
    if run_state == RUNNING:
        return True
    opened = [output for output in outputs if not output.settled]
    if _review_bit_red(card, opened):
        return False
    asked = tuple(getattr(card, "verify_asked_heads", ()) or ())
    answered = bool(opened) and all(
        any(_same_commit(sha, output.head) for sha in asked) for output in opened
    )
    return not answered


def untested_outputs(card: Any, outputs: Sequence[OutputFact]) -> tuple[str, ...]:
    """``label@head`` of each open pull request the engine found no green run on (#2718).

    Only on a Work with more than one open pull request, where this is the question nobody
    could see the answer to: a verification job credits only the head of the repository it ran
    in, so the card's own head can be green while a companion's holds the release. A reading
    taken at a head the pull request has since moved off says nothing about it.
    """
    opened = [output for output in outputs if not output.settled]
    reading = getattr(card, "untested_heads", ()) or ()
    if len(opened) < 2 or not reading:
        return ()
    heads = {url.casefold(): head for url, head in reading}
    return tuple(
        f"{output.label}@{output.head[:7]}"
        for output in opened
        if output.url.casefold() in heads
        and _same_commit(heads[output.url.casefold()], output.head)
    )


def _review_bit_red(card: Any, opened: Sequence[OutputFact]) -> bool:
    """The engine's last reading found its review bit red, at the heads that are open now (#2405).

    The engine asks for a verify turn only on a green review bit, and that bit is the card's
    own: an outside reviewer's clean verdict counts only on the card whose issue it was posted
    on. A pull request two cards share can therefore read clean here and still leave the other
    card's bit red forever. A reading that named a head since moved says nothing any more, and
    neither does one that names none of the pull requests open now: it may be about a pull
    request since merged or closed, taken before the engine read the one open now.
    """
    reading = getattr(card, "release_reading", ()) or ()
    if not getattr(card, "review_bit_red", False) or not reading:
        return False
    heads = {url.casefold(): head for url, head in reading}
    named = [output for output in opened if output.url.casefold() in heads]
    return bool(named) and all(
        _same_commit(heads[output.url.casefold()], output.head) for output in named
    )


def _verifying(
    review: ReviewSummary | None,
    verification: VerificationSummary | None,
    run_id: str,
    *,
    outputs: Sequence[OutputFact] = (),
    ci: Sequence[CiCheck] = (),
    verifies_itself: bool = False,
) -> ReviewSummary | None:
    """A clean review is a person's merge only once a green run covers its head (#2352, #2364).

    The final suite runs on the head the review passed (#2253), so until it finishes the
    Work is waiting on that suite, not on anyone, and a failed one is the Verify step's to
    report, never a merge to ask for. A head no green run covers is not a merge either: on a
    project whose engine asks for the missing run itself it waits on that, and elsewhere the
    Verify step says it never ran.
    """
    status = verification.status if verification is not None else ""
    if review is None or review.cause != CAUSE_REVIEWED_CLEAN:
        return review
    checks = ci_state(ci)
    if status in {"running", "queued"} or checks == "pending":
        waits = NEXT_VERIFICATION
    elif failed_verification_holds(verification, run_id, ci) or checks == "failing":
        waits = ""
    elif verification_covers(verification, outputs, ci):
        return review
    elif checks == "waiting":
        # CI the engine re-reads on every live wake: nobody else has to ask for it.
        waits = NEXT_VERIFICATION
    else:
        waits = NEXT_VERIFICATION if verifies_itself else ""
    return replace(review, next_expected=waits, needs_human=False, decision="", cause="")


def _continuing_after_step(card: Any, outputs: Sequence[OutputFact]) -> tuple | None:
    """A card that merged a step and is handing back to its worker is not waiting on anyone.

    Its outputs are all merged and its issue is open, which is exactly what ``_all_settled``
    asks a person to close — but the engine has already queued the worker's next turn (#2730),
    and a worker cap can hold that turn for a while. Only a card whose every merged output is
    a step it continued past reads this way; one merged completion keeps the old question.
    """
    steps = {url.lower() for url in getattr(card, "steps_merged", ()) or ()}
    if not steps or not outputs or getattr(card, "dispatch_failed", False):
        return None
    if any(not output.settled for output in outputs):
        return None
    merged = [output for output in outputs if output.state == "merged"]
    if not merged or any(output.url.lower() not in steps for output in merged):
        return None
    return (
        REVIEW_PENDING,
        "Step merged; the worker carries on with the next part",
        "the worker's next turn",
    )


def _all_settled(outputs: Sequence[OutputFact], human) -> ReviewSummary | None:
    """Every output merged or closed while the card is open: say which, else None (#2226)."""
    if not outputs or any(not output.settled for output in outputs):
        return None
    merged = [output.label for output in outputs if output.state == "merged"]
    if merged:
        return human(
            REVIEW_HUMAN,
            f"Merged {', '.join(merged)}; the issue is still open",
            DECIDE_RELEASE,
            cause=CAUSE_MERGED_OPEN,
        )
    label = (
        "Its pull request was closed without merging"
        if len(outputs) == 1
        else "Its pull requests were closed without merging"
    )
    return human(REVIEW_HUMAN, label, cause=CAUSE_CLOSED_UNMERGED)


def _conflict_label(conflicted: Sequence[OutputFact], *, passed: bool = False) -> str:
    """``Reviewed clean at abc1234, but it conflicts with master``, naming each pull request
    when there are several (#2332)."""
    first = conflicted[0]
    base = first.base or "its base branch"
    if len(conflicted) == 1:
        how = "Review passed" if passed else "Reviewed clean"
        return f"{how} at {first.head[:7]}, but it conflicts with {base}"
    names = ", ".join(output.label for output in conflicted)
    return f"{names} conflict with their base branches"


def _changes_asked(
    sent_back: Sequence[OutputFact],
    open_outputs: Sequence[OutputFact],
    run_state: str,
    now: datetime | None,
    human,
    summary,
) -> ReviewSummary | None:
    """The reviewer sent a head back (#2332): the automatic fix round's to answer for a grace
    period or while a Run works, then a person's to start."""
    first = sent_back[0]
    label = f"Reviewer asked for changes at {first.head[:7]}"
    if len(open_outputs) > 1:
        label += f" on {first.label}"
        others = [output.label for output in open_outputs if clean_at_head(output)]
        if others:
            label += f"; {', '.join(others)} reviewed clean"
    moment = now or datetime.now(UTC)
    newest = max(output.review_changes_at for output in sent_back if output.review_changes_at)
    fresh = newest.timestamp() > 0 and moment - newest < CHANGES_GRACE
    if run_state == RUNNING or fresh:
        return summary(REVIEW_CHANGES, label, "automatic fix round", pull_request=first.url)
    return human(
        REVIEW_HUMAN, f"{label}; no fix round started", DECIDE_FIX,
        pull_request=first.url, cause=CAUSE_CHANGES_ASKED,
    )


def _reviewer_label(url: str, verdict: str, detail: str, held: Sequence[str] = ()) -> str:
    """Why the instance's reviewer will not act on this pull request, in a person's words.

    ``held`` is the labels on a ``held`` review's chat (``OutputFact.review_held``, #2442).
    """
    if verdict == "held":
        where = f" in {detail}" if detail else ""
        return f"External review is {', '.join(held) or 'held'}{where}; waiting for a person"
    if verdict == "unwatched":
        identity = pull_identity(url)
        repository = identity[0] if identity is not None else url
        return f"The reviewer doesn't watch {repository}"
    if verdict == "stalled":
        # The stall is about a merge that did not happen, never about the review (#2327).
        return f"Not merged: {detail}" if detail else "Not merged"
    if verdict == "suppressed":
        return "The reviewer stopped reporting back"
    if verdict == "given_up":
        return "External review gave up: its chats closed without reviewing; waiting for a person"
    if verdict == "reviewed":
        at = f" at {detail[:7]}" if detail else ""
        return f"Reviewed{at}; ready for a person to decide"
    return "In review; waiting for a person"


def parked_for_person(card: Any, *, verifying: bool = False) -> str:
    """The role of the column a person has to move the card out of, or "" (#2219).

    A card resting in Blocked or Needs More Info waits for a person — unless it names the
    issues it waits on, which the engine releases by itself when they close. ``verifying``
    says the current Run's own verification is queued or running (``stale_park``).
    """
    if card is None or getattr(card, "done", False) or getattr(card, "blocked_by", ()):
        return ""
    role = str(getattr(card, "status_role", "") or "")
    if (verifying or prompt_park(card)) and role == NEEDS_MORE_INFO:
        return ""
    return role if role in _PARKED else ""


def prompt_park(card: Any) -> bool:
    """Whether the card rests in Needs More Info only because its chat waits on a prompt (#2887).

    The engine parks it there so the owner hears about it. The chat's own wait already says
    what to do on the chat's page, so the column is not a second question about the Work.
    """
    return str(getattr(card, "parked_as", "") or "") == "prompt"


def stale_park(verification: VerificationSummary | None, run_id: str) -> bool:
    """Whether the current Run's own verification is queued or running (#2777).

    A worker asks its question before it builds, so a Needs More Info column left standing
    while that Run verifies its work is left over from the spec phase, not a question.
    """
    return bool(
        run_id
        and verification is not None
        and verification.run_id == run_id
        and verification.status in {"queued", "running"}
    )


def _work_state(card: Any, issue_state: str) -> str:
    if card is not None:
        if getattr(card, "done", False):
            return "done"
        status = str(getattr(card, "status", "") or "")
        if status:
            return status
    return issue_state


def build_work_card(
    ref: WorkRef,
    *,
    issue: IssueRef | None,
    title: str,
    issue_state: str = "",
    facts: WorkFacts | None = None,
    trace: RunTrace | None = None,
    claims: Sequence[WorkspaceClaim] | None = (),
    verifications: Sequence[RunVerification] = (),
    workspace: WorkWorkspace | None = None,
    attention: Sequence[WorkAttention] = (),
    chats: Sequence[WorkChat] = (),
    issue_updated_at: str = "",
    project: str = "",
    chat_reviews: Mapping[tuple[str, int], Sequence[str]] | None = None,
    jobs: Sequence[WorkJob] = (),
    paused_label: str = "",
) -> WorkCard:
    """One Work's card. Pure: every input was gathered before this runs.

    ``facts`` are the Work's own (``work_facts``): its Poller card, outputs and finish time,
    the one reading of them every Work surface shares (#2229). ``issue_updated_at`` is the
    issue's last GitHub change: the age of last resort (#2226). ``chat_reviews`` are the
    commits finished ChatGPT review chats covered, by the pull request each reviewed (#2411).
    ``jobs`` are its chats' unfinished jobs (``work_jobs``, #2573). ``paused_label`` is the
    project manifest's, for a Work the deck runs itself: no live card carries it (#2944).
    """
    facts = facts or WorkFacts(ref)
    card, finished_at = facts.card, facts.finished_at
    work_id = ref.work_id
    current = trace.current if trace is not None else None
    if current is not None:
        run_id, run_state, reason, next_expected = (
            current.run_id,
            current.state,
            current.reason,
            current.next_expected,
        )
        if current.state in {FINISHED, SUPERSEDED} and trace is not None and trace.preflight:
            reason, next_expected = trace.preflight, trace.preflight_next
    elif trace is not None and trace.preflight and trace.preflight_next:
        run_id, run_state, reason, next_expected = (
            "",
            "preflight",
            trace.preflight,
            trace.preflight_next,
        )
    else:
        run_id, run_state, reason, next_expected = "", "", "", ""
    run_label = (
        _run_label(run_state)
        if run_state and run_state != "preflight"
        else ("not started" if run_state == "preflight" else "no Run")
    )
    verification = summarize_verification(verifications, run_id, workspace, facts.outputs)
    verifying = stale_park(verification, run_id)
    review = summarize_review(
        card, run_id, run_state, outputs=facts.outputs, verifying=verifying
    )
    verifies_itself = _deck_verifies(card, facts.outputs, run_state)
    ci = ci_checks(card, facts.outputs)
    review = _verifying(
        review, verification, run_id, outputs=facts.outputs, ci=ci,
        verifies_itself=verifies_itself,
    )
    if review is not None and next_expected == NEXT_REVIEW:
        # The Run waits on review; the review knows whether that is a reviewer or a person.
        next_expected = review.next_expected
    workspace_lines, workspace_note = compact_workspace(claims)
    issue_url = (
        issue.url
        if issue is not None
        else f"https://github.com/{ref.repository}/issues/{ref.number}"
    )
    source = f"{issue.repository}#{issue.number}" if issue is not None else ref.source_key
    latest = max((chat.last_activity for chat in chats if chat.last_activity), default=None)
    if current is not None and current.latest_at and (latest is None or current.latest_at > latest):
        latest = current.latest_at
    live = card is not None and not getattr(card, "done", False)
    deploying = live and _deploying(card)
    merged_at = _stamp(getattr(card, "deploy_since", "")) if deploying else None
    if merged_at is not None and (latest is None or merged_at > latest):
        # The merge the deploy is of is the Work's latest real event (#2332).
        latest = merged_at
    if latest is None:
        # No chat or Run has moved: the card's own last change, else the issue's, so no
        # row reads as having waited forever (#2226).
        latest = next(
            (
                when
                for when in (
                    _stamp(getattr(card, "dispatch_failed_at", "")),
                    _stamp(getattr(card, "started_at", "")),
                    _stamp(issue_updated_at),
                )
                if when is not None
            ),
            None,
        )
    failed = live and bool(getattr(card, "dispatch_failed", False))
    # A deck pause holds only while the Work is unfinished (#2559): a finished one starts nothing.
    deck_paused = facts.deck_paused and finished_at is None and not facts.awaiting_finish
    # A Work no live card tracks is paused by the same label on its cached issue (#2944): a
    # card handed over to the deck keeps the label a person put on it.
    pause_label = str(getattr(card, "paused_label", "") or "") if live else paused_label
    label_paused = (
        is_paused(card, facts.record)
        if live
        else bool(pause_label) and carries_label(facts.record, pause_label)
    )
    park_note = str(getattr(card, "park_note", "") or "") if live else ""
    rearms = failed and _rearms(card)
    # A worker stopped on a prompt waits on a person, not on a retry (#2896). When no chat
    # card shows that prompt, the Work itself is the place that says so.
    prompting = live and waits_on_person(current)
    prompt_shown = any(
        item.asks or item.interaction
        for item in attention
        if item.state not in HANDOFF_STATES
    )
    if live and _recovers(card, current):
        workers = _worker_chats(trace, chats)
        attention = [
            replace(item, recovering=True) if item.session_key in workers else item
            for item in attention
        ]
    return WorkCard(
        work_id=work_id,
        href=work_href(work_id),
        source_label=source,
        source_url=issue_url,
        title=title or source,
        # A closed issue is a finished Work even after the engine dropped its card, and
        # even before the engine notices its card's issue closed (#2226).
        work_state=(
            "done"
            if finished_at is not None
            else _work_state(card, "done" if issue_state == "closed" else issue_state)
        ),
        run_id=run_id,
        run_state=run_state,
        run_label=run_label,
        run_reason=reason,
        next_expected=next_expected,
        stuck=run_state in _STUCK_STATES and not prompting,
        attention=tuple(attention),
        workspace=workspace_lines,
        workspace_note=workspace_note,
        verification=verification,
        review=review,
        outputs=facts.outputs,
        chat_count=len(chats),
        working_count=sum(1 for chat in chats if chat.working),
        last_activity=latest,
        unreadable=tuple(trace.unreadable) if trace is not None else (),
        finished_at=finished_at,
        # A parked review or release already says so, in its own words.
        parked=(
            ""
            if review is not None and review.needs_human
            else parked_for_person(card, verifying=verifying)
            or (NEEDS_MORE_INFO if prompting and not prompt_shown else "")
        ),
        dispatch_failed=failed,
        run_refusal=(
            str(getattr(card, "dispatch_failed_refusal", "") or "")
            if failed
            else (current.refusal if current is not None else "")
        ),
        dispatch_rearms=rearms,
        revives=int(getattr(card, "revives", 0) or 0) if card is not None else 0,
        release_parked=str(getattr(card, "release_parked", "") or "") if live else "",
        parked_as=str(getattr(card, "parked_as", "") or "") if live else "",
        self_releasing=live and bool(getattr(card, "auto_release", False)),
        verifies_itself=live and verifies_itself,
        ci=ci,
        untested=untested_outputs(card, facts.outputs) if live else (),
        passed_heads=tuple(getattr(card, "review_passed_heads", ()) or ()) if live else (),
        kept_pass_heads=tuple(getattr(card, "review_pass_heads", ()) or ()) if live else (),
        resting=str(getattr(card, "status_role", "") or "") if live else "",
        comments=issue_comments(facts.record),
        awaiting_finish=facts.awaiting_finish,
        deploy_due=facts.deploy_due,
        card_mark=card_mark(card),
        # The worker's own report to a person first: its chat may be out of the scan window
        # while the engine still holds what it said (#2332).
        question=question(park_note) or question(_last_word(chats)),
        project=project,
        deploying=deploying,
        park_reason=blocked_reason(park_note),
        deploy_recheck=live and bool(getattr(card, "deploy_recheck", False)),
        chatgpt_reviewed=chat_reviewed(facts.outputs, chat_reviews or {}),
        working_chats=tuple((chat.title, chat.href) for chat in chats if chat.working),
        woke_itself=any(chat.working for chat in chats)
        and all(chat.background for chat in chats if chat.working),
        paused=label_paused or deck_paused,
        label_paused=label_paused,
        deck_paused=deck_paused,
        starred=facts.starred,
        start_label=facts.start_label,
        staging=facts.staging,
        pause_label=pause_label,
        blocked_by=tuple(str(ref) for ref in getattr(card, "blocked_by", ()) or ()) if live else (),
        jobs=tuple(jobs),
        scheduled=carries_label(facts.record, SCHEDULE_LABEL),
        parked_label=carries_label(facts.record, PARKED_LABEL),
        labels=_labels(facts.record),
    )


def _labels(record: Any) -> tuple[str, ...]:
    labels = record.get("labels") if isinstance(record, dict) else None
    return tuple(str(name) for name in labels) if isinstance(labels, list) else ()


def carries_label(record: Any, label: str) -> bool:
    """Does the cached issue carry ``label``? Case and spacing as GitHub ignores them.

    The one such check for a cached issue record (#2956): the Work card, the Poller card's
    pause and the router's all read it.
    """
    wanted = (label or "").strip().casefold()
    labels = record.get("labels") if isinstance(record, dict) else None
    if not wanted or not isinstance(labels, list):
        return False
    return wanted in {str(name).strip().casefold() for name in labels}


def is_paused(card: Any, record: Any) -> bool:
    """Does the Work's issue carry its instance's paused label (#2548)?

    The deck's cached issue is read first: the webhook brings a label change within seconds,
    where the engine's own reading (``BoardCard.paused``) waits for its next wake. A record
    from before labels were cached falls back to the engine's.
    """
    label = str(getattr(card, "paused_label", "") or "").strip()
    if card is None or not label:
        return False
    if isinstance(record, dict) and isinstance(record.get("labels"), list):
        return carries_label(record, label)
    return bool(getattr(card, "paused", False))


def chat_reviewed(
    outputs: Sequence[OutputFact], reviews: Mapping[tuple[str, int], Sequence[str]]
) -> bool:
    """A finished ChatGPT review covered the result's every pull request at its head (#2411).

    The result is the open pull requests, or once none is open the merged ones: a closed
    one is nobody's result. ``reviews`` maps ``(repository casefolded, number)`` to the
    commits finished review chats were opened about; only the exact head counts, so a
    review of a commit the pull request has since moved past is not this one's.
    """
    result = [output for output in outputs if not output.settled] or [
        output for output in outputs if output.state == "merged"
    ]
    return bool(result) and all(
        any(
            _same_commit(head, output.head)
            for head in reviews.get((output.repository.casefold(), output.number), ())
        )
        for output in result
    )


def _deploying(card: Any) -> bool:
    """The engine waits for this card's deploy, and nothing parked or blocks its release."""
    return bool(
        getattr(card, "awaiting_deploy", False)
        and not getattr(card, "release_blocked", "")
        and not getattr(card, "release_parked", "")
    )


def _rearms(card: Any) -> bool:
    """A dispatch park the engine retries once by itself: the test ``dispatch.rearmable`` makes,
    short of its cooldown, which only says *when*."""
    return bool(
        getattr(card, "dispatch_failed_clean", False)
        and getattr(card, "dispatch_failed_at", "")
        and not getattr(card, "dispatch_rearmed", False)
    )


def _recovers(card: Any, current: Any) -> bool:
    """The engine restarts this card's current Run by itself.

    Only on the Run trace's own evidence: the current Run ended without a report and the trace
    expects the automatic retry, which it says only for a death or an abandonment with restarts
    left (``revive.decide``) or a clean dispatch park's free retry. A Run still going has not
    ended, so nothing is being recovered and a stopped chat stays a person's look (#2235 review).
    """
    return bool(
        active_card(card)
        and current is not None
        and getattr(current, "state", "") in _STUCK_STATES
        and getattr(current, "next_expected", "") == NEXT_RETRY
    )


def _worker_chats(trace: RunTrace | None, chats: Sequence[WorkChat]) -> frozenset[str]:
    """Every session key of a chat that took part in one of this Work's Runs."""
    if trace is None:
        return frozenset()
    current = getattr(trace, "current", None)
    runs = (*((current,) if current is not None else ()), *getattr(trace, "previous", ()))
    keys = {
        key
        for run in runs
        for session in getattr(run, "sessions", ())
        for key in (session.session_key, session.chat_id)
        if key
    }
    for chat in chats:
        legs = {chat.chat_id, chat.session_key, *chat.legs}
        if legs & keys:
            keys |= legs
    return frozenset(keys)


# --- which Works, and which chats, the deck shows -------------------------------------------


def active_card(card: Any) -> bool:
    """A Poller card the engine is actually handling, rather than one waiting in a column."""
    if card is None or getattr(card, "done", False):
        return False
    return bool(
        getattr(card, "ownership", False)
        or getattr(card, "started", False)
        or getattr(card, "generation", "")
        or getattr(card, "dispatch", None) is not None
        or getattr(card, "dispatch_failed", False)
    )


@dataclass(frozen=True)
class WorkCandidate:
    """A Work the deck knows about, with just enough to decide whether to show it."""

    ref: WorkRef
    work: Work | None  # None: a Poller card no chat has joined yet
    card: Any
    last_activity: datetime | None
    # Current operational evidence: a working chat, an active or in-review Poller card, one
    # parked for a person, a Deckhand card waiting on one, a Runtime execution that may still
    # be running, or a verification queued or running. Never age: an old Work with any of
    # these is current.
    live: bool


def work_candidates(
    state: Any,
    projection: WorkProjection,
    *,
    board: Any = None,
    owned_insights: Mapping[str, Sequence[Any]] | None = None,
    legs_by_chat: Mapping[str, Sequence[str]] | None = None,
    current_work_ids: Collection[str] = (),
    awaiting_work_ids: Collection[str] = (),
    presentation: Any = None,
    let_go: Mapping[str, WorkCandidate] | None = None,
    unstarted: Mapping[WorkRef, datetime] | None = None,
) -> list[WorkCandidate]:
    """Every Work with a loaded chat, an active Poller card or a current Runtime or queue
    fact, newest activity first.

    ``current_work_ids`` are Works the Runtime or the job queue says are still executing or
    verifying (``current_work_ids`` in ``work_view``). This is the recurring Active/Recent
    path, so it never reads the durable inventory: a Work only the ``works`` table remembers
    has no current or recent fact, so it is Archived by construction (``archived_works``).
    ``awaiting_work_ids`` are Works whose issue closed and that wait for a person's Finish
    (#2510): each stays a candidate after its chats unload and its card is forgotten, since
    only a person can let it go. ``let_go`` are recently finished Works as an earlier pass
    built them (#3189): one whose chats the live window let go is still built from that.
    ``unstarted`` are open issues no agent works on, by when each was opened
    (``unstarted_issues``, #3558): each is current, so it waits in Needs you until a person
    starts an agent on it, hides it, or it ages past ``UNSTARTED_WINDOW``.
    """
    owned_insights = owned_insights or {}
    legs_by_chat = legs_by_chat or {}
    current_work_ids = frozenset(current_work_ids)
    found: dict[WorkRef, WorkCandidate] = {}
    for work in projection.works:
        card = board.card(work.ref.repository, work.ref.number) if board is not None else None
        legs = [key for chat_id in work.chat_ids for key in legs_by_chat.get(chat_id, (chat_id,))]
        loaded = [state.sessions[key] for key in legs if key in state.sessions]
        latest = max((_when(session) for session in loaded if _when(session)), default=None)
        live = (
            # The same rule as the card's working count: a chat holding only a background
            # command does not keep its Work live (#2783).
            any(_leg_working(session, presentation) for session in loaded)
            or active_card(card)
            or _reviewing(card)
            or bool(parked_for_person(card))
            or any(_waiting(insight) for insight in owned_insights.get(work.work_id, ()))
            or work.work_id in current_work_ids
        )
        found[work.ref] = WorkCandidate(work.ref, work, card, latest, live)
    for card in board.cards.values() if board is not None else ():
        if not (active_card(card) or _reviewing(card) or parked_for_person(card)):
            continue
        ref = WorkRef.from_issue(IssueRef(card.repository, card.number))
        if ref not in found:
            found[ref] = WorkCandidate(ref, None, card, None, True)
    for work_id in current_work_ids:
        ref = parse_work_id(work_id)
        if ref is not None and ref not in found:
            card = board.card(ref.repository, ref.number) if board is not None else None
            found[ref] = WorkCandidate(ref, None, card, None, True)
    for saved in (let_go or {}).values():
        if saved.ref not in found:
            card = board.card(saved.ref.repository, saved.ref.number) if board is not None else None
            found[saved.ref] = replace(saved, card=card, live=False)
    for work_id in awaiting_work_ids:
        ref = parse_work_id(work_id)
        if ref is not None and ref not in found:
            card = board.card(ref.repository, ref.number) if board is not None else None
            found[ref] = WorkCandidate(ref, None, card, None, False)
    for ref, opened in (unstarted or {}).items():
        if ref not in found:
            found[ref] = WorkCandidate(ref, None, None, opened, True)
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        found.values(),
        key=lambda item: (item.last_activity or floor, item.ref.work_id),
        reverse=True,
    )


# How long an open issue no agent works on stays a Work (#3558): older ones were left on
# purpose, and listing every one at once would bury the rest of Needs you.
UNSTARTED_WINDOW = timedelta(days=14)


def unstarted_issues(backlog: Any, *, now: datetime) -> dict[WorkRef, datetime]:
    """Open issues in a Poller repository that no agent works on, by when each was opened.

    One carrying none of its repository's ownership labels, opened within
    ``UNSTARTED_WINDOW`` (#3558). A schedule definition stays open with no agent on purpose,
    so it is not one. Read from the backlog's cached issue lists only; a list
    read too long ago to say what is still open names none, so a closed issue never shows.
    """
    found: dict[WorkRef, datetime] = {}
    if backlog is None:
        return found
    horizon = getattr(backlog, "CLOSURE_HORIZON_S", 3600.0)
    for entry in getattr(backlog, "repositories", ()) or ():
        if not entry.present or entry.age_s is None or entry.age_s > horizon:
            continue
        labels = {label.casefold() for label in backlog.board.ownership_labels(entry.repository)}
        if not labels:
            continue
        for issue in entry.issues:
            opened = _stamp(issue.created_at)
            if (
                not issue.is_open
                or opened is None
                or now - opened > UNSTARTED_WINDOW
                or any(label.casefold() in labels for label in issue.labels)
                or issue.has_label(SCHEDULE_LABEL)
            ):
                continue
            found[WorkRef.from_issue(IssueRef(entry.repository, issue.number))] = opened
    return found


def _waiting(insight: Any) -> bool:
    """A Deckhand card that waits on a person; a finished handoff is not one (#2219)."""
    return getattr(insight, "kind", "") != KIND_FINISHED


def _reviewing(card: Any) -> bool:
    return card is not None and not getattr(card, "done", False) and bool(
        getattr(card, "in_review", False)
    )


# Presentation buckets (#2201). Derived on every read from the candidate's facts; nothing
# stores them, and archiving a Work deletes and hides nothing it owns.
ACTIVE = "active"
RECENT = "recent"
ARCHIVED = "archived"


def classify_work(candidate: WorkCandidate, *, now: datetime) -> str:
    """Active on any current evidence, else Recent within ``RECENT_WORK``, else Archived."""
    if candidate.live:
        return ACTIVE
    if candidate.last_activity is not None and now - candidate.last_activity <= RECENT_WORK:
        return RECENT
    return ARCHIVED


@dataclass(frozen=True)
class WorkBuckets:
    active: tuple[WorkCandidate, ...] = ()
    recent: tuple[WorkCandidate, ...] = ()
    archived: tuple[WorkCandidate, ...] = ()


def bucket_works(candidates: Sequence[WorkCandidate], *, now: datetime) -> WorkBuckets:
    """The candidates split by ``classify_work``, each keeping the candidates' order."""
    found: dict[str, list[WorkCandidate]] = {ACTIVE: [], RECENT: [], ARCHIVED: []}
    for item in candidates:
        found[classify_work(item, now=now)].append(item)
    return WorkBuckets(tuple(found[ACTIVE]), tuple(found[RECENT]), tuple(found[ARCHIVED]))


@dataclass(frozen=True)
class FrontSelection:
    """Which Works the front page builds rows for (#2219).

    Nothing here cuts a Work to make room. Every Active and Recent Work is built and put in
    its section first, and only then does a section bound what it shows
    (``work_story.front_sections``), so a person's question can never be dropped behind
    newer healthy automation. Finished Works are chosen by when they finished: one that
    finished within ``RECENT_WORK`` is built however long ago its chats last moved, and one
    that finished before that is left out unless something current still happens on it.
    """

    shown: tuple[WorkCandidate, ...]
    buckets: WorkBuckets


def front_page_works(
    candidates: Sequence[WorkCandidate],
    *,
    now: datetime,
    finished_at: Mapping[str, datetime] | None = None,
    awaiting: Collection[str] = frozenset(),
) -> FrontSelection:
    """The candidates the front page builds: current, recent and recently finished Works.

    A Work in ``awaiting`` — its issue closed, and no person has finished it (#2510) — is
    built however long ago it closed or last moved: it waits in Needs you until someone does.
    """
    finished_at = finished_at or {}
    awaiting = frozenset(awaiting)
    buckets = bucket_works(candidates, now=now)

    def recently_finished(item: WorkCandidate) -> bool:
        when = finished_at.get(item.ref.work_id)
        return when is not None and now - when <= RECENT_WORK

    def finished_long_ago(item: WorkCandidate) -> bool:
        if item.ref.work_id not in finished_at or recently_finished(item):
            return False
        if item.ref.work_id in awaiting:
            return False
        # A chat carried on in it since is recent Work, however long ago it finished (#2357).
        return continued_at(finished_at[item.ref.work_id], item.last_activity) is None

    shown = [
        *buckets.active,
        *(item for item in buckets.recent if not finished_long_ago(item)),
        *(
            item
            for item in buckets.archived
            if recently_finished(item) or item.ref.work_id in awaiting
        ),
    ]
    return FrontSelection(tuple(shown), buckets)


def continued_at(
    finished_at: datetime | None, last_activity: datetime | None
) -> datetime | None:
    """When a finished Work's chats last moved, if that was well after it finished (#2357).

    A chat carried on in a done Work keeps the Work done, but the front page dates and sorts
    it by this instead of by the close, so the chat can be found again. None when the Work is
    not finished, or its chats have not moved past ``CONTINUED_GRACE`` after it finished.
    """
    if finished_at is None or last_activity is None:
        return None
    return last_activity if last_activity - finished_at > CONTINUED_GRACE else None


# --- the board, in one line (#2219) -------------------------------------------------------------


@dataclass(frozen=True)
class BoardSummary:
    """The Poller board as counts: the front page's replacement for its card list.

    Counted from the engine's own state (``BoardView``), by column *role* rather than a
    column's spelling, so a board that renames its columns still counts. ``complete`` false
    means an instance could not be read, and the counts are only part of the answer.
    """

    cards: int = 0  # every card not in the done column
    in_review: int = 0
    blocked: int = 0
    needs_info: int = 0
    complete: bool = True


def work_project(repository: str, board: Any) -> str:
    """The project a Work in ``repository`` belongs to (#2311): the Poller instance whose
    manifest declares that repository, so an instance's several repositories are one project,
    else the repository's own name. "" when there is no repository to name."""
    key = repository.casefold()
    for scope in getattr(board, "scopes", ()) or ():
        if scope.instance and any(name.casefold() == key for name in scope.issue_repositories):
            return scope.instance
    return repository.rsplit("/", 1)[-1]


def summarize_board(board: Any) -> BoardSummary | None:
    """Counts from the board the overview already read; None when no Poller is installed."""
    if board is None or not (getattr(board, "scopes", ()) or getattr(board, "cards", {})):
        return None
    open_cards = [card for card in board.cards.values() if not getattr(card, "done", False)]
    roles = [str(getattr(card, "status_role", "") or "") for card in open_cards]
    return BoardSummary(
        cards=len(open_cards),
        in_review=sum(1 for card in open_cards if getattr(card, "in_review", False)),
        blocked=roles.count("blocked"),
        needs_info=roles.count("needs_more_info"),
        complete=bool(getattr(board, "complete", True)),
    )


# --- the archive: the durable inventory, read only where it is asked for ---------------------


def archived_count(buckets: WorkBuckets, db: Any) -> int:
    """How many Works are Archived, without reading the inventory's rows (#2201).

    Every Work the deck knows is in the ``works`` table or among the candidates, and every
    Active or Recent Work is a candidate. So the archive is the table's row count, plus the
    candidates the table lacks, minus the Active and Recent ones: one ``COUNT(*)`` and one
    lookup bounded by the candidates. A store that cannot answer counts the candidates only.
    """
    candidates = [*buckets.active, *buckets.recent, *buckets.archived]
    try:
        total = int(db.count_works())
        known = db.known_work_ids(item.ref.work_id for item in candidates)
    except Exception:  # noqa: BLE001 -- an unreadable inventory must not hide the live Works
        return len(buckets.archived)
    total += sum(1 for item in candidates if item.ref.work_id not in known)
    return max(len(buckets.archived), total - len(buckets.active) - len(buckets.recent))


def work_inventory(db: Any) -> tuple[WorkRef, ...]:
    """Every Work the ``works`` table has recorded (#2201). The Archived Works view only.

    The table outlives the chats that named each Work, so it keeps an old Work countable,
    searchable and openable once no provider scan loads its chats. Its ``created_at`` is
    when the row was written, which a backfill sets to the backfill's own time, so it is
    never read as activity. A store without the table gives none.
    """
    load = getattr(db, "load_works", None)
    try:
        rows = load() if load is not None else {}
    except Exception:  # noqa: BLE001 -- an unreadable inventory must not hide the live Works
        return ()
    return tuple(ref for work_id in rows if (ref := parse_work_id(work_id)) is not None)


def in_inventory(db: Any, ref: WorkRef) -> bool:
    """Whether the ``works`` table names this one Work: a single keyed lookup."""
    try:
        return ref.work_id in db.known_work_ids((ref.work_id,))
    except Exception:  # noqa: BLE001 -- unreadable: the page says the Work is unknown
        return False


def archived_works(
    buckets: WorkBuckets, inventory: Sequence[WorkRef], *, board: Any = None
) -> tuple[WorkCandidate, ...]:
    """The Archived candidates, then every Work only the inventory still names.

    A Work only the inventory names has no chat, card, Runtime or queue fact the candidates
    did not already cover, so it is Archived whatever its row's ``created_at`` says.
    """
    seen = {item.ref for item in (*buckets.active, *buckets.recent, *buckets.archived)}
    extra = []
    for ref in sorted(set(inventory) - seen, key=lambda ref: ref.work_id, reverse=True):
        card = board.card(ref.repository, ref.number) if board is not None else None
        extra.append(WorkCandidate(ref, None, card, None, False))
    return (*buckets.archived, *extra)


@dataclass(frozen=True)
class OtherChat:
    """A chat that belongs to no Work: shown as it is, never folded into one."""

    chat_id: str
    session_key: str
    title: str
    href: str
    working: bool
    last_activity: datetime | None
    attention: str = ""
    # A person can act on it here: it waits on one (not a finished handoff) and can still be
    # woken where it ran (#2332). What the front page counts and marks as needing you.
    needs_you: bool = False


# Deckhand states that report a finished turn rather than ask for anything (``triage``): a
# handoff nobody has acknowledged yet is not a human dependency (#2219).
HANDOFF_STATES = frozenset({"finished", "merged", "closed"})


def other_activity(
    state: Any,
    projection: WorkProjection,
    *,
    now: datetime,
    attention_keys: Mapping[str, str] | None = None,
    chats: Any = None,
    presentation: Any = None,
    limit: int = MAX_OTHER_CHATS,
) -> tuple[tuple[OtherChat, ...], int]:
    """Top-level chats in no Work that are working, carded, or moved within ``RECENT_CHAT``.

    Returns the rows to show and how many more matched. A delegated child is left to its
    parent's row in the chat list, as it is there, and a reviewer's chat is left out too.
    """
    attention_keys = attention_keys or {}
    in_work = {chat_id for work in projection.works for chat_id in work.chat_ids}
    rows: dict[str, OtherChat] = {}
    for chat_id, legs in chat_legs(state, chats).items():
        if chat_id in in_work:
            continue
        current, _ = _chat_row(state, chat_id, legs, chats)
        if (
            current is None
            or getattr(current, "parent_session_key", None)
            or getattr(current, "is_delegated", False)
            # A deck-started reviewer's chat, as in the chat list (#3101).
            or is_reviewer_prompt(getattr(current, "initial_prompt", None))
        ):
            continue
        # The chat row's own state, background command included: only a Work leaves that
        # out of its working count (#2783).
        working = any(getattr(state.sessions.get(key), "thinking", False) for key in legs)
        attention = next((attention_keys[leg] for leg in legs if leg in attention_keys), "")
        latest = _latest(state, legs)
        # A chat whose worktree is gone and that cannot resume in its repository (#2524)
        # cannot be answered in place, so its wait keeps it on the list no longer than any
        # other chat (#2332).
        stranded = bool(attention) and cannot_resume(current)
        held = bool(attention) and not stranded
        if not (working or held or (latest is not None and now - latest <= RECENT_CHAT)):
            continue
        shown = presentation.display(current) if presentation is not None else current
        rows[chat_id] = OtherChat(
            chat_id=chat_id,
            session_key=current.key,
            title=getattr(shown, "display_title", current.key),
            href=getattr(current, "detail_path", f"/sessions/{current.key}"),
            working=working,
            last_activity=latest,
            attention=attention,
            needs_you=held and attention not in HANDOFF_STATES,
        )
    floor = datetime.min.replace(tzinfo=UTC)
    ordered = sorted(
        rows.values(),
        key=lambda row: (bool(row.attention) or row.working, row.last_activity or floor),
        reverse=True,
    )
    return tuple(ordered[:limit]), max(0, len(ordered) - limit)


def work_chats(
    state: Any,
    work: Work | None,
    projection: WorkProjection,
    *,
    chats: Any = None,
    legs_by_chat: Mapping[str, Sequence[str]] | None = None,
    presentation: Any = None,
) -> tuple[WorkChat, ...]:
    """The Work's member chats, one row per chat however many legs or accounts it used."""
    if work is None:
        return ()
    legs_by_chat = legs_by_chat if legs_by_chat is not None else chat_legs(state, chats)
    rows = []
    for membership in work.memberships:
        legs = tuple(legs_by_chat.get(membership.chat_id, ())) or (membership.chat_id,)
        session, legs = _chat_row(state, membership.chat_id, legs, chats)
        evidence = tuple(dict.fromkeys(str(item.evidence) for item in membership.provenance))
        pulls: dict[tuple[str, int], str] = {}
        for leg in legs:
            for pull in state.session_pull_requests.get(leg, ()):
                pulls.setdefault(pull.identity, pull.url)
        if session is not None:
            shown = presentation.display(session) if presentation is not None else session
            title = getattr(shown, "display_title", session.key)
            href = getattr(session, "detail_path", f"/sessions/{session.key}")
            key = session.key
        else:
            title, href, key = membership.chat_id, f"/sessions/{legs[0]}", legs[0]
        latest = _latest(state, legs)
        other_works = len(projection.works_for_chat(membership.chat_id)) - 1
        if other_works:
            href = chat_href_in_work(href, work.ref.work_id)
        rows.append(
            WorkChat(
                chat_id=membership.chat_id,
                session_key=key,
                title=title,
                href=href,
                working=_chat_working(state, legs, presentation),
                background=_chat_background(state, legs, presentation),
                last_activity=latest,
                evidence=evidence,
                other_works=other_works,
                pull_requests=tuple(pulls.values()),
                legs=tuple(legs),
                last_text=str(getattr(session, "last_text", "") or "") if session else "",
            )
        )
    floor = datetime.min.replace(tzinfo=UTC)
    return tuple(
        sorted(rows, key=lambda row: (row.working, row.last_activity or floor), reverse=True)
    )


@dataclass(frozen=True)
class UnplacedChat:
    """A delegated chat this Work was one candidate for; it joined no Work (#2207)."""

    chat_id: str
    session_key: str
    title: str
    href: str
    candidates: int  # how many Works its parent was in when it was delegated


def unplaced_chats(
    state: Any,
    ref: WorkRef,
    projection: WorkProjection,
    *,
    chats: Any = None,
    legs_by_chat: Mapping[str, Sequence[str]] | None = None,
    presentation: Any = None,
) -> tuple[UnplacedChat, ...]:
    """Chats saved as ambiguous with this Work among the candidates; never members."""
    found = [item for item in projection.ambiguous if ref in item.candidates]
    if not found:
        return ()
    legs_by_chat = legs_by_chat if legs_by_chat is not None else chat_legs(state, chats)
    rows = []
    for item in found:
        legs = tuple(legs_by_chat.get(item.chat_id, ())) or (item.chat_id,)
        session, legs = _chat_row(state, item.chat_id, legs, chats)
        if session is not None:
            shown = presentation.display(session) if presentation is not None else session
            title = getattr(shown, "display_title", session.key)
            href = getattr(session, "detail_path", f"/sessions/{session.key}")
            key = session.key
        else:
            title, href, key = item.chat_id, f"/sessions/{legs[0]}", legs[0]
        rows.append(UnplacedChat(item.chat_id, key, title, href, len(item.candidates)))
    return tuple(rows)


def work_title(
    ref: WorkRef,
    *,
    backlog: Any = None,
    chats: Sequence[WorkChat] = (),
    record: Mapping[str, Any] | None = None,
    custom: str = "",
    agent: str = "",
    chat: str = "",
) -> tuple[str, str]:
    """(title, issue state): the Work's custom title, else the name its worker gave it, else
    the name its own agent chat gave itself, else GitHub's own words, else the dispatched
    chat's title.

    ``custom`` is the name the operator gave the Work on this deck (#2328,
    ``work_resolution``); it wins over every derived title but never changes the issue state.
    ``agent`` is the name a worker gave the Work when it filed its report (#2474): the issue
    title is whatever the requester typed first — a pasted link, say — and only the worker
    knows what the Work turned out to be. Neither renames the GitHub issue.
    ``chat`` is the title the Work's own agent chat (the one the deck started for it) gave
    itself (#3597): a Work agent names its chat, not its Work, so without it a Work started
    by the deck kept the requester's words. A leading ``Spec:`` is dropped, since the card
    already says when a Work waits on its spec and the label would outlive the spec.

    Nothing here calls GitHub. The issue's own cached record comes first whenever it exists
    (#2226): the webhooks write it through, it outlives the issue closing, and it is not
    lost while a repository's open-issue list is being re-read. The cached list comes next,
    and a chat's title, which a worker names after the task, only when neither knows it.
    """
    title, state = _derived_title(ref, backlog=backlog, chats=chats, record=record)
    return (custom or agent or _without_spec_prefix(chat) or title), state


_SPEC_PREFIX = re.compile(r"^\s*spec\s*:\s*", re.IGNORECASE)


def _without_spec_prefix(title: str) -> str:
    """``title`` without a leading ``Spec:``, unless nothing would be left; the rest starts
    with a capital, as "Spec: stale reads" would otherwise leave "stale reads"."""
    title = title.strip()
    rest = _SPEC_PREFIX.sub("", title)
    if rest == title or not rest:
        return title
    return rest[0].upper() + rest[1:]


def _derived_title(
    ref: WorkRef,
    *,
    backlog: Any,
    chats: Sequence[WorkChat],
    record: Mapping[str, Any] | None,
) -> tuple[str, str]:
    if isinstance(record, Mapping) and not record.get("is_pr"):
        title = str(record.get("title") or "").strip()
        if title:
            return title, str(record.get("state") or "").lower()
    issue = backlog.issue(ref.repository, ref.number) if backlog is not None else None
    if issue is not None:
        return issue.title, issue.state
    # The chat the Poller dispatched for this issue is named after it; a helper chat is not.
    # A chat no longer loaded is titled by its bare id, which names nothing (#3257).
    titled = [chat for chat in chats if chat.title and chat.title != chat.chat_id]
    dispatched = [chat for chat in titled if str(WorkEvidence.ISSUE_URL) in chat.evidence]
    named = dispatched or titled
    return (named[0].title if named else ""), ""
