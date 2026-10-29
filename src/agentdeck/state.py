"""AppState: single in-memory source of truth for sessions and usage.

Mutations publish coarse-grained topics to the EventBus:
- "sessions" — the session list changed (SSE re-renders the whole list)
- "usage"    — account limits or host resource usage changed
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Container, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from . import job_view, work_counter
from .chat_identity import (
    ChatIdentity,
    ChatLinkConflict,
    LegStanding,
    published_chat_reference,
    working_leg,
)
from .context_policy import ContextAction
from .events import EventBus
from .host_stats import HostStats
from .live_window import LiveWindow
from .meta_chat import reset_leg_reasons
from .models import (
    MUTE_LEASE_POLICIES,
    MUTE_SOURCE_BOSS,
    STALL_S,
    AttentionReport,
    Capability,
    GeneratedTitle,
    HeadReviewLaunch,
    IssueRef,
    MuteLease,
    PollerReviewVerdict,
    PullRequestRef,
    Session,
    SessionStatus,
    UsageSnapshot,
)
from .questions import OpenQuestion, event_question, trailing_question
from .transcript_rollbacks import HIDDEN
from .work import (
    AMBIGUOUS,
    WorkBinding,
    WorkChatFacts,
    WorkEvidence,
    WorkProjection,
    WorkRef,
    build_work_projection,
)
from .work_backfill import (
    BACKFILL_NAME,
    CUTOVER_NAME,
    DELEGATION_NONE,
    DELEGATION_RUN,
    UNRESOLVED_PARENT,
    LegacyChat,
    plan_backfill,
)

if TYPE_CHECKING:
    from .db import ChatSnapshot, Db

log = logging.getLogger(__name__)

_STATUS_ORDER = {SessionStatus.LIVE: 0, SessionStatus.IDLE: 1, SessionStatus.REMOTE: 2}
_KANBAN_MODE_RE = re.compile(r"\s+\((review|merge-fix|merge-arm|resume)\)$", re.IGNORECASE)
OPERATOR_PRESENCE_IDLE_AFTER_S = 300.0
# Every chat page listens on its own chat's topic (#2865); an announcement that
# names no chat publishes this one, which every chat page also listens on.
ALL_CHATS_TOPIC = "chats"
# A commit's review verdict or a review launch moved: the head-review chips (#1127).
REVIEWS_TOPIC = "reviews"


def chat_topic(chat_key: str) -> str:
    """The bus topic one chat's changes are published on (ADR 0007)."""
    return f"chat:{chat_key}"


SESSION_SCAN_MAX_AGE_S = 1800.0
# How long a side chat started from a Work's page may take to be named before its pending
# links are dropped (#2388): a start that failed after it was accepted is never named.
SIDE_CHAT_PENDING_MAX_AGE = timedelta(hours=24)
# How long the deck will call a chat working purely on having handed it something,
# with the chat itself having produced nothing at all since (#2003).
#
# The stamp exists to cover one small window: the deck starts a handoff, and the
# chat has not yet shown up as thinking. That is seconds. Any chat that does
# *anything* — a token, a tool call, a progress tick — moves past the stamp and
# clears it on the next read, so this ceiling can only ever be reached by a chat
# that has gone completely silent since the deck wrote to it. That is not a
# working chat, it is a stuck one, and the right answer for a stuck one is to
# stop calling it working so Deckhand raises it for attention.
#
# Ten minutes is far past any legitimate handoff window and well short of a
# person noticing. It exists because six separate races were each able to leave
# one stamp behind, and an unbounded stamp turns any one of them into a chat
# pinned under Working for good.
WORKING_STAMP_MAX_AGE_S = 600.0


def _text_digest(text: str) -> str:
    """Hash one delivered user turn, so a scan can recognise a turn it saw."""
    return hashlib.sha256(text.encode()).hexdigest()


def copy_session(session: Session, /, **changes: object) -> Session:
    """One shallow copy of a ``Session``, with ``changes`` applied.

    Same result as ``dataclasses.replace``, for about a third of the work.
    ``replace`` rebuilds the object through ``__init__``, which means reading
    all forty-odd fields back off the original one ``getattr`` at a time — some
    17 µs a call on this host. The display projection below makes one of these
    per visible card and is rebuilt by every SSE tick, every Deckhand refresh
    and every page render, so that copy became the single largest CPU cost in
    the web process; opening a chat then had to queue behind it (#1088).

    ``Session`` is a plain mutable dataclass with no ``__post_init__`` and no
    non-init fields, so a shallow copy plus the changed attributes is the same
    object ``replace`` would have built. Both share their field values with the
    original, exactly as before.

    Copied through ``__dict__`` rather than ``copy.copy``: with no ``__copy__``
    of its own, ``copy.copy`` goes the long way round through ``__reduce_ex__``
    and ``copy._reconstruct``, which was still several percent of the web
    worker's event-loop thread — the liveness sweep takes one of these per
    session as its change-detection pre-image (#1310). ``Session`` has no
    ``__slots__``, so its instance dictionary is the whole of its state.
    """
    clone = object.__new__(Session)
    clone.__dict__.update(session.__dict__)
    for name, value in changes.items():
        setattr(clone, name, value)
    return clone


def generated_display_title(session: Session, semantic_title: str) -> str:
    """Attach stable project and issue identity to a generated semantic title."""
    return _generated_display_title(
        semantic_title, session.project_name, session.primary_issue, session.title
    )


@lru_cache(maxsize=4096)
def _generated_display_title(
    semantic_title: str,
    project_name: str | None,
    issue: IssueRef | None,
    session_title: str | None,
) -> str:
    """The formatting alone, memoized on the four inputs that decide it.

    Two regexes over every session, rebuilt by every refresh pass and every SSE
    tick even though none of the inputs had moved (#1303). The inputs are all
    hashable and immutable, so the memo cannot answer for a session whose title,
    project or issue has since changed — it just stops re-deriving the ones that
    have not.
    """
    if project_name is not None:
        semantic_title = re.sub(
            rf"^{re.escape(project_name)}\s*(?:·|[-:])\s*",
            "",
            semantic_title,
            count=1,
            flags=re.IGNORECASE,
        )
    if issue is not None:
        suffix_match = _KANBAN_MODE_RE.search(session_title or "")
        suffix = suffix_match.group(0) if suffix_match else ""
        return f"{issue.repository.rsplit('/', 1)[-1]}#{issue.number} · {semantic_title}{suffix}"
    if project_name is not None:
        return f"{project_name} · {semantic_title}"
    return semantic_title


@dataclass(frozen=True)
class _PresentationParentGraph:
    """One parent graph retained by the existing presentation cache."""

    signature: object
    parent_by_key: Mapping[str, str | None]


@dataclass(frozen=True)
class SessionPresentation:
    """One stable, request-scoped projection of the visible session graph.

    ``display()`` deliberately reaches one session wider than the graph: a chat
    kept out of the list still has a page of its own to render (#876).
    """

    visible: tuple[Session, ...]
    top_level: tuple[Session, ...]
    children_of: Mapping[str, tuple[Session, ...]]
    working_count: int
    _display_by_key: Mapping[str, Session]
    _working_through_subagents: frozenset[str]
    _visible_parent_graph: _PresentationParentGraph

    def display(self, session: Session) -> Session:
        """Return this snapshot's effective display copy of ``session``."""
        return self._display_by_key.get(session.key, session)

    def has_working_subagent(self, session: Session) -> bool:
        """Whether embedded or nested subagent work makes ``session`` active."""
        return session.key in self._working_through_subagents

    def active_child_sessions(self, session: Session) -> tuple[Session, ...]:
        """Active child Sessions not already represented as embedded progress.

        Codex native subagents can appear through both ``Session.subagents`` and
        the relationship graph. Delegated Sessions have only the graph entry.
        Keeping this projection here gives session detail and its SSE fragment
        the same authoritative children as the dashboard without duplicating a
        native subagent row.
        """
        embedded_ids = {agent.agent_id for agent in session.subagents}
        return tuple(
            child
            for child in self.children_of.get(session.key, ())
            if child.thinking and child.session_id not in embedded_ids
        )


@dataclass(frozen=True)
class OperatorPresence:
    """One read of the single operator's dashboard-input recency."""

    status: Literal["present", "away"]
    last_input_at: datetime | None
    idle_for_s: float | None


class LetGoSessionChanged(ValueError):
    """An incremental scan named a chat the live window let go (#2841): a full scan,
    not an error, brings it back."""


class AppState:
    def __init__(
        self,
        bus: EventBus | None = None,
        db: Db | None = None,
        budget_db: Db | None = None,
    ):
        self.bus = bus or EventBus()
        self.db = db
        self.budget_db = budget_db if budget_db is not None else db
        self.sessions: dict[str, Session] = {}
        # Which chats stay live in memory (#2841). Off until the app sets the window
        # from config, so a bare state keeps every chat a scan gives it.
        self.live_window = LiveWindow()
        # Chats a full scan let go because their Work is finished.
        self._let_go: set[str] = set()
        # Top-level chats that have derived work from a descendant in the current
        # orchestration wave. Kept until both the descendants and the parent's own
        # turn are idle, so a final child completing while the parent is composing
        # its response still produces one recovery edge after that response ends.
        self._orchestrating_sessions: set[str] = set()
        self.session_scan_revisions: dict[str, int] = {}
        # Bumped by every ``sessions_changed`` announcement; see that method.
        self.sessions_revision: int = 0
        # Per-chat change numbers (#2865): ``_session_revisions`` moves for the
        # chats an announcement names, ``_sessions_epoch`` for one that names none.
        self._sessions_epoch: int = 0
        self._session_revisions: dict[str, int] = {}
        # Each chat's ``visible_sessions`` row and what it was built from (#2865).
        self._visible_cache: dict[tuple[str, ...], tuple] = {}
        # Each chat's display copy and what it was built from (#2865).
        self._display_cache: dict[str, tuple[Session, tuple[int, int], dict, Session]] = {}
        # Wall-clock time the cached presentation's clock-derived activity next moves.
        self._session_presentation_expires_at: datetime | None = None
        self._activity_deadline: datetime | None = None
        self._session_presentation_cache: SessionPresentation | None = None
        # What the cached presentation was built from. Reuse is keyed on these
        # rather than on elapsed time: the revision covers every in-process
        # change, and the chat snapshot covers the one input another process
        # writes — the persistent runtime links chat legs straight into the
        # shared database, which no in-process invalidation can see.
        self._session_presentation_revision: int | None = None
        self._session_presentation_chats: ChatSnapshot | None = None
        self._session_presentation_chat_revision: int | None = None
        # The work-link rows ``work_projection`` read, keyed on the database's change
        # counter (``Db.change_counter``, #2865).
        self._work_link_rows: tuple | None = None
        # The last Work projection and exactly what it was built from (#2654).
        self._work_projection_cache: tuple[tuple, WorkProjection] | None = None
        self.session_scan_times: dict[str, datetime] = {}
        self.usage: dict[str, UsageSnapshot] = {}
        # account key -> why its meter credential was refused, while it is refused
        # (#758). Set by the poller, cleared by the next successful read; the
        # render turns it into the sign-in-again state.
        self.usage_credential_errors: dict[str, str] = {}
        # A usage snapshot is shown as "stale" only once it is older than this
        # (seconds) — data age, not a single failed poll (issue #6). Resolved
        # from the poll interval at app startup; this is the safe default.
        self.usage_stale_after_s: float = 300.0
        self.host_stats: HostStats | None = None
        # Commands waiting on the Mac for approval, for the top bar's icon (#2864).
        self.mac_waiting = 0
        # Last read of the runtime's heavy-command queue (#1301). Web-process
        # local and rebuilt by the collector within a tick of a restart; the
        # runtime owns the durable state.
        self._job_queue: job_view.JobQueueView = job_view.EMPTY
        # Input activity is intentionally web-process-local. A restart defaults
        # to away until the single operator next touches the dashboard.
        self.last_operator_input_at: datetime | None = None
        self.transcript_offsets: dict[str, tuple[int, int]] = {}  # v0.2: (byte_offset, seq)
        # child_key -> delegating parent_key, discovered from delegation markers
        # in each provider's transcripts. Cross-provider (a Claude chat that
        # delegated a Codex session, etc.) is why this lives in state rather than
        # on the Session: the child and parent are produced by different scans.
        self.delegation_parents: dict[str, str] = {}
        # child_key -> raw parent session_id, recorded authoritatively at
        # delegation time (the invoking session captured from
        # CLAUDE_CODE_SESSION_ID, etc.). We store the raw id, not a resolved
        # key, and resolve it against the currently-visible sessions at
        # render time: the parent may not be scanned in yet when the child
        # starts, and lazy resolution self-heals that race. DB-backed and kept
        # separate from the scan-discovered ``delegation_parents`` so a provider
        # scan's set_delegation_parents() can never clear it; survives restarts
        # and cross-provider cases the transcripts cannot express (a Claude chat
        # that delegated a Codex run).
        self.recorded_delegation_parents: dict[str, str] = (
            db.load_delegation_parents() if db else {}
        )
        self.delegated_session_keys = db.load_delegated_sessions() if db else set()
        # The first start under the saved-outcome rule (#2207): kept once, never moved.
        # Pinned to this start, so a write that fails now and succeeds on a retry still
        # places every delegation this process records after it.
        self._cutover_at = datetime.now(UTC).isoformat()
        self._delegation_cutover()
        # Per-chat boss-engagement window. Bumped when the boss writes into that
        # chat, which is what lets a mute lease's spent hard cap be re-earned.
        # Last boss-turn text hash seen per chat, so a scan opens one window per
        # turn rather than one per scan (#475).
        self._last_boss_turn_digest: dict[str, str] = {}
        self.boss_turn_windows: dict[str, str] = (
            db.load_boss_turn_windows() if db else {}
        )
        # Chats the deck is currently attending (#823). In-memory on purpose: a
        # restart is one of the uncertainties a mute lease must fail open on, and
        # an empty map is exactly that. See the mute-lease section below.
        self.mute_leases: dict[str, MuteLease] = {}
        # Chats whose hard cap is spent, and the window it was spent in — keyed
        # by (chat, source), because the sources have separate caps and one
        # spending its own must not lock the others out of the same chat.
        # Cleared implicitly by the boss opening a new window.
        self._mute_cap_spent: dict[tuple[str, str], str] = {}
        self.generated_titles: dict[str, GeneratedTitle] = (
            db.load_generated_titles() if db else {}
        )
        self.session_pull_requests: dict[str, tuple[PullRequestRef, ...]] = (
            db.load_session_pull_requests() if db else {}
        )
        self.session_issues: dict[str, tuple[IssueRef, ...]] = (
            db.load_session_issues() if db else {}
        )
        # Work inventory read path for issues registered before #2140.
        self._record_works(ref for refs in self.session_issues.values() for ref in refs)
        self.attention_reports: dict[str, AttentionReport] = (
            db.load_session_attention_reports() if db else {}
        )
        # When the deck last handed this chat something to do — a user message, a
        # worker instruction, a nudge. Every delivery path records it here (#1450).
        #
        # A chat the deck has just given work to is working, whatever its
        # transcript says yet. Inferring that from the provider's own files means
        # waiting up to a scan interval to notice, and a turn that starts and
        # finishes inside that window is never seen working at all — so the
        # working → not-working edge that Deckhand runs on is silently lost. The
        # deck caused this turn, so it need not go looking for evidence that it
        # began. Kept in memory: a restart resolves to "not sent", which the
        # observed state then corrects, and a stale stamp must never outlive the
        # process that could reconcile it.
        self.session_sent_at: dict[str, datetime] = {}
        load_session_last_opened = (
            getattr(db, "load_session_last_opened", None) if db else None
        )
        self.session_last_opened: dict[str, datetime] = (
            load_session_last_opened() if load_session_last_opened is not None else {}
        )
        # Transcript lines people rolled out of their chats (#3474); every transcript read
        # leaves them out through this one registry.
        load_rollbacks = getattr(db, "load_transcript_rollbacks", None) if db else None
        HIDDEN.replace_all(load_rollbacks() if load_rollbacks is not None else ())
        # What is known about each reviewed commit, keyed by (lowercased repo, sha).
        # A cached ``None`` means "asked, nothing recorded": the session chip (#1127)
        # asks on every stream tick, and a table read per tick per open page would be
        # paid for a question whose answer only changes when a verdict is recorded —
        # and every writer goes through ``record_review_verdict``.
        self._review_verdicts: dict[tuple[str, str], PollerReviewVerdict | None] = {}
        # Reviews this deck started from a session chip, by the same key.
        self.head_review_launches: dict[tuple[str, str], HeadReviewLaunch] = {}
        self.open_questions: dict[str, OpenQuestion] = (
            db.load_open_questions() if db else {}
        )
        self.session_tokens: dict[str, str] = (
            self.budget_db.load_session_tokens() if self.budget_db else {}
        )
        # What `follow_runtime_relations` last saw of the rows the runtime writes itself.
        self._runtime_relation_stamp: tuple[int, ...] | None = None

    # --- invalidations -------------------------------------------------

    def sessions_changed(self, keys: Iterable[str] | None = None) -> None:
        """Announce that consumers should render the current session state.

        The revision is the same announcement in a form a loop can compare
        against its own last pass. Bus subscribers see a topic and rebuild; a
        poller that only wants to know *whether* anything moved since it last
        looked would otherwise have to rebuild the whole projection to find out
        it had not (#1310).

        ``keys`` names the chats that changed (ADR 0007, #2865). Each one, and
        every chat above it that shows its activity, gets its own change number
        and a ``chat:<id>`` publication, so an open chat page wakes for its own
        chat and nobody else's. ``sessions`` carries the same keys as its data
        for whole-deck readers. ``None`` still means "anything may have moved":
        every chat's view is rebuilt and every chat page wakes.
        """
        self.sessions_revision += 1
        if keys is None:
            self._sessions_epoch += 1
            self.bus.publish("sessions")
            self.bus.publish(ALL_CHATS_TOPIC)
            return
        changed = self._with_ancestors(keys)
        if not changed:
            return
        topics: set[str] = set()
        # One chat mapping read for the announcement, not one per chat (#1310).
        chats = self.chat_snapshot()
        for key in changed:
            self._session_revisions[key] = self._session_revisions.get(key, 0) + 1
            topics.add(chat_topic(self.chat_identity_key(key, chats)))
        self.bus.publish("sessions", frozenset(changed))
        for topic in sorted(topics):
            self.bus.publish(topic)

    def _with_ancestors(self, keys: Iterable[str]) -> set[str]:
        """``keys`` and every chat above them whose display can borrow their activity.

        A parent reads Working while a child works (``_effective_activity``), so a
        child's change is its parents' change too. The last presentation's parent
        graph is the authoritative one; the raw parent links cover a chat that graph
        has not seen yet.
        """
        cached = self._session_presentation_cache
        graph = cached._visible_parent_graph.parent_by_key if cached is not None else {}
        out: set[str] = set()
        frontier = [key for key in keys if key]
        while frontier:
            key = frontier.pop()
            if key in out:
                continue
            out.add(key)
            session = self.sessions.get(key)
            for parent in (
                graph.get(key),
                session.parent_session_key if session is not None else None,
                self.delegation_parents.get(key),
            ):
                if parent and parent not in out:
                    frontier.append(parent)
        return out

    def presentation_deadline(self) -> datetime | None:
        """When the cached presentation's clock-derived activity next moves, if ever.

        A mute lease running out moves a row too (its ``muted`` flag), so its end is one
        of these deadlines (#2865).
        """
        ends = [
            at
            for at in (self._session_presentation_expires_at, self.mute_lease_deadline())
            if at is not None
        ]
        return min(ends, default=None)

    def mute_lease_deadline(self) -> datetime | None:
        """When the first held mute lease ends; ``expire_mute_leases`` is due then (#2865).

        No clock re-reads leases any more, so whoever sleeps on the deck wakes at this.
        A lapsed lease not yet dropped answers a time already past.
        """
        return min(
            (
                min(lease.expires_at, lease.hard_expires_at)
                if lease.hard_expires_at is not None
                else lease.expires_at
                for lease in self.mute_leases.values()
            ),
            default=None,
        )

    def session_revision(self, key: str) -> tuple[int, int]:
        """One chat's change number: moves whenever that chat's view may have (#2865)."""
        return (self._sessions_epoch, self._session_revisions.get(key, 0))

    def mark_session_opened(
        self, session_key: str, *, at: datetime | None = None
    ) -> datetime:
        """Advance one chat's durable read marker and tell Deckhand.

        Opening a chat is what clears its attention colour (#2599), so Deckhand
        re-reads the deck as soon as this lands rather than on its next tick.
        """
        at = at or datetime.now(UTC)
        session_key = self.chat_current_session_key(session_key)
        recorder = getattr(self.db, "record_session_opened", None) if self.db else None
        opened_at = recorder(session_key, at) if recorder is not None else at
        self.session_last_opened[session_key] = opened_at
        self.bus.publish("session_opened", session_key)
        return opened_at

    def chat_last_opened(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> datetime | None:
        """When any leg of this chat was last opened, or None if never."""
        return max(
            (
                self.session_last_opened[key]
                for key in self.chat_session_keys(session_key, chats)
                if key in self.session_last_opened
            ),
            default=None,
        )

    def usage_changed(self) -> None:
        """Announce that consumers should render the current usage state."""
        self.bus.publish("usage")

    def deployment_changed(self) -> None:
        """Announce that the deploy state files moved (release or refusal, #786)."""
        self.bus.publish("deployment")

    def set_job_queue(self, view: job_view.JobQueueView) -> None:
        """Store the runtime's queue, publishing only on a real change.

        The collector polls every couple of seconds and the queue is idle most
        of the day; publishing every tick would push a topbar fragment at every
        open tab for nothing. An unfinished job is also effective session work,
        so a real change advances the session projection and wakes Deckhand.
        """
        if view == self._job_queue:
            return
        before = set(self._job_queue.active_rows)
        after = set(view.active_rows)
        self._job_queue = view
        self.bus.publish("jobs")
        # Only the chats whose own jobs moved; the current leg of each is the one a
        # page is following (#2865).
        owners = {
            row.session_key for row in before ^ after if row.session_key is not None
        }
        self.sessions_changed(
            {self.chat_current_session_key(key) for key in owners} | owners
        )

    def job_queue(self) -> job_view.JobQueueView:
        """The queue as the topbar renders it, chats resolved at render time."""
        return self._job_queue.resolved(self._job_chat)

    def _job_chat(self, session_key: str) -> tuple[str | None, str | None]:
        """Title and deck path for the chat that submitted a job, if it is known.

        A job can outlive the chat that submitted it, and a submitting shell may
        have had no session at all, so both halves are optional.
        """
        key = self.chat_current_session_key(session_key)
        session = self.sessions.get(key) or self.sessions.get(session_key)
        if session is None:
            return None, None
        return session.display_title, self.chat_path_for_session(session.key)

    def _job_activity(self, visible_keys: Container[str]) -> dict[str, datetime]:
        """Latest unfinished-job transition for each visible owning chat.

        Jobs retain the concrete session key that submitted them. A durable chat
        may have switched provider legs since then, so activity follows the
        chat's current leg when it is visible and falls back to the submitted
        leg for pre-chat or partial-scan state.
        """
        activity: dict[str, datetime] = {}
        chats = self.chat_snapshot()
        for row in self._job_queue.active_rows:
            if row.session_key is None:
                continue
            current = self.chat_current_session_key(row.session_key, chats)
            key = current if current in visible_keys else row.session_key
            if key not in visible_keys:
                continue
            try:
                stamp = datetime.fromtimestamp(row.since, UTC)
            except (OverflowError, OSError, ValueError):
                continue
            activity[key] = max(stamp, activity.get(key, stamp))
        return activity

    # --- operator presence ---------------------------------------------

    def report_operator_input(self, at: datetime | None = None) -> OperatorPresence:
        """Record one client-reported input event using the server's clock."""
        at = at or datetime.now(UTC)
        if self.last_operator_input_at is None or at > self.last_operator_input_at:
            # The away->present edge has to be read *before* the update: afterwards
            # the boss always reads as present, so the transition is gone and a
            # subscriber watching only `presence` can never recover it.
            returning = self.operator_presence(at).status != "present"
            self.last_operator_input_at = at
            self.bus.publish("presence")
            if returning:
                self.bus.publish("boss_returned")
        return self.operator_presence(at)

    def operator_presence(self, now: datetime | None = None) -> OperatorPresence:
        """Derive present/away without a timer that can go stale overnight."""
        now = now or datetime.now(UTC)
        if self.last_operator_input_at is None:
            return OperatorPresence("away", None, None)
        idle_for_s = max(0.0, (now - self.last_operator_input_at).total_seconds())
        return OperatorPresence(
            "present" if idle_for_s < OPERATOR_PRESENCE_IDLE_AFTER_S else "away",
            self.last_operator_input_at,
            idle_for_s,
        )

    def assistant_changed(self) -> None:
        """Announce that consumers should render the current Deckhand state."""
        self.bus.publish("assistant")

    # --- session identity tokens --------------------------------------

    def bind_session_token(self, token: str, session_key: str) -> None:
        """Record which session a deck-minted token identifies.

        The token is handed to the agent in its own instructions before the
        provider has assigned an id, so binding happens as soon as the start
        call returns one. Nothing is published: this is identity, not display.

        A reserved token that turns out to name an impossible link — the source
        already moved on, or the session is already a leg somewhere else — binds
        anyway and leaves the successor standing on its own. Every automatic
        switch path reserves before provider I/O, and by the time the provider
        has answered the work is already running: refusing the identity here
        would lose a live session rather than refuse a fork. Linear-only is
        still honoured, because the refused link is simply not made (#1267).
        """
        if self.session_tokens.get(token) == session_key:
            return
        self.session_tokens[token] = session_key
        if self.budget_db is not None:
            try:
                chat = self.budget_db.record_session_token(token, session_key)
            except ChatLinkConflict as exc:
                log.warning(
                    "chat link refused for %s: %s — it stands as its own chat",
                    session_key,
                    exc,
                )
                self.budget_db.ensure_session_chats((session_key,))
                return
            if chat is not None and self.chat_identity_is_durable:
                changed = False
                for leg in chat.legs:
                    session = self.sessions.get(leg)
                    if session is not None and session.chat_id != chat.chat_id:
                        self.sessions[leg] = replace(session, chat_id=chat.chat_id)
                        changed = True
                if changed:
                    self.sessions_changed(chat.legs)

    def chat_for_session(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> ChatIdentity | None:
        if chats is not None:
            return chats.chat_for_session(session_key)
        reader = getattr(self.budget_db, "chat_for_session", None)
        return reader(session_key) if reader is not None else None

    def chat_by_id(self, chat_id: str) -> ChatIdentity | None:
        reader = getattr(self.budget_db, "chat_by_id", None)
        return reader(chat_id) if reader is not None else None

    def chat_snapshot(self) -> ChatSnapshot | None:
        """The whole chat mapping, for a caller about to walk every session.

        Chat identity is read once per session by the spend projection, the
        visible-session projection and session admission, and each of those runs
        over every session the deck holds several times a second. Reading it one
        session at a time made those loops the single largest consumer of the
        web worker's event loop (#1291). Pass the result back in through the
        ``chats`` argument of the readers below and the pass costs one lookup
        instead of one per session. Valid for that synchronous pass only.
        """
        reader = getattr(self.budget_db, "chat_snapshot", None)
        return reader() if reader is not None else None

    @property
    def chat_identity_is_durable(self) -> bool:
        """Whether a published chat id will still resolve after a web restart."""
        return self.budget_db is not None

    def chat_path_for_session(self, session_key: str) -> str:
        if not self.chat_identity_is_durable:
            return f"/sessions/{session_key}"
        chat = self.chat_for_session(session_key)
        return f"/chats/{chat.chat_id}" if chat else f"/sessions/{session_key}"

    def _leg_standings(
        self,
        keys: Iterable[str],
        override: Session | None = None,
        *,
        resolvable_only: bool = False,
    ) -> tuple[LegStanding, ...]:
        """What ``working_leg`` needs about each leg of a chat, chain order.

        A leg this deck has not scanned yet stands as "not live, never seen",
        which keeps the control-plane rule that a durable link is trusted before
        the scan that installs its session: nothing is live, so the chain's own
        end still wins (#1266). ``resolvable_only`` is for the readers that have
        to hand back a ``Session`` and would otherwise have nothing to return.

        ``override`` lets an admission pass judge the session it is holding by
        the status the provider scan just reported, rather than by the copy the
        store still has from the previous scan.
        """
        standings: list[LegStanding] = []
        for key in keys:
            leg = override if override is not None and override.key == key else None
            leg = leg or self.sessions.get(key)
            if leg is None:
                if resolvable_only:
                    continue
                standings.append(LegStanding(key, live=False))
                continue
            standings.append(
                LegStanding(
                    key,
                    leg.status is SessionStatus.LIVE,
                    leg.last_activity,
                    failed=leg.turn_failed,
                )
            )
        return tuple(standings)

    def current_session_for_chat(self, chat: ChatIdentity) -> Session | None:
        """The leg that is working, or None during the link-before-scan race."""
        current = working_leg(self._leg_standings(chat.legs, resolvable_only=True))
        return self.sessions[current.key] if current is not None else None

    def chat_reachable_leg_key(self, session_key: str) -> str | None:
        """The working leg a control write can actually be delivered to.

        ``chat_current_session_key`` may name a leg the deck has linked but not
        yet scanned, which is right for chat-owned state and useless to a write.
        ``None`` when no leg of the chat can be reached at all.
        """
        current = working_leg(
            self._leg_standings(self.chat_session_keys(session_key), resolvable_only=True)
        )
        return current.key if current is not None else None

    def chat_session_keys(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> tuple[str, ...]:
        """Resolved work-object members, oldest first; one key without identity."""
        chat = (
            self.chat_for_session(session_key, chats)
            if self.chat_identity_is_durable
            else None
        )
        return chat.legs if chat is not None else (session_key,)

    def chat_identity_key(self, session_key: str, chats: ChatSnapshot | None = None) -> str:
        """A name for the chat that does not change when it changes leg.

        Cooldowns and check identities are about the conversation, so keying
        them on the concrete leg would hand a chat a fresh window every time it
        moved account. Falls back to the session key when there is no durable
        chat identity, which is the pre-chat behaviour unchanged.
        """
        chat = (
            self.chat_for_session(session_key, chats)
            if self.chat_identity_is_durable
            else None
        )
        return chat.chat_id if chat is not None else session_key

    def chat_rollover_reasons(self, session_key: str) -> dict[str, str]:
        """Successor leg key → why this chat rolled over into it (#1667).

        Read from the frozen context-delivery ledger, which is the only durable
        record that a leg boundary was a rollover rather than a move to another
        account. Empty without a database, so the seam simply keeps its existing
        wording rather than the page failing over a decoration.
        """
        if self.db is None:
            return {}
        rows = self.db.list_chat_context_deliveries(self.chat_identity_key(session_key))
        # The deck chat's fresh starts after an idle hour are drawn as the same seam (#3403).
        return reset_leg_reasons(self, self.db) | {
            row.target_session_key: row.reason
            for row in rows
            if row.executed_action == ContextAction.ROLLOVER.value
            and row.target_session_key
            and row.reason
        }

    def chat_current_session_key(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> str:
        """The leg that is working, suitable for storing chat-owned state.

        This is the one authoritative answer to "which leg is this chat on"
        (#1501). The web layer's control writes, the poller's follow-ups, the
        transcript and every piece of chat-owned state read it, so none of them
        can decide differently from the others.
        """
        keys = self.chat_session_keys(session_key, chats)
        current = working_leg(self._leg_standings(keys))
        return current.key if current is not None else keys[-1]

    def chat_newest_leg_key(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> str:
        """The end of the chat's chain — the only leg a new leg may follow.

        Not the same question as ``chat_current_session_key``. That one asks
        which leg is *working*, which is what a delivery needs; this one asks
        where the chain ends, which is what growing the chain needs. They come
        apart whenever a chat is handed back to an account it used before, and
        a chat's chain is linear, so appending anywhere but the end is refused
        (#1582).
        """
        return self.chat_session_keys(session_key, chats)[-1]

    def current_leg_for_published_url(self, url: str) -> str | None:
        """The leg a follow-up would continue, given a URL some turn published.

        Either spelling of a chat URL resolves, and both resolve to the chat's
        *current* leg rather than to the leg the URL happens to name — the same
        rule every other write follows (#1266). ``None`` when the URL names
        nothing this deck knows, which is a reason to start a fresh chat rather
        than to refuse the work.
        """
        reference = published_chat_reference(url)
        if reference is None:
            return None
        kind, value = reference
        if kind == "chat":
            chat = self.chat_by_id(value)
            if chat is None:
                return None
            current = self.current_session_for_chat(chat)
            return current.key if current is not None else chat.newest_session_key
        # A dead leg still names its chat, which is the whole point of an alias:
        # the successor continues the conversation, not the process that ended.
        if self.chat_for_session(value) is not None:
            return self.chat_current_session_key(value)
        return value if value in self.sessions else None

    def reserve_chat_successor(
        self,
        token: str,
        source_session_key: str,
        *,
        newest_run: tuple[str, str] | None = None,
    ) -> str | None:
        reserve = getattr(self.budget_db, "reserve_chat_successor", None)
        if reserve is None:
            return None
        if newest_run is None:
            return reserve(token, source_session_key)
        return reserve(token, source_session_key, newest_run=newest_run)

    def pending_chat_successor(self, source_session_key: str) -> tuple[str, datetime] | None:
        pending = getattr(self.budget_db, "pending_chat_successor", None)
        return pending(source_session_key) if pending is not None else None

    def reserve_chat_fork(
        self, token: str, source_session_key: str, source_seq: int, action_id: str | None
    ) -> str | None:
        """Record a side chat's source (#2485); the token that owns this press, or None."""
        reserve = getattr(self.budget_db, "reserve_chat_fork", None)
        if reserve is None:
            return None
        return reserve(token, source_session_key, source_seq, action_id)

    def release_chat_fork(self, token: str) -> None:
        """Drop a side chat's reservation that no session ever claimed (#2485)."""
        release = getattr(self.budget_db, "release_chat_fork", None)
        if release is not None:
            release(token)

    def record_transcript_rollback(self, session_key: str, first_seq: int, last_seq: int) -> bool:
        """Take lines ``first_seq..last_seq`` of a leg out of its chat for good (#3474)."""
        record = getattr(self.budget_db, "record_transcript_rollback", None)
        if record is None or not record(session_key, first_seq, last_seq):
            return False
        HIDDEN.add(session_key, first_seq, last_seq)
        self.sessions_changed([session_key])
        return True

    def chat_fork_source(self, session_key: str) -> tuple[str, int] | None:
        """The session and transcript line a side chat was cut from, or None."""
        reader = getattr(self.budget_db, "chat_fork_source", None)
        return reader(session_key) if reader is not None else None

    def release_chat_successor(self, token: str) -> None:
        release = getattr(self.budget_db, "release_chat_successor", None)
        if release is not None:
            release(token)

    def session_key_for_token(self, token: str) -> str | None:
        session_key = self.session_tokens.get(token)
        if session_key is not None or self.budget_db is None:
            return session_key
        session_key = self.budget_db.session_key_for_token(token)
        if session_key is not None:
            self.session_tokens[token] = session_key
        return session_key

    def session_key_has_token(self, session_key: str) -> bool:
        """Whether the deck minted a token for this key, so the key is real before any scan."""
        if session_key in self.session_tokens.values():
            return True
        reader = getattr(self.budget_db, "session_key_has_token", None)
        return bool(reader(session_key)) if reader is not None else False

    def follow_runtime_relations(self) -> bool:
        """Refresh this process's cached tokens and card registrations from the store (#2144).

        A Poller delivery now goes straight to the runtime, which binds the new session's token
        and registers its card in the shared store. Those durable rows are the only record;
        nothing here acknowledges, orders or re-delivers anything, and delivery never waits for
        it. The web only keeps in-memory copies of those rows, so this refreshes the copies and
        publishes the usual session change. The stamp is derived from the rows themselves, and
        a reload happens only after it moves. True when a row this process lacked was adopted.
        """
        stamp_of = getattr(self.budget_db, "runtime_relation_stamp", None)
        if stamp_of is None:
            return False
        try:
            stamp = stamp_of()
            if stamp is None or stamp == self._runtime_relation_stamp:
                return False
            tokens = self.budget_db.load_session_tokens()
            issues = self.db.load_session_issues() if self.db is not None else {}
        except Exception:  # noqa: BLE001 -- a follower must never take the web down
            log.exception("could not follow the runtime's session relations")
            return False
        self._runtime_relation_stamp = stamp
        adopted = {
            token: key for token, key in tokens.items() if self.session_tokens.get(token) != key
        }
        self.session_tokens.update(adopted)
        # Only additions are adopted. A registration this process removed is already gone from
        # the store, and one the store still lacks is a write of ours that has not landed.
        changed = [
            key for key, refs in issues.items()
            if not set(refs) <= set(self.session_issues.get(key, ()))
        ]
        for key in changed:
            known = self.session_issues.get(key, ())
            self.session_issues[key] = (*known, *(ref for ref in issues[key] if ref not in known))
            self._record_works(issues[key])
        for key in changed:
            if key in self.sessions:
                self._republish_chat(key)
        return bool(adopted or changed)

    # --- registered GitHub relationships ------------------------------
    # Explicit issues and PRs are exactly the ones registered for a session.
    # A provider-derived kanban issue is projected beside registered issues,
    # but conversation prose and shared-checkout branches are never guessed.

    def issues_for(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> tuple[IssueRef, ...]:
        refs: dict[tuple[str, int], IssueRef] = {}
        for key in self.chat_session_keys(session_key, chats):
            for ref in self.session_issues.get(key, ()):
                refs.setdefault(ref.identity, ref)
        return tuple(refs.values())

    def work_projection(self) -> WorkProjection:
        """Group chats into issue-backed Works; read-only (#2138).

        Gathers each leg's explicit, saved evidence from one snapshot and leaves
        the grouping to the pure builder. The parent chain is never read: a
        delegated child's Work was saved on it when it was created (#2207).
        """
        sessions = dict(self.sessions)
        chats = self.chat_snapshot()
        # The two link tables, read again only after something committed (#2865).
        counter = getattr(self.db, "change_counter", None)
        stamp = counter() if counter is not None else None
        saved = self._work_link_rows
        if stamp is None or saved is None or saved[0] != stamp:
            saved = self._work_link_rows = (
                stamp,
                self._saved_rows("session_run_work_ids"),
                self._saved_rows("session_work_bindings"),
            )
        _, run_rows, binding_rows = saved
        # Rebuilt only when an input moved: every Work page open and stream pass asks
        # for it, and the build was the costliest step of opening a Work (#2654). The
        # key holds the inputs themselves rather than revision counters, so a writer
        # that forgets to announce itself still invalidates it. Without a chat
        # snapshot a leg's chat is read one at a time, which nothing here can key.
        signature = (
            chats,
            chats.revision if chats is not None else None,
            tuple(run_rows),
            tuple(binding_rows),
            tuple((leg, session.issue_url) for leg, session in sessions.items()),
            tuple(self.session_issues.items()),
        )
        cached = self._work_projection_cache
        if chats is not None and cached is not None and cached[0] == signature:
            return cached[1]
        runs: dict[str, list[tuple[str, str]]] = {}
        for key, work_id in run_rows:
            runs.setdefault(key, []).append((key, work_id))
        bindings: dict[str, list[WorkBinding]] = {}
        for row in binding_rows:
            bindings.setdefault(row.session_key, []).append(
                WorkBinding(row.session_key, row.work_id, row.evidence, row.parent_chat_id)
            )
        facts = [
            WorkChatFacts(
                chat_id=chat_id,
                registered=tuple((leg, ref) for ref in self.session_issues.get(leg, ())),
                issue_urls=(
                    ((leg, sessions[leg].issue_url),)
                    if leg in sessions and sessions[leg].issue_url
                    else ()
                ),
                runs=tuple(runs.get(leg, ())),
                bindings=tuple(bindings.get(leg, ())),
            )
            for chat_id, legs in self._legs_by_chat(sessions, chats).items()
            for leg in legs
        ]
        work_counter.count(work_counter.WORK_PROJECTION)
        projection = build_work_projection(facts)
        self._work_projection_cache = (signature, projection) if chats is not None else None
        return projection

    def _saved_rows(self, method: str) -> list:
        read = getattr(self.db, method, None)
        return list(read()) if read is not None else []

    def _legs_by_chat(self, sessions: Mapping[str, Session], chats) -> dict[str, tuple[str, ...]]:
        legs_by_chat: dict[str, tuple[str, ...]] = {}
        for key in sessions:
            legs_by_chat.setdefault(
                self.chat_identity_key(key, chats), self.chat_session_keys(key, chats)
            )
        return legs_by_chat

    def _delegation_parent_resolver(self, sessions: Mapping[str, Session], chats):
        """Resolve a delegation parent reference — a full key or raw id — to a chat id.

        An unresolved or ambiguous reference resolves to a marker that matches no
        chat, so it can never place a child.
        """
        known_keys = set(sessions)
        if chats is not None and self.chat_identity_is_durable:
            known_keys.update(chats.chat_by_session)
        by_session_id: dict[str, set[str]] = {}
        for key in known_keys:
            session = sessions.get(key)
            session_id = session.session_id if session else key.rpartition(":")[2]
            by_session_id.setdefault(session_id, set()).add(key)

        def resolve(ref: str) -> str:
            matches = {ref} if ref in known_keys else by_session_id.get(ref, set())
            parents = {self.chat_identity_key(key, chats) for key in matches}
            return parents.pop() if len(parents) == 1 else f"{UNRESOLVED_PARENT}{ref}"

        return resolve

    def _decided_delegations(self) -> set[str]:
        """Session keys whose delegated Work outcome is already saved (#2207).

        Any ``session_work_bindings`` row — including ``run`` and ``none`` — or any Run
        binding counts: a delegation is decided once and never revisited.
        """
        return {row.session_key for row in self._saved_rows("session_work_bindings")} | {
            key for key, _ in self._saved_rows("session_run_work_ids")
        }

    def _decide_delegated_work(
        self,
        session_key: str,
        parent_session_id: str,
        *,
        resolve: Callable[[str], str] | None = None,
        projection: WorkProjection | None = None,
    ) -> bool:
        """Save a delegated child's one Work outcome (#2142, #2207); False while pending.

        The parent's current Run wins; else the parent chat's single Work is saved as
        ``delegated``; a parent in several Works saves each candidate as ``ambiguous``
        and places the child in none; a parent in no Work saves ``none``. Every branch
        saves a row, so a re-recorded delegation never adds a second outcome. A parent
        that does not resolve yet saves nothing: the delegation stays pending and
        ``finalize_pending_delegations`` decides it once the parent is scanned.
        """
        bind = getattr(self.db, "bind_session_work", None)
        if not parent_session_id or bind is None:
            return True
        latest = getattr(self.db, "latest_session_run", None)
        run = latest(parent_session_id) if latest is not None else None
        if run is not None:
            self.bind_session_run(session_key, run.work_id, run.run_id)
            bind(session_key, run.work_id, DELEGATION_RUN, run.run_id)
            return True
        chats = self.chat_snapshot()
        if resolve is None:
            resolve = self._delegation_parent_resolver(dict(self.sessions), chats)
        parent = resolve(parent_session_id)
        if parent.startswith(UNRESOLVED_PARENT):
            return False
        if parent == self.chat_identity_key(session_key, chats):
            bind(session_key, "", DELEGATION_NONE, parent)
            return True
        projection = projection if projection is not None else self.work_projection()
        works = projection.works_for_chat(parent)
        if len(works) == 1:
            bind(session_key, works[0].work_id, str(WorkEvidence.DELEGATED), parent)
        elif works:
            for work in works:
                bind(session_key, work.work_id, AMBIGUOUS, parent)
            log.info(
                "delegated %s joins no Work: parent %s is in %d (%s)",
                session_key,
                parent,
                len(works),
                ", ".join(work.work_id for work in works),
            )
        else:
            bind(session_key, "", DELEGATION_NONE, parent)
        return True

    def finalize_work_side_chats(self, now: datetime | None = None) -> int:
        """Link each side chat started from a Work's page once it exists (#2388); how many.

        The press saved its Work and pull requests before the spawn, under its action id,
        which is also the chat's session token. Once that token names a scanned session, the
        chat gets the Work's issue and those pull requests registered, the same as the
        session API's own registrations, and a ``side_chat`` Work binding: a member of the
        Work with no Run. Every write is idempotent, so the row is deleted only after them
        and a crash in between repeats them. A row nothing named within a day is a start
        that failed after it was accepted, and is dropped.
        """
        pending = self._saved_rows("pending_work_side_chats")
        if not pending:
            return 0
        delete = self.db.delete_work_side_chat
        now = now or datetime.now(UTC)
        linked = 0
        for row in pending:
            work = WorkRef.from_work_id(row.work_id)
            key = self.session_key_for_token(row.action_id)
            if work is None or key is None or key not in self.sessions:
                if work is None or now - row.created_at > SIDE_CHAT_PENDING_MAX_AGE:
                    delete(row.action_id)
                continue
            self.add_issue(key, IssueRef(work.repository, work.number))
            for raw in row.pull_requests:
                ref = PullRequestRef.parse(raw)
                if ref is not None:
                    self.add_pull_request(key, ref)
            self.db.bind_session_work(key, row.work_id, WorkEvidence.SIDE_CHAT)
            delete(row.action_id)
            linked += 1
        return linked

    def finalize_pending_delegations(self) -> int:
        """Decide every recorded delegation still pending on its parent; how many were.

        Only after the one-time backfill has run, which owns the older children. The
        parent resolver and the projection are built once per pass; a child whose own
        parent was decided in this pass is picked up on the next one.
        """
        if not self.delegated_work_backfill_done():
            return 0
        decided = self._decided_delegations()
        pending = {
            key: parent
            for key, parent in self.recorded_delegation_parents.items()
            if key not in decided and parent
        }
        if not pending:
            return 0
        resolve = self._delegation_parent_resolver(dict(self.sessions), self.chat_snapshot())
        resolvable = {
            key: parent
            for key, parent in pending.items()
            if not resolve(parent).startswith(UNRESOLVED_PARENT)
        }
        if not resolvable:
            return 0
        projection = self.work_projection()
        finalized = sum(
            self._decide_delegated_work(key, parent, resolve=resolve, projection=projection)
            for key, parent in sorted(resolvable.items())
        )
        if finalized:
            self.sessions_changed(resolvable)
        return finalized

    def delegation_cutover_ready(self) -> bool:
        """Whether a delegated child may be started now (#2207).

        The injector asks before calling the provider. False only when the store keeps
        a cutover and it can be neither read nor written: then no child starts, so none
        can be recorded before a cutover a later start writes. A store with no cutover
        runs no backfill either, so it never waits.
        """
        # Asked of the store itself: a store without the marker methods keeps nothing.
        keeps = getattr(self.db, "keeps_work_backfills", None)
        supported = keeps is not None and bool(keeps())
        return not supported or self._delegation_cutover() is not None

    def _delegation_cutover(self) -> str | None:
        """The durable cutover marker, written first if it is missing; None if unwritable.

        Every delegation after the marker belongs to the creation rule, so its absence
        must never read as "everything is legacy": the backfill refuses to run without
        it, and each attempt retries the write with this process's start time.
        """
        done = getattr(self.db, "work_backfill", None)
        record = getattr(self.db, "record_work_backfill", None)
        if done is None or record is None:
            return None
        found = done(CUTOVER_NAME)
        if found is None:
            record(CUTOVER_NAME, self._cutover_at)
            found = done(CUTOVER_NAME)
        return found

    def delegated_work_backfill_done(self) -> bool:
        done = getattr(self.db, "work_backfill", None)
        return done is not None and done(BACKFILL_NAME) is not None

    def backfill_delegated_work(self) -> dict[str, int] | None:
        """Run the legacy parent rule once over older delegated children (#2207).

        Saves each result as a ``backfilled``, ``ambiguous`` or ``none`` row and records
        that the backfill ran, so it never runs again. A child whose parent does not
        resolve saves nothing and stays pending for ``finalize_pending_delegations``.
        None when it already ran or cannot be recorded; call it only after every
        account's first full scan, because a parent's provider-inferred issue comes
        from the scan.
        """
        done = getattr(self.db, "work_backfill", None)
        if done is None or done(BACKFILL_NAME) is not None:
            return None
        sessions = dict(self.sessions)
        chats = self.chat_snapshot()
        projection = self.work_projection()
        resolve = self._delegation_parent_resolver(sessions, chats)
        decided = {
            self.chat_identity_key(key, chats)
            for key in (
                *(row.session_key for row in self._saved_rows("session_work_bindings")),
                *(key for key, _ in self._saved_rows("session_run_work_ids")),
            )
        }
        recorded = self.recorded_delegation_parents
        # Delegations recorded since the cutover belong to the creation rule, which
        # takes the parent's Run first; the backfill never decides them.
        cutover = self._delegation_cutover()
        since = getattr(self.db, "delegations_recorded_since", None)
        if cutover is None or since is None:
            log.warning("delegated Work backfill waits: the cutover marker is not recorded yet")
            return None
        new = since(cutover)
        # A delegation only a transcript scan found has no recorded time; its child
        # session's start stands in for one, and a child started after the cutover is
        # not the backfill's.
        cut = datetime.fromisoformat(cutover)
        new |= {
            key
            for key in self.delegation_parents
            if key not in recorded
            and (started := getattr(sessions.get(key), "started_at", None)) is not None
            and (started if started.tzinfo else started.replace(tzinfo=UTC)) >= cut
        }
        legs = []
        for chat_id, chat_legs in self._legs_by_chat(sessions, chats).items():
            works = frozenset(projection.works_for_chat(chat_id))
            for leg in chat_legs:
                session = sessions.get(leg)
                internal = bool(session and session.parent_session_key)
                ref = (session.parent_session_key if session else None) or (
                    recorded.get(leg) or self.delegation_parents.get(leg)
                )
                legs.append(
                    LegacyChat(
                        chat_id,
                        leg,
                        works,
                        parent=resolve(ref) if ref else None,
                        delegated=(leg in recorded or leg in self.delegation_parents)
                        and leg not in new,
                        internal=internal,
                    )
                )
        # A recorded child whose chat is not scanned right now is still decided once,
        # here, by the same rule: it has no evidence of its own to add.
        seen = {leg.session_key for leg in legs}
        for key in sorted({*recorded, *self.delegation_parents} - seen - new):
            ref = recorded.get(key) or self.delegation_parents.get(key)
            legs.append(
                LegacyChat(
                    self.chat_identity_key(key, chats),
                    key,
                    parent=resolve(ref) if ref else None,
                    delegated=True,
                )
            )
        result = plan_backfill(legs, decided=decided)
        rows = [(r.session_key, r.work_id, r.evidence, r.parent_chat_id) for r in result.rows]
        summary = result.summary()
        # One transaction: the marker that stops a rerun exists only if every row does.
        publish = getattr(self.db, "publish_work_backfill", None)
        if publish is None or not publish(
            BACKFILL_NAME, json.dumps(summary, sort_keys=True), rows
        ):
            return None
        log.info("delegated Work backfill %s: %s", BACKFILL_NAME, summary)
        return summary

    def set_issues(
        self, session_key: str, refs: Iterable[IssueRef]
    ) -> tuple[IssueRef, ...]:
        deduped: dict[tuple[str, int], IssueRef] = {}
        for ref in refs:
            deduped.setdefault(ref.identity, ref)
        current = tuple(deduped.values())
        keys = self.chat_session_keys(session_key)
        if current == self.issues_for(session_key):
            return current
        if self.db is not None:
            for key in keys:
                self.db.delete_session_issues(key)
            for ref in current:
                self.db.record_session_issue(self.chat_current_session_key(session_key), ref)
            self._record_works(current)
        for key in keys:
            self.session_issues.pop(key, None)
        if current:
            self.session_issues[self.chat_current_session_key(session_key)] = current
        self._republish_chat(session_key)
        return current

    def _record_works(self, refs: Iterable[IssueRef]) -> None:
        """Keep a Work inventory row for each issue (#2140).

        The ``work_id`` itself never comes from here: ``work.work_id_for``
        derives it from the issue, so a missing row changes nothing.
        """
        record = getattr(self.db, "record_works", None)
        if record is not None:
            record(refs)

    def bind_session_run(
        self, session_key: str, work_id: str, run_id: str, *, delivery_id: str | None = None
    ) -> None:
        """Record that this session took part in one Run of one Work (#2142).

        Keyed by the exact session leg that took the delivery, not the chat: a chat's later
        legs did not take part in this Run. Idempotent, and never an ownership claim.
        """
        bind = getattr(self.db, "bind_session_run", None)
        if bind is not None:
            bind(session_key, work_id, run_id, delivery_id=delivery_id)

    def add_issue(self, session_key: str, ref: IssueRef) -> tuple[IssueRef, ...]:
        return self.set_issues(session_key, (*self.issues_for(session_key), ref))

    def remove_issue(self, session_key: str, ref: IssueRef) -> tuple[IssueRef, ...]:
        return self.set_issues(
            session_key,
            tuple(item for item in self.issues_for(session_key) if item.identity != ref.identity),
        )

    def review_verdict(self, repo: str, sha: str) -> PollerReviewVerdict | None:
        """What is recorded about this commit's review, if anything (#1127).

        One table read per commit this process is asked about, then a dict read. A
        commit nobody reviewed is remembered as unreviewed, which is the answer the
        chip needs most often.
        """
        if not repo or not sha:
            return None
        key = (repo.lower(), sha)
        if key in self._review_verdicts:
            return self._review_verdicts[key]
        verdict = self.db.poller_review_verdict(repo, sha) if self.db is not None else None
        self._review_verdicts[key] = verdict
        return verdict

    def record_review_verdict(self, verdict: PollerReviewVerdict) -> None:
        """Store one commit's verdict and let every open chip see it.

        The one writer, so nothing can leave the table and this cache disagreeing.
        The cache is updated even when there is no store and the row goes nowhere: a
        review someone started from a chip still gets to answer that chip for as
        long as this process lives.
        """
        if self.db is not None:
            self.db.record_poller_review_verdict(verdict)
        self._review_verdicts[(verdict.repo.lower(), verdict.sha)] = verdict
        self.bus.publish(REVIEWS_TOPIC)

    def record_chat_review_head(
        self,
        chat_repository: str,
        chat_number: int,
        target_repo: str,
        target_number: int,
        head_sha: str,
    ) -> None:
        """Remember the commit the review chat just opened covers for one pull request."""
        if self.db is None or not chat_repository or not head_sha:
            return
        self.db.record_chat_review_head(
            chat_repository, chat_number, target_repo, target_number, head_sha
        )

    def chat_review_heads(self, chat_repository: str) -> dict[int, dict[tuple[str, int], str]]:
        """Which commit each ChatGPT review chat was opened about, per pull request (#2088,
        #2637).

        Read straight from the table on every ask, not cached: the rows are written by
        this same process as it opens chats, and a cache would have to be invalidated on
        a write that happens far away from the chip that reads it. One small indexed read
        per session tick is cheaper than that mistake.
        """
        if self.db is None or not chat_repository:
            return {}
        return self.db.chat_review_heads(chat_repository)

    def head_review_launch(self, repo: str, sha: str) -> HeadReviewLaunch | None:
        """The review this deck started for this commit, if one is on record."""
        if not repo or not sha:
            return None
        return self.head_review_launches.get((repo.lower(), sha))

    def note_head_review_launch(self, launch: HeadReviewLaunch) -> None:
        """Remember which chat is reviewing this commit."""
        self.head_review_launches[launch.key] = launch
        self.bus.publish(REVIEWS_TOPIC)

    def pull_requests_for(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> tuple[PullRequestRef, ...]:
        refs: dict[tuple[str, int], PullRequestRef] = {}
        for key in self.chat_session_keys(session_key, chats):
            for ref in self.session_pull_requests.get(key, ()):
                refs.setdefault(ref.identity, ref)
        return tuple(refs.values())

    def set_pull_requests(
        self, session_key: str, refs: Iterable[PullRequestRef]
    ) -> tuple[PullRequestRef, ...]:
        """Replace one session's registered PRs and publish on change.

        A pull request given with no Work keeps the Work it was registered for (#3392), so
        replacing the list never forgets which Work an earlier registration named.
        """
        before = self.pull_requests_for(session_key)
        recorded = {ref.identity: ref.work_id for ref in before}
        deduped: dict[tuple[str, int], PullRequestRef] = {}
        for ref in refs:
            if not ref.work_id and recorded.get(ref.identity):
                ref = replace(ref, work_id=recorded[ref.identity])
            deduped.setdefault(ref.identity, ref)
        current = tuple(deduped.values())
        keys = self.chat_session_keys(session_key)
        if [(ref, ref.work_id) for ref in current] == [(ref, ref.work_id) for ref in before]:
            return current
        if self.db is not None:
            for key in keys:
                self.db.delete_session_pull_requests(key)
            for ref in current:
                self.db.record_session_pull_request(
                    self.chat_current_session_key(session_key), ref
                )
        for key in keys:
            self.session_pull_requests.pop(key, None)
        if current:
            self.session_pull_requests[self.chat_current_session_key(session_key)] = current
        self._republish_chat(session_key)
        return current

    def reload_pull_requests(self) -> list[PullRequestRef]:
        """Re-read every chat's registered pull requests from the database (#3244).

        The old web worker keeps serving for a while after this one loaded them at start, so a
        pull request registered there is missing here until this runs. Returns the ones this
        worker did not know, so the caller can act on them as freshly registered.
        """
        if self.db is None:
            return []
        loaded = self.db.load_session_pull_requests()

        def worked(refs) -> list:
            # A Work-only change is a change too (#3392): ``work_id`` is not in the identity.
            return [(ref, ref.work_id) for ref in refs or ()]

        changed = {
            key
            for key in {*loaded, *self.session_pull_requests}
            if worked(loaded.get(key)) != worked(self.session_pull_requests.get(key))
        }
        if not changed:
            return []
        known = {
            (ref.identity, ref.work_id)
            for refs in self.session_pull_requests.values()
            for ref in refs
        }
        self.session_pull_requests = loaded
        for key in changed:
            self._republish_chat(key)
        learned: dict[tuple[str, int], PullRequestRef] = {}
        for refs in loaded.values():
            for ref in refs:
                if (ref.identity, ref.work_id) not in known:
                    learned.setdefault(ref.identity, ref)
        return list(learned.values())

    def add_pull_request(
        self, session_key: str, ref: PullRequestRef
    ) -> tuple[PullRequestRef, ...]:
        """Register one PR, keeping an existing registration of it in place.

        Naming a Work for a pull request already registered records that Work (#3392).
        """
        refs = list(self.pull_requests_for(session_key))
        for index, item in enumerate(refs):
            if item.identity == ref.identity:
                if ref.work_id:
                    refs[index] = replace(item, work_id=ref.work_id)
                return self.set_pull_requests(session_key, refs)
        return self.set_pull_requests(session_key, (*refs, ref))

    def remove_pull_request(
        self, session_key: str, ref: PullRequestRef
    ) -> tuple[PullRequestRef, ...]:
        return self.set_pull_requests(
            session_key,
            tuple(
                item
                for item in self.pull_requests_for(session_key)
                if item.identity != ref.identity
            ),
        )

    def _with_pull_requests(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        refs = self.pull_requests_for(session.key, chats)
        return replace(session, pull_requests=refs) if session.pull_requests != refs else session

    def _with_issues(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        refs = list(self.issues_for(session.key, chats))
        inferred = IssueRef.parse(session.issue_url or "")
        if inferred is not None and all(ref.identity != inferred.identity for ref in refs):
            refs.insert(0, inferred)
        current = tuple(refs)
        return replace(session, issues=current) if session.issues != current else session

    def _republish_chat(self, session_key: str) -> None:
        """Re-project chat-owned metadata onto every currently resolved leg."""
        keys = self.chat_session_keys(session_key)
        for key in keys:
            session = self.sessions.get(key)
            if session is not None:
                self.sessions[key] = self._with_issues(
                    self._with_pull_requests(self._with_generated_title(session))
                )
        self.sessions_changed((session_key, *keys))

    def sessions_scanned(self, account_key: str) -> None:
        """Record one successful authoritative provider scan for ``account_key``."""
        revision = self.session_scan_revisions.get(account_key, 0) + 1
        self.session_scan_revisions[account_key] = revision
        self.session_scan_times[account_key] = datetime.now(UTC)
        self.bus.publish("session_scans", account_key)

    def session_scan_revision(self, account_key: str) -> int:
        return self.session_scan_revisions.get(account_key, 0)

    def session_scan_is_current(
        self,
        account_key: str,
        *,
        now: datetime | None = None,
        max_age_s: float = SESSION_SCAN_MAX_AGE_S,
    ) -> bool:
        """Whether active-session admission may trust this account's last successful scan."""
        scanned_at = self.session_scan_times.get(account_key)
        if scanned_at is None:
            return False
        return ((now or datetime.now(UTC)) - scanned_at).total_seconds() <= max_age_s

    # --- session attention reports -----------------------------------

    def note_session_sent(self, session_key: str) -> None:
        """The deck has just handed this chat something to do (#1450).

        Called from every path that can give a session work — a user message, a
        worker instruction, a nudge — so a not-working → working transition the
        deck itself caused can never be missed. Losing one loses the card that
        the matching stop would have raised.

        The stamp is optimistic — written when the deck starts handing over,
        before the provider has taken anything — so a handoff that then fails or
        is refused must take it back through ``clear_session_sent``.
        """
        self.session_sent_at[session_key] = datetime.now(UTC)

    def clear_session_sent(self, session_key: str) -> None:
        """That delivery reached nothing, so the chat is not working after all.

        Nothing else would ever clear it. ``session_awaiting_reply`` drops the
        stamp when the chat's own activity moves past it, and a chat that was
        never actually given anything produces no activity to move — so a
        refused or failed handoff would hold it classified as working for good,
        swallowing the working → stopped edge that raises its card (#1450).
        """
        self.session_sent_at.pop(session_key, None)

    def session_awaiting_reply(self, session: Session) -> bool:
        """Whether the deck is still waiting for this chat to answer a delivery.

        True from the moment the deck sends until the chat's own activity moves
        past that send. The stamp is cleared as soon as it does, so a chat that
        answered is not held working by a delivery it has already dealt with.

        And it expires, after ``WORKING_STAMP_MAX_AGE_S``. Without a ceiling the
        only two ways out are the chat acting or a caller taking the stamp back
        by hand, so a chat that is handed something and then goes silent forever
        — the whole of #2003 — sits under Working with nothing running and never
        raises a card. Anything the chat actually does still clears it sooner;
        the ceiling only decides what happens when the chat does nothing at all.
        """
        sent_at = self.session_sent_at.get(session.key)
        if sent_at is None:
            return False
        stamps = [
            stamp
            for stamp in (session.last_activity, session.last_progress)
            if stamp is not None
        ]
        if stamps and max(stamps) > sent_at:
            del self.session_sent_at[session.key]
            return False
        if (datetime.now(UTC) - sent_at).total_seconds() >= WORKING_STAMP_MAX_AGE_S:
            # Dropped rather than merely reported false, so the working → stopped
            # edge is raised once and the chat becomes a card instead of silently
            # ceasing to be working every time this is read.
            del self.session_sent_at[session.key]
            return False
        return True

    def attention_report_for(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> AttentionReport | None:
        report = self.attention_reports.get(session_key)
        if report is not None:
            return report
        # A chat linked before ``adopt_chat_leg`` ran still carries its report on
        # the leg that wrote it; the chat is what the report is about.
        return next(
            (
                self.attention_reports[key]
                for key in reversed(self.chat_session_keys(session_key, chats))
                if key in self.attention_reports
            ),
            None,
        )

    def set_attention_report(
        self, session_key: str, state: str, detail: str = ""
    ) -> AttentionReport:
        # The report describes the chat, so it is stored on the leg the chat is
        # currently on and cleared off the ones it has left. The same shape the
        # generated title and the registered issues already use.
        session_key = self.chat_current_session_key(session_key)
        for stale in self.chat_session_keys(session_key):
            if stale == session_key:
                continue
            if self.attention_reports.pop(stale, None) is not None and self.db is not None:
                self.db.delete_session_attention_report(stale)
        report = (
            self.db.record_session_attention_report(session_key, state, detail)
            if self.db is not None
            else AttentionReport(state, datetime.now(UTC), detail)
        )
        self.attention_reports[session_key] = report
        session = self.sessions.get(session_key)
        if state == "waiting" and session is not None:
            old_question = session.question
            self.reconcile_open_question(session, ())
            if session.question != old_question:
                self.sessions_changed((session_key,))
        self.bus.publish("attention_reports", session_key)
        return report

    def clear_attention_report(self, session_key: str) -> bool:
        # Chain-wide: pressing Done on the chat settles it, and a report left on
        # an earlier leg would otherwise reappear the moment anything folded.
        cleared = False
        for key in self.chat_session_keys(session_key):
            if self.attention_reports.pop(key, None) is None:
                continue
            cleared = True
            if self.db is not None:
                self.db.delete_session_attention_report(key)
        if not cleared:
            return False
        self.bus.publish("attention_reports", session_key)
        return True

    def _expire_attention_report(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> None:
        """Expire a report when a later user turn reaches the read model."""
        report = self.attention_report_for(session.key, chats)
        if (
            report is None
            or session.last_role != "user"
            or session.last_activity is None
            or session.last_activity <= report.updated_at
        ):
            return
        # Chain-wide: the boss answering on the leg the chat moved to settles a
        # report the chat left on the leg it came from.
        for key in self.chat_session_keys(session.key, chats):
            if self.attention_reports.pop(key, None) is None:
                continue
            if self.db is not None:
                self.db.delete_session_attention_report(key)

    # --- deck-derived open questions ---------------------------------

    def open_question_for(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> OpenQuestion | None:
        """The chat's parked question, whichever leg it was parked in."""
        question = self.open_questions.get(session_key)
        if question is not None:
            return question
        return next(
            (
                self.open_questions[key]
                for key in reversed(self.chat_session_keys(session_key, chats))
                if key in self.open_questions
            ),
            None,
        )

    def open_question_cursor(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> int | None:
        question = self.open_question_for(session_key, chats)
        if question is None:
            return None
        observed_session_key = question.observed_session_key or question.session_key
        return question.observed_seq if observed_session_key == session_key else 0

    def _derived_question(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> tuple[str | None, str | None]:
        if session.question:
            source = (
                "trailing"
                if session.last_role == "agent"
                and trailing_question(session.last_text) == session.question
                else "interaction"
            )
            return session.question, source
        if session.last_role == "agent":
            if question := trailing_question(session.last_text):
                return question, "trailing"
        report = self.attention_report_for(session.key, chats)
        if (
            report is not None
            and report.state == "waiting"
            and session.last_role == "agent"
            and session.last_activity is not None
            and report.updated_at >= session.last_activity
        ):
            text = (session.last_text or "").strip()
            if text:
                return text, "waiting_report"
        return None, None

    def _store_open_question(self, question: OpenQuestion) -> None:
        if self.open_questions.get(question.session_key) == question:
            return
        self.open_questions[question.session_key] = question
        if self.db is not None:
            self.db.record_open_question(question)

    def drop_open_question(self, session_key: str) -> bool:
        """Drop the debt without answering it (the existing Done action)."""
        dropped = False
        for key in self.chat_session_keys(session_key):
            if self.open_questions.pop(key, None) is None:
                continue
            dropped = True
            if self.db is not None:
                self.db.delete_open_question(key)
            session = self.sessions.get(key)
            if session is not None:
                session.question = None
        if not dropped:
            return False
        self.sessions_changed(self.chat_session_keys(session_key))
        return True

    def reconcile_open_question(
        self, session: Session, events: Iterable, chats: ChatSnapshot | None = None
    ) -> None:
        """Snapshot questions and clear once a later agent turn proves continuation.

        ``chats`` is the caller's one-lookup-per-pass chat snapshot. This runs
        over every session on every liveness sweep and every scan, and each of
        the three chat-wide reads below resolved the chat on its own — a
        ``PRAGMA`` and, whenever the runtime process had committed since, a full
        reload of the chat index, thousands of times a sweep (#1310).
        """
        current = self.open_question_for(session.key, chats)
        changed = False
        events = sorted(events, key=lambda item: item.seq)
        # How far this pass read, used only when the scan gave no position. A scan
        # can report an idle chat with no ``last_event_seq`` (0), and a question
        # derived from it was then stamped at 0: the next pass re-read the whole
        # transcript from its start, cleared the question on its old answer and
        # reply, and derived it again — a delete and an insert of the same row,
        # every chat, every scan (#3010). A scan that did give one is kept: the
        # read runs after the scan, so stamping a question the stale scan
        # re-derives at the read's end would hide the answer that cleared it.
        read_to = events[-1].seq if events and not session.last_event_seq else 0
        for event in events:
            if current is not None:
                observed_session_key = (
                    current.observed_session_key or current.session_key
                )
                if (
                    observed_session_key == session.key
                    and event.seq <= current.observed_seq
                ):
                    continue
            if current is not None:
                asked_seq = current.asked_seq if current.session_key == session.key else 0
                current = replace(
                    current,
                    observed_seq=event.seq,
                    observed_session_key=session.key,
                )
                if event.role == "user" and event.seq > asked_seq:
                    current = replace(
                        current,
                        answered_seq=event.seq,
                        answered_session_key=session.key,
                    )
                elif (
                    event.role == "assistant"
                    and current.answered_seq is not None
                    and (
                        (current.answered_session_key or current.session_key)
                        != session.key
                        or event.seq > current.answered_seq
                    )
                ):
                    question_key = current.session_key
                    self.open_questions.pop(question_key, None)
                    if self.db is not None:
                        self.db.delete_open_question(question_key)
                    current = None
                changed = True
            if event.role == "assistant" and (question_text := event_question(event)):
                if current is not None and current.session_key != session.key:
                    self.open_questions.pop(current.session_key, None)
                    if self.db is not None:
                        self.db.delete_open_question(current.session_key)
                current = OpenQuestion(
                    session.key,
                    question_text,
                    event.ts or session.last_activity or datetime.now(UTC),
                    "interaction" if event.question else "trailing",
                    event.seq,
                    event.seq,
                    observed_session_key=session.key,
                )
                changed = True

        # AskUserQuestion blocks the Claude turn. If the transcript has advanced to
        # a later assistant event, an answer or dismissal necessarily unblocked it
        # even when a new CLI callback phrase was parsed as opaque tool output. This
        # also repairs durable snapshots created before that phrase was recognized.
        # A prose question is not necessarily a handoff. Agents often introduce
        # their next experiment with one ("Can this point rank outside the nearest
        # ten?") and immediately carry on with a tool call. A later agent event is
        # proof the question was rhetorical, so it must not leave the working chat
        # parked in Deckhand's Waiting group (#1008).
        report = self.attention_reports.get(session.key)
        reported_waiting = (
            current is not None
            and report is not None
            and report.state == "waiting"
            and report.updated_at >= current.asked_at
        )
        repaired_interaction = current is not None and current.source == "interaction"
        rhetorical_trailing = (
            current is not None
            and current.source == "trailing"
            and not reported_waiting
        )
        if (
            current is not None
            and current.answered_seq is None
            and (repaired_interaction or rhetorical_trailing)
            and session.last_role == "agent"
            and session.last_event_seq
            > (current.asked_seq if current.session_key == session.key else 0)
        ):
            question_key = current.session_key
            self.open_questions.pop(question_key, None)
            if self.db is not None:
                self.db.delete_open_question(question_key)
            current = None
            changed = True

        derived, source = self._derived_question(session, chats)
        if derived and (
            current is None
            or (
                derived != current.question
                and (
                    session.last_event_seq
                    > (current.asked_seq if current.session_key == session.key else 0)
                    or source == "interaction"
                )
            )
        ):
            observed_seq = (
                current.observed_seq
                if current is not None
                and (current.observed_session_key or current.session_key) == session.key
                else 0
            )
            asked_seq = max(
                session.last_event_seq,
                observed_seq,
                read_to,
            )
            if current is not None and current.session_key != session.key:
                self.open_questions.pop(current.session_key, None)
                if self.db is not None:
                    self.db.delete_open_question(current.session_key)
            current = OpenQuestion(
                session.key,
                derived,
                session.last_activity or datetime.now(UTC),
                source or "trailing",
                asked_seq,
                asked_seq,
                observed_session_key=session.key,
            )
            changed = True
        if current is not None:
            self._store_open_question(current)
            session.question = current.question
        elif changed:
            session.question = None

    def _with_open_question(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        current = self.open_question_for(session.key, chats)
        if current is None or session.question == current.question:
            return session
        return replace(session, question=current.question)

    # --- sessions -----------------------------------------------------

    def replace_account_sessions(self, account_key: str, sessions: list[Session]) -> bool:
        """Replace all sessions of one account; returns True (and publishes) on change."""
        before = dict(self.sessions)
        ensure_chats = getattr(self.budget_db, "ensure_session_chats", None)
        if ensure_chats is not None:
            ensure_chats(session.key for session in sessions)
        chats = self.chat_snapshot()
        admitted = [self._admit(session, chats) for session in sessions]
        # A chat whose Work is finished is let go even when the provider still lists
        # it (#2841). An incremental scan that later names one is how its new activity
        # comes back: ``merge_account_sessions`` asks for a full scan instead.
        let_go = {s.key for s in admitted if self.live_window.drops(s)}
        if let_go:
            admitted = [s for s in admitted if s.key not in let_go]
        self._let_go = {k for k in self._let_go if not k.startswith(f"{account_key}:")} | let_go
        new = {s.key: s for s in admitted}
        old = {k: v for k, v in before.items() if v.account_key == account_key}
        for k in old.keys() - new.keys():
            del self.sessions[k]
        self.sessions.update(new)
        # One whole-deck parent map for the pass, shared by both readers below.
        # Each used to build its own over every session the deck holds, so a
        # scan resolved the same ~3,600-session graph two and three times over;
        # between them ``_parents_by_key`` and ``_parent_index`` were 7.6% of the
        # web worker. This standalone saving was found while profiling #1352;
        # it does not narrow that issue's account-wide provider scan. It is the
        # same sharing #1291 introduced within a single reader, applied across
        # the readers a scan runs back to back.
        # Built after ``self.sessions`` is updated so both see the new state, and
        # not cached beyond this call: ``chat_snapshot`` is valid for one
        # synchronous pass only.
        parent_by_key = self._parents_by_key(list(self.sessions.values()))
        self._publish_worker_idle((before.get(s.key), s) for s in admitted)
        self._publish_orchestration_idle(parent_by_key)
        if self.sessions == before:
            return False
        moved_sessions = [s for s in admitted if before.get(s.key) != s]
        self._record_moved_sessions(moved_sessions)
        # The chats the scan moved, and the ones it dropped (#2865).
        self.sessions_changed(
            {s.key for s in moved_sessions} | (old.keys() - new.keys())
        )
        return True

    def publish_live_companions(self) -> frozenset[str]:
        """Tell the live window which chats come with the ones held (#2841); returns the
        newly named ones the deck does not hold yet, which a scan should list.

        A chat comes whole: every leg of a held chat is kept, so one that started on
        another account before the window still shows its whole transcript. Delegation
        keeps nesting: a held chat's parent and its children come along too.
        """
        window = self.live_window
        if not window.enabled:
            return frozenset()
        chats = self.chat_snapshot()
        held = set(self.sessions)
        # Only chats kept on their own count as anchors. A held chat is not one merely
        # by being held: that would keep every chat for good, and nothing would ever
        # age out of the window or be let go after its hour (review of #2860).
        cutoff = datetime.fromtimestamp(window.cutoff(), UTC)
        # Roots that keep a chat on their own: never the companions themselves, or a
        # relative loaded for its root would become a root, drop out of the companions,
        # and be let go and named again on alternate scans (review of #2860).
        extra = window.opened() | window.pinned
        anchors = {
            key
            for key, session in self.sessions.items()
            if key in extra
            or session.status is SessionStatus.LIVE
            or session.thinking
            or (session.last_activity is not None and session.last_activity >= cutoff)
        }
        # The whole family of each root, transitively: every leg of every chat in it,
        # and every delegation parent and child however deep (review of #2860).
        related: dict[str, set[str]] = {}

        def link(a: str, b: str) -> None:
            related.setdefault(a, set()).add(b)
            related.setdefault(b, set()).add(a)

        for links in (self.recorded_delegation_parents, self.delegation_parents):
            for child, parent in list(links.items()):
                if parent and ":" in parent:
                    link(child, parent)
        for key, session in self.sessions.items():
            if session.parent_session_key and ":" in session.parent_session_key:
                link(key, session.parent_session_key)
        keys: set[str] = set(anchors)
        frontier = list(anchors)
        while frontier:
            key = frontier.pop()
            for near in (*self.chat_session_keys(key, chats), *related.get(key, ())):
                if near not in keys:
                    keys.add(near)
                    frontier.append(near)
        # An anchor is judged on its own at every scan, so it ages out or its hour ends;
        # only the legs and relatives it brings are companions.
        keys -= anchors
        added = self.live_window.set_companions(keys)
        return frozenset(key for key in added if key not in held)

    def add_account_sessions(self, account_key: str, sessions: list[Session]) -> bool:
        """Admit these sessions, new or known, without touching their account siblings.

        The live window's one-chat load (#2841): a chat someone opened joins the deck
        from a scan of its own transcript, and the rest of the account is left as the
        last full scan published it.
        """
        self._let_go.difference_update(session.key for session in sessions)
        return self.merge_account_sessions(account_key, sessions, admit_new=True)

    def merge_account_sessions(
        self, account_key: str, sessions: list[Session], *, admit_new: bool = False
    ) -> bool:
        """Replace named existing sessions without deleting their account siblings."""
        before = dict(self.sessions)
        for session in sessions:
            current = before.get(session.key)
            if current is None and session.key in self._let_go:
                raise LetGoSessionChanged(session.key)
            if admit_new and current is None and session.account_key == account_key:
                continue
            if session.account_key != account_key or current is None:
                raise ValueError(
                    f"incremental scan cannot admit unknown session {session.key}"
                )
            if current.account_key != account_key:
                raise ValueError(
                    f"incremental scan cannot move session {session.key} between accounts"
                )
        ensure_chats = getattr(self.budget_db, "ensure_session_chats", None)
        if ensure_chats is not None:
            ensure_chats(session.key for session in sessions)
        chats = self.chat_snapshot()
        admitted = [self._admit(session, chats) for session in sessions]
        self.sessions.update((session.key, session) for session in admitted)
        parent_by_key = self._parents_by_key(list(self.sessions.values()))
        # A collector scan is also the retry edge for a completed orchestration
        # wave until its subscriber acknowledges it. Even a source append whose
        # normalized session is unchanged must not suppress that retry.
        self._publish_orchestration_idle(parent_by_key)
        moved_sessions = [
            self.sessions[session.key]
            for session in admitted
            if before.get(session.key) != self.sessions[session.key]
        ]
        # Every chat this scan named had its source written, so its transcript has
        # something new for an open page even when its normalized session did not
        # move (#2865): that page tails on its chat's topic, not on a clock.
        self.wake_chat_pages(
            session.key for session in admitted if before.get(session.key) == session
        )
        if not moved_sessions:
            return False
        self._publish_worker_idle((before.get(s.key), s) for s in moved_sessions)
        self._record_moved_sessions(moved_sessions)
        self.sessions_changed(s.key for s in moved_sessions)
        return True

    def wake_chat_pages(self, keys: Iterable[str]) -> None:
        """Wake the pages following these chats without changing any chat's view.

        For what an open chat page reads beyond the session itself (#2865): a source
        write that left the normalized session as it was still appended to the
        transcript the page is tailing, and a git context landing moves its chips.
        """
        chats = self.chat_snapshot()
        topics = {chat_topic(self.chat_identity_key(key, chats)) for key in keys}
        for topic in sorted(topics):
            self.bus.publish(topic)

    def _record_moved_sessions(self, moved_sessions: list[Session]) -> None:
        if self.db is not None:
            # Only the sessions this operation actually moved. Re-writing an
            # unchanged session's row
            # rewrites the columns it already holds: this is a write per session
            # per scan per account, and it was the largest database cost in the
            # scan loop (#1348). ``last_seen`` therefore records the last scan in
            # which a row's recorded fields moved, not the last scan that saw the
            # session; nothing reads that column.
            self.db.upsert_sessions_seen(moved_sessions)
            # A poller worker names its card through the inferred issue URL.
            self._record_works(
                ref for s in moved_sessions if (ref := IssueRef.parse(s.issue_url or ""))
            )

    def update_session(self, session: Session) -> None:
        before = dict(self.sessions)
        ensure_chats = getattr(self.budget_db, "ensure_session_chats", None)
        if ensure_chats is not None:
            ensure_chats((session.key,))
        session = self._admit(session, self.chat_snapshot())
        self.sessions[session.key] = session
        parent_by_key = self._parents_by_key(list(self.sessions.values()))
        self._publish_worker_idle([(before.get(session.key), session)])
        self._publish_orchestration_idle(parent_by_key)
        if before.get(session.key) == session:
            return
        self.sessions_changed((session.key,))

    def _publish_worker_idle(self, pairs: Iterable[tuple[Session | None, Session]]) -> None:
        """Announce every chat that just stopped working (#891).

        The edge, not the state: a chat that *was* thinking and is not any more
        is the moment babysitting wants — about one scan after the turn ended,
        instead of up to a fifteen-minute timer. Like ``boss_returned`` it has to
        be published where the transition is still visible: the previous session
        is gone as soon as it is replaced or refreshed, and no subscriber
        watching only ``sessions`` could recover it afterwards.

        Every writer feeds it, and the liveness path is the one that matters
        most: ``apply_session_changes`` refreshes the objects the read model
        already holds, and for an owned worker that is normally where a finished
        turn lands first — every ``liveness_interval_s`` for claude_code, and on
        the runtime's own callback for Codex. Watching only the full
        scan would have published the edge for about a third of claude workers
        and almost never for Codex.

        Four shapes are deliberately not this edge:

        - A turn that **stalled**. Thinking is suppressed by stall evidence
          (``runtime_turn_state``), so a stall looks exactly like a finished turn
          from here. It is not one, and #891 leaves stalls to the fallback sweep:
          a nudge would land inside somebody's open turn, and ``STALL_S`` is ten
          minutes, which one long test suite reaches.
        - A turn holding an **answerable interaction**, which suppresses thinking
          the same way for the same reason (``runtime_turn_state`` again): the
          turn is open, waiting on a human. ``Capability.INTERACT`` is that fact
          as the deck publishes it.
        - A chat **waiting on its own question**. It belongs with the boss, not
          with any automatic attendance.
        - A **subagent**. It is a row under the chat that spawned it, not a chat
          anything can be steered into, so its turns are not a worker's turns.

        Nothing else is judged here. Whether the chat is a worker at all, and
        whether it is working an open owned issue, is the subscriber's join.
        """
        for previous, session in pairs:
            if previous is None or not previous.thinking:
                continue
            if session.thinking or session.stalled or session.is_subagent:
                continue
            if session.is_waiting or Capability.INTERACT in session.capabilities:
                continue
            self.bus.publish("worker_idle", session.key)

    def _publish_orchestration_idle(
        self, parent_by_key: dict[str, str | None] | None = None
    ) -> None:
        """Announce when a parent that had descendant work becomes fully idle.

        Provider-owned ``Session.thinking`` describes only the session's own turn.
        Descendant work is normalized separately by ``_effective_activity`` from
        addressable child sessions and embedded provider rosters. Remember every
        parent that enters that derived state, then publish exactly once after no
        descendant *or parent turn* remains active. The subscriber decides whether
        the completed wave needs recovery; this edge makes no semantic judgement.
        """
        sessions = list(self.sessions.values())
        if parent_by_key is None:
            parent_by_key = self._parents_by_key(sessions)
        working_keys, through_subagents, _ = self._effective_activity(
            sessions, parent_by_key
        )
        known_keys = {session.key for session in sessions}
        parent_busy = {
            session.key
            for session in sessions
            if session.lifecycle_active is True
            or session.stalled
            or session.is_waiting
            or Capability.STEER in session.capabilities
            or Capability.INTERACT in session.capabilities
        }
        completed = {
            key
            for key in self._orchestrating_sessions - working_keys - parent_busy
            if key in known_keys
        }
        self._orchestrating_sessions.update(through_subagents)
        self._orchestrating_sessions.intersection_update(known_keys)
        for key in sorted(completed):
            self.bus.publish("orchestration_idle", key)

    def acknowledge_orchestration_idle(self, session_key: str) -> None:
        """Retire one completed wave after its recovery check was accepted.

        Until this acknowledgement every collector scan republishes the edge.
        That is deliberate: the event bus has no replay and may drop a full
        queue, while a transient check refusal must not lose the only wake.
        """
        self._orchestrating_sessions.discard(session_key)

    def _admit(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        """Apply the account-session admission rule and return the canonical session.

        Learns and persists a newly self-flagged delegation, stamps
        ``is_delegated=True`` on a session whose key is already persisted, and
        attaches any generated title and registered pull requests. Callers own
        change-detection and publish.
        ``mark_delegated_session`` deliberately does NOT route through here — it
        flags an *un*-self-flagged session unconditionally and with a different
        persist/publish contract.
        """
        if self.chat_identity_is_durable:
            chat = self.chat_for_session(session.key, chats)
            if chat is not None and session.chat_id != chat.chat_id:
                session = replace(session, chat_id=chat.chat_id)
        elif session.chat_id is not None:
            session = replace(session, chat_id=None)
        session = self._with_single_live_leg(session, chats)
        self._expire_attention_report(session, chats)
        self._note_boss_turn_from_scan(session)
        if session.is_delegated and session.key not in self.delegated_session_keys:
            self.delegated_session_keys.add(session.key)
            if self.db is not None:
                self.db.record_delegated_session(session.key)
        elif session.key in self.delegated_session_keys:
            session = replace(session, is_delegated=True)
        return self._with_mute(
            self._with_open_question(
                self._with_issues(
                    self._with_pull_requests(
                        self._with_generated_title(session, chats), chats
                    ),
                    chats,
                ),
                chats,
            )
        )

    def _with_single_live_leg(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        """One chat, one live leg: a superseded leg reads as history (#1501).

        Two provider processes belonging to one chat really can be running at
        once — the deck hands a card back to an earlier account without killing
        what it left behind — but only one of them is the conversation. While
        both say ``live``, no view can answer "which leg is working" from status
        alone, which is how a chat came to present its working leg as history
        and offer its controls on the idle one.

        The leg that is not current reads ``idle``: its transcript is still
        readable and it is still revivable, it simply is not where the chat is.
        Only the leg being admitted is demoted, from the status its own scan
        just reported, so the store never feeds a demotion back into the next
        decision and the two legs cannot demote each other.
        """
        if session.status is not SessionStatus.LIVE:
            return session
        keys = self.chat_session_keys(session.key, chats)
        if len(keys) < 2:
            return session
        current = working_leg(self._leg_standings(keys, session))
        if current is None or current.key == session.key:
            return session
        return replace(session, status=SessionStatus.IDLE)

    def session_has_a_turn_running(self, session_key: str) -> bool:
        """Whether this session is mid-turn right now.

        The injector asks before resending a message the agent never
        acknowledged: a CLI that is merely slow to start has written nothing
        either, and a resend into it would arrive twice.
        """
        session = self.sessions.get(session_key)
        return bool(session is not None and session.thinking)

    def _with_generated_title(
        self, session: Session, chats: ChatSnapshot | None = None
    ) -> Session:
        record = self.generated_title_for(session.key, chats)
        if record is None:
            return session
        title = generated_display_title(session, record.title)
        return (
            replace(session, generated_title=title)
            if session.generated_title != title
            else session
        )

    def generated_title_for(
        self, session_key: str, chats: ChatSnapshot | None = None
    ) -> GeneratedTitle | None:
        """Newest chat-owned title recorded by any concrete leg."""
        return max(
            (
                self.generated_titles[key]
                for key in self.chat_session_keys(session_key, chats)
                if key in self.generated_titles
            ),
            key=lambda item: item.updated_at,
            default=None,
        )

    def set_generated_title(
        self,
        session_key: str,
        title: str,
        evidence_signature: str,
        source: str = "deckhand",
    ) -> bool:
        """Store a display title. A hand-typed one outranks every other source.

        Returns whether the title was stored: a chat the operator named by hand
        keeps that name until they change or clear it, so a later self-naming
        write from the chat itself is refused rather than silently winning.
        """
        if source != "user":
            standing = self.generated_title_for(session_key)
            if standing is not None and standing.source == "user":
                return False
        keys = self.chat_session_keys(session_key)
        storage_key = self.chat_current_session_key(session_key)
        if self.db is not None:
            for key in keys:
                self.db.delete_generated_title(key)
        record = (
            self.db.record_generated_title(storage_key, title, evidence_signature, source)
            if self.db is not None
            else GeneratedTitle(title, evidence_signature, datetime.now(UTC), source)
        )
        for key in keys:
            self.generated_titles.pop(key, None)
        self.generated_titles[storage_key] = record
        self._republish_chat(session_key)
        return True

    def clear_generated_title(self, session_key: str, source: str = "deckhand") -> bool:
        """Drop a stored display title so the provider-native one shows again.

        The same precedence as ``set_generated_title``: only the operator may
        undo the name the operator typed, so a chat clearing its own title
        cannot quietly erase one and fall back to the provider's.
        """
        keys = self.chat_session_keys(session_key)
        if not any(key in self.generated_titles for key in keys):
            return False
        if source != "user":
            standing = self.generated_title_for(session_key)
            if standing is not None and standing.source == "user":
                return False
        for key in keys:
            self.generated_titles.pop(key, None)
            if self.db is not None:
                self.db.delete_generated_title(key)
            session = self.sessions.get(key)
            if session is not None and session.generated_title is not None:
                self.sessions[key] = replace(session, generated_title=None)
        self.sessions_changed(keys)
        return True

    def mark_delegated_session(
        self, session_key: str, parent_session_id: str | None = None
    ) -> None:
        """Persist machine-started work so Deckhand ignores it across rescans/restarts.

        ``parent_session_id`` identifies the session that started this
        delegation, either as its raw provider session id (e.g. the invoking
        Claude chat's ``$CLAUDE_CODE_SESSION_ID``) or as its full AgentDeck
        session key. We store it as-is and resolve either form lazily in
        ``session_tree`` so the delegated child nests under its parent,
        including cross-provider, even if the parent has not been scanned in
        yet at delegation time.
        """
        self.delegated_session_keys.add(session_key)
        if parent_session_id:
            self.recorded_delegation_parents[session_key] = parent_session_id
        if self.db is not None:
            # The injector checked the cutover before starting this child
            # (``delegation_cutover_ready``), so its row cannot predate the cutover.
            self.db.record_delegated_session(session_key, parent_session_id)
            if parent_session_id and session_key not in self._decided_delegations():
                self._decide_delegated_work(session_key, parent_session_id)
        session = self.sessions.get(session_key)
        if session is not None and not session.is_delegated:
            self.sessions[session_key] = replace(session, is_delegated=True)
        # The parent's tree moved too, and it may be named by a raw provider id
        # that only ``session_tree`` resolves: rare enough to announce for every chat.
        self.sessions_changed()

    def mute_lease(
        self, session_key: str, *, now: datetime | None = None
    ) -> MuteLease | None:
        """The live lease on this chat, dropping it if it has lapsed."""
        lease = self.mute_leases.get(session_key)
        if lease is None:
            return None
        at = now or datetime.now(UTC)
        if not lease.active_at(at):
            self._drop_lapsed_lease(session_key, lease, at)
            return None
        return lease

    def _drop_lapsed_lease(
        self, session_key: str, lease: MuteLease, at: datetime
    ) -> None:
        """Forget a lease that ran out, remembering a hard cap it spent.

        Every path that drops a lapsed lease goes through here. It used to be
        inline in ``mute_lease``, which meant the 15 s sweep — the path that
        almost always gets there first — dropped the lease without recording the
        cap, and the deck could take a fresh lease straight away. The
        cap only means anything if whoever notices the lapse writes it down.
        """
        self.mute_leases.pop(session_key, None)
        if lease.hard_expires_at is not None and at >= lease.hard_expires_at:
            self._mute_cap_spent[(session_key, lease.source)] = lease.attempt_window

    def session_is_muted(self, session_key: str, *, now: datetime | None = None) -> bool:
        return self.mute_lease(session_key, now=now) is not None

    def grant_mute_lease(
        self,
        session_key: str,
        *,
        source: str = MUTE_SOURCE_BOSS,
        now: datetime | None = None,
    ) -> MuteLease | None:
        """Open or renew a lease, refusing once the hard total cap is reached.

        Renewal extends the expiry but never the cap: a capped lease has to
        lapse and be re-earned once its policy's hard cap is reached.

        A grant from a *stronger* source replaces the lease outright; a grant
        from a weaker one leaves the existing lease alone and returns it.
        """
        policy = MUTE_LEASE_POLICIES.get(source)
        if policy is None:
            return None
        at = now or datetime.now(UTC)
        existing = self.mute_lease(session_key, now=at)
        window = self.boss_turn_window(session_key)
        if (
            existing is None
            and policy.hard_cap_seconds is not None
            and self._mute_cap_spent.get((session_key, source)) == window
        ):
            # The cap is spent for this window. Re-granting here would make it a
            # speed bump rather than a cap; the deck has to wait for the boss to
            # engage with the chat, which is what "re-earn itself" means.
            return None
        if existing is not None and existing.source != source:
            held = MUTE_LEASE_POLICIES.get(existing.source)
            if held is not None and policy.precedence < held.precedence:
                # A weaker claim over a stronger one is a downgrade, not a
                # renewal. The chat stays muted under the claim already on it.
                return existing
        if existing is not None and existing.source == source:
            if existing.hard_expires_at is not None and (
                at + timedelta(seconds=policy.lease_seconds)
                >= existing.hard_expires_at
            ):
                # Renewing past the cap is what the cap exists to refuse; the
                # lease keeps its remaining life and then lapses.
                lease = existing
                self._publish_mute(session_key)
                return lease
            lease = replace(
                existing,
                expires_at=at + timedelta(seconds=policy.lease_seconds),
            )
        else:
            cap = policy.hard_cap_seconds
            lease = MuteLease(
                session_key=session_key,
                source=source,
                granted_at=at,
                expires_at=at + timedelta(seconds=policy.lease_seconds),
                hard_expires_at=(
                    at + timedelta(seconds=cap) if cap is not None else None
                ),
                attempt_window=window,
            )
        self.mute_leases[session_key] = lease
        self._publish_mute(session_key)
        return lease

    def release_mute_lease(self, session_key: str) -> bool:
        """End a mute now, and say whether there was one to end."""
        if self.mute_leases.pop(session_key, None) is None:
            return False
        self._publish_mute(session_key)
        return True

    def expire_mute_leases(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Drop lapsed leases and return the chats that just came back unmuted."""
        at = now or datetime.now(UTC)
        lapsed = tuple(
            (key, lease)
            for key, lease in self.mute_leases.items()
            if not lease.active_at(at)
        )
        for key, lease in lapsed:
            self._drop_lapsed_lease(key, lease, at)
            self._publish_mute(key)
        return tuple(key for key, _ in lapsed)

    def _publish_mute(self, session_key: str) -> None:
        """Re-project the card so a mute is never invisible, and never sticky."""
        session = self.sessions.get(session_key)
        if session is None:
            return
        projected = self._with_mute(session)
        if projected != session:
            self.sessions[session_key] = projected
            self.sessions_changed((session_key,))
        # The count of attended chats lives in the assistant region rather than
        # the session list, so it needs its own push.
        self.assistant_changed()

    def _with_mute(self, session: Session) -> Session:
        muted = self.session_is_muted(session.key)
        if session.muted == muted:
            return session
        return replace(session, muted=muted)

    def boss_turn_window(self, session_key: str) -> str:
        """The chat's current boss-engagement window; "" is the initial one."""
        return self.boss_turn_windows.get(session_key, "")

    def open_boss_turn_window(self, session_key: str) -> str:
        """Start a fresh window because the boss engaged with this chat (#472).

        The same signal ends any mute lease on that chat (#823), and for the same
        reason it opens a window: the boss is here, so the deck's claim to be
        handling this is stale. Both boss-write paths funnel through here — the
        route reporting an accepted inject/steer/interaction answer, and the scan
        noticing a turn the boss typed elsewhere. Neither fires on merely reading
        the chat, which is what makes "watching does not end it" true.
        """
        self.release_mute_lease(session_key)
        for spent in tuple(self._mute_cap_spent):
            if spent[0] == session_key:
                del self._mute_cap_spent[spent]
        opened_at = datetime.now(UTC).isoformat()
        self.boss_turn_windows[session_key] = opened_at
        if self.db is not None:
            self.db.open_boss_turn_window(session_key, opened_at)
        return opened_at

    def _note_boss_turn_from_scan(self, session: Session) -> None:
        """Open a new window from the session scan, not a transcript read (#475).

        Scans see `last_role`/`last_prompt` promptly, so the window follows the
        boss instead of following whoever happens to be viewing a transcript.
        """
        if session.last_role != "user" or not session.last_prompt:
            return
        digest = _text_digest(session.last_prompt)
        if digest == self._last_boss_turn_digest.get(session.key):
            return
        self._last_boss_turn_digest[session.key] = digest
        self.open_boss_turn_window(session.key)

    def note_boss_turn(self, session_key: str, text: str) -> str:
        """Record a boss write the deck itself accepted, without waiting for a scan.

        The scan and the transcript read both learn about a boss turn only after
        the provider has written it, so the route reports the turn as soon as it
        is accepted. The digest is stored too, so the scan recognises the same
        turn instead of opening a second window for it.
        """
        self._last_boss_turn_digest[session_key] = _text_digest(text)
        return self.open_boss_turn_window(session_key)

    def apply_session_changes(
        self,
        sessions: Iterable[Session],
        project: Callable[[Session], object],
    ) -> list[Session]:
        """Own the live-overlay transition: mutate, detect change, publish once.

        The liveness sweep and Persistent Runtime callbacks refresh fields on
        the ``Session`` objects the read model already holds. ``project``
        applies that in-place refresh to each session; a session counts as
        changed only when it differs from a whole-object pre-image, so no
        per-field list can fall out of sync with what ``project`` writes.
        ``"sessions"`` is published exactly once for the batch, and only when
        something changed. Returns the changed sessions (same identities) so a
        sweep can still report what it touched.

        The whole-object pre-image is also where a finished turn is *first*
        visible on an owned worker, so ``worker_idle`` (#891) is published from
        here as well as from the two replacing writers.
        """
        changed: list[Session] = []
        idle: list[tuple[Session | None, Session]] = []
        # One chat lookup for the whole sweep, the way ``visible_sessions``
        # already does it. The question reconciliation below is chat-wide, and
        # resolving the chat per session made the sweep the largest consumer of
        # the event-loop thread (#1310).
        chats = self.chat_snapshot()
        for session in sessions:
            before = copy_session(session)
            project(session)
            if (
                session.key in self.open_questions
                or self._derived_question(session, chats)[0]
            ):
                self.reconcile_open_question(session, (), chats)
            if session != before:
                changed.append(session)
                idle.append((before, session))
        if changed:
            self.sessions_changed(session.key for session in changed)
        self._publish_worker_idle(idle)
        # Only when this batch actually moved something. The wave edge is
        # derived from the session graph, so a sweep that changed nothing can
        # only recompute the answer the last writer already published — and on
        # this deck that is every sweep: the sweep visits every session in the
        # account every ``liveness_interval_s`` and, measured over consecutive
        # sweeps, changes none of them. Running the whole-deck parent index and
        # effective-activity pass anyway made this the single largest cost in
        # the liveness loop (#1348). The full scan still republishes the edge
        # unconditionally, which is what ``acknowledge_orchestration_idle``'s
        # "every collector scan republishes" contract is about.
        if changed:
            self._publish_orchestration_idle()
        return changed

    def _sort_key(self, s: Session, working: bool | None = None) -> tuple[int, int, float]:
        """``working`` overrides ``s.thinking``: the list sorts a parent by its own activity,
        never by a busy child's (#2864), while its card still says Working."""
        working = s.thinking if working is None else working
        status_order = _STATUS_ORDER.get(s.status, 9)
        if s.status == SessionStatus.IDLE and s.show_when_idle:
            # Providers such as Codex cannot reliably map native process state
            # to session activity, so do not penalize their visible idle sessions.
            status_order = _STATUS_ORDER[SessionStatus.LIVE]
        return (
            status_order,
            0 if working else 1,  # actively-working sessions float to the top
            # Activity timestamps change on every streamed event. Sorting active
            # chats by that timestamp made multiple workers continually trade
            # the top spot. Tie them here: Python's stable sort preserves their
            # insertion order until a chat enters or leaves the active tier.
            0.0
            if working
            else -(s.last_activity.timestamp() if s.last_activity else 0.0),
        )

    def sessions_for_account(self, account_key: str) -> list[Session]:
        return sorted(self.account_sessions(account_key), key=self._sort_key)

    def account_sessions(self, account_key: str) -> list[Session]:
        """The account's sessions in no particular order.

        For a caller that only wants to touch each of them. The liveness sweep
        is one, and sorting the whole account for it cost a full comparison sort
        of every session the deck holds, every sweep, for an order nothing
        downstream read (#1310).
        """
        return [s for s in self.sessions.values() if s.account_key == account_key]

    def all_sessions(self) -> list[Session]:
        """Listable sessions across accounts, working first then most-recent.
        """
        return sorted(self.sessions.values(), key=self._sort_key)

    def visible_sessions(self) -> list[Session]:
        """One current-leg row per durable chat, with chat-owned state projected.

        A single-leg chat keeps the provider visibility rule. Once a durable
        successor exists, the chat itself is a durable work object and remains
        eligible even during the old-leg-idle/new-leg-scan race. History paging
        still bounds how many resting rows are rendered at once.
        """
        projected: dict[str, Session] = {}
        # One chat lookup for the whole pass: this runs on every SSE tick, for
        # every attached browser, over every session the deck holds (#1291).
        chats = self.chat_snapshot()
        durable = self.chat_identity_is_durable
        # Each chat's row is reused while none of its legs changed (#2865): a copy made
        # every pass for every chat is what made one chat's change cost the whole deck.
        chats_revision = chats.revision if chats is not None else None
        previous = self._visible_cache
        cache: dict[tuple[str, ...], tuple] = {}
        for session in self.all_sessions():
            chat = self.chat_for_session(session.key, chats) if durable else None
            keys = chat.legs if chat is not None else (session.key,)
            if keys in cache:
                continue
            leg_objects = tuple(self.sessions.get(key) for key in keys)
            signature = (
                chats_revision,
                tuple(self.session_revision(key) for key in keys),
            )
            entry = previous.get(keys)
            if (
                entry is not None
                and entry[0] == signature
                and all(a is b for a, b in zip(entry[1], leg_objects, strict=True))
            ):
                cache[keys] = entry
                if entry[2] is not None and entry[2].key not in projected:
                    projected[entry[2].key] = entry[2]
                continue
            cache[keys] = (signature, leg_objects, None)
            current_leg = working_leg(self._leg_standings(keys, resolvable_only=True))
            current = self.sessions[current_leg.key] if current_leg is not None else None
            if (
                current is None
                or current.key in projected
            ):
                continue
            legs = tuple(self.sessions[key] for key in keys if key in self.sessions)
            # The account badge describes the durable chain, oldest first. The
            # working leg may be an earlier process, but moving it below must
            # not reverse the conversation's recorded direction.
            accounts = tuple(dict.fromkeys(leg.account_key for leg in legs))
            # Chain order with the working leg last, so the row's account chain
            # metadata lookups below pick it rather than the leg appended last
            # (#1501). This is presentation priority, not historical order.
            legs = (*(leg for leg in legs if leg.key != current.key), current)
            ordinarily_visible = any(
                leg.status != SessionStatus.IDLE
                or leg.show_when_idle
                or (leg.is_waiting and Capability.INJECT in leg.capabilities)
                for leg in legs
            )
            if not ordinarily_visible and len(keys) == 1:
                continue

            worker_type = current.worker_type or next(
                (leg.worker_type for leg in reversed(legs) if leg.worker_type), None
            )
            work_item_leg = next(
                (leg for leg in reversed(legs) if leg.issue_url), None
            )
            current = copy_session(
                current,
                chat_account_keys=accounts if len(keys) > 1 else (),
                is_delegated=(
                    current.is_delegated
                    or any(key in self.delegated_session_keys for key in keys)
                    or any(leg.is_delegated for leg in legs)
                ),
                worker_type=worker_type,
                issue_url=current.issue_url or (
                    work_item_leg.issue_url if work_item_leg is not None else None
                ),
                issue_status=current.issue_status or (
                    work_item_leg.issue_status if work_item_leg is not None else None
                ),
                issue_status_kind=current.issue_status_kind or (
                    work_item_leg.issue_status_kind
                    if work_item_leg is not None
                    else None
                ),
            )
            current = self._with_mute(
                self._with_issues(
                    self._with_pull_requests(
                        self._with_generated_title(current, chats), chats
                    ),
                    chats,
                )
            )
            projected[current.key] = current
            cache[keys] = (signature, leg_objects, current)
        self._visible_cache = cache
        return sorted(projected.values(), key=self._sort_key)

    def set_delegation_parents(self, account_key: str, pairs: dict[str, str]) -> None:
        """Replace the delegation pairs whose PARENT is in ``account_key``.

        Each provider scan reports the child->parent pairs it found in that
        account's transcripts; scoping the clear to parents in the account keeps
        cross-provider pairs from other accounts intact.
        """
        prefix = f"{account_key}:"
        updated = {
            child: parent
            for child, parent in self.delegation_parents.items()
            if not parent.startswith(prefix)
        }
        updated.update(pairs)
        if updated == self.delegation_parents:
            return
        moved = set(updated.items()) ^ set(self.delegation_parents.items())
        self.delegation_parents = updated
        # Each child that moved, and its old and new parent (#2865).
        self.sessions_changed({key for pair in moved for key in pair})

    @staticmethod
    def _parent_index(sessions: Iterable[Session]) -> dict[str, str]:
        """Index sessions by every form a recorded delegation parent can take.

        A delegating chat names itself with whichever identity it can actually
        read: a Claude turn has a bare ``$CLAUDE_CODE_SESSION_ID``, while a
        session token or ``$AGENTDECK_SESSION_KEY`` yields a full session key.
        Both forms must land on the same parent, so index by session id and by
        key. Keys are indexed last: a full key is unambiguous, so it wins the
        astronomically-unlikely clash with some other session's raw id.
        """
        index = {session.session_id: session.key for session in sessions}
        index.update({session.key: session.key for session in sessions})
        return index

    def _parents_by_key(
        self,
        sessions: list[Session],
        parent_index: dict[str, str] | None = None,
        chats: ChatSnapshot | None = None,
    ) -> dict[str, str | None]:
        """Every session's parent, resolving chat identity once for the pass.

        Four callers each walked the whole session set resolving parents one
        session at a time, and every one of those resolutions was a chat lookup
        (#1291). They share this instead, so the pass costs one lookup.
        """
        if parent_index is None:
            parent_index = self._parent_index(sessions)
        if chats is None:
            chats = self.chat_snapshot()
        return {
            session.key: self._parent_key(session, parent_index, chats)
            for session in sessions
        }

    def _parent_key(
        self,
        session: Session,
        parent_index: dict[str, str],
        chats: ChatSnapshot | None = None,
    ) -> str | None:
        """A session's parent: its own subagent link, then a delegation link.

        Recorded (delegation-time) parents take precedence over scan-discovered
        ones: they are authoritative and cover cross-provider delegation the
        transcript markers cannot express. The recorded value is a raw
        session_id or a full session key, resolved here against
        ``parent_index`` (built from the currently-visible sessions); an
        unresolved or self-referential match yields no parent, so the child
        simply stays top-level.
        """
        if session.parent_session_key:
            return parent_index.get(
                session.parent_session_key, session.parent_session_key
            )
        keys = self.chat_session_keys(session.key, chats)
        recorded = next(
            (
                self.recorded_delegation_parents[key]
                for key in reversed(keys)
                if key in self.recorded_delegation_parents
            ),
            None,
        )
        if recorded is not None:
            resolved = parent_index.get(recorded)
            return resolved if resolved != session.key else None
        return next(
            (
                self.delegation_parents[key]
                for key in reversed(keys)
                if key in self.delegation_parents
            ),
            None,
        )

    def _effective_activity(
        self, visible: list[Session], parent_by_key: Mapping[str, str | None]
    ) -> tuple[set[str], set[str], dict[str, datetime | None]]:
        """Return effective-working keys, descendant-work keys, and progress.

        Providers keep ``Session.thinking`` scoped to the session's own turn.
        A submitted heavy job is session work too, even while it waits for the
        host slot. Parent activity is relationship state: Codex may report
        spawned agents directly through ``subagent_count``, while Claude and
        delegated workers appear as child sessions. Propagate every form through
        the parent graph without mutating provider-owned session records.
        """
        job_activity = self._job_activity({session.key for session in visible})
        own_working = {
            session.key
            for session in visible
            if session.thinking or session.key in job_activity
        }
        progress: dict[str, datetime | None] = {}
        for session in visible:
            stamps = [
                session.last_progress or session.last_activity,
                job_activity.get(session.key),
            ]
            if session.subagent_count:
                stamps.extend(
                    agent.updated_at or agent.started_at
                    for agent in session.subagents
                    if agent.status in ("working", "quiet")
                )
            progress[session.key] = max(
                (stamp for stamp in stamps if stamp is not None), default=None
            )
        # A raw descendant count is a claim, not evidence of motion: a subagent
        # whose terminal envelope was missed keeps its count forever, and an
        # unbounded promotion would keep the whole ancestor chain reading
        # Working indefinitely (#791). Bound it by the same stall threshold the
        # providers use for a session's own turn; an unknown stamp still
        # promotes, since absence of a timestamp is not evidence of staleness.
        now = datetime.now(UTC)
        through_subagents = {
            session.key
            for session in visible
            if session.subagent_count
            and (
                (stamp := progress.get(session.key)) is None
                or (now - stamp).total_seconds() < STALL_S
            )
        }
        # The promotion above is the projection's one clock-derived input: it ends
        # by itself STALL_S after the stamp, with nothing published. That moment is
        # the cached projection's deadline (ADR 0007), not a cache age.
        self._activity_deadline = min(
            (
                stamp + timedelta(seconds=STALL_S)
                for key in through_subagents
                if (stamp := progress.get(key)) is not None
            ),
            default=None,
        )
        frontier = list(own_working | through_subagents)
        while frontier:
            child = frontier.pop()
            parent = parent_by_key.get(child)
            if parent is None:
                continue
            child_progress = progress.get(child)
            parent_progress = progress.get(parent)
            progress_changed = child_progress is not None and (
                parent_progress is None or child_progress > parent_progress
            )
            if progress_changed:
                progress[parent] = child_progress
            newly_working = parent not in through_subagents
            if newly_working:
                through_subagents.add(parent)
            if newly_working or progress_changed:
                frontier.append(parent)
        return own_working | through_subagents, through_subagents, progress

    @staticmethod
    def _effective_activity_changes(
        session: Session,
        working_keys: Container[str],
        progress: Mapping[str, datetime | None],
    ) -> dict[str, object]:
        """The effective-activity overlay, as fields for one display copy."""
        effective_progress = progress.get(session.key)
        # A chat paused on a question directed at the operator is waiting, not
        # working, and activity borrowed from a queued heavy job or a descendant
        # does not change that. Stamping it anyway is what showed a chat blocked
        # on AskUserQuestion as Working while the suites it had just submitted
        # ran, and — because the pill rule is ``is_waiting and not thinking`` —
        # withheld its waiting pill and attention card with it (#1878). Only
        # borrowed motion is declined here; the chat's own live turn still wins,
        # so a turn that merely typed a question mark on its way past is
        # unaffected.
        if session.key not in working_keys or (session.is_waiting and not session.thinking):
            return {"last_progress": effective_progress}
        return {
            "thinking": True,
            # Descendant work makes an ancestor effectively working, but it is
            # not evidence about the ancestor's *own* turn: clearing a stall the
            # provider observed would suppress exactly the presentation that
            # catches a dead descendant (#791). Only the session's own thinking
            # turn overrides its stall.
            "stalled": False if session.thinking else session.stalled,
            "activity": session.activity or "Working",
            "last_progress": effective_progress,
        }

    @staticmethod
    def _with_effective_activity(
        session: Session,
        working_keys: Container[str],
        progress: Mapping[str, datetime | None],
    ) -> Session:
        return copy_session(
            session,
            **AppState._effective_activity_changes(session, working_keys, progress),
        )

    @staticmethod
    def _check_recency(session: Session) -> tuple[datetime, str]:
        """Order checks by when they last spoke; the key breaks exact ties."""
        return (session.last_activity or datetime.fromtimestamp(0, UTC), session.key)

    @staticmethod
    def _display_anchor(
        key: str, parent_by_key: Mapping[str, str | None], top_keys: set[str]
    ) -> str | None:
        """The nearest top-level ancestor ``key`` should nest under, if any.

        The display tree is one level deep, but the session graph is not: a
        cross-account subagent nests under a delegated worker that itself nests
        under the chat that delegated it. Walking to the top-level ancestor
        flattens that chain into the row it belongs to instead of dropping the
        deepest session, which is what made cross-account subagents vanish from
        the session view entirely. ``None`` means genuinely unanchored — the
        chain leaves the visible set (an aged-out parent) or closes on itself.
        """
        seen: set[str] = set()
        current = parent_by_key.get(key)
        while current is not None and current not in top_keys:
            if current in seen or current not in parent_by_key:
                return None  # parent not visible, or a cycle
            seen.add(current)
            current = parent_by_key[current]
        return current

    def session_presentation(self) -> SessionPresentation:
        """Build one immutable display projection of the visible session graph.

        ``top_level`` and ``children_of`` split sessions into a one-level tree:
        every non-top-level session is grouped under its nearest *top-level*
        ancestor, so a chain deeper than one level (a cross-account subagent of
        a delegated worker) flattens into that ancestor's row rather than
        disappearing. A session whose chain leaves the visible set is dropped
        rather than floated as a top-level card — a subagent only makes sense
        under its parent, and an orphaned one (parent aged out) would otherwise
        clutter the list with contextless cards. Order within each list follows
        ``visible_sessions``' sort, with parents promoted when descendant work
        makes them effectively active.

        The display copies go one step wider than the tree: every session the
        list hides but a page still renders gets one too (#876). The tree itself
        is built from ``visible`` alone, so nothing there can gain a second row.
        """
        # ``chat_snapshot`` is one ``PRAGMA data_version`` behind a lock, and it
        # hands back the *same* object while the chat tables have not moved — so
        # identity here is an exact test for "another process linked a leg",
        # bought for a few microseconds on a hit against ~70 ms on a rebuild.
        chats = self.chat_snapshot()
        cached = self._session_presentation_cache
        expires_at = self._session_presentation_expires_at
        if (
            cached is not None
            and self._session_presentation_revision == self.sessions_revision
            and self._session_presentation_chats is chats
            and self._session_presentation_chat_revision
            == (chats.revision if chats is not None else None)
            # The one input that moves with the clock rather than with state is a
            # deadline, not a cache age (ADR 0007): see ``_activity_expiry``.
            and (expires_at is None or datetime.now(UTC) < expires_at)
        ):
            return cached
        work_counter.count(work_counter.SESSION_PRESENTATION)
        visible = self.visible_sessions()
        all_sessions = list(self.sessions.values())
        chat_signature = (
            self.chat_identity_is_durable,
            chats,
            chats.revision if chats is not None else None,
        )
        delegation_signature = (
            frozenset(self.recorded_delegation_parents.items()),
            frozenset(self.delegation_parents.items()),
        )
        visible_chat_ids = {
            session.chat_id for session in visible if session.chat_id is not None
        }
        visible_signature = (
            # Order is significant only for a duplicate raw session id: the
            # existing last-wins rule must remain exact even in that rare case.
            tuple(
                (
                    session.key,
                    session.session_id,
                    session.chat_id,
                    session.parent_session_key,
                )
                for session in visible
            ),
            tuple(
                (leg.key, leg.session_id, leg.chat_id)
                for leg in all_sessions
                if leg.chat_id in visible_chat_ids
            ),
            delegation_signature,
            chat_signature,
        )
        previous_visible_graph = (
            cached._visible_parent_graph if cached is not None else None
        )
        if (
            previous_visible_graph is not None
            and previous_visible_graph.signature == visible_signature
        ):
            visible_parent_graph = previous_visible_graph
            parent_by_key = visible_parent_graph.parent_by_key
        else:
            # Resolve recorded delegation parents against what's visible now.
            parent_index = self._parent_index(visible)
            visible_by_chat = {
                session.chat_id: session.key
                for session in visible
                if session.chat_id is not None
            }
            for leg in all_sessions:
                current_key = visible_by_chat.get(leg.chat_id)
                if current_key is not None:
                    parent_index[leg.key] = current_key
                    parent_index[leg.session_id] = current_key
            parent_by_key = MappingProxyType(
                self._parents_by_key(visible, parent_index, chats)
            )
            visible_parent_graph = _PresentationParentGraph(
                signature=visible_signature,
                parent_by_key=parent_by_key,
            )

        working_keys, through_subagents, effective_progress = self._effective_activity(
            visible, parent_by_key
        )
        own_working = frozenset(
            {session.key for session in visible if session.thinking}
            | set(self._job_activity({session.key for session in visible}))
        )
        top_keys = {key for key, parent in parent_by_key.items() if parent is None}
        # One display copy per card, carrying the effective-activity overlay. A
        # copy is rebuilt only when its chat changed or its overlay did (#2865):
        # one chat's change no longer re-copies every chat on the deck.
        display_cache = self._display_cache
        display_by_key: dict[str, Session] = {}
        for session in visible:
            changes = self._effective_activity_changes(
                session, working_keys, effective_progress
            )
            revision = self.session_revision(session.key)
            entry = display_cache.get(session.key)
            if (
                entry is not None
                and entry[0] is session
                and entry[1] == revision
                and entry[2] == changes
            ):
                display_by_key[session.key] = entry[3]
                continue
            work_counter.count(work_counter.CHAT_DISPLAY)
            display = copy_session(session, **changes)
            display_cache[session.key] = (session, revision, changes, display)
            display_by_key[session.key] = display
        for key in set(display_cache) - set(display_by_key):
            del display_cache[key]
        top: list[Session] = []
        children: dict[str, list[Session]] = {}
        for s in visible:
            effective = display_by_key[s.key]
            if parent_by_key[s.key] is None:
                top.append(effective)
                continue
            anchor = self._display_anchor(s.key, parent_by_key, top_keys)
            if anchor is None:
                continue  # orphan (parent not visible) — omit from the tree
            # A finished Task/Agent subagent (its own transcript went quiet,
            # so ``thinking`` aged out) is dead weight: it keeps padding the
            # parent's sub-agent count and nested rows with work that is over.
            # Nest it only while it is still alive. This is scoped to true
            # subagents (``parent_session_key`` set); a delegated worker child
            # (cross-provider link, no ``parent_session_key``) still nests when
            # idle, since an idle worker is not "dead".
            if s.parent_session_key is not None and s.key not in working_keys:
                continue
            children.setdefault(anchor, []).append(effective)
        # Each chat sorts by its own activity (#2864): a busy child no longer lifts its parent.
        top.sort(key=lambda s: self._sort_key(s, working=s.key in own_working))
        immutable_children = MappingProxyType(
            {parent: tuple(sessions) for parent, sessions in children.items()}
        )
        presentation = SessionPresentation(
            visible=tuple(display_by_key[session.key] for session in visible),
            top_level=tuple(top),
            children_of=immutable_children,
            working_count=sum(session.thinking for session in top),
            _display_by_key=MappingProxyType(display_by_key),
            _working_through_subagents=frozenset(through_subagents),
            _visible_parent_graph=visible_parent_graph,
        )
        self._session_presentation_cache = presentation
        self._session_presentation_expires_at = self._activity_deadline
        self._session_presentation_revision = self.sessions_revision
        self._session_presentation_chats = chats
        self._session_presentation_chat_revision = (
            chats.revision if chats is not None else None
        )
        return presentation

    # --- usage --------------------------------------------------------

    def set_mac_waiting(self, count: int) -> None:
        """Store the Mac's waiting count, pushing the icon only when it changes (#2864)."""
        count = max(0, int(count))
        if count == self.mac_waiting:
            return
        self.mac_waiting = count
        self.bus.publish("mac_runner")

    def set_host_stats(self, snapshot: HostStats) -> None:
        """Store the host CPU/memory sample and announce it on its own topic.

        It rides the same topbar fragment as the account meters, but it is not
        the same kind of news: the sampler publishes one of these every thirty
        seconds forever (three before #2864), where a meter or a capacity estimate changes when an
        account actually moves. While the two shared the ``usage`` topic, every
        attached browser treated a CPU sample as a reason to rebuild the whole
        deck projection, several times a second, on a deck where nothing had
        happened (#1310).
        """
        self.host_stats = snapshot
        self.bus.publish("host")

    def set_usage(self, snapshot: UsageSnapshot) -> None:
        self.usage[snapshot.account_key] = snapshot
        # A read that landed is the proof the credential works again.
        self.usage_credential_errors.pop(snapshot.account_key, None)
        if self.db is not None:
            self.db.record_usage(snapshot)
        self.usage_changed()

    def warm_usage(self, snapshot: UsageSnapshot) -> None:
        """Seed a persisted snapshot at startup without re-recording it to
        history. ``setdefault`` so a live poll that already landed wins; a stale
        warm value only fills the gap until the first successful poll, and
        age-based staleness in the render marks it accordingly."""
        self.usage.setdefault(snapshot.account_key, snapshot)

    def mark_usage_stale(self, account_key: str) -> None:
        snap = self.usage.get(account_key)
        if snap is not None and not snap.stale:
            self.usage[account_key] = replace(snap, stale=True)
            self.usage_changed()

    def mark_usage_credential_rejected(self, account_key: str, reason: str) -> None:
        """The provider refused this account's meter credential (#758).

        Stronger than ``mark_usage_stale``, and deliberately so: a poll that was
        *refused* learned nothing about the account, so the last good percentage
        is withdrawn rather than aged. Keeping it made a rejected Cursor meter
        render ``2% mo`` for hours — a stale-but-plausible number reads as the
        account being nearly empty, which is worse than a visible gap. Dropping
        the snapshot is what makes every reader see the same thing (the header
        bars, the accounts list, and admission through ``state.usage``) instead of
        only the one surface that knows about this map. For admission that reads
        as "usage unknown", which fails open — the standing policy everywhere in
        this repo, because treating ignorance as a refusal lets one broken
        credential halt dispatch (``poller_engine.github.account_has_room``). It
        also arrives sooner rather than differently: that already discards a snapshot
        past their max age, and a refused meter only gets older. The reason is
        kept so the render can say which credential to fix, and both are cleared
        by the next successful read.
        """
        changed = self.usage.pop(account_key, None) is not None
        if self.usage_credential_errors.get(account_key) != reason:
            self.usage_credential_errors[account_key] = reason
            changed = True
        if changed:
            self.usage_changed()
