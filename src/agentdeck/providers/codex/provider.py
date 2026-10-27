"""CodexProvider — translates a CODEX_HOME into agentdeck sessions."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from watchfiles import Change

from ... import images as images_mod
from ... import kanban as kanban_mod
from ... import live_window, runtime_feed
from ... import models as models_mod
from ...attachments import display_message
from ...models import (
    STALL_S,
    Account,
    Capability,
    ForkFrom,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    SubagentProgress,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
    detailed_activity_label,
    event_progress_at,
    event_turn_open,
    runtime_control_capabilities,
    runtime_turn_state,
    turn_stalled,
)
from .. import instructions as instructions_mod
from .. import transcript_reader as transcript_reader_mod
from .._filecache import FileCache, SharedFileCache, size_mtime_sig
from .._tailcache import TranscriptTailCache
from ..base import (
    ContextObservation,
    EffortChoice,
    ModelChoice,
    NewSessionDefaults,
    SessionProvider,
    log_runtime_account_split,
    recency_deadline,
)
from . import transcripts as transcripts_mod
from .inject import (
    inject_session,
    is_injectable_rollout,
)
from .inject import (
    start_session as start_codex_session,
)
from .runtime_client import CodexRuntimeClient
from .usage import CodexUsagePoller, fetch_usage_once

if TYPE_CHECKING:
    from ...state import AppState

DETAIL_WINDOW = 400
MAX_SESSIONS = 200
LIVE_WINDOW_S = 30.0
# How often the runtime's state is read when it cannot say it moved: a runtime from
# before its change feed, or none reachable (#2865).
RUNTIME_FALLBACK_S = 1.0
SUBAGENT_QUIET_S = 300.0
SUBAGENT_STALL_S = STALL_S
RECENT_SUBAGENT_S = 30 * 60.0
MAX_SUBAGENTS_SHOWN = 4
# Sized for one account's whole rollout history rather than for its live
# sessions: the caches these bound are read once per historical file per scan,
# so a cap below the file count evicts every entry before it is ever reused.
# The records are a TranscriptMeta and a set of session keys, so
# thousands of them are cheap; the byte cap is the real backstop (#1335).
_HISTORY_CACHE_ENTRIES = 16384
_HISTORY_CACHE_BYTES = 64 * 1024 * 1024
# ``_CorpusMeta`` is four small fields, so the same entry count costs about a
# megabyte. Kept generous rather than tight: this cap must never be the reason
# a whole-corpus walk loses an entry it filled earlier in the same walk.
_CORPUS_CACHE_BYTES = 16 * 1024 * 1024
# The full metadata record is asked for every listed rollout plus every owned
# chat's own rollout, and the deck owns more chats each day. At 1,852 owned
# chats that set came to ~68 MB against a 64 MiB cap, so every scan evicted what
# it had just parsed and re-read ~3,200 transcripts: one web core busy all the
# time (#2652). Sized well above today's set; a scan warns before it outgrows it.
_META_CACHE_BYTES = 192 * 1024 * 1024
_META_CACHE_WARN_SHARE = 0.8


@dataclass(frozen=True, slots=True)
class _RuntimeScanRead:
    meta: transcripts_mod.TranscriptMeta
    last_event: TranscriptEvent | None


@dataclass(frozen=True, slots=True)
class _ChangedScanRead(_RuntimeScanRead):
    delegated_session_keys: frozenset[str]

log = logging.getLogger(__name__)


def _display_kind(kind: str | None) -> str | None:
    """Return only source kinds that add useful information to a card."""
    return kind if kind == "exec" else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _mtime_epoch(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _all_rollouts(root: Path) -> list[Path]:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return []
    return list(sessions.glob("*/*/*/rollout-*.jsonl"))


def _named_rollouts(root: Path, session_ids: frozenset[str]) -> list[Path]:
    """The rollouts named for these session ids: a rollout's file name ends in its id."""
    sessions = root / "sessions"
    if not sessions.is_dir():
        return []
    return [
        path
        for session_id in sorted(session_ids)
        if session_id and "/" not in session_id
        for path in sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl")
    ]


def _newest_rollouts(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:MAX_SESSIONS]]


def _list_rollouts(root: Path) -> list[Path]:
    return _newest_rollouts(_all_rollouts(root))


def _compact_line(value: str | None, limit: int = 220) -> str | None:
    if not value:
        return None
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    return line[:limit] or None


class _RolloutRole(Protocol):
    """The identity fields that say what a rollout *is*, without its content.

    Both ``TranscriptMeta`` and ``_CorpusMeta`` satisfy this, so the two helper
    predicates below cannot answer differently depending on which one a caller
    happens to be holding.
    """

    session_id: str
    is_approval_review: bool
    is_subagent: bool
    is_spawned_subagent: bool


@dataclass(frozen=True, slots=True)
class _CorpusMeta:
    """What the whole-corpus scan passes need from a rollout, and nothing else.

    Two loops in a full scan — the owned-thread rescue and the delegation map —
    read *every* rollout the account ever
    wrote, while ``MAX_SESSIONS`` bounds only the chats the scan reports. They
    used to ask ``_cached_meta`` for that, but a ``TranscriptMeta`` carries the
    text a session displays and pickles to a median 8 KB, so this host's 4,195
    rollouts came to ~60 MB against ``_HISTORY_CACHE_BYTES``. The set evicted
    itself part-way through the very walk that filled it and every scan paid the
    disk tier again: 12.2 s of metadata alone, warm, per scan (#1489).

    This projection is ~150 bytes, so a whole account's history stays resident
    in well under a megabyte and the walk becomes a stat and a dict hit. It is
    derived from the same parse ``_cached_meta`` uses, so no value here can
    drift from the full record.
    """

    session_id: str
    is_approval_review: bool
    is_subagent: bool
    is_spawned_subagent: bool

    @classmethod
    def of(cls, meta: transcripts_mod.TranscriptMeta) -> _CorpusMeta:
        return cls(
            session_id=meta.session_id,
            is_approval_review=meta.is_approval_review,
            is_subagent=meta.is_subagent,
            is_spawned_subagent=meta.is_spawned_subagent,
        )


def _is_helper_rollout(meta: _RolloutRole) -> bool:
    """A rollout that exists to serve a chat rather than to be one.

    Spawned agents and guardian/auto-review runs both reuse the parent chat's
    ``session_id``, so every caller that keys on a session id has to reject them
    first or the newest helper file shadows the real conversation. The two
    signals differ in strength: ``is_subagent`` is structural, ``is_approval_review``
    is a literal match on Codex's reviewer prompt, kept because it predates the
    structural markers.
    """
    return meta.is_approval_review or meta.is_subagent


class CodexProvider(SessionProvider):
    """Read Codex CLI rollouts.

    Codex exposes no process-to-session PID registry. LIVE therefore means the
    rollout was written within ``LIVE_WINDOW_S``; a quiet process waiting for
    input may be reported IDLE, and a just-finished process briefly remains LIVE.
    """

    provider_id = "codex"
    supports_new_session = True
    # Historical CLI behaviour: account-free machine delegation means Codex
    # unless the caller explicitly chooses another provider.
    machine_delegation_default = True
    # Chosen at thread/start (new chat only); the app-server turn protocol has no
    # model field, so a running thread keeps its creation model.
    selectable_models = (
        ModelChoice("gpt-5.6-luna", "Luna"),
        ModelChoice("gpt-5.6-terra", "Terra"),
        ModelChoice("gpt-5.6-sol", "Sol"),
        ModelChoice("gpt-6-astra", "Astra"),
    )
    selectable_efforts = tuple(
        EffortChoice(value, label)
        for value, label in (
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("xhigh", "Extra high"),
            ("max", "Max"),
            ("ultra", "Ultra"),
        )
    )
    _default_model = "gpt-5.6-sol"
    _model_default_efforts = {
        # Astra is listed but is not `_default_model`: the account's own
        # config.toml still wins, and which model the deck falls back to is a
        # spend decision, not a support one (#1913).
        "gpt-6-astra": "medium",
        "gpt-5.6-sol": "medium",
        "gpt-5.6-terra": "medium",
        "gpt-5.6-luna": "medium",
    }

    def new_session_defaults(
        self, account: Account, *, model_override: str | None = None
    ) -> NewSessionDefaults:
        """Read the same account-level Codex config inherited by app-server."""
        configured: dict = {}
        try:
            with (account.root / "config.toml").open("rb") as config_file:
                loaded = tomllib.load(config_file)
                if isinstance(loaded, dict):
                    configured = loaded
        except (OSError, tomllib.TOMLDecodeError):
            pass
        configured_model = configured.get("model")
        model = (
            model_override
            or (configured_model if isinstance(configured_model, str) else None)
            or self._default_model
        )
        configured_effort = configured.get("model_reasoning_effort")
        effort = (
            configured_effort
            if isinstance(configured_effort, str)
            else self._model_default_efforts.get(model)
        )
        return NewSessionDefaults(model=model, effort=effort)

    def __init__(self) -> None:
        cache_version_paths = (
            Path(transcripts_mod.__file__),
            Path(transcript_reader_mod.__file__),
            Path(instructions_mod.__file__),
            Path(images_mod.__file__),
            Path(models_mod.__file__),
        )
        # A scan folds delegation parentage out of
        # *every* rollout the account ever wrote, not just the listed ones, so
        # these three answers are asked ~3.5k times per scan on this host. The
        # files are immutable once written, but a process-local memo threw the
        # work away on every worker handoff, and re-parsing 553k JSONL lines is
        # what pushed cold readiness past the supervisor's 90s gate and stopped
        # the deck deploying (#1335). Sharing them on disk means a replacement
        # worker inherits the parse instead of repeating it. The caps are sized
        # to hold a whole account's history: an LRU that evicts during the walk
        # that fills it saves nothing.
        # Meta carries the last text a session shows, so it invalidates on both
        # size and mtime. A same-mtime append must be visible to an authoritative
        # scan even after restart, without depending on watcher delivery (#1368).
        self._meta_cache: SharedFileCache[transcripts_mod.TranscriptMeta] = (
            SharedFileCache(
                size_mtime_sig,
                "codex-transcript-meta",
                version_paths=cache_version_paths,
                local_entries=_HISTORY_CACHE_ENTRIES,
                local_bytes=_META_CACHE_BYTES,
                max_entries=_HISTORY_CACHE_ENTRIES,
                max_bytes=_META_CACHE_BYTES,
            )
        )
        self._meta_cache_warned = False
        self._meta_working_bytes: dict[str, int] = {}
        # The whole-corpus passes read this instead of the full record above.
        # Its own caps exist because the point is that a whole account's history
        # fits: 4,195 of these are under a megabyte, so the byte cap can never
        # be what evicts an entry the same walk is about to want (#1489).
        self._corpus_cache: SharedFileCache[_CorpusMeta] = SharedFileCache(
            size_mtime_sig,
            "codex-rollout-role",
            version_paths=cache_version_paths,
            local_entries=_HISTORY_CACHE_ENTRIES,
            local_bytes=_CORPUS_CACHE_BYTES,
            max_entries=_HISTORY_CACHE_ENTRIES,
            max_bytes=_CORPUS_CACHE_BYTES,
        )
        self._transcript_cache: SharedFileCache[list[TranscriptEvent]] = SharedFileCache(
            size_mtime_sig,
            "codex-render-events",
            version_paths=cache_version_paths,
        )
        self._billable_paths: dict[str, list[Path]] = {}
        self._liveness_inputs: dict[
            tuple[str, str], tuple[object, bool, bool, str | None]
        ] = {}
        # A Codex worker never writes itself a title, and its first prompt is the
        # same dispatch boilerplate as every other worker's, so the card has to be
        # named from the issue the prompt names (#310). Same host-level cache the
        # Claude provider reads, since it is the same question about the same issue.
        self._kanban = kanban_mod.KanbanTitleCache()
        self._last_ev_cache: FileCache[TranscriptEvent | None] = FileCache(
            size_mtime_sig, max_entries=_HISTORY_CACHE_ENTRIES
        )
        self._delegation_cache: SharedFileCache[frozenset[str]] = SharedFileCache(
            size_mtime_sig,
            "codex-delegated-session-keys",
            version_paths=cache_version_paths,
            local_entries=_HISTORY_CACHE_ENTRIES,
            local_bytes=_HISTORY_CACHE_BYTES,
            max_entries=_HISTORY_CACHE_ENTRIES,
            max_bytes=_HISTORY_CACHE_BYTES,
        )
        # The parsed detail-window tail, resumed from the reader's byte cursor
        # so opening a large chat doesn't reparse the whole JSONL (#791).
        self._tail_cache = TranscriptTailCache(
            transcripts_mod.read_events, window_size=DETAIL_WINDOW
        )
        # Keyed on (account root, session id), not the account label: the
        # provider is a process-wide singleton and two accounts can share a
        # label, which bled cached paths across roots on Claude (#737, #790).
        self._paths: dict[tuple[str, str], Path] = {}
        self._clients: dict[str, CodexRuntimeClient] = {}
        self._states = {}
        self._runtime_tasks: dict[str, asyncio.Task] = {}
        self._subagent_names: dict[str, str] = {}
        # Track per-account runtime-refresh health so a persistent outage warns
        # once (on the healthy->failing edge) instead of every scan interval.
        self._refresh_ok: dict[str, bool] = {}

    async def start_account(self, account: Account, state) -> None:
        client = CodexRuntimeClient(
            account,
            on_change=lambda thread_id: self._runtime_changed(account, state, thread_id),
        )
        self._clients[account.key] = client
        self._states[account.key] = state
        await client.start()
        self._refresh_ok[account.key] = True
        self._runtime_tasks[account.key] = asyncio.create_task(
            self._watch_runtime(account, state, client),
            name=f"codex-runtime:{account.key}",
        )

    async def _watch_runtime(self, account, state, client: CodexRuntimeClient) -> None:
        """Re-read the account's runtime state when the runtime says it moved (#2865).

        A runtime from before the change feed, or one that cannot be reached, is read
        on the old one-second cadence instead.
        """
        item = runtime_feed.workers_item(account.provider_id, account.label)
        with runtime_feed.shared().subscribe(item) as changes:
            failed = False
            while True:
                # A failed read is tried again on the fallback cadence (#2865).
                await changes.wait(RUNTIME_FALLBACK_S, retry=failed)
                try:
                    await client.refresh()
                except Exception as exc:  # noqa: BLE001 -- retried on the fallback cadence
                    failed = True
                    self._refresh_failed(account, state, client, exc)
                else:
                    failed = False
                    was_ok = self._refresh_ok.get(account.key, True)
                    self._refresh_ok[account.key] = True
                    if not was_ok:
                        self._reproject_runtime_sessions(account, state, client)

    def _refresh_failed(self, account, state, client, exc: Exception) -> None:
        was_ok = self._refresh_ok.get(account.key, True)
        self._refresh_ok[account.key] = False
        if was_ok:
            # Set the flag first so the reprojection withdraws controls,
            # then let AppState publish once if any session changed.
            self._reproject_runtime_sessions(account, state, client)
            log_runtime_account_split(log, "Codex", account.key, exc)
        log.debug("Codex runtime refresh failed for %s: %s", account.key, exc)

    def _reproject_runtime_sessions(
        self, account: Account, state, client: CodexRuntimeClient
    ) -> list[Session]:
        targets = [
            session
            for session in state.sessions.values()
            if session.account_key == account.key
            and session.parent_session_key is None
            and (client.owns(session.session_id) or session.kind == "appServer")
        ]
        return state.apply_session_changes(
            targets, lambda session: self._project_runtime_session(account, session)
        )

    async def stop_account(self, account: Account) -> None:
        task = self._runtime_tasks.pop(account.key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = self._clients.pop(account.key, None)
        self._states.pop(account.key, None)
        self._refresh_ok.pop(account.key, None)
        if client is not None:
            await client.stop()

    def _runtime_changed(self, account: Account, state, thread_id: str) -> None:
        session = state.sessions.get(f"{account.key}:{thread_id}")
        targets = [session] if session is not None else []
        state.apply_session_changes(
            targets, lambda session: self._project_runtime_session(account, session)
        )
        # A replaced interaction can leave the session as it was; the chat's open page
        # reads it directly and waits on its chat's topic (#2865).
        wake = getattr(state, "wake_chat_pages", None)
        if session is not None and wake is not None:
            wake((session.key,))

    def _project_runtime_session(
        self,
        account: Account,
        session: Session,
        path: Path | None = None,
        scan_read: _RuntimeScanRead | None = None,
    ) -> bool:
        """Project transcript baseline plus any owned runtime overlay."""
        path = path or self._transcript_path(account, session)
        if path is not None:
            last_activity = _mtime(path)
            meta = scan_read.meta if scan_read is not None else self._cached_meta(path)
            lifecycle_active = (
                meta.task_active if meta.task_status is not None else None
            )
            status, thinking, activity, stalled, last_progress = (
                self._derived_state_from_event(
                    scan_read.last_event,
                    last_activity,
                    lifecycle_active=lifecycle_active,
                    turn_started_at=meta.task_started_at,
                )
                if scan_read is not None and last_activity is not None
                else self._derived_state(
                    path,
                    last_activity,
                    lifecycle_active=lifecycle_active,
                    turn_started_at=meta.task_started_at,
                )
            )
            session.status = status
            session.thinking = thinking
            session.stalled = stalled
            session.lifecycle_active = lifecycle_active
            session.activity = activity
            session.last_activity = last_activity
            session.last_progress = last_progress
            session.last_prompt = meta.last_prompt
            session.last_text = meta.last_text
            session.last_role = meta.last_role
            session.last_message_at = meta.last_message_at
            session.model = meta.model
            session.tokens = meta.tokens
            session.context_tokens = meta.context_tokens
            session.capabilities = self._capabilities(
                account, session.session_id, path, meta, status
            )
            session.kind = _display_kind(meta.kind)
            last_event = (
                scan_read.last_event
                if scan_read is not None
                else self._cached_last_event(path)
            )
        else:
            session.status = SessionStatus.IDLE
            session.thinking = False
            session.stalled = False
            session.lifecycle_active = None
            session.activity = None
            session.capabilities = frozenset()
            session.kind = None
            last_event = None
        session.last_event_seq = last_event.seq if last_event is not None else 0
        session.question = None
        client = self._clients.get(account.key)
        if client is None or not client.owns(session.session_id):
            return False
        active = client.active_turn(session.session_id) is not None
        interaction = self._actionable_interaction(account, session.session_id)
        progress = event_progress_at(
            last_event,
            session.last_progress or session.last_activity or session.started_at,
        )
        if active and progress is None:
            # First runtime observation before a transcript exists: establish a
            # durable local progress anchor once, then retain it across sweeps.
            progress = datetime.now(UTC)
        age = (datetime.now(UTC) - progress).total_seconds() if progress else 1e9
        stalled = turn_stalled(
            live=True,
            lifecycle_active=active,
            last_ev=last_event,
            age_s=age,
        )
        session.status = SessionStatus.LIVE if active else SessionStatus.IDLE
        session.lifecycle_active = active
        session.stalled, session.thinking = runtime_turn_state(
            active_turn=active,
            actionable_interaction=interaction is not None,
            stalled_evidence=stalled,
        )
        session.last_progress = progress
        session.activity = (
            "Waiting for you"
            if interaction
            else detailed_activity_label(
                activity_label(
                    active,
                    active,
                    last_event,
                    age,
                    lifecycle_active=active,
                ),
                last_event,
            )
        )
        session.question = self._interaction_summary(interaction)
        session.kind = "appServer"
        session.capabilities = self._runtime_capabilities(account, session.session_id)
        return True

    @staticmethod
    def _interaction_summary(interaction: PendingInteraction | None) -> str | None:
        if interaction is None:
            return None
        if interaction.questions:
            return " ".join(question.prompt for question in interaction.questions)
        return interaction.message or interaction.title

    def _actionable_interaction(
        self, account: Account, thread_id: str
    ) -> PendingInteraction | None:
        client = self._clients.get(account.key)
        if (
            client is None
            or not self._refresh_ok.get(account.key, True)
            or not client.owns(thread_id)
        ):
            return None
        return client.interaction(thread_id)

    def interactions_unknown(self, account: Account) -> bool:
        return account.key in self._clients and not self._refresh_ok.get(account.key, True)

    def runtime_unreachable(self, account: Account, session: Session) -> bool:
        # A failed refresh keeps the last thread map, so it still names this
        # chat while the runtime restarts (#2847).
        client = self._clients.get(account.key)
        return bool(
            client is not None
            and not self._refresh_ok.get(account.key, True)
            and client.owns(session.session_id)
        )

    def _runtime_capabilities(self, account: Account, thread_id: str) -> frozenset[Capability]:
        client = self._clients.get(account.key)
        available = (
            client is not None
            and self._refresh_ok.get(account.key, True)
            and client.owns(thread_id)
        )
        capabilities = frozenset({Capability.TRANSCRIPT}) | runtime_control_capabilities(
            available=available,
            active_turn=available and client.active_turn(thread_id) is not None,
            actionable_interaction=self._actionable_interaction(account, thread_id)
            is not None,
        )
        if available:
            capabilities |= {Capability.EFFORT}
            if client.supports(models_mod.SNAPSHOT_RELOCATE_V1):
                # The app-server resumes this thread in its repository once its own
                # directory is gone (#2524).
                capabilities |= {Capability.RESUME_ELSEWHERE}
            if (
                client.supports(models_mod.SNAPSHOT_CONTEXT_MANAGEMENT_V1)
                and client.worker_key(thread_id) is not None
            ):
                capabilities |= {
                    Capability.CONTEXT_COMPACT,
                    Capability.CONTEXT_ROLLOVER,
                }
            if client.permission_prompts_available(thread_id):
                capabilities |= {Capability.PERMISSION_PROMPTS}
        return capabilities

    def _cached_meta(self, path: Path) -> transcripts_mod.TranscriptMeta:
        return self._meta_cache.get(
            path, transcripts_mod.transcript_meta, transcripts_mod.TranscriptMeta()
        )

    def _corpus_meta(self, path: Path) -> _CorpusMeta:
        """What one rollout *is*, cheap enough to remember for the whole corpus.

        Computed through ``_cached_meta`` rather than from a second parser, so a
        rollout's role is always exactly what the full record would have said.
        A cold miss therefore costs one parse either way; what changes is that
        the answer survives, because the projection is small enough to keep.
        """
        return self._corpus_cache.get(
            path,
            lambda item: _CorpusMeta.of(self._cached_meta(item)),
            _CorpusMeta.of(transcripts_mod.TranscriptMeta()),
        )

    def _cached_last_event(self, path: Path) -> TranscriptEvent | None:
        return self._last_ev_cache.get(path, transcripts_mod.last_event, None)

    def _scan_reads(
        self,
        account: Account,
        owned_ids: set[str],
        extra_keys: frozenset[str] = frozenset(),
        only: frozenset[str] | None = None,
    ) -> tuple[
        list[Path],
        list[Path],
        list[tuple[Path, transcripts_mod.TranscriptMeta]],
        dict[Path, _RuntimeScanRead],
        set[str],
        dict[str, str],
    ]:
        """Every disk and JSON read a full scan needs, in one thread hop.

        Listing the rollout
        tree, parsing each rollout's metadata and its delegation markers, and
        reading each listed rollout's last event all ran inline on the loop —
        thousands of rollouts of transcript JSON per scan, and the reason the
        deck answered nothing while a scan was in flight (#1487). The build loop
        that follows asks for exactly these values again and now gets cache
        hits.

        The delegation map is built here rather than in the build loop for a
        second reason: the memo is byte-capped, so a walk over a multi-gigabyte
        account evicts a rollout parsed early in the very same walk. Reading the
        marker and its parent's metadata together, while both are still in hand,
        is what keeps the answer out of the loop thread rather than merely
        usually out of it.
        """
        if only is not None:
            # One chat's own rollout by its name, not the account's whole listing.
            all_rollout_paths = _named_rollouts(account.root, only)
            rollout_paths = list(all_rollout_paths)
        else:
            all_rollout_paths = _all_rollouts(account.root)
            rollout_paths = _newest_rollouts(all_rollout_paths)
        # The live window (#2841): an owned chat older than the window is no longer
        # rescued unless it is in a Work still going or was just opened, and an older
        # chat someone opened is rescued even when this deck does not own it.
        # ``extra_keys`` was read on the event loop before this thread hop: the window's
        # opened-chats map is written there, and this runs in a worker thread.
        window = live_window.current()
        prefix = f"{account.key}:"
        extra_ids = {key.removeprefix(prefix) for key in extra_keys if key.startswith(prefix)}
        if owned_ids or extra_ids:
            included = set(rollout_paths)
            for path in all_rollout_paths:
                if path in included:
                    continue
                corpus = self._corpus_meta(path)
                # Only the chat's own rollout. Its guardians and spawned agents
                # share its session_id and are never listed this old. Rescuing
                # them too nearly doubled the full records a scan holds (#2652).
                if _is_helper_rollout(corpus) or not corpus.session_id:
                    continue
                session_id = corpus.session_id
                if session_id in extra_ids or (
                    session_id in owned_ids
                    and (
                        window is None
                        or window.keeps(prefix + session_id, _mtime_epoch(path))
                    )
                ):
                    rollout_paths.append(path)
                    included.add(path)
        rollouts = [(path, self._cached_meta(path)) for path in rollout_paths]
        self._warn_if_meta_cache_outgrown(account, rollout_paths)
        # A parent chat that delegated a Codex session via AgentDeck records a
        # "delegation: running (/sessions/<child-key>)" marker in its own rollout.
        # Collect both the flat set (for is_delegated) and the child->parent map
        # so delegated sessions nest under the chat that spawned them.
        delegated_session_keys: set[str] = set()
        delegation_parent: dict[str, str] = {}
        for path in all_rollout_paths:
            children = self._cached_delegated_session_keys(path)
            if not children:
                continue
            delegated_session_keys.update(children)
            parent_meta = self._corpus_meta(path)
            if parent_meta.is_subagent or not parent_meta.session_id:
                continue
            parent_key = f"{account.key}:{parent_meta.session_id}"
            for child_key in children:
                if child_key != parent_key:
                    delegation_parent.setdefault(child_key, parent_key)
        runtime_reads = {
            path: _RuntimeScanRead(meta, self._cached_last_event(path))
            for path, meta in rollouts
        }
        return (
            all_rollout_paths,
            rollout_paths,
            rollouts,
            runtime_reads,
            delegated_session_keys,
            delegation_parent,
        )

    def _warn_if_meta_cache_outgrown(self, account: Account, paths: list[Path]) -> None:
        """Say once when the scans' records near the metadata cache's cap.

        Past the cap the cache evicts what the same scan just parsed, and the
        only symptom is a web worker that never stops parsing (#2652). Every
        Codex account shares the one cache, so their latest sets are summed.
        """
        self._meta_working_bytes[account.key] = self._meta_cache.record_bytes(paths)
        if self._meta_cache_warned:
            return
        needed = sum(self._meta_working_bytes.values())
        limit = self._meta_cache.byte_limit
        if needed > limit * _META_CACHE_WARN_SHARE:
            self._meta_cache_warned = True
            log.warning(
                "codex metadata cache is %.0f%% full from the latest scans "
                "(%d bytes of %d); raise _META_CACHE_BYTES before it evicts "
                "its own working set",
                100 * needed / limit,
                needed,
                limit,
            )

    def _cached_delegated_session_keys(self, path: Path) -> frozenset[str]:
        return self._delegation_cache.get(
            path, transcripts_mod.delegated_session_keys, frozenset()
        )

    def fork_refusal(self, account: Account, session: Session) -> str | None:
        client = self._clients.get(account.key)
        if client is None or not self._refresh_ok.get(account.key, True):
            return "The Codex runtime is not reachable right now."
        if not client.supports(models_mod.SNAPSHOT_FORK_V1):
            return "The deck's runtime is too old to make side chats; it needs a restart."
        if session.cwd is None or not session.cwd.is_dir():
            return "This chat's working directory no longer exists, so a side chat cannot start."
        if self._transcript_path(account, session) is None:
            return "This chat's history is not on this host."
        return None

    def fork_ref(self, account: Account, session: Session, seq: int) -> str | None:
        path = self._transcript_path(account, session)
        return transcripts_mod.fork_turn(path, seq) if path is not None else None

    def _transcript_path(self, account: Account, session: Session) -> Path | None:
        path = self._paths.get((str(account.root), session.session_id))
        if path is not None and path.is_file():
            meta = self._cached_meta(path)
            if not _is_helper_rollout(meta):
                return path
        for candidate in _list_rollouts(account.root):
            meta = self._cached_meta(candidate)
            if meta.session_id == session.session_id and not _is_helper_rollout(meta):
                self._paths[(str(account.root), session.session_id)] = candidate
                return candidate
        return None

    def _capabilities(
        self, account: Account, session_id: str, path: Path, meta, status: SessionStatus
    ) -> frozenset[Capability]:
        client = self._clients.get(account.key)
        if client is not None and client.owns(session_id):
            return self._runtime_capabilities(account, session_id)
        capabilities = {Capability.TRANSCRIPT}
        cwd_exists = bool(meta.cwd and Path(meta.cwd).is_dir())
        if status == SessionStatus.IDLE and cwd_exists and is_injectable_rollout(path, meta.kind):
            capabilities.add(Capability.INJECT)
        return frozenset(capabilities)

    def _derived_state(
        self,
        path: Path,
        last_activity: datetime | None,
        *,
        lifecycle_active: bool | None = None,
        turn_started_at: datetime | None = None,
    ) -> tuple[SessionStatus, bool, str | None, bool, datetime | None]:
        if last_activity is None:
            return (SessionStatus.IDLE, False, None, False, None)
        return self._derived_state_from_event(
            self._cached_last_event(path),
            last_activity,
            lifecycle_active=lifecycle_active,
            turn_started_at=turn_started_at,
        )

    def _derived_state_from_event(
        self,
        last_event: TranscriptEvent | None,
        last_activity: datetime,
        *,
        lifecycle_active: bool | None = None,
        turn_started_at: datetime | None = None,
    ) -> tuple[SessionStatus, bool, str | None, bool, datetime | None]:
        """Derive card state from a transcript event read off the event loop."""
        write_age = max(0.0, (datetime.now(UTC) - last_activity).total_seconds())
        live = write_age < LIVE_WINDOW_S
        status = SessionStatus.LIVE if live else SessionStatus.IDLE
        progress = event_progress_at(last_event, last_activity)
        if lifecycle_active and turn_started_at is not None and (
            progress is None or progress < turn_started_at
        ):
            progress = turn_started_at
        age = max(0.0, (datetime.now(UTC) - progress).total_seconds()) if progress else 1e9
        # Unowned rollouts have no process lifecycle feed. Structural open-turn
        # evidence therefore sustains Working through quiet tool gaps.
        evidence_available = (
            live or lifecycle_active is True or event_turn_open(last_event) is True
        )
        stalled = turn_stalled(
            live=evidence_available,
            lifecycle_active=lifecycle_active,
            last_ev=last_event,
            age_s=age,
        )
        activity = detailed_activity_label(
            activity_label(
                evidence_available,
                live,
                last_event,
                age,
                lifecycle_active=lifecycle_active,
            ),
            last_event,
        )
        return (status, activity is not None, activity, stalled, progress)

    @staticmethod
    def _apply_model(events: list[TranscriptEvent], model: str | None) -> list[TranscriptEvent]:
        for event in events:
            if event.role == "assistant" and event.model is None:
                event.model = model
        return events

    def _decorate_events(
        self, events: list[TranscriptEvent], model: str | None
    ) -> list[TranscriptEvent]:
        self._apply_model(events, model)
        for event in events:
            for agent_id, nickname in event.subagent_identities:
                self._subagent_names[agent_id] = nickname
            if event.subagent_id and event.subagent_name is None:
                event.subagent_name = self._subagent_names.get(event.subagent_id)
        return events

    def watch_paths(self, account: Account) -> list[Path]:
        sessions = account.root / "sessions"
        return [sessions] if sessions.exists() else []

    def watch_roots(self, account: Account) -> list[Path]:
        return [account.root] if account.root.is_dir() else []

    def watch_root_change(self, account: Account, path: Path) -> bool:
        try:
            source = path.relative_to(account.root).parts[0]
        except (IndexError, ValueError):
            return False
        return source == "sessions"

    # ``scan_sessions(only=...)`` reads just the named chats (#2841).
    supports_targeted_scan = True

    async def scan_sessions(
        self, account: Account, *, only: frozenset[str] | None = None
    ) -> list[Session]:
        """Every chat this account lists, or with ``only`` just those session ids.

        ``only`` is the live window's one-chat load: it finds each named chat's own
        rollout by name instead of listing the account, and changes none of the
        account-wide records a full scan owns (billable paths, delegation links), only
        adding the chats' paths. The next full scan lists them properly.
        """
        client = self._clients.get(account.key)
        # A one-chat load refreshes nothing account-wide: a chat old enough to be let
        # go is not running, and the next full scan refreshes the runtime as usual.
        if client is not None and only is None:
            try:
                await client.refresh()
            except Exception as exc:  # noqa: BLE001 -- transcript scan remains useful
                # Warn only on the healthy->failing edge: a dead runtime client
                # freezes cached liveness (finished sessions stay "working"), so
                # it must be visible, but scan runs every few seconds and a real
                # outage would otherwise flood the log.
                if self._refresh_ok.get(account.key, True):
                    log.warning(
                        "Codex runtime unavailable during scan for %s: %s",
                        account.key,
                        exc,
                    )
                self._refresh_ok[account.key] = False
            else:
                if not self._refresh_ok.get(account.key, True):
                    log.info("Codex runtime scan recovered for %s", account.key)
                self._refresh_ok[account.key] = True
        sessions = []
        subagents: dict[str, list[SubagentProgress]] = {}
        turn_starts: dict[str, datetime] = {}
        current_paths: dict[tuple[str, str], Path] = {}
        seen: set[str] = set()
        owned_ids = (
            set(client.owned_thread_ids()) if client is not None and only is None else set()
        )
        (
            all_rollout_paths,
            rollout_paths,
            rollouts,
            runtime_reads,
            delegated_session_keys,
            delegation_parent,
        ) = await asyncio.to_thread(
            self._scan_reads,
            account,
            owned_ids,
            window.extra_keys() if (window := live_window.current()) is not None else frozenset(),
            only,
        )
        if only is None:
            self._billable_paths[account.key] = all_rollout_paths
        krefs = [
            ref
            for ref in (kanban_mod.parse_ref(meta.first_prompt) for _path, meta in rollouts)
            if ref is not None
        ]
        if krefs:
            try:
                await self._kanban.resolve_missing(krefs, datetime.now(UTC).timestamp())
            except Exception as exc:  # noqa: BLE001 — title polish must never break a scan
                log.debug("kanban title resolve failed: %s", exc)
        for path, meta in rollouts:
            if meta.is_spawned_subagent:
                if meta.agent_id and meta.agent_nickname:
                    self._subagent_names[meta.agent_id] = meta.agent_nickname
                last_activity = _mtime(path)
                age = (
                    (datetime.now(UTC) - last_activity).total_seconds()
                    if last_activity
                    else 1e9
                )
                if meta.session_id:
                    if meta.task_active or age < RECENT_SUBAGENT_S:
                        status = (
                            ("working" if age < SUBAGENT_QUIET_S else "quiet")
                            if meta.task_active
                            else (meta.task_status or "finished")
                        )
                        subagents.setdefault(meta.session_id, []).append(
                            SubagentProgress(
                                agent_id=meta.agent_id or path.stem[-36:],
                                nickname=meta.agent_nickname,
                                role=meta.agent_role,
                                task=meta.title,
                                status=status,
                                result=_compact_line(
                                    meta.last_agent_message or meta.last_text
                                ),
                                started_at=meta.started_at,
                                updated_at=last_activity,
                            )
                        )
                    # Also surface it as a real child session so it nests under
                    # the parent in the list tree. A spawned rollout reuses the
                    # parent's session_id (meta.session_id); its OWN id is
                    # meta.agent_id (session_meta.id).
                    child_id = meta.agent_id
                    recent_child = meta.task_active or age < RECENT_SUBAGENT_S
                    if child_id and child_id not in seen and recent_child:
                        seen.add(child_id)
                        # "Active" means it actually wrote recently — the
                        # task_active flag alone gets stuck True on a subagent
                        # that died mid-task, showing "Working" for hours and
                        # flooding the working count + Deckhand stalls.
                        child_active = meta.task_active and age < SUBAGENT_STALL_S
                        sessions.append(
                            Session(
                                key=f"{account.key}:{child_id}",
                                account_key=account.key,
                                session_id=child_id,
                                status=(
                                    SessionStatus.LIVE if child_active else SessionStatus.IDLE
                                ),
                                thinking=child_active,
                                activity="Working" if child_active else None,
                                title=meta.agent_nickname or meta.title,
                                last_text=meta.last_text,
                                last_role=meta.last_role,
                                last_message_at=meta.last_message_at,
                                cwd=Path(meta.cwd) if meta.cwd else None,
                                model=meta.model,
                                # ``kind="subagent"`` is what keeps this row out
                                # of Deckhand triage entirely — the parent chat
                                # owns its handoff.
                                kind="subagent",
                                worker_type="you",
                                # quiet/finished/stuck subagents stay nested under
                                # a visible parent but out of the working count;
                                # only a recently-active one is live work.
                                is_delegated=not child_active,
                                parent_session_key=f"{account.key}:{meta.session_id}",
                                started_at=meta.started_at,
                                last_activity=last_activity,
                                show_when_idle=True,
                            )
                        )
                        current_paths[(str(account.root), child_id)] = path
            # Reject every helper rollout before ID deduplication, or the newest
            # of them replaces the real chat it shares a session_id with.
            if _is_helper_rollout(meta):
                continue
            session_id = meta.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            if meta.task_started_at:
                turn_starts[session_id] = meta.task_started_at
            current_paths[(str(account.root), session_id)] = path
            last_activity = _mtime(path)
            lifecycle_active = (
                meta.task_active if meta.task_status is not None else None
            )
            status, thinking, activity, stalled, last_progress = (
                self._derived_state_from_event(
                    runtime_reads[path].last_event,
                    last_activity,
                    lifecycle_active=lifecycle_active,
                    turn_started_at=meta.task_started_at,
                )
                if last_activity is not None
                else (SessionStatus.IDLE, False, None, False, None)
            )
            kref = kanban_mod.parse_ref(meta.first_prompt)
            title = meta.title
            issue_url = issue_status = issue_status_kind = None
            if kref is not None:
                # The dispatch prompt makes a poor title: every worker's is the same
                # boilerplate, so the cards are indistinguishable. Prefer the resolved
                # issue title, then the bare "repo#n" reference, which still beats it.
                issue_url = kanban_mod.issue_url(kref)
                title = kanban_mod.format_title(kref, self._kanban.get(kref))
                badge = self._kanban.get_status(kref)
                if badge is not None:
                    issue_status, issue_status_kind = badge
            session = Session(
                key=f"{account.key}:{session_id}",
                account_key=account.key,
                session_id=session_id,
                status=status,
                thinking=thinking,
                stalled=stalled,
                lifecycle_active=lifecycle_active,
                activity=activity,
                cwd=Path(meta.cwd) if meta.cwd else None,
                title=title,
                issue_url=issue_url,
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
                initial_prompt=meta.first_prompt,
                last_prompt=meta.last_prompt,
                last_text=meta.last_text,
                last_role=meta.last_role,
                last_message_at=meta.last_message_at,
                model=meta.model,
                kind=_display_kind(meta.kind),
                # A card worked for an issue is a worker card, whichever provider runs
                # it: the list colour follows the work, not the vendor.
                worker_type="kanban" if kref is not None else "you",
                is_delegated=(
                    meta.kind == "exec"
                    or f"{account.key}:{session_id}" in delegated_session_keys
                ),
                # An AgentDeck-delegated session nests under the chat that
                # delegated it (same treatment as a spawned subagent).
                parent_session_key=delegation_parent.get(
                    f"{account.key}:{session_id}"
                ),
                started_at=meta.started_at,
                last_activity=last_activity,
                last_progress=last_progress,
                tokens=meta.tokens,
                context_tokens=meta.context_tokens,
                show_when_idle=True,
                capabilities=self._capabilities(account, session_id, path, meta, status),
            )
            self._project_runtime_session(account, session, path, runtime_reads[path])
            sessions.append(session)
        for session in sessions:
            agents = subagents.get(session.session_id, [])
            turn_started = turn_starts.get(session.session_id)
            if turn_started is not None:
                agents = [
                    agent
                    for agent in agents
                    if agent.started_at is None or agent.started_at >= turn_started
                ]
            agents.sort(
                key=lambda item: (
                    item.status not in ("working", "quiet"),
                    -(item.updated_at.timestamp() if item.updated_at else 0.0),
                )
            )
            session.subagents = tuple(agents[:MAX_SUBAGENTS_SHOWN])
            now = datetime.now(UTC)
            active_agent_progress = [
                stamp
                for agent in agents
                if agent.status in ("working", "quiet")
                and (stamp := agent.updated_at or agent.started_at) is not None
                and (now - stamp).total_seconds() < SUBAGENT_STALL_S
            ]
            session.subagent_count = len(active_agent_progress)
        if only is not None:
            self._paths.update(current_paths)
            return sessions
        self._paths = {
            key: value for key, value in self._paths.items() if key[0] != str(account.root)
        }
        self._paths.update(current_paths)
        # Publish this account's delegation links so cross-provider children
        # (e.g. a Codex chat that delegated a Claude session) nest correctly.
        state = self._states.get(account.key)
        if state is not None:
            state.set_delegation_parents(account.key, delegation_parent)
        return sessions

    async def scan_changed_sessions(
        self,
        account: Account,
        changes: frozenset[tuple[Change, Path]],
        state: AppState,
    ) -> list[Session] | None:
        """Refresh every safely attributable known rollout in a coalesced batch.

        This used to accept a batch of exactly one change. The watcher debounces
        for 500ms and the scan loop coalesces everything that arrives during its
        whole interval, so with more than one chat writing a batch is essentially
        never a single change and Codex fell back to a full scan on every wake
        (#1487). Size is not what makes a batch unsafe; an entry that could move
        account membership is, and that is the only thing that hands authority
        back now.
        """
        if not changes or self._billable_paths.get(account.key) is None:
            return None
        candidates: list[Path] = []
        for change, path in changes:
            try:
                relative = path.relative_to(account.root)
            except ValueError:
                continue  # outside the account tree; nothing to attribute
            if not relative.parts or relative.parts[0] != "sessions":
                continue  # not a rollout, so membership cannot have moved
            if change is not Change.modified:
                return None  # a rollout appeared or went away
            if (
                len(relative.parts) != 5
                or path.suffix != ".jsonl"
                or not path.is_file()
            ):
                return None
            candidates.append(path)
        if not candidates:
            # Every change was noise. Merging nothing is the whole point: the
            # slow reconciliation still runs on its own cadence.
            return []
        # The guards below read each rollout's metadata and delegation markers.
        # That is the same transcript JSON a full scan parses, so it belongs off
        # the event loop here too.
        changed_reads = await asyncio.to_thread(self._read_changed, candidates)
        # One pass over the account's sessions and delegation map for the whole
        # batch, rather than one per change.
        parent_keys = {
            candidate.parent_session_key
            for candidate in state.account_sessions(account.key)
        }
        delegation_keys = set(state.delegation_parents) | set(
            state.delegation_parents.values()
        )
        refreshable: list[tuple[Session, Path, _ChangedScanRead]] = []
        krefs = []
        for path in candidates:
            scan_read = changed_reads[path]
            meta = scan_read.meta
            session_id = meta.session_id
            if not session_id or _is_helper_rollout(meta):
                return None
            session = state.sessions.get(f"{account.key}:{session_id}")
            if (
                session is None
                or session.parent_session_key is not None
                or session.is_delegated
                or session.subagents
                or session.subagent_count
                or self._paths.get((str(account.root), session_id)) != path
                or scan_read.delegated_session_keys
                or meta.kind == "exec"
                or session.key in parent_keys
                or session.key in delegation_keys
            ):
                return None
            refreshable.append((session, path, scan_read))
            kref = kanban_mod.parse_ref(meta.first_prompt)
            if kref is not None:
                krefs.append(kref)
        if krefs:
            try:
                await self._kanban.resolve_missing(
                    krefs, datetime.now(UTC).timestamp()
                )
            except Exception as exc:  # noqa: BLE001 — title polish must never break a scan
                log.debug("kanban title resolve failed: %s", exc)
        return [
            self._refresh_known_rollout(account, session, path, scan_read)
            for session, path, scan_read in refreshable
        ]

    def _read_changed(self, paths: list[Path]) -> dict[Path, _ChangedScanRead]:
        """Read changed rollouts once, without accepting a cached same-signature value.

        Full scans detect appends through the metadata cache's size-plus-mtime
        signature, including after restart. A watcher event is stronger evidence
        still, so refresh also covers an unusual in-place rewrite whose size and
        mtime both remain unchanged.
        """
        return {
            path: _ChangedScanRead(
                meta=self._meta_cache.refresh(
                    path,
                    transcripts_mod.transcript_meta,
                    transcripts_mod.TranscriptMeta(),
                ),
                delegated_session_keys=self._cached_delegated_session_keys(path),
                last_event=self._cached_last_event(path),
            )
            for path in paths
        }

    async def context_observations(
        self, account: Account, sessions: list[Session]
    ) -> tuple[ContextObservation, ...]:
        """Return only transcript readings with an exact current worker mapping."""
        client = self._clients.get(account.key)
        if client is None:
            return ()
        try:
            await client.refresh()
        except Exception as exc:  # noqa: BLE001 — stale mappings must not rearm
            log.debug("Codex context observation refresh failed for %s: %s", account.key, exc)
            return ()
        observations = []
        for session in sessions:
            worker_key = client.worker_key(session.session_id)
            path = self._paths.get((str(account.root), session.session_id))
            if worker_key is None or path is None:
                continue
            observed = transcripts_mod.context_observation(path)
            if observed is None or observed.tokens != session.context_tokens:
                continue
            observations.append(
                ContextObservation(
                    worker_key=worker_key,
                    source_session_id=session.session_id,
                    tokens=observed.tokens,
                    observed_at=observed.observed_at,
                    source_version=observed.source_version,
                )
            )
        return tuple(observations)

    def _refresh_known_rollout(
        self,
        account: Account,
        current: Session,
        path: Path,
        scan_read: _ChangedScanRead,
    ) -> Session:
        session = copy.copy(current)
        meta = scan_read.meta
        last_activity = _mtime(path)
        lifecycle_active = meta.task_active if meta.task_status is not None else None
        status, thinking, activity, stalled, last_progress = (
            self._derived_state_from_event(
                scan_read.last_event,
                last_activity,
                lifecycle_active=lifecycle_active,
                turn_started_at=meta.task_started_at,
            )
            if last_activity is not None
            else (SessionStatus.IDLE, False, None, False, None)
        )
        kref = kanban_mod.parse_ref(meta.first_prompt)
        title = meta.title
        issue_url = issue_status = issue_status_kind = None
        if kref is not None:
            issue_url = kanban_mod.issue_url(kref)
            title = kanban_mod.format_title(kref, self._kanban.get(kref))
            badge = self._kanban.get_status(kref)
            if badge is not None:
                issue_status, issue_status_kind = badge
        session.status = status
        session.thinking = thinking
        session.stalled = stalled
        session.lifecycle_active = lifecycle_active
        session.activity = activity
        session.cwd = Path(meta.cwd) if meta.cwd else None
        session.title = title
        session.issue_url = issue_url
        session.issue_status = issue_status
        session.issue_status_kind = issue_status_kind
        session.initial_prompt = meta.first_prompt
        session.last_prompt = meta.last_prompt
        session.last_text = meta.last_text
        session.last_role = meta.last_role
        session.last_message_at = meta.last_message_at
        session.model = meta.model
        session.kind = _display_kind(meta.kind)
        session.worker_type = "kanban" if kref is not None else "you"
        session.is_delegated = False
        session.parent_session_key = None
        session.started_at = meta.started_at
        session.last_activity = last_activity
        session.last_progress = last_progress
        session.tokens = meta.tokens
        session.context_tokens = meta.context_tokens
        session.subagents = ()
        session.subagent_count = 0
        session.show_when_idle = True
        session.capabilities = self._capabilities(
            account, current.session_id, path, meta, status
        )
        self._project_runtime_session(account, session, path, scan_read)
        return session

    def next_liveness_deadline(
        self, account: Account, sessions: list[Session]
    ) -> float | None:
        """The sweep's clocks: a rollout's live window and a working turn's stall (#2865)."""
        return recency_deadline(sessions, (LIVE_WINDOW_S, SUBAGENT_STALL_S), time.time())

    def sweep_liveness(
        self, account: Account, sessions: list[Session], state: AppState
    ) -> list[Session]:
        """Refresh recency-derived status and mtime-cached card metadata.

        Run when a rollout changes or ``next_liveness_deadline`` falls due (#2865).
        """

        client = self._clients.get(account.key)
        targets: list[Session] = []
        paths: dict[str, Path | None] = {}
        now = datetime.now(UTC)
        missing = object()
        for session in sessions:
            if session.parent_session_key is not None:
                continue
            owned = client is not None and client.owns(session.session_id)
            path = self._paths.get((str(account.root), session.session_id))
            if path is None:
                path = self._transcript_path(account, session)
            paths[session.key] = path
            try:
                signature: object = size_mtime_sig(path) if path is not None else None
            except OSError:
                signature = None
            active = owned and client.active_turn(session.session_id) is not None
            interaction = self._actionable_interaction(account, session.session_id)
            current = (
                signature,
                owned,
                active,
                interaction.id if interaction is not None else None,
            )
            cache_key = (str(account.root), session.session_id)
            previous = self._liveness_inputs.get(cache_key, missing)
            self._liveness_inputs[cache_key] = current
            age = (
                (now - session.last_progress).total_seconds()
                if session.last_progress is not None
                else 0.0
            )
            write_age = (
                (now - session.last_activity).total_seconds()
                if session.last_activity is not None
                else 0.0
            )
            clock_transition = (
                not owned
                and session.status == SessionStatus.LIVE
                and write_age >= LIVE_WINDOW_S
            ) or session.thinking and age >= SUBAGENT_STALL_S
            if previous != current or clock_transition:
                targets.append(session)

        def project(session: Session) -> None:
            # Subagent child sessions: _transcript_path rejects is_subagent
            # rollouts, so the sweep can't refresh them. Leave their state to the
            # full scan, which builds them from the spawned rollout directly.
            if session.parent_session_key is not None:
                return
            self._project_runtime_session(account, session, paths.get(session.key))

        return state.apply_session_changes(targets, project)

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            return await fetch_usage_once(account)
        except Exception as exc:  # noqa: BLE001 -- one failed usage read is non-fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return CodexUsagePoller(
            account,
            state,
            interval_s=kwargs.get("interval_s", 300.0),
            cache_dir=kwargs.get("cache_dir"),
        )

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        effort: str | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
    ) -> InjectResult:
        del allow_over_budget
        client = self._clients.get(account.key)
        command = message.split(maxsplit=1)
        if command and command[0] == "/compact":
            if len(command) != 1:
                return InjectResult(False, "usage: /compact")
            if images:
                return InjectResult(False, "/compact does not accept image attachments")
            if client is None or not client.owns(session.session_id):
                return InjectResult(
                    False,
                    "/compact is available only for AgentDeck-owned Codex chats",
                )
            result = await client.compact(session.session_id)
            if not result.accepted:
                return result
            return InjectResult(
                True,
                result.reason,
                result.session_id,
                transcript_expected=False,
            )
        if client is not None and client.owns(session.session_id):
            kwargs = {"images": images, "effort": effort}
            if require_idle:
                kwargs["require_idle"] = True
                kwargs["expected_last_result_at"] = expected_last_result_at
            result = await client.queue_turn(session.session_id, message, **kwargs)
            if not result.accepted and result.reason == "delivery_id_conflict":
                return InjectResult(False, "client_action_conflict")
            return result
        path = self._transcript_path(account, session)
        if path is None:
            return InjectResult(False, "session rollout no longer exists")
        return await inject_session(
            account,
            session,
            path,
            message,
            timeout_s=timeout_s,
            images=images,
        )

    async def worker_version(self, account: Account, session: Session) -> float | None:
        client = self._clients.get(account.key)
        return (
            await client.worker_version("codex", session.session_id)
            if client is not None
            else None
        )

    async def delivery_spent(
        self, account: Account, session: Session, delivery_id: str
    ) -> bool:
        """Whether the runtime already holds a durable receipt for this delivery.

        Asked of the persistent runtime because that is where the fence lives and
        where it is written to disk, so the answer outlives a web process that was
        cancelled mid-send by a frontend deploy (#2003). Read-only.
        """
        client = self._clients.get(account.key)
        return (
            await client.delivery_spent("codex", session.session_id, delivery_id)
            if client is not None
            else False
        )

    async def start_session(
        self,
        account: Account,
        cwd: Path,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        allow_over_budget: bool = False,
        delegated: bool = False,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
        role: str | None = None,
    ) -> InjectResult:
        # Interactive Codex starts currently have no usage-admission gate; keep
        # the provider-neutral argument so manual starts share one contract.
        del allow_over_budget
        if role:
            # The Codex runtime's start takes no role yet, so a Work agent (#2895 step 5b)
            # starts on a Claude account; refusing beats a chat without its role.
            return InjectResult(False, "a Codex chat cannot start with a session role yet")
        client = self._clients.get(account.key)
        if fork_from is not None and (
            client is None or not client.supports(models_mod.SNAPSHOT_FORK_V1)
        ):
            # The exec fallback below cannot copy a thread, and an older runtime
            # would drop the fork and start an empty chat.
            return InjectResult(False, "the runtime cannot make side chats yet")
        if client is not None:
            result = await client.start_thread(
                cwd,
                message,
                images=images,
                sandbox=sandbox,
                model=model,
                effort=effort,
                approval_policy=approval_policy,
                delegated=delegated,
                session_token=session_token,
                **({"fork_from": fork_from} if fork_from is not None else {}),
            )
            if result.accepted and result.session_id:
                state = self._states.get(account.key)
                if state is not None:
                    now = datetime.now(UTC)
                    state.update_session(
                        Session(
                            key=f"{account.key}:{result.session_id}",
                            account_key=account.key,
                            session_id=result.session_id,
                            status=SessionStatus.LIVE,
                            thinking=True,
                            activity="Working",
                            cwd=cwd,
                            title=display_message(message)[:200],
                            initial_prompt=display_message(message),
                            last_prompt=display_message(message),
                            last_role="user",
                            last_message_at=now,
                            kind="appServer",
                            worker_type="you",
                            started_at=now,
                            last_activity=now,
                            show_when_idle=True,
                            capabilities=self._runtime_capabilities(account, result.session_id),
                        )
                    )
            return result
        return await start_codex_session(
            account,
            cwd,
            message,
            timeout_s=timeout_s,
            images=images,
            effort=effort,
        )

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            # The fallback ``codex exec`` start call only returns after its turn.
            return InjectResult(True)
        try:
            return await asyncio.wait_for(
                client.wait_for_thread(session_id),
                timeout=timeout_s,
            )
        except TimeoutError:
            return InjectResult(False, "Codex delegation timed out")

    async def session_result(self, account: Account, session_id: str) -> str | None:
        session = Session(
            key=f"{account.key}:{session_id}",
            account_key=account.key,
            session_id=session_id,
            status=SessionStatus.IDLE,
        )
        path = await asyncio.to_thread(self._transcript_path, account, session)
        if path is None:
            return None
        meta = await asyncio.to_thread(transcripts_mod.transcript_meta, path)
        # Prefer the turn's canonical final message (task_complete.last_agent_message);
        # fall back to the last assistant item only if no completed turn is present.
        return meta.last_agent_message or meta.last_text

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
        allow_over_budget: bool = False,
    ) -> InjectResult:
        del allow_over_budget
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        result = await client.steer(session.session_id, message, images=images)
        if not result.accepted and result.reason == "delivery_id_conflict":
            return InjectResult(False, "client_action_conflict")
        return result

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.interrupt(session.session_id)

    def permission_prompts_allowed(
        self, account: Account, session: Session
    ) -> bool:
        client = self._clients.get(account.key)
        return bool(
            client is not None
            and client.owns(session.session_id)
            and client.permission_prompts_allowed(session.session_id)
        )

    def permission_prompts_next_turn(self, account: Account, session: Session) -> bool:
        # A Codex turn already in flight keeps the reviewer it started with; the
        # new setting governs the approvals of the turns after it.
        return session.status == SessionStatus.LIVE

    async def set_permission_prompts(
        self, account: Account, session: Session, enabled: bool
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None or not client.owns(session.session_id):
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.set_permission_prompts(session.session_id, enabled)

    async def _answer_actionable(
        self,
        account: Account,
        session: Session,
        interaction: PendingInteraction,
        *,
        answers,
        decision: str | None,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.answer(
            session.session_id,
            interaction.id,
            answers=answers,
            decision=decision,
        )

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        return await asyncio.to_thread(self._read_transcript_file, path, after_seq)

    async def transcript_image(
        self, account: Account, session: Session, seq: int, image_index: int
    ) -> tuple[str, bytes] | None:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.transcript_image, path, seq, image_index)
            if path is not None
            else None
        )

    def _read_transcript_file(self, path: Path, after_seq: int) -> list[TranscriptEvent]:
        """Read and filter a transcript outside the caller's event loop."""
        cached = self._transcript_cache.get(
            path, lambda item: transcripts_mod.read_events(item).events, []
        )
        events = [event for event in cached if event.seq > after_seq]
        return self._decorate_events(events, self._cached_meta(path).model)

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.transcript_cursor, path)
            if path is not None
            else (0, 0)
        )

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return ([], byte_offset, seq)
        read = await asyncio.to_thread(
            transcripts_mod.read_events, path, byte_offset=byte_offset, seq=seq
        )
        events = self._decorate_events(read.events, self._cached_meta(path).model)
        return (events, read.byte_offset, read.seq)

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        event = self._cached_last_event(path)
        if event is not None and event.role == "assistant" and event.model is None:
            event.model = self._cached_meta(path).model
        return event

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        return await asyncio.to_thread(transcripts_mod.recent_conversation, path, limit=limit)

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        path = self._transcript_path(account, session)
        if path is None:
            return TranscriptDetail(
                events=[],
                tokens=TokenTotals(),
                model=None,
                todos=[],
                total_events=0,
                earliest_seq=0,
            )
        detail = await asyncio.to_thread(
            self._load_transcript_file,
            path,
            before_seq,
        )
        self._decorate_events(detail.events, detail.model)
        return detail

    def _load_transcript_file(self, path: Path, before_seq: int | None) -> TranscriptDetail:
        """Build a detail window without blocking the caller's event loop."""
        meta = self._cached_meta(path)
        if before_seq is None:
            # The common open path: the cached, incrementally-resumed tail.
            # Only the returned window (a copy) needs the model fallback.
            tail = self._tail_cache.read_tail(path)
            self._apply_model(tail.events, meta.model)
            return TranscriptDetail(
                events=tail.events,
                tokens=tail.tokens,
                model=meta.model,
                todos=[],
                total_events=tail.total_events,
                earliest_seq=tail.events[0].seq if tail.events else 0,
                skipped=tail.skipped,
            )
        # "Load earlier" needs events before the cached tail; reparse fully.
        read = transcripts_mod.read_events(path)
        all_events = read.events
        self._apply_model(all_events, meta.model)
        events = [event for event in all_events if event.seq < before_seq]
        window = events[-DETAIL_WINDOW:]
        return TranscriptDetail(
            events=window,
            tokens=transcripts_mod.token_totals(all_events),
            model=meta.model,
            todos=[],
            total_events=len(all_events),
            earliest_seq=window[0].seq if window else 0,
            skipped=read.skipped,
        )
