"""The Work-first UI's loaders and its one background refresher (#2147).

``load_work_overview`` builds every dashboard Work card in one pass, sharing one read of the
Poller board, the job queue and the stores between them. ``load_work_detail`` builds one
Work's page, including the inspected workspace, which the dashboard never pays for.

``WorkOverviewService`` is the dashboard's cache boundary for Works. It rebuilds the overview
on a cadence and shortly after a session or Deckhand change, and publishes the ``works``
topic only when the overview actually changed. The dashboard stream renders Work cards from
that snapshot alone, so a Work change never renders the legacy chat list, and a chat change
that leaves every Work card as it was sends nothing at all.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from markupsafe import Markup

from .. import (
    live_prototype,
    review_refs,
    watch_retry,
    wayfinder_map,
    work_bugs,
    work_counter,
    work_tree,
)
from ..board_watch import BOARD_TOPIC
from ..chat_board import ESCALATED, NEEDS_YOU_LABELS
from ..chat_board import TOPIC as CHAT_BOARD_TOPIC
from ..chat_notes import TOPIC as CHAT_NOTES_TOPIC
from ..jobs import WorkRows, rows_for_work
from ..models import IssueRef
from ..poller_engine import verification
from ..providers.claude_code.auth import looks_like_auth_failure
from ..quota_retry import QUOTA_RETRY_ID_PREFIX
from ..quota_retry import message_id_for as quota_retry_message_id
from ..review_refs import review_target
from ..runtime_feed import RUNTIME_TOPIC
from ..staging_deploys import staging_copy
from ..state import REVIEWS_TOPIC, chat_topic
from ..wake_on import KIND_PULL_REQUEST, KIND_REVIEW, NeedsYouStop, WakeRequest
from ..work import Work, WorkRef, work_id_for
from ..work_actions import (
    BRING_BACK,
    ActionButton,
    WorkActionPress,
    buttons,
    finish_control,
    merge_approval_control,
    pause_control,
    pause_refusal,
    star_control,
)
from ..work_attention import (
    ISSUE_CLOSED,
    MERGE,
    NEEDS_ANSWER,
    UNSTARTED,
    OperatorAttention,
    operator_attention,
)
from ..work_facts import (
    FINISH_SLACK,
    Registration,
    WorkFacts,
    awaiting_finish,
    discovery_urls,
    pull_identity,
    pull_state,
    work_facts,
    work_finished_at,
)
from ..work_hidden import HiddenWork, still_hidden
from ..work_overview import (
    CONTINUED_GRACE,
    MAX_DASHBOARD_WORKS,
    RECENT_CHAT,
    RECENT_WORK,
    OtherChat,
    OtherDeckWork,
    UnplacedChat,
    WorkAttention,
    WorkCandidate,
    WorkCard,
    WorkChat,
    archived_count,
    attribute_insights,
    build_work_card,
    chat_legs,
    continued_at,
    front_page_works,
    in_inventory,
    other_activity,
    other_deck_works,
    unplaced_chats,
    unstarted_issues,
    work_candidates,
    work_chats,
    work_href,
    work_jobs,
    work_project,
    work_title,
)
from ..work_resolution import WorkResolution
from ..work_status_log import StatusPoint, changes
from ..work_stops import FORGET_AFTER, facts_mark, is_unseen, track_stops
from ..work_story import (
    NEEDS_YOU,
    FrontRow,
    FrontSections,
    MessageTarget,
    WorkspaceRow,
    WorkStatus,
    front_row,
    front_sections,
    message_target,
    opening_chat,
    waiting_attention,
    waiting_story,
    work_status,
    workspace_rows,
)
from ..work_workspace import (
    WorkPullRequest,
    WorkWorkspace,
    gather_work_workspace,
    parse_work_id,
    run_verifications,
)
from ..work_workspace_claims import WorkspaceClaim, read_claims
from .render import chat_board_service

logger = logging.getLogger(__name__)

WORKS_TOPIC = "works"
# The top bar's list of new Needs you Works (#3239): published only when that list changed.
NEEDS_YOU_TOPIC = "needs_you"

# The sources that failed during the overview pass running now (#2751): each read below that
# answers nothing rather than raise names itself here, so the status log can tell a Work that
# moved because its facts moved from one that moved because a read came back empty. Unset
# outside an overview pass, where nothing collects them.
_DEGRADED: ContextVar[list[str] | None] = ContextVar("work_overview_degraded", default=None)
# What one overview pass asked about a repository, asked once per pass (#3228): whether the
# deck runs its Works itself, and its paused label. The router's answer re-reads every
# poller manifest's stat, and a pass asked it once or twice per Work.
_PASS_REPOS: ContextVar[dict | None] = ContextVar("work_overview_repos", default=None)


def _per_pass(key: tuple, read: Callable[[], Any]) -> Any:
    """``read()``, once per overview pass for ``key``; every call outside a pass reads."""
    memo = _PASS_REPOS.get()
    if memo is None:
        return read()
    if key not in memo:
        memo[key] = read()
    return memo[key]


def _switched(app: Any, repository: str) -> bool:
    """Whether the deck runs ``repository``'s Works itself: a poller manifest declares it."""
    switched = getattr(getattr(app.state, "work_router", None), "switched", None)
    if switched is None:
        return False
    return _per_pass(("switched", repository.lower()), lambda: bool(switched(repository)))


def _degraded(source: str) -> None:
    found = _DEGRADED.get()
    if found is not None and source not in found:
        found.append(source)


# --- shared reads ---------------------------------------------------------------------------


# A slow Runtime must not hold the dashboard's Work cards back; a missed read shows no
# verification, never "verified".
_RUNTIME_READ_TIMEOUT_S = 5.0


async def _runtime_rows(
    client: httpx.AsyncClient, path: str, key: str, params: dict | None = None
) -> list[dict]:
    try:
        response = await client.get(path, params=params, timeout=_RUNTIME_READ_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        _degraded(f"runtime{path}")
        return []
    found = payload.get(key) if isinstance(payload, dict) else None
    return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []


async def runtime_queue(app: Any, *, work_id: str = "") -> list[dict]:
    """The Runtime's live job queue, or no rows when it cannot be reached.

    ``work_id`` asks for only the rows that Work's page reads (#2837). Filtered here as well,
    so a Runtime from before the filter, which sends the whole queue, gives the same rows.
    """
    client: httpx.AsyncClient | None = getattr(app.state, "runtime_http", None)
    if client is None:
        return []
    if not work_id:
        return await _runtime_rows(client, "/jobs", "jobs")
    rows = await _runtime_rows(client, "/jobs", "jobs", {"work_id": work_id})
    return rows_for_work(rows, work_id)


async def runtime_jobs(
    app: Any, work_ids: Sequence[str], *, queue: list[dict] | None = None
) -> list[dict]:
    """These Works' verification jobs: their retained evidence plus the live queue (#2148).

    One queue read for all of them (none when ``queue`` was already read this pass), and one
    evidence read per Work: the evidence ledger keeps every finished job, so reading all of
    it would grow with the deck's whole history. An unreachable runtime gives no rows, which
    the cards show as no verification.
    """
    client: httpx.AsyncClient | None = getattr(app.state, "runtime_http", None)
    if client is None:
        return []
    evidence = await asyncio.gather(
        *(
            _runtime_rows(client, "/job-evidence", "evidence", {"work_id": work_id})
            for work_id in work_ids
        )
    )
    batches = [*evidence, queue if queue is not None else await runtime_queue(app)]
    rows: dict[str, dict] = {}
    for batch in batches:  # the queue's row, read last, wins over the evidence's copy
        for row in batch:
            if row.get("id"):
                rows[str(row["id"])] = row
    return list(rows.values())


def _db_path(app: Any) -> str | None:
    config = getattr(app.state, "config", None)
    return getattr(getattr(config, "history", None), "db_path", None)


# Job states that mean a verification is still in progress.
_PENDING_JOB_STATES = frozenset({"queued", "running"})


# How long after an ended turn its uncollected background commands still count as the chat's
# next step (#2890). A command that never finishes (a server left running) must not hold the
# Work forever.
BACKGROUND_HOLD = timedelta(hours=2)


async def current_work_ids(app: Any, queue: Sequence[dict]) -> frozenset[str]:
    """Works with a verification queued or running (#2201), read from the queue in hand."""
    named = [
        str(job.get("work_id") or "")
        for job in queue
        if job.get("purpose") == verification.PURPOSE
        and str(job.get("state") or "") in _PENDING_JOB_STATES
    ]
    # One spelling per Work, so the ids match the Works list's own however a job typed it.
    refs = (parse_work_id(work_id) for work_id in named)
    return frozenset(ref.work_id for ref in refs if ref is not None)


def _read_claims(
    db_path: str | Path | None, work_ids: Sequence[str]
) -> dict[str, list[WorkspaceClaim] | None]:
    """Each Work's workspace claims, read-only, in one thread hop."""
    if db_path is None:
        return {work_id: [] for work_id in work_ids}
    return {work_id: read_claims(db_path, work_id) for work_id in work_ids}


def _board_and_backlog(app: Any) -> tuple[Any, Any]:
    issues = getattr(app.state, "issues", None)
    if issues is None:
        return None, None
    board = issues.read_board()
    try:
        backlog = issues.cached()
    except Exception:  # noqa: BLE001 -- titles are decoration; a bad cache must not hide Works
        logger.debug("issue cache unreadable for Work titles", exc_info=True)
        _degraded("issue_backlog")
        backlog = None
    return board, backlog


def _attention(
    app: Any,
    insights: Sequence[Any],
    presentation: Any,
    states: Mapping[str, str],
    pending_message_ids: Collection[str] = (),
) -> tuple[WorkAttention, ...]:
    """The Deckhand cards a Work owns. ``states`` is the pass's ``insight_states``: the same
    for every Work, so read once per pass rather than once per card (#2841)."""
    if not insights:
        return ()
    assistant = getattr(app.state, "assistant", None)
    state = app.state.app_state
    sessions = state.sessions
    rows = []
    for insight in insights:
        session = sessions.get(insight.session_key)
        shown = presentation.display(session) if session is not None and presentation else session
        asks, question, interaction, header = _open_question(state, assistant, insight, session)
        retry_id = quota_retry_message_id(session) if session is not None else None
        rows.append(
            WorkAttention(
                session_key=insight.session_key,
                chat_title=getattr(shown, "display_title", "") or insight.session_key,
                state=states.get(insight.session_key, insight.kind),
                href=getattr(session, "detail_path", "") or f"/sessions/{insight.session_key}",
                asks=asks,
                question=question,
                interaction=interaction,
                header=header,
                recovering=bool(retry_id and retry_id in pending_message_ids),
                cause=str(getattr(insight, "cause", "") or ""),
            )
        )
    return tuple(rows)


# Pending interactions that ask the operator a question; the rest (a permission, a command
# or file approval, a browser step) ask for a decision, not an answer.
_QUESTION_INTERACTIONS = frozenset({"question", "mcp_form"})


def _open_question(
    state: Any, assistant: Any, insight: Any, session: Any
) -> tuple[bool, str, str, str]:
    """``(asks, question, interaction kind, header)`` for the chat behind a Deckhand card
    (#2224); the header is the form question's own short name, "" for a question in text.

    The deck's own facts decide it, never the card. A live pending interaction comes first,
    read the way Deckhand reads it, and asks only when it is question-style. Otherwise the
    chat's open question decides while one is recorded: a reply sets its ``answered_seq`` at
    once, while ``Session.question`` keeps the same text until a later agent turn proves the
    chat went on, so the session text is trusted only when no record exists.
    """
    from ..providers import pending_interaction_for

    accounts = getattr(assistant, "accounts", None)
    pending = (
        pending_interaction_for(session, accounts)
        if session is not None and accounts is not None
        else None
    )
    if pending is not None:
        first = next((q for q in pending.questions if q.prompt.strip()), None)
        asked = first.prompt if first is not None else ""
        text = (asked or pending.message or pending.title or "").strip()
        header = str(first.header or "").strip() if first is not None else ""
        kind = pending.kind or "interaction"
        return pending.kind in _QUESTION_INTERACTIONS, text, kind, header
    finder = getattr(state, "open_question_for", None)
    parked = finder(insight.session_key) if finder is not None else None
    if parked is not None:
        text = parked.question.strip() if parked.answered_seq is None else ""
        return bool(text), text, "", ""
    text = str(getattr(session, "question", "") or "").strip()
    return bool(text), text, "", ""


@dataclass(frozen=True)
class _Pass:
    """One snapshot of everything the Work cards read, gathered once."""

    state: Any
    projection: Any
    chats: Any
    legs: dict[str, tuple[str, ...]]
    board: Any
    backlog: Any
    insights: tuple[Any, ...]
    owned: dict[str, list[Any]]
    presentation: Any
    # The backlog's cached open pull requests by the issue each one closes or, in its own
    # repository, names in its branch (#2226).
    open_pulls: dict[str, tuple[dict, ...]] = field(default_factory=dict)
    # Every custom Work title by id (#2328): one control-DB read for the whole pass.
    titles: dict[str, str] = field(default_factory=dict)
    agent_titles: dict[str, str] = field(default_factory=dict)  # a worker's names (#2474)
    # The names Works' own agent chats gave themselves, by Work id (#3597).
    chat_titles: dict[str, str] = field(default_factory=dict)
    # The commits finished ChatGPT review chats covered, by the pull request each reviewed
    # (#2411): the chat board's rows in memory and one local read of the recorded heads.
    chat_reviews: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)
    # The still-open review chats, keyed the same way (#2895, D7 rule 6; #2943).
    open_reviews: dict[tuple[str, int], tuple[OpenReview, ...]] = field(default_factory=dict)
    # The review chats that escalated to the owner, keyed the same way (#3216).
    escalated_reviews: dict[tuple[str, int], EscalatedReview] = field(default_factory=dict)
    # Each visible Deckhand card's rendered attention state, by session key: one walk of the
    # insights per pass, not one per Work card (#2841).
    insight_states: dict[str, str] = field(default_factory=dict)
    # Every scheduled/dispatching message id in one pass-wide DB read. A cancelled or failed
    # quota retry is absent, so its stopped chat returns to Needs retry (#2928 review).
    pending_message_ids: frozenset[str] = frozenset()
    # The chats a message is being handed to, or was just delivered to (#3084).
    message_handoffs: dict[str, datetime] = field(default_factory=dict)


def _cached_issues(
    app: Any, candidates: Sequence[Any], *, strict: bool = False
) -> dict[str, dict]:
    """Each candidate's cached issue record, in one local metadata-cache read (#2219).

    Every Work asks: the record is the issue's title after it closes, and its ``closedAt``
    finishes the Work (#2226). No GitHub call is made; a Work the deck never resolved simply
    has no record. ``strict`` raises a failed read rather than answering with no records
    (#3553).
    """
    issues = getattr(app.state, "issues", None)
    lookup = getattr(issues, "cached_issues", None)
    if lookup is None:
        return {}
    wanted = {(item.ref.repository, item.ref.number): item.ref.work_id for item in candidates}
    try:
        records = lookup(wanted)
    except Exception:  # noqa: BLE001 -- a cache that cannot answer leaves every Work unfinished
        logger.debug("issue cache unreadable for the Work overview", exc_info=True)
        _degraded("cached_issues")
        if strict:
            raise
        return {}
    return {wanted[ref]: record for ref, record in records.items() if ref in wanted}


def _open_pulls(backlog: Any) -> dict[str, tuple[dict, ...]]:
    """The backlog's cached open pull requests keyed by the issue each one closes or, in its
    own repository, names in its branch."""
    by_issue = getattr(backlog, "pulls_for_work", None)
    return by_issue() if by_issue is not None else {}


def _cached_pulls(app: Any, urls: Sequence[str], *, strict: bool = False) -> dict[str, dict]:
    """The cached row of each pull request by lowercased URL, in one local read (#2226).

    The rows ``git_context`` and the webhook write-through keep; no GitHub call is made, and
    a pull request the deck never resolved has no row, which reads as state unknown.
    """
    issues = getattr(app.state, "issues", None)
    lookup = getattr(issues, "cached_pulls", None)
    wanted = {}
    for url in urls:
        identity = pull_identity(url)
        if identity is not None:
            wanted[identity] = url.lower()
    if lookup is None or not wanted:
        return {}
    try:
        records = lookup(wanted)
    except Exception:  # noqa: BLE001 -- unknown pull request state is never a guess
        logger.debug("pull request cache unreadable for the Work overview", exc_info=True)
        _degraded("cached_pulls")
        if strict:
            raise
        return {}
    return {wanted[ref]: record for ref, record in records.items() if ref in wanted}


@dataclass(frozen=True)
class _PassReads:
    """The part of a pass read from files and databases, which can run off the event loop."""

    board: Any
    backlog: Any
    titles: dict[str, str]
    agent_titles: dict[str, str]
    chat_titles: dict[str, str]
    review_heads: dict[int, dict[tuple[str, int], str]]
    pending_message_ids: frozenset[str]
    message_handoffs: dict[str, datetime]


def _pass_reads(app: Any) -> _PassReads:
    """The pass's file and database reads: the Poller board, the cached backlog, the control
    DB's names, and the recorded review heads. Each holds its own lock or
    reads files, so ``gather_pass_off_loop`` runs this in a thread (#2841)."""
    board, backlog = _board_and_backlog(app)
    scheduler = getattr(app.state, "scheduled_messages", None)
    pending_message_ids = (
        scheduler.pending_ids()
        if scheduler is not None and hasattr(scheduler, "pending_ids")
        else frozenset()
    )
    from ..work_derived import HANDOFF_GRACE

    now = datetime.now(UTC)
    handoffs = getattr(scheduler, "handoffs", None)
    message_handoffs = handoffs(now, now - HANDOFF_GRACE) if handoffs is not None else {}
    return _PassReads(
        board=board,
        backlog=backlog,
        titles=_custom_titles(app),
        agent_titles=_agent_titles(app),
        chat_titles=_chat_titles(app),
        review_heads=_review_heads(app, app.state.app_state),
        pending_message_ids=pending_message_ids,
        message_handoffs=message_handoffs,
    )


async def gather_pass_off_loop(app: Any) -> _Pass:
    """``gather_pass`` with its file and database reads in a thread, so a rebuild of the Work
    cards does not hold every other request on the event loop while it reads (#2841). The
    in-memory snapshot is still taken on the loop, where the state it reads is changed."""
    return gather_pass(app, await asyncio.to_thread(_pass_reads, app))


def gather_pass(app: Any, reads: _PassReads | None = None) -> _Pass:
    from .render import assistant_insight_states

    state = app.state.app_state
    assistant = getattr(app.state, "assistant", None)
    if reads is None:
        reads = _pass_reads(app)
    board, backlog = reads.board, reads.backlog
    projection = state.work_projection()
    chats = state.chat_snapshot()
    insights = tuple(assistant.visible_insights) if assistant is not None else ()
    chat_of = {
        insight.session_key: state.chat_identity_key(insight.session_key, chats)
        for insight in insights
    }
    return _Pass(
        state=state,
        projection=projection,
        chats=chats,
        legs=chat_legs(state, chats),
        board=board,
        backlog=backlog,
        insights=insights,
        owned=attribute_insights(insights, projection, chat_of),
        presentation=state.session_presentation(),
        open_pulls=_open_pulls(backlog),
        titles=reads.titles,
        agent_titles=reads.agent_titles,
        chat_titles=reads.chat_titles,
        chat_reviews=_chat_reviews(app, reads.review_heads),
        open_reviews=_open_reviews(app, reads.review_heads),
        escalated_reviews=_escalated_reviews(app, reads.review_heads),
        insight_states=assistant_insight_states(assistant) if assistant is not None else {},
        pending_message_ids=reads.pending_message_ids,
        message_handoffs=reads.message_handoffs,
    )


def _review_heads(app: Any, state: Any) -> dict[int, dict[tuple[str, int], str]]:
    """The recorded commit of every review chat in the chat board's repository (#2411)."""
    service = chat_board_service(app.state)
    if service is None:
        return {}
    try:
        return state.chat_review_heads(service.repository)
    except Exception:  # noqa: BLE001 -- an unreadable record credits no review, as before
        logger.debug("chat review heads unreadable for the Work overview", exc_info=True)
        _degraded("chat_reviews")
        return {}


def _chat_reviews(
    app: Any, heads: Mapping[int, Mapping[tuple[str, int], str]], *, state: str = "closed"
) -> dict[tuple[str, int], tuple[str, ...]]:
    """What the chat's ChatGPT chip calls reviewed, for the Work's Review step (#2411).

    A review chat opened by hand is on the chat board, and the deck recorded the commit it
    was opened about, but the instance's reviewer never hears of it. Only a finished review
    with a recorded commit counts: an unknown commit is never credited to the current head.
    ``state="open"`` reads the reviews still running instead (#2895).
    """
    service = chat_board_service(app.state)
    if service is None or not heads:
        return {}
    found: dict[tuple[str, int], tuple[str, ...]] = {}
    for row in list(service.board.rows.values()):
        if row.state != state or review_target(row.title) is None:
            continue
        # Each pull request the chat covered, not the chat's title: a Work's chat is titled
        # by its issue and covers every pull request it listed (#2637).
        for key, head in heads.get(row.number, {}).items():
            found[key] = (*found.get(key, ()), head)
    return found


@dataclass(frozen=True)
class OpenReview:
    """One still-open review chat about one pull request (#2895, #2943)."""

    chat: int
    head: str  # the commit the deck recorded when it opened the chat
    #: The newest review chat the deck recorded for this pull request, open or closed: its
    #: head is the deck's own latest word on the pull request's head, which the card's cached
    #: copy can lag behind.
    newest: bool = False
    opened_at: datetime | None = None
    #: The last sign of a reviewer: the opening, the chat's own activity, a fallback start.
    active_at: datetime | None = None
    reviewing: bool = False  # a fallback reviewer of ours runs on it now
    #: When its quiet means no reviewer is coming; None while one runs or when unknown.
    stuck_at: datetime | None = None
    #: Labelled ``blocked`` or ``needs-more-info``: a reviewer stopped in it (#2974).
    held: bool = False
    #: Closed by a reviewer that gave up, with the Work's next chat still owed by the deck:
    #: until when the deck tries to open it (#3192). ``None`` for an open chat.
    reopen_until: datetime | None = None
    #: Why ChatGPT was not started on it, in words for the owner (#3326); "" when it was.
    start_failed: str = ""
    #: Why it is ``held``, from the newest ``Blocked:`` comment (#3356); "" when none said.
    held_why: str = ""
    #: A reviewer of ours ran on it, ended, and left it held: the deck's one retry is spent,
    #: and the review is stuck at once rather than after the quiet wait (#3356).
    stopped_by_ours: bool = False


def _open_reviews(
    app: Any, heads: Mapping[int, Mapping[tuple[str, int], str]]
) -> dict[tuple[str, int], tuple[OpenReview, ...]]:
    """The open review chats of each pull request, with when each opened and was last
    worked on (#2943). The chat board's rows in memory, the recorded heads and one read of
    the fallback reviewer's ledger; no GitHub call."""
    service = chat_board_service(app.state)
    if service is None or not heads:
        return {}
    def _moment(value: str) -> datetime | None:
        try:
            at = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return at if at.tzinfo is not None else None

    newest: dict[tuple[str, int], int] = {}
    for chat, pulls in heads.items():
        for key in pulls:
            newest[key] = max(newest.get(key, 0), chat)
    fallback = getattr(app.state, "review_fallback", None)
    started: dict[int, tuple[float, bool]] = {}
    quiet: float | None = None
    start = None
    if fallback is not None and fallback.enabled:
        try:
            started = fallback.reviewers_on()
            quiet = fallback.quiet_after_s()
        except Exception:  # noqa: BLE001 -- unreadable: no stuck call, never a false one
            logger.debug("review fallback ledger unreadable for the Work overview", exc_info=True)
            started, quiet = {}, None
        try:
            # ChatGPT is only pressed while the switch is off (#3326).
            start = fallback.last_start() if fallback.switch() is None else None
        except Exception:  # noqa: BLE001 -- unreadable: no failure named, never a false one
            logger.debug("last ChatGPT start unreadable for the Work overview", exc_info=True)
    from ..review_fallback import start_failed_on

    found: dict[tuple[str, int], tuple[OpenReview, ...]] = {}
    for row in list(service.board.rows.values()):
        if row.state != "open" or review_target(row.title) is None:
            continue
        opened = _moment(row.created_at)
        moments = [at for at in (opened, _moment(row.updated_at)) if at is not None]
        last_start, reviewing = started.get(row.number, (None, False))
        if last_start is not None:
            moments.append(datetime.fromtimestamp(last_start, UTC))
        active = max(moments, default=None)
        held = any(label in row.labels for label in NEEDS_YOU_LABELS)
        # Only a `blocked` review is a failed one; `needs-more-info` waits on a person's answer.
        blocked = "blocked" in row.labels and "needs-more-info" not in row.labels
        # A failure our reviewer confirmed: a `Blocked:` reason written after it started. A
        # person's bare `blocked` label, or an older run's reason, is not one.
        stopped_at = service.board.blocked_at(row.number) if blocked else None
        ours_stopped = (
            stopped_at is not None
            and last_start is not None
            and stopped_at >= last_start
            and not reviewing
        )
        stuck_at = (
            (active if ours_stopped else active + timedelta(seconds=quiet))
            if quiet is not None and active is not None and not reviewing
            else None
        )
        for key, head in heads.get(row.number, {}).items():
            review = OpenReview(
                chat=row.number,
                head=head,
                newest=newest.get(key) == row.number,
                opened_at=opened,
                active_at=active,
                reviewing=reviewing,
                stuck_at=stuck_at,
                held=held,
                start_failed=start_failed_on(row, start),
                held_why=service.board.block_reason(row.number) if blocked else "",
                stopped_by_ours=ours_stopped,
            )
            found[key] = (*found.get(key, ()), review)
    if fallback is not None and fallback.enabled:
        # A give-up whose next chat the deck is about to open (#3192): the review is still
        # the deck's, not an agent's turn that ended without a next step.
        from ..review_fallback import REOPEN_WITHIN_S, awaiting_reopen

        for number, _target, gave_up in awaiting_reopen(service.board, now=time.time()):
            row = service.board.rows.get(number)
            if row is None:
                continue
            at = datetime.fromtimestamp(gave_up, UTC)
            for key, head in heads.get(number, {}).items():
                review = OpenReview(
                    chat=number,
                    head=head,
                    newest=newest.get(key) == number,
                    opened_at=_moment(row.created_at),
                    active_at=at,
                    reopen_until=at + timedelta(seconds=REOPEN_WITHIN_S),
                )
                found[key] = (*found.get(key, ()), review)
    return found


@dataclass(frozen=True)
class EscalatedReview:
    """A pull request's newest review chat, closed by a reviewer escalating to the owner."""

    chat: int
    ref: str  # the chat as ``owner/repo#n``
    head: str  # the commit the deck recorded when it opened the chat
    at: datetime | None  # when the reviewer escalated
    url: str = ""  # the escalation comment, with the reviewer's question; "" when not named


def _escalated_reviews(
    app: Any, heads: Mapping[int, Mapping[tuple[str, int], str]]
) -> dict[tuple[str, int], EscalatedReview]:
    """Each pull request whose newest review chat ended escalated to the owner (#3216).

    Neither a pass nor findings: the reviewer asks the owner a question and reviews nothing
    more by itself, so the Work waits on the owner rather than on a review. Only the newest
    chat counts, so a review asked for again replaces it. The chat board's memory, no GitHub.
    """
    service = chat_board_service(app.state)
    if service is None or not heads:
        return {}
    newest: dict[tuple[str, int], int] = {}
    for chat, pulls in heads.items():
        for key in pulls:
            newest[key] = max(newest.get(key, 0), chat)
    found: dict[tuple[str, int], EscalatedReview] = {}
    for number, (ending, at) in list(service.board.endings.items()):
        if ending != ESCALATED:
            continue
        for key, head in heads.get(number, {}).items():
            if newest.get(key) == number:
                found[key] = EscalatedReview(
                    number,
                    f"{service.repository}#{number}",
                    head,
                    datetime.fromtimestamp(at, UTC),
                    service.board.escalations.get(number, ""),
                )
    return found


def _custom_titles(app: Any) -> dict[str, str]:
    """The names the operator gave Works on this deck (#2328); none is the derived title."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return {}
    try:
        return control.work_titles()
    except Exception:  # noqa: BLE001 -- an unreadable name is the derived title, never a gap
        logger.debug("custom Work titles unreadable", exc_info=True)
        return {}


def _agent_titles(app: Any) -> dict[str, str]:
    """The names workers gave their Works when they reported (#2474), by Work id."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return {}
    try:
        return control.work_agent_titles()
    except Exception:  # noqa: BLE001 -- an unreadable name is the derived title, never a gap
        logger.debug("worker Work titles unreadable", exc_info=True)
        return {}


def _chat_titles(app: Any) -> dict[str, str]:
    """The names Works' own agent chats gave themselves, by Work id (#3597)."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return {}
    try:
        return control.work_start_chat_titles()
    except Exception:  # noqa: BLE001 -- an unreadable name is the derived title, never a gap
        logger.debug("Work-start chat titles unreadable", exc_info=True)
        return {}


def start_chat_title(snapshot: Any, work_id: str) -> str:
    """The name the chat the deck started for this Work gave itself, "" for none (#3597).

    Only that chat (``Db.work_start_chat_titles``): a helper, reviewer or rescue chat in the
    Work never names it."""
    return snapshot.chat_titles.get(work_id, "")


# --- one Work card, rebuilt only when what it reads moved (ADR 0007, #2865) ------------------

# A card and the front page derive a few states from the clock: a finished Work coming back,
# the recent windows. Each ends this
# long after a stamp the card or its facts carry, so a reused card is only reused until the
# earliest such moment — a deadline, not a cache age.
_TIME_THRESHOLDS = (
    CONTINUED_GRACE,
    FINISH_SLACK,
    RECENT_WORK,
    RECENT_CHAT,
)


def _stamps(value: Any, depth: int = 0):
    """Every aware datetime inside a card or its facts, a few levels deep."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            yield value
        return
    if depth > 3:
        return
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            yield from _stamps(getattr(value, item.name), depth + 1)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _stamps(item, depth + 1)


def _derived_deadlines(value: Any):
    """The moment each card's derived status changes by itself (#2943: a review going
    quiet), in a card or a sequence of cards."""
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _derived_deadlines(item)
        return
    deadline = getattr(getattr(value, "derived", None), "deadline", None)
    if isinstance(deadline, datetime) and deadline.tzinfo is not None:
        yield deadline


def time_deadline(now: datetime, *values: Any) -> datetime | None:
    """The first moment after ``now`` a clock-derived state of these may change."""
    return min(
        (
            *(
                moment
                for value in values
                for stamp in _stamps(value)
                for gap in _TIME_THRESHOLDS
                if (moment := stamp + gap) > now
            ),
            *(moment for value in values for moment in _derived_deadlines(value) if moment > now),
        ),
        default=None,
    )


#: Published when a derived status's own inputs move: a wake-on (#2895).
WORK_INPUTS_TOPIC = "work_inputs"


def _derived_key(app: Any, snapshot: Any, ref: WorkRef) -> tuple:
    """What a derived status reads beyond the chats, for the card cache (#2895 step 3)."""
    if not _switched(app, ref.repository):
        return ()
    from ..work_router import owner_of_issue

    owner = owner_of_issue(
        snapshot.state, ref.repository, ref.number, projection=getattr(snapshot, "projection", None)
    )
    wake_on = getattr(app.state, "wake_on", None)
    registry = getattr(wake_on, "registry", None)
    waits = tuple(registry.for_chat(owner[0])) if registry is not None and owner else ()
    reviews = tuple(sorted((getattr(snapshot, "open_reviews", None) or {}).items()))
    escalated = tuple(sorted((getattr(snapshot, "escalated_reviews", None) or {}).items()))
    # The release's Ready for merge and the deck's one request for a next step (#2960).
    awaiting = tuple(sorted(_awaiting_merge(app, ref.work_id).items()))
    # And the passes behind it, which a Ready for merge line names (#2992).
    passed = tuple(sorted(_pass_marks(app, ref.work_id)))
    # Its place in the deck's merge queue (#3022).
    queued = tuple(_queue_places(app, ref.work_id))
    # What its own pull request waits for (#3097).
    held = _pr_hold(app, ref.work_id, ())
    endings = getattr(app.state, "work_endings", None)
    asked = endings.get(ref.work_id) if endings is not None else (None, None)
    retried = tuple(
        sorted(
            review.chat
            for _key, chats in reviews
            for review in chats
            if endings is not None and endings.review_started(review.chat)
        )
    )
    handoffs = getattr(snapshot, "message_handoffs", None) or {}
    handed = (owner[0] in handoffs, handoffs.get(owner[0])) if owner else (False, None)
    # The check of its merged change in the live app (#3141).
    live = _live_state(app, ref.work_id)
    # The deck's own retry of a turn a full account stopped, while it is scheduled (#3305).
    pending = getattr(snapshot, "pending_message_ids", None) or frozenset()
    retrying = tuple(sorted(i for i in pending if i.startswith(QUOTA_RETRY_ID_PREFIX)))
    return (
        owner, waits, reviews, escalated, awaiting, passed, asked, retried, queued, handed, held,
        live, retrying,
    )


def _deck_paused_label(app: Any, repository: str) -> str:
    """The paused label of a Work the deck runs itself (#2944): its project manifest's."""
    if not _switched(app, repository):
        return ""
    ports = getattr(getattr(app.state, "work_release", None), "ports", None)
    policy = getattr(ports, "policy", None)
    if policy is None:
        return ""

    def read() -> str:
        try:
            return str(getattr(policy(repository), "paused_label", "") or "")
        except Exception:  # noqa: BLE001 -- an unreadable manifest pauses nothing
            return ""

    return _per_pass(("paused_label", repository.lower()), read)


def _reads_closed(card: WorkCard) -> bool:
    """D7 rule 5, "Issue closed — finish it?": the issue closed and nobody signed it off yet.

    Once a person finished it (the sign-off cleared ``awaiting_finish``), never back to a
    finish question the Finish route would refuse (D15).
    """
    return card.awaiting_finish


def _still_finished(app: Any, work_id: str, card: WorkCard, session: Any) -> bool:
    """Finished: its issue closed, a person signed it off, and nothing is left open (D15).

    With every pull request settled it stays finished whatever its chat says later (#2944):
    a restart notice, the hand-over notice or a reply to a follow-up ends a turn too, and
    none of those is work. Only a running turn or an open question shows over it.

    With a pull request still open, a turn that ended after the sign-off reads through D7
    again, so it shows what became of that pull request. The press time is the sign-off
    record's (#2510); a Work finished before sign-offs were recorded is measured from its
    issue's closing.
    """
    if card.finished_at is None or card.awaiting_finish:
        return False
    if all(output.settled for output in card.outputs):
        return True
    control = getattr(app.state, "control_db", None)
    signed = None
    if control is not None:
        signed = (control.work_signoffs().get(work_id) or (None, None))[1]
    since = signed or card.finished_at
    ended = getattr(session, "last_message_at", None) if session is not None else None
    if ended is None or since is None:
        return True
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=UTC)
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return ended <= since


def derived_for(
    app: Any, snapshot: Any, ref: WorkRef, card: WorkCard, jobs: Sequence[Any] = ()
) -> Any:
    """The derived status of a Work the deck runs itself, or ``None`` (#2895 step 3, D7).

    Only a Work in a repository a poller manifest declares. Read from the owning chat (its
    question box, turn, stall, failure and last text), its wake-ons, the open review chats and
    the card's own facts; nothing is stored.
    """
    from ..work_derived import DerivedInputs, derived_status
    from ..work_router import owner_of_issue

    if not _switched(app, ref.repository):
        return None
    state = snapshot.state
    owner = owner_of_issue(
        state, ref.repository, ref.number, projection=getattr(snapshot, "projection", None)
    )
    session = None
    if owner is not None:
        chat = state.chat_by_id(owner[0])
        session = state.current_session_for_chat(chat) if chat is not None else None
        session = session or state.sessions.get(owner[0])
    question = ""
    assistant = getattr(app.state, "assistant", None)
    accounts = getattr(assistant, "accounts", None)
    if session is not None and accounts is not None:
        from ..providers import pending_interaction_for

        pending = pending_interaction_for(session, accounts)
        if pending is not None and pending.kind in _QUESTION_INTERACTIONS:
            asked = next((q.prompt for q in pending.questions if q.prompt.strip()), "")
            question = (asked or pending.message or pending.title or "a question").strip()
        elif pending is not None:
            # A permission the provider asks for even under bypass (``rm -rf *``, say) holds
            # the turn as surely as a question: it read as an ending and was filed as a bug
            # (#3138). Its title ("Allow Bash?") is the line; its message is the command.
            question = (pending.title or pending.message or "approve the agent's request").strip()
    # A turn or a helper agent, not a background command left running after the turn ended
    # (#683, #2783): the Work overview's own ``_leg_working``, plus a stalled turn, exactly as
    # the release's ``work_busy`` reads it. Only a live worker is ever marked stalled: the
    # provider sets the flag from the worker's real liveness (#2956).
    from ..work_overview import _leg_working

    stalled = bool(session is not None and session.stalled)
    running = bool(
        session is not None
        and (_leg_working(session, getattr(snapshot, "presentation", None)) or stalled)
    )
    # D7 rule 6, "no verdict at its head", read from the deck's own record of review chats: a
    # ready pull request is in review only while a review chat opened about it is still open,
    # at the card's head or at the newest head the deck recorded for it (#2943: the card's
    # cached head can lag the push the review was opened for). A verdict of any kind, from any
    # reviewer, ends that chat.
    reviewing = getattr(snapshot, "open_reviews", None) or {}
    review: Any = None
    reviewed_output: Any = None
    for output in card.outputs:
        if output.settled or output.draft:
            continue
        head = output.head.lower()
        chats = reviewing.get((output.repository.casefold(), output.number), ())
        review = next(
            (
                chat
                for chat in chats
                if chat.newest
                or (head and len(chat.head) >= 7 and head.startswith(chat.head.lower()))
            ),
            None,
        )
        if review is not None:
            review, reviewed_output = (output.number, review), output
            break
    # The reviewer escalated an open pull request's current head to the owner (#3216).
    escalations = getattr(snapshot, "escalated_reviews", None) or {}
    escalated: Any = None
    for output in card.outputs:
        found = escalations.get((output.repository.casefold(), output.number))
        head = output.head.lower()
        if (
            not output.settled
            and found is not None
            and (not head or (len(found.head) >= 7 and head.startswith(found.head.lower())))
        ):
            escalated = (output, found)
            break
    # Every open pull request reviewed and tested, a person's Merge all that is missing: the
    # release's own answer, at the heads it was given for (#2960).
    ready = _ready_for_merge(app, ref.work_id, card.outputs)
    ready_reviewed_at, ready_review_chat = _ready_review(app, ref.work_id, ready)
    if review is not None and ready is not None:
        # The pass is recorded from the verdict comment, a few seconds before its review chat
        # closes: a chat opened before that pass is answered, not still reviewing (#3324).
        passed_at, _ = _ready_review(app, ref.work_id, reviewed_output)
        opened = review[1].opened_at
        if passed_at is not None and opened is not None and _aware(opened) <= passed_at:
            review = None
    # Its place in the deck's merge queue (#3022): the open pull request nearest the front.
    queue_place, queued = None, None
    for place in sorted(_queue_places(app, ref.work_id), key=lambda row: row[1]):
        queued = next(
            (
                output
                for output in card.outputs
                if not output.settled
                and f"{output.repository}#{output.number}".lower() == place[0]
            ),
            None,
        )
        if queued is not None:
            queue_place = place
            break
    endings = getattr(app.state, "work_endings", None)
    nudged_at, nudged_for = endings.get(ref.work_id) if endings is not None else (None, None)
    waits: list[tuple[str, datetime | None]] = []
    registry = getattr(getattr(app.state, "wake_on", None), "registry", None)
    # A wait on the Work's own pull request merging is the Merge a Ready line asks for, so a
    # ready Work drops it rather than reading "Waiting on pull request …" (#3003). A wait on
    # anything else (a render, CI elsewhere) still comes first.
    # Any other wait on its own pull request says what that pull request waits for (#3097).
    own_pulls = {(o.repository.casefold(), o.number) for o in card.outputs}
    pr_waits: list[tuple[str, datetime | None]] = []
    if registry is not None and owner is not None:
        for wait in registry.for_chat(owner[0]):
            own = (
                wait.kind == KIND_PULL_REQUEST
                and (wait.repository.casefold(), wait.number) in own_pulls
            )
            if own and ready is not None:
                continue
            until = datetime.fromtimestamp(wait.deadline, UTC) if wait.deadline else None
            (pr_waits if own else waits).append((_wait_words(wait, own_pulls), until))
    verification = card.verification
    if verification is not None and getattr(verification, "status", "") in {"queued", "running"}:
        waits.append(("its tests", None))
    elif any(_open_deck_verification(job, ref.work_id) for job in jobs):
        # A deck-run Work has no Run, so its verification job never becomes the card's
        # ``verification`` (``run_verifications`` reads Run jobs only), and the agentdeck#2974
        # run read as an ending without a next step while it ran (#2965).
        waits.append(("its tests", None))
    waits.extend((job.label, None) for job in card.jobs)
    # A command the turn left running in the background wakes the chat when it exits (#3405):
    # the agent's own way to wait on a sweep or a build. Not work (#2783), but its next step,
    # held for ``BACKGROUND_HOLD`` (#2890): Claude stops a background command by then.
    from ..models import BACKGROUND_TASK_ACTIVITY

    ended_at = getattr(session, "last_message_at", None) if session is not None else None
    if (
        session is not None
        and ended_at is not None
        and getattr(session, "thinking", False)
        and getattr(session, "activity", None) == BACKGROUND_TASK_ACTIVITY
    ):
        hold = _aware(ended_at) + BACKGROUND_HOLD
        if datetime.now(UTC) < hold:
            waits.append(("its background command", hold))
    # A message handed to the chat after its turn ended, a fired wake-on's among them: the
    # turn it starts is the Work's next step until it shows as running (#3084).
    from ..work_derived import HANDOFF_GRACE

    handoffs = getattr(snapshot, "message_handoffs", None) or {}
    handed, handed_until = False, None
    if owner is not None and owner[0] in handoffs:
        handed_at = handoffs[owner[0]]
        if ended_at is None or handed_at > (
            ended_at if ended_at.tzinfo is not None else ended_at.replace(tzinfo=UTC)
        ):
            handed, handed_until = True, handed_at + HANDOFF_GRACE
    # The latest thing the agent said in this turn, else what the provider says it does
    # (#2944): "Using tools" alone says nothing, and is dropped.
    progress = ""
    if session is not None:
        spoke = getattr(session, "last_role", "") == "agent" and session.last_text
        progress = str(session.last_text if spoke else getattr(session, "activity", "") or "")
    # The pull request a Stopped line names: the open one, else the newest that landed.
    named = [o for o in card.outputs if not o.settled] or list(card.outputs)
    pr = named[-1] if named else None
    chat_path = getattr(state, "chat_path_for_session", None)
    chat_href = ""
    if session is not None and chat_path is not None:
        try:
            chat_href = str(chat_path(session.key) or "")
        except Exception:  # noqa: BLE001 -- the line stands without a link
            chat_href = ""
    pr_hold = _pr_hold(app, ref.work_id, card.outputs) if pr_waits else _PrHold()
    moved = [
        _aware(at)
        for at in (ended_at, pr_hold.since, pr_hold.findings_at, pr_hold.passed_at)
        if at is not None
    ]
    # The chat marked itself waiting for a person and ended its turn (#3106): the Mac runner's
    # wrapper does, for a command only the boss's tap runs. That is its next step. The mark
    # expires with the next user turn; one with a detail is the runtime's own (the loop guard).
    waiting_on_you, approval_href = "", ""
    attention_for = getattr(state, "attention_report_for", None)
    report = attention_for(session.key) if session is not None and attention_for else None
    if (
        report is not None
        and report.state == "waiting"
        and not report.detail
        and not running
        and getattr(session, "last_role", "") == "agent"
    ):
        from ..mac_runner import approval_link

        waiting_on_you = str(session.last_text or "").strip() or "the agent is waiting for you"
        approval_href = approval_link(waiting_on_you)
    # A turn that failed on a full account or a lost connection, with the deck's own retry
    # scheduled for the reset (#3305): that retry continues it, so it is a wait, not a death.
    retry_at = getattr(session, "automatic_retry_at", None) if session is not None else None
    if retry_at is not None and quota_retry_message_id(session) not in (
        getattr(snapshot, "pending_message_ids", None) or ()
    ):
        retry_at = None
    # A turn the CLI failed at once because the account's sign-in expired (#3354): the deck
    # did nothing wrong, and only a person's sign-in brings the account back.
    login_expired = ""
    if (
        session is not None
        and session.turn_failed
        and getattr(session, "last_role", "") == "agent"
        and looks_like_auth_failure(session.last_text)
    ):
        account = str(getattr(session, "account_key", "") or "")
        login_expired = account.partition(":")[2] or account or "chat's"
    cut_off = bool(session is not None and getattr(session, "turn_cut_off", False) and not running)
    cut_off_at = getattr(session, "turn_cut_off_at", None) if cut_off else None
    pr_open = any(output.is_open for output in card.outputs)
    # Merged and nothing open (#3578): the live check is due whether or not the issue closed.
    merged = not pr_open and any(output.state == "merged" for output in card.outputs)
    live = _live_state(app, ref.work_id) if card.awaiting_finish or merged else _LiveState()
    spec = _posted_spec(app, ref, card, owner[0] if owner is not None else "")
    inputs = DerivedInputs(
        question=question,
        running=running,
        stalled=stalled,
        progress=progress,
        handed=handed,
        handed_until=handed_until,
        last_progress=getattr(session, "last_progress", None) if session is not None else None,
        died=bool(session is not None and session.turn_failed and not running),
        died_at=getattr(session, "last_message_at", None) if session is not None else None,
        retry_at=retry_at,
        login_expired=login_expired,
        # A turn a runtime death cut off is not an ending (#3008): asking it for a next step
        # raced the runtime's own revival and read its lost question as none.
        cut_off=cut_off,
        cut_off_at=cut_off_at,
        reviving=bool(cut_off and getattr(session, "revival_due", False)),
        waiting_on_you=waiting_on_you,
        approval_href=approval_href,
        closed=_reads_closed(card),
        pr_open=pr_open,
        merged=merged,
        live_pending=live.pending,
        live_result=live.result,
        live_line=live.line,
        # A person finished it (D15): it stays finished until a message wakes its chat.
        finished=_still_finished(app, ref.work_id, card, session),
        review_pr=review[0] if review is not None else None,
        review_since=review[1].opened_at if review is not None else None,
        review_active=review[1].active_at if review is not None else None,
        review_stuck_at=review[1].stuck_at if review is not None else None,
        review_chat=review[1].chat if review is not None else None,
        review_held=bool(review is not None and review[1].held),
        review_held_why=review[1].held_why if review is not None else "",
        review_stopped_by_ours=bool(review is not None and review[1].stopped_by_ours),
        review_reopen_until=review[1].reopen_until if review is not None else None,
        review_start_failed=review[1].start_failed if review is not None else "",
        review_retried=bool(
            review is not None
            and (
                review[1].stopped_by_ours
                or (endings is not None and endings.review_started(review[1].chat))
            )
        ),
        escalated_pr=escalated[0].number if escalated is not None else None,
        escalated_at=escalated[1].at if escalated is not None else None,
        escalated_chat=escalated[1].ref if escalated is not None else "",
        escalated_url=escalated[1].url if escalated is not None else "",
        waits=tuple(waits),
        pr_waits=tuple(pr_waits),
        pr_hold=pr_hold.words,
        pr_hold_person=pr_hold.person,
        pr_findings_at=pr_hold.findings_at,
        pr_findings_told=pr_hold.findings_told,
        pr_quiet_since=max(moved, default=None),
        last_text=(
            str(session.last_text or "")
            if session is not None and getattr(session, "last_role", "") == "agent"
            else ""
        ),
        ended_at=ended_at,
        ready_pr=ready.number if ready is not None else None,
        ready_reviewed_at=ready_reviewed_at,
        ready_review_chat=ready_review_chat,
        queue_pr=queued.number if queued is not None else None,
        queue_place=queue_place[1] if queue_place else 0,
        queue_size=queue_place[2] if queue_place else 0,
        queue_round=queue_place[3] if queue_place else 0,
        nudged_at=nudged_at,
        nudged_for=nudged_for,
        asks=endings is not None,
        pr=pr.number if pr is not None else None,
        chat_href=chat_href,
        scheduled=card.scheduled,
        parked=card.parked_label,
        spec_posted_at=spec[0] if spec is not None else None,
        spec_url=spec[1] if spec is not None else "",
    )
    return derived_status(inputs, now=datetime.now(UTC))


def _posted_spec(
    app: Any, ref: WorkRef, card: WorkCard, chat_id: str
) -> tuple[datetime, str] | None:
    """``(posted at, url)`` of a spec-only Work's spec waiting for approval, else ``None``.

    The issue carries its project's spec-only label, no pull request is open, and the owning
    chat's newest comment on the issue (``chat_notes``, the deck's own logins) is its spec,
    marked with ``SPEC_MARKER`` as its brief says, and known to be the issue's newest comment
    (``issue_latest_comment``): any other comment since, or no known newest comment, leaves the
    Work to the rules below as usual (#3247).
    """
    from ..issue_latest_comment import newest_comment_id
    from ..work_overview import carries_label
    from ..work_signals import SPEC_MARKER

    if not chat_id or any(output.is_open for output in card.outputs):
        return None
    try:
        policy = app.state.work_release.ports.policy(ref.repository)
        label = str(getattr(policy, "spec_only_label", "") or "")
    except Exception:  # noqa: BLE001 -- no policy read: no spec-only label known
        return None
    if not carries_label({"labels": list(card.labels)}, label):
        return None
    store = getattr(app.state, "control_db", None)
    try:
        notes = store.chat_notes(chat_id) if store is not None else []
    except Exception:  # noqa: BLE001 -- unreadable: the Work reads as any other
        return None
    mine = [
        note
        for note in notes
        if note.repository == ref.repository.casefold()
        and note.number == ref.number
        and not note.is_pull
    ]
    note = max(mine, key=lambda note: note.comment_id, default=None)
    if note is None or SPEC_MARKER not in note.body:
        return None
    # Known to be the issue's newest: an unknown, deleted or unreadable anchor proves nothing.
    if newest_comment_id(f"{ref.repository}#{ref.number}") != note.comment_id:
        return None
    try:
        posted = datetime.fromisoformat(note.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(posted), note.url


@dataclass(frozen=True)
class _PrHold:
    """What a Work's own open pull request waits for, as the release last said it (#3097)."""

    words: str = ""
    person: bool = False
    since: datetime | None = None
    findings_at: datetime | None = None
    findings_told: bool = False
    passed_at: datetime | None = None


def _pr_hold(app: Any, work_id: str, outputs: Sequence[Any]) -> _PrHold:
    """The release's last hold of this Work, a reviewer's findings since, and its last pass.

    A Work the release has not looked at since this was recorded reads from its passes alone:
    an open pull request with no pass at the head the card holds is not reviewed there, which
    is how a lost pass looked (#3097).
    """
    from ..work_release import same_commit

    store = getattr(getattr(app.state, "work_release", None), "store", None)
    try:
        entry = store.get(work_id) if store is not None else {}
    except Exception:  # noqa: BLE001 -- unreadable: the wait stands without its reason
        return _PrHold()
    entry = entry if isinstance(entry, Mapping) else {}
    passes = entry.get("passes") if isinstance(entry.get("passes"), Mapping) else {}
    stamps = [_stamp(row.get("at")) for row in passes.values() if isinstance(row, Mapping)]
    passed_at = max((at for at in stamps if at is not None), default=None)
    hold = entry.get("hold") if isinstance(entry.get("hold"), Mapping) else {}
    since = _stamp(hold.get("since"))
    words, person = str(hold.get("words") or ""), bool(hold.get("person"))
    if not hold and entry.get("needs_verify"):
        words = "no test run started"
    elif not hold:
        offered = [o for o in outputs if not o.settled and not o.draft and o.head]
        for output in offered:
            row = passes.get(f"{output.repository}#{output.number}".lower()) or {}
            if not same_commit(output.head, str(row.get("head") or "")):
                words = "not reviewed at its head"
                break
    findings = entry.get("findings") if isinstance(entry.get("findings"), Mapping) else {}
    found_at = _stamp(findings.get("at"))
    if found_at is not None and since is not None and found_at < since:
        found_at = None  # answered by a push since: the hold moved on
    return _PrHold(
        words=words,
        person=person,
        since=since,
        findings_at=found_at,
        findings_told=bool(findings.get("told")),
        passed_at=passed_at,
    )


def _stamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), UTC) if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _open_deck_verification(job: Any, work_id: str) -> bool:
    """A queued or running verification job for this Work that no Run owns (#2965)."""
    return (
        isinstance(job, Mapping)
        and job.get("purpose") == "verification"
        and str(job.get("work_id") or "").casefold() == work_id.casefold()
        and not job.get("run_id")
        and job.get("state") in {"queued", "running"}
    )


def _wait_words(wait: Any, own_pulls: set[tuple[str, int]]) -> str:
    """What a Work's line says it waits on, never naming a pull request (#3720)."""
    if wait.kind not in {KIND_PULL_REQUEST, KIND_REVIEW}:
        return wait.target
    own = (wait.repository.casefold(), wait.number) in own_pulls
    what = "its pull request" if own else "another pull request"
    return what if wait.kind == KIND_PULL_REQUEST else f"the review of {what}"


def _pass_marks(app: Any, work_id: str) -> list[tuple[str, str, str]]:
    """``(pull, when, review chat)`` per recorded review pass, for the card cache (#2992)."""
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    try:
        passes = store.get(work_id).get("passes") or {} if store is not None else {}
        return [
            (str(pull), str(row.get("at") or ""), str(row.get("chat") or ""))
            for pull, row in passes.items()
            if isinstance(row, dict)
        ]
    except Exception:  # noqa: BLE001 -- unreadable: nothing to name
        return []


def _queue_places(app: Any, work_id: str) -> list[tuple[str, int, int, int]]:
    """``(pull, place, size, rounds)`` per pull request of this Work in a merge queue (#3022)."""
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    if store is None:
        return []
    try:
        return sorted(store.place(work_id))
    except Exception:  # noqa: BLE001 -- unreadable: in no queue
        return []


def _release_read(release: Any, name: str, work_id: str) -> str:
    """One of the release's words about an early Merge (#3320); "" when it has none."""
    read = getattr(release, name, None)
    try:
        return str(read(work_id) or "") if read is not None else ""
    except Exception:  # noqa: BLE001 -- unreadable: nothing offered
        return ""


def _awaiting_merge(app: Any, work_id: str) -> dict:
    """``pull -> head`` while the release waits only for a person's Merge (#2960)."""
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    if store is None:
        return {}
    try:
        awaiting = store.get(work_id).get("awaiting_merge") or {}
    except Exception:  # noqa: BLE001 -- unreadable: nothing is ready
        return {}
    return awaiting if isinstance(awaiting, dict) else {}


@dataclass(frozen=True)
class _LiveState:
    """The live check of a merged Work (#3141): asked and not answered, or its answer."""

    pending: bool = False
    result: str = ""
    line: str = ""


def _live_state(app: Any, work_id: str) -> _LiveState:
    """Whether the deck asked this Work's agent to check it live, and what it recorded.

    Pending from the ask until a result recorded after it; a result recorded before the ask
    was about an earlier merge. Unreadable is no ask, so the Finish line stands.
    """
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    asked_at = None
    if store is not None:
        try:
            asked = store.get(work_id).get("live_check_asked")
        except Exception:  # noqa: BLE001 -- unreadable: nothing was asked
            asked = None
        if isinstance(asked, dict) and isinstance(asked.get("at"), (int, float)):
            asked_at = datetime.fromtimestamp(float(asked["at"]), UTC)
    control = getattr(app.state, "control_db", None)
    reader = getattr(control, "work_live_check", None)
    check = None
    if reader is not None:
        try:
            check = reader(work_id)
        except Exception:  # noqa: BLE001 -- unreadable: no answer yet
            check = None
    checked_at = _aware(check.checked_at) if check is not None and check.checked_at else None
    if asked_at is not None and (checked_at is None or checked_at < asked_at):
        return _LiveState(pending=True)
    if check is None:
        return _LiveState()
    return _LiveState(result=check.result, line=check.line)


def _waiting_on_tests(app: Any, work_id: str) -> bool:
    """Does the release hold this Work only for a verification nobody ran (#2999)?

    Its review passed and no job is running or failed at its head: a red run is the agent's
    to fix, never to run again.
    """
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    if store is None:
        return False
    try:
        return bool(store.get(work_id).get("needs_verify"))
    except Exception:  # noqa: BLE001 -- unreadable: the full list of endings is asked
        return False


def _ready_review(app: Any, work_id: str, ready: Any) -> tuple[datetime | None, str]:
    """When the pass behind a Ready for merge was recorded, and its review chat (#2992)."""
    if ready is None:
        return None, ""
    store = getattr(getattr(app.state, "work_release", None), "store", None)
    try:
        passes = store.get(work_id).get("passes") or {} if store is not None else {}
        passed = passes.get(f"{ready.repository}#{ready.number}".lower()) or {}
        at = passed.get("at")
        when = datetime.fromtimestamp(float(at), UTC) if at else None
        return when, str(passed.get("chat") or "")
    except Exception:  # noqa: BLE001 -- the line stands without its review
        return None, ""


def _ready_for_merge(app: Any, work_id: str, outputs: Sequence[Any]) -> Any:
    """The open pull request a Ready for merge line names, or ``None``.

    Ready only when the release's answer covers every open, ready pull request of the Work at
    the head the card holds now: a push since then is a new head nobody reviewed yet.
    """
    from ..work_release import same_commit

    awaiting = _awaiting_merge(app, work_id)
    offered = [output for output in outputs if not output.settled and not output.draft]
    if not awaiting or not offered:
        return None
    for output in offered:
        head = awaiting.get(f"{output.repository}#{output.number}".lower())
        if not isinstance(head, str) or not same_commit(output.head, head):
            return None
    return offered[0]


def _card_inputs(
    app: Any,
    snapshot: _Pass,
    ref: WorkRef,
    work: Work | None,
    *,
    jobs: list[dict],
    claims: Any,
    facts: WorkFacts,
    costly: bool = True,
) -> tuple[tuple, tuple]:
    """What ``_card`` reads for one Work: ``(compared by value, compared by identity)``.

    ``costly=False`` leaves out the two reads that cost most of a pass (#3228): the derived
    status's own inputs and the manifest's paused label. Both change only with a topic that
    moves ``_card_gate`` (``work_inputs``, ``reviews``) or a manifest edit, which the gate's
    ``FULL_CHECK_S`` covers.

    The chats come in as their change numbers and their display copies, which the
    presentation reuses while a chat is unchanged (``AppState.session_presentation``), so a
    change to a chat in another Work leaves this tuple as it was.
    """
    state = snapshot.state
    memberships = work.memberships if work is not None else ()
    legs = tuple(
        leg
        for membership in memberships
        for leg in (snapshot.legs.get(membership.chat_id, ()) or (membership.chat_id,))
    )
    owned = tuple(snapshot.owned.get(ref.work_id, ()))
    owned_keys = tuple(getattr(insight, "session_key", "") for insight in owned)
    revision = getattr(state, "session_revision", None)
    can_send = _can_send(app)
    sessions = state.sessions
    presentation = snapshot.presentation
    display = presentation.display if presentation is not None else (lambda session: session)
    by_identity = tuple(
        display(session) if (session := sessions.get(key)) is not None else None
        for key in (*legs, *owned_keys)
    )
    by_value = (
        ref,
        work,
        tuple(tuple(sorted(row.items())) if isinstance(row, dict) else row for row in jobs),
        claims if claims is None else tuple(claims),
        facts,
        tuple(revision(key) for key in (*legs, *owned_keys)) if revision else None,
        tuple(len(snapshot.projection.works_for_chat(m.chat_id)) for m in memberships),
        tuple(can_send(key) for key in legs),
        owned,
        tuple(snapshot.insight_states.get(key) for key in owned_keys),
        tuple(
            quota_retry_message_id(session) in snapshot.pending_message_ids
            if session is not None
            else False
            for key in owned_keys
            for session in (sessions.get(key),)
        ),
        snapshot.titles.get(ref.work_id, ""),
        snapshot.agent_titles.get(ref.work_id, ""),
        start_chat_title(snapshot, ref.work_id),
        snapshot.backlog.issue(ref.repository, ref.number) if snapshot.backlog else None,
        work_project(ref.repository, snapshot.board),
    )
    if costly:
        by_value = (
            *by_value,
            _derived_key(app, snapshot, ref),
            # A manifest's paused label changes a card no other input does (#2956).
            _deck_paused_label(app, ref.repository),
        )
    return by_value, by_identity


# A pass re-reads every Work's inputs at least this often (#3228), even when no change named
# it: the one net under an input that moved without announcing itself. Not a clock: it only
# decides how much of a pass that runs anyway is checked.
FULL_CHECK_S = 60.0


def _card_gate(app: Any, snapshot: _Pass, cheap: tuple[tuple, tuple]) -> tuple:
    """When ``_cached_card`` may skip the costly part of a Work's inputs (#3228).

    ``cheap`` is ``_card_inputs(costly=False)``: everything the card reads but the derived
    status's inputs and the paused label. Those two move only with a topic in
    ``WorkOverviewService.TOPICS`` other than ``sessions`` (a chat write is in ``cheap``
    already, by its change number), so the gate carries those topics' revisions, and a
    ``FULL_CHECK_S`` bucket as the net under a manifest edit. A pass a chat write started
    reads them only for that chat's Works.
    """
    by_value, by_identity = cheap
    bus = getattr(app.state.app_state, "bus", None)
    unscoped = (
        bus.revision(*(t for t in WorkOverviewService.TOPICS if t != "sessions"))
        if bus is not None and hasattr(bus, "revision")
        else None
    )
    # By identity, as the full comparison does: the cache entry holds every one of these
    # objects (its ``by_identity``), so none of their ids can be reused while it is compared.
    return (
        unscoped,
        int(time.monotonic() // FULL_CHECK_S),
        by_value,
        tuple(id(item) for item in (*by_identity, snapshot.board)),
        snapshot.chat_reviews,
    )


def _cached_card(
    app: Any,
    snapshot: _Pass,
    ref: WorkRef,
    work: Work | None,
    *,
    hydrated: bool,
    now: datetime,
    **kwargs: Any,
) -> tuple[WorkCard, tuple[WorkChat, ...]]:
    """``_card``, reusing the last one built for this Work while nothing it reads moved.

    A pass still walks every Work; it only stops rebuilding the cards whose chats, facts
    and jobs are as they were, so one chat changing builds the cards of its own Works
    and no others (ADR 0007). Counted as ``work_counter.WORK_CARD`` per build.
    """
    cache = getattr(app.state, "work_card_cache", None)
    if cache is None:
        cache = app.state.work_card_cache = {}
    key = (ref.work_id, hydrated)
    inputs = {
        "jobs": kwargs["jobs"],
        "claims": kwargs.get("claims"),
        "facts": kwargs["facts"],
    }
    entry = cache.get(key)
    gate = _card_gate(
        app, snapshot, _card_inputs(app, snapshot, ref, work, costly=False, **inputs)
    )
    if (
        entry is not None
        and entry[5] == gate
        and (entry[3] is None or now < entry[3])
    ):
        return entry[4]
    work_counter.count(work_counter.WORK_CARD_CHECK)
    by_value, by_identity = _card_inputs(app, snapshot, ref, work, **inputs)
    # The board by identity: ``read_board`` hands back the same object while its files
    # have not moved, and comparing a whole board per card would cost more than the card.
    by_identity = (*by_identity, snapshot.board)
    shared = snapshot.chat_reviews
    if (
        entry is not None
        and entry[0] == by_value
        and len(entry[1]) == len(by_identity)
        and all(a is b for a, b in zip(entry[1], by_identity, strict=True))
        and entry[2] == shared
        and (entry[3] is None or now < entry[3])
    ):
        cache[key] = (*entry[:5], gate)
        return entry[4]
    work_counter.count(work_counter.WORK_CARD)
    built = _card(app, snapshot, ref, work, **kwargs)
    cache[key] = (
        by_value,
        by_identity,
        shared,
        time_deadline(now, built[0], kwargs["facts"]),
        built,
        gate,
    )
    return built


def _card(
    app: Any,
    snapshot: _Pass,
    ref: WorkRef,
    work: Work | None,
    *,
    jobs: list[dict],
    claims: list[WorkspaceClaim] | None,
    facts: WorkFacts,
    workspace: WorkWorkspace | None = None,
) -> tuple[WorkCard, tuple[WorkChat, ...]]:
    """One Work's card and its chats.

    ``facts`` are the Work's facts from ``gather_facts``, the one pass both pages read, so
    the two cannot disagree about a Work's outputs or finish (#2229)."""
    issue_record = facts.record
    chats = work_chats(
        snapshot.state,
        work,
        snapshot.projection,
        chats=snapshot.chats,
        legs_by_chat=snapshot.legs,
        presentation=snapshot.presentation,
    )
    title, issue_state = work_title(
        ref,
        backlog=snapshot.backlog,
        chats=chats,
        record=issue_record,
        custom=snapshot.titles.get(ref.work_id, ""),
        agent=snapshot.agent_titles.get(ref.work_id, ""),
        chat=start_chat_title(snapshot, ref.work_id),
    )
    backlog_issue = (
        snapshot.backlog.issue(ref.repository, ref.number) if snapshot.backlog else None
    )
    updated = str((issue_record or {}).get("updatedAt") or "") or str(
        getattr(backlog_issue, "updated_at", "") or ""
    )
    issue = work.issue if work is not None else None
    built = build_work_card(
        ref,
        issue=issue,
        title=title,
        issue_state=issue_state,
        facts=facts,
        claims=claims,
        verifications=run_verifications(jobs, ref.work_id),
        workspace=workspace,
        attention=_attention(
            app, snapshot.owned.get(ref.work_id, ()), snapshot.presentation,
            snapshot.insight_states, snapshot.pending_message_ids,
        ),
        chats=chats,
        issue_updated_at=updated,
        project=work_project(issue.repository if issue is not None else ref.repository,
                             snapshot.board),
        chat_reviews=snapshot.chat_reviews,
        jobs=work_jobs(jobs, chats),
        paused_label=_deck_paused_label(app, ref.repository),
    )
    opening = opening_chat(message_target(built, chats, _can_send(app)), chats)
    if opening is not None:
        built = replace(built, chat_href=opening[1], chat_key=opening[0])
    # A Work no agent works on yet has no chat to derive a status from (#3558).
    derived = None if built.start_label else derived_for(app, snapshot, ref, built, jobs)
    if derived is not None:
        built = replace(built, derived=derived)
    return built, chats


# The rescans ``_publish_live_works`` starts, held so none is collected mid-flight.
_LIVE_SCANS: set[asyncio.Task] = set()


def _publish_live_works(
    app: Any,
    snapshot: _Pass,
    facts: Mapping[str, Any],
    candidates: Sequence[Any] = (),
    *,
    now: datetime | None = None,
) -> None:
    """Tell the live window which chats are in Works still going and which in finished
    ones (#2841): the first stay live whatever their age, the second are let go.

    A Work's chats are its members the deck holds plus every chat registered to its
    issue, each with all of its legs, so a chat the window let go is found again once
    its Work is going. A Work whose issue closed but which still waits for a person to
    finish it is still going: its page asks for a Finish or a follow-up in its chat, and
    a chat let go left that page with no chat to open or message (#2990). So is one finished
    with a pull request still open: its release places that pull request through the chat
    that registered it, and a chat let go left a clean review's verdict naming no pull
    request of the Work (#3125).

    A recently finished Work is remembered as this pass built it, with its chats
    (``app.state.let_go_works``), so it stays built after the window lets its chats go. Without
    that the Work left the pass, its chats left ``finished``, the next scan loaded them again
    and the one after let them go: every finished Work of an account flipped in and out of the
    front page and its status log with each scan (#3189). A Work that finished before
    ``RECENT_WORK`` but that a chat carried on in since is shown by that chat's activity
    (``continued_at``), which only a held chat has, so its chats are kept like a Work going.
    """
    state = snapshot.state
    window = getattr(state, "live_window", None)
    if window is None or not window.enabled:
        return
    remembered: dict[str, tuple[Any, frozenset[str]]] = getattr(
        app.state, "let_go_works", None
    ) or {}
    chats = snapshot.chats
    keys: dict[str, set[str]] = {}
    for key, refs in list(state.session_issues.items()):
        for ref in refs:
            keys.setdefault(work_id_for(ref), set()).add(key)
    for work in snapshot.projection.works:
        found = keys.setdefault(work.work_id, set())
        for chat_id in work.chat_ids:
            found.update(snapshot.legs.get(chat_id, ()))
    for work_id, (_, saved) in remembered.items():
        keys.setdefault(work_id, set()).update(saved)
    by_id = {item.ref.work_id: item for item in candidates}
    now = now or datetime.now(UTC)
    pinned: set[str] = set()
    finished: set[str] = set()
    kept: dict[str, tuple[Any, frozenset[str]]] = {}
    for work_id, item in facts.items():
        done = (
            item.finished_at is not None
            and not item.awaiting_finish
            and all(output.settled for output in getattr(item, "outputs", ()))
        )
        candidate = by_id.get(work_id)
        target = finished if done and not _continued(item, candidate, now) else pinned
        chat_keys: set[str] = set()
        for key in keys.get(work_id, ()):
            chat_keys.update(state.chat_session_keys(key, chats))
        target.update(chat_keys)
        if (
            done
            and candidate is not None
            and candidate.work is not None
            and now - item.finished_at <= RECENT_WORK
        ):
            kept[work_id] = (candidate, frozenset(chat_keys))
    app.state.let_go_works = kept
    added = window.set_works(pinned=pinned, finished=finished)
    # A chat newly kept for a Work still going, that the deck does not hold, is listed
    # by its account's next full scan; ask for that scan now rather than in up to five
    # minutes. This is what brings them in after a restart, whose first scan ran before
    # any Work was known. Only on a change, so a chat no scan can find costs one scan.
    missing = {key.rsplit(":", 1)[0] for key in added if key not in state.sessions}
    collector = getattr(app.state, "collector", None)
    scan_now = getattr(collector, "scan_now", None)
    if missing and scan_now is not None:
        for account_key in sorted(missing):
            task = asyncio.create_task(scan_now(account_key), name=f"live-window-{account_key}")
            _LIVE_SCANS.add(task)
            task.add_done_callback(_LIVE_SCANS.discard)


def _registrations(snapshot: _Pass, work: Work | None) -> tuple[Registration, ...]:
    """The pull requests the Work's chats registered — the ones its Work page lists under
    each chat — and whether each chat is in this Work alone."""
    if work is None:
        return ()
    found: dict[str, Registration] = {}
    for membership in work.memberships:
        alone = len(snapshot.projection.works_for_chat(membership.chat_id)) == 1
        for leg in snapshot.legs.get(membership.chat_id, ()) or (membership.chat_id,):
            for pull in snapshot.state.session_pull_requests.get(leg, ()):
                held = found.get(pull.url.lower())
                if held is None or (alone and not held.exclusive):
                    found[pull.url.lower()] = Registration(pull.url, alone)
    return tuple(found.values())


def _cached_merge_checks(app: Any, rows: Mapping[str, Mapping[str, Any]]) -> dict[str, dict]:
    """The cached ``merge_checks`` row of every open pull request among ``rows``, by
    lowercased URL (#2275, #2332). One local read; a pull request not yet checked has no row.
    Only armed ones are ever fetched for their checks, and only Merge candidates for their
    merge answer (``merge_watch_pulls``); a row for anything else is simply absent."""
    issues = getattr(app.state, "issues", None)
    lookup = getattr(issues, "cached_merge_checks", None)
    wanted = {}
    for url, row in rows.items():
        identity = pull_identity(url)
        if identity is not None and pull_state(row) == "open":
            wanted[identity] = url
    if lookup is None or not wanted:
        return {}
    try:
        records = lookup(wanted)
    except Exception:  # noqa: BLE001 -- a check we cannot read is never reported as missing
        logger.debug("merge check cache unreadable for the Work overview", exc_info=True)
        _degraded("merge_checks")
        return {}
    return {wanted[ref]: record for ref, record in records.items() if ref in wanted}


def _staging_sites(app: Any, repositories: Iterable[str]) -> dict[str, Any]:
    """Each repository's ``StagingSite`` from its manifest's ``[staging]`` (#3180), by
    lowercased ``owner/repo``; a repository that deploys into no staging copy is absent."""
    ports = getattr(getattr(app.state, "work_release", None), "ports", None)
    policy = getattr(ports, "policy", None)
    if policy is None:
        return {}
    found = {}
    for repository in {name.lower() for name in repositories if name}:
        try:
            rules = policy(repository)
        except Exception:  # noqa: BLE001 -- an unreadable manifest shows no staging copy
            continue
        site = getattr(rules, "staging", None)
        if site is not None and repository in getattr(rules, "staging_repos", ()):
            found[repository] = site
    return found


def _staging(app: Any, found: dict[str, WorkFacts]) -> dict[str, WorkFacts]:
    """``found`` with each Work's staging copy (#3180): one cache read for the whole pass."""
    service = getattr(app.state, "staging_deploys", None)
    if service is None:
        return found
    sites = _staging_sites(
        app, (output.repository for facts in found.values() for output in facts.outputs)
    )
    if not sites:
        return found
    heads = {
        (output.repository, output.head)
        for facts in found.values()
        for output in facts.outputs
        if output.head and output.state == "open" and output.repository.lower() in sites
    }
    try:
        deployed = service.lookup(heads)
    except Exception:  # noqa: BLE001 -- a copy we cannot read is never called ready
        logger.debug("staging deploys unreadable for the Work overview", exc_info=True)
        _degraded("staging_deploys")
        return found
    for work_id, facts in found.items():
        copy = staging_copy(facts.outputs, sites, deployed)
        if copy is not None:
            found[work_id] = replace(facts, staging=copy)
    return found


def gather_facts(
    app: Any, snapshot: _Pass, candidates: Sequence[Any], *, strict: bool = False
) -> dict[str, WorkFacts]:
    """Every candidate's ``WorkFacts``, from three local cache reads for the whole pass.

    The one place a Work's issue record, open pull requests, chat registrations and cached
    pull request rows are joined (#2229). The front page and the Work page both read
    a Work through this, and it never calls GitHub.

    ``strict`` (the overview's pass) raises when the issue or pull request cache cannot be
    read: without them every closed, merged or paused Work reads as open and stopped, so the
    pass keeps the last overview and retries rather than show, log or report that (#3553).
    """
    records = _cached_issues(app, candidates, strict=strict)
    now = datetime.now(UTC)
    inputs = []
    for item in candidates:
        ref = item.ref
        open_pulls = snapshot.open_pulls.get(f"{ref.repository}#{ref.number}".lower(), ())
        registered = _registrations(snapshot, getattr(item, "work", None))
        inputs.append((ref, open_pulls, registered))
    urls = [url for item in inputs for url in discovery_urls(*item)]
    rows = _cached_pulls(app, urls, strict=strict)
    checks = _cached_merge_checks(app, rows)
    cutoff, signoffs = signoff_inputs(app)
    held = deck_paused_ids(app)
    starred = starred_ids(app)
    unstarted = unstarted_issues(
        snapshot.backlog, now=now, mine=getattr(app.state, "deck_claims", None)
    )
    finished_maps = _finished_maps(app, records, signoffs, cutoff)
    found = {}
    for (ref, open_pulls, registered), item in zip(inputs, candidates, strict=True):
        facts = work_facts(
            ref,
            record=records.get(ref.work_id),
            rows=rows,
            open_pulls=open_pulls,
            registered=registered,
            checks=checks,
        )
        signed = signoffs.get(ref.work_id)
        if awaiting_finish(
            facts.finished_at, signed[1] if signed else None, cutoff
        ) and not _finishes_by_itself(facts, finished_maps):
            facts = replace(facts, awaiting_finish=True)
        if ref.work_id in held:
            facts = replace(facts, deck_paused=True)
        if ref.work_id.casefold() in starred:
            facts = replace(facts, starred=True)
        if ref in unstarted and getattr(item, "work", None) is None:
            # No agent works on it yet (#3558): the label that would start one.
            labels = snapshot.backlog.board.ownership_labels(ref.repository)
            facts = replace(facts, start_label=labels[0])
        found[ref.work_id] = facts
    return _staging(app, found)


def deck_paused_ids(app: Any) -> frozenset[str]:
    """The Works a person paused on this deck (#2559), in one control-DB read."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return frozenset()
    try:
        return frozenset(control.paused_works())
    except Exception:  # noqa: BLE001 -- an unreadable pause store pauses nothing
        logger.debug("deck pauses unreadable for the Work overview", exc_info=True)
        _degraded("deck_paused")
        return frozenset()


def starred_ids(app: Any) -> frozenset[str]:
    """The Works a person starred (#3116), casefolded, in one control-DB read."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return frozenset()
    return frozenset(work_id.casefold() for work_id in control.starred_works())


def reconcile_deck_pauses(app: Any, facts: dict[str, WorkFacts]) -> None:
    """Drop a finished Work's deck pause (#2559): nothing starts on it.

    It once also handed a deck pause to a live Poller card's paused label; the Poller's cards
    went in #3025.
    """
    control = getattr(app.state, "control_db", None)
    if control is None:
        return
    for work_id, item in list(facts.items()):
        if item.deck_paused and item.finished_at is not None:
            control.resume_work(work_id)
            facts[work_id] = replace(item, deck_paused=False)


# The ``work_backfills`` row whose summary holds when this deck began asking a person to finish
# each Work whose issue closed (#2510). Written once; every close before it counts as signed off.
SIGNOFF_CUTOFF = "work_signoff_v1"


def start_signoff_cutoff(control: Any) -> None:
    """Record when this deck began asking a person to finish each closed Work (#2510).

    Called once at startup, so viewing Works never writes. A marker already there is kept: the
    cutoff is the first start of the code that asks, and every close before it is signed off.
    """
    if control is None or not control.keeps_work_backfills():
        return
    if control.work_backfill(SIGNOFF_CUTOFF) is None:
        stamp = datetime.now(UTC).isoformat()
        control.record_work_backfill(SIGNOFF_CUTOFF, json.dumps({"cutoff": stamp}))


def _labels(facts: WorkFacts) -> list:
    record = facts.record
    labels = record.get("labels") if isinstance(record, Mapping) else None
    return labels if isinstance(labels, list) else []


def _finished_maps(
    app: Any,
    records: Mapping[str, Any],
    signoffs: Mapping[str, tuple[datetime, datetime | None]],
    cutoff: datetime | None,
) -> frozenset[str]:
    """The casefolded ids of the wayfinder maps, among the closed records' parents, that a
    person has finished (#3770). A parent the deck builds no Work for right now is read from
    the issue cache all the same, in one more local read, so a child finishes with its map
    on the front page and on its own page alike. A parent with no record finishes nothing."""
    parents = {}
    for record in records.values():
        parent = work_tree.parent_ref(record) if work_finished_at(record) else None
        if parent is not None:
            parents[parent.work_id] = parent
    missing = [
        SimpleNamespace(ref=ref) for work_id, ref in parents.items() if work_id not in records
    ]
    found = {**_cached_issues(app, missing), **records} if missing else records
    signed_by_id = {key.casefold(): value for key, value in signoffs.items()}
    finished = set()
    for work_id in parents:
        record = found.get(work_id)
        labels = record.get("labels") if isinstance(record, Mapping) else None
        closed = work_finished_at(record)
        signed = signed_by_id.get(work_id.casefold())
        if (
            closed is not None
            and wayfinder_map.is_map(labels if isinstance(labels, list) else ())
            and not awaiting_finish(closed, signed[1] if signed else None, cutoff)
        ):
            finished.add(work_id.casefold())
    return frozenset(finished)


def _finishes_by_itself(facts: WorkFacts, finished_maps: Collection[str] = ()) -> bool:
    """A closed wayfinder ticket needs no Finish (#3675): its answer is already on its map,
    and the owner signs the whole map off at its destination. Any other closed sub-issue of a
    map (a build issue filed under it) finishes with the map's own Finish (#3770), so one press
    ends the whole map. One closed with a pull request still open, or one whose state the deck
    has not read yet, waits as any Work does, so that pull request is not forgotten (#3187)."""
    if not all(output.settled for output in facts.outputs):
        return False
    if wayfinder_map.is_ticket(_labels(facts)):
        return True
    parent = work_tree.parent_ref(facts.record)
    return parent is not None and parent.work_id.casefold() in finished_maps


def signoff_inputs(
    app: Any,
) -> tuple[datetime | None, dict[str, tuple[datetime, datetime | None]]]:
    """This deck's sign-off cutoff and every stored sign-off (#2510).

    No control store, or no cutoff on record, means every Work finishes when its issue closes,
    as it did before, rather than all of them waiting at once.
    """
    control = getattr(app.state, "control_db", None)
    if control is None:
        return None, {}
    summary = control.work_backfill(SIGNOFF_CUTOFF)
    cutoff = None
    try:
        value = json.loads(summary)["cutoff"] if summary else None
        cutoff = datetime.fromisoformat(value) if isinstance(value, str) else None
    except (ValueError, KeyError, TypeError):
        logger.warning("work sign-off cutoff unreadable: %r", summary)
        _degraded("signoff_cutoff")
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    return cutoff, control.work_signoffs()


def awaiting_work_ids(app: Any) -> frozenset[str]:
    """Works the deck saw close and that no person has finished since (#2510)."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return frozenset()
    return frozenset(
        work_id
        for work_id, (closed, finished) in control.work_signoffs().items()
        if finished is None or finished < closed - FINISH_SLACK
    )


def _note_awaiting(app: Any, facts: Mapping[str, WorkFacts]) -> None:
    """Store every Work this pass found waiting for Finish, so it stays a candidate (#2510)."""
    control = getattr(app.state, "control_db", None)
    if control is None:
        return
    waiting = [
        (work_id, item.finished_at)
        for work_id, item in facts.items()
        if item.awaiting_finish and item.finished_at is not None
    ]
    if not waiting:
        return
    stored = control.work_signoffs()
    for work_id, closed in waiting:
        known = stored.get(work_id)
        if known is None or known[0] < closed:  # written once per close, not every pass
            control.note_work_closed(work_id, closed)


# --- the dashboard --------------------------------------------------------------------------


def _hidden_works(app: Any, cards: dict[str, WorkCard]) -> dict[str, HiddenWork]:
    """The hides that still hold for these Works, expiring the ones that no longer do (#2238).

    A hide ends when the Work's issue has a newer comment, its Poller card moved, or a chat
    carried on in the finished Work after it was archived (``work_hidden.still_hidden``);
    the row is deleted here, so the Work is classified normally from this pass on. A hide
    taken while the comment count was not known yet
    records the first count seen, and a lower count (a deleted comment) becomes the new
    baseline, so the next new comment still ends the hide. A hide for a Work not among
    ``cards`` is left as it is.
    """
    control = getattr(app.state, "control_db", None)
    if control is None:
        return {}
    kept: dict[str, HiddenWork] = {}
    for work_id, hidden in control.hidden_works().items():
        card = cards.get(work_id)
        if card is None:
            continue
        continued = continued_at(card.finished_at, card.last_activity)
        if not still_hidden(hidden, card.comments, continued):
            logger.info("hidden Work %s came back: its issue, card or chat moved", work_id)
            control.unhide_work(work_id)
            continue
        seen = card.comments
        if seen is not None and (hidden.comments is None or seen < hidden.comments):
            # A deleted comment lowers the baseline, so the next new one still counts.
            control.note_hidden_comments(work_id, card.comments)
        kept[work_id] = hidden
    return kept

def _mark_stops(
    app: Any,
    rows: Sequence[FrontRow],
    now: datetime,
    changed_since: datetime | None = None,
) -> list[FrontRow]:
    """Each Needs you row with when it stopped and whether it is unseen (#2283).

    Records the pass's classification first (``work_stops.track_stops``): a Work that entered
    a category gets a stop dated now, one that left keeps a row with no category. On the
    first pass, the Works already waiting are counted as seen, so turning this on does not
    mark every old question new; that pass is recorded even when nothing waits, so a later
    stop is always new.

    ``changed_since`` is when the first change since the previous pass arrived. The overview
    is not rebuilt while nobody watches (#2865), so a Work that stopped in that gap is dated
    by its own last activity inside the gap, not by when a page next connected. While a page
    watches, the gap is one debounce and the date is as before.
    """
    control = getattr(app.state, "control_db", None)
    if control is None:
        return list(rows)
    present = {
        row.card.work_id: (
            (row.attention.category if row.attention is not None else NEEDS_YOU)
            if row.section == NEEDS_YOU
            else ""
        )
        for row in rows
    }
    previous = control.work_stops()
    bootstrap = control.work_stops_started() is None
    changed = track_stops(
        previous,
        present,
        now=now,
        seed={row.card.work_id: row.card.last_activity for row in rows},
        bootstrap=bootstrap,
        marks={row.card.work_id: facts_mark(row.card) for row in rows},
        changed_since=changed_since,
    )
    control.save_work_stops(changed.values(), forget_before=now - FORGET_AFTER)
    if bootstrap:
        seen = [work_id for work_id, stop in changed.items() if stop.category]
        if seen:
            control.note_work_opened(seen, now)
        control.start_work_stops(now)
    stops = {**previous, **changed}
    opened = control.works_opened()
    marked = []
    for row in rows:
        stop = stops.get(row.card.work_id)
        if row.section == NEEDS_YOU and stop is not None and stop.category:
            row = replace(
                row,
                stopped_at=stop.stopped_at,
                unseen=is_unseen(stop, opened.get(row.card.work_id)),
            )
        marked.append(row)
    return marked


# How long a build waits for a starting process's first scans before it gives up this time.
SCAN_WAIT_S = 60.0


async def sessions_scanned(app: Any) -> bool:
    """Wait until every account has had its first scan, so the chats Works are found by are
    loaded; False when that did not happen within ``SCAN_WAIT_S``.

    A Work with no Poller card or current fact is built from its chats (``work_candidates``),
    so a pass before then leaves out every finished Work whose chats are not scanned yet:
    each web restart dropped hundreds from Finished for a few seconds (#2760).
    """
    wait = getattr(getattr(app.state, "collector", None), "wait_initial_scans", None)
    return wait is None or bool(await wait(SCAN_WAIT_S))


def _log_status(
    app: Any,
    rows: Sequence[FrontRow],
    now: datetime,
    *,
    hidden: frozenset[str],
    degraded: Sequence[str],
) -> list[StatusPoint]:
    """Append every Work whose front-page status this pass changed to the status log (#2751).

    The pass compares with the newest status this process logged for each Work, read from the
    log itself the first time, so a restart does not log every Work again. A Work the pass no
    longer builds is logged once as gone.
    """
    control = getattr(app.state, "control_db", None)
    if control is None:
        return []
    last: dict[str, StatusPoint] | None = getattr(app.state, "work_status_points", None)
    if last is None:
        last = control.latest_work_status_points()
        app.state.work_status_points = last
    found = changes(
        last,
        rows,
        now=now,
        hidden=hidden,
        degraded=degraded,
        pid=os.getpid(),
        revision=getattr(app.state, "revision", None),
    )
    if found:
        control.append_work_status_log(found)
        for item in found:
            last[item.work_id] = item
    return found


def _report_bugs(
    app: Any, rows: Sequence[FrontRow], board: Any, now: datetime, degraded: Sequence[str]
) -> None:
    """Hand the Works that stopped on an AgentDeck bug to its Poller (#2888)."""
    sent = getattr(app.state, "work_bugs_sent", None)
    if sent is None:
        sent = app.state.work_bugs_sent = set()
    entered = getattr(app.state, "work_retry_since", None)
    if entered is None:
        entered = app.state.work_retry_since = {}
    try:
        work_bugs.report(
            rows, board, now=now, degraded=degraded,
            revision=getattr(app.state, "revision", None), sent=sent,
            since=work_bugs.track_entries(
                getattr(app.state, "work_status_points", None), entered
            ),
        )
    except Exception:  # noqa: BLE001 - a report must never cost the overview its pass
        logger.exception("work bug reports failed")


@dataclass(frozen=True)
class WorkOverview:
    cards: tuple[WorkCard, ...] = ()
    hidden: int = 0  # unused since #2219: sections bound themselves (``FrontSections``)
    archived: int = 0  # Works on the Archived Works view (#2201)
    other: tuple[OtherChat, ...] = ()
    other_hidden: int = 0
    # Issues another deck works, listed read only in their own folded group (#3834).
    other_decks: tuple[OtherDeckWork, ...] = ()
    # The front page's sections (#2219): every card above in exactly one of them.
    front: FrontSections = FrontSections()
    # Issues whose cached record cannot say whether or when the Work finished, newest
    # activity first: the service re-reads a few per minute (#2226). Not part of equality,
    # so the list alone never republishes the cards.
    unresolved: tuple[tuple[str, int], ...] = field(default=(), compare=False)
    # Listed Works' pull requests whose cached row predates the size fields, Needs you first:
    # the same backfill re-reads them, so the row can show ``+a/−d · N files`` (#2236).
    unsized: tuple[tuple[str, int, str], ...] = field(default=(), compare=False)
    # Listed Works' open pull requests, each with its head: the same backfill re-reads one
    # whose cached row has not been rewritten for a while, so a merge no webhook delivered
    # still settles it (#3217).
    open_rows: tuple[tuple[str, int, str], ...] = field(default=(), compare=False)
    # Listed Works' open pull requests with auto-merge on: the service keeps their required
    # checks read, so one whose check never ran leaves Working now (#2275).
    merge_watch: tuple[tuple[str, int], ...] = field(default=(), compare=False)
    # Every chat some Work owns (#2256): the chat list leaves these out. Compared, so a chat
    # joining a Work republishes and the list drops it without waiting for a session change.
    work_chat_ids: frozenset[str] = frozenset()
    # The grouping the cards were built from, so a chat page loaded on its own can name the
    # Work it belongs to without building one (#2346). Not part of equality.
    projection: Any = field(default=None, compare=False, repr=False)
    # The reads this pass failed, so the refresher asks no Work for a next step from it
    # (#3031). Not part of equality.
    degraded: tuple[str, ...] = field(default=(), compare=False)


def owned_chat_ids(projection: Any) -> frozenset[str]:
    """The chats any Work in ``projection`` counts as a member."""
    return frozenset(chat_id for work in projection.works for chat_id in work.chat_ids)


def work_chat_ids(app: Any) -> frozenset[str]:
    """Chats some Work owns, for the chat list to leave out (#2256).

    Read from the refresher's snapshot, so a list render builds no projection of its own;
    only before the first snapshot exists is the projection built here.
    """
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    if overview is not None:
        return overview.work_chat_ids
    return owned_chat_ids(app.state.app_state.work_projection())


def chat_work_id(
    app: Any, chat_id: str, *, session_key: str = "", wanted: str = ""
) -> str | None:
    """The one Work that owns ``chat_id``, or None (#2346).

    A chat page loaded on its own puts this Work beside it, the way opening the chat from
    that Work does, and links up to it (#2512). A chat several Works share names the one it
    was opened from (``wanted``, from its address), else the one its latest Run was for,
    and only one it is still part of (#2567). With neither it names none — the same rule
    the cards use for a shared chat's problems.
    """
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    projection = getattr(overview, "projection", None)
    if projection is None:
        projection = app.state.app_state.work_projection()
    works = {ref.work_id for ref in projection.works_for_chat(chat_id)}
    named = WorkRef.from_work_id(wanted.strip())
    if named is not None and named.work_id in works:
        return named.work_id
    if len(works) == 1:
        return next(iter(works))
    latest_run = getattr(getattr(app.state.app_state, "db", None), "latest_session_run", None)
    binding = latest_run(session_key) if session_key and latest_run is not None else None
    if binding is not None and binding.work_id in works:
        return binding.work_id
    return None


def chat_work_actions(
    app: Any, work_id: str | None, chat_paths: Sequence[str] = ()
) -> tuple[WorkCard | None, tuple[ActionButton, ...]]:
    """The chat's Work and the buttons its Work page offers now, for the chat page (#2972).

    Read once from the refresher's snapshot when the page loads, so it builds nothing. Inspect
    is left out (the chat's header already links to the Work), and so is a link that only
    opens this same chat, named by ``chat_paths``. Answer links to the Work page's message box.
    """
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    if work_id is None or overview is None:
        return None, ()
    card = next((c for c in overview.cards if c.work_id == work_id), None)
    if card is None:
        return None, ()
    hidden = any(row.card.work_id == work_id for row in overview.front.hidden)
    status = work_status(card)
    attention = operator_attention(card, waiting_story(card, status))
    found = [
        button
        for button in buttons(card, attention, status.where_href, hidden=hidden)
        if button.action != "inspect"
        and not (button.href and button.href.split("?", 1)[0] in chat_paths)
    ]
    if hidden:
        found.append(BRING_BACK)
    pause = pause_control(card)
    if pause is not None:
        found.append(pause)
    return card, tuple(found)


def paused_hold(app: Any, session_key: str) -> str | None:
    """Why the deck starts no turn in this chat now, or None (#2559).

    A person paused a Work the chat belongs to, by its Poller's label or on this deck. Read
    from the refresher's snapshot, which a Pause or Resume press rebuilds at once, so this
    costs a send nothing but a set lookup.
    """
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    if overview is None:
        return None
    paused = {card.work_id: card for card in overview.cards if card.paused}
    projection = getattr(overview, "projection", None)
    if not paused or projection is None:
        return None
    chat_id = app.state.app_state.chat_identity_key(session_key)
    for work in projection.works_for_chat(chat_id):
        card = paused.get(work.work_id)
        if card is not None:
            return (
                f"{card.source_label} is paused, so the deck starts nothing in its chats;"
                " resume it on its Work page first"
            )
    return None


def note_work_opened(app: Any, work_id: str) -> None:
    """The operator looked at this Work: it is no longer new on the front page (#2283).

    Opening its page counts, and so does opening the chat it opens into, which a phone's
    front-page row does instead of the page (#2512).

    The front page is rebuilt at once only when it shows this Work as new. Every open used
    to rebuild it, and that rebuild shares the one event loop the Work page itself is
    served from, so opening Works in turn slowed each other down (#2422). A Work that
    became new since the last build loses its mark at the next regular one.
    """
    control = getattr(app.state, "control_db", None)
    if control is None or not control.note_work_opened([work_id], datetime.now(UTC)):
        return
    service = getattr(app.state, "work_overview", None)
    if service is None or not hasattr(service, "refresh_soon"):
        return
    overview = getattr(service, "overview", None)
    if overview is not None and not _shown_as_new(overview, work_id):
        return
    service.refresh_soon()


def _shown_as_new(overview: WorkOverview, work_id: str) -> bool:
    front = overview.front
    return any(
        row.unseen and row.card.work_id == work_id
        for row in (*front.needs_you, *front.needs_you_works, *front.stopped, *front.hidden)
    )


# How many Works the top bar's list shows before "+N more" (#3239).
NEEDS_YOU_MENU_LIMIT = 8
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class NeedsYouMenu:
    """The top bar's list (#3239): Needs you Works stopped since their page was last opened.

    The same rows as the front page's unseen marks, so the icon and the front page agree;
    a hidden Work (#2238) is not in Needs you and so not here either. A Work whose issue
    closed and only waits for a Finish is left out, here and on the phone (#3368): nothing is
    urgent about it, and the front page still lists it.
    """

    rows: tuple[FrontRow, ...] = ()
    new: int = 0  # every new Work, also the ones past ``NEEDS_YOU_MENU_LIMIT``
    total: int = 0  # everything in Needs you, for the footer link
    ids: tuple[str, ...] = ()  # every new Work's id, for "Mark all read" (#3246)

    @property
    def more(self) -> int:
        return self.new - len(self.rows)

    @property
    def signature(self) -> tuple:
        """What the list shows: a push is due only when this moved."""
        return (
            self.new,
            self.total,
            self.ids,
            tuple(
                (
                    row.card.work_id,
                    row.card.title,
                    row.attention.category if row.attention is not None else "",
                    row.stopped_at,
                )
                for row in self.rows
            ),
        )


def needs_you_menu(overview: WorkOverview | None, seen: Iterable[str] = ()) -> NeedsYouMenu:
    """The new Needs you Works, the most recent stop first (#3239).

    ``seen`` are Works just marked read from the list itself (#3246): the overview still
    shows them as new until its rebuild, so the reply to that press leaves them out.
    """
    if overview is None:
        return NeedsYouMenu()
    new = _new_needs_you(overview, set(seen))
    return NeedsYouMenu(
        tuple(new[:NEEDS_YOU_MENU_LIMIT]),
        len(new),
        len(overview.front.needs_you_works),
        tuple(row.card.work_id for row in new),
    )


def _new_needs_you(overview: WorkOverview, skip: set[str]) -> list[FrontRow]:
    return sorted(
        (
            row for row in overview.front.needs_you_works
            if row.unseen and row.card.work_id not in skip and not _awaits_finish(row)
        ),
        key=lambda row: row.stopped_at or _EPOCH,
        reverse=True,
    )


def _awaits_finish(row: FrontRow) -> bool:
    return row.attention is not None and row.attention.category == ISSUE_CLOSED


# How many Works one pass pushes one by one; the rest go in one summary (#3348).
NEEDS_YOU_PUSH_LIMIT = 3


@dataclass(frozen=True)
class NeedsYouPush:
    """One phone notification: the top bar's row for a Work, or the summary past the limit."""

    title: str
    body: str
    url: str
    tag: str
    works: tuple[str, ...] = ()  # the Works it names: cleared once all left the list (#3365)


def needs_you_pushes(
    overview: WorkOverview,
    sent: set[tuple[str, str]] | None,
    *,
    muted: Callable[[str], bool] = lambda key: False,
) -> tuple[list[NeedsYouPush], set[tuple[str, str]]]:
    """The notifications owed for the top bar's list, and the stops to remember as told (#3348).

    A stop is a Work and when it entered its category: a Work new in the list since ``sent``
    is pushed once, and pushed again only when it stops anew. ``sent`` None is the first pass
    ever, which records the list unsent rather than buzz about every Work already in it. A
    Work whose chat holds a mute lease is remembered without a push. A degraded overview may
    have dropped Works for a pass, so it forgets none: one coming back is not news.
    """
    rows = _new_needs_you(overview, set())
    stops = {
        (row.card.work_id, row.stopped_at.isoformat() if row.stopped_at else ""): row
        for row in rows
    }
    kept = set(stops)
    if overview.degraded and sent is not None:
        kept |= sent
    if sent is None:
        return [], kept
    fresh = [
        row for stop, row in stops.items()
        if stop not in sent and not (row.card.chat_key and muted(row.card.chat_key))
    ]
    maps = wayfinder_maps(overview.front)
    pushes = [_needs_you_push(row, maps) for row in fresh[:NEEDS_YOU_PUSH_LIMIT]]
    if more := len(fresh) - len(pushes):
        title = "1 more Work needs you" if more == 1 else f"{more} more Works need you"
        rest = tuple(row.card.work_id for row in fresh[NEEDS_YOU_PUSH_LIMIT:])
        pushes.append(NeedsYouPush(title, "", "/", NEEDS_YOU_SUMMARY_TAG, rest))
    return pushes, kept


NEEDS_YOU_SUMMARY_TAG = "needs-you"


def _needs_you_push(row: FrontRow, maps: Sequence[wayfinder_map.Progress]) -> NeedsYouPush:
    """One Work's push. A wayfinder ticket's question names its map and the question, and
    opens the ticket's chat at its form (#3696); every other stop says its category."""
    card = row.card
    plan = map_for(maps, card.work_id)
    ticket = plan is not None and plan.work_id != card.work_id
    asked = wayfinder_map.question(row) if ticket else None
    if plan is not None and asked is not None:
        return NeedsYouPush(
            f"{plan.title} asks", asked.line, asked.href, f"work:{card.work_id}", (card.work_id,)
        )
    return NeedsYouPush(
        card.title,
        f"{row.attention.label if row.attention else 'Needs you'} · {row.source}",
        card.chat_href or card.href,
        f"work:{card.work_id}",
        (card.work_id,),
    )


def wayfinder_maps(front: Any) -> tuple[wayfinder_map.Progress, ...]:
    """Every wayfinder map's progress the front page holds (#3696), from its folded rows."""
    if front is None:
        return ()
    found: dict[str, wayfinder_map.Progress] = {}
    for row in (
        *front.needs_you, *front.hidden, *front.active, *front.waiting, *front.live,
        *front.finished, *front.paused,
    ):
        plan = getattr(row, "map", None)
        if plan is not None:
            found.setdefault(plan.work_id, plan)
    return tuple(found.values())


def map_for(
    maps: Sequence[wayfinder_map.Progress], work_id: str
) -> wayfinder_map.Progress | None:
    """The map a Work is, or is a ticket of, or None."""
    return next((plan for plan in maps if plan.holds(work_id)), None)


@dataclass(frozen=True)
class MapStrip:
    """What a wayfinder ticket's chat page shows of its map (#3696): the map, and the
    question Submit leads on to, None when this was the last one."""

    map: wayfinder_map.Progress
    next: wayfinder_map.Question | None


def chat_map_strip(app: Any, work_id: str | None) -> MapStrip | None:
    """The chat page's map strip for a ticket's chat, from the refresher's last overview; None
    for a chat whose Work is not a map's ticket, or before the first overview."""
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    if work_id is None or overview is None:
        return None
    plan = map_for(wayfinder_maps(getattr(overview, "front", None)), work_id)
    if plan is None or plan.work_id == work_id:
        return None
    return MapStrip(plan, plan.next_after(work_id))


def owner_in_chat(state: Any, request: WakeRequest) -> bool:
    """Whether the owner is in the chat a wait wakes (#3464): a page of it is open.

    The chat page holds its chat's topics only while shown, or while hidden in voice mode:
    ``base.html`` closes its streams once the page is hidden otherwise.
    """
    key = request.session_key
    keys = {request.chat_id, key, state.chat_identity_key(key)}
    return any(state.bus.subscribers(chat_topic(each)) for each in keys)


def needs_you_stops(
    overview: WorkOverview, identity: Callable[[str], str] = lambda key: key
) -> list[NeedsYouStop]:
    """Every Work in Needs you, as a needs-you wake-on tells it (#3446).

    All of Needs you, not only the top bar's unseen rows: a chat relaying what needs the owner
    must hear of a Work the owner happened to open already. A row with no recorded stop has no
    time it entered, so it never counts as new. The reason is the chat's own question when one
    asks (a single waiting question leaves the row's words empty), and ``identity`` turns each
    of the Work's chat legs into the durable chat a registration is keyed by.
    """
    stops = []
    for row in overview.front.needs_you_works:
        if row.attention is None:
            continue
        card = row.card
        waiting = waiting_attention(card)
        asked = next((item.question for item in waiting if item.asks and item.question), "")
        legs = {card.chat_key or "", *(item.session_key for item in card.attention)} - {""}
        stops.append(
            NeedsYouStop(
                work_id=card.work_id,
                category=row.attention.category,
                label=row.attention.label,
                title=card.title,
                issue=card.source_label,
                issue_url=card.source_url,
                page=card.href,
                reason=asked or card.question or row.attention.reason or row.reason,
                stopped_at=row.stopped_at.timestamp() if row.stopped_at is not None else 0.0,
                chats=tuple(sorted({identity(leg) for leg in legs})),
            )
        )
    return stops


def needs_you_clears(
    overview: WorkOverview, showing: dict[str, set[str]], sent: Iterable[NeedsYouPush] = ()
) -> tuple[list[str], dict[str, set[str]]]:
    """The notifications to clear on every device, and the ones still showing (#3365).

    ``showing`` is what earlier passes sent, by tag, plus this pass's ``sent``: a newer push
    replaces its tag's notification, and a summary also keeps naming the Works it named
    before. A tag none of whose Works is new in the top bar's list any more — opened or
    marked read on any device, or out of Needs you — is cleared once and forgotten. Only a
    notification really sent is ever cleared: a push that closes nothing can make a phone
    show its own notice. A degraded pass may have dropped Works, so it clears none.
    """
    showing = {tag: set(works) for tag, works in showing.items()}
    for item in sent:
        kept = showing.get(item.tag, set()) if item.tag == NEEDS_YOU_SUMMARY_TAG else set()
        showing[item.tag] = kept | set(item.works)
    if overview.degraded:
        return [], showing
    new = {row.card.work_id for row in _new_needs_you(overview, set())}
    clears = sorted(tag for tag, works in showing.items() if not works & new)
    return clears, {tag: works for tag, works in showing.items() if tag not in clears}


# What ``population_rows`` calls a section; a Needs you row's bucket is its category instead.
POPULATION_SECTIONS = {
    "needs_you": "Needs you",
    "hidden": "Hidden",
    "active": "Active",
    "waiting": "Waiting",
    "live": "Live checking",
    "finished": "Recently finished",
    "paused": "Paused",
    "folded": "Under its parent",
    "unlisted": "Not listed",
}


def needs_you_rows(overview: WorkOverview, repo: str | None = None) -> list[dict[str, Any]]:
    """The front page's Needs you section as short rows to answer "do you need me?" from
    (#3773): one call for the deck chat, in the page's order, hidden Works left out.

    ``question`` is the card's own short line on a ``Needs answer`` row, never the whole
    question box. ``repo`` keeps one repository, compared case-insensitively.

    A parent row folded over a leading child (#3622) shows the child's attention, so the
    row names the child (``item.actor``, the Work its controls act on) and its parent as
    ``under``: a follow-up read of ``ref``'s question box then reads the right Work.
    """
    wanted = (repo or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for item in overview.front.needs_you:
        actor = item.actor
        ref = WorkRef.from_work_id(actor.work_id)
        if wanted and (ref is None or ref.repository != wanted):
            continue
        op = item.attention
        row = {
            "ref": ref.source_key if ref is not None else actor.work_id,
            "title": actor.title,
            "waits_for": op.label if op is not None else item.status.headline,
        }
        if actor.work_id != item.card.work_id:
            parent = WorkRef.from_work_id(item.card.work_id)
            row["under"] = parent.source_key if parent is not None else item.card.work_id
        if op is not None and op.category == NEEDS_ANSWER and op.reason:
            row["question"] = op.reason
        rows.append(row)
    return rows


def population_rows(overview: WorkOverview) -> list[dict[str, Any]]:
    """One row per Work in an overview already built, as the front page shows it (#2250).

    Nothing is computed here: the section, the operator's category and actions, and the
    reason line are the ones the front-page row renders (``partials/work_front.html``). A
    Work the overview holds but no section lists (past a section's limit, or quiet) is a
    ``Not listed`` row, so a Work leaving the front page still shows in a comparison.
    """
    rows: list[dict[str, Any]] = []
    listed: set[str] = set()
    front = overview.front
    for section, items in (
        ("needs_you", front.needs_you),
        ("hidden", front.hidden),
        ("active", front.active),
        ("waiting", front.waiting),
        ("live", front.live),
        ("finished", front.finished),
        ("paused", front.paused),
    ):
        for item in items:
            op = item.attention
            listed.add(item.card.work_id)
            rows.append(
                {
                    "work_id": item.card.work_id,
                    "title": item.card.title,
                    "section": section,
                    "bucket": (
                        op.label if op is not None and section == "needs_you"
                        else POPULATION_SECTIONS[section]
                    ),
                    "attention": op.category if op is not None else "",
                    "headline": op.label if op is not None else item.status.headline,
                    "reason": op.reason if op is not None else item.reason,
                    "line": item.line,
                    "actions": list(op.actions) if op is not None else [],
                }
            )
            # A sub-issue's Work folded into this parent row (#3622): listed under it.
            for child in item.children:
                if child.card.work_id in listed:
                    continue
                listed.add(child.card.work_id)
                rows.append(
                    {
                        "work_id": child.card.work_id,
                        "title": child.card.title,
                        "section": "folded",
                        "bucket": f"{POPULATION_SECTIONS['folded']} {item.card.work_id}",
                        "attention": (
                            child.attention.category if child.attention is not None else ""
                        ),
                        "headline": child.status.headline,
                        "reason": child.reason,
                        "line": child.line,
                        "actions": [],
                    }
                )
    for card in overview.cards:
        if card.work_id in listed:
            continue
        listed.add(card.work_id)
        rows.append(
            {
                "work_id": card.work_id,
                "title": card.title,
                "section": "unlisted",
                "bucket": POPULATION_SECTIONS["unlisted"],
                "attention": "",
                "headline": "",
                "reason": "",
                "line": "",
                "actions": [],
            }
        )
    return sorted(rows, key=lambda row: row["work_id"])


def unsized_pulls(cards: Sequence[WorkCard]) -> tuple[tuple[str, int, str], ...]:
    """Pull requests the deck holds a row for that does not carry the size yet (#2236), each
    with its head: a push drops the row's file list (#2331), and the backfill's once-an-hour
    retry is per head, so the next push is read again rather than waiting out the hour."""
    found = (
        (output.repository, output.number, output.head)
        for card in cards
        for output in card.outputs
        if output.state and output.repository and not output.file_list
    )
    return tuple(dict.fromkeys(found))


def open_pull_rows(cards: Sequence[WorkCard]) -> tuple[tuple[str, int, str], ...]:
    """Listed Works' open pull requests, each with its head (#3217).

    Nothing else reads a Work's pull request again once its chat went quiet: a merge whose
    webhook never arrived (the repository's hook came later, a delivery was dropped) left the
    row open for good, and a finished Work with an open pull request reads its chat's next
    ending as one without a next step.
    """
    found = (
        (output.repository, output.number, output.head)
        for card in cards
        for output in card.outputs
        if output.is_open and output.repository
    )
    return tuple(dict.fromkeys(found))


def merge_watch_pulls(cards: Sequence[WorkCard]) -> tuple[tuple[str, int], ...]:
    """Open pull requests whose ``merge_checks`` row is worth reading: those with auto-merge
    on, for their required checks (#2275), and those a person may be offered Merge on — reviewed
    clean at their head, or at a head a review passed — for GitHub's merge answer (#2332)."""
    found = (
        (output.repository, output.number)
        for card in cards
        for output in card.outputs
        if output.repository and not output.settled and _merge_watched(card, output)
    )
    return tuple(dict.fromkeys(found))


def _merge_watched(card: WorkCard, output: Any) -> bool:
    if output.auto_merge:
        return True
    # A Work the deck runs itself is offered Merge by its own release (#3199): without this
    # none of its pull requests ever had a row.
    from ..work_derived import READY

    return getattr(card.derived, "kind", "") == READY and bool(output.head)


def unresolved_issues(
    candidates: Sequence[Any], records: dict[str, dict], backlog: Any
) -> tuple[tuple[str, int], ...]:
    """Works whose issue record is closed without ``closedAt``, or missing for a Work no
    cached open-issue list vouches for (#2226)."""
    found = []
    for item in candidates:
        record = records.get(item.ref.work_id)
        if record is not None:
            if record.get("is_pr") or record.get("state") != "closed" or record.get("closedAt"):
                continue
        elif backlog is not None and backlog.issue(item.ref.repository, item.ref.number):
            continue
        found.append((item.ref.repository, item.ref.number))
    return tuple(found)


# The derived kinds of a deck-run Work whose chat has ended and nothing else is under way.
_ENDED_KINDS = frozenset({"ended", "nudged", "unfinished", "review_queued", "review_stuck"})
# The kinds a Work properly rests in (#2960): one of them clears the deck's request for a next
# step, so the next ending without one is asked about afresh. A running, stalled or died turn
# clears nothing: the answer to the request is still under way.
_RESTING_KINDS = frozenset(
    {
        "question", "waiting", "queued", "review", "ready", "closed", "finished", "scheduled",
        "pr_held", "pr_stuck", "live_failed", "not_live", "review_escalated", "spec_wait",
        "parked",
    }
)


async def start_quiet_reviews(app: Any, cards: Sequence[WorkCard]) -> int:
    """Start a reviewer once on each review chat no reviewer ran on (#2960, part 2).

    The Agent review button, pressed by the deck: every ready head is reviewed by itself, so a
    quiet chat is not the owner's to notice. Once per chat; if that reviewer also leaves it
    quiet, the Work reads Needs retry and is reported as a bug. Returns how many started.

    At most one reviewer starts per pass, so the pass's own starts never refuse each other on
    "a new session is already starting" (#2967); the rest start on later passes.
    """
    from ..review_fallback import busy_refusal

    endings = getattr(app.state, "work_endings", None)
    fallback = getattr(app.state, "review_fallback", None)
    board = chat_board_service(app.state)
    if endings is None or fallback is None or board is None:
        return 0
    for card in cards:
        derived = getattr(card, "derived", None)
        chat = getattr(derived, "review_chat", None)
        if derived is None or derived.kind != "review_queued" or chat is None or card.paused:
            continue
        if endings.review_started(chat):
            continue
        row = board.board.rows.get(chat)
        if row is None:
            continue
        # Written first: a crash between the two loses the retry, never doubles it.
        endings.record_review(chat)
        try:
            # The cap holds: the deck's retry is not a person's press.
            ok, note = await fallback.start_on(row, by_person=False)
        except Exception:  # noqa: BLE001 -- the Work then reads Needs retry, as a bug
            # It may have launched before failing, so the retry stays spent and nothing else
            # starts this pass.
            logger.warning(
                "work %s quiet review #%s: start failed", card.work_id, chat, exc_info=True
            )
            break
        if not ok and busy_refusal(note):
            # Only waiting for room, or for another start of that account to finish (#2964):
            # the next pass asks again, and the one retry is kept for a review that is really
            # quiet.
            endings.forget_review(chat)
            logger.info("work %s quiet review #%s: not yet (%s)", card.work_id, chat, note)
            continue
        if not ok:
            logger.info("work %s quiet review #%s: not started (%s)", card.work_id, chat, note)
            continue
        logger.info("work %s quiet review #%s: started a %s reviewer", card.work_id, chat, note)
        return 1
    return 0


# How many failed reads of an ended Work's issue hold it from being asked (#3144 review).
ENDING_REREAD_TRIES = 3
ENDING_REREAD_RETRY_S = 60.0


async def reread_ended_issues(
    app: Any, cards: Sequence[WorkCard], *, degraded: Sequence[str] = ()
) -> frozenset[str]:
    """Re-read the issue of each Work whose turn ended without a next step, once per ending
    (#3143); the Works read, which this pass must not ask or report.

    An errand Work produces no pull request, so nothing else re-reads it (#2947), and an agent
    that closed its issue as its last act reads as ended with a dropped ``closed`` delivery:
    it was asked for a next step, said the issue was closed, and was reported as a bug. One
    forced read per turn end; the next pass, which the publish starts, sees the close. A read
    that fails or reads nothing holds the Work too and is tried again on a later pass, up to
    ``ENDING_REREAD_TRIES`` times, so a GitHub outage cannot hold it for good (#3144 review).
    """
    refresh = getattr(getattr(app.state, "issues", None), "refresh_issue_records", None)
    if degraded or refresh is None:
        return frozenset()
    done = getattr(app.state, "work_ending_rereads", None)
    if done is None:
        done = app.state.work_ending_rereads = {}
    read: set[str] = set()
    for card in cards:
        derived = getattr(card, "derived", None)
        if derived is None or derived.at is None or card.work_state == "done":
            continue
        if derived.kind != "ended" and not (derived.kind == "unfinished" and derived.bug):
            continue
        ref = WorkRef.from_work_id(card.work_id)
        if ref is None:
            continue
        at, failed = done.get(card.work_id, (None, 0))
        if at == derived.at and failed < 0:
            continue  # read for this turn end
        failed = failed if at == derived.at else 0
        if failed >= ENDING_REREAD_TRIES:
            continue  # asked from the cache as it is
        try:
            refreshed = await refresh([(ref.repository, ref.number)], limit=1)
        except Exception:  # noqa: BLE001 -- held, and tried again on a later pass
            logger.warning("work %s issue re-read failed", card.work_id, exc_info=True)
            refreshed = 0
        # Nothing read (no ``gh``, a failed fetch, another process's lease) is a failure too.
        done[card.work_id] = (derived.at, -1 if refreshed else failed + 1)
        read.add(card.work_id)
    if read:
        app.state.app_state.bus.publish(WORK_INPUTS_TOPIC)
    return frozenset(read)


def ask_for_next_steps(
    app: Any,
    cards: Sequence[WorkCard],
    *,
    degraded: Sequence[str] = (),
    held: Collection[str] = (),
) -> int:
    """Ask each deck-run Work whose turn ended without a next step for one, once (#2960).

    The ledger is written before the message is scheduled, and the message's id is the Work
    and turn end, so a pass repeated, or a crash in between, never asks twice. Returns how
    many were asked.

    A pass that failed a read (``degraded``) asks nothing, as ``work_bugs.report`` reports
    nothing: a timed-out job queue read gives no rows, so a Work whose tests are queued or
    running reads as ended, and four Works were asked at once that way (#3031). A restarting
    runtime shows no open question, so a Work waiting on one reads as ended too (#3487). The next
    pass, with its reads answered, asks any Work that really ended. A Work in ``held`` just
    had its issue re-read (``reread_ended_issues``), and waits for the pass that sees it.
    """
    if degraded:
        return 0
    from ..scheduled_messages import ScheduledMessage
    from ..work_derived import ENDING_SETTLE
    from ..work_endings import NUDGE_TEXT, REVIEW_PASSED_NUDGE_TEXT, message_id
    from ..work_router import owner_of_issue

    endings = getattr(app.state, "work_endings", None)
    messages = getattr(app.state, "scheduled_messages", None)
    if endings is None or messages is None:
        return 0
    state = app.state.app_state
    asked = 0
    resting = []
    for card in cards:
        derived = getattr(card, "derived", None)
        if derived is None:
            continue
        if derived.kind in _RESTING_KINDS:
            resting.append(card.work_id)
            continue
        if derived.kind == "review_stuck":
            if derived.review_failed and not card.paused:
                asked += _tell_review_stuck(app, card, derived, messages)
            continue
        if derived.kind != "ended" or card.paused or derived.at is None:
            continue
        if card.work_id in held:
            continue
        if datetime.now(UTC) < derived.at + ENDING_SETTLE:
            continue  # a person who stopped the turn may still be typing (#2965)
        ref = WorkRef.from_work_id(card.work_id)
        owner = owner_of_issue(state, ref.repository, ref.number) if ref is not None else None
        if owner is None:
            continue
        try:
            endings.record(card.work_id, derived.at)
            key = message_id(card.work_id, derived.at)
            if not messages.exists(key):
                now = datetime.now(UTC)
                messages.schedule(
                    ScheduledMessage(
                        id=key,
                        chat_id=owner[0],
                        text=(
                            REVIEW_PASSED_NUDGE_TEXT
                            if _waiting_on_tests(app, card.work_id)
                            else NUDGE_TEXT
                        ).format(ref=owner[1]),
                        due_at=now,
                        created_at=now,
                    )
                )
        except Exception:  # noqa: BLE001 -- the next pass, or the grace, covers it
            logger.warning("work %s next-step request failed", card.work_id, exc_info=True)
            continue
        asked += 1
        logger.info("work %s ended without a next step; asked its agent once", card.work_id)
    try:
        endings.forget(resting)
    except Exception:  # noqa: BLE001 -- forgotten on a later pass
        logger.warning("work endings ledger unwritable", exc_info=True)
    if asked:
        app.state.app_state.bus.publish(WORK_INPUTS_TOPIC)
    return asked


def _tell_review_stuck(app: Any, card: WorkCard, derived: Any, messages: Any) -> int:
    """Tell the Work's chat once that its review stopped with no verdict, and why (#3356).

    The deck already spent its own reviewer on the chat, so nothing else starts one; without
    this the Work chat waited on a verdict that was never coming. Keyed by the review chat, so
    a repeated pass, or a crash after scheduling, never tells twice. Returns 1 when told.
    """
    from ..scheduled_messages import ScheduledMessage
    from ..work_endings import REVIEW_STUCK_TEXT, review_stuck_id, review_stuck_reasons
    from ..work_router import owner_of_issue

    chat = getattr(derived, "review_chat", None)
    ref = WorkRef.from_work_id(card.work_id)
    if chat is None or ref is None:
        return 0
    owner = owner_of_issue(app.state.app_state, ref.repository, ref.number)
    if owner is None:
        return 0
    key = review_stuck_id(card.work_id, chat)
    try:
        if messages.exists(key):
            return 0
        service = chat_board_service(app.state)
        reasons = service.board.block_reasons(chat) if service is not None else ()
        row = service.board.rows.get(chat) if service is not None else None
        now = datetime.now(UTC)
        messages.schedule(
            ScheduledMessage(
                id=key,
                chat_id=owner[0],
                text=REVIEW_STUCK_TEXT.format(
                    pr=derived.pr,
                    ref=owner[1],
                    chat=(row.url if row is not None and row.url else f"#{chat}"),
                    reasons=review_stuck_reasons(reasons),
                ),
                due_at=now,
                created_at=now,
            )
        )
    except Exception:  # noqa: BLE001 -- the next pass tells it
        logger.warning("work %s stuck-review message failed", card.work_id, exc_info=True)
        return 0
    logger.info("work %s review #%s stopped with no verdict; told its agent", card.work_id, chat)
    return 1


def stopped_open_issues(
    cards: Sequence[WorkCard], facts: Mapping[str, WorkFacts]
) -> tuple[tuple[str, int], ...]:
    """Deck-run Works whose chat stopped while their cached issue still reads open (#2960).

    The deck hears a close only from the ``issues`` ``closed`` delivery; one that never came
    left a Work its agent had closed in Needs you as Stopped. The overview's backfill re-reads
    these, each at most once per ``BACKFILL_RETRY_S``, so a missed close corrects itself
    without a page being opened (#2947 covers only an opened page with every PR settled).
    """
    found = []
    for card in cards:
        derived = getattr(card, "derived", None)
        if derived is None or derived.kind not in _ENDED_KINDS:
            continue
        record = getattr(facts.get(card.work_id), "record", None)
        if not record or record.get("is_pr") or record.get("state") != "open":
            continue
        ref = WorkRef.from_work_id(card.work_id)
        if ref is not None:
            found.append((ref.repository, ref.number))
    return tuple(found)


async def _tree_parents(
    app: Any,
    snapshot: _Pass,
    shown: Sequence[WorkCandidate],
    candidates: Sequence[WorkCandidate],
    facts: dict[str, WorkFacts],
) -> tuple[dict[str, str], tuple[WorkCandidate, ...]]:
    """Each Work's parent Work id, and the parents to build only for their children (#3622).

    A shown Work's ancestors join the pass however quiet they are, so its row has a parent
    row to fold into: a candidate the front page did not pick, else one built from its issue
    alone, whose facts are gathered here and added to ``facts``. Walked a level per round,
    since a parent's own parent is known only from its record. No GitHub call.
    """
    by_id = {item.ref.work_id: item for item in candidates}
    listed = {item.ref.work_id for item in shown}
    parents = work_tree.parents_of(
        {work_id: item.record for work_id, item in facts.items() if item.record}
    )
    added: dict[str, WorkCandidate] = {}
    for _ in range(work_tree.MAX_DEPTH):
        wanted = {
            ancestor
            for work_id in (*listed, *added)
            for ancestor in work_tree.ancestors(work_id, parents)
            if ancestor not in listed and ancestor not in added
        }
        if not wanted:
            break
        fresh = []
        for work_id in sorted(wanted):
            item = by_id.get(work_id)
            if item is None:
                ref = parse_work_id(work_id)
                if ref is None:
                    continue
                item = WorkCandidate(ref, None, None, False)
                fresh.append(item)
            added[work_id] = item
        if fresh:
            more = await asyncio.to_thread(
                functools.partial(gather_facts, app, snapshot, fresh, strict=True)
            )
            facts.update(more)
            parents.update(
                work_tree.parents_of(
                    {work_id: item.record for work_id, item in more.items() if item.record}
                )
            )
    return parents, tuple(added.values())


def _tree_record_ids(
    shown: Sequence[WorkCandidate], parents: Mapping[str, str], facts: Mapping[str, WorkFacts]
) -> set[str]:
    """The issues whose records a tree on the front page reads (#3622): every shown Work's
    ancestors, and every shown Work with sub-issues of its own, whose count a closed child
    moves even when none of its children is listed."""
    found = {
        ancestor for item in shown for ancestor in work_tree.ancestors(item.ref.work_id, parents)
    }
    for item in shown:
        held = facts.get(item.ref.work_id)
        if held is not None and work_tree.sub_issue_counts(held.record) is not None:
            found.add(item.ref.work_id)
    return found


# At most this many parent issues are read from GitHub per overview pass (#3622).
TREE_REFRESH_LIMIT = 10


def _refresh_tree_records(app: Any, work_ids: Collection[str]) -> None:
    """Read the parent issues' records that are missing or stale, off the pass (#3622).

    A parent first named by a child's record has none until something reads it, and its
    sub-issue counts move only when it is read again after a child closes. Read on the open
    TTL even when closed (``refresh_tree_records``): live, a read seconds after a link got
    GitHub's old count. Each costs at most one read per open TTL; a read rebuilds the
    overview. One at a time.
    """
    refresh = getattr(getattr(app.state, "issues", None), "refresh_tree_records", None)
    running = getattr(app.state, "tree_refresh", None)
    if refresh is None or not work_ids or (running is not None and not running.done()):
        return
    refs = [
        (ref.repository, ref.number)
        for ref in (parse_work_id(work_id) for work_id in sorted(work_ids))
        if ref is not None
    ]

    async def run() -> None:
        try:
            read = await refresh(refs, limit=TREE_REFRESH_LIMIT)
        except Exception:  # noqa: BLE001 -- the rows draw from the cache as it is
            logger.warning("parent issue records could not be refreshed", exc_info=True)
            return
        if read:
            app.state.app_state.bus.publish(WORK_INPUTS_TOPIC)

    app.state.tree_refresh = asyncio.get_running_loop().create_task(run())


def _parse_time(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return when.replace(tzinfo=UTC) if when is not None and when.tzinfo is None else when


def _end_hides(app: Any, hidden: frozenset[str], ended: frozenset[str]) -> frozenset[str]:
    """End the hides of parent rows a child newly needs the owner in (#3622)."""
    control = getattr(app.state, "control_db", None)
    for work_id in ended:
        logger.info("hidden Work %s came back: a sub-issue's Work needs you", work_id)
        if control is not None:
            control.unhide_work(work_id)
    return hidden - ended


async def _work_candidates(app: Any, snapshot: _Pass, queue: Sequence[dict]) -> Sequence[Any]:
    """Every Work the dashboard builds this pass, the front page's and ``pull_work``'s alike."""
    held = await asyncio.to_thread(deck_paused_ids, app)
    return work_candidates(
        snapshot.state,
        snapshot.projection,
        owned_insights=snapshot.owned,
        legs_by_chat=snapshot.legs,
        current_work_ids=await current_work_ids(app, queue),
        # A Work paused on the deck stays built, like one waiting for Finish, so it stays on
        # the Paused list and its chats stay held however long it sits quiet (#2559).
        awaiting_work_ids=await asyncio.to_thread(awaiting_work_ids, app) | held,
        presentation=snapshot.presentation,
        let_go=_let_go_works(app),
        unstarted=unstarted_issues(
            snapshot.backlog,
            now=datetime.now(UTC),
            mine=getattr(app.state, "deck_claims", None),
        ),
    )


def _continued(item: Any, candidate: Any, now: datetime) -> bool:
    """A Work that finished before ``RECENT_WORK`` and is shown because a chat carried on in it
    since, within ``RECENT_WORK`` (``front_page_works``)."""
    if candidate is None or item.finished_at is None or now - item.finished_at <= RECENT_WORK:
        return False
    continued = continued_at(item.finished_at, candidate.last_activity)
    return continued is not None and now - continued <= RECENT_WORK


def _let_go_works(app: Any) -> dict[str, Any]:
    """The recently finished Works whose chats the live window may have let go, as the last
    pass built them (#3189); ``_publish_live_works`` keeps them."""
    remembered = getattr(app.state, "let_go_works", None) or {}
    return {work_id: candidate for work_id, (candidate, _) in remembered.items()}


async def pull_work(app: Any, repository: str, number: int) -> tuple[str, tuple[str, ...]] | None:
    """The one Work whose outputs include this pull request, as its issue and open pull
    requests (``owner/repo#n``), or None when no Work — or more than one — claims it (#2640).

    The poller's review path asks this for a pull request GitHub links to no issue: one that
    says ``Part of owner/issues#530`` belongs to that Work on the deck (its card's pull request,
    or a chat's registration) and nowhere on GitHub. The pull requests are the ones the Work
    page's review button passes, so both reviews list the same Work. The issue is spelt the
    way GitHub spells it when the deck holds its record, because it titles the chat.

    A review asked for by the Work's issue itself finds that Work too (#2684), so a chat opened
    with `agentdeck chat-review owner/issues#530` lists the pull requests the Work's chats
    registered, not only the ones GitHub links to the issue.
    """
    snapshot = gather_pass(app)
    candidates = await _work_candidates(app, snapshot, await runtime_queue(app))
    facts = await asyncio.to_thread(gather_facts, app, snapshot, candidates)
    wanted = (repository.lower(), number)
    found = [
        item
        for item in facts.values()
        if item.ref is not None
        and (
            (item.ref.repository.lower(), item.ref.number) == wanted
            or any((o.repository.lower(), o.number) == wanted for o in item.outputs)
        )
    ]
    if len(found) != 1:
        return None
    item = found[0]
    issue = f"{item.ref.repository}#{item.ref.number}"
    try:
        spelt = review_refs.normalise(str((item.record or {}).get("url") or ""))
    except review_refs.ReviewError:
        spelt = None
    if spelt is not None and f"{spelt[0]}#{spelt[1]}".lower() == issue.lower():
        issue = f"{spelt[0]}#{spelt[1]}"
    pulls = tuple(f"{o.repository}#{o.number}" for o in item.outputs if not o.settled)
    return issue, pulls


def _note_interactions_unknown(app: Any) -> None:
    """Mark the pass degraded while any account's runtime cannot say what it asks (#3487).

    A restarting runtime answers no open question for any chat, so each Work waiting on
    one read as ended and was asked for a next step, which cut its question off. Checked
    for the whole pass, not per card, since a card cached from an earlier pass is not
    rebuilt; before the cards and after them, as the runtime can go down in between.
    """
    from ..providers import PROVIDERS

    for account in getattr(getattr(app.state, "assistant", None), "accounts", None) or ():
        provider = PROVIDERS.get(account.provider_id)
        if provider is not None and provider.interactions_unknown(account):
            _degraded("runtime_interactions")
            return


async def load_work_overview(
    app: Any, *, now: datetime | None = None, changed_since: datetime | None = None
) -> WorkOverview:
    """Every dashboard Work card and the chats outside every Work, from one pass."""
    degraded: list[str] = []
    token = _DEGRADED.set(degraded)
    repos = _PASS_REPOS.set({})
    try:
        return await _load_work_overview(
            app, now=now or datetime.now(UTC), degraded=degraded, changed_since=changed_since
        )
    finally:
        _PASS_REPOS.reset(repos)
        _DEGRADED.reset(token)


async def _load_work_overview(
    app: Any, *, now: datetime, degraded: list[str], changed_since: datetime | None = None
) -> WorkOverview:
    snapshot = await gather_pass_off_loop(app)
    _note_interactions_unknown(app)
    queue = await runtime_queue(app)
    candidates = await _work_candidates(app, snapshot, queue)
    facts = await asyncio.to_thread(
        functools.partial(gather_facts, app, snapshot, candidates, strict=True)
    )
    await asyncio.to_thread(_note_awaiting, app, facts)
    reconcile_deck_pauses(app, facts)
    _publish_live_works(app, snapshot, facts, candidates, now=now)
    finished_at = {
        work_id: item.finished_at for work_id, item in facts.items() if item.finished_at is not None
    }
    awaiting = frozenset(
        work_id
        for work_id, item in facts.items()
        if item.awaiting_finish or (item.deck_paused and item.finished_at is None)
    )
    selection = front_page_works(candidates, now=now, finished_at=finished_at, awaiting=awaiting)
    shown, buckets = selection.shown, selection.buckets
    archived = archived_count(buckets, snapshot.state.db)
    # The parent issues the shown Works fold into (#3622): each is built too, and the ones
    # listed only for their children (``tree_only``) are kept out of everything per-Work.
    parents, tree_only = await _tree_parents(app, snapshot, shown, candidates, facts)
    _refresh_tree_records(app, _tree_record_ids(shown, parents, facts))
    shown = (*shown, *tree_only)
    tree_ids = frozenset(item.ref.work_id for item in tree_only)
    counts = {
        work_id: found
        for work_id, item in facts.items()
        if (found := work_tree.sub_issue_counts(item.record)) is not None
    }
    db_path = _db_path(app)

    # 1. Classify every sectioned Work cheaply (#2219): candidate facts, owned attention,
    #    finish times and the live queue. No workspace claims per Work, and no retained job
    #    evidence: neither moves a Work between sections.
    # Each Work's rows of the queue, from one walk of it (#2841): the whole queue scanned
    # again for every card was much of a pass's time on a deck with a long job history.
    queue_rows = WorkRows(queue)

    def light(item: Any) -> tuple[WorkCard, FrontRow]:
        card, _ = _cached_card(
            app,
            snapshot,
            item.ref,
            item.work,
            hydrated=False,
            now=now,
            jobs=queue_rows.for_work(item.ref.work_id),
            claims=(),
            facts=facts[item.ref.work_id],
        )
        return card, front_row(card, current=item.live)

    built: dict[str, tuple[WorkCard, FrontRow]] = {}
    for item in shown:
        built[item.ref.work_id] = light(item)
        # Let a waiting page request in between cards: a pass is pure work on the event loop,
        # and one long run of it held every request behind it for up to a second (#2841).
        await asyncio.sleep(0)
    hides = await asyncio.to_thread(
        _hidden_works, app, {work_id: card for work_id, (card, _) in built.items()}
    )
    hidden = frozenset(hides)

    def folded(rows: Sequence[FrontRow]) -> list[FrontRow]:
        return work_tree.fold(
            rows, parents=parents, counts=counts, hidden=hidden, tree_only=tree_ids
        )

    classified = front_sections(
        folded([row for _, row in built.values()]),
        now=now,
        max_active=MAX_DASHBOARD_WORKS,
        hidden=hidden,
    )
    # 2. The section policy decides what is listed: every Needs you row, Active and Recently
    #    finished up to their limits. 3. Only those rows are hydrated with their claims and
    #    their retained evidence (#2148).
    listed = list(
        dict.fromkeys(
            item.card.work_id
            for row in (
                *classified.needs_you,
                *classified.hidden,
                *classified.active,
                *classified.finished,
            )
            # A parent row draws its children's state, so they are hydrated with it (#3622).
            for item in (row, *row.children)
        )
    )
    jobs = WorkRows(await runtime_jobs(app, listed, queue=queue))
    stores = await asyncio.to_thread(_read_claims, db_path, listed)
    by_id = {item.ref.work_id: item for item in shown}
    for work_id in listed:
        item = by_id[work_id]
        card, _ = _cached_card(
            app,
            snapshot,
            item.ref,
            item.work,
            hydrated=True,
            now=now,
            jobs=jobs.for_work(work_id),
            claims=stores.get(work_id, []),
            facts=facts[work_id],
        )
        built[work_id] = (card, front_row(card, current=item.live))
        await asyncio.sleep(0)
    cards = [built[item.ref.work_id][0] for item in shown if item.ref.work_id not in tree_ids]
    # A Work this pass no longer builds keeps no card in the cache (#2865).
    card_cache = getattr(app.state, "work_card_cache", None)
    if card_cache:
        for key in [key for key in card_cache if key[0] not in built]:
            del card_cache[key]
    await start_quiet_reviews(
        app, [built[work_id][0] for work_id in built if work_id not in tree_ids]
    )
    rows = await asyncio.to_thread(
        _mark_stops,
        app,
        [built[item.ref.work_id][1] for item in shown if item.ref.work_id not in tree_ids],
        now,
        changed_since,
    )
    tree_rows = [built[work_id][1] for work_id in tree_ids]
    # A child that newly needs the owner brings back the parent row hiding it (#3622).
    ended = work_tree.hides_ended(
        [*rows, *tree_rows],
        parents=parents,
        hidden_at={work_id: _parse_time(item.hidden_at) for work_id, item in hides.items()},
    )
    if ended:
        hidden = await asyncio.to_thread(_end_hides, app, hidden, ended)
    board = snapshot.board
    if board is not None and not getattr(board, "complete", True):
        _degraded("board_incomplete")
    _note_interactions_unknown(app)  # again: the runtime may have gone during the pass
    await asyncio.to_thread(
        _log_status, app, rows, now, hidden=hidden, degraded=tuple(degraded)
    )
    await asyncio.to_thread(_report_bugs, app, rows, board, now, tuple(degraded))
    states = snapshot.insight_states
    other, other_hidden = other_activity(
        snapshot.state,
        snapshot.projection,
        now=now,
        attention_keys={
            insight.session_key: states.get(insight.session_key, insight.kind)
            for insight in snapshot.insights
        },
        chats=snapshot.chats,
        presentation=snapshot.presentation,
    )
    records = {work_id: item.record for work_id, item in facts.items() if item.record is not None}
    # A Work with a chat on this deck shows as one already, even after it went to another.
    held_here = {item.ref.work_id for item in candidates if getattr(item, "work", None)}
    return WorkOverview(
        tuple(cards),
        0,
        archived,
        other,
        other_hidden,
        other_decks=tuple(
            item
            for item in other_deck_works(
                snapshot.backlog, now=now, mine=getattr(app.state, "deck_claims", None)
            )
            if item.work_id not in held_here
        ),
        front=front_sections(
            folded([*rows, *tree_rows]), now=now, max_active=MAX_DASHBOARD_WORKS, hidden=hidden
        ),
        unresolved=(
            *unresolved_issues(candidates, records, snapshot.backlog),
            *stopped_open_issues(cards, facts),
        ),
        unsized=unsized_pulls([built[work_id][0] for work_id in listed]),
        open_rows=open_pull_rows([built[work_id][0] for work_id in listed]),
        merge_watch=merge_watch_pulls([built[work_id][0] for work_id in listed]),
        work_chat_ids=owned_chat_ids(snapshot.projection),
        projection=snapshot.projection,
        degraded=tuple(degraded),
    )


class WorkOverviewService:
    """Keeps the dashboard's Work overview current and says when it changed.

    Event-driven (ADR 0007, #2865): a pass runs when something a card reads changed —
    a chat (``sessions``), Deckhand (``assistant``), the poller board (``board``), the
    runtime's Runs and queue (``runtime``, ``jobs``), a review verdict (``reviews``) or the
    chat board — or when a card's clock-derived state falls due (``time_deadline``). And
    only while a dashboard is connected to ``works``: with nobody watching, a change just
    marks the overview stale, and the next page that connects or loads gets it rebuilt.
    """

    # The changes a card can read; see the class docstring.
    TOPICS = (
        "sessions",
        "assistant",
        "attention_reports",  # a chat marking itself waiting for a person (#3106)
        "jobs",
        BOARD_TOPIC,
        RUNTIME_TOPIC,
        REVIEWS_TOPIC,
        CHAT_BOARD_TOPIC,
        WORK_INPUTS_TOPIC,
        CHAT_NOTES_TOPIC,  # the agent's own comment, a spec-only Work's posted spec (#3247)
    )
    # The issue-record backfill (#2226): at most this many GitHub reads per this many
    # seconds, so thousands of records cached before ``closedAt`` never become a burst.
    BACKFILL_MAX = 4
    BACKFILL_EVERY_S = 60.0
    # An issue tried once waits this long before it is tried again, so one that cannot be
    # read never holds the few slots against the rest.
    BACKFILL_RETRY_S = 3600.0
    # An open pull request's cached row older than this is read again by the same backfill
    # (#3217): a webhook rewrites a moving one within seconds, so only a missed one ages out.
    OPEN_ROW_STALE_S = 3600.0
    DEBOUNCE_S = 1.0  # a burst of session changes becomes one rebuild
    # A working chat publishes on every write; the cards need at most one pass per this.
    MIN_INTERVAL_S = 3.0
    # A failed rebuild is tried again after this long, doubling up to the cap, while a
    # dashboard is open; with none open the kept stale marker makes the next connect retry.
    RETRY_MIN_S = 5.0
    RETRY_MAX_S = 300.0
    # With only a phone push listening, a card's clock-derived state rebuilds at most this
    # often (#3348): enough to buzz on a timed stop, without a page's every-change rate.
    PHONE_CLOCK_MIN_S = 300.0

    def __init__(self, app: Any) -> None:
        self._app = app
        self.overview: WorkOverview | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._backfilled_at: float | None = None
        # ``("issue" | "pull", (repository, number))`` -> when it was last tried.
        self._backfill_tried: dict[tuple[str, tuple[str, int]], float] = {}
        self._merges_checked_at: float | None = None
        self._soon: asyncio.Task | None = None
        # A rebuild was asked for while ``_soon`` ran (#3328): that one owes another pass.
        self._again = False
        # Something a card reads changed since the overview was built.
        self._stale = True
        # When the first change since the last build arrived (wall clock), so a Work that
        # stopped while nobody watched is dated inside that gap, not at the next connect.
        self._stale_since: datetime | None = None
        # When the built overview's clock-derived states next move (``time_deadline``).
        self._deadline: datetime | None = None
        # Set when a dashboard connects: the refresher may be sleeping with no deadline
        # from a pass nobody watched, and must now arm the one it skipped (#2865).
        self._rearm = asyncio.Event()
        # The newest turn end the last pass saw (``_turn_ended``, #2960).
        self._seen_end = 0.0
        # The next derived deadline (``_derived_deadlines``), kept with nobody watching.
        self._work_deadline: datetime | None = None
        # What the top bar's list showed at the last ``needs_you`` publication (#3239).
        self._needs_you = NeedsYouMenu().signature
        # The list's stops the phone was told about (#3348), read from the store once, and
        # the notifications it may still show (#3365).
        self._pushed: set[tuple[str, str]] | None = None
        self._showing: dict[str, set[str]] = {}
        self._push_lock = asyncio.Lock()  # one pass's sends and clears at a time, in order
        self._pushed_read = False
        self._push_tasks: set[asyncio.Task] = set()
        self._phone_checked: tuple[float, bool] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="work-overview")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _due(self) -> bool:
        return (
            self.overview is None
            or self._stale
            or (self._deadline is not None and datetime.now(UTC) >= self._deadline)
        )

    def _watched(self) -> bool:
        # A page showing the top bar's Needs you list watches too (#3239): a phone left on a
        # chat would otherwise show a count from whenever the front page was last open.
        bus = self._app.state.app_state.bus
        if bus.subscribers(WORKS_TOPIC) + bus.subscribers(NEEDS_YOU_TOPIC) > 0:
            return True
        # A chat waiting to hear of Works that need the owner watches too (#3446).
        wake_on = getattr(self._app.state, "wake_on", None)
        try:
            return wake_on is not None and wake_on.needs_you_waited()
        except Exception:  # noqa: BLE001 -- an unreadable registry: nobody waiting
            return False

    def _phone_listening(self) -> bool:
        """A phone would be pushed the list's new Works (#3348). Read from the store at most
        once a minute: the refresher asks on every change it wakes for."""
        now = time.monotonic()
        if self._phone_checked is not None and now - self._phone_checked[0] < 60.0:
            return self._phone_checked[1]
        push = getattr(self._app.state, "push", None)
        try:
            listening = push is not None and push.has_subscriptions()
        except Exception:  # noqa: BLE001 -- unreadable subscriptions: nobody listening
            listening = False
        self._phone_checked = (now, listening)
        return listening

    def phone_changed(self) -> None:
        """A push subscription was added or removed (#3348): read it again, and wake the
        refresher, which may be sleeping with no deadline because nobody listened."""
        self._phone_checked = None
        self._rearm.set()

    def _push_needs_you(self, overview: WorkOverview) -> None:
        """Tell the phone about Works new in the top bar's list since the last pass (#3348).

        What was told is stored before anything is sent: a send that fails is not tried
        again, so a phone never buzzes twice for one stop. The same pass clears, on every
        device, a notification whose Works all left the list (#3365). Passes send one after
        another, so a clear never overtakes the send it closes, and only a notification at
        least one device took counts as showing: no clear goes for one nobody got.
        """
        push = getattr(self._app.state, "push", None)
        store = getattr(self._app.state, "control_db", None)
        if push is None or store is None or not push.enabled:
            return
        if not self._pushed_read:
            self._pushed = store.load_needs_you_pushed()
            self._showing = store.load_needs_you_showing()
            self._pushed_read = True
        state = self._app.state.app_state
        sends, kept = needs_you_pushes(overview, self._pushed, muted=state.session_is_muted)
        if kept != self._pushed:
            store.save_needs_you_pushed(kept)
            self._pushed = kept
        if not sends and not self._showing and not self._push_tasks:
            return

        async def send() -> None:
            async with self._push_lock:
                delivered = [
                    item for item in sends
                    if await asyncio.to_thread(
                        push.send_to_all, item.title, item.body, item.url, item.tag
                    )
                ]
                clears, showing = needs_you_clears(overview, self._showing, delivered)
                if showing != self._showing:
                    store.save_needs_you_showing(showing)
                    self._showing = showing
                if clears:
                    await asyncio.to_thread(push.clear_all, clears)

        task = asyncio.create_task(send(), name="needs-you-push")
        self._push_tasks.add(task)
        task.add_done_callback(self._push_sent)

    def _wake_needs_you(self, overview: WorkOverview) -> None:
        """Wake each chat waiting on Works that need the owner (#3446)."""
        wake_on = getattr(self._app.state, "wake_on", None)
        if wake_on is None:
            return
        state = self._app.state.app_state
        try:
            wake_on.needs_you(
                needs_you_stops(overview, state.chat_identity_key),
                complete=not overview.degraded,
            )
        except Exception:  # noqa: BLE001 -- a wake is never worth a failed pass
            logger.exception("Needs you wake-on failed")

    def _push_sent(self, task: asyncio.Task) -> None:
        self._push_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Needs you push failed: %s", task.exception())

    def _newest_end(self) -> float:
        """The newest last message of any chat not taking a turn now, as a timestamp."""
        from ..work_overview import _leg_working

        newest = 0.0
        if getattr(self._app.state, "work_endings", None) is None:
            return newest  # nothing is asked, so nothing is owed (``_turn_ended``)
        sessions = getattr(self._app.state.app_state, "sessions", None) or {}
        for session in list(sessions.values()):
            when = getattr(session, "last_message_at", None)
            if when is None or _leg_working(session):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            newest = max(newest, when.timestamp())
        return newest

    def _turn_ended(self) -> bool:
        """Did a chat end a turn the last pass did not see (#2960)?

        A pass then runs even with nobody watching: it is where the deck asks a Work that
        ended without a next step for one (``ask_for_next_steps``), and that must not wait
        for the owner to open the deck. Read from what each chat holds, not from tracking
        turns start and end: any idle chat whose last message is newer than the newest one
        the last pass saw (``_seen_end``, written by the pass itself).
        """
        if getattr(self._app.state, "work_endings", None) is None:
            return False
        if self.overview is None:
            return True  # nothing built since the start: the first pass looks at every Work
        return self._newest_end() > self._seen_end

    async def current(self, *, wait: float = 0.0) -> WorkOverview:
        """The last overview, building one first if none exists yet.

        A stale one is rebuilt behind the caller. ``wait`` is how long to wait for that
        rebuild: the front page waits a moment, so a reload paints the current cards rather
        than the old ones swapped for the new a second later (#3108). A rebuild slower than
        that is still answered with the old cards, and the page's own stream receives the
        new ones as a ``works`` push.
        """
        if self.overview is None:
            await self.refresh()
        elif self._due():
            self.refresh_soon()
            if wait > 0 and self._soon is not None:
                await asyncio.wait({self._soon}, timeout=wait)
        return self.overview or WorkOverview()

    def connected(self) -> None:
        """A stream just subscribed to ``works`` or ``needs_you``: catch up anything it
        missed, and wake the refresher so it arms the card deadline it skipped while
        unwatched."""
        self._rearm.set()
        if self._due():
            self.refresh_soon()

    async def refresh(self) -> bool:
        """Rebuild; publish ``works`` and return True only when something changed.

        Never before every account's first scan (#2760): the first scans publish sessions one
        account at a time, and an overview built from part of them drops Works for a pass.
        """
        if not await sessions_scanned(self._app):
            return False
        async with self._lock:
            self._stale = False
            changed_since, self._stale_since = self._stale_since, None
            work_counter.count(work_counter.WORK_OVERVIEW)
            now = datetime.now(UTC)
            # Before the build: an ending that lands during it is newer, and owes another.
            seen_end = self._newest_end()
            try:
                overview = await load_work_overview(
                    self._app, now=now, changed_since=changed_since
                )
            except BaseException:
                # Nothing was built: keep the change owed, and when it first arrived, so
                # a retry or the next connect still catches it up (#2865).
                self._stale = True
                if changed_since is not None and (
                    self._stale_since is None or changed_since < self._stale_since
                ):
                    self._stale_since = changed_since
                raise
            self._deadline = time_deadline(now, overview.cards, overview.other)
            self._seen_end = seen_end
            # The derived states the deck acts on by itself (a quiet review, an unanswered
            # request) fall due with nobody watching too (#2960).
            self._work_deadline = min(
                (moment for moment in _derived_deadlines(overview.cards) if moment > now),
                default=None,
            )
            changed = overview != self.overview
            if changed:
                self.overview = overview
        # Only once the snapshot is current: delivery holds a paused Work's chats by it
        # (``paused_hold``), and a request sent while it still showed a just-resumed Work as
        # paused was refused, yet recorded as asked (#3103). On the loop: it schedules a
        # message and publishes, neither of which is thread-safe.
        held = await reread_ended_issues(self._app, overview.cards, degraded=overview.degraded)
        ask_for_next_steps(self._app, overview.cards, degraded=overview.degraded, held=held)
        if held:
            # A held Work owes a pass: the one its read publishes, or a retry of a failed one.
            self.refresh_soon(delay=ENDING_REREAD_RETRY_S)
        # Every pass, not only a changed one: a wake whose save failed is tried again.
        self._wake_needs_you(overview)
        if not changed:
            return False
        self._app.state.app_state.bus.publish(WORKS_TOPIC)
        menu = needs_you_menu(overview).signature
        if menu != self._needs_you:
            self._needs_you = menu
            self._app.state.app_state.bus.publish(NEEDS_YOU_TOPIC)
        try:
            self._push_needs_you(overview)
        except Exception:  # noqa: BLE001 -- a push is never worth a failed pass
            logger.exception("Needs you push failed")
        return True

    def refresh_soon(self, *, delay: float = 0.0) -> None:
        """Rebuild in the background now, not at the next change: a Work was just opened
        (#2283), a page connected to a stale overview, or a rebuild failed (``delay``).

        A refresh already scheduled covers it only until it starts: one already building may
        have read the records before this change (a Work just marked read), so a call now
        makes it build once more when it finishes, or other devices keep the Work as new
        until something unrelated changes (#3328). A delayed retry asks for no such pass;
        its own back-off covers it. One failing is logged and tried again with a
        back-off while a dashboard is open (#2865): no clock rebuilds the overview any more,
        so a connected page would otherwise keep the failed pass's cards until something
        else changed.
        """
        if self._soon is not None and not self._soon.done():
            # Its own pass asking (a held Work's retry) is not a change it missed.
            if not delay and asyncio.current_task() is not self._soon:
                self._again = True
            return

        async def run() -> None:
            wait = delay
            backoff = watch_retry.Backoff(first=self.RETRY_MIN_S, cap=self.RETRY_MAX_S)
            while True:
                if wait:
                    await asyncio.sleep(wait)
                    if not self._watched():
                        return  # the kept stale marker makes the next connect retry
                try:
                    self._again = False
                    await self.refresh()
                    if not self._again:
                        return
                    wait = 0.0
                    continue
                except Exception:  # noqa: BLE001 -- retried below, or by the next connect
                    logger.exception("work overview refresh failed")
                if not self._watched():
                    return
                wait = backoff.failed()

        self._soon = asyncio.create_task(run(), name="work-overview-opened")

    async def _run(self) -> None:
        bus = self._app.state.app_state.bus
        loop = asyncio.get_running_loop()
        built_at = loop.time()
        with bus.subscribe(*self.TOPICS) as sub:
            while True:
                # Sleep until a change, or until a card's clock-derived state falls due.
                # Nobody watching means no deadline either: the next connect catches up.
                timeout = None
                watched = self._watched()
                due_at = self._deadline if watched else self._work_deadline
                # A phone listening takes the cards' clock too, at its slower rate (#3348).
                phone_at = None if watched else self._phone_deadline(built_at, loop)
                if phone_at is not None:
                    due_at = phone_at if due_at is None else min(due_at, phone_at)
                if due_at is not None:
                    timeout = max(0.0, (due_at - datetime.now(UTC)).total_seconds())
                changed = await self._wait_for_change(sub, timeout)
                if changed:
                    self._stale = True
                    if self._stale_since is None:
                        self._stale_since = datetime.now(UTC)
                # With nobody watching, a pass still runs when a Work chat's turn ended or a
                # Work's own inputs moved (a merge, a release record): that is where a Work
                # ended without a next step is asked for one (#2960).
                topics = sub.changed_topics() if changed else set()
                fell_due = (
                    self._work_deadline is not None
                    and datetime.now(UTC) >= self._work_deadline
                )
                owed = fell_due or (
                    changed and (WORK_INPUTS_TOPIC in topics or self._turn_ended())
                ) or (phone_at is not None and datetime.now(UTC) >= phone_at)
                if fell_due:
                    # Spent: the pass sets the next one, and a failed pass must not spin here.
                    self._work_deadline = None
                    self._stale = True
                if (not self._watched() and not owed) or not self._due():
                    # A connect with nothing owed only re-plans the deadline above.
                    continue
                wait = max(self.DEBOUNCE_S, built_at + self.MIN_INTERVAL_S - loop.time())
                await asyncio.sleep(wait)
                while sub.get_nowait() is not None:
                    pass
                sub.changed_topics()
                built_at = loop.time()
                try:
                    await self.refresh()
                    read = await self.backfill(loop.time())
                    read += await self.check_merges(loop.time())
                    if read:
                        await self.refresh()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 -- one bad pass must not stop the refresher
                    logger.exception("work overview refresh failed")
                    self.refresh_soon(delay=self.RETRY_MIN_S)

    def _phone_deadline(self, built_at: float, loop: Any) -> datetime | None:
        """When a card's clock-derived state is due for a phone, not before
        ``PHONE_CLOCK_MIN_S`` after the last build; None when no phone listens."""
        if self._deadline is None or not self._phone_listening():
            return None
        floor = datetime.now(UTC) + timedelta(
            seconds=max(0.0, built_at + self.PHONE_CLOCK_MIN_S - loop.time())
        )
        return max(self._deadline, floor)

    async def _wait_for_change(self, sub: Any, timeout: float | None) -> bool:
        """Sleep until a topic moves, a dashboard connects, or ``timeout``; whether a topic
        moved."""
        get = asyncio.ensure_future(sub.get())
        rearm = asyncio.ensure_future(self._rearm.wait())
        try:
            await asyncio.wait({get, rearm}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (get, rearm):
                task.cancel()
            await asyncio.gather(get, rearm, return_exceptions=True)
        self._rearm.clear()
        # A ``get`` that finished as it was cancelled still took a change off the queue.
        return get.done() and not get.cancelled()

    async def backfill(self, now: float) -> int:
        """Re-read a few issue records and pull request rows the overview could not use; how
        many were read.

        Issue records first, then pull request rows cached before they carried the pull
        request's size (#2236), then open pull requests' rows no webhook rewrote for
        ``OPEN_ROW_STALE_S`` (#3217), in one budget: ``BACKFILL_MAX`` per ``BACKFILL_EVERY_S``,
        through the shared metadata cache and its lease, so the same key is never read twice
        across processes.
        """
        overview = self.overview
        issues = getattr(self._app.state, "issues", None)
        refreshers = {
            "issue": getattr(issues, "refresh_issue_records", None),
            "pull": getattr(issues, "refresh_pull_rows", None),
        }
        stale = getattr(issues, "stale_pull_rows", None)
        if overview is None:
            return 0
        if self._backfilled_at is not None and now - self._backfilled_at < self.BACKFILL_EVERY_S:
            return 0
        tried = self._backfill_tried
        for key, when in list(tried.items()):
            if now - when >= self.BACKFILL_RETRY_S:
                del tried[key]
        wanted = [
            *(("issue", ref) for ref in overview.unresolved if refreshers["issue"] is not None),
            *(("pull", ref) for ref in overview.unsized if refreshers["pull"] is not None),
        ]
        if stale is not None and refreshers["pull"] is not None:
            aging = [ref for ref in overview.open_rows if ("pull", ref) not in tried]
            if aging:
                wanted += [("pull", ref) for ref in stale(aging, self.OPEN_ROW_STALE_S)]
        batch = [item for item in dict.fromkeys(wanted) if item not in tried][: self.BACKFILL_MAX]
        if not batch:
            return 0
        self._backfilled_at = now
        tried.update((item, now) for item in batch)
        read = 0
        for kind, refresh in refreshers.items():
            refs = [ref[:2] for which, ref in batch if which == kind]
            if refs and refresh is not None:
                read += await refresh(refs, limit=self.BACKFILL_MAX)
        return read


    async def check_merges(self, now: float) -> int:
        """Keep Merge candidates' check rows current; how many rows are held (#2275, #2332).

        At most once per ``BACKFILL_EVERY_S`` and ``BACKFILL_MAX`` pull requests. The fetch is
        not forced, so a row still inside the cache's open TTL costs no GitHub call.
        """
        overview = self.overview
        refresh = getattr(getattr(self._app.state, "issues", None), "refresh_merge_checks", None)
        if overview is None or refresh is None or not overview.merge_watch:
            return 0
        if self._merges_checked_at is not None and now - self._merges_checked_at < (
            self.BACKFILL_EVERY_S
        ):
            return 0
        self._merges_checked_at = now
        return await refresh(overview.merge_watch, limit=self.BACKFILL_MAX)


# --- one Work -------------------------------------------------------------------------------

# The Work page's workspace is the one part that runs git. The page and its stream share one
# inspection per Work for this long, so an open page does not re-read every checkout on each
# refresh, and the stream's first pass reuses what the page load just built.
WORKSPACE_TTL_S = 30.0
_WORKSPACE_CACHE_SIZE = 16


def _workspace_cache(app: Any) -> OrderedDict[str, tuple[float, WorkWorkspace | None]]:
    cache = getattr(app.state, "work_workspace_cache", None)
    if cache is None:
        cache = app.state.work_workspace_cache = OrderedDict()
    return cache


@dataclass(frozen=True)
class WorkDiagram:
    """The Work page's Diagram button (#3827): what it is called and where it goes."""

    title: str
    href: str
    external: bool = False  # GitHub's page for the file, opened in a new tab


def work_diagram(app: Any, ref: WorkRef, chats: Sequence[WorkChat]) -> WorkDiagram | None:
    """The diagram on the live page of the Work's newest chat that has one (#3827).

    It opens that live page while the file is there. Once the worktree is gone, an
    agentdeck diagram opens from the deck's own checkout, and any other repository's from
    GitHub: only the deck's checkout is kept current, so another one could be stale.
    """
    state = app.state.app_state
    newest = sorted(
        chats, key=lambda chat: chat.last_activity or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for chat in newest:
        record = live_prototype.read(state.chat_identity_key(chat.session_key))
        link = live_prototype.diagram_link(record, chat.session_key, ref.repository)
        if link is not None:
            return WorkDiagram(*link)
    return None


@dataclass(frozen=True)
class WorkDetail:
    card: WorkCard
    workspace: WorkWorkspace | None
    chats: tuple[WorkChat, ...]
    verifications: tuple[Any, ...]  # every retained RunVerification, newest first
    # Where a message sent to the Work goes (#2218); None when no chat can take one now.
    message_target: MessageTarget | None = None
    unplaced: tuple[UnplacedChat, ...] = ()  # ambiguous delegations naming this Work
    # The Work's newest action press and what became of it (#2237), or None.
    last_action: WorkActionPress | None = None
    # The Work's hide, while it holds (#2238), or None.
    hidden: HiddenWork | None = None
    # The name the operator gave the Work on this deck, "" for the derived title (#2328).
    custom_title: str = ""
    # The Work's persisted decision record (#2328), or None before the verify worker wrote it.
    resolution: WorkResolution | None = None
    # The release's ``approval`` for an early Merge (#3320), and its line once one was cleared.
    approval: str = ""
    approval_cleared: str = ""
    # The issue's parent and sub-issues (#3622), or None when it is in no tree.
    tree: work_tree.WorkTree | None = None
    # The Diagram button (#3827), or None while no chat of the Work has drawn one.
    diagram: WorkDiagram | None = None

    @property
    def resolution_behind(self) -> datetime | None:
        """When the newest head of the Work's open pull requests was committed, if the
        resolution was written before it (#3199); None while the resolution is current or
        either time is unknown."""
        written = self.resolution.updated_at if self.resolution is not None else None
        heads = [
            output.head_at
            for output in self.card.outputs
            if not output.settled and output.head_at is not None
        ]
        if written is None or not heads:
            return None
        if written.tzinfo is None:
            written = written.replace(tzinfo=UTC)
        newest = max(heads)
        return newest if newest > written else None

    @property
    def status(self) -> WorkStatus:
        return work_status(self.card)

    @property
    def line(self) -> WorkStatus:
        """The page's status box: the same one status as everything else on it (#2944)."""
        return self.status

    @property
    def attention(self) -> OperatorAttention | None:
        """The same operator category the front page's row shows (#2236), judged on the same
        story, so a Work its own notification woke keeps its actions here too (#2760)."""
        return operator_attention(self.card, waiting_story(self.card, self.status))

    @property
    def buttons(self) -> tuple[ActionButton, ...]:
        """The same controls the front page's row offers (#2237); Hide only while not hidden."""
        return buttons(
            self.card,
            self.attention,
            self.status.where_href,
            page=True,
            hidden=self.hidden is not None,
        )

    @property
    def finish_control(self) -> ActionButton | None:
        """Finish beside the headline when its actions offer none (#3187)."""
        return finish_control(self.card, self.attention)

    @property
    def pause_control(self) -> ActionButton | None:
        """Pause, or Resume on a paused Work (#2548), beside the status headline."""
        return pause_control(self.card)

    @property
    def merge_approval_control(self) -> ActionButton | None:
        """Merge when ready, or Withdraw while a press stands (#3320), beside the headline."""
        return merge_approval_control(self.card, self.attention, self.approval)

    @property
    def merge_approved(self) -> bool:
        """Does a person's early Merge press stand (#3320)?"""
        from ..work_release import MERGE_APPROVED

        return self.approval == MERGE_APPROVED

    @property
    def star_control(self) -> ActionButton:
        """Star, or Unstar on a starred Work (#3116), beside the status headline."""
        return star_control(self.card)

    @property
    def pause_refusal(self) -> str:
        """Why this unfinished Work offers no Pause (#2559), "" when it does or has finished."""
        card = self.card
        if card.paused or card.finished_at is not None or card.awaiting_finish:
            return ""
        return pause_refusal(card)

    @property
    def last_action_followed(self) -> str:
        """The newest press in the past tense once it was acted on, else "" (#2460).

        A Merge is acted on once the Work merged; until then the press's own receipt says what
        comes next.
        """
        press = self.last_action
        if press is None or press.state != "sent" or press.reserved is None:
            return ""
        if press.action == MERGE:
            outputs = self.card.outputs
            if any(o.state == "merged" for o in outputs) and all(o.settled for o in outputs):
                return "Merge asked; merged"
        return ""

    @property
    def bring_back(self) -> ActionButton:
        """What a hidden Work's page offers (#2238)."""
        return BRING_BACK

    @property
    def workspace_rows(self) -> tuple[WorkspaceRow, ...]:
        return workspace_rows(self.workspace, self.card.workspace)

    @property
    def open_chat(self) -> tuple[str, str] | None:
        """``(session key, path)`` of the chat a desktop opens beside this Work (#2345); the
        same chat a phone's front-page row opens (``opening_chat``, #2512)."""
        return opening_chat(self.message_target, self.chats)


def work_checkouts(
    workspace: WorkWorkspace | None,
) -> tuple[tuple[str, WorkPullRequest | None], ...]:
    """Every checkout of this Work on this host, with the pull request to diff it against
    (#2379): each repository's registered worktrees, then its chats' directories, one entry
    per checkout. A path that no longer exists is history, not a checkout."""
    if workspace is None:
        return ()
    found: dict[str, WorkPullRequest | None] = {}
    for repo in workspace.repositories:
        pulls = [item for item in repo.pull_requests if item.base_branch]
        for item in (*repo.worktrees, *repo.cwds):
            seen = item.observation
            if seen.exists:
                found.setdefault(seen.toplevel or seen.path, _checkout_pull(seen.branch, pulls))
    for item in workspace.unresolved_observations:
        seen = item.observation
        if seen.exists:
            found.setdefault(seen.toplevel or seen.path, None)
    return tuple(found.items())


def _checkout_pull(branch: str, pulls: Sequence[WorkPullRequest]) -> WorkPullRequest | None:
    """The pull request a checkout is diffed against: the one whose head is the checkout's
    branch. A repository can hold several worktrees and pull requests, so any other pairing
    is a guess, made only when it cannot be wrong on what is known: a single pull request,
    and either side's branch unknown. A checkout on some other branch gets none."""
    if branch:
        matched = [pull for pull in pulls if pull.head_branch == branch]
        if len(matched) == 1:
            return matched[0]
    if len(pulls) == 1 and (not branch or not pulls[0].head_branch):
        return pulls[0]
    return None


def _can_send(app: Any) -> Callable[[str], bool]:
    """Whether a chat's leg takes a message now: the same test the chat's own composer has."""
    from ..models import Capability

    config = getattr(getattr(app.state, "config", None), "inject", None)
    injector = getattr(app.state, "injector", None)
    sessions = app.state.app_state.sessions

    def can_send(session_key: str) -> bool:
        session = sessions.get(session_key)
        if session is None or config is None or not config.enabled:
            return False
        if Capability.INJECT in session.capabilities:
            return True
        return bool(injector is not None and injector.can_queue(session_key))

    return can_send


class StepTimes:
    """Wall milliseconds per step of one Work page build, for its timing header (#2654).

    A step that awaits a thread also counts the time it waited for the event loop to come
    back, which is what a slow open on a busy deck is made of. ``received`` is when the
    request reached the app (``perf_counter``, stamped by a middleware): the gap to the
    handler's start is ``lag``, the time the open sat queued behind the loop, and it counts
    in the total.
    """

    def __init__(self, received: float | None = None) -> None:
        now = time.perf_counter()
        self.started = min(received, now) if received is not None else now
        self.steps: dict[str, float] = {"lag": (now - self.started) * 1000}
        self._mark = now

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.steps[name] = self.steps.get(name, 0.0) + (now - self._mark) * 1000
        self._mark = now

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def server_timing(self) -> str:
        parts = [f"{name};dur={ms:.1f}" for name, ms in self.steps.items()]
        return ", ".join([*parts, f"total;dur={self.total_ms:.1f}"])

    def summary(self) -> str:
        return " ".join(f"{name}={ms:.0f}ms" for name, ms in self.steps.items())


async def _reread_settled_issue(app: Any, ref: WorkRef, facts: WorkFacts) -> bool:
    """Re-read a deck-run Work's open issue once its pull requests have all settled (#2947).

    The deck hears a close only from the ``issues`` ``closed`` delivery, and no Poller re-reads
    a Work the deck runs itself, so a dropped delivery would leave it open for good. Only a
    stale record is read (the cache's open TTL), so a page drawn often costs one read per few
    minutes at most. True when the record may have changed; the bus then tells the overview.
    """
    record = facts.record
    router = getattr(app.state, "work_router", None)
    switched = getattr(router, "switched", None)
    refresh = getattr(getattr(app.state, "issues", None), "refresh_stale_issue_records", None)
    if (
        refresh is None
        or switched is None
        or not switched(ref.repository)
        or not record
        or record.get("is_pr")
        or record.get("state") != "open"
        or not facts.outputs
        or not all(output.settled for output in facts.outputs)
    ):
        return False
    try:
        read = await refresh([(ref.repository, ref.number)])
    except Exception:  # noqa: BLE001 -- the page draws from the cache as it is
        logger.warning("work %s issue re-read failed", ref.work_id, exc_info=True)
        return False
    if read:
        app.state.app_state.bus.publish(WORK_INPUTS_TOPIC)
    return bool(read)


async def load_work_detail(
    app: Any,
    ref: WorkRef,
    *,
    workspace: WorkWorkspace | None | bool = True,
    times: StepTimes | None = None,
) -> WorkDetail | None:
    """One Work's page, or None when nothing on the deck names it.

    ``workspace=True`` inspects the workspace (bounded git reads, in a thread).
    """
    lap = times.lap if times is not None else (lambda _name: None)
    snapshot = await gather_pass_off_loop(app)
    work = snapshot.projection.get(ref)
    lap("pass")
    jobs = await runtime_jobs(
        app, [ref.work_id], queue=await runtime_queue(app, work_id=ref.work_id)
    )
    lap("jobs")
    claims = (await asyncio.to_thread(_read_claims, _db_path(app), [ref.work_id]))[ref.work_id]
    lap("stores")
    if workspace is True:
        cache = _workspace_cache(app)
        cached = cache.get(ref.work_id)
        if cached is not None and time.monotonic() - cached[0] < WORKSPACE_TTL_S:
            workspace = cached[1]
        else:
            assistant = getattr(app.state, "assistant", None)
            facts = gather_work_workspace(
                snapshot.state,
                ref,
                board=snapshot.board,
                git_contexts=assistant.contexts if assistant is not None else None,
                jobs=jobs,
                claims=claims,
            )
            workspace = await asyncio.to_thread(facts.build) if facts.known else None
            cache[ref.work_id] = (time.monotonic(), workspace)
            cache.move_to_end(ref.work_id)
            while len(cache) > _WORKSPACE_CACHE_SIZE:
                cache.popitem(last=False)
    elif workspace is False:
        workspace = None
    lap("workspace")
    # The same facts the front page reads, so the Work page agrees with it (#2229).
    candidate = SimpleNamespace(ref=ref, work=work)
    facts = (await asyncio.to_thread(gather_facts, app, snapshot, [candidate]))[ref.work_id]
    if await _reread_settled_issue(app, ref, facts) or await _reread_tree_issue(
        app, ref, facts
    ):
        facts = (await asyncio.to_thread(gather_facts, app, snapshot, [candidate]))[ref.work_id]
    lap("facts")
    card, chats = _card(
        app,
        snapshot,
        ref,
        work,
        jobs=jobs,
        claims=claims,
        facts=facts,
        workspace=workspace,
    )
    lap("card")
    if (
        work is None
        and workspace is None
        and not facts.awaiting_finish
        and not facts.start_label  # an issue no agent works on yet is a Work too (#3558)
        # A parent issue's row, and each sub-issue its page lists, opens its page whether or
        # not an agent works on it (#3622).
        and work_tree.sub_issue_counts(facts.record) is None
        and work_tree.parent_ref(facts.record) is None
        and not in_inventory(snapshot.state.db, ref)
    ):
        return None
    tree = await _work_tree(app, ref, facts.record)
    lap("tree")
    control = getattr(app.state, "control_db", None)
    presses = control.load_work_actions(ref.work_id, limit=1) if control is not None else ()
    hidden = await asyncio.to_thread(_hidden_works, app, {ref.work_id: card})
    resolution = control.work_resolution(ref.work_id) if control is not None else None
    release = getattr(app.state, "work_release", None)
    detail = WorkDetail(
        card=card,
        workspace=workspace,
        chats=chats,
        verifications=run_verifications(jobs, ref.work_id, limit=None),
        last_action=presses[0] if presses else None,
        hidden=hidden.get(ref.work_id),
        custom_title=snapshot.titles.get(ref.work_id, ""),
        resolution=resolution,
        approval=_release_read(release, "approval", ref.work_id),
        approval_cleared=_release_read(release, "approval_cleared", ref.work_id),
        tree=tree,
        diagram=await asyncio.to_thread(work_diagram, app, ref, chats),
        message_target=message_target(card, chats, _can_send(app)),
        unplaced=unplaced_chats(
            snapshot.state,
            ref,
            snapshot.projection,
            chats=snapshot.chats,
            legs_by_chat=snapshot.legs,
            presentation=snapshot.presentation,
        ),
    )
    lap("rest")
    return detail


async def _reread_tree_issue(app: Any, ref: WorkRef, facts: WorkFacts) -> bool:
    """Read a Work page's own issue record when it is missing, or stale and in a tree (#3622):
    a parent the front page links to that nothing has read yet, or one whose sub-issue
    counts a child's close moved. Through the cache's TTL, so a page drawn often
    costs one read per few minutes at most. True when the record may have changed."""
    record = facts.record
    if record and work_tree.parent_ref(record) is None and (
        work_tree.sub_issue_counts(record) is None
    ):
        return False
    refresh = getattr(getattr(app.state, "issues", None), "refresh_stale_issue_records", None)
    if refresh is None:
        return False
    try:
        return bool(await refresh([(ref.repository, ref.number)]))
    except Exception:  # noqa: BLE001 -- the page draws from the cache as it is
        logger.warning("work %s issue re-read failed", ref.work_id, exc_info=True)
        return False


def _front_rows(app: Any) -> dict[str, FrontRow]:
    """Every Work's own front-page row in the last overview, folded children included."""
    service = getattr(app.state, "work_overview", None)
    overview = getattr(service, "overview", None)
    front = getattr(overview, "front", None)
    if front is None:
        return {}
    found: dict[str, FrontRow] = {}
    for row in (
        *front.needs_you, *front.hidden, *front.active, *front.waiting, *front.live,
        *front.finished, *front.paused,
    ):
        for item in (row.own or row, *row.children):
            found.setdefault(item.card.work_id, item)
    return found


def tree_revision(overview: Any, work_id: str) -> str:
    """The revision of the parent row a Work's sub-issues fold into on the front page, or ""
    when it has none: moves whenever a child's row, or its sub-issue count, does (#3622)."""
    front = getattr(overview, "front", None)
    if front is None:
        return ""
    for row in (
        *front.needs_you, *front.hidden, *front.active, *front.waiting, *front.live,
        *front.finished, *front.paused,
    ):
        if (row.children or row.progress) and row.card.work_id == work_id:
            return row.revision
    return ""


async def _work_tree(app: Any, ref: WorkRef, record: Any) -> work_tree.WorkTree | None:
    """The Work page's tree (#3622): the issue's parent from its cached record, and its
    sub-issues from GitHub's own list through the shared cache, each with its front-page
    state. None when the issue has neither."""
    parent = work_tree.parent_ref(record)
    counts = work_tree.sub_issue_counts(record)
    if parent is None and counts is None:
        return None
    issues = getattr(app.state, "issues", None)
    rows = _front_rows(app)
    parent_line = None
    if parent is not None:
        lookup = getattr(issues, "cached_issues", None)
        try:
            held = lookup([(parent.repository, parent.number)]) if lookup is not None else {}
        except Exception:  # noqa: BLE001 -- the link still shows, with its number
            held = {}
        found = held.get((parent.repository, parent.number)) or {}
        parent_line = work_tree.tree_line(
            parent,
            ref,
            title=str(found.get("title") or ""),
            state=str(found.get("state") or "open"),
            row=rows.get(parent.work_id),
            href=work_href(parent.work_id),
        )
    children: list[work_tree.TreeLine] = []
    items: Sequence[dict] | None = None
    unreadable = False
    reads = _TreeReads(ref, issues)
    if counts is not None:
        items = await reads.read("sub_issues", ref.repository, ref.number)
        unreadable = items is None
        for item in items or ():
            child = WorkRef.from_issue(IssueRef(item["repository"], item["number"]))
            children.append(
                work_tree.tree_line(
                    child,
                    ref,
                    title=str(item.get("title") or ""),
                    state=str(item.get("state") or "open"),
                    row=rows.get(child.work_id),
                    href=work_href(child.work_id),
                )
            )
    diagram = await _wayfinder_diagram(app, ref, record, items or (), rows, reads=reads)
    if reads.stale:
        _refresh_tree(app, ref, reads)
    return work_tree.WorkTree(parent_line, tuple(children), counts, unreadable, diagram)


class _TreeReads:
    """A Work page tree's GitHub reads, drawn from what the cache holds (#3739).

    A held value is used even when out of date, so opening a page never waits on GitHub
    for a map its webhooks keep current; only a read with nothing held waits. Each read is
    kept, with whether it was out of date, so ``_refresh_tree`` can re-read them afterwards
    and tell the open pages to redraw only when GitHub's answer moved."""

    def __init__(self, ref: WorkRef, issues: Any) -> None:
        self.ref = ref
        self.issues = issues
        self.drawn: dict[tuple, Any] = {}
        self.stale = False

    async def read(self, name: str, *args: Any) -> Any:
        held = getattr(self.issues, f"held_{name}", None)
        try:
            value, fresh = held(*args) if held is not None else (None, False)
        except Exception:  # noqa: BLE001 -- read from GitHub instead
            logger.debug(
                "tree %s: held %s%s unreadable", self.ref.work_id, name, args, exc_info=True
            )
            value, fresh = None, False
        if value is None:
            method = getattr(self.issues, name, None)
            try:
                value = await method(*args) if method is not None else None
            except Exception:  # noqa: BLE001 -- drawn without it, and said so
                logger.debug(
                    "tree %s: %s%s unreadable", self.ref.work_id, name, args, exc_info=True
                )
                value = None
            fresh = True
        self.drawn[(name, *args)] = value
        self.stale = self.stale or not fresh
        return value


def _refresh_tree(app: Any, ref: WorkRef, reads: _TreeReads) -> None:
    """Re-read a tree drawn from out-of-date rows in the background, one run per Work at a
    time, and redraw its open pages when GitHub's answer differs from what was drawn
    (#3739). The reads go through the shared cache's leases, so two pages or processes still
    fetch each row once."""
    running: dict[str, asyncio.Task] | None = getattr(app.state, "work_tree_refreshes", None)
    if running is None:
        running = app.state.work_tree_refreshes = {}
    task = running.get(ref.work_id)
    if task is not None and not task.done():
        return

    async def again(name: str, *args: Any) -> Any:
        method = getattr(reads.issues, name, None)
        try:
            return await method(*args) if method is not None else None
        except Exception:  # noqa: BLE001 -- the next page build reads it again
            logger.debug("tree %s: %s%s refresh failed", ref.work_id, name, args, exc_info=True)
            return None

    async def run() -> None:
        drawn = list(reads.drawn.items())
        values = await asyncio.gather(*(again(*read) for read, _value in drawn))
        if any(
            value is not None and value != held
            for value, (_read, held) in zip(values, drawn, strict=True)
        ):
            wayfinder_map.bump_generation()
            app.state.app_state.bus.publish(WORKS_TOPIC)

    def forget(done: asyncio.Task) -> None:
        if running.get(ref.work_id) is done:
            del running[ref.work_id]

    running[ref.work_id] = asyncio.get_running_loop().create_task(run())
    running[ref.work_id].add_done_callback(forget)


async def _wayfinder_diagram(
    app: Any,
    ref: WorkRef,
    record: Any,
    items: Sequence[dict],
    rows: Mapping[str, FrontRow],
    *,
    reads: _TreeReads | None = None,
) -> wayfinder_map.Diagram | None:
    """A wayfinder map's diagram (#3627): its sub-issues as boxes, their "blocked by" as
    arrows, its body's fog and destination. None unless the issue is labelled a map and has
    sub-issues. Each ticket's title, state and type come from its own cached issue record,
    which webhooks keep current, before the sub-issue list's copy; the body and each ticket's
    dependencies are cached reads, marked out of date by their webhooks and drawn from what
    is held meanwhile (``_TreeReads``, #3739). Nothing here polls."""
    labels = record.get("labels") if isinstance(record, Mapping) else None
    if not items or not wayfinder_map.is_map(labels if isinstance(labels, list) else ()):
        return None
    issues = getattr(app.state, "issues", None)
    refs = [WorkRef.from_issue(IssueRef(item["repository"], item["number"])) for item in items]
    read = (reads or _TreeReads(ref, issues)).read
    body, *blockers = await asyncio.gather(
        read("issue_body", ref.repository, ref.number),
        *(read("blocked_by", child.repository, child.number) for child in refs),
    )
    lookup = getattr(issues, "cached_issues", None)
    try:
        held = lookup([(child.repository, child.number) for child in refs]) if lookup else {}
    except Exception:  # noqa: BLE001 -- the sub-issue list's own copy is drawn instead
        held = {}
    def closed(child: WorkRef, item: Mapping) -> bool:
        own = held.get((child.repository, child.number)) or {}
        return str(own.get("state") or item.get("state") or "").lower() == "closed"

    def key_of(repository: str, number: int) -> str:
        return WorkRef.from_issue(IssueRef(repository, number)).work_id

    mine = {child.work_id for child in refs}
    shut = {child.work_id for child, item in zip(refs, items, strict=True) if closed(child, item)}
    tickets = []
    for child, item, blocked in zip(refs, items, blockers, strict=True):
        own = held.get((child.repository, child.number)) or {}
        row = rows.get(child.work_id)
        blocked_by = wayfinder_map.blocked_keys(blocked or (), key_of)
        # Not started and still blocked by an open ticket of this map: that ticket's to
        # finish, not the owner's to start (#3736).
        waiting_on = [key for key in blocked_by if key in mine and key not in shut]
        unstarted = row is None or (
            row.attention is not None and row.attention.category == UNSTARTED
        )
        state = wayfinder_map.ticket_state(
            closed=child.work_id in shut,
            section=row.section if row is not None else None,
            waits=bool(row.waits) if row is not None else False,
            held=bool(waiting_on) and unstarted,
        )
        # The ticket's own record first; the sub-issue list's copy for one never read (#3627).
        own_labels = own.get("labels") if "labels" in own else item.get("labels")
        tickets.append(
            wayfinder_map.Ticket(
                key=child.work_id,
                label=work_tree.label_in(child, ref),
                title=str(own.get("title") or item.get("title") or work_tree.label_in(child, ref)),
                href=work_href(child.work_id),
                state=state,
                kind=wayfinder_map.ticket_kind(own_labels if isinstance(own_labels, list) else ()),
                status=(row.line or row.status.headline) if row is not None else "",
                blocked_by=blocked_by,
                asks=(
                    wayfinder_map.question(row)
                    if row is not None and state == wayfinder_map.NEEDS_YOU
                    else None
                ),
            )
        )
    control = getattr(app.state, "control_db", None)
    diagram = wayfinder_map.layout(
        tickets,
        fog_items=wayfinder_map.fog(body or ""),
        goal=wayfinder_map.destination(body or ""),
        moved=control.wayfinder_layout(ref.work_id) if control is not None else None,
    )
    # "Waiting for you" starts with the question the map's front-page row leads with (#3696).
    plan = map_for(wayfinder_maps(getattr(_overview(app), "front", None)), ref.work_id)
    if plan is not None and diagram.questions:
        place = {item.work_id: index for index, item in enumerate(plan.questions)}
        ordered = sorted(diagram.questions, key=lambda item: place.get(item.work_id, len(place)))
        diagram = replace(diagram, questions=tuple(ordered))
    unread = body is None or any(blocked is None for blocked in blockers)
    return replace(diagram, partial=unread) if unread else diagram


def _overview(app: Any) -> Any:
    return getattr(getattr(app.state, "work_overview", None), "overview", None)


# --- rendering ------------------------------------------------------------------------------

# The Work page's sections, in the order the page shows them (#2218): the status story, the
# Work-level message box, the milestones, the result, the resolution (#2328), the workspace,
# then the collapsed Runs, Chats and technical details.
WORK_SECTIONS = (
    "status",
    "tree",
    "message",
    "lifecycle",
    "result",
    "resolution",
    "workspace",
    "chats",
    "inspector",
)


# What each Work page section reads of the Work (#3228), from ``work_detail.html``, so a live
# build renders only the sections whose inputs moved: a review verdict moves the status, the
# milestones, the result and the details, but not the resolution, the workspace or the chats.
# ``line`` is the status as of the build, which also carries what moves with the clock. A
# section that starts reading more of the Work must say so here, or it shows stale markup.
SECTION_INPUTS: dict[str, Callable[[WorkDetail, Any], tuple]] = {
    # The status reads the whole card through its controls, and moves with every chat.
    "status": lambda d, line: (
        d.card, line, d.hidden, d.last_action, d.message_target, d.custom_title, d.chats,
        # Its controls and notes, as rendered: derived from the fields above today, named
        # here so a control that starts reading something else cannot go stale.
        d.attention, d.buttons, d.finish_control, d.pause_control, d.star_control,
        d.pause_refusal, d.last_action_followed, d.bring_back, d.merge_approval_control,
        d.approval, d.approval_cleared, d.diagram,
    ),
    # The issue's parent and sub-issues (#3622), each line as the front page last drew it.
    "tree": lambda d, line: (d.tree,),
    "message": lambda d, line: (d.card, line, d.message_target, d.chats),
    "lifecycle": lambda d, line: (d.card.derived, line.milestones),
    "result": lambda d, line: (
        d.card.outputs, d.card.staging, d.card.verification
    ),
    "resolution": lambda d, line: (d.resolution, d.resolution_behind),
    "workspace": lambda d, line: (
        d.card.work_id, d.card.workspace_note, d.card.workspace, d.workspace, d.workspace_rows
    ),
    "chats": lambda d, line: (d.card.work_id, d.card.derived, d.chats, d.unplaced),
    "inspector": lambda d, line: (
        d.card.work_id, d.chats, d.verifications, d.workspace,
    ),
}


# Which section shows which pre-rendered fragment (``work_detail.html``); no other reads one.
SECTION_FRAGMENTS: dict[str, tuple[str, ...]] = {
    "inspector": ("workspace_html",),
}


def render_work_sections(
    templates: Any,
    detail: WorkDetail,
    *,
    names: Sequence[str] = WORK_SECTIONS,
    previous: Mapping[str, tuple[tuple, str]] | None = None,
) -> list[tuple[str, str]]:
    """The Work page's sections in ``names``, each rendered on its own.

    With ``previous`` (``render_changed_sections``), a section whose inputs are as they were
    keeps its markup and is not rendered again.
    """
    return [
        (name, html)
        for name, (_inputs, html) in render_changed_sections(
            templates, detail, names=names, previous=previous or {}
        ).items()
    ]


def render_changed_sections(
    templates: Any,
    detail: WorkDetail,
    *,
    names: Sequence[str] = WORK_SECTIONS,
    previous: Mapping[str, tuple[tuple, str]],
) -> dict[str, tuple[tuple, str]]:
    """``name -> (inputs, html)``, rendering only the sections whose ``SECTION_INPUTS`` moved
    since ``previous`` (#3228). Counted as ``work_counter.WORK_SECTION`` per render."""
    from .work_workspace_view import render_work_workspace

    line = detail.line
    template = templates.get_template("partials/work_detail.html")
    # The workspace fragment, built only for the section that shows it (#3228): a status or
    # chats change does not render it.
    builders: dict[str, Callable[[], str]] = {
        "workspace_html": lambda: (
            render_work_workspace(templates, detail.workspace, show_verifications=False)
            if detail.workspace is not None
            else ""
        ),
    }
    built: dict[str, str] = {}

    def extra(name: str) -> dict[str, Any]:
        fragments = {key: "" for key in builders}
        for key in SECTION_FRAGMENTS.get(name, ()):
            if key not in built:
                work_counter.count(work_counter.WORK_FRAGMENT)
                built[key] = builders[key]()
            fragments[key] = built[key]
        return {"status": line, **fragments}

    rendered: dict[str, tuple[tuple, str]] = {}
    for name in names:
        inputs = SECTION_INPUTS[name](detail, line)
        kept = previous.get(name)
        if kept is not None and kept[0] == inputs:
            rendered[name] = kept
            continue
        work_counter.count(work_counter.WORK_SECTION)
        rendered[name] = (inputs, template.render(section=name, detail=detail, **extra(name)))
    return rendered


# The front page's live parts (#2219), each its own stream event and swap target, in the order
# the page lists them (#3818): the Work sections swap keyed by Work, the board line and the
# other activity whole. ``needs``, ``stuck``, ``release`` and ``cleanup`` are the Needs you
# groups at their places on the page (``work_attention.FRONT_PLACES``).
FRONT_ROW_PARTS = frozenset(
    {
        "needs", "active", "stuck", "waiting", "release", "live", "cleanup", "finished",
        "paused", "stopped", "hidden",
    }
)
FRONT_PARTS = (
    "summary", "needs", "release", "stuck", "active", "cleanup", "waiting", "live", "finished",
    "paused", "stopped", "board",
)
# The ``/events?view=`` values that show Works: the front page, ``/works?view=hidden`` and the
# default ``/works`` list. Every other page's stream neither subscribes to ``works`` nor sends it.
WORKS_FRONT_VIEW = "works"
WORKS_HIDDEN_VIEW = "works-hidden"
WORKS_INDEX_VIEW = "works-index"
WORKS_VIEWS = frozenset({WORKS_FRONT_VIEW, WORKS_HIDDEN_VIEW, WORKS_INDEX_VIEW})


class RowRenders:
    """Each front-page and ``/works`` row rendered once per revision, for every tab (#3228).

    A pass that changed one Work of a hundred re-renders that one row: the others are served
    from here. The key is everything the row's macro reads besides its card's facts, which
    ``FrontRow.revision`` already covers. Bounded, like ``SharedFragments``: an entry
    nobody has asked for in a while is the first to go.
    """

    MAXSIZE = 1000

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple, Markup] = OrderedDict()

    def get(self, key: tuple, build: Callable[[], Any]) -> Markup:
        html = self._entries.get(key)
        if html is None:
            work_counter.count(work_counter.WORK_ROW)
            html = Markup(str(build()))
            self._entries[key] = html
            while len(self._entries) > self.MAXSIZE:
                self._entries.popitem(last=False)
        else:
            self._entries.move_to_end(key)
        return html


def row_renders(app_state: Any) -> RowRenders:
    """The web process's one ``RowRenders``, kept on ``app.state`` (or a test's stand-in)."""
    renders = getattr(app_state, "work_row_renders", None)
    if renders is None:
        renders = RowRenders()
        try:
            app_state.work_row_renders = renders
        except AttributeError:  # a stand-in that cannot hold it renders every row
            pass
    return renders


def row_stub(work_id: str, revision: str, *, tag: str = "article") -> Markup:
    """A row the reader already shows at this revision: ``keyed-works`` keeps its own node.

    Sent in place of the row on a stream that already sent it, so a push carries only the
    rows that changed, and still says where every row goes.
    """
    return Markup(
        '<{0} hidden data-work-same data-work-id="{1}" data-work-rev="{2}"></{0}>'
    ).format(Markup(tag), work_id, revision)


def row_sig(work_id: str, revision: str) -> str:
    """A row's short fingerprint: 32-bit FNV-1a of ``"<work_id> <revision>"``, as hex.

    ``work_list.js`` (``rowSig``) sends one per row a page shows when its stream connects
    (``?sig=``), so the stream need not send those rows again (#3228). Nothing is trusted from
    it: a fingerprint names a row the reader claims to hold, and a wrong claim only means that
    row arrives as a placeholder, which the page answers by reconnecting without it.
    """
    value = 0x811C9DC5
    for char in f"{work_id} {revision}":
        value = ((value ^ ord(char)) * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


class SentRows(dict):
    """A stream's record of the rows its reader holds: ``(part, work_id) -> revision`` for what
    it sent, and ``held``, the fingerprints the reader named when it connected (``row_sig``)."""

    MAX_SIGS = 400

    def __init__(self, sig: str = "") -> None:
        super().__init__()
        self.held = set(part for part in sig.split(".")[: self.MAX_SIGS] if len(part) == 8)


def mark_sent(sent: dict, shown: set[str], part: str, work_id: str, revision: str) -> bool:
    """Record a row a stream is sending; True when its reader already holds it, so it can go
    as a ``row_stub``: it sent that revision before, or the reader named it on connecting. A
    Work's second row in one part always goes whole: its first row takes the node the page
    holds, and a placeholder would find nothing left to keep."""
    slot = (part, work_id)
    held = sent.get(slot) == revision
    if slot not in sent and row_sig(work_id, revision) in getattr(sent, "held", ()):
        held = True
    held = held and work_id not in shown
    shown.add(work_id)
    sent[slot] = revision
    return held


def forget_unsent(sent: dict, part: str, html: str) -> None:
    """Drop the rows ``part`` no longer lists from a stream's ``sent``: the page removes them,
    so one coming back later must be sent whole, not as a placeholder."""
    held = getattr(sent, "held", None)
    for slot in [slot for slot in sent if slot[0] == part]:
        if f'data-work-id="{slot[1]}"' not in html:
            if held:
                held.discard(row_sig(slot[1], sent[slot]))
            del sent[slot]


def front_row_html(
    overview: WorkOverview,
    *,
    renders: RowRenders | None,
    part: str = "",
    sent: dict | None = None,
) -> Callable[..., Markup]:
    """The ``row_html`` ``work_front.html`` calls for each row (its ``put`` macro).

    ``renders`` reuses rows built for an earlier pass; ``sent`` is a stream's record of the
    rows it already sent its reader, ``(part, work_id) -> revision``, and a row it holds at
    that revision goes as a ``row_stub`` (#3228).
    """
    one_repository = overview.front.one_repository
    shown: set[str] = set()

    def row_html(macro: Any, item: Any, **flags: Any) -> Markup:
        work_id = item.card.work_id
        revision = item.revision
        if sent is not None:
            if mark_sent(sent, shown, part, work_id, revision):
                return row_stub(work_id, revision)
        if renders is None:
            return Markup(macro(item, **flags))
        key = (
            macro.name,
            work_id,
            revision,
            item.section,
            item.reason,
            repr(item.attention),
            repr(item.status),
            one_repository,
            tuple(sorted(flags.items())),
        )
        return renders.get(key, lambda: macro(item, **flags))

    return row_html


def render_work_front(
    templates: Any,
    overview: WorkOverview,
    part: str,
    *,
    renders: RowRenders | None = None,
    sent: dict | None = None,
) -> str:
    """One front-page part; ``renders`` and ``sent`` as in ``front_row_html``."""
    html = templates.get_template("partials/work_front.html").render(
        work_overview=overview,
        part=part,
        row_html=front_row_html(overview, renders=renders, part=part, sent=sent),
    )
    if sent is not None:
        forget_unsent(sent, part, html)
    return html


def render_work_other(templates: Any, overview: WorkOverview) -> str:
    return templates.get_template("partials/work_other.html").render(work_overview=overview)


def render_work_other_decks(templates: Any, overview: WorkOverview) -> str:
    return templates.get_template("partials/work_other_decks.html").render(work_overview=overview)


def other_deck_work(app: Any, ref: WorkRef) -> OtherDeckWork | None:
    """The issue as another deck's Work, read fresh from the cached lists, or None (#3834).

    The Work page and Take over ask this rather than the last overview, so a press made
    after the issue came to this deck is refused instead of reassigning it again.
    """
    _, backlog = _board_and_backlog(app)
    found = other_deck_works(
        backlog, now=datetime.now(UTC), mine=getattr(app.state, "deck_claims", None)
    )
    return next((item for item in found if item.work_id == ref.work_id), None)
