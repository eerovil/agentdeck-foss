"""SQLite persistence for usage history, the sessions-seen ledger, Deckhand
state, shared UI preferences and the control plane. The store is required: the
web process and the persistent runtime open the same file through ``open_db``,
and its schema upgrades only through ``db_migrations.MIGRATIONS`` (#2601).

Writes are small and infrequent (usage every ~5 min, sessions-seen every scan),
so a short synchronous insert on the event loop is negligible here; we keep one
connection guarded by a lock rather than a thread pool.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .chat_identity import ChatIdentity, ChatLinkConflict
from .composer_phrases import ComposerPhrase
from .context_delivery import ContextDelivery
from .db_migrations import apply_ordered_migrations
from .models import (
    AttentionReport,
    GeneratedTitle,
    IssueRef,
    PollerReviewVerdict,
    PullRequestRef,
    SeenSession,
    Session,
    UsageSnapshot,
)
from .questions import OpenQuestion
from .scheduled_messages import HeldAttachment, ScheduledMessage
from .sqlite_open import create_schema, use_wal
from .work import WorkRef

if TYPE_CHECKING:
    from .work_actions import WorkActionPress
    from .work_hidden import HiddenWork
    from .work_resolution import LiveCheck, WorkResolution
    from .work_status_log import StatusPoint as WorkStatusPoint
    from .work_stops import WorkStop

log = logging.getLogger(__name__)

# A chat message the operator also posted to the Work's issue (#2416). It is reserved in the
# ``work_actions`` ledger under the same at-most-once rules as a press, but it is not a Work
# action, so the Work's own reads leave it out.
ISSUE_POST_ACTION = "post_to_issue"
# A Claude or Codex reviewer's plain comment the deck posted on its card (#2814). The same
# ledger and rules, and likewise not a press on the Work.
REVIEWER_COMMENT_ACTION = "reviewer_comment"


def _escape_like(value: str) -> str:
    """Neutralize LIKE wildcards in a user's word.

    A typed ``%`` or ``_`` is a character someone searched for, not a pattern: a
    bare ``%`` would otherwise match every row and read as "everything mentions
    this". Paired with ``ESCAPE '\\'`` at each call site.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_dt(value: object) -> datetime | None:
    """Decode a persisted ISO timestamp, tolerating a NULL or a corrupt row."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_SCHEDULED_COLUMNS = (
    "id, chat_id, message, due_at, created_at, effort, state, reason,"
    " hold_reason, attachments"
)


def _held_attachments(value: object) -> tuple[HeldAttachment, ...]:
    """Decode a held message's uploads, which this module wrote itself."""
    if not isinstance(value, str) or not value:
        return ()
    try:
        items = json.loads(value)
    except ValueError:
        return ()
    if not isinstance(items, list):
        return ()
    return tuple(
        HeldAttachment(Path(item["path"]), item["kind"])
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item.get("kind") in ("image", "file")
    )

class ChatNote(NamedTuple):
    """A GitHub comment a chat shows and its agent never sees (#3227)."""

    repository: str
    comment_id: int
    chat_id: str
    number: int
    is_pull: bool
    author: str
    url: str
    body: str
    body_html: str | None
    created_at: str
    updated_at: str


class SessionRunBinding(NamedTuple):
    """One session's participation in one Run of one Work (#2142)."""

    session_key: str
    work_id: str
    run_id: str
    first_delivery_id: str | None
    bound_at: str


class SessionWorkBinding(NamedTuple):
    """A delegated session's saved Work outcome (#2207)."""

    session_key: str
    work_id: str
    evidence: str  # delegated | backfilled | ambiguous
    parent_chat_id: str
    bound_at: str


class PendingSideChat(NamedTuple):
    """A side chat started from a Work's page and not yet linked to it (#2388)."""

    action_id: str  # also the new chat's session token
    work_id: str
    pull_requests: tuple[str, ...]
    created_at: datetime


class DeliveryRoute(NamedTuple):
    """The target one logical delivery was assigned to by the runtime's scheduler (#2146)."""

    delivery_id: str
    request_fingerprint: str
    work_id: str
    run_id: str
    provider: str
    account: str
    worker_key: str
    decided_at: str
    # Why the chosen account was already unable to run a turn when this route was frozen, or
    # "" when it was not (or could not be judged). Evidence about this delivery, not about the
    # account whenever someone later looks (#1519, #2200).
    refused_at_delivery: str = ""


class RoutingStoreError(Exception):
    """The routing store could not answer. Never read as "nothing is frozen"."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_history (
    account_key TEXT NOT NULL,
    ts TEXT NOT NULL,
    five_hour_pct REAL,
    seven_day_pct REAL,
    -- Billing-cycle meter (#730, Cursor). Added after this table shipped, so
    -- ordered migration 6 is what puts it on an existing database.
    month_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_acct_ts
    ON usage_history(account_key, ts);
CREATE TABLE IF NOT EXISTS sessions_seen (
    session_key TEXT PRIMARY KEY,
    account_key TEXT,
    title TEXT,
    cwd TEXT,
    first_seen TEXT,
    last_seen TEXT,
    -- The session's kind, so search can leave subagents out of its answers
    -- (#2055). Added after this table shipped, so ordered migration 10 is
    -- what puts it on an existing database; a row written before it exists
    -- reads as NULL until a scan or the transcript indexer says otherwise.
    kind TEXT
);
-- Deck-level chat identity. A provider session remains one immutable leg; the
-- ordered UNIQUE positions make the V1 chain linear and append-only.
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_legs (
    chat_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    session_key TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, position)
);
CREATE INDEX IF NOT EXISTS idx_chat_legs_session ON chat_legs(session_key);
-- The operator's model choice for one logical chat (#1665). It is deliberately a
-- soft preference and not a pin: it says which model the chat's *next* leg should
-- start on, while account and provider selection stay with the existing headroom
-- and affinity rules. It is keyed by chat rather than by session key because every
-- other per-conversation table is keyed per leg, and a leg is exactly what this
-- outlives.
CREATE TABLE IF NOT EXISTS chat_model_preferences (
    chat_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_deliveries (
    action_id TEXT PRIMARY KEY,
    attempt INTEGER NOT NULL DEFAULT 1,
    provider_id TEXT NOT NULL, account_key TEXT NOT NULL,
    worker_key TEXT NOT NULL, delivery_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, chat_id TEXT NOT NULL,
    source_session_key TEXT NOT NULL, source_session_id TEXT NOT NULL,
    target_session_key TEXT, runtime_capability_version TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('off', 'observe', 'enforce')),
    proposed_action TEXT NOT NULL CHECK (proposed_action IN ('none', 'compact', 'rollover')),
    executed_action TEXT NOT NULL CHECK (executed_action IN ('none', 'compact', 'rollover')),
    reason TEXT NOT NULL,
    dispatch_generation TEXT, phase_delivery_id TEXT, phase_source_session TEXT,
    runtime_fingerprint TEXT,
    delivery_stage TEXT NOT NULL DEFAULT 'legacy_unknown',
    provider_turn_id TEXT, pre_write_cursor TEXT, post_write_cursor TEXT,
    evidence_checked TEXT, uncertainty_reason TEXT,
    resolution_choice TEXT, resolution_rationale TEXT, resolved_at TEXT,
    source_last_event_seq INTEGER,
    expected_session_id TEXT, expected_last_result_at REAL,
    before_tokens INTEGER, after_tokens INTEGER,
    maintenance_completed_at TEXT,
    after_observed_at TEXT, after_observation_version INTEGER,
    rearmed INTEGER NOT NULL DEFAULT 0,
    handoff_text TEXT, handoff_sha256 TEXT, handoff_chars INTEGER NOT NULL DEFAULT 0,
    effective_cwd TEXT, effective_model TEXT, effective_effort TEXT,
    effective_permission_mode TEXT, effective_role TEXT,
    effective_delegated INTEGER NOT NULL DEFAULT 0,
    -- The chat's model preference as it stood when this delivery froze (#1665).
    -- Added after this table shipped, so ordered migration 7 is what puts it
    -- on an existing database.
    preferred_model TEXT,
    session_token TEXT, final_payload_sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'reserved', 'context_pending', 'successor_created', 'message_pending',
        'completed', 'clean_failed', 'uncertain', 'cancelled'
    )),
    maintenance_result TEXT CHECK (
        maintenance_result IS NULL OR maintenance_result IN ('succeeded', 'failed', 'unknown')
    ),
    provider_write_state TEXT NOT NULL DEFAULT 'not_started' CHECK (
        provider_write_state IN ('not_started', 'started', 'unknown')
    ),
    successor_created INTEGER NOT NULL DEFAULT 0,
    retryable_same_delivery INTEGER NOT NULL DEFAULT 0,
    final_accepted INTEGER, final_action TEXT, final_reason TEXT,
    final_session_id TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    -- The delivery's Work and Run (#2142), frozen with the row. Added after this
    -- table shipped, so migration 9 puts them on an existing database.
    work_id TEXT, run_id TEXT,
    UNIQUE(provider_id, account_key, worker_key, delivery_id)
);
CREATE INDEX IF NOT EXISTS idx_context_delivery_identity
    ON context_deliveries(provider_id, account_key, worker_key, delivery_id);
CREATE INDEX IF NOT EXISTS idx_context_delivery_source_time
    ON context_deliveries(source_session_key, created_at);
CREATE INDEX IF NOT EXISTS idx_context_delivery_chat_time
    ON context_deliveries(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_context_delivery_status_time
    ON context_deliveries(status, updated_at);
-- A target leg can be scanned and publish its singleton URL just before its
-- Continue token binds. Absorbing it must never invalidate that URL.
CREATE TABLE IF NOT EXISTS chat_aliases (
    alias_chat_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL
);
-- One counter every write to chat_legs or chat_aliases moves, from any connection
-- in any process, so the in-memory chat index reloads only when those two tables
-- changed rather than on every commit to the file (#2665).
CREATE TABLE IF NOT EXISTS chat_index_stamp (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    stamp INTEGER NOT NULL
);
INSERT OR IGNORE INTO chat_index_stamp(id, stamp) VALUES (0, 0);
CREATE TRIGGER IF NOT EXISTS chat_legs_stamp_insert AFTER INSERT ON chat_legs
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
CREATE TRIGGER IF NOT EXISTS chat_legs_stamp_update AFTER UPDATE ON chat_legs
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
CREATE TRIGGER IF NOT EXISTS chat_legs_stamp_delete AFTER DELETE ON chat_legs
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
CREATE TRIGGER IF NOT EXISTS chat_aliases_stamp_insert AFTER INSERT ON chat_aliases
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
CREATE TRIGGER IF NOT EXISTS chat_aliases_stamp_update AFTER UPDATE ON chat_aliases
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
CREATE TRIGGER IF NOT EXISTS chat_aliases_stamp_delete AFTER DELETE ON chat_aliases
    BEGIN UPDATE chat_index_stamp SET stamp = stamp + 1 WHERE id = 0; END;
-- Reserve the one successor a leg may create before provider I/O. Keeping the
-- stable token makes retries idempotent while the source PK prevents forks.
CREATE TABLE IF NOT EXISTS chat_successors (
    source_session_key TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    target_session_key TEXT UNIQUE,
    reserved_at TEXT NOT NULL,
    linked_at TEXT
);
-- A side chat: a new chat started as a copy of another session's history cut
-- after one transcript line (#2485). Keyed by the start's session token, so the
-- side chat is found through session_tokens once the provider names it and a
-- start that never got that far leaves a row nothing reads. Deliberately not a
-- chat_successors row: that chain stays linear.
CREATE TABLE IF NOT EXISTS chat_forks (
    token TEXT PRIMARY KEY,
    source_session_key TEXT NOT NULL,
    source_seq INTEGER NOT NULL,
    action_id TEXT UNIQUE,
    reserved_at TEXT NOT NULL
);
-- Transcript lines a person rolled out of a chat (#3474), first to last inclusive. The
-- provider's file keeps them (Claude branches the same file), so the deck leaves
-- them out of every read of that leg.
CREATE TABLE IF NOT EXISTS transcript_rollbacks (
    session_key TEXT NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    rolled_back_at TEXT NOT NULL,
    PRIMARY KEY (session_key, first_seq, last_seq)
);
-- The operator's durable read marker. Kept apart from sessions_seen because a
-- detail page can be opened before the next collector write, and opening a chat
-- must never have to manufacture discovery metadata for it (#1154).
CREATE TABLE IF NOT EXISTS session_last_opened (
    session_key TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL
);
-- Authoritative Session Project resolutions, keyed the way Spend History
-- attributes transcripts. Only repository-anchored names are written here, so a
-- session whose later evidence is a generic temporary root is still attributed
-- to the project AgentDeck genuinely identified earlier (#689).
CREATE TABLE IF NOT EXISTS session_projects (
    account_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_key, session_id)
);
-- Legacy tables retained so removing budget enforcement never destroys existing
-- installations' data. No current code reads or writes these rows (#1497).
CREATE TABLE IF NOT EXISTS session_budgets (
    session_key TEXT PRIMARY KEY,
    limit_micros INTEGER NOT NULL,
    spent_micros INTEGER,
    unpriced_tokens INTEGER NOT NULL DEFAULT 0,
    partial INTEGER NOT NULL DEFAULT 0,
    boss_granted INTEGER NOT NULL DEFAULT 0,
    spend_accounting_version INTEGER NOT NULL DEFAULT 1,
    -- The unpriced ceiling (#729). NULL on every session whose provider can be
    -- priced in USD, which is what keeps the token test inert for them.
    token_limit INTEGER,
    tokens_used INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_budget_defaults (
    kind TEXT PRIMARY KEY,
    limit_micros INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS generated_titles (
    session_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    evidence_signature TEXT NOT NULL,
    -- Rows that predate agent-authored titles are Deckhand's own, which is
    -- exactly what the default backfills on an existing database.
    source TEXT NOT NULL DEFAULT 'deckhand',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_tokens (
    token TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Per-session escape hatch from the owned Codex runtime's auto-review default.
-- Absence is the safe/default state: approval requests stay with auto-review.
CREATE TABLE IF NOT EXISTS session_permission_prompts (
    session_key TEXT PRIMARY KEY,
    previous_policy_json TEXT NOT NULL,
    enabled_at TEXT NOT NULL
);
-- A handoff callback is an agent-authored write that may outlive the web
-- request which asked for it. Reserve its pre-minted destination token before
-- provider I/O so replay can return the same waiting-chat URL, never spawn a
-- second chat after an uncertain response (#909).
CREATE TABLE IF NOT EXISTS continued_chat_starts (
    token TEXT PRIMARY KEY,
    source_session_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved', 'dispatched')),
    reserved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_pull_requests (
    session_key TEXT NOT NULL,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    work_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_key, repository, number)
);
CREATE TABLE IF NOT EXISTS session_issues (
    session_key TEXT NOT NULL,
    repository TEXT NOT NULL,
    number INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (session_key, repository, number)
);
-- Work inventory (#2140). work_id is derived from the source (work.work_id_for),
-- so this table is history, never the thing that decides an id.
CREATE TABLE IF NOT EXISTS works (
    work_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Which sessions took part in which Run of which Work (#2142). A participation
-- fact, not ownership: one session may hold several rows and one Run several
-- sessions. first_delivery_id is the delivery that first bound them.
CREATE TABLE IF NOT EXISTS session_run_bindings (
    session_key TEXT NOT NULL,
    work_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    first_delivery_id TEXT,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (session_key, work_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_session_run_bindings_run
    ON session_run_bindings(work_id, run_id);
-- Which target the runtime's scheduler assigned one logical delivery (#2146). Its only
-- job is to freeze that choice: once provider I/O may have started, a retry of the same
-- delivery_id must reach the same account. Whether the write started, finished or is
-- uncertain stays on the provider's own delivery receipt and is never copied here.
CREATE TABLE IF NOT EXISTS session_work_bindings (
    session_key TEXT NOT NULL,
    work_id TEXT NOT NULL,
    evidence TEXT NOT NULL,
    parent_chat_id TEXT NOT NULL DEFAULT '',
    bound_at TEXT NOT NULL,
    PRIMARY KEY (session_key, work_id)
);
-- A side chat started from a Work's page, until it is linked (#2388). The press's own
-- action id is also the new chat's session token, so ``session_tokens`` names the chat;
-- the issue comes from the work_id. Only the pull requests seen at the press cannot be
-- rebuilt. The row is deleted once the chat carries its links.
CREATE TABLE IF NOT EXISTS work_side_chats (
    action_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    pull_requests TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_backfills (
    name TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_routes (
    delivery_id TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL,
    work_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account TEXT NOT NULL,
    worker_key TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    refused_at_delivery TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_delivery_routes_work
    ON delivery_routes(work_id, decided_at);
CREATE TABLE IF NOT EXISTS session_attention_reports (
    session_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_open_questions (
    session_key TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    source TEXT NOT NULL,
    asked_seq INTEGER NOT NULL,
    observed_seq INTEGER NOT NULL,
    answered_seq INTEGER,
    observed_session_key TEXT,
    answered_session_key TEXT
);
CREATE TABLE IF NOT EXISTS assistant_checkpoint (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegated_sessions (
    session_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    parent_session_id TEXT
);
-- One row per chat, bumped when the boss writes into that chat (#472).
-- Opening a new window lets a mute lease's spent hard cap be re-earned.
CREATE TABLE IF NOT EXISTS boss_turn_windows (
    session_key TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL
);
-- One row per board command the boss sent from a chat (#1131). Nothing writes or reads it
-- since the Poller's board commands were retired (#3025); the table stays so old databases
-- need no migration.
CREATE TABLE IF NOT EXISTS board_commands (
    action_id TEXT PRIMARY KEY,
    command TEXT NOT NULL CHECK (command IN ('merge', 'merge-review')),
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    -- The chat the button was pressed on. Not part of any identity: it says
    -- who asked, for the audit read, while the card is what was acted on.
    session_key TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    settled_at TEXT,
    outcome TEXT CHECK (outcome IN ('done', 'failed')),
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_board_commands_issue
    ON board_commands(repo, issue_number);
-- One row per write action the operator pressed on a Work (#2237), reserved
-- before the external write and settled after it. ``board_commands`` is keyed
-- to a chat and checks its two commands, so Work actions keep their own small
-- table under the same discipline: the client's action id is the identity, an
-- unsettled row may have landed and is never repeated, and ``result`` is the
-- receipt the Work page reads back. ``action`` is checked in Python against the
-- Work action allow-list.
CREATE TABLE IF NOT EXISTS work_actions (
    action_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    settled_at TEXT,
    outcome TEXT CHECK (outcome IN ('done', 'failed')),
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_actions_work ON work_actions(work_id, reserved_at);
-- One row per Work the operator hid from Needs you (#2238). A display preference of this
-- deck only: nothing on GitHub or the board changes. ``comments`` is the source issue's
-- comment count and ``card_mark`` the Poller card's fingerprint (``work_hidden.card_mark``)
-- when it was hidden; either moving on expires the row. NULL is "not known then".
CREATE TABLE IF NOT EXISTS work_hidden (
    work_id TEXT PRIMARY KEY,
    hidden_at TEXT NOT NULL,
    comments INTEGER,
    card_mark TEXT
);
-- One row per Work a person paused on this deck (#2559), for a Work whose pause no Poller label
-- carries: no live card, so the deck itself is all that could start anything on it. While the
-- row is there the deck sends nothing to the Work's chats; Resume deletes it.
CREATE TABLE IF NOT EXISTS work_paused (
    work_id TEXT PRIMARY KEY,
    paused_at TEXT NOT NULL
);
-- One row per Work a person starred (#3116): it goes ahead of unstarred Works in the merge
-- queue, the job queue, review starts, start retries and the front page. Kept until unstarred.
CREATE TABLE IF NOT EXISTS work_starred (
    work_id TEXT PRIMARY KEY,
    starred_at TEXT NOT NULL
);
-- A person's sign-off on each Work whose issue closed (#2510, ``work_signoff``): ``closed_at``
-- is the issue's latest close the deck saw, ``finished_at`` when someone pressed Finish (or
-- closed the issue from the deck), NULL while the Work waits for that. A display record of this
-- deck only: GitHub and the board never read it.
CREATE TABLE IF NOT EXISTS work_signoff (
    work_id TEXT PRIMARY KEY,
    closed_at TEXT NOT NULL,
    finished_at TEXT
);
-- When each Work entered its current Needs you category (#2283, ``work_stops``), and "" once
-- it left, so entering again is a new stop. A display record of this deck only.
CREATE TABLE IF NOT EXISTS work_stops (
    work_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    stopped_at TEXT NOT NULL,
    -- ``work_stops.facts_mark`` when the row was written (#2332); NULL on older rows.
    facts_mark TEXT
);
-- Every change of a Work's front-page status (#2751, ``work_status_log``): its section,
-- Needs you category, status kind and tone, the ones it replaced, and the facts and failed
-- reads behind it. Append-only and diagnostic; rows older than 30 days are dropped at open.
CREATE TABLE IF NOT EXISTS work_status_log (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    work_id TEXT NOT NULL,
    section TEXT NOT NULL,
    category TEXT NOT NULL,
    kind TEXT NOT NULL,
    tone TEXT NOT NULL,
    headline TEXT NOT NULL,
    prev_section TEXT NOT NULL,
    prev_category TEXT NOT NULL,
    prev_kind TEXT NOT NULL,
    prev_tone TEXT NOT NULL,
    facts_mark TEXT,
    why TEXT NOT NULL,
    degraded TEXT NOT NULL,
    pid INTEGER,
    revision TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_status_log_work ON work_status_log(work_id, at);
CREATE INDEX IF NOT EXISTS idx_work_status_log_at ON work_status_log(at);
-- When the operator last opened each Work's page (#2283): a Work that stopped after that is
-- unseen on the front page.
CREATE TABLE IF NOT EXISTS work_opened (
    work_id TEXT PRIMARY KEY,
    opened_at TEXT NOT NULL
);
-- Work-owned metadata (#2328, ``work_resolution``): a custom title the operator gave the Work
-- on this deck (NULL is the derived title; the GitHub issue is never renamed), and the
-- resolution the verify worker writes: problem, solution, why this solution, why it is
-- correct. Every resolution write replaces all four parts; the Run and chat that wrote it
-- last are kept as provenance. ``agent_title`` is the name a worker gave the Work when it
-- filed its report (#2474): the operator's ``title`` still wins over it. ``live_check_*`` is
-- the agent's check of the merged change in the live app (#3141), written apart from the
-- resolution so a later resolution write never clears it; ordered migration 15 adds it.
CREATE TABLE IF NOT EXISTS work_metadata (
    work_id TEXT PRIMARY KEY,
    title TEXT,
    title_updated_at TEXT,
    problem_summary TEXT,
    solution_summary TEXT,
    decision_rationale TEXT,
    correctness_summary TEXT,
    resolution_updated_at TEXT,
    resolution_run_id TEXT,
    resolution_session_key TEXT,
    agent_title TEXT,
    agent_title_updated_at TEXT,
    agent_title_session_key TEXT,
    live_check_result TEXT,
    live_check_note TEXT,
    live_check_at TEXT,
    live_check_session_key TEXT
);
-- Where a person moved a wayfinder map's boxes (#3627): one JSON object per map Work, box key
-- to ``[x, y]``, the same on every device. A box not named keeps the server's layout.
CREATE TABLE IF NOT EXISTS wayfinder_layout (
    work_id TEXT PRIMARY KEY,
    positions TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- When this deck first recorded stops (#2283): one row once the first pass ran, even if it
-- found nothing waiting, so only that pass counts what already waits as seen.
CREATE TABLE IF NOT EXISTS work_stops_started (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL
);
-- One row per run the Mac command-approval program has reported an outcome for
-- (#1865), reserved before the message is injected and settled after it. The
-- program retries a callback it could not deliver, so the run id is the identity:
-- a repeat for a run already delivered changes nothing at all, while a repeat for
-- one that failed mid-flight is re-attempted under the same injection action id,
-- which the runtime's delivery fence collapses into the first message rather than
-- a second one. Nothing about the command itself is kept here; the program's own
-- run list is where that lives.
CREATE TABLE IF NOT EXISTS mac_runner_callbacks (
    run_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    settled_at TEXT,
    state TEXT CHECK (state IN ('delivered', 'failed')),
    detail TEXT,
    -- When a send under this run's action id actually reached the provider, so
    -- the runtime's delivery fence is armed and a later retry will be collapsed
    -- into that first message rather than becoming a second one. Deliberately
    -- outside everything `reserve` resets: the fence is disk-backed and lives in
    -- the persistent runtime, so the evidence that it is armed has to survive a
    -- web restart the same way the fence itself does (#2003). Null means no send
    -- has reached the provider yet, and the next attempt is the first that can.
    handed_over_at TEXT
);
-- Optimistic rows a person threw away (#2031).
--
-- A caller with a stable action id retries until the deck confirms delivery, and
-- a message the agent took and never wrote down is never confirmed — so the Mac
-- program is still re-posting an outcome whose row has been dismissed. The queue
-- alone cannot hold that decision: it is memory, and a frontend deploy empties
-- it several times a day, after which the next retry would draw the dismissed
-- row all over again. So the decision is kept here instead.
--
-- Deliberately not a delivery record. Nothing in here claims a message reached
-- an agent; it only says a person is done looking at this one.
--
-- Keyed by the action id alone, never by the leg it was dismissed on. Action ids
-- are unique per send, and a chat's work moves between legs: a Mac callback that
-- arrives after a rollover is deliberately retargeted to whichever leg is
-- working now, so a dismissal recorded against the leg the row was drawn on
-- would be invisible to the very retry it exists to stop (#2031). The leg is
-- kept alongside as a record of where the person was, and is not read.
CREATE TABLE IF NOT EXISTS dismissed_deliveries (
    action_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    dismissed_at TEXT NOT NULL
);
-- A delegation's status, kept so it outlives the web process that started it (#2582).
-- The child chat runs in the persistent runtime and goes on through a web deploy; the
-- record of who is waiting for it must too, or the caller polls a 404 until it times out.
-- owner_pid is the web process watching the child now. A new process takes over an
-- unfinished row only once that process has exited, because a deploy runs the old and
-- new web processes side by side for a while and only one of them may write the result.
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    account_key TEXT NOT NULL,
    session_key TEXT,
    session_token TEXT,
    reason TEXT,
    final_message TEXT,
    owner_pid INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS manual_new_chat_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    cwd TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_new_chat_defaults (
    account_key TEXT PRIMARY KEY,
    model TEXT,
    effort TEXT,
    updated_at TEXT NOT NULL
);
-- When a person will reset an account's usage by hand (#3379); see planned_reset.py.
CREATE TABLE IF NOT EXISTS planned_resets (
    account_key TEXT PRIMARY KEY,
    reset_at TEXT NOT NULL
);
-- The deck's edited quick phrases (#743), one ordered setting rather than
-- per-row state: every save replaces the whole table. No rows means nobody has
-- customized this deck, so the built-in vocabulary applies.
CREATE TABLE IF NOT EXISTS composer_phrases (
    position INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    message TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- The `/settings` "Guess for me" switch (#3367). No row means on.
CREATE TABLE IF NOT EXISTS guess_preferences (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
-- A deferred message (wake-on, or one held while the runtime restarts) is a
-- durable intent, not an in-process timer. The ID
-- is also the provider delivery ID, so a row left in `dispatching` by a web
-- restart can be retried without writing the turn twice.
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    message TEXT NOT NULL,
    effort TEXT,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('scheduled', 'dispatching', 'delivered', 'failed', 'cancelled')
    ),
    reason TEXT,
    updated_at TEXT NOT NULL,
    -- Set on a message held while its runtime restarts (#2847); NULL for wake-on.
    hold_reason TEXT,
    -- JSON list of {"path", "kind"} uploads a held message keeps until delivery.
    attachments TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_due
    ON scheduled_messages(state, due_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_messages_chat
    ON scheduled_messages(chat_id, due_at);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_vapid (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    public_key TEXT NOT NULL,
    private_pem TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- The top bar's new Needs you Works the phone was already told about (#3348), as a JSON
-- list of [work_id, stopped_at]: no row yet means the first pass records the list unsent.
CREATE TABLE IF NOT EXISTS push_needs_you_sent (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- The Needs you notifications a phone may still show (#3365), as a JSON object of tag to
-- the Works it names: one whose Works all left the new list is cleared on every device.
CREATE TABLE IF NOT EXISTS push_needs_you_showing (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Nothing reads this since #2886 removed the finished-notification setting; it stays so
-- an existing database needs no migration.
CREATE TABLE IF NOT EXISTS push_preferences (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    finished_mode TEXT NOT NULL DEFAULT 'all' CHECK (finished_mode IN ('all', 'idle')),
    updated_at TEXT NOT NULL
);
-- Capacity calibration (#535). Deliberately its own model: this
-- needs synchronized meter/reset markers and account-wide spend
-- counters that the percentage-only usage_history cannot carry.
CREATE TABLE IF NOT EXISTS capacity_scan (
    account_key TEXT PRIMARY KEY,
    scanned_through TEXT
);
CREATE TABLE IF NOT EXISTS capacity_windows (
    account_key TEXT NOT NULL,
    window TEXT NOT NULL,
    anchor_pct REAL,
    anchor_resets_at TEXT,
    anchor_started_at TEXT,
    accrued_micros INTEGER NOT NULL DEFAULT 0,
    accrued_unpriced INTEGER NOT NULL DEFAULT 0,
    accrued_partial INTEGER NOT NULL DEFAULT 0,
    estimate_micros INTEGER,
    estimate_updated_at TEXT,
    estimate_last_sample_at TEXT,
    estimate_sample_count INTEGER,
    estimate_spread_pct REAL,
    estimate_low_confidence INTEGER NOT NULL DEFAULT 0,
    estimate_provisional INTEGER NOT NULL DEFAULT 0,
    estimate_partial INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_key, window)
);
CREATE TABLE IF NOT EXISTS capacity_samples (
    account_key TEXT NOT NULL,
    window TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    capacity_micros INTEGER NOT NULL,
    pct_delta REAL NOT NULL,
    partial INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_capacity_samples_acct
    ON capacity_samples(account_key, window, observed_at);
CREATE TABLE IF NOT EXISTS capacity_history (
    account_key TEXT NOT NULL,
    window TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    capacity_micros INTEGER NOT NULL,
    previous_micros INTEGER,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capacity_history_acct
    ON capacity_history(account_key, window, changed_at);
-- What each poller merge review decided about one commit (#1122). Keyed by the
-- reviewed pull request's own repository and head commit, which is the thing a
-- verdict is actually about; that repository is not always the card's, because
-- one card may ship several. The card record that held this is deleted the
-- moment the release progression consumes the verdict, so without this table
-- nothing remembers a commit was reviewed at all. `verdict` is what the
-- reviewer reported and `result` what the release progression then did with it.
-- Re-reviewing a commit replaces its row: the key is the commit, not the round.
CREATE TABLE IF NOT EXISTS poller_review_verdicts (
    repo TEXT NOT NULL,
    sha TEXT NOT NULL,
    pull_request TEXT,
    issue_repo TEXT,
    issue_number INTEGER,
    verdict TEXT NOT NULL,
    result TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    generation TEXT,
    reviewed_at TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (repo, sha)
);
CREATE INDEX IF NOT EXISTS idx_poller_review_verdicts_recorded
    ON poller_review_verdicts(recorded_at);

-- Which commit a ChatGPT review chat was opened about, per pull request it covers
-- (#2088, #2637). The head at the moment the deck wrote the chat is the head that chat
-- reviews — and comparing it with the pull request's current head is what lets the chip
-- and the Work's review step say whether the latest commit has been reviewed. Keyed by
-- the chat and the pull request: a Work's chat lists several, and two reviews of one pull
-- request are two chats. Only the deck's own calls write here, so a chat we have no row
-- for is a chat from before this existed, which renders as silence.
CREATE TABLE IF NOT EXISTS chat_review_heads (
    chat_repository TEXT NOT NULL,
    chat_number INTEGER NOT NULL,
    target_repo TEXT NOT NULL,
    target_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (chat_repository, chat_number, target_repo, target_number)
);

-- A note a chat shows but its agent never sees (#3227): a comment the deck's own GitHub
-- account posted on the chat's issue or one of its pull requests, from the webhook log.
-- ``chat_id`` is the chat that owned the item when the comment arrived. ``body_html`` is
-- GitHub's own rendering, read once per created or edited comment; NULL shows the plain
-- text until it lands.
CREATE TABLE IF NOT EXISTS chat_notes (
    repository TEXT NOT NULL,
    comment_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    is_pull INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL,
    body_html TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repository, comment_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_notes_chat ON chat_notes(chat_id, created_at);

-- The deck chat's starts (#3403), by the token each start was minted under: ``first``
-- began the deck chat, each ``reset`` began a leg of it with a clean context after an
-- idle hour. The token resolves to the leg's session key once the provider names one.
CREATE TABLE IF NOT EXISTS deck_chat_starts (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- A press that took a spare CLI booted ahead of it (#3420), by the client's action
-- id: a resend of that press must name the same worker key and token, or it would
-- start a second chat on the next spare. Kept a day.
CREATE TABLE IF NOT EXISTS start_spare_claims (
    action_id TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    worker_key TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
"""


@functools.lru_cache(maxsize=1)
def _declared_columns() -> dict[str, tuple[tuple[str, str, bool, str | None], ...]]:
    """The column shape ``_SCHEMA`` declares, read back from a throwaway database.

    Building it by *running* the schema instead of parsing it means the expected
    shape can never drift from the statements a fresh database is created from.
    """
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(_SCHEMA)
        tables = [
            str(row[0])
            for row in scratch.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: tuple(
                (str(name), str(decl_type), bool(notnull), default)
                for _cid, name, decl_type, notnull, default, _pk in scratch.execute(
                    f"PRAGMA table_info({table})"
                )
            )
            for table in tables
        }
    finally:
        scratch.close()


def _is_schema_error(exc: sqlite3.Error) -> bool:
    """True when the statement referenced something this database does not have."""
    message = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "no such column" in message
        or "no such table" in message
        or "has no column named" in message
    )


@dataclass
class ChatSnapshot:
    """In-memory mirror of ``chat_legs`` and ``chat_aliases`` (#1291).

    ``legs_by_chat`` holds each chat's legs in position order, ``chat_by_session``
    is the reverse lookup, and ``aliases`` maps an absorbed chat id to the one
    that swallowed it — the same three facts the two SELECTs used to fetch.

    A caller that walks every session takes one of these from ``chat_snapshot``
    and reads it directly, so the whole pass pays one freshness check rather
    than one per session. It is a view, not a copy: it stays valid for the
    synchronous pass that took it, and is not something to hold across an await.
    """

    legs_by_chat: dict[str, tuple[str, ...]] = field(default_factory=dict)
    chat_by_session: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    # Same-process writers update this view in place, while a commit from the
    # runtime's chat-table commit replaces it (``chat_index_stamp``). Readers that
    # retain derived state can therefore key on object identity plus revision
    # without copying all three maps (#1347).
    revision: int = 0

    def chat_by_id(self, chat_id: str) -> ChatIdentity | None:
        """Resolve a chat id, following an absorbed chat's alias to its owner."""
        seen: set[str] = set()
        current = chat_id
        while current not in seen:
            seen.add(current)
            legs = self.legs_by_chat.get(current)
            if legs:
                return ChatIdentity(current, legs)
            alias_target = self.aliases.get(current)
            if alias_target is None:
                return None
            current = alias_target
        return None

    def chat_for_session(self, session_key: str) -> ChatIdentity | None:
        chat_id = self.chat_by_session.get(session_key)
        if chat_id is None:
            return None
        return ChatIdentity(chat_id, self.legs_by_chat[chat_id])


class Db:
    def __init__(
        self,
        path: Path,
        retention_days: int = 30,
        context_retention_days: int = 30,
    ):
        self.path = path
        self.retention_days = retention_days
        self.context_retention_days = context_retention_days
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        use_wal(self._conn)
        # Read-mostly mirror of chat_legs/chat_aliases; see the chat identity
        # section for why it exists and how it stays fresh (#1291).
        self._chat_index: ChatSnapshot | None = None
        self._chat_index_version: int | None = None
        self._chat_index_stamp: int | None = None
        # Work ids already in ``works``, so a repeated sighting costs no write.
        self._known_work_ids: set[str] = set()
        self._init_schema()
        self._known_work_ids = set(self.load_works())

    def _init_schema(self) -> None:
        with self._conn:
            # Upgrade the tables that already exist first; ``_SCHEMA`` then
            # creates any missing table directly in its current shape.
            apply_ordered_migrations(self._conn)
            # One transaction, not one per statement: a hundred separate commits
            # each wait on the disk, which made opening a fresh database cost
            # seconds (#3052).
            create_schema(self._conn, _SCHEMA)
            self._backfill_chat_identities()
        self._prune_usage_history()
        self.prune_context_deliveries(self.context_retention_days)
        self._prune_work_status_log()

    # --- context delivery ledger (#1471) -------------------------------

    _CONTEXT_FIELDS = tuple(ContextDelivery.__dataclass_fields__)

    def _context_row(self, values: tuple | None) -> ContextDelivery | None:
        if values is None:
            return None
        data = dict(zip(self._CONTEXT_FIELDS, values, strict=True))
        for name in (
            "effective_delegated",
            "rearmed",
            "successor_created",
            "retryable_same_delivery",
        ):
            data[name] = bool(data[name])
        if data["final_accepted"] is not None:
            data["final_accepted"] = bool(data["final_accepted"])
        return ContextDelivery(**data)

    def reserve_context_delivery(self, row: ContextDelivery) -> ContextDelivery | None:
        """Transactionally reserve or reload one worker/delivery identity."""
        now = datetime.now(UTC).isoformat()
        values = [getattr(row, name) for name in self._CONTEXT_FIELDS]
        values[self._CONTEXT_FIELDS.index("created_at")] = row.created_at or now
        values[self._CONTEXT_FIELDS.index("updated_at")] = row.updated_at or now
        columns = ", ".join(self._CONTEXT_FIELDS)
        placeholders = ", ".join("?" for _ in self._CONTEXT_FIELDS)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    f"INSERT OR IGNORE INTO context_deliveries ({columns}) VALUES ({placeholders})",
                    values,
                )
                found = self._conn.execute(
                    f"SELECT {columns} FROM context_deliveries"
                    " WHERE provider_id=? AND account_key=?"
                    " AND worker_key=? AND delivery_id=?",
                    (row.provider_id, row.account_key, row.worker_key, row.delivery_id),
                ).fetchone()
            return self._context_row(found)
        except sqlite3.Error as exc:
            self._log_failure("context delivery reserve", exc)
            return None

    def get_context_delivery(
        self, provider_id: str, account_key: str, worker_key: str, delivery_id: str
    ) -> ContextDelivery | None:
        columns = ", ".join(self._CONTEXT_FIELDS)
        rows = self._query(
            f"SELECT {columns} FROM context_deliveries"
            " WHERE provider_id=? AND account_key=?"
            " AND worker_key=? AND delivery_id=?",
            (provider_id, account_key, worker_key, delivery_id),
        )
        return self._context_row(rows[0]) if rows else None

    def rearm_cancelled_context_delivery(self, action_id: str) -> bool:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE context_deliveries SET status='reserved', attempt=attempt+1,"
                    " error=NULL, updated_at=? WHERE action_id=? AND status='cancelled'"
                    " AND error='worker_changed' AND provider_write_state='not_started'"
                    " AND successor_created=0 AND target_session_key IS NULL",
                    (datetime.now(UTC).isoformat(), action_id),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("context delivery rearm", exc)
            return False

    def rearm_clean_failed_context_delivery(self, action_id: str) -> bool:
        """Re-arm a delivery whose refusal provably wrote nothing (#1491).

        ``clean_failed`` is the ledger's own word for a delivery the runtime
        rejected *before* touching the provider: no write started, no successor,
        no target session. Every later ``mark_*`` transition already accepts it
        as a retryable predecessor. The one thing it could not survive was a
        retry that re-rendered its request — the frozen hash refused that as a
        ``delivery_id_conflict``, so an ordinary ``at_capacity`` refusal made the
        delivery ID permanently undeliverable. Re-arming here is what lets the
        request hash be replaced; the guards below are what keep at-most-once,
        because a delivery that may have written is never ``clean_failed``.

        ``retryable_same_delivery`` is excluded because that row is a clean failure
        the provider asked to be resumed under this exact delivery, with context
        maintenance still standing behind it (#1493); re-rendering would drop it.
        """
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE context_deliveries SET status='reserved', attempt=attempt+1,"
                    " error=NULL, final_accepted=NULL, final_action=NULL,"
                    " final_reason=NULL, final_session_id=NULL, updated_at=?"
                    " WHERE action_id=? AND status='clean_failed'"
                    " AND provider_write_state='not_started'"
                    " AND successor_created=0 AND target_session_key IS NULL"
                    " AND retryable_same_delivery=0"
                    " AND COALESCE(final_accepted, 0)=0",
                    (datetime.now(UTC).isoformat(), action_id),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("context delivery clean rearm", exc)
            return False

    def refresh_rearmed_context_delivery(
        self, action_id: str, row: ContextDelivery, *, request_hash: bool = False
    ) -> bool:
        """Replace frozen pre-I/O evidence after a clean rearm.

        ``request_hash`` is replaced only for a clean-failure rearm: that retry is
        by definition a *re-rendered* request, so keeping the old hash would leave
        the row disagreeing with the payload it is about to fence. A worker-change
        rearm re-decides the same request and keeps its hash immutable.
        """
        immutable = {
            "action_id",
            "attempt",
            "provider_id",
            "account_key",
            "worker_key",
            "delivery_id",
            "created_at",
        }
        if not request_hash:
            immutable.add("request_sha256")
        changes = {
            name: getattr(row, name)
            for name in self._CONTEXT_FIELDS
            if name not in immutable
        }
        return self._context_transition(
            action_id,
            {"reserved"},
            **changes,
        )

    def adopt_context_correlation(self, action_id: str, work_id: str, run_id: str) -> bool:
        """Give a row frozen before #2142 its correlation, once and only if it has none."""
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE context_deliveries SET work_id=?, run_id=?, updated_at=?"
                    " WHERE action_id=? AND work_id IS NULL AND run_id IS NULL",
                    (work_id, run_id, datetime.now(UTC).isoformat(), action_id),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("context delivery correlation", exc)
            return False

    def _context_transition(
        self,
        action_id: str,
        allowed: set[str],
        *,
        attempt_sql: str | None = None,
        **changes: object,
    ) -> bool:
        changes["updated_at"] = datetime.now(UTC).isoformat()
        assignments = [f"{name} = ?" for name in changes]
        values = list(changes.values())
        if attempt_sql:
            assignments.append(f"attempt = {attempt_sql}")
        marks = ", ".join("?" for _ in allowed)
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    f"UPDATE context_deliveries SET {', '.join(assignments)}"
                    f" WHERE action_id=? AND status IN ({marks})",
                    (*values, action_id, *sorted(allowed)),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("context delivery transition", exc)
            return False

    def mark_context_pending(self, action_id: str) -> bool:
        return self._context_transition(
            action_id,
            {"reserved", "clean_failed"},
            attempt_sql="CASE WHEN status='clean_failed' THEN attempt + 1 ELSE attempt END",
            status="context_pending",
            error=None,
        )

    def mark_successor_created(self, action_id: str, target_session_key: str) -> bool:
        return self._context_transition(
            action_id,
            {"reserved", "context_pending", "uncertain"},
            status="successor_created",
            target_session_key=target_session_key,
            successor_created=True,
        )

    def mark_message_pending(self, action_id: str) -> bool:
        return self._context_transition(
            action_id,
            {"context_pending", "successor_created", "clean_failed"},
            status="message_pending",
        )

    def mark_completed(self, action_id: str, **result: object) -> bool:
        if result.get("maintenance_result") == "succeeded":
            result.setdefault("maintenance_completed_at", datetime.now(UTC).isoformat())
        return self._context_transition(
            action_id,
            {
                "reserved",
                "context_pending",
                "successor_created",
                "message_pending",
                "clean_failed",
                "uncertain",
            },
            status="completed",
            **result,
        )

    def mark_clean_failed(self, action_id: str, **result: object) -> bool:
        return self._context_transition(
            action_id,
            {
                "reserved",
                "context_pending",
                "successor_created",
                "message_pending",
                "uncertain",
            },
            status="clean_failed",
            **result,
        )

    def mark_uncertain(self, action_id: str, **result: object) -> bool:
        return self._context_transition(
            action_id,
            {"reserved", "context_pending", "successor_created", "message_pending"},
            status="uncertain",
            **result,
        )

    def record_context_evidence(self, action_id: str, **evidence: object) -> bool:
        """Persist monotonic reconciliation evidence without settling the fence."""
        return self._context_transition(
            action_id,
            {
                "reserved",
                "context_pending",
                "successor_created",
                "message_pending",
                "uncertain",
            },
            **evidence,
        )

    def resolve_context_delivery(
        self, action_id: str, delivery_id: str, choice: str, rationale: str
    ) -> ContextDelivery | None:
        """Apply one explicit, audited resolution; exact retries replay idempotently."""
        if (
            choice not in {"accepted", "not_written", "abandoned"}
            or not rationale.strip()
        ):
            return None
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                current = self._conn.execute(
                    f"SELECT {', '.join(self._CONTEXT_FIELDS)} FROM context_deliveries"
                    " WHERE action_id=? AND delivery_id=?",
                    (action_id, delivery_id),
                ).fetchone()
                row = self._context_row(current)
                if row is None:
                    return None
                if row.resolution_choice is not None:
                    return (
                        row
                        if (
                            row.resolution_choice == choice
                            and row.resolution_rationale == rationale.strip()
                        )
                        else None
                    )
                if row.status != "uncertain":
                    return None
                if choice == "not_written":
                    status = "clean_failed"
                    accepted = 0
                    action = "rejected"
                    reason = "operator_proved_not_written"
                    write_state = "not_started"
                    retryable = 1
                    stage = "prepared_before_write"
                else:
                    status = "completed"
                    accepted = 1 if choice == "accepted" else 0
                    action = "accepted" if choice == "accepted" else "rejected"
                    reason = (
                        "operator_confirmed_accepted"
                        if choice == "accepted"
                        else "operator_abandoned"
                    )
                    write_state = "started" if choice == "accepted" else "unknown"
                    retryable = 0
                    stage = (
                        "provider_turn_identified"
                        if choice == "accepted"
                        else "irreducibly_uncertain"
                    )
                self._conn.execute(
                    "UPDATE context_deliveries SET status=?, final_accepted=?, final_action=?,"
                    " final_reason=?, provider_write_state=?, retryable_same_delivery=?,"
                    " delivery_stage=?, resolution_choice=?, resolution_rationale=?,"
                    " resolved_at=?, updated_at=? WHERE action_id=? AND delivery_id=?"
                    " AND status='uncertain' AND resolution_choice IS NULL",
                    (
                        status,
                        accepted,
                        action,
                        reason,
                        write_state,
                        retryable,
                        stage,
                        choice,
                        rationale.strip(),
                        now,
                        now,
                        action_id,
                        delivery_id,
                    ),
                )
                found = self._conn.execute(
                    f"SELECT {', '.join(self._CONTEXT_FIELDS)} FROM context_deliveries"
                    " WHERE action_id=? AND delivery_id=?",
                    (action_id, delivery_id),
                ).fetchone()
            return self._context_row(found)
        except sqlite3.Error as exc:
            self._log_failure("context delivery resolution", exc)
            return None

    def mark_cancelled(self, action_id: str, error: str | None = None) -> bool:
        return self._context_transition(
            action_id,
            {"reserved", "context_pending"},
            status="cancelled",
            error=error,
        )

    def link_target(self, action_id: str, target_session_key: str) -> bool:
        return self._context_transition(
            action_id,
            {"reserved", "context_pending", "successor_created", "message_pending"},
            target_session_key=target_session_key,
        )

    def record_context_observation(
        self,
        provider_id: str,
        account_key: str,
        worker_key: str,
        source_session_id: str,
        after_tokens: int | None,
        *,
        observed_at: datetime,
        source_version: int,
        rearm_below_tokens: int,
    ) -> bool:
        """CAS the latest real post-compact reading for one physical worker leg."""
        if after_tokens is None:
            return False
        observed = observed_at.astimezone(UTC).isoformat()
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE context_deliveries SET"
                    " maintenance_completed_at=COALESCE(maintenance_completed_at, updated_at),"
                    " after_tokens=?, after_observed_at=?,"
                    " after_observation_version=?,"
                    " rearmed=CASE WHEN rearmed=1 OR ? < ? THEN 1 ELSE 0 END,"
                    " updated_at=? WHERE action_id=("
                    " SELECT action_id FROM context_deliveries"
                    " WHERE provider_id=? AND account_key=? AND worker_key=?"
                    " AND source_session_id=? AND executed_action='compact'"
                    " AND status='completed' AND maintenance_result='succeeded'"
                    " ORDER BY created_at DESC LIMIT 1)"
                    " AND COALESCE(maintenance_completed_at, updated_at) < ?"
                    " AND (after_observation_version IS NULL"
                    " OR after_observation_version < ?)",
                    (
                        after_tokens,
                        observed,
                        source_version,
                        after_tokens,
                        rearm_below_tokens,
                        now,
                        provider_id,
                        account_key,
                        worker_key,
                        source_session_id,
                        observed,
                        source_version,
                    ),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("context observation", exc)
            return False

    def latest_executed_context_cycle(
        self, provider_id: str, account_key: str, worker_key: str, source_session_id: str
    ) -> ContextDelivery | None:
        columns = ", ".join(self._CONTEXT_FIELDS)
        rows = self._query(
            f"SELECT {columns} FROM context_deliveries"
            " WHERE provider_id=? AND account_key=? AND worker_key=?"
            " AND source_session_id=? AND executed_action != 'none'"
            " AND status IN ('completed', 'clean_failed', 'uncertain')"
            " AND (maintenance_result IS NOT NULL OR successor_created=1"
            " OR provider_write_state!='not_started')"
            " ORDER BY created_at DESC LIMIT 1",
            (provider_id, account_key, worker_key, source_session_id),
        )
        return self._context_row(rows[0]) if rows else None

    def latest_context_delivery(
        self, provider_id: str, account_key: str, worker_key: str, source_session_id: str
    ) -> ContextDelivery | None:
        columns = ", ".join(self._CONTEXT_FIELDS)
        rows = self._query(
            f"SELECT {columns} FROM context_deliveries"
            " WHERE provider_id=? AND account_key=? AND worker_key=?"
            " AND source_session_id=?"
            " ORDER BY created_at DESC LIMIT 1",
            (provider_id, account_key, worker_key, source_session_id),
        )
        return self._context_row(rows[0]) if rows else None

    def pending_context_delivery(
        self, provider_id: str, account_key: str, worker_key: str
    ) -> ContextDelivery | None:
        """Return the unresolved context operation fencing this worker, if any."""
        columns = ", ".join(self._CONTEXT_FIELDS)
        rows = self._query(
            f"SELECT {columns} FROM context_deliveries"
            " WHERE provider_id=? AND account_key=? AND worker_key=?"
            " AND executed_action != 'none'"
            " AND (status IN ('context_pending', 'successor_created',"
            " 'message_pending', 'uncertain')"
            " OR (status='clean_failed' AND retryable_same_delivery=1))"
            " ORDER BY created_at DESC LIMIT 1",
            (provider_id, account_key, worker_key),
        )
        return self._context_row(rows[0]) if rows else None

    def list_chat_context_deliveries(self, chat_id: str) -> tuple[ContextDelivery, ...]:
        columns = ", ".join(self._CONTEXT_FIELDS)
        return tuple(
            self._context_row(row)
            for row in self._query(
                f"SELECT {columns} FROM context_deliveries WHERE chat_id=? ORDER BY created_at",
                (chat_id,),
            )
        )  # type: ignore[misc]

    def prune_context_deliveries(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "DELETE FROM context_deliveries WHERE updated_at < ?"
                    " AND (status IN ('completed', 'cancelled')"
                    " OR (status='clean_failed' AND retryable_same_delivery=0))",
                    (cutoff,),
                )
            return cursor.rowcount
        except sqlite3.Error as exc:
            self._log_failure("context delivery prune", exc)
            return 0

    def _backfill_chat_identities(self) -> None:
        """Give every session already known to this database one stable chat."""
        missing = self._conn.execute(
            "SELECT s.session_key FROM sessions_seen AS s"
            " LEFT JOIN chat_legs AS l ON l.session_key = s.session_key"
            " WHERE l.session_key IS NULL ORDER BY s.first_seen, s.session_key"
        ).fetchall()
        for (session_key,) in missing:
            self._ensure_session_chat_conn(str(session_key))

    def reserve_continued_chat_start(
        self, token: str, source_session_key: str
    ) -> str | None:
        """Reserve a callback, or return the state a prior attempt reached."""
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO continued_chat_starts"
                    " (token, source_session_key, state, reserved_at)"
                    " VALUES (?, ?, 'reserved', ?)",
                    (token, source_session_key, datetime.now(UTC).isoformat()),
                )
                if cursor.rowcount == 1:
                    return "new"
                row = self._conn.execute(
                    "SELECT state FROM continued_chat_starts WHERE token = ?", (token,)
                ).fetchone()
                return str(row[0]) if row else None
        except sqlite3.Error as exc:
            log.debug("continued-chat reservation failed: %s", exc)
            return None

    def mark_continued_chat_dispatched(self, token: str) -> bool:
        """Fence an attempt immediately before provider I/O becomes uncertain.

        True only when this call moved a ``reserved`` row to ``dispatched``, so a caller that
        must not start without the fence (the usage move, #3362) can fail closed.
        """
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE continued_chat_starts SET state = 'dispatched'"
                    " WHERE token = ? AND state = 'reserved'",
                    (token,),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            log.debug("continued-chat dispatch mark failed: %s", exc)
            return False

    def release_continued_chat_start(self, token: str) -> None:
        """Release only after start_new proves it wrote nothing."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM continued_chat_starts WHERE token = ?", (token,)
                )
        except sqlite3.Error as exc:
            log.debug("continued-chat reservation release failed: %s", exc)

    # --- durability seam ------------------------------------------------
    # Every persisting method funnels through these, so the lock, the transaction,
    # and the "a DB error is non-fatal — log at debug and degrade" policy live in
    # exactly one place instead of being re-typed per method.

    def _log_failure(self, what: str, exc: sqlite3.Error) -> None:
        """Degrade quietly on a transient failure, loudly on a broken schema.

        A locked or busy database is ordinary and self-healing. A statement naming a
        column or table this database does not have is a deploy-shaped bug that makes
        the feature above it a permanent no-op, and #579 showed that a debug line is
        not enough to notice one.
        """
        level = logging.ERROR if _is_schema_error(exc) else logging.DEBUG
        log.log(level, "db %s failed: %s", what, exc)

    def _write(self, sql: str, params: tuple = ()) -> bool:
        try:
            with self._lock, self._conn:
                self._conn.execute(sql, params)
            return True
        except sqlite3.Error as exc:
            self._log_failure("write", exc)
            return False

    def _write_many(self, sql: str, rows: Iterable[tuple]) -> None:
        try:
            with self._lock, self._conn:
                self._conn.executemany(sql, rows)
        except sqlite3.Error as exc:
            self._log_failure("write", exc)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            self._log_failure("query", exc)
            return []

    def _query_or_raise(self, sql: str, params: tuple = ()) -> list[tuple]:
        """``_query`` for a read whose failure must not read as "no rows" (#2862)."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def change_counter(self) -> tuple[int, int] | None:
        """A value that moves whenever anything committed to this database (#2865).

        ``PRAGMA data_version`` moves on another connection's commit — the runtime and
        the poller write this file too — and ``total_changes`` on this one's own writes,
        so a read keyed on both is redone only after a write, never on a clock. ``None``
        when the database cannot answer: a caller then reads afresh.
        """
        try:
            with self._lock:
                version = self._conn.execute("PRAGMA data_version").fetchone()[0]
                return (int(version), self._conn.total_changes)
        except sqlite3.Error as exc:
            self._log_failure("query", exc)
            return None

    def _prune_usage_history(self) -> None:
        """Drop usage_history rows older than the retention window, once at open.
        ``retention_days <= 0`` keeps everything — a valid "disable pruning"
        setting — so the append-only table can't grow unbounded by default."""
        if self.retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        self._write("DELETE FROM usage_history WHERE ts < ?", (cutoff,))

    def record_usage(self, snapshot: UsageSnapshot) -> None:
        self._write(
            "INSERT INTO usage_history"
            "(account_key, ts, five_hour_pct, seven_day_pct, month_pct)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.account_key,
                snapshot.fetched_at.isoformat(),
                snapshot.five_hour_pct,
                snapshot.seven_day_pct,
                snapshot.month_pct,
            ),
        )

    def latest_usage(self, account_key: str) -> UsageSnapshot | None:
        """Most recent persisted snapshot for warming state on startup. Reset
        times aren't stored in history, so they come back ``None`` — the live
        poller fills them on its first success."""
        rows = self._query(
            "SELECT ts, five_hour_pct, seven_day_pct, month_pct FROM usage_history"
            " WHERE account_key = ? ORDER BY ts DESC LIMIT 1",
            (account_key,),
        )
        if not rows:
            return None
        ts, five, seven, month = rows[0]
        try:
            fetched = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        return UsageSnapshot(
            account_key=account_key,
            five_hour_pct=five,
            five_hour_resets_at=None,
            seven_day_pct=seven,
            seven_day_resets_at=None,
            fetched_at=fetched,
            month_pct=month,
        )

    def recent_five_hour(self, account_key: str, limit: int = 24) -> list[float]:
        rows = self._query(
            "SELECT five_hour_pct FROM usage_history"
            " WHERE account_key = ? AND five_hour_pct IS NOT NULL"
            " ORDER BY ts DESC LIMIT ?",
            (account_key, limit),
        )
        return [float(r[0]) for r in reversed(rows)]

    def upsert_sessions_seen(self, sessions: list[Session]) -> None:
        now = datetime.now(UTC).isoformat()
        self._write_many(
            "INSERT INTO sessions_seen(session_key, account_key, title, cwd,"
            " first_seen, last_seen, kind) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " title=excluded.title, cwd=excluded.cwd, last_seen=excluded.last_seen,"
            " kind=excluded.kind",
            (
                (
                    s.key,
                    s.account_key,
                    s.title,
                    str(s.cwd) if s.cwd else None,
                    now,
                    now,
                    s.kind,
                )
                for s in sessions
            ),
        )

    def mark_session_kind(self, session_key: str, kind: str) -> None:
        """Record what one already-known session is, without touching the rest.

        The transcript indexer's way of saying "this row is a subagent's" about
        a chat the live scans stopped reporting long ago (#2055). Rows the scan
        never revisits would otherwise keep the NULL kind they were written with,
        and a search would still offer them. It updates only — a session with no
        ledger row has nothing to hide from a search.
        """
        self._write(
            "UPDATE sessions_seen SET kind = ? WHERE session_key = ?",
            (kind, session_key),
        )

    _SEEN_COLUMNS = (
        "session_key, account_key, title, cwd, first_seen, last_seen, kind"
    )

    def _seen_row(self, row: tuple) -> SeenSession:
        return SeenSession(
            session_key=str(row[0]),
            account_key=str(row[1]) if row[1] else None,
            title=str(row[2]) if row[2] else None,
            cwd=str(row[3]) if row[3] else None,
            first_seen=_parse_dt(row[4]),
            last_seen=_parse_dt(row[5]),
            kind=str(row[6]) if row[6] else None,
        )

    def search_sessions_seen(
        self, query: str, limit: int = 200
    ) -> tuple[SeenSession, ...]:
        """Chats whose title, directory or key contains every word of ``query``.

        This is the shallow half of deep search (#2034), and its reach is the
        point: ``sessions_seen`` holds every session the deck has ever observed,
        including the ones the 200-per-account scan cap and the paged list keep
        off the deck entirely. Words are ANDed so a second word narrows rather
        than widens, and each is matched with LIKE — the ledger is thousands of
        short rows, so a substring scan over it is a millisecond and needs no
        index of its own.

        Subagents are left out (#2055). They are a chat's internal legs, not
        chats a person went looking for, and they outnumber the answer badly: a
        Task-tool subagent carries its parent's directory and a title of its own,
        so a search for a project matched every leg it ever spawned. A row whose
        kind is NULL predates the column and is still shown — the live scan and
        the transcript indexer are what fill it in.
        """
        words = [word for word in query.split() if word]
        if not words:
            return ()
        clauses = []
        params: list[object] = []
        # Every word, not a prefix of them. Dropping the tail silently returned a
        # chat that matched only the words that survived, which reads as a match
        # for the whole phrase — a false positive the caller cannot detect. The
        # route already bounds the query length, so the clause count is bounded
        # with it.
        for word in words:
            clauses.append(
                "(COALESCE(title, '') LIKE ? ESCAPE '\\'"
                " OR COALESCE(cwd, '') LIKE ? ESCAPE '\\'"
                " OR session_key LIKE ? ESCAPE '\\')"
            )
            like = f"%{_escape_like(word)}%"
            params.extend([like, like, like])
        params.append(limit)
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._SEEN_COLUMNS} FROM sessions_seen"
                    f" WHERE {' AND '.join(clauses)}"
                    " AND COALESCE(kind, '') <> 'subagent'"
                    " ORDER BY last_seen DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("sessions_seen search failed: %s", exc)
            return ()
        return tuple(self._seen_row(row) for row in rows)

    def seen_session(self, session_key: str) -> SeenSession | None:
        """The ledger row for one session, for a hit found by another index."""
        try:
            with self._lock:
                row = self._conn.execute(
                    f"SELECT {self._SEEN_COLUMNS} FROM sessions_seen"
                    " WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return self._seen_row(row) if row is not None else None

    # --- durable chat identity ---------------------------------------

    # Chat identity is read once per session per pass, by three separate hot
    # callers — the spend projection's budget key, the visible-session
    # projection, and session admission — over every session the deck holds. On
    # a deck with a few thousand sessions that was two indexed SELECTs several
    # thousand times per liveness sweep, per full scan, and per SSE tick, which
    # measured as ~48% of the web worker's event-loop thread (#1291). The two
    # tables are small and only this deck writes them, so the whole mapping is
    # held in memory and the lookups become dictionary reads.
    #
    # Staleness is the real risk, not size: the runtime is a *separate process*
    # on the same file (``open_db``) and links chat legs of its own, so
    # an in-process cache that only its own writers invalidate would serve a
    # continued chat's old identity until restart. ``PRAGMA data_version``
    # changes exactly when some other connection commits and never for this
    # one's own writes, so it is the cheap, correct freshness check: one pragma
    # per lookup instead of two table reads, and a reload only after a foreign
    # commit actually lands.

    def _load_chat_index_conn(self) -> ChatSnapshot:
        legs: dict[str, list[str]] = {}
        chat_by_session: dict[str, str] = {}
        for chat_id, session_key in self._conn.execute(
            "SELECT chat_id, session_key FROM chat_legs ORDER BY chat_id, position"
        ):
            legs.setdefault(str(chat_id), []).append(str(session_key))
            chat_by_session[str(session_key)] = str(chat_id)
        aliases = {
            str(alias_chat_id): str(chat_id)
            for alias_chat_id, chat_id in self._conn.execute(
                "SELECT alias_chat_id, chat_id FROM chat_aliases"
            )
        }
        return ChatSnapshot(
            legs_by_chat={chat_id: tuple(keys) for chat_id, keys in legs.items()},
            chat_by_session=chat_by_session,
            aliases=aliases,
        )

    # ``data_version`` moves on a foreign commit to *any* table, though, and the
    # runtime and the deck's other connections commit about once a second. Reloading
    # some 50,000 legs (~200 ms) on each one, and with it dropping every view keyed on
    # the snapshot's identity, kept the web worker busy on an idle deck (#2665). So a
    # moved ``data_version`` only prompts a read of ``chat_index_stamp``, which triggers
    # on the two chat tables bump, and the mirror reloads only when that moved.

    def _chat_index_conn(self) -> ChatSnapshot:
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if self._chat_index is not None and self._chat_index_version == version:
            return self._chat_index
        stamp = self._conn.execute(
            "SELECT stamp FROM chat_index_stamp WHERE id = 0"
        ).fetchone()
        stamp = stamp[0] if stamp is not None else None
        if self._chat_index is None or stamp is None or self._chat_index_stamp != stamp:
            self._chat_index = self._load_chat_index_conn()
            self._chat_index_stamp = stamp
        self._chat_index_version = version
        return self._chat_index

    def _invalidate_chat_index(self) -> None:
        self._chat_index = None
        self._chat_index_version = None
        self._chat_index_stamp = None

    @contextmanager
    def _chat_index_writes(self) -> Iterator[None]:
        """Drop the in-memory index if the transaction that updated it does not commit.

        The two conn-level writers keep the index in step with their own inserts
        so a scan that creates many chats stays linear. A rollback would leave
        those updates describing rows that no longer exist, so anything that
        escapes — a ``sqlite3.Error`` or a ``ChatLinkConflict`` raised after an
        ``_ensure_session_chat_conn`` already ran — throws the index away and
        the next read rebuilds it from disk.
        """
        try:
            yield
        except BaseException:
            self._invalidate_chat_index()
            raise

    def _chat_by_id_conn(self, chat_id: str) -> ChatIdentity | None:
        return self._chat_index_conn().chat_by_id(chat_id)

    def _chat_for_session_conn(self, session_key: str) -> ChatIdentity | None:
        return self._chat_index_conn().chat_for_session(session_key)

    def _ensure_session_chat_conn(self, session_key: str) -> ChatIdentity:
        existing = self._chat_for_session_conn(session_key)
        if existing is not None:
            return existing
        chat_id = uuid.uuid4().hex
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO chats(chat_id, created_at) VALUES (?, ?)", (chat_id, now)
        )
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO chat_legs(chat_id, position, session_key, added_at)"
            " VALUES (?, 0, ?, ?)",
            (chat_id, session_key, now),
        )
        if cursor.rowcount == 0:
            # Another connection won the race while this transaction waited.
            self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
            self._invalidate_chat_index()
            existing = self._chat_for_session_conn(session_key)
            if existing is None:
                raise sqlite3.IntegrityError("session chat creation lost without a winner")
            return existing
        index = self._chat_index_conn()
        index.legs_by_chat[chat_id] = (session_key,)
        index.chat_by_session[session_key] = chat_id
        index.revision += 1
        return ChatIdentity(chat_id, (session_key,))

    def ensure_session_chats(
        self, session_keys: Iterable[str]
    ) -> tuple[ChatIdentity, ...]:
        try:
            with self._lock, self._conn, self._chat_index_writes():
                return tuple(
                    self._ensure_session_chat_conn(str(session_key))
                    for session_key in session_keys
                )
        except sqlite3.Error as exc:
            self._log_failure("chat identity ensure", exc)
            return ()

    def chat_for_session(self, session_key: str) -> ChatIdentity | None:
        try:
            with self._lock:
                return self._chat_for_session_conn(session_key)
        except sqlite3.Error as exc:
            self._log_failure("chat identity read", exc)
            return None

    def chat_by_id(self, chat_id: str) -> ChatIdentity | None:
        try:
            with self._lock:
                return self._chat_by_id_conn(chat_id)
        except sqlite3.Error as exc:
            self._log_failure("chat identity read", exc)
            return None

    def chat_snapshot(self) -> ChatSnapshot:
        """The whole chat mapping, for a caller about to walk every session.

        One freshness check for the pass instead of one per lookup. The check is
        a ``PRAGMA`` rather than a table read, but at a few thousand sessions
        several times a second even that was the dominant remaining cost (#1291).
        """
        try:
            with self._lock:
                return self._chat_index_conn()
        except sqlite3.Error as exc:
            self._log_failure("chat identity read", exc)
            return ChatSnapshot()

    def _append_chat_leg_conn(
        self, source_session_key: str, target_session_key: str
    ) -> ChatIdentity:
        if source_session_key == target_session_key:
            raise ChatLinkConflict("a session cannot succeed itself")
        source = self._ensure_session_chat_conn(source_session_key)
        target = self._chat_for_session_conn(target_session_key)
        if target is not None and target.chat_id == source.chat_id:
            source_position = source.legs.index(source_session_key)
            if (
                source_position + 1 < len(source.legs)
                and source.legs[source_position + 1] == target_session_key
            ):
                return source
            raise ChatLinkConflict("the target is already another leg of this chat")
        if source.newest_session_key != source_session_key:
            raise ChatLinkConflict("the source already has a successor")
        if target is not None:
            if len(target.legs) != 1:
                raise ChatLinkConflict("the target already belongs to another chat")
            reserved = self._conn.execute(
                "SELECT 1 FROM chat_successors WHERE source_session_key = ?",
                (target_session_key,),
            ).fetchone()
            if reserved:
                raise ChatLinkConflict("the target has already reserved a successor")
            self._conn.execute(
                "DELETE FROM chat_legs WHERE session_key = ?", (target_session_key,)
            )
            self._conn.execute(
                "INSERT INTO chat_aliases(alias_chat_id, chat_id) VALUES (?, ?)",
                (target.chat_id, source.chat_id),
            )
            self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (target.chat_id,))
            index = self._chat_index_conn()
            index.legs_by_chat.pop(target.chat_id, None)
            index.aliases[target.chat_id] = source.chat_id
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            "INSERT INTO chat_legs(chat_id, position, session_key, added_at) VALUES (?, ?, ?, ?)",
            (source.chat_id, len(source.legs), target_session_key, now),
        )
        legs = (*source.legs, target_session_key)
        index = self._chat_index_conn()
        index.legs_by_chat[source.chat_id] = legs
        index.chat_by_session[target_session_key] = source.chat_id
        index.revision += 1
        return ChatIdentity(source.chat_id, legs)

    def append_chat_leg(
        self, source_session_key: str, target_session_key: str
    ) -> ChatIdentity:
        with self._lock, self._conn, self._chat_index_writes():
            return self._append_chat_leg_conn(source_session_key, target_session_key)

    def reserve_chat_successor(
        self,
        token: str,
        source_session_key: str,
        *,
        newest_run: tuple[str, str] | None = None,
    ) -> str | None:
        """Fence one linear successor before provider I/O starts.

        ``newest_run`` — the (Work, Run) the continued chat served — answers ``superseded``
        when another Run of that Work has frozen a route since. The check and the insert
        are one transaction, and a Run's admission looks for reservations again after it
        freezes its route, so a Continue and a new Run cannot both pass (#2862).
        """
        try:
            with self._lock, self._conn, self._chat_index_writes():
                # A deferred transaction lets two connections both pass the
                # conflict reads before either writes. Serialize the decision
                # so the loser observes the winner and returns conflict.
                self._conn.execute("BEGIN IMMEDIATE")
                source = self._ensure_session_chat_conn(source_session_key)
                existing = self._conn.execute(
                    "SELECT token, target_session_key FROM chat_successors"
                    " WHERE source_session_key = ?",
                    (source_session_key,),
                ).fetchone()
                if existing:
                    if str(existing[0]) != token:
                        return "conflict"
                    return "linked" if existing[1] is not None else "reserved"
                if source.newest_session_key != source_session_key:
                    return "conflict"
                token_owner = self._conn.execute(
                    "SELECT source_session_key FROM chat_successors WHERE token = ?",
                    (token,),
                ).fetchone()
                if token_owner:
                    return "conflict"
                if newest_run is not None:
                    newest = self._conn.execute(
                        "SELECT run_id FROM delivery_routes WHERE work_id = ?"
                        " ORDER BY decided_at DESC, rowid DESC LIMIT 1",
                        (newest_run[0],),
                    ).fetchone()
                    if newest is not None and str(newest[0]) != newest_run[1]:
                        return "superseded"
                self._conn.execute(
                    "INSERT INTO chat_successors(source_session_key, token, reserved_at)"
                    " VALUES (?, ?, ?)",
                    (source_session_key, token, datetime.now(UTC).isoformat()),
                )
                return "new"
        except sqlite3.Error as exc:
            self._log_failure("chat successor reservation", exc)
            return None

    def pending_chat_successor(self, source_session_key: str) -> tuple[str, datetime] | None:
        """The reservation blocking this source, while no chat has claimed it yet.

        A reservation is deleted when its start is refused outright, and given a
        target once the new chat appears. Anything else — a start that failed
        later, or one whose in-memory record a web restart dropped — leaves a row
        that refuses every future continuation of that chat. The caller is the
        one that can tell a live start from a dead one, so hand it both the token
        it would have to ask about and when the row was written (#1620).
        """
        rows = self._query(
            "SELECT token, reserved_at FROM chat_successors"
            " WHERE source_session_key = ? AND target_session_key IS NULL",
            (source_session_key,),
        )
        if not rows:
            return None
        reserved_at = _parse_dt(rows[0][1]) or datetime.now(UTC)
        return str(rows[0][0]), reserved_at

    def chat_successor(
        self, source_session_key: str, *, excluding: str | None = None
    ) -> tuple[str | None, datetime] | None:
        """The chat this leg was continued into, or None when nobody continued it.

        The target is None while the new chat is still starting and has not named itself.
        ``excluding`` is the asking delivery's own token: the successor it reserved to carry
        the chat on is its own leg, not a Continue of the leg before (#2884). A store that
        cannot be read raises rather than answering "nobody" (#2862).
        """
        rows = self._query_or_raise(
            "SELECT target_session_key, reserved_at FROM chat_successors"
            " WHERE source_session_key = ? AND token IS NOT ?",
            (source_session_key, excluding),
        )
        if not rows:
            return None
        target = str(rows[0][0]) if rows[0][0] is not None else None
        return target, _parse_dt(rows[0][1]) or datetime.now(UTC)

    def chat_predecessor(self, target_session_key: str) -> str | None:
        """The leg this chat was continued from, or None when it was not a Continue.

        A store that cannot be read raises rather than answering "not a Continue".
        """
        rows = self._query_or_raise(
            "SELECT source_session_key FROM chat_successors WHERE target_session_key = ?",
            (target_session_key,),
        )
        return str(rows[0][0]) if rows else None

    def chat_run_binding(
        self, session_key: str, *, max_legs: int
    ) -> SessionRunBinding | None:
        """The Run this chat last served: the newest of its legs a delivery bound (#2862).

        A Continue copies its source's Run onto the new leg (#2881), but a leg linked
        before that has none, so the walk goes back along the chat's Continues. None when
        no leg was ever bound. Raises
        when the store cannot be read or the chain is longer than ``max_legs`` — neither
        proves the chat serves no Run.
        """
        with self._lock:
            return self._chat_run_binding_conn(session_key, max_legs=max_legs)

    def _chat_run_binding_conn(
        self, session_key: str, *, max_legs: int
    ) -> SessionRunBinding | None:
        leg: str | None = session_key
        for _ in range(max_legs):
            row = self._conn.execute(
                "SELECT session_key, work_id, run_id, first_delivery_id, bound_at"
                " FROM session_run_bindings WHERE session_key = ?"
                " ORDER BY bound_at DESC, rowid DESC LIMIT 1",
                (leg,),
            ).fetchone()
            if row:
                return SessionRunBinding(
                    str(row[0]), str(row[1]), str(row[2]), row[3], str(row[4])
                )
            predecessor = self._conn.execute(
                "SELECT source_session_key FROM chat_successors WHERE target_session_key = ?",
                (leg,),
            ).fetchone()
            if predecessor is None:
                return None
            leg = str(predecessor[0])
        raise LookupError(f"{session_key} has more than {max_legs} continued legs")

    def continuations_reserved_since(
        self, work_id: str, since: datetime, *, max_legs: int, excluding: str | None = None
    ) -> list[str]:
        """Chats of this Work a Continue reserved a successor for at or after ``since``.

        A Run's admission asks this after freezing its route: a Continue that reserved in
        between its first look and the freeze is one it never saw (#2862). ``excluding`` is
        the asking delivery's own token: a follow-up that continues the Run's chat reserves
        that chat's successor itself, and is not its own rival. Raises when it cannot tell.
        """
        rows = self._query_or_raise(
            "SELECT source_session_key FROM chat_successors"
            " WHERE reserved_at >= ? AND token IS NOT ?",
            (since.astimezone(UTC).isoformat(), excluding),
        )
        found = []
        for (source,) in rows:
            binding = self.chat_run_binding(str(source), max_legs=max_legs)
            if binding is not None and binding.work_id == work_id:
                found.append(str(source))
        return found

    def release_chat_successor(self, token: str) -> None:
        self._write(
            "DELETE FROM chat_successors WHERE token = ? AND target_session_key IS NULL",
            (token,),
        )

    def reserve_chat_fork(
        self, token: str, source_session_key: str, source_seq: int, action_id: str | None
    ) -> str | None:
        """Record a side chat's source before provider I/O; answer the token that owns it.

        A resend of the same press (same ``action_id``) gets the token its first
        attempt reserved, so the caller can send the browser to that start instead of
        starting a second side chat.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO chat_forks(token, source_session_key, source_seq,"
                    " action_id, reserved_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT DO NOTHING",
                    (
                        token,
                        source_session_key,
                        source_seq,
                        action_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                if action_id is None:
                    return token
                row = self._conn.execute(
                    "SELECT token FROM chat_forks WHERE action_id = ?", (action_id,)
                ).fetchone()
                return str(row[0]) if row else None
        except sqlite3.Error as exc:
            self._log_failure("chat fork reservation", exc)
            return None

    def claim_start_spare(
        self, action_id: str, token: str, worker_key: str
    ) -> tuple[str, str] | None:
        """Record that this press takes a spare; answer the claim that owns the press.

        A resend of the same press gets its first attempt's token and key back, never
        the spare offered now. ``None`` when the store failed: start cold instead.
        """
        now = datetime.now(UTC)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM start_spare_claims WHERE claimed_at < ?",
                    ((now - timedelta(days=1)).isoformat(),),
                )
                self._conn.execute(
                    "INSERT INTO start_spare_claims(action_id, token, worker_key, claimed_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (action_id, token, worker_key, now.isoformat()),
                )
                row = self._conn.execute(
                    "SELECT token, worker_key FROM start_spare_claims WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            self._log_failure("start spare claim", exc)
            return None
        return (str(row[0]), str(row[1])) if row else None

    def forget_start_spare_claim(self, action_id: str) -> None:
        """Drop a claim whose start never ran, so nothing reads its token as the chat's."""
        self._write("DELETE FROM start_spare_claims WHERE action_id = ?", (action_id,))

    def start_spare_claim(self, action_id: str) -> tuple[str, str] | None:
        """The token and worker key an earlier attempt of this press took, if any."""
        rows = self._query(
            "SELECT token, worker_key FROM start_spare_claims WHERE action_id = ?",
            (action_id,),
        )
        return (str(rows[0][0]), str(rows[0][1])) if rows else None

    def release_chat_fork(self, token: str) -> None:
        """Forget a side chat's reservation whose start never named a session.

        A start refused or failed before the provider answered leaves a row that
        would answer every resend of that press with a start that does not exist.
        A row whose token already names a session is the side chat's link, kept.
        """
        self._write(
            "DELETE FROM chat_forks WHERE token = ?"
            " AND token NOT IN (SELECT token FROM session_tokens)",
            (token,),
        )

    def record_transcript_rollback(self, session_key: str, first_seq: int, last_seq: int) -> bool:
        """Record lines ``first_seq..last_seq`` of a leg as rolled out of its chat (#3474)."""
        return self._write(
            "INSERT INTO transcript_rollbacks(session_key, first_seq, last_seq, rolled_back_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (session_key, first_seq, last_seq, datetime.now(UTC).isoformat()),
        )

    def load_transcript_rollbacks(self) -> list[tuple[str, int, int]]:
        return [
            (str(key), int(first), int(last))
            for key, first, last in self._query(
                "SELECT session_key, first_seq, last_seq FROM transcript_rollbacks"
            )
        ]

    def chat_fork_source(self, session_key: str) -> tuple[str, int] | None:
        """The session and line a side chat was cut from, or None for any other chat."""
        rows = self._query(
            "SELECT f.source_session_key, f.source_seq FROM chat_forks f"
            " JOIN session_tokens t ON t.token = f.token WHERE t.session_key = ?",
            (session_key,),
        )
        return (str(rows[0][0]), int(rows[0][1])) if rows else None

    def load_session_last_opened(self) -> dict[str, datetime]:
        return {
            str(session_key): opened_at
            for session_key, raw_opened_at in self._query(
                "SELECT session_key, opened_at FROM session_last_opened"
            )
            if (opened_at := _parse_dt(raw_opened_at)) is not None
        }

    def record_session_opened(
        self, session_key: str, opened_at: datetime | None = None
    ) -> datetime:
        opened_at = opened_at or datetime.now(UTC)
        self._write(
            "INSERT INTO session_last_opened(session_key, opened_at) VALUES (?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET opened_at=excluded.opened_at",
            (session_key, opened_at.isoformat()),
        )
        return opened_at

    def load_generated_titles(self) -> dict[str, GeneratedTitle]:
        rows = self._query(
            "SELECT session_key, title, evidence_signature, updated_at, source"
            " FROM generated_titles"
        )
        try:
            return {
                str(session_key): GeneratedTitle(
                    str(title),
                    str(signature),
                    datetime.fromisoformat(str(updated_at)),
                    str(source or "deckhand"),
                )
                for session_key, title, signature, updated_at, source in rows
            }
        except ValueError as exc:  # a stored timestamp that won't parse
            log.debug("load_generated_titles decode failed: %s", exc)
            return {}

    def record_generated_title(
        self, session_key: str, title: str, evidence_signature: str, source: str = "deckhand"
    ) -> GeneratedTitle:
        updated_at = datetime.now(UTC)
        self._write(
            "INSERT INTO generated_titles(session_key, title, evidence_signature,"
            " updated_at, source) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " title=excluded.title, evidence_signature=excluded.evidence_signature,"
            " updated_at=excluded.updated_at, source=excluded.source",
            (session_key, title, evidence_signature, updated_at.isoformat(), source),
        )
        return GeneratedTitle(title, evidence_signature, updated_at, source)

    def delete_generated_title(self, session_key: str) -> None:
        self._write("DELETE FROM generated_titles WHERE session_key = ?", (session_key,))

    # --- session identity tokens --------------------------------------
    # Minted before a session exists and bound to its key once the provider
    # returns one, so an agent can resolve which chat it is.

    def load_session_tokens(self) -> dict[str, str]:
        return {
            str(token): str(session_key)
            for token, session_key in self._query("SELECT token, session_key FROM session_tokens")
        }

    def record_session_token(self, token: str, session_key: str) -> ChatIdentity | None:
        """Bind a provider leg and consume any armed Continue relation atomically."""
        try:
            with self._lock, self._conn, self._chat_index_writes():
                self._conn.execute(
                    "INSERT INTO session_tokens(token, session_key, created_at)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(token) DO UPDATE SET session_key=excluded.session_key",
                    (token, session_key, datetime.now(UTC).isoformat()),
                )
                successor = self._conn.execute(
                    "SELECT source_session_key, target_session_key FROM chat_successors"
                    " WHERE token = ?",
                    (token,),
                ).fetchone()
                if successor is None:
                    self._ensure_session_chat_conn(session_key)
                    return None
                source_session_key = str(successor[0])
                existing_target = successor[1]
                if existing_target is not None and str(existing_target) != session_key:
                    raise ChatLinkConflict("a successor token resolved to two sessions")
                chat = self._append_chat_leg_conn(source_session_key, session_key)
                self._conn.execute(
                    "UPDATE chat_successors SET target_session_key = ?, linked_at = ?"
                    " WHERE token = ? AND target_session_key IS NULL",
                    (session_key, datetime.now(UTC).isoformat(), token),
                )
                # A Continue carries the chat on under the Run it served, so the new leg
                # joins that Run too: every reader of a leg's Run, ``--verification``
                # included, then answers the same for it (#2881). The source may be a leg
                # linked before that, with no Run of its own, so walk the chat back; a
                # chain past the cap copies nothing, as a Run it cannot name is not proof.
                from .run_execution import MAX_CONTINUATIONS

                try:
                    run = self._chat_run_binding_conn(
                        source_session_key, max_legs=MAX_CONTINUATIONS
                    )
                except LookupError:
                    log.warning("not copying a Run onto %s: chain too long", session_key)
                    run = None
                if run is not None:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO session_run_bindings"
                        "(session_key, work_id, run_id, first_delivery_id, bound_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            session_key,
                            run.work_id,
                            run.run_id,
                            run.first_delivery_id,
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                return chat
        except ChatLinkConflict:
            raise
        except sqlite3.Error as exc:
            self._log_failure("session token/chat link", exc)
            return None

    def session_key_for_token(self, token: str) -> str | None:
        rows = self._query("SELECT session_key FROM session_tokens WHERE token = ?", (token,))
        return str(rows[0][0]) if rows else None

    def session_key_has_token(self, session_key: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM session_tokens WHERE session_key = ? LIMIT 1", (session_key,)
        )
        return bool(rows)

    def runtime_relation_stamp(self) -> tuple[int, ...] | None:
        """A cheap stamp of the session-token and session-issue rows, for the web's follower.

        The runtime writes both rows itself when the Poller delivers straight to it (#2144).
        The web keeps them in memory, so it compares this stamp and reloads only after it moves.
        The runtime only ever adds rows, and an added row always changes a count.
        """
        rows = self._query(
            "SELECT (SELECT count(*) FROM session_tokens),"
            " (SELECT count(*) FROM session_issues),"
            " (SELECT coalesce(max(rowid), 0) FROM session_issues)"
        )
        return tuple(int(value) for value in rows[0]) if rows else None

    # --- per-session permission prompts -------------------------------

    def permission_prompts_allowed(self, session_key: str) -> bool:
        return bool(
            self._query(
                "SELECT 1 FROM session_permission_prompts WHERE session_key = ?",
                (session_key,),
            )
        )

    def permission_prompts_previous_policy(
        self, session_key: str
    ) -> str | dict[str, object] | None:
        rows = self._query(
            "SELECT previous_policy_json FROM session_permission_prompts WHERE session_key = ?",
            (session_key,),
        )
        if not rows:
            return None
        policy = json.loads(str(rows[0][0]))
        return policy if isinstance(policy, (str, dict)) else None

    def set_permission_prompts(
        self,
        session_key: str,
        enabled: bool,
        previous_policy: str | dict[str, object] | None = None,
    ) -> bool:
        if enabled:
            if previous_policy is None:
                raise ValueError("previous approval policy is required when enabling prompts")
            return self._write(
                "INSERT INTO session_permission_prompts"
                "(session_key, previous_policy_json, enabled_at) VALUES (?, ?, ?)"
                " ON CONFLICT(session_key) DO NOTHING",
                (
                    session_key,
                    json.dumps(previous_policy, separators=(",", ":"), sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return self._write(
            "DELETE FROM session_permission_prompts WHERE session_key = ?",
            (session_key,),
        )

    # --- session pull requests ----------------------------------------
    # Registered by the session's own agent through the local API; the only
    # source of a chat's PRs.

    def load_session_pull_requests(self) -> dict[str, tuple[PullRequestRef, ...]]:
        rows = self._query(
            "SELECT session_key, repository, number, work_id FROM session_pull_requests"
            " ORDER BY session_key, added_at, rowid"
        )
        registered: dict[str, tuple[PullRequestRef, ...]] = {}
        for session_key, repository, number, work_id in rows:
            try:
                ref = PullRequestRef(str(repository), int(number), str(work_id or ""))
            except (TypeError, ValueError):
                log.debug("skipping malformed session pull request for %r", session_key)
                continue
            registered[str(session_key)] = registered.get(str(session_key), ()) + (ref,)
        return registered

    def record_session_pull_request(self, session_key: str, ref: PullRequestRef) -> None:
        self._write(
            "INSERT INTO session_pull_requests"
            "(session_key, repository, number, added_at, work_id) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key, repository, number) DO NOTHING",
            (
                session_key,
                ref.repository,
                ref.number,
                datetime.now(UTC).isoformat(),
                ref.work_id,
            ),
        )

    def delete_session_pull_request(self, session_key: str, ref: PullRequestRef) -> None:
        self._write(
            "DELETE FROM session_pull_requests"
            " WHERE session_key = ? AND repository = ? AND number = ?",
            (session_key, ref.repository, ref.number),
        )

    def delete_session_pull_requests(self, session_key: str) -> None:
        self._write("DELETE FROM session_pull_requests WHERE session_key = ?", (session_key,))

    # --- session issues -----------------------------------------------
    # Explicit issue relationships survive provider scans and web restarts,
    # just like the PRs a session declares as its own work.

    def load_session_issues(self) -> dict[str, tuple[IssueRef, ...]]:
        rows = self._query(
            "SELECT session_key, repository, number FROM session_issues"
            " ORDER BY session_key, added_at, rowid"
        )
        registered: dict[str, tuple[IssueRef, ...]] = {}
        for session_key, repository, number in rows:
            try:
                ref = IssueRef(str(repository), int(number))
            except (TypeError, ValueError):
                log.debug("skipping malformed session issue for %r", session_key)
                continue
            registered[str(session_key)] = registered.get(str(session_key), ()) + (ref,)
        return registered

    def record_session_issue(self, session_key: str, ref: IssueRef) -> None:
        self._write(
            "INSERT INTO session_issues(session_key, repository, number, added_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_key, repository, number) DO NOTHING",
            (session_key, ref.repository, ref.number, datetime.now(UTC).isoformat()),
        )

    def delete_session_issue(self, session_key: str, ref: IssueRef) -> None:
        self._write(
            "DELETE FROM session_issues WHERE session_key = ? AND repository = ? AND number = ?",
            (session_key, ref.repository, ref.number),
        )

    def delete_session_issues(self, session_key: str) -> None:
        self._write("DELETE FROM session_issues WHERE session_key = ?", (session_key,))

    # --- work inventory (#2140) ---------------------------------------
    # One row per Work, keyed by its source-derived work_id. Inserts are
    # idempotent, so a restart or a repeated sighting never mints a second row
    # and the first created_at stands.

    def record_works(self, refs: Iterable[IssueRef]) -> None:
        now = datetime.now(UTC).isoformat()
        works = {WorkRef.from_issue(ref) for ref in refs}
        with self._lock:
            fresh = [work for work in works if work.work_id not in self._known_work_ids]
        if not fresh:
            return
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO works(work_id, source_kind, source_key, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    [(w.work_id, w.source_kind, w.source_key, now) for w in sorted(fresh)],
                )
                self._known_work_ids.update(work.work_id for work in fresh)
        except sqlite3.Error as exc:
            self._log_failure("write", exc)

    def load_works(self) -> dict[str, tuple[str, str, str]]:
        """``work_id`` → (source_kind, source_key, created_at)."""
        rows = self._query("SELECT work_id, source_kind, source_key, created_at FROM works")
        return {str(row[0]): (str(row[1]), str(row[2]), str(row[3])) for row in rows}

    def count_works(self) -> int:
        """How many Works the inventory holds, without reading them (#2201)."""
        rows = self._query("SELECT COUNT(*) FROM works")
        return int(rows[0][0]) if rows else 0

    def known_work_ids(self, work_ids: Iterable[str]) -> set[str]:
        """Which of ``work_ids`` the inventory holds: one lookup per id, never the table."""
        wanted = list(dict.fromkeys(work_ids))
        found: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            rows = self._query(
                f"SELECT work_id FROM works WHERE work_id IN ({marks})", tuple(chunk)
            )
            found.update(str(row[0]) for row in rows)
        return found

    # --- session run bindings (#2142) ---------------------------------
    # Participation, not ownership: idempotent per (session, Work, Run), and the
    # first binding's delivery and time stand.

    def bind_session_run(
        self, session_key: str, work_id: str, run_id: str, *, delivery_id: str | None = None
    ) -> None:
        if not (session_key and work_id and run_id):
            return
        self._write(
            "INSERT OR IGNORE INTO session_run_bindings"
            "(session_key, work_id, run_id, first_delivery_id, bound_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_key, work_id, run_id, delivery_id, datetime.now(UTC).isoformat()),
        )

    def session_run_bindings(
        self,
        *,
        session_key: str | None = None,
        work_id: str | None = None,
        run_id: str | None = None,
    ) -> list[SessionRunBinding]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("session_key", session_key),
            ("work_id", work_id),
            ("run_id", run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(
            "SELECT session_key, work_id, run_id, first_delivery_id, bound_at"
            f" FROM session_run_bindings{where} ORDER BY bound_at, session_key",
            tuple(params),
        )
        return [
            SessionRunBinding(str(r[0]), str(r[1]), str(r[2]), r[3], str(r[4])) for r in rows
        ]

    def session_run_work_ids(self) -> list[tuple[str, str]]:
        """Every (session_key, work_id) pair a Run binding names, once each (#2207)."""
        rows = self._query(
            "SELECT DISTINCT session_key, work_id FROM session_run_bindings"
            " ORDER BY session_key, work_id"
        )
        return [(str(r[0]), str(r[1])) for r in rows]

    # --- delegated Work bindings (#2207) --------------------------------
    # A delegated child's Work, saved once when it is created (or by the one-time
    # backfill) so the projection never walks the parent chain. ``ambiguous`` rows
    # name candidates and join nothing. The first outcome per (session, Work) stands.

    def bind_session_work(
        self, session_key: str, work_id: str, evidence: str, parent_chat_id: str = ""
    ) -> None:
        # Only a ``none`` outcome names no Work.
        if not (session_key and evidence and (work_id or evidence == "none")):
            return
        self._write(
            "INSERT OR IGNORE INTO session_work_bindings"
            "(session_key, work_id, evidence, parent_chat_id, bound_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_key, work_id, evidence, parent_chat_id, datetime.now(UTC).isoformat()),
        )

    def publish_work_backfill(
        self, name: str, summary: str, rows: Iterable[tuple[str, str, str, str]]
    ) -> bool:
        """Save a backfill's rows and its marker in one transaction; False if it failed.

        The marker is what stops the backfill from ever running again, so it may only
        exist when every row it stands for is durable too (#2207).
        """
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO session_work_bindings"
                    "(session_key, work_id, evidence, parent_chat_id, bound_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (key, work_id, evidence, parent, now)
                        for key, work_id, evidence, parent in rows
                        if key and evidence and (work_id or evidence == "none")
                    ],
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO work_backfills(name, ran_at, summary) VALUES (?, ?, ?)",
                    (name, now, summary),
                )
            return True
        except sqlite3.Error as exc:
            self._log_failure("write", exc)
            return False

    def session_work_bindings(self) -> list[SessionWorkBinding]:
        rows = self._query(
            "SELECT session_key, work_id, evidence, parent_chat_id, bound_at"
            " FROM session_work_bindings ORDER BY session_key, work_id"
        )
        return [SessionWorkBinding(*(str(value) for value in r)) for r in rows]

    def work_backfill(self, name: str) -> str | None:
        """The summary a named one-time backfill recorded, or None if it never ran."""
        rows = self._query("SELECT summary FROM work_backfills WHERE name = ?", (name,))
        return str(rows[0][0]) if rows else None

    def keeps_work_backfills(self) -> bool:
        """This store keeps the delegated-work cutover and backfill markers (#2207)."""
        return True

    def record_work_backfill(self, name: str, summary: str) -> bool:
        return self._write(
            "INSERT OR IGNORE INTO work_backfills(name, ran_at, summary) VALUES (?, ?, ?)",
            (name, datetime.now(UTC).isoformat(), summary),
        )

    # --- delivery routes (#2146) ---------------------------------------
    # The scheduler's frozen target per delivery. Unlike the stores above, a read that fails
    # raises rather than answering empty: "no route" would let a retry pick another target.

    _ROUTE_COLUMNS = (
        "delivery_id, request_fingerprint, work_id, run_id, provider, account,"
        " worker_key, decided_at, refused_at_delivery"
    )

    def _route_rows(self, sql: str, params: tuple) -> list[DeliveryRoute]:
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            self._log_failure("query", exc)
            raise RoutingStoreError(str(exc)) from exc
        return [DeliveryRoute(*(str(value) for value in row)) for row in rows]

    def delivery_route(self, delivery_id: str) -> DeliveryRoute | None:
        rows = self._route_rows(
            f"SELECT {self._ROUTE_COLUMNS} FROM delivery_routes WHERE delivery_id = ?",
            (delivery_id,),
        )
        return rows[0] if rows else None

    def latest_delivery_route(
        self, work_id: str, *, run_id: str | None = None
    ) -> DeliveryRoute | None:
        """The newest route decided for this Work (or this Run of it)."""
        where, params = "work_id = ?", (work_id,)
        if run_id is not None:
            where, params = "work_id = ? AND run_id = ?", (work_id, run_id)
        rows = self._route_rows(
            f"SELECT {self._ROUTE_COLUMNS} FROM delivery_routes WHERE {where}"
            " ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            params,
        )
        return rows[0] if rows else None

    def latest_delivery_routes_by_run(self, work_id: str) -> list[DeliveryRoute]:
        """The newest route of every Run of this Work, newest Run first (#2159)."""
        rows = self._route_rows(
            f"SELECT {self._ROUTE_COLUMNS} FROM delivery_routes WHERE work_id = ?"
            " ORDER BY decided_at DESC, rowid DESC",
            (work_id,),
        )
        newest: dict[str, DeliveryRoute] = {}
        for route in rows:
            newest.setdefault(route.run_id, route)
        return list(newest.values())

    def freeze_delivery_route(self, route: DeliveryRoute) -> bool:
        """Record the target before the selected host is called. False: nothing may be sent."""
        return self._write(
            f"INSERT INTO delivery_routes({self._ROUTE_COLUMNS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(route),
        )

    def release_delivery_route(self, delivery_id: str) -> bool:
        return self._write("DELETE FROM delivery_routes WHERE delivery_id = ?", (delivery_id,))

    def latest_session_run(self, session_ref: str) -> SessionRunBinding | None:
        """The Run this session most recently joined, by full key or raw provider id.

        A delegating parent names itself either way (``delegated_sessions`` stores it
        as given), so a raw id matches the leg whose key ends in it.
        """
        ref = (session_ref or "").strip()
        if not ref:
            return None
        rows = self._query(
            "SELECT session_key, work_id, run_id, first_delivery_id, bound_at"
            " FROM session_run_bindings"
            " WHERE session_key = ? OR session_key LIKE ? ESCAPE '\\'"
            " ORDER BY bound_at DESC, rowid DESC LIMIT 1",
            (ref, f"%:{_escape_like(ref)}"),
        )
        if not rows:
            return None
        r = rows[0]
        return SessionRunBinding(str(r[0]), str(r[1]), str(r[2]), r[3], str(r[4]))

    # --- session attention reports -----------------------------------

    def load_session_attention_reports(self) -> dict[str, AttentionReport]:
        reports: dict[str, AttentionReport] = {}
        for session_key, state, updated_at, detail in self._query(
            "SELECT session_key, state, updated_at, detail FROM session_attention_reports"
        ):
            if state not in {"waiting", "finished"}:
                log.debug("skipping invalid attention state for %r: %r", session_key, state)
                continue
            try:
                reports[str(session_key)] = AttentionReport(
                    str(state),
                    datetime.fromisoformat(str(updated_at)),
                    str(detail or ""),
                )
            except ValueError:
                log.debug("skipping invalid attention timestamp for %r", session_key)
        return reports

    def record_session_attention_report(
        self, session_key: str, state: str, detail: str = ""
    ) -> AttentionReport:
        report = AttentionReport(state, datetime.now(UTC), detail)
        self._write(
            "INSERT INTO session_attention_reports(session_key, state, updated_at, detail)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET"
            " state=excluded.state, updated_at=excluded.updated_at, detail=excluded.detail",
            (session_key, state, report.updated_at.isoformat(), detail),
        )
        return report

    def delete_session_attention_report(self, session_key: str) -> None:
        self._write("DELETE FROM session_attention_reports WHERE session_key = ?", (session_key,))

    # --- deck-derived open questions ---------------------------------

    def load_open_questions(self) -> dict[str, OpenQuestion]:
        questions: dict[str, OpenQuestion] = {}
        rows = self._query(
            "SELECT session_key, question, asked_at, source, asked_seq,"
            " observed_seq, answered_seq, observed_session_key, answered_session_key"
            " FROM session_open_questions"
        )
        for (
            key,
            text,
            asked_at,
            source,
            asked_seq,
            observed_seq,
            answered_seq,
            observed_session_key,
            answered_session_key,
        ) in rows:
            try:
                questions[str(key)] = OpenQuestion(
                    session_key=str(key),
                    question=str(text),
                    asked_at=datetime.fromisoformat(str(asked_at)),
                    source=str(source),
                    asked_seq=int(asked_seq),
                    observed_seq=int(observed_seq),
                    answered_seq=int(answered_seq) if answered_seq is not None else None,
                    observed_session_key=(
                        str(observed_session_key)
                        if observed_session_key is not None
                        else None
                    ),
                    answered_session_key=(
                        str(answered_session_key)
                        if answered_session_key is not None
                        else None
                    ),
                )
            except (TypeError, ValueError):
                log.debug("skipping invalid open question for %r", key)
        return questions

    def record_open_question(self, question: OpenQuestion) -> None:
        self._write(
            "INSERT INTO session_open_questions"
            " (session_key, question, asked_at, source, asked_seq, observed_seq,"
            " answered_seq, observed_session_key, answered_session_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " question=excluded.question, asked_at=excluded.asked_at, source=excluded.source,"
            " asked_seq=excluded.asked_seq, observed_seq=excluded.observed_seq,"
            " answered_seq=excluded.answered_seq,"
            " observed_session_key=excluded.observed_session_key,"
            " answered_session_key=excluded.answered_session_key",
            (
                question.session_key,
                question.question,
                question.asked_at.isoformat(),
                question.source,
                question.asked_seq,
                question.observed_seq,
                question.answered_seq,
                question.observed_session_key,
                question.answered_session_key,
            ),
        )

    def delete_open_question(self, session_key: str) -> None:
        self._write("DELETE FROM session_open_questions WHERE session_key = ?", (session_key,))

    def load_manual_new_chat_cwd(self) -> str | None:
        rows = self._query("SELECT cwd FROM manual_new_chat_state WHERE singleton = 1")
        return str(rows[0][0]) if rows else None

    def load_account_new_chat_defaults(self, account_key: str) -> dict[str, str | None]:
        rows = self._query(
            "SELECT model, effort FROM account_new_chat_defaults WHERE account_key = ?",
            (account_key,),
        )
        if not rows:
            return {}
        model, effort = rows[0]
        return {
            "model": str(model) if model is not None else None,
            "effort": str(effort) if effort is not None else None,
        }

    def load_composer_phrases(self) -> tuple[ComposerPhrase, ...]:
        """This deck's edited quick phrases in display order; empty means built-ins."""
        return tuple(
            ComposerPhrase(str(label), str(message))
            for label, message in self._query(
                "SELECT label, message FROM composer_phrases ORDER BY position"
            )
        )

    def record_composer_phrases(self, phrases: Iterable[ComposerPhrase]) -> None:
        """Replace the whole list in one transaction — order is part of the value,
        so a partially rewritten table would render a list nobody saved."""
        now = datetime.now(UTC).isoformat()
        rows = [(index, phrase.label, phrase.text, now) for index, phrase in enumerate(phrases)]
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM composer_phrases")
                self._conn.executemany(
                    "INSERT INTO composer_phrases(position, label, message, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            self._log_failure("write", exc)

    def clear_composer_phrases(self) -> None:
        """Forget the customization, which is what restores the built-in list."""
        self._write("DELETE FROM composer_phrases")

    def load_guessing_enabled(self) -> bool:
        """Whether the deck guesses for the person (#3367); on until switched off."""
        rows = self._query("SELECT enabled FROM guess_preferences WHERE singleton = 1")
        return not rows or bool(rows[0][0])

    def record_guessing_enabled(self, enabled: bool) -> None:
        self._write(
            "INSERT INTO guess_preferences(singleton, enabled, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET enabled = excluded.enabled,"
            " updated_at = excluded.updated_at",
            (int(enabled), datetime.now(UTC).isoformat()),
        )

    @staticmethod
    def _scheduled_message_from_row(row: tuple) -> ScheduledMessage | None:
        due_at, created_at = _parse_dt(row[3]), _parse_dt(row[4])
        if due_at is None or created_at is None:
            return None
        return ScheduledMessage(
            id=str(row[0]),
            chat_id=str(row[1]),
            text=str(row[2]),
            due_at=due_at,
            created_at=created_at,
            effort=str(row[5]) if row[5] is not None else None,
            state=str(row[6]),
            reason=str(row[7]) if row[7] is not None else None,
            hold_reason=str(row[8]) if row[8] is not None else None,
            attachments=_held_attachments(row[9]),
        )

    def record_scheduled_message(
        self, message: ScheduledMessage
    ) -> ScheduledMessage | None:
        """Reserve one deferred turn, replaying an identical client retry."""
        now = datetime.now(UTC).isoformat()
        attachments = (
            json.dumps(
                [{"path": str(item.path), "kind": item.kind} for item in message.attachments]
            )
            if message.attachments
            else None
        )
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO scheduled_messages"
                    " (id, chat_id, message, effort, due_at, created_at, state,"
                    " reason, updated_at, hold_reason, attachments)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message.id,
                        message.chat_id,
                        message.text,
                        message.effort,
                        message.due_at.isoformat(),
                        message.created_at.isoformat(),
                        message.state,
                        message.reason,
                        now,
                        message.hold_reason,
                        attachments,
                    ),
                )
                row = self._conn.execute(
                    f"SELECT {_SCHEDULED_COLUMNS} FROM scheduled_messages WHERE id = ?",
                    (message.id,),
                ).fetchone()
        except sqlite3.Error as exc:
            self._log_failure("scheduled message write", exc)
            return None
        existing = self._scheduled_message_from_row(row) if row is not None else None
        if existing is None or (
            existing.chat_id,
            existing.text,
            existing.effort,
        ) != (message.chat_id, message.text, message.effort):
            return None
        return existing

    def load_scheduled_message(self, message_id: str) -> ScheduledMessage | None:
        rows = self._query(
            f"SELECT {_SCHEDULED_COLUMNS} FROM scheduled_messages WHERE id = ?",
            (message_id,),
        )
        return self._scheduled_message_from_row(rows[0]) if rows else None

    def has_scheduled_message(self, message_id: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM scheduled_messages WHERE id = ?", (message_id,)
        )
        return bool(rows)

    def deck_message_ids(
        self, chat_id: str, texts
    ) -> dict[str, list[tuple[str, datetime | None]]]:
        """Each text the deck itself sent into this chat, with every record of sending it:
        its ID and when it was handed over (#3200).

        A person's held send carries a ``hold_reason``; the deck's own messages never do,
        and their ID prefix names the sender (``deck_notices.DECK_KINDS``).
        """
        wanted = sorted({text for text in texts if text})
        found: dict[str, list[tuple[str, datetime | None]]] = {}
        for start in range(0, len(wanted), 200):
            chunk = wanted[start : start + 200]
            rows = self._query(
                "SELECT message, id, updated_at FROM scheduled_messages WHERE chat_id = ?"
                " AND hold_reason IS NULL AND state IN ('dispatching', 'delivered')"
                f" AND message IN ({', '.join('?' for _ in chunk)})",
                (chat_id, *chunk),
            )
            for message, message_id, updated_at in rows:
                try:
                    at = datetime.fromisoformat(str(updated_at))
                except ValueError:
                    at = None
                found.setdefault(message, []).append((message_id, at))
        return found

    def load_visible_scheduled_messages(
        self, chat_id: str
    ) -> tuple[ScheduledMessage, ...]:
        rows = self._query(
            f"SELECT {_SCHEDULED_COLUMNS} FROM scheduled_messages WHERE chat_id = ?"
            " AND hold_reason IS NOT NULL"
            " AND state IN ('scheduled', 'dispatching', 'failed')"
            " ORDER BY due_at, created_at, id",
            (chat_id,),
        )
        return tuple(
            message
            for row in rows
            if (message := self._scheduled_message_from_row(row)) is not None
        )

    def load_due_scheduled_messages(
        self, now: datetime
    ) -> tuple[ScheduledMessage, ...]:
        rows = self._query(
            f"SELECT {_SCHEDULED_COLUMNS} FROM scheduled_messages"
            " WHERE state IN ('scheduled', 'dispatching') AND due_at <= ?"
            " ORDER BY due_at, created_at, id",
            (now.isoformat(),),
        )
        return tuple(
            message
            for row in rows
            if (message := self._scheduled_message_from_row(row)) is not None
        )

    def pending_scheduled_message_ids(self) -> frozenset[str]:
        """Ids that still own a future delivery, in one pass-wide read."""
        rows = self._query(
            "SELECT id FROM scheduled_messages WHERE state IN ('scheduled', 'dispatching')"
        )
        return frozenset(str(row[0]) for row in rows)

    def scheduled_message_handoffs(self, now: datetime, since: datetime) -> dict[str, datetime]:
        """Each chat a message was handed to at or after ``since``, in one pass-wide read (#3084).

        A due row still waiting or mid-send counts from its due time or its last move (its
        claim), a delivered one from its delivery; the newest per chat. A turn a message
        starts shows as running only seconds later, so its Work must not read as ended
        meanwhile, but a row that stays unsent past ``since`` counts no longer.
        """
        rows = self._query(
            "SELECT chat_id, due_at, updated_at FROM scheduled_messages"
            " WHERE (state IN ('scheduled', 'dispatching') AND due_at <= ?)"
            " OR (state = 'delivered' AND updated_at >= ?)",
            (now.isoformat(), since.isoformat()),
        )
        found: dict[str, datetime] = {}
        for chat_id, due_at, updated_at in rows:
            try:
                at = max(
                    datetime.fromisoformat(str(due_at)), datetime.fromisoformat(str(updated_at))
                )
            except ValueError:
                continue
            chat = str(chat_id)
            if at >= since and (chat not in found or at > found[chat]):
                found[chat] = at
        return found

    def next_scheduled_message_due(self, now: datetime) -> datetime | None:
        """The earliest ``due_at`` of a waiting row not due yet, so the scheduler can sleep
        exactly until it instead of looking every second (#2865)."""
        rows = self._query(
            "SELECT MIN(due_at) FROM scheduled_messages WHERE state = 'scheduled' AND due_at > ?",
            (now.isoformat(),),
        )
        return _parse_dt(rows[0][0]) if rows and rows[0][0] is not None else None

    def claim_scheduled_message(self, message_id: str) -> bool:
        """Fence a send before I/O; an old dispatching row is a safe retry."""
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT state FROM scheduled_messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
                if row is None or str(row[0]) not in ("scheduled", "dispatching"):
                    return False
                self._conn.execute(
                    "UPDATE scheduled_messages SET state = 'dispatching', updated_at = ?"
                    " WHERE id = ?",
                    (datetime.now(UTC).isoformat(), message_id),
                )
                return True
        except sqlite3.Error as exc:
            self._log_failure("scheduled message claim", exc)
            return False

    def settle_scheduled_message(
        self, message_id: str, state: str, reason: str | None = None
    ) -> None:
        if state not in ("delivered", "failed"):
            raise ValueError("invalid scheduled message outcome")
        self._write(
            "UPDATE scheduled_messages SET state = ?, reason = ?, updated_at = ?"
            " WHERE id = ? AND state = 'dispatching'",
            (state, reason, datetime.now(UTC).isoformat(), message_id),
        )

    def fail_waiting_scheduled_message(self, message_id: str, reason: str) -> bool:
        """Report a message that never left the store unsent; False if it moved on."""
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE scheduled_messages SET state = 'failed', reason = ?, updated_at = ?"
                    " WHERE id = ? AND state = 'scheduled'",
                    (reason, datetime.now(UTC).isoformat(), message_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("scheduled message fail", exc)
            return False

    def cancel_scheduled_message(self, message_id: str, chat_id: str) -> bool:
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE scheduled_messages SET state = 'cancelled', updated_at = ?"
                    " WHERE id = ? AND chat_id = ? AND state IN ('scheduled', 'failed')",
                    (datetime.now(UTC).isoformat(), message_id, chat_id),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._log_failure("scheduled message cancel", exc)
            return False

    def load_delegated_sessions(self) -> set[str]:
        rows = self._query("SELECT session_key FROM delegated_sessions")
        return {str(row[0]) for row in rows}

    def delegations_recorded_since(self, since: str) -> set[str]:
        """Delegated sessions first recorded at or after ``since`` (#2207)."""
        rows = self._query(
            "SELECT session_key FROM delegated_sessions WHERE created_at >= ?", (since,)
        )
        return {str(row[0]) for row in rows}

    def load_delegation_parents(self) -> dict[str, str]:
        rows = self._query(
            "SELECT session_key, parent_session_id FROM delegated_sessions"
            " WHERE parent_session_id IS NOT NULL"
        )
        return {str(row[0]): str(row[1]) for row in rows}

    def record_delegated_session(
        self, session_key: str, parent_session_id: str | None = None
    ) -> None:
        if parent_session_id is None:
            # Never clobber a previously recorded parent with a bare is_delegated
            # re-record.
            self._write(
                "INSERT OR IGNORE INTO delegated_sessions(session_key, created_at) VALUES (?, ?)",
                (session_key, datetime.now(UTC).isoformat()),
            )
        else:
            self._write(
                "INSERT INTO delegated_sessions"
                "(session_key, created_at, parent_session_id)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(session_key) DO UPDATE SET"
                " parent_session_id = excluded.parent_session_id",
                (session_key, datetime.now(UTC).isoformat(), parent_session_id),
            )

    def mac_runner_callback_state(self, run_id: str) -> str | None:
        """``'delivered'``, ``'failed'``, ``'reserved'``, or None for an unseen run."""
        rows = self._query(
            "SELECT state FROM mac_runner_callbacks WHERE run_id = ?", (run_id,)
        )
        if not rows:
            return None
        return rows[0][0] or "reserved"

    def mac_runner_callback_handed_over(self, run_id: str) -> bool:
        """Whether a send under this run's action id has reached the provider.

        True means the runtime's delivery fence is armed, so the next attempt
        will be collapsed into that first message and gives the chat nothing new.
        Read from disk rather than from the web process's own queue, because the
        fence outlives a web restart and this answer has to as well (#2003).
        """
        rows = self._query(
            "SELECT handed_over_at FROM mac_runner_callbacks WHERE run_id = ?", (run_id,)
        )
        return bool(rows and rows[0][0])

    def mark_mac_runner_callback_handed_over(self, run_id: str) -> None:
        """Record that a send under this run's action id reached the provider.

        Never unset, and untouched by ``reserve``: a fence once armed stays
        armed, so a later retry is a repair however many times the program comes
        back and whatever restarts happen in between.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE mac_runner_callbacks SET handed_over_at = ?"
                    " WHERE run_id = ? AND handed_over_at IS NULL",
                    (datetime.now(UTC).isoformat(), run_id),
                )
        except sqlite3.Error as exc:
            log.debug("mac runner handover marker failed: %s", exc)

    def reserve_mac_runner_callback(
        self, run_id: str, session_key: str, outcome: str
    ) -> bool | None:
        """Claim the right to deliver one run's outcome.

        True means this caller owns the attempt, False that the outcome is already
        in a chat and this is a retry to be answered without touching it, and None
        that no durable row could be written — which the route refuses on, because
        without the row nothing stops the next retry delivering a second message.

        A row left reserved by a dead web process, or settled as failed, is claimed
        again rather than treated as spent: the injection action id is what keeps
        that second attempt from becoming a second message.
        """
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO mac_runner_callbacks"
                    " (run_id, session_key, outcome, reserved_at, settled_at,"
                    " state, detail)"
                    " VALUES (?, ?, ?, ?, NULL, NULL, '')",
                    (run_id, session_key, outcome, datetime.now(UTC).isoformat()),
                )
                if cursor.rowcount == 1:
                    return True
                retried = self._conn.execute(
                    "UPDATE mac_runner_callbacks"
                    " SET reserved_at = ?, settled_at = NULL, state = NULL, detail = ''"
                    " WHERE run_id = ? AND (state IS NULL OR state = 'failed')",
                    (datetime.now(UTC).isoformat(), run_id),
                )
                return retried.rowcount == 1
        except sqlite3.Error as exc:
            log.debug("mac runner callback reservation failed: %s", exc)
            return None

    def settle_mac_runner_callback(
        self, run_id: str, state: str, detail: str = ""
    ) -> None:
        """Record whether the reserved outcome reached the chat."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE mac_runner_callbacks"
                    " SET settled_at = ?, state = ?, detail = ?"
                    " WHERE run_id = ? AND state IS NULL",
                    (datetime.now(UTC).isoformat(), state, detail[:2000], run_id),
                )
        except sqlite3.Error as exc:
            log.debug("mac runner callback settle failed: %s", exc)

    def record_dismissed_delivery(self, action_id: str, session_key: str) -> None:
        """Remember that a person threw this action's optimistic row away (#2031).

        ``session_key`` is where they were looking when they did it, kept for the
        record and deliberately not part of the key — see the table's comment.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO dismissed_deliveries"
                    " (action_id, session_key, dismissed_at) VALUES (?, ?, ?)",
                    (action_id, session_key, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            log.debug("dismissed delivery record failed: %s", exc)

    def delivery_was_dismissed(self, action_id: str) -> bool:
        """Whether that decision is on record. False on any doubt.

        Answering False merely puts the caller back where it was before #2031 —
        the retry draws a row again — so a database that cannot be read must not
        take the rest of the delivery down with it.
        """
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT 1 FROM dismissed_deliveries WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("dismissed delivery lookup failed: %s", exc)
            return False
        return row is not None

    # --- delegations that outlive the web process (#2582) --------------

    _DELEGATION_COLUMNS = (
        "id",
        "state",
        "account_key",
        "session_key",
        "session_token",
        "reason",
        "final_message",
        "owner_pid",
        "created_at",
    )
    _UNFINISHED_DELEGATION_STATES = ("starting", "running")

    def save_delegation(self, record: Mapping[str, Any]) -> None:
        """Write one delegation's whole current status, replacing what was there."""
        values = tuple(record.get(column) for column in self._DELEGATION_COLUMNS)
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO delegations"
                    f" ({', '.join(self._DELEGATION_COLUMNS)}, updated_at)"
                    f" VALUES ({', '.join('?' for _ in self._DELEGATION_COLUMNS)}, ?)",
                    (*values, datetime.now(UTC).timestamp()),
                )
        except sqlite3.Error as exc:
            log.warning("delegation save failed for %s: %s", record.get("id"), exc)

    def _delegation_rows(self, where: str, params: tuple) -> list[dict[str, Any]]:
        rows = self._query(
            f"SELECT {', '.join(self._DELEGATION_COLUMNS)} FROM delegations WHERE {where}",
            params,
        )
        return [dict(zip(self._DELEGATION_COLUMNS, row, strict=True)) for row in rows]

    def load_delegation(self, delegation_id: str) -> dict[str, Any] | None:
        rows = self._delegation_rows("id = ?", (delegation_id,))
        return rows[0] if rows else None

    def unfinished_delegations(self) -> list[dict[str, Any]]:
        return self._delegation_rows(
            "state IN (?, ?) ORDER BY created_at", self._UNFINISHED_DELEGATION_STATES
        )

    def claim_delegation(
        self, delegation_id: str, expected_owner: int | None, owner: int
    ) -> bool:
        """Make ``owner`` the watcher of an unfinished delegation; False if someone else did.

        Conditional on the owner still being the one the caller saw, so two web
        processes taking over the same row cannot both win it.
        """
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "UPDATE delegations SET owner_pid = ?, updated_at = ?"
                    " WHERE id = ? AND owner_pid IS ? AND state IN (?, ?)",
                    (
                        owner,
                        datetime.now(UTC).timestamp(),
                        delegation_id,
                        expected_owner,
                        *self._UNFINISHED_DELEGATION_STATES,
                    ),
                )
        except sqlite3.Error as exc:
            log.warning("delegation claim failed for %s: %s", delegation_id, exc)
            return False
        return cursor.rowcount == 1

    def prune_delegations(self, finished_before: float) -> None:
        """Forget finished delegations last written before ``finished_before``.

        An unfinished one is never pruned: a caller may still be waiting on it.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM delegations WHERE state NOT IN (?, ?) AND updated_at < ?",
                    (*self._UNFINISHED_DELEGATION_STATES, finished_before),
                )
        except sqlite3.Error as exc:
            log.warning("delegation prune failed: %s", exc)

    # --- side chats started from a Work's page (#2388) ------------------

    def record_work_side_chat(
        self, action_id: str, work_id: str, pull_requests: Sequence[str]
    ) -> bool:
        """Save a side chat's pending links before it is started; False if it was not saved.

        Written before the spawn, so a web restart before the provider names the chat
        still links it. ``pull_requests`` are the refs the Work showed at the press.
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO work_side_chats"
                    " (action_id, work_id, pull_requests, created_at) VALUES (?, ?, ?, ?)",
                    (
                        action_id,
                        work_id,
                        json.dumps(list(pull_requests)),
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return True
        except sqlite3.Error as exc:
            self._log_failure("write", exc)
            return False

    def pending_work_side_chats(self) -> list[PendingSideChat]:
        rows = self._query(
            "SELECT action_id, work_id, pull_requests, created_at FROM work_side_chats"
            " ORDER BY created_at, action_id"
        )
        pending = []
        for action_id, work_id, pulls, created_at in rows:
            try:
                refs = tuple(str(item) for item in json.loads(pulls or "[]"))
            except (TypeError, ValueError):
                refs = ()
            try:
                created = datetime.fromisoformat(str(created_at))
            except ValueError:
                created = datetime.min.replace(tzinfo=UTC)
            pending.append(PendingSideChat(str(action_id), str(work_id), refs, created))
        return pending

    def delete_work_side_chat(self, action_id: str) -> None:
        self._write("DELETE FROM work_side_chats WHERE action_id = ?", (action_id,))

    def reserve_work_action(self, action_id: str, work_id: str, action: str) -> bool | None:
        """Reserve a Work action before its write (#2237); False means the id is spent.

        ``None`` is "the reservation could not be made", which the route refuses on: without
        a durable row nothing stops the same press writing twice.
        """
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO work_actions"
                    " (action_id, work_id, action, reserved_at, settled_at, outcome, result)"
                    " VALUES (?, ?, ?, ?, NULL, NULL, '')",
                    (action_id, work_id, action, datetime.now(UTC).isoformat()),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            log.debug("work action reservation failed: %s", exc)
            return None

    def release_failed_work_action(self, action_id: str) -> None:
        """Free an action id whose write was refused before anything ran (#2971).

        Only a ``failed`` row: one still unsettled may have begun its write, and a ``done``
        one did, so either stays spent.
        """
        self._write(
            "DELETE FROM work_actions WHERE action_id = ? AND outcome = 'failed'", (action_id,)
        )

    def settle_work_action(self, action_id: str, outcome: str, result: str = "") -> None:
        """Record what the reserved action did. Never releases the row."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE work_actions SET settled_at = ?, outcome = ?, result = ?"
                    " WHERE action_id = ? AND outcome IS NULL",
                    (datetime.now(UTC).isoformat(), outcome, result[:2000], action_id),
                )
        except sqlite3.Error as exc:
            log.debug("work action settle failed: %s", exc)

    _WORK_ACTION_COLUMNS = "action_id, work_id, action, reserved_at, settled_at, outcome, result"

    @staticmethod
    def _work_action_press(row: Any) -> WorkActionPress:
        # Here rather than at the top: ``work_actions`` reads the Work model, and the store
        # must not import the whole of it to hand back one row.
        from .work_actions import WorkActionPress

        return WorkActionPress(
            action_id=str(row[0]),
            work_id=str(row[1]),
            action=str(row[2]),
            reserved_at=str(row[3]),
            settled_at=str(row[4]) if row[4] is not None else None,
            outcome=str(row[5]) if row[5] is not None else None,
            result=str(row[6] or ""),
        )

    def work_action(self, action_id: str) -> WorkActionPress | None:
        """The press recorded under this action id, if any."""
        try:
            with self._lock:
                row = self._conn.execute(
                    f"SELECT {self._WORK_ACTION_COLUMNS} FROM work_actions WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("work action read failed: %s", exc)
            return None
        return self._work_action_press(row) if row is not None else None

    def load_work_actions(self, work_id: str, limit: int = 20) -> tuple[WorkActionPress, ...]:
        """The Work's newest presses, newest first.

        A chat message posted to the issue (#2416), or a reviewer's comment (#2814), shares the
        ledger but is not a press on the Work: it is left out before the limit, so it can
        neither stand for the Work's last action nor push a Merge out of the double-press window.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._WORK_ACTION_COLUMNS} FROM work_actions WHERE work_id = ?"
                    " AND action NOT IN (?, ?) ORDER BY reserved_at DESC, rowid DESC LIMIT ?",
                    (work_id, ISSUE_POST_ACTION, REVIEWER_COMMENT_ACTION, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work action read failed: %s", exc)
            return ()
        return tuple(self._work_action_press(row) for row in rows)

    def load_issue_posts(self, work_id: str, limit: int = 1) -> tuple[WorkActionPress, ...]:
        """The newest chat messages posted to this Work's issue (#2416), newest first."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._WORK_ACTION_COLUMNS} FROM work_actions WHERE work_id = ?"
                    " AND action = ? ORDER BY reserved_at DESC, rowid DESC LIMIT ?",
                    (work_id, ISSUE_POST_ACTION, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("issue post read failed: %s", exc)
            return ()
        return tuple(self._work_action_press(row) for row in rows)

    def hide_work(self, work_id: str, comments: int | None, card_mark: str | None) -> bool:
        """Hide a Work from Needs you (#2238); hiding it again re-marks it. False on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO work_hidden (work_id, hidden_at, comments, card_mark)"
                    " VALUES (?, ?, ?, ?)",
                    (work_id, datetime.now(UTC).isoformat(), comments, card_mark),
                )
        except sqlite3.Error as exc:
            log.debug("work hide failed: %s", exc)
            return False
        return True

    def unhide_work(self, work_id: str) -> bool:
        """Bring a hidden Work back; True also when it was not hidden. False on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM work_hidden WHERE work_id = ?", (work_id,))
        except sqlite3.Error as exc:
            log.debug("work unhide failed: %s", exc)
            return False
        return True

    def note_hidden_comments(self, work_id: str, comments: int) -> None:
        """Lower a hidden Work's comment baseline to ``comments``, or set it when unknown.

        Never raises it: a higher count is a new comment, which ends the hide instead. A
        lower one is a deleted comment, and the next new comment must still count (#2238).
        """
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE work_hidden SET comments = ?"
                    " WHERE work_id = ? AND (comments IS NULL OR comments > ?)",
                    (comments, work_id, comments),
                )
        except sqlite3.Error as exc:
            log.debug("work hide update failed: %s", exc)

    def hidden_works(self) -> dict[str, HiddenWork]:
        """Every hidden Work by id. Unreadable is empty: a Work is then simply not hidden."""
        from .work_hidden import HiddenWork

        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT work_id, hidden_at, comments, card_mark FROM work_hidden"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work hide read failed: %s", exc)
            return {}
        return {
            str(row[0]): HiddenWork(
                str(row[0]),
                str(row[1]),
                int(row[2]) if row[2] is not None else None,
                str(row[3]) if row[3] is not None else None,
            )
            for row in rows
        }

    def pause_work(self, work_id: str) -> bool:
        """Pause a Work on this deck (#2559); pausing it again keeps the first time."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO work_paused (work_id, paused_at) VALUES (?, ?)",
                    (work_id, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            log.debug("work pause failed: %s", exc)
            return False
        return True

    def resume_work(self, work_id: str) -> bool:
        """End a deck pause; True also when the Work was not paused. False on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM work_paused WHERE work_id = ?", (work_id,))
        except sqlite3.Error as exc:
            log.debug("work resume failed: %s", exc)
            return False
        return True

    def paused_works(self) -> dict[str, datetime]:
        """Every Work paused on this deck, by id (#2559). Unreadable is empty."""
        try:
            with self._lock:
                rows = self._conn.execute("SELECT work_id, paused_at FROM work_paused").fetchall()
        except sqlite3.Error as exc:
            log.debug("work pause read failed: %s", exc)
            return {}
        found: dict[str, datetime] = {}
        for work_id, paused_at in rows:
            try:
                found[str(work_id)] = datetime.fromisoformat(str(paused_at))
            except ValueError:
                continue
        return found

    def star_work(self, work_id: str, starred: bool = True) -> bool:
        """Star a Work, or unstar it with ``starred=False`` (#3116). Both are idempotent;
        False only when the write failed."""
        try:
            with self._lock, self._conn:
                if starred:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO work_starred (work_id, starred_at) VALUES (?, ?)",
                        (work_id, datetime.now(UTC).isoformat()),
                    )
                else:
                    self._conn.execute("DELETE FROM work_starred WHERE work_id = ?", (work_id,))
        except sqlite3.Error as exc:
            log.debug("work star failed: %s", exc)
            return False
        return True

    def starred_works(self) -> frozenset[str]:
        """Every starred Work's id (#3116). Unreadable is none: a star only reorders."""
        try:
            with self._lock:
                rows = self._conn.execute("SELECT work_id FROM work_starred").fetchall()
        except sqlite3.Error as exc:
            log.debug("work star read failed: %s", exc)
            return frozenset()
        return frozenset(str(row[0]) for row in rows)

    def note_work_closed(self, work_id: str, closed_at: datetime) -> None:
        """Remember that a Work's issue closed and waits for a person's Finish (#2510).

        Only ever moves ``closed_at`` forward, so a stale pass cannot pull back a newer close.
        """
        stamp = closed_at.astimezone(UTC).isoformat()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_signoff (work_id, closed_at, finished_at) VALUES (?, ?, NULL)"
                    " ON CONFLICT(work_id) DO UPDATE SET closed_at = excluded.closed_at"
                    " WHERE excluded.closed_at > work_signoff.closed_at",
                    (work_id, stamp),
                )
        except sqlite3.Error as exc:
            log.debug("work close note failed: %s", exc)

    def finish_work(self, work_id: str, at: datetime) -> bool:
        """Record a person's Finish on a Work (#2510); a later press moves it. False on failure."""
        stamp = at.astimezone(UTC).isoformat()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_signoff (work_id, closed_at, finished_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(work_id) DO UPDATE SET finished_at = excluded.finished_at",
                    (work_id, stamp, stamp),
                )
        except sqlite3.Error as exc:
            log.debug("work finish failed: %s", exc)
            return False
        return True

    def work_signoffs(self) -> dict[str, tuple[datetime, datetime | None]]:
        """Every Work's ``(closed_at, finished_at)`` (#2510). Unreadable is empty."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT work_id, closed_at, finished_at FROM work_signoff"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work sign-off read failed: %s", exc)
            return {}
        found: dict[str, tuple[datetime, datetime | None]] = {}
        for work_id, closed, finished in rows:
            try:
                found[str(work_id)] = (
                    datetime.fromisoformat(str(closed)),
                    datetime.fromisoformat(str(finished)) if finished else None,
                )
            except ValueError:
                continue
        return found

    def work_stops(self) -> dict[str, WorkStop]:
        """Every Work's stored stop (#2283). Unreadable is empty: nothing is then new."""
        from .work_stops import WorkStop

        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT work_id, category, stopped_at, facts_mark FROM work_stops"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work stop read failed: %s", exc)
            return {}
        found = {}
        for work_id, category, stopped_at, mark in rows:
            try:
                when = datetime.fromisoformat(str(stopped_at))
            except ValueError:
                continue
            found[str(work_id)] = WorkStop(
                str(work_id), str(category), when, str(mark) if mark is not None else None
            )
        return found

    def save_work_stops(self, stops: Iterable[WorkStop], *, forget_before: datetime) -> None:
        """Write the changed stops, and drop ones that left Needs you before ``forget_before``."""
        rows = [(s.work_id, s.category, s.stopped_at.isoformat(), s.mark) for s in stops]
        try:
            with self._lock, self._conn:
                if rows:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO work_stops"
                        " (work_id, category, stopped_at, facts_mark) VALUES (?, ?, ?, ?)",
                        rows,
                    )
                self._conn.execute(
                    "DELETE FROM work_stops WHERE category = '' AND stopped_at < ?",
                    (forget_before.isoformat(),),
                )
        except sqlite3.Error as exc:
            log.debug("work stop write failed: %s", exc)

    # --- Work status log (#2751) ----------------------------------------

    _STATUS_LOG_FIELDS = (
        "at, work_id, section, category, kind, tone, headline, prev_section, prev_category,"
        " prev_kind, prev_tone, facts_mark, why, degraded, pid, revision"
    )

    def _prune_work_status_log(self) -> None:
        """Drop status-log rows past ``work_status_log.RETENTION``, once at open."""
        from .work_status_log import RETENTION

        cutoff = (datetime.now(UTC) - RETENTION).isoformat()
        self._write("DELETE FROM work_status_log WHERE at < ?", (cutoff,))

    def append_work_status_log(self, points: Iterable[WorkStatusPoint]) -> None:
        """Append status changes; a failed write is logged and the pass goes on."""
        rows = [
            (
                p.at.isoformat(),
                p.work_id,
                p.section,
                p.category,
                p.kind,
                p.tone,
                p.headline,
                p.prev_section,
                p.prev_category,
                p.prev_kind,
                p.prev_tone,
                p.facts_mark,
                json.dumps(dict(p.why), default=str, separators=(",", ":")),
                json.dumps(list(p.degraded)),
                p.pid,
                p.revision,
            )
            for p in points
        ]
        if not rows:
            return
        try:
            with self._lock, self._conn:
                self._conn.executemany(
                    f"INSERT INTO work_status_log ({self._STATUS_LOG_FIELDS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
        except sqlite3.Error as exc:
            log.warning("work status log write failed: %s", exc)

    def work_status_log(
        self,
        *,
        work_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> list[WorkStatusPoint]:
        """The newest ``limit`` status-log rows matching, oldest first."""
        where, args = [], []
        if work_id is not None:
            where.append("work_id = ?")
            args.append(work_id)
        if since is not None:
            where.append("at >= ?")
            args.append(since.isoformat())
        if until is not None:
            where.append("at <= ?")
            args.append(until.isoformat())
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._STATUS_LOG_FIELDS} FROM work_status_log{clause}"
                    " ORDER BY id DESC LIMIT ?",
                    (*args, max(0, int(limit))),
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work status log read failed: %s", exc)
            return []
        return [p for p in map(_status_point, reversed(rows)) if p is not None]

    def latest_work_status_points(self) -> dict[str, WorkStatusPoint]:
        """Each Work's newest status-log row: where the next pass compares from."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT {self._STATUS_LOG_FIELDS} FROM work_status_log WHERE id IN"
                    " (SELECT MAX(id) FROM work_status_log GROUP BY work_id)"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work status log read failed: %s", exc)
            return {}
        found = {}
        for row in rows:
            item = _status_point(row)
            if item is not None:
                found[item.work_id] = item
        return found

    def work_stops_started(self) -> datetime | None:
        """When the first stop-tracking pass ran (#2283); None before it, or when unreadable."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT started_at FROM work_stops_started WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("work stops start read failed: %s", exc)
            return None
        if row is None:
            return None
        try:
            return datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None

    def start_work_stops(self, started_at: datetime) -> None:
        """Record that the first stop-tracking pass ran; a later call keeps the first stamp."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO work_stops_started (id, started_at) VALUES (1, ?)",
                    (started_at.isoformat(),),
                )
        except sqlite3.Error as exc:
            log.debug("work stops start write failed: %s", exc)

    def works_opened(self) -> dict[str, datetime]:
        """When each Work's page was last opened (#2283)."""
        try:
            with self._lock:
                rows = self._conn.execute("SELECT work_id, opened_at FROM work_opened").fetchall()
        except sqlite3.Error as exc:
            log.debug("work opened read failed: %s", exc)
            return {}
        found = {}
        for work_id, opened_at in rows:
            try:
                found[str(work_id)] = datetime.fromisoformat(str(opened_at))
            except ValueError:
                continue
        return found

    def note_work_opened(self, work_ids: Iterable[str], opened_at: datetime) -> bool:
        """Record these Works as opened at ``opened_at``; a later mark already kept stands."""
        when = opened_at.astimezone(UTC)
        rows = [(work_id, when.isoformat()) for work_id in work_ids]
        try:
            with self._lock, self._conn:
                for work_id, stamp in rows:
                    before = self._conn.execute(
                        "SELECT opened_at FROM work_opened WHERE work_id = ?", (work_id,)
                    ).fetchone()
                    if before is not None:
                        try:
                            if datetime.fromisoformat(str(before[0])) >= when:
                                continue
                        except ValueError:
                            pass
                    self._conn.execute(
                        "INSERT OR REPLACE INTO work_opened (work_id, opened_at) VALUES (?, ?)",
                        (work_id, stamp),
                    )
        except sqlite3.Error as exc:
            log.debug("work opened write failed: %s", exc)
            return False
        return True

    def work_titles(self) -> dict[str, str]:
        """Every custom Work title by id (#2328). Unreadable is empty: the derived title shows."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT work_id, title FROM work_metadata"
                    " WHERE title IS NOT NULL AND title != ''"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work title read failed: %s", exc)
            return {}
        return {str(work_id): str(title) for work_id, title in rows}

    def set_work_title(self, work_id: str, title: str) -> bool:
        """Set a Work's custom title; an empty one clears it. False on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_metadata (work_id, title, title_updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(work_id) DO UPDATE SET title = excluded.title,"
                    " title_updated_at = excluded.title_updated_at",
                    (work_id, title or None, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            log.debug("work title write failed: %s", exc)
            return False
        return True

    def work_agent_titles(self) -> dict[str, str]:
        """Every name a worker gave its Work (#2474), by id. The operator's own ``title`` is
        separate and wins; unreadable is empty, so the derived title shows."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT work_id, agent_title FROM work_metadata"
                    " WHERE agent_title IS NOT NULL AND agent_title != ''"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work agent title read failed: %s", exc)
            return {}
        return {str(work_id): str(title) for work_id, title in rows}

    def work_start_chat_titles(self) -> dict[str, str]:
        """The name each Work's own agent chat gave itself, by Work id (#3597).

        A Work agent names its chat, not its Work, so this is the Work's name when nothing
        better is set. Only the chat's own name counts: a Deckhand guess or a name the operator
        typed on the chat is skipped. Unreadable is empty, so the derived title shows.

        The own chat is the one the Work's start token names. A start that took a chat begun
        before its issue existed (#3590) runs it under another token, so then it is the
        Work's first side chat bound after the start was reserved: that start links its chat
        before any helper chat can be started from the Work. A title lives on the chat's
        newest leg, which a Continue gives a key of its own, so every leg is read; newest wins.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "WITH own AS ("
                    " SELECT a.work_id, COALESCE(t.session_key, ("
                    "  SELECT b.session_key FROM session_work_bindings b"
                    "  WHERE b.work_id = a.work_id AND b.evidence = 'side_chat'"
                    "  AND b.bound_at >= a.reserved_at ORDER BY b.bound_at LIMIT 1"
                    " )) AS session_key"
                    " FROM work_actions a LEFT JOIN session_tokens t ON t.token = a.action_id"
                    " WHERE a.action_id LIKE 'work-start-%')"
                    " SELECT own.work_id, g.title FROM own"
                    " LEFT JOIN chat_legs mine ON mine.session_key = own.session_key"
                    " LEFT JOIN chat_legs leg ON leg.chat_id = mine.chat_id"
                    " JOIN generated_titles g"
                    " ON g.session_key = COALESCE(leg.session_key, own.session_key)"
                    " WHERE g.source = 'agent' AND g.title != '' ORDER BY g.updated_at"
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("work start chat title read failed: %s", exc)
            return {}
        return {str(work_id): str(title) for work_id, title in rows}

    def set_work_agent_title(self, work_id: str, title: str, session_key: str) -> bool:
        """Set the name a worker gave its Work; an empty one clears it. Never touches the
        operator's ``title``. False on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_metadata (work_id, agent_title, agent_title_updated_at,"
                    " agent_title_session_key) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(work_id) DO UPDATE SET agent_title = excluded.agent_title,"
                    " agent_title_updated_at = excluded.agent_title_updated_at,"
                    " agent_title_session_key = excluded.agent_title_session_key",
                    (
                        work_id,
                        title or None,
                        datetime.now(UTC).isoformat(),
                        session_key or None,
                    ),
                )
        except sqlite3.Error as exc:
            log.debug("work agent title write failed: %s", exc)
            return False
        return True

    def work_resolution(self, work_id: str) -> WorkResolution | None:
        """The Work's resolution (#2328), or None when none was written or it is unreadable."""
        from .work_resolution import WorkResolution

        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT problem_summary, solution_summary, decision_rationale,"
                    " correctness_summary, resolution_updated_at, resolution_run_id,"
                    " resolution_session_key FROM work_metadata"
                    " WHERE work_id = ? AND resolution_updated_at IS NOT NULL",
                    (work_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("work resolution read failed: %s", exc)
            return None
        if row is None:
            return None
        try:
            updated = datetime.fromisoformat(str(row[4]))
        except ValueError:
            updated = None
        return WorkResolution(
            work_id,
            str(row[0] or ""),
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or ""),
            updated,
            str(row[5] or ""),
            str(row[6] or ""),
        )

    def set_work_resolution(self, resolution: WorkResolution) -> WorkResolution | None:
        """Replace the Work's whole resolution, keeping its title; the stored record, or None."""
        stamp = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_metadata (work_id, problem_summary, solution_summary,"
                    " decision_rationale, correctness_summary, resolution_updated_at,"
                    " resolution_run_id, resolution_session_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(work_id) DO UPDATE SET"
                    " problem_summary = excluded.problem_summary,"
                    " solution_summary = excluded.solution_summary,"
                    " decision_rationale = excluded.decision_rationale,"
                    " correctness_summary = excluded.correctness_summary,"
                    " resolution_updated_at = excluded.resolution_updated_at,"
                    " resolution_run_id = excluded.resolution_run_id,"
                    " resolution_session_key = excluded.resolution_session_key",
                    (
                        resolution.work_id,
                        resolution.problem,
                        resolution.solution,
                        resolution.rationale,
                        resolution.correctness,
                        stamp,
                        resolution.run_id or None,
                        resolution.session_key or None,
                    ),
                )
        except sqlite3.Error as exc:
            log.debug("work resolution write failed: %s", exc)
            return None
        return self.work_resolution(resolution.work_id)

    def work_live_check(self, work_id: str) -> LiveCheck | None:
        """The agent's live check of the Work (#3141), or None when none or unreadable."""
        from .work_resolution import LiveCheck

        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT live_check_result, live_check_note, live_check_at,"
                    " live_check_session_key FROM work_metadata"
                    " WHERE work_id = ? AND live_check_result IS NOT NULL",
                    (work_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("work live check read failed: %s", exc)
            return None
        if row is None:
            return None
        try:
            checked = datetime.fromisoformat(str(row[2]))
        except ValueError:
            checked = None
        return LiveCheck(work_id, str(row[0]), str(row[1] or ""), checked, str(row[3] or ""))

    def set_work_live_check(
        self, work_id: str, result: str, note: str, session_key: str
    ) -> LiveCheck | None:
        """Record the agent's live check (#3141), replacing an earlier one; None on failure."""
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO work_metadata (work_id, live_check_result, live_check_note,"
                    " live_check_at, live_check_session_key) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(work_id) DO UPDATE SET"
                    " live_check_result = excluded.live_check_result,"
                    " live_check_note = excluded.live_check_note,"
                    " live_check_at = excluded.live_check_at,"
                    " live_check_session_key = excluded.live_check_session_key",
                    (
                        work_id,
                        result,
                        note or None,
                        datetime.now(UTC).isoformat(),
                        session_key or None,
                    ),
                )
        except sqlite3.Error as exc:
            log.debug("work live check write failed: %s", exc)
            return None
        return self.work_live_check(work_id)

    def wayfinder_layout(self, work_id: str) -> dict[str, tuple[float, float]]:
        """Where a person moved a wayfinder map's boxes (#3627); empty for none or unreadable."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT positions FROM wayfinder_layout WHERE work_id = ?", (work_id,)
                ).fetchone()
            value = json.loads(row[0]) if row is not None else {}
        except (sqlite3.Error, ValueError) as exc:
            log.debug("wayfinder layout read failed: %s", exc)
            return {}
        found = {}
        for key, at in (value.items() if isinstance(value, dict) else ()):
            if (
                isinstance(at, list)
                and len(at) == 2
                and all(isinstance(v, int | float) and not isinstance(v, bool) for v in at)
            ):
                found[str(key)] = (float(at[0]), float(at[1]))
        return found

    def move_wayfinder_box(
        self, work_id: str, key: str, at: tuple[float, float] | None
    ) -> bool:
        """Put one of a map's boxes at ``at``, or back in the server's layout for None (#3627).

        One box per write, read and written in one transaction, so two devices moving
        different boxes never erase each other's move."""
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT positions FROM wayfinder_layout WHERE work_id = ?", (work_id,)
                ).fetchone()
                try:
                    held = json.loads(row[0]) if row is not None else {}
                except ValueError:
                    held = {}
                held = held if isinstance(held, dict) else {}
                if at is None:
                    held.pop(key, None)
                else:
                    held[key] = [at[0], at[1]]
                if not held:
                    self._conn.execute(
                        "DELETE FROM wayfinder_layout WHERE work_id = ?", (work_id,)
                    )
                    return True
                self._conn.execute(
                    "INSERT INTO wayfinder_layout (work_id, positions, updated_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(work_id) DO UPDATE SET"
                    " positions = excluded.positions, updated_at = excluded.updated_at",
                    (
                        work_id,
                        json.dumps(held, separators=(",", ":"), sort_keys=True),
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except sqlite3.Error as exc:
            log.debug("wayfinder layout write failed: %s", exc)
            return False
        return True

    def reset_wayfinder_layout(self, work_id: str) -> bool:
        """Put every box of a map back in the server's layout (#3627)."""
        try:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM wayfinder_layout WHERE work_id = ?", (work_id,))
        except sqlite3.Error as exc:
            log.debug("wayfinder layout reset failed: %s", exc)
            return False
        return True

    def load_boss_turn_windows(self) -> dict[str, str]:
        """Every chat's current attempt window (#472)."""
        rows = self._query("SELECT session_key, opened_at FROM boss_turn_windows")
        return {str(key): str(opened_at) for key, opened_at in rows}

    def open_boss_turn_window(self, session_key: str, opened_at: str) -> None:
        """Start a fresh window for one chat, making every headline attemptable again."""
        self._write(
            "INSERT INTO boss_turn_windows(session_key, opened_at) VALUES (?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET opened_at=excluded.opened_at",
            (session_key, opened_at),
        )

    def record_manual_new_chat_cwd(self, cwd: str) -> None:
        self._write(
            "INSERT INTO manual_new_chat_state(singleton, cwd, updated_at)"
            " VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " cwd=excluded.cwd, updated_at=excluded.updated_at",
            (cwd, datetime.now(UTC).isoformat()),
        )

    def record_account_new_chat_defaults(
        self, account_key: str, model: str | None, effort: str | None
    ) -> None:
        self._write(
            "INSERT INTO account_new_chat_defaults(account_key, model, effort, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(account_key) DO UPDATE SET"
            " model=excluded.model, effort=excluded.effort, updated_at=excluded.updated_at",
            (account_key, model, effort, datetime.now(UTC).isoformat()),
        )

    # --- planned usage resets (#3379) -----------------------------------

    def planned_resets(self, now: datetime | None = None) -> dict[str, datetime]:
        """Each account's planned reset still ahead; past and unreadable ones read as none."""
        moment = now or datetime.now(UTC)
        plans: dict[str, datetime] = {}
        for account, value in self._query("SELECT account_key, reset_at FROM planned_resets"):
            try:
                at = datetime.fromisoformat(str(value))
            except ValueError:
                continue
            if at.tzinfo is not None and at > moment:
                plans[str(account)] = at
        return plans

    def record_planned_reset(self, account_key: str, at: datetime | None) -> None:
        """Set one account's plan, or clear it with ``None``; past plans are dropped too."""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM planned_resets WHERE account_key = ? OR reset_at <= ?",
                (account_key, datetime.now(UTC).isoformat()),
            )
            if at is not None:
                self._conn.execute(
                    "INSERT INTO planned_resets(account_key, reset_at) VALUES (?, ?)",
                    # UTC, so the text comparison above orders every stored time correctly.
                    (account_key, at.astimezone(UTC).isoformat()),
                )

    # --- per-chat model preference (#1665) ----------------------------

    def chat_model_preference(self, chat_id: str) -> str:
        """The model this chat's next leg should start on, or ``""`` for none."""
        if not chat_id:
            return ""
        rows = self._query(
            "SELECT model FROM chat_model_preferences WHERE chat_id = ?", (chat_id,)
        )
        return str(rows[0][0]) if rows else ""

    def record_chat_model_preference(self, chat_id: str, model: str | None) -> None:
        """Store the operator's choice, or clear it when they choose no preference."""
        if not model:
            self._write(
                "DELETE FROM chat_model_preferences WHERE chat_id = ?", (chat_id,)
            )
            return
        self._write(
            "INSERT INTO chat_model_preferences(chat_id, model, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            " model=excluded.model, updated_at=excluded.updated_at",
            (chat_id, model, datetime.now(UTC).isoformat()),
        )

    def load_assistant_checkpoint(self) -> dict[str, Any] | None:
        rows = self._query("SELECT payload FROM assistant_checkpoint WHERE singleton = 1")
        if not rows:
            return None
        try:
            payload = json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError) as exc:
            log.debug("load_assistant_checkpoint decode failed: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    def record_assistant_checkpoint(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            log.debug("record_assistant_checkpoint encode failed: %s", exc)
            return
        self._write(
            "INSERT INTO assistant_checkpoint(singleton, payload, updated_at)"
            " VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " payload=excluded.payload, updated_at=excluded.updated_at",
            (encoded, datetime.now(UTC).isoformat()),
        )

    # --- web push (issue #7) ------------------------------------------

    def load_vapid_keys(self) -> tuple[str, str] | None:
        rows = self._query("SELECT public_key, private_pem FROM push_vapid WHERE singleton = 1")
        return (rows[0][0], rows[0][1]) if rows else None

    def save_vapid_keys(self, public_key: str, private_pem: str) -> None:
        self._write(
            "INSERT INTO push_vapid(singleton, public_key, private_pem, created_at)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET"
            " public_key=excluded.public_key, private_pem=excluded.private_pem,"
            " created_at=excluded.created_at",
            (public_key, private_pem, datetime.now(UTC).isoformat()),
        )

    def load_push_subscriptions(self) -> list[dict]:
        """Subscriptions in the ``subscription_info`` shape pywebpush expects."""
        rows = self._query("SELECT endpoint, p256dh, auth FROM push_subscriptions")
        return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}} for r in rows]

    def add_push_subscription(self, endpoint: str, p256dh: str, auth: str) -> None:
        self._write(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET"
            " p256dh=excluded.p256dh, auth=excluded.auth",
            (endpoint, p256dh, auth, datetime.now(UTC).isoformat()),
        )

    def delete_push_subscription(self, endpoint: str) -> None:
        self._write("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def load_needs_you_pushed(self) -> set[tuple[str, str]] | None:
        """The Needs you stops already pushed (#3348); None before the first pass stored one."""
        rows = self._query("SELECT payload FROM push_needs_you_sent WHERE singleton = 1")
        if not rows:
            return None
        try:
            items = json.loads(rows[0][0])
        except (TypeError, ValueError):
            return set()
        return {
            (str(item[0]), str(item[1]))
            for item in items if isinstance(item, list) and len(item) == 2
        }

    def save_needs_you_pushed(self, sent: Iterable[tuple[str, str]]) -> None:
        self._write(
            "INSERT INTO push_needs_you_sent(singleton, payload, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " payload=excluded.payload, updated_at=excluded.updated_at",
            (json.dumps(sorted([list(item) for item in sent])), datetime.now(UTC).isoformat()),
        )

    def load_needs_you_showing(self) -> dict[str, set[str]]:
        """The Needs you notifications sent and not yet cleared, by tag (#3365)."""
        rows = self._query("SELECT payload FROM push_needs_you_showing WHERE singleton = 1")
        try:
            items = json.loads(rows[0][0]) if rows else {}
        except (TypeError, ValueError):
            return {}
        if not isinstance(items, dict):
            return {}
        return {
            str(tag): {str(work) for work in works}
            for tag, works in items.items() if isinstance(works, list)
        }

    def save_needs_you_showing(self, showing: dict[str, set[str]]) -> None:
        self._write(
            "INSERT INTO push_needs_you_showing(singleton, payload, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " payload=excluded.payload, updated_at=excluded.updated_at",
            (
                json.dumps({tag: sorted(works) for tag, works in sorted(showing.items())}),
                datetime.now(UTC).isoformat(),
            ),
        )

    # --- poller review history (#1122) ---

    def record_poller_review_verdict(self, verdict: PollerReviewVerdict) -> None:
        """Store what one merge review decided about one commit, replacing any earlier row.

        The poller drains its own records and may hand the same one over twice when a
        removal failed after the write landed, so this has to be an upsert rather than an
        insert: a duplicated delivery must not become a duplicated review.
        """
        self._write(
            "INSERT INTO poller_review_verdicts("
            " repo, sha, pull_request, issue_repo, issue_number, verdict, result,"
            " auto, generation, reviewed_at, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(repo, sha) DO UPDATE SET"
            " pull_request=excluded.pull_request, issue_repo=excluded.issue_repo,"
            " issue_number=excluded.issue_number, verdict=excluded.verdict,"
            " result=excluded.result, auto=excluded.auto,"
            " generation=excluded.generation, reviewed_at=excluded.reviewed_at,"
            " recorded_at=excluded.recorded_at",
            (
                verdict.repo,
                verdict.sha,
                verdict.pull_request,
                verdict.issue_repo,
                int(verdict.issue_number or 0),
                verdict.verdict,
                verdict.result,
                1 if verdict.auto else 0,
                verdict.generation,
                verdict.reviewed_at.isoformat() if verdict.reviewed_at else None,
                verdict.recorded_at.isoformat(),
            ),
        )

    def load_poller_review_verdicts(
        self, limit: int = 200, repo: str | None = None
    ) -> tuple[PollerReviewVerdict, ...]:
        """The most recently recorded verdicts, newest first."""
        sql = (
            "SELECT repo, sha, pull_request, issue_repo, issue_number, verdict, result,"
            " auto, generation, reviewed_at, recorded_at FROM poller_review_verdicts"
        )
        params: tuple = ()
        if repo:
            sql += " WHERE repo = ?"
            params = (repo,)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        rows = self._query(sql, (*params, int(limit)))
        return tuple(_poller_review_verdict(row) for row in rows)

    def poller_review_verdict(self, repo: str, sha: str) -> PollerReviewVerdict | None:
        """What a review decided about exactly this commit, if anything.

        The repository match ignores case, because the spellings meet here from two
        places: a verdict carries the repository GitHub named, while a session's chip
        (#1127) asks with the spelling its own git remote uses. GitHub repository names
        are case-insensitive, so those are the same repository and must find one row.
        """
        rows = self._query(
            "SELECT repo, sha, pull_request, issue_repo, issue_number, verdict, result,"
            " auto, generation, reviewed_at, recorded_at FROM poller_review_verdicts"
            " WHERE repo = ? COLLATE NOCASE AND sha = ?",
            (repo, sha),
        )
        return _poller_review_verdict(rows[0]) if rows else None

    # --- which commit each ChatGPT review chat is about (#2088) ---

    def record_chat_review_head(
        self,
        chat_repository: str,
        chat_number: int,
        target_repo: str,
        target_number: int,
        head_sha: str,
    ) -> None:
        """Remember the commit a review chat was opened about for one pull request.

        An upsert, because the same chat can be written twice: a create whose reconcile
        handed the chat to another caller still ends with both callers holding the same
        chat number and the same heads, and a retry must not become a second row.
        """
        self._write(
            "INSERT INTO chat_review_heads("
            " chat_repository, chat_number, target_repo, target_number, head_sha,"
            " recorded_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_repository, chat_number, target_repo, target_number)"
            " DO UPDATE SET head_sha=excluded.head_sha, recorded_at=excluded.recorded_at",
            (
                chat_repository,
                int(chat_number),
                target_repo.casefold(),
                int(target_number),
                head_sha,
                datetime.now(UTC).isoformat(),
            ),
        )

    def chat_review_heads(self, chat_repository: str) -> dict[int, dict[tuple[str, int], str]]:
        """Every recorded chat in this chat repository: chat number to each pull request's
        commit, the pull request keyed by its casefolded repository and number (#2637).

        Read whole rather than one chat at a time: a session page draws a handful of
        chips on every stream tick, and this only ever holds a few rows per review chat
        this deck opened.
        """
        rows = self._query(
            "SELECT chat_number, target_repo, target_number, head_sha FROM chat_review_heads"
            " WHERE chat_repository = ? COLLATE NOCASE",
            (chat_repository,),
        )
        found: dict[int, dict[tuple[str, int], str]] = {}
        for row in rows:
            if row[3]:
                pull = (str(row[1]).casefold(), int(row[2]))
                found.setdefault(int(row[0]), {})[pull] = row[3]
        return found

    # --- notes a chat shows and its agent never sees (#3227) ---

    _CHAT_NOTE_COLUMNS = (
        "repository, comment_id, chat_id, number, is_pull, author, url, body, body_html,"
        " created_at, updated_at"
    )

    def save_chat_note(self, note: ChatNote) -> bool:
        """Keep ``note``, unless a newer edit of the same comment is already kept.

        True when the row changed. An edit clears the rendered HTML, which belongs to the
        old text; the newest delivery wins whatever order they arrive in.
        """
        before = self._query(
            "SELECT updated_at, body FROM chat_notes WHERE repository = ? AND comment_id = ?",
            (note.repository.casefold(), int(note.comment_id)),
        )
        if before and (before[0][0] > note.updated_at or tuple(before[0]) == (
            note.updated_at,
            note.body,
        )):
            return False
        return self._write(
            f"INSERT INTO chat_notes({self._CHAT_NOTE_COLUMNS})"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)"
            " ON CONFLICT(repository, comment_id) DO UPDATE SET"
            " body=excluded.body, body_html=NULL, updated_at=excluded.updated_at",
            (
                note.repository.casefold(),
                int(note.comment_id),
                note.chat_id,
                int(note.number),
                int(bool(note.is_pull)),
                note.author,
                note.url,
                note.body,
                note.created_at,
                note.updated_at,
            ),
        )

    def set_chat_note_html(
        self, repository: str, comment_id: int, updated_at: str, body_html: str
    ) -> bool:
        """Store GitHub's rendering of the comment's ``updated_at`` version, only if that is
        still the version kept."""
        return self._write(
            "UPDATE chat_notes SET body_html = ?"
            " WHERE repository = ? AND comment_id = ? AND updated_at = ?",
            (body_html, repository.casefold(), int(comment_id), updated_at),
        )

    def delete_chat_note(self, repository: str, comment_id: int) -> bool:
        return self._write(
            "DELETE FROM chat_notes WHERE repository = ? AND comment_id = ?",
            (repository.casefold(), int(comment_id)),
        )

    def record_deck_chat_start(self, token: str, kind: str) -> bool:
        """Remember a deck chat start (#3403); ``kind`` is ``first`` or ``reset``."""
        return self._write(
            "INSERT OR IGNORE INTO deck_chat_starts(token, kind, created_at) VALUES (?, ?, ?)",
            (token, kind, datetime.now(UTC).isoformat()),
        )

    def deck_chat_starts(self) -> list[tuple[str, str, datetime]]:
        """Every deck chat start as ``(token, kind, created_at)``, oldest first."""
        return [
            (str(token), str(kind), datetime.fromisoformat(str(created_at)))
            for token, kind, created_at in self._query(
                "SELECT token, kind, created_at FROM deck_chat_starts ORDER BY created_at, token"
            )
        ]

    def chat_notes(self, chat_id: str, limit: int = 200) -> list[ChatNote]:
        """The chat's newest ``limit`` notes, oldest first."""
        rows = self._query(
            f"SELECT {self._CHAT_NOTE_COLUMNS} FROM chat_notes WHERE chat_id = ?"
            " ORDER BY created_at DESC, comment_id DESC LIMIT ?",
            (chat_id, int(limit)),
        )
        return [
            ChatNote(
                str(row[0]), int(row[1]), str(row[2]), int(row[3]), bool(row[4]),
                str(row[5]), str(row[6]), str(row[7]), row[8], str(row[9]), str(row[10]),
            )
            for row in reversed(rows)
        ]

    def chat_note_html_mentions(self, text: str) -> bool:
        """Does any kept note's rendering contain ``text``? The image route serves only
        what a note shows."""
        return bool(
            self._query(
                "SELECT 1 FROM chat_notes WHERE instr(body_html, ?) > 0 LIMIT 1", (text,)
            )
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _poller_review_verdict(row) -> PollerReviewVerdict:
    return PollerReviewVerdict(
        repo=row[0],
        sha=row[1],
        pull_request=row[2] or "",
        issue_repo=row[3] or "",
        issue_number=int(row[4] or 0),
        verdict=row[5],
        result=row[6],
        auto=bool(row[7]),
        generation=row[8] or "",
        reviewed_at=_parse_dt(row[9]),
        recorded_at=_parse_dt(row[10]) or datetime.now(UTC),
    )


def _status_point(row: Sequence[Any]) -> WorkStatusPoint | None:
    """One ``work_status_log`` row back as a ``StatusPoint``; None when it cannot be read."""
    from .work_status_log import StatusPoint

    (at, work_id, section, category, kind, tone, headline, prev_section, prev_category,
     prev_kind, prev_tone, mark, why, degraded, pid, revision) = row
    try:
        when = datetime.fromisoformat(str(at))
        facts = json.loads(why) if why else {}
        failed = json.loads(degraded) if degraded else []
    except ValueError:
        return None
    return StatusPoint(
        work_id=str(work_id),
        at=when,
        section=str(section),
        category=str(category),
        kind=str(kind),
        tone=str(tone),
        headline=str(headline),
        prev_section=str(prev_section),
        prev_category=str(prev_category),
        prev_kind=str(prev_kind),
        prev_tone=str(prev_tone),
        facts_mark=str(mark) if mark is not None else None,
        why=facts if isinstance(facts, dict) else {},
        degraded=tuple(str(item) for item in failed) if isinstance(failed, list) else (),
        pid=int(pid) if pid is not None else None,
        revision=str(revision) if revision is not None else None,
    )


def open_db(
    db_path: str, retention_days: int, context_retention_days: int = 30
) -> Db:
    """Open the required store shared by the web process and the runtime."""
    try:
        return Db(Path(db_path).expanduser(), retention_days, context_retention_days)
    except sqlite3.Error as exc:
        raise RuntimeError("control-plane storage unavailable") from exc
