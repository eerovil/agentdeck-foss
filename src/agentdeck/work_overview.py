"""The Work card: what one Work is, where it has got to and what it needs (#2147).

This is the read model the Work-first UI renders. A **Work** is the human task (#2138), and
this module turns what the deck already knows about one into the handful of facts an
operator reads without opening a chat:

* what it is: its source issue and title, and the issue's own state;
* what needs attention: the Deckhand cards this Work owns (see ``attribute_insights``);
* which workspace: the Work's repository claims (#2149), one line per repository;
* what exists: the newest explicit verification (#2148) and the pull requests attributed to
  the Work itself.

Everything here is derived on every read and nothing is stored. Chats and sessions stay
secondary: they are listed, never used to name an output.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .agent_review import is_reviewer_prompt
from .chat_cwd import cannot_resume
from .issue_context import identity
from .models import BACKGROUND_TASK_ACTIVITY, IssueRef
from .poller_engine.schedules import LABEL as SCHEDULE_LABEL
from .triage import KIND_FINISHED
from .work import Work, WorkEvidence, WorkProjection, WorkRef
from .work_derived import PARKED_LABEL
from .work_facts import (
    DiffStat,
    OutputFact,
    WorkFacts,
    _stamp,
    diffstat,
    target_branches,
)
from .work_hidden import issue_comments
from .work_wording import question
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
    # The form question's own short name (``InteractionQuestion.header``), "" for a question
    # in the chat's text: a wayfinder map names its waiting questions by it (#3696).
    header: str = ""
    # The chat stopped, and something restarts it by itself (a pending quota retry, #2928): a
    # stop there is not a person's to look at.
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
    are the whole answer (#2195).
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
    work_state: str  # the issue's own state, "done" once it finished
    attention: tuple[WorkAttention, ...]
    workspace: tuple[WorkspaceLine, ...]
    workspace_note: str
    verification: VerificationSummary | None
    outputs: tuple[OutputFact, ...]  # the Work's own facts' (``work_facts``), never a page's
    chat_count: int
    working_count: int
    last_activity: datetime | None
    unreadable: tuple[str, ...] = ()
    # When the Work finished (#2219): its issue's closing, never chat activity.
    finished_at: datetime | None = None
    # What a hide is compared against (#2238, ``work_hidden``): the source issue's comment
    # count, None when not known.
    comments: int | None = None
    # What the Work's most recently active chat last asked, in one line (``work_wording``),
    # from the deck's own copy of its transcript: a parked Work's question (#2254).
    question: str = ""
    # Which project the Work belongs to (#2311, ``work_project``): the front page's filter.
    project: str = ""
    # The issue closed and no person has finished the Work yet (``work_facts.awaiting_finish``,
    # #2510): the front page keeps it in Needs you rather than Recently finished.
    awaiting_finish: bool = False
    # A finished ChatGPT review chat on the chat board covered the head of every pull request
    # that is, or was merged as, the Work's result (``chat_reviewed``, #2411).
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
    # (#2559): it sends nothing to its chats.
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
    # Its chats' queued or running jobs (``work_jobs``, #2573).
    jobs: tuple[WorkJob, ...] = ()
    # The issue is a schedule definition (``scheduled`` label, ``poller_engine.schedules``):
    # it stays open on purpose and never has a next step of its own (#2998).
    scheduled: bool = False
    # The issue carries the ``parked`` label (``work_derived.PARKED_LABEL``): a person asked
    # its agent to leave it until they write (#3560).
    parked_label: bool = False
    # The labels on its cached issue, as GitHub spells them: a deck-run Work reads its
    # project's spec-only label from them (#3247).
    labels: tuple[str, ...] = ()
    # The issue's own title, whatever ``title`` the deck shows: a wayfinder map is named by it
    # wherever its questions are pointed at (#3696). "" when no record holds it.
    issue_title: str = ""
    # The one status line of a Work the deck runs itself (``work_derived``, #2895 step 3): a
    # project's Work. ``None`` keeps today's status.
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
        return bool(self.attention)

    @property
    def revision(self) -> str:
        """Changes exactly when the card's facts do, so an unchanged card is never swapped."""
        return hashlib.sha1(repr(self).encode(), usedforsecurity=False).hexdigest()[:12]


def _same_commit(one: str, other: str) -> bool:
    """Two heads, either possibly shortened, surely name the same commit."""
    one, other = one.strip().casefold(), other.strip().casefold()
    return len(one) >= 7 and len(other) >= 7 and (one.startswith(other) or other.startswith(one))


def build_work_card(
    ref: WorkRef,
    *,
    issue: IssueRef | None,
    title: str,
    issue_state: str = "",
    facts: WorkFacts | None = None,
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

    ``facts`` are the Work's own (``work_facts``): its outputs and finish time, the one
    reading of them every Work surface shares (#2229). ``issue_updated_at`` is the issue's
    last GitHub change: the age of last resort (#2226). ``chat_reviews`` are the commits
    finished ChatGPT review chats covered, by the pull request each reviewed (#2411).
    ``jobs`` are its chats' unfinished jobs (``work_jobs``, #2573). ``paused_label`` is the
    project manifest's (#2944).
    """
    facts = facts or WorkFacts(ref)
    finished_at = facts.finished_at
    work_id = ref.work_id
    verification = summarize_verification(verifications, "", workspace, facts.outputs)
    workspace_lines, workspace_note = compact_workspace(claims)
    issue_url = (
        issue.url
        if issue is not None
        else f"https://github.com/{ref.repository}/issues/{ref.number}"
    )
    source = f"{issue.repository}#{issue.number}" if issue is not None else ref.source_key
    latest = max((chat.last_activity for chat in chats if chat.last_activity), default=None)
    if latest is None:
        # No chat has moved: the issue's own last change, so no row reads as having waited
        # forever (#2226).
        latest = _stamp(issue_updated_at)
    # A deck pause holds only while the Work is unfinished (#2559): a finished one starts nothing.
    deck_paused = facts.deck_paused and finished_at is None and not facts.awaiting_finish
    label_paused = bool(paused_label) and carries_label(facts.record, paused_label)
    return WorkCard(
        work_id=work_id,
        href=work_href(work_id),
        source_label=source,
        source_url=issue_url,
        title=title or source,
        # A closed issue is a finished Work (#2226).
        work_state=(
            "done"
            if finished_at is not None or issue_state == "closed"
            else issue_state
        ),
        attention=tuple(attention),
        workspace=workspace_lines,
        workspace_note=workspace_note,
        verification=verification,
        outputs=facts.outputs,
        chat_count=len(chats),
        working_count=sum(1 for chat in chats if chat.working),
        last_activity=latest,
        finished_at=finished_at,
        comments=issue_comments(facts.record),
        awaiting_finish=facts.awaiting_finish,
        question=question(_last_word(chats)),
        project=project,
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
        pause_label=paused_label,
        jobs=tuple(jobs),
        scheduled=carries_label(facts.record, SCHEDULE_LABEL),
        parked_label=carries_label(facts.record, PARKED_LABEL),
        labels=_labels(facts.record),
        issue_title=_issue_title(facts.record),
    )


def _issue_title(record: Any) -> str:
    """The issue's own title on GitHub, "" for none or a pull request's record."""
    if not isinstance(record, dict) or record.get("is_pr"):
        return ""
    return str(record.get("title") or "").strip()


def _labels(record: Any) -> tuple[str, ...]:
    labels = record.get("labels") if isinstance(record, dict) else None
    return tuple(str(name) for name in labels) if isinstance(labels, list) else ()


def carries_label(record: Any, label: str) -> bool:
    """Does the cached issue carry ``label``? Case and spacing as GitHub ignores them.

    The one such check for a cached issue record (#2956): the Work card and the router both
    read it.
    """
    wanted = (label or "").strip().casefold()
    labels = record.get("labels") if isinstance(record, dict) else None
    if not wanted or not isinstance(labels, list):
        return False
    return wanted in {str(name).strip().casefold() for name in labels}


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


# --- which Works, and which chats, the deck shows -------------------------------------------


@dataclass(frozen=True)
class WorkCandidate:
    """A Work the deck knows about, with just enough to decide whether to show it."""

    ref: WorkRef
    work: Work | None  # None: a Work no chat has joined
    last_activity: datetime | None
    # Current operational evidence: a working chat, a Deckhand card waiting on a person, a
    # Runtime execution that may still be running, or a verification queued or running.
    # Never age: an old Work with any of these is current.
    live: bool


def work_candidates(
    state: Any,
    projection: WorkProjection,
    *,
    owned_insights: Mapping[str, Sequence[Any]] | None = None,
    legs_by_chat: Mapping[str, Sequence[str]] | None = None,
    current_work_ids: Collection[str] = (),
    awaiting_work_ids: Collection[str] = (),
    presentation: Any = None,
    let_go: Mapping[str, WorkCandidate] | None = None,
    unstarted: Mapping[WorkRef, datetime] | None = None,
) -> list[WorkCandidate]:
    """Every Work with a loaded chat or a current Runtime or queue fact, newest activity first.

    ``current_work_ids`` are Works the Runtime or the job queue says are still executing or
    verifying (``current_work_ids`` in ``work_view``). This is the recurring Active/Recent
    path, so it never reads the durable inventory: a Work only the ``works`` table remembers
    has no current or recent fact, so it is Archived by construction (``archived_works``).
    ``awaiting_work_ids`` are Works whose issue closed and that wait for a person's Finish
    (#2510): each stays a candidate after its chats unload, since only a person can let it go.
    ``let_go`` are recently finished Works as an earlier pass built them (#3189): one whose
    chats the live window let go is still built from that.
    ``unstarted`` are open issues no agent works on, by when each was opened
    (``unstarted_issues``, #3558): each is current, so it waits in Needs you until a person
    starts an agent on it, hides it, or it ages past ``UNSTARTED_WINDOW``.
    """
    owned_insights = owned_insights or {}
    legs_by_chat = legs_by_chat or {}
    current_work_ids = frozenset(current_work_ids)
    found: dict[WorkRef, WorkCandidate] = {}
    for work in projection.works:
        legs = [key for chat_id in work.chat_ids for key in legs_by_chat.get(chat_id, (chat_id,))]
        loaded = [state.sessions[key] for key in legs if key in state.sessions]
        latest = max((_when(session) for session in loaded if _when(session)), default=None)
        live = (
            # The same rule as the card's working count: a chat holding only a background
            # command does not keep its Work live (#2783).
            any(_leg_working(session, presentation) for session in loaded)
            or any(_waiting(insight) for insight in owned_insights.get(work.work_id, ()))
            or work.work_id in current_work_ids
        )
        found[work.ref] = WorkCandidate(work.ref, work, latest, live)
    for work_id in current_work_ids:
        ref = parse_work_id(work_id)
        if ref is not None and ref not in found:
            found[ref] = WorkCandidate(ref, None, None, True)
    for saved in (let_go or {}).values():
        if saved.ref not in found:
            found[saved.ref] = replace(saved, live=False)
    for work_id in awaiting_work_ids:
        ref = parse_work_id(work_id)
        if ref is not None and ref not in found:
            found[ref] = WorkCandidate(ref, None, None, False)
    for ref, opened in (unstarted or {}).items():
        if ref not in found:
            found[ref] = WorkCandidate(ref, None, opened, True)
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(
        found.values(),
        key=lambda item: (item.last_activity or floor, item.ref.work_id),
        reverse=True,
    )


# How long an open issue no agent works on stays a Work (#3558): older ones were left on
# purpose, and listing every one at once would bury the rest of Needs you.
UNSTARTED_WINDOW = timedelta(days=14)


def unstarted_issues(
    backlog: Any,
    *,
    now: datetime,
    mine: Callable[[str, str], bool] | None = None,
) -> dict[WorkRef, datetime]:
    """Open issues in a Poller repository that no agent works on, by when each was opened.

    One carrying none of its repository's ownership labels, opened within
    ``UNSTARTED_WINDOW`` (#3558). A schedule definition stays open with no agent on purpose,
    so it is not one, and nor is an issue another deck works, which ``mine(repository,
    assignee)`` says from its first assignee (``work_claims``, #3731): one assigned to someone
    else, and an unassigned one where another deck takes those. The owner chose that over
    offering it for Start agent there, which flooded that deck's list. Read
    from the backlog's cached issue lists only; a list read too long ago to say what is still
    open names none, so a closed issue never shows.
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
                or (
                    mine is not None
                    and not mine(entry.repository, (issue.assignees or ("",))[0].lower())
                )
            ):
                continue
            found[WorkRef.from_issue(IssueRef(entry.repository, issue.number))] = opened
    return found


@dataclass(frozen=True)
class OtherDeckWork:
    """An issue another deck works, shown on this one read only (#3834).

    ``assignee`` is the first assignee's login, lowercased ("" for nobody); ``started`` says
    an agent label is on it, so the other deck's agent works it; ``pull`` is the cached open
    pull request belonging to it, if any; ``start_label`` is the label Take over adds when
    none is on.
    """

    ref: WorkRef
    title: str
    url: str
    assignee: str
    started: bool
    labels: tuple[str, ...]
    start_label: str
    pull: Mapping[str, Any] | None = None
    updated: datetime | None = None
    # Every assignee, lowercased: Take over removes them all, or a second one could stay
    # ahead of this deck's person.
    assignees: tuple[str, ...] = ()

    @property
    def work_id(self) -> str:
        return self.ref.work_id

    @property
    def href(self) -> str:
        return work_href(self.ref.work_id)


def other_deck_works(
    backlog: Any,
    *,
    now: datetime,
    mine: Callable[[str, str], bool] | None,
) -> tuple[OtherDeckWork, ...]:
    """Open issues in a Poller repository that another deck works, newest first (#3834).

    The issues ``unstarted_issues`` leaves out because ``mine`` says they are another
    deck's (``work_claims``): every one carrying an agent label, and the ones no agent has
    started yet that were opened within ``UNSTARTED_WINDOW``, the same window as Not
    started. A schedule definition is never one. With no deck person named, ``mine`` says
    every issue is this deck's, so the list is empty. Read from the backlog's cached lists
    only, with the same freshness rule, so nothing new is asked of GitHub.
    """
    if backlog is None or mine is None:
        return ()
    horizon = getattr(backlog, "CLOSURE_HORIZON_S", 3600.0)
    pulls_by_issue = backlog.pulls_for_work() if hasattr(backlog, "pulls_for_work") else {}
    found: list[OtherDeckWork] = []
    for entry in getattr(backlog, "repositories", ()) or ():
        if not entry.present or entry.age_s is None or entry.age_s > horizon:
            continue
        owned = list(backlog.board.ownership_labels(entry.repository))
        if not owned:
            continue
        folded = {label.casefold() for label in owned}
        for issue in entry.issues:
            assignee = (issue.assignees or ("",))[0].strip().lower()
            if not issue.is_open or issue.has_label(SCHEDULE_LABEL):
                continue
            if mine(entry.repository, assignee):
                continue
            started = any(label.casefold() in folded for label in issue.labels)
            opened = _stamp(issue.created_at)
            if not started and (opened is None or now - opened > UNSTARTED_WINDOW):
                continue
            pulls = pulls_by_issue.get(identity(entry.repository, issue.number)) or ()
            found.append(
                OtherDeckWork(
                    ref=WorkRef.from_issue(IssueRef(entry.repository, issue.number)),
                    title=issue.title,
                    url=issue.url,
                    assignee=assignee,
                    started=started,
                    labels=tuple(issue.labels),
                    start_label=owned[0],
                    pull=pulls[0] if pulls else None,
                    updated=_stamp(issue.updated_at) or opened,
                    assignees=tuple(
                        login.strip().lower() for login in issue.assignees if login.strip()
                    ),
                )
            )
    floor = datetime.min.replace(tzinfo=UTC)
    return tuple(sorted(found, key=lambda item: item.updated or floor, reverse=True))


def _waiting(insight: Any) -> bool:
    """A Deckhand card that waits on a person; a finished handoff is not one (#2219)."""
    return getattr(insight, "kind", "") != KIND_FINISHED


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


# --- which project a Work belongs to ----------------------------------------------------------


def work_project(repository: str, board: Any) -> str:
    """The project a Work in ``repository`` belongs to (#2311): the Poller instance whose
    manifest declares that repository, so an instance's several repositories are one project,
    else the repository's own name. "" when there is no repository to name."""
    key = repository.casefold()
    for scope in getattr(board, "scopes", ()) or ():
        if scope.instance and any(name.casefold() == key for name in scope.issue_repositories):
            return scope.instance
    return repository.rsplit("/", 1)[-1]


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
    buckets: WorkBuckets, inventory: Sequence[WorkRef]
) -> tuple[WorkCandidate, ...]:
    """The Archived candidates, then every Work only the inventory still names.

    A Work only the inventory names has no chat, Runtime or queue fact the candidates did not
    already cover, so it is Archived whatever its row's ``created_at`` says.
    """
    seen = {item.ref for item in (*buckets.active, *buckets.recent, *buckets.archived)}
    extra = []
    for ref in sorted(set(inventory) - seen, key=lambda ref: ref.work_id, reverse=True):
        extra.append(WorkCandidate(ref, None, None, False))
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
    # The chat started for this issue is named after it; a helper chat is not.
    # A chat no longer loaded is titled by its bare id, which names nothing (#3257).
    titled = [chat for chat in chats if chat.title and chat.title != chat.chat_id]
    dispatched = [chat for chat in titled if str(WorkEvidence.ISSUE_URL) in chat.evidence]
    named = dispatched or titled
    return (named[0].title if named else ""), ""
