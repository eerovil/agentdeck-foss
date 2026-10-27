"""ClaudeCodeProvider — translates a $CLAUDE_CONFIG_DIR into agentdeck sessions.

Discovers sessions (live from the pid registry, idle from transcript files),
reads their transcripts, surfaces per-account usage limits, and derives the
claude.ai/code deep link for cloud/RC-spawned sessions. Existing CLI chats are
read-only here (reply on claude.ai via the deep link); sessions the deck itself
spawned as worker processes can be started and driven from the UI via the
runtime service (new chat, send, steer, interrupt).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from watchfiles import Change

from ... import images as images_mod
from ... import kanban as kanban_mod
from ... import live_window, runtime_feed
from ... import models as models_mod
from ... import questions as questions_mod
from ...action_context import current_client_action_id
from ...chat_cwd import cwd_exists
from ...models import (
    BACKGROUND_AGENT_ACTIVITY,
    BACKGROUND_TASK_ACTIVITY,
    CONTROL_CAPABILITIES,
    STALL_S,
    Account,
    Capability,
    ForkFrom,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
    event_progress_at,
    event_turn_open,
    runtime_control_capabilities,
    runtime_turn_state,
    turn_stalled,
)
from ...transcript_rollbacks import HIDDEN
from .. import transcript_reader as transcript_reader_mod
from .._filecache import FileCache, SharedFileCache, mtime_sig, size_mtime_sig
from .._tailcache import TranscriptTailCache
from ..base import (
    EffortChoice,
    ModelChoice,
    NewSessionDefaults,
    SessionProvider,
    recency_deadline,
)
from . import history as history_mod
from . import registry as registry_mod
from . import transcripts as transcripts_mod
from .usage import ClaudeUsagePoller, fetch_usage_once
from .worker_client import ClaudeWorkerClient

if TYPE_CHECKING:
    from ...state import AppState

# Max events rendered on a detail page; older ones fetched via "load earlier".
DETAIL_WINDOW = 400

log = logging.getLogger(__name__)

# Upper bound on idle sessions surfaced per account in v0.1 — the projects/ dir
# can hold thousands of transcripts; show the most recently active.
MAX_IDLE_SESSIONS = 200

# Entry cap for the per-transcript memos. A scan's working set is the kept
# sessions plus their subagents, so this holds many scans' worth while still
# refusing to grow with every transcript the account has ever had: unbounded,
# they were a standing share of a multi-gigabyte web worker (#1487).
SCAN_CACHE_ENTRIES = 2048
# A replacement worker needs the previous worker's whole scan working set, not
# merely the most recent rows retained in memory. The live accounts currently
# exceed 4,000 transcript paths between them, so keep the disk tier larger than
# the deliberately bounded process-local tier (#1505).
SHARED_SCAN_CACHE_ENTRIES = 8192


@dataclass(frozen=True, slots=True)
class _RootScanRead:
    meta: transcripts_mod.TranscriptMeta
    last_event: TranscriptEvent | None
    context_tokens: int | None
    task_ledger: transcripts_mod.TaskLedger


@dataclass(frozen=True, slots=True)
class _SubagentScanRead:
    meta: transcripts_mod.TranscriptMeta
    last_event: TranscriptEvent | None
    task: str | None

# Brief write recency resolves only ambiguous transcript events. Structural or
# provider lifecycle keeps an Active Turn working beyond this window.
THINKING_WINDOW_S = 25.0
# How often the worker snapshot is read when the runtime cannot say it moved: one
# from before its change feed, or none reachable (#2865).
WORKER_FALLBACK_S = 1.0

# How long the liveness sweep trusts "this session id has no transcript file".
# Resolving a miss walks every project directory twice (#1325), so a session
# whose transcript never appears used to pay that walk on every sweep forever.
# Only the sweep honours this: the authoritative scan re-seeds ``_paths`` from
# the listing it already built, so a transcript that does show up is picked up
# by the next scan regardless, and every user-facing read still resolves exactly.
MISSING_TRANSCRIPT_TTL_S = 60.0
# Bound on remembered misses, in the spirit of #1324. Session ids are unbounded
# over a long uptime; expired entries are dropped once the dict passes this.
MISSING_TRANSCRIPT_CACHE_MAX = 4096


def worker_type(is_kanban: bool, has_deep_link: bool) -> str:
    """Classify a session for list colouring: an autonomous kanban worker, a
    cloud/RC-spawned session, or one of your own interactive chats. Kanban wins
    over cloud — a live kanban worker is also RC-spawned, but "kanban" is the
    more useful label."""
    if is_kanban:
        return "kanban"
    if has_deep_link:
        return "cloud"
    return "you"


def _pending_from_dict(data: object) -> PendingInteraction | None:
    """Rebuild a PendingInteraction from the neutral dict the runtime publishes in
    the worker snapshot (the runtime does the Claude wire-schema mapping — see
    worker._normalize_interaction). The model owns the schema, so this can no
    longer silently drop cwd/url/secret/option-value the way it used to."""
    return PendingInteraction.from_dict(data)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _newest_helper_write(transcript: Path | None) -> datetime | None:
    """When one of this chat's helper agents last wrote its own transcript (#3067).

    A chat waiting on its helper agents (``<session>/subagents/*.jsonl``) writes nothing
    to its own transcript until they report back, so read on its own the chat looks
    stalled while its helpers are busy.
    """
    if transcript is None:
        return None
    try:
        helpers = list((transcript.parent / transcript.stem / "subagents").glob("*.jsonl"))
    except OSError:
        return None
    return max(
        (written for path in helpers if (written := _mtime(path)) is not None), default=None
    )


def transcript_for_session(config_dir: Path, session_id: str) -> Path | None:
    """The chat transcript one account holds for this id, if it holds one.

    One glob rather than the whole ``_transcripts_by_mtime`` inventory: the
    caller (``agentdeck job submit`` working out which account a bare
    ``$CLAUDE_CODE_SESSION_ID`` belongs to, #2092) asks about a single id and
    would otherwise pay for tens of thousands of stats to answer it. Only the
    ``projects/<slug>/<uuid>.jsonl`` chat shape counts; a subagent transcript
    is not a chat anything can be announced to.

    The id is joined as a literal name rather than globbed, so an id carrying
    ``*`` or ``[`` cannot match some other chat's transcript.
    """
    projects = config_dir / "projects"
    if not projects.is_dir():
        return None
    try:
        slugs = sorted(projects.iterdir())
    except OSError:
        return None
    for slug in slugs:
        candidate = slug / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _transcripts_by_mtime(config_dir: Path) -> dict[str, tuple[Path, float]]:
    """Map sessionId -> (newest transcript path, its mtime).

    The mtime comes back with the path because both callers want it and a
    second ``stat`` per file is not free: an account with tens of thousands of
    transcripts paid for ~12,000 extra syscalls on every 15-second scan just to
    sort a list it had already stat'ed (#1088).
    """
    out: dict[str, tuple[Path, float]] = {}
    projects = config_dir / "projects"
    if not projects.is_dir():
        return out
    for path in projects.glob("*/*.jsonl"):
        sid = path.stem
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # A transcript deleted between the listing and the stat is simply
            # not there; the scan must not die on the race.
            continue
        prev = out.get(sid)
        if prev is None or mtime > prev[1]:
            out[sid] = (path, mtime)
    return out


def _list_transcripts(config_dir: Path) -> dict[str, Path]:
    """Map sessionId -> newest transcript path under projects/<slug>/<uuid>.jsonl."""
    return {
        sid: path for sid, (path, _mt) in _transcripts_by_mtime(config_dir).items()
    }


def _list_subagents(config_dir: Path) -> dict[str, tuple[Path, str]]:
    """Map subagent id -> (newest transcript path, parent session uuid).

    Claude Task/Agent-tool subagents live at
    ``projects/<slug>/<parent-uuid>/subagents/agent-*.jsonl`` (``isSidechain``),
    which the top-level ``<slug>/<uuid>.jsonl`` glob never reaches. The parent
    uuid is the directory that contains the ``subagents/`` folder.
    """
    out: dict[str, tuple[Path, str]] = {}
    projects = config_dir / "projects"
    if not projects.is_dir():
        return out
    for path in projects.glob("*/*/subagents/*.jsonl"):
        sid = path.stem
        prev = out.get(sid)
        if prev is None or path.stat().st_mtime > prev[0].stat().st_mtime:
            out[sid] = (path, path.parent.parent.name)
    return out


def _mtimes(paths: list[Path]) -> dict[Path, float]:
    """Each transcript's last-write time; a file that vanished is left out."""
    found: dict[Path, float] = {}
    for path in paths:
        try:
            found[path] = path.stat().st_mtime
        except OSError:
            continue
    return found


def _named_inventory(
    config_dir: Path, session_ids: frozenset[str]
) -> tuple[dict[str, Path], list[str], dict[str, tuple[Path, str]]]:
    """``_scan_inventory`` for just these chats, found by name (#2841)."""
    transcripts: dict[str, Path] = {}
    subagents: dict[str, tuple[Path, str]] = {}
    projects = config_dir / "projects"
    for sid in sorted(session_ids):
        path = transcript_for_session(config_dir, sid)
        if path is None:
            continue
        transcripts[sid] = path
        if projects.is_dir() and "/" not in sid:
            for sub in projects.glob(f"*/{sid}/subagents/*.jsonl"):
                subagents[sub.stem] = (sub, sid)
    return transcripts, list(transcripts), subagents


def _scan_inventory(
    config_dir: Path,
) -> tuple[dict[str, Path], list[str], dict[str, tuple[Path, str]]]:
    """One filesystem inventory per scan: transcripts by id, those ids newest
    first, and the subagent index.

    Listing an account's ``projects`` tree and ranking it by mtime is tens of
    thousands of directory entries and stats. Kept together so the scan can run
    all of it in one thread instead of stalling the event loop — every
    millisecond spent here is a millisecond no page can be rendered in.
    """
    by_mtime = _transcripts_by_mtime(config_dir)
    ranked = sorted(by_mtime, key=lambda sid: by_mtime[sid][1], reverse=True)
    transcripts = {sid: path for sid, (path, _mt) in by_mtime.items()}
    return transcripts, ranked, _list_subagents(config_dir)


def _is_collected(subagent_id: str, collected: frozenset[str]) -> bool:
    """Has the parent taken delivery of this subagent's result?

    A subagent's transcript is ``agent-<task-id>.jsonl`` while the parent's
    ``<task-notification>`` names the bare task id, so match both spellings rather
    than assume which one a future CLI writes.
    """
    return bool(collected & {subagent_id, subagent_id.removeprefix("agent-")})


def _subagent_task(path: Path) -> str | None:
    """The subagent's first user prompt (its Task description).

    ``transcript_meta`` intentionally drops ``isSidechain`` user lines, so a
    subagent transcript yields no ``first_user``; read the first user text here
    directly so the nested row shows the task instead of the ``agent-<hash>`` id.
    """
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if obj.get("type") != "user":
                    continue
                content = obj.get("message", {}).get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                return text
    except OSError:
        return None
    return None



def _moved_cwd(cwd: Path | None, worker_cwd: str | None) -> Path | None:
    """The runtime's directory for a chat it moved out of a deleted one (#2524).

    The transcript and the prompt history keep the directory a chat started in.
    Once that is gone the runtime resumes the chat in its repository and records
    the move on its own worker record, which is then the truth about where the
    chat runs.
    """
    if not worker_cwd or cwd is None or Path(worker_cwd) == cwd:
        return cwd
    if cwd_exists(cwd) or not cwd_exists(Path(worker_cwd)):
        return cwd
    return Path(worker_cwd)

class ClaudeCodeProvider(SessionProvider):
    provider_id = "claude_code"
    # New chats spawn deck-owned worker processes via the runtime service. When
    # that service has no Claude workers (feature off), start_session returns a
    # clean error and the provider stays a read-only transcript viewer.
    supports_new_session = True
    # Aliases (not pinned ids) so the list survives model version bumps; the CLI
    # resolves them. Applied via --model at worker spawn (new chat only).
    selectable_models = (
        ModelChoice("opus", "Opus"),
        ModelChoice("sonnet", "Sonnet"),
        ModelChoice("haiku", "Haiku"),
        ModelChoice("fable", "Fable 5.1"),
    )
    selectable_efforts = tuple(
        EffortChoice(value, label)
        for value, label in (
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("xhigh", "Extra high"),
            ("max", "Max"),
        )
    )
    _model_default_efforts = {
        "opus": "medium",
        "sonnet": "medium",
        "fable": "medium",
    }
    # Claude Code (reproduced on 2.1.220) silently drops a prompt containing the
    # bare word "Stuck?" from the request it sends. The CLI still writes the
    # message to its own transcript, so the deck, the person, and the transcript
    # all agree a question was asked while the model was handed nothing and
    # answers "what would you like to work on?". It bites the message that opens
    # a CLI process — a new chat, and every delivery that has to respawn an idle
    # owned worker with --resume — which is most of what this deck sends.
    # Narrow on purpose: "Broken?", "Stuck" without the question mark, lowercase
    # "stuck?", "Stuck ?" and "aStuck?" all arrive intact.
    _DROPPED_BY_CLI = re.compile(r"(?<!\w)Stuck\?")

    @classmethod
    def user_message_refusal(cls, text: str) -> str | None:
        if cls._DROPPED_BY_CLI.search(text):
            return (
                "Claude Code drops a message containing “Stuck?” before the "
                "model sees it, so this would be answered blind. Reword it "
                "(“stuck?” in lower case gets through) and send again."
            )
        return None

    def new_session_defaults(
        self, account: Account, *, model_override: str | None = None
    ) -> NewSessionDefaults:
        """Read defaults inherited by the account's Claude worker process."""
        settings: dict = {}
        try:
            loaded = json.loads((account.root / "settings.json").read_text())
            if isinstance(loaded, dict):
                settings = loaded
        except (OSError, json.JSONDecodeError):
            pass
        env = settings.get("env")
        env_model = env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None
        configured_model = settings.get("model")
        model = model_override or (
            env_model
            if isinstance(env_model, str)
            else configured_model
            if isinstance(configured_model, str)
            else None
        )
        env_effort = (
            env.get("CLAUDE_CODE_EFFORT_LEVEL") if isinstance(env, dict) else None
        )
        configured_effort = settings.get("effortLevel")
        model_default_effort = self._model_default_efforts.get(model)
        if isinstance(env_effort, str):
            effort = model_default_effort if env_effort == "auto" else env_effort
        elif isinstance(configured_effort, str):
            effort = (
                model_default_effort
                if configured_effort == "auto"
                else configured_effort
            )
        else:
            effort = model_default_effort
        return NewSessionDefaults(model=model, effort=effort)

    def __init__(self) -> None:
        cache_version_paths = (
            Path(transcripts_mod.__file__),
            Path(transcript_reader_mod.__file__),
            Path(images_mod.__file__),
            Path(questions_mod.__file__),
            Path(models_mod.__file__),
        )
        # Cheap transcript reads memoized by file signature so idle transcripts
        # aren't re-parsed every scan. Meta carries the last text a session shows,
        # so it needs the finer (size, mtime_ns) signature for the same reason
        # last-event does: a filesystem whose timestamps advance on a clock tick
        # rather than per write gives an append the mtime it already had, and the
        # cache then answers with the text from before it (#1368). Context tokens
        # are a number nobody reads a stale copy of for long, and stay on mtime.
        self._meta_cache: FileCache[transcripts_mod.TranscriptMeta] = FileCache(
            size_mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        self._last_ev_cache: FileCache[TranscriptEvent | None] = FileCache(
            size_mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        self._ctx_cache: FileCache[int | None] = FileCache(
            mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        # The subagent row's task description, which ``transcript_meta`` cannot
        # carry: a sidechain's first user line. Memoized like the rest so the
        # prewarm below covers it rather than the build loop reopening every
        # subagent transcript on the event loop.
        self._subagent_task_cache: FileCache[str | None] = FileCache(
            size_mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        # What opened each working chat's current turn (#2760): read only while it works.
        self._opener_cache: FileCache[bool] = FileCache(
            size_mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        # One complete scan record crosses the process boundary. Keeping the
        # component caches local preserves their individual hot paths while a
        # replacement worker can reuse all parsing performed by its predecessor.
        self._scan_read_cache: SharedFileCache[_RootScanRead | _SubagentScanRead] = (
            SharedFileCache(
                size_mtime_sig,
                "claude-full-scan-read",
                version_paths=(
                    *cache_version_paths,
                    Path(__file__),
                ),
                local_entries=SCAN_CACHE_ENTRIES,
                max_entries=SHARED_SCAN_CACHE_ENTRIES,
            )
        )
        self._transcript_cache: SharedFileCache[list[TranscriptEvent]] = SharedFileCache(
            size_mtime_sig,
            "claude-render-events",
            version_paths=cache_version_paths,
        )
        self._billable_paths: dict[str, list[Path]] = {}
        self._sidechain_parents: dict[str, frozenset[str]] = {}
        self._liveness_signatures: dict[tuple[str, str], object] = {}
        # Which background work a chat has launched and which of it it has
        # collected — spawned agents and backgrounded shell commands alike. On the
        # same fine signature: the answer changes with the very append that ends a
        # piece of that work.
        self._tasks_cache: FileCache[transcripts_mod.TaskLedger] = FileCache(
            size_mtime_sig, max_entries=SCAN_CACHE_ENTRIES
        )
        # The parsed detail-window tail, resumed from the reader's byte cursor
        # so opening a large chat doesn't reparse the whole JSONL (#791).
        self._tail_cache = TranscriptTailCache(
            transcripts_mod.read_events, window_size=DETAIL_WINDOW
        )
        # Where each session's transcript is, so resolving it costs one stat
        # instead of a directory walk. The subagent pattern below has to list
        # every project directory to find its ``subagents/`` folders, which on a
        # real account means tens of thousands of dirents — per live session, on
        # every sweep, on the event loop. Each full scan re-seeds this from the
        # listing it already built, so a transcript that moves to another project
        # slug is picked up within one scan interval. Keyed by config root rather
        # than account key: the provider is a process-wide singleton, and what a
        # session id resolves against is a directory, not a label.
        self._paths: dict[tuple[str, str], Path] = {}
        # Each config root's ``history.jsonl``, read from where the last scan stopped
        # (#2865). Keyed by root for the same reason as ``_paths``.
        self._histories: dict[str, history_mod.HistoryIndex] = {}
        # The negative half of ``_paths``: same key, monotonic time of the last
        # failed resolve. Only the liveness sweep reads it (#1325).
        self._missing_paths: dict[tuple[str, str], float] = {}
        # Resolves kanban dispatch prompts to real GitHub issue titles.
        self._kanban = kanban_mod.KanbanTitleCache()
        # Deck-owned worker clients (one per account). Each tracks runtime
        # availability so new-chat capability can change without a web restart.
        self._workers: dict[str, ClaudeWorkerClient] = {}
        self._states: dict[str, object] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}

    # --- deck-owned workers (optional; runtime-gated) ------------------

    async def start_account(self, account: Account, state) -> None:
        client = ClaudeWorkerClient(
            account,
            on_change=lambda: self._runtime_changed(account, state),
            on_session_token=lambda token, session_id: state.bind_session_token(
                token, f"{account.key}:{session_id}"
            ),
        )
        self._workers[account.key] = client
        self._states[account.key] = state
        await client.probe()
        self._watch_tasks[account.key] = asyncio.create_task(
            self._watch(account, client), name=f"claude-workers:{account.key}"
        )

    async def _watch(self, account: Account, client: ClaudeWorkerClient) -> None:
        """Re-read the worker snapshot when the runtime says it moved (#2865).

        One snapshot fetch per change rather than one a second; a runtime that predates
        the change feed, or cannot be reached, is read on the old one-second cadence.
        """
        item = runtime_feed.workers_item(account.provider_id, account.label)
        with runtime_feed.shared().subscribe(item) as changes:
            failed = False
            while True:
                await changes.wait(WORKER_FALLBACK_S, retry=failed)
                try:
                    await client.refresh()  # callback reprojects cached Sessions on change
                    # A refused read leaves the client unavailable: tried again on the
                    # fallback cadence, not at the next change (#2865).
                    failed = not client.available
                except Exception as exc:  # noqa: BLE001 -- retried on the fallback cadence
                    failed = True
                    log.debug("claude worker refresh failed for %s: %s", account.key, exc)

    async def stop_account(self, account: Account) -> None:
        task = self._watch_tasks.pop(account.key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._states.pop(account.key, None)
        self._sidechain_parents.pop(account.key, None)
        client = self._workers.pop(account.key, None)
        if client is not None:
            await client.stop()

    def can_start_session(self, account: Account) -> bool:
        client = self._workers.get(account.key)
        return bool(client and client.available)

    def offers_new_session(self, account: Account) -> bool:
        # Still offered while the runtime cannot be reached, as while it restarts, but
        # not when it answered that it serves no workers for this account (#2975).
        client = self._workers.get(account.key)
        return self.can_start_session(account) or bool(
            client is not None and getattr(client, "unreachable", False)
        )

    async def prepare_spare(
        self,
        account: Account,
        shape: str,
        key: str,
        *,
        session_token: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        role: str | None = None,
    ) -> str:
        """Ask the runtime to keep a CLI booted for this kind of new chat (#3420).

        ``""`` when this account has no runtime that can: the start is then cold. ``role``
        is a deck-started Work's (#3590); a runtime too old to read it boots a plain spare,
        which that Work's start then does not match, so it is only cold.
        """
        client = self._workers.get(account.key)
        if client is None or not client.supports(models_mod.SNAPSHOT_SPARE_V1):
            return ""
        return await client.prepare_spare(
            shape,
            key,
            session_token=session_token,
            cwd=cwd,
            model=model,
            effort=effort,
            **({"role": role} if role else {}),
        )

    async def drop_spare(self, account: Account, shape: str) -> str:
        """Stop the CLI this account keeps for ``shape``; ``""`` with no runtime that can."""
        client = self._workers.get(account.key)
        if client is None or not client.supports(models_mod.SNAPSHOT_SPARE_V1):
            return ""
        return await client.drop_spare(shape)

    def interactions_unknown(self, account: Account) -> bool:
        # Down, failing, or not yet answered since this process started: a deploy restarts
        # the web just before the runtime, so a fresh web often meets one still coming up.
        client = self._workers.get(account.key)
        return bool(
            client is not None
            and not getattr(client, "available", False)
            and (getattr(client, "unreachable", False) or not getattr(client, "answered", True))
        )

    def runtime_unreachable(self, account: Account, session: Session) -> bool:
        # A dropped connection clears only the snapshot bytes, so the owned map
        # still names this chat while the runtime restarts (#2847).
        client = self._workers.get(account.key)
        return bool(
            client is not None
            and not client.available
            and client.owns(session.session_id)
        )

    def fork_refusal(self, account: Account, session: Session) -> str | None:
        client = self._workers.get(account.key)
        if client is None or not client.available:
            return "Claude workers are not running on this deck right now."
        if not client.supports(models_mod.SNAPSHOT_FORK_V1):
            return "The deck's runtime is too old to make side chats; it needs a restart."
        if session.cwd is None or not session.cwd.is_dir():
            return "This chat's working directory no longer exists, so a side chat cannot start."
        path = self._transcript_path(account, session)
        if path is None or "subagents" in path.parts:
            return "This chat's history is not on this host."
        return None

    def fork_settings(self, account: Account, session: Session) -> tuple[str | None, str | None]:
        client = self._workers.get(account.key)
        model, effort = (
            client.settings_for(session.session_id)
            if client is not None and client.owns(session.session_id)
            else (None, None)
        )
        # Otherwise the model the transcript last answered with; "<synthetic>" and
        # the like are the CLI's own lines, not a model to start on.
        if model is None and session.model and session.model.startswith("claude-"):
            model = session.model
        return model, effort

    def fork_ref(self, account: Account, session: Session, seq: int) -> str | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        return transcripts_mod.fork_ref(path, seq, turn_finished=not session.thinking)

    def rewind_refusal(self, account: Account, session: Session) -> str | None:
        client = self._workers.get(account.key)
        if client is None or not client.available:
            return "Claude workers are not running on this deck right now."
        if not client.supports(models_mod.SNAPSHOT_REWIND_V1):
            return "The deck's runtime is too old to roll chats back; it needs a restart."
        if not client.owns(session.session_id):
            return "Only a chat the deck runs can be rolled back."
        path = self._transcript_path(account, session)
        if path is None or "subagents" in path.parts:
            return "This chat's history is not on this host."
        return None

    def rewind_floor(self, account: Account, session: Session) -> int:
        path = self._transcript_path(account, session)
        return transcripts_mod.first_prompt_seq(path) if path is not None else 0

    def rewind_ref(self, account: Account, session: Session, seq: int) -> str | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        return transcripts_mod.rewind_ref(path, seq)

    async def rewind(
        self,
        account: Account,
        session: Session,
        at: str,
        *,
        chat_keys: Iterable[str] = (),
        since: float | None = None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.rewind(session.session_id, at, chat_keys=chat_keys, since=since)

    def _actionable_interaction(
        self, account: Account, session_id: str
    ) -> PendingInteraction | None:
        workers = self._workers.get(account.key)
        if workers is None or not workers.available or not workers.owns(session_id):
            return None
        return _pending_from_dict(workers.pending_interaction(session_id))

    def _runtime_changed(self, account: Account, state) -> None:
        sessions = state.sessions_for_account(account.key)
        # sweep_liveness applies the refresh and publishes once through AppState
        # if any owned worker's projection changed.
        self.sweep_liveness(account, sessions, state)

    def _project_owned_worker(
        self,
        account: Account,
        session: Session,
        *,
        transcript_path: Path | None,
        last_write: datetime | None,
        scan_read: _RootScanRead | None = None,
    ) -> bool:
        """Overlay one owned worker's runtime state onto discovered metadata."""
        workers = self._workers.get(account.key)
        if workers is None or not workers.owns(session.session_id):
            return False
        turn_active = workers.turn_active(session.session_id)
        # The runtime tracks the Agent tasks it saw launched on its own stream; a
        # backgrounded shell command is only in the transcript, and its launch can
        # sit arbitrarily far back in it. Either kind of outstanding work keeps the
        # chat effectively working (#617, #683).
        runtime_active = bool(workers.effective_activity(session.session_id))
        native_background_active = runtime_active and not turn_active
        background = (
            True if scan_read is not None and scan_read.task_ledger.outstanding else None
        ) if scan_read is not None else self._background_work_active(transcript_path)
        effective_activity = runtime_active or background is True
        interaction = self._actionable_interaction(account, session.session_id)
        # Only a prompt the CLI is stuck on holds the turn. An open question it has moved
        # past (#3451) is still answerable, but the chat may be working meanwhile.
        turn_held = workers.turn_held(session.session_id)
        turn_started_at = workers.turn_started_at(session.session_id)
        activity, stalled, progress, failed = (
            self._turn_state_from_event(
                True,
                scan_read.last_event,
                last_write,
                lifecycle_active=effective_activity,
                helper_write=_newest_helper_write(transcript_path),
                turn_started_at=turn_started_at,
            )
            if scan_read is not None and last_write is not None
            else self._turn_state(
                True,
                transcript_path,
                last_write,
                lifecycle_active=effective_activity,
                turn_started_at=turn_started_at,
            )
        )
        # Only a live worker runs or stalls a turn (#2956): a dead one can leave an open turn
        # or an outstanding task in its transcript, and that stale flag held the Work's
        # release as "still working" with nothing running.
        live = workers.live(session.session_id)
        # The runtime only stalls an open turn (#3016): a turn that ended with a command left
        # running in the background is waiting on that command, not hung, and reading it as
        # stalled showed the Work "no progress since" while the runtime said nothing was.
        stalled = stalled and live and turn_active
        # The runtime's record says whether a turn is open (#3123): with no turn and no helper
        # agent, only a background command keeps the chat going, whatever the transcript's
        # tail suggests. A chat quiet past the stall window had no label at all, and the
        # Work read that as Working.
        if background is True and not runtime_active:
            activity = BACKGROUND_TASK_ACTIVITY
        if native_background_active:
            # Native agents can work long after the outer transcript goes quiet.
            # Their runtime lifecycle signal is stronger than transcript-based
            # stall evidence, and capacity counts the same signal.
            activity = BACKGROUND_AGENT_ACTIVITY
            stalled = False
        session.status = SessionStatus.LIVE if live else SessionStatus.IDLE
        session.lifecycle_active = effective_activity
        session.stalled, session.thinking = runtime_turn_state(
            active_turn=effective_activity and live,
            # A root may be waiting on the person while its native agents keep
            # working. In that case the question stays answerable but does not
            # make the root look idle while it still holds a kanban slot.
            #
            # A question the CLI never routed as a control request is the chat
            # waiting on the person just the same: without a prompt tool the
            # turn stays open and only the transcript shows the ask, so keying
            # on the routed interaction alone left the chat reading as working
            # with no label at all (#1878).
            actionable_interaction=(
                (turn_held or bool(session.question))
                and not native_background_active
            ),
            stalled_evidence=stalled,
        )
        session.activity = activity if session.thinking else None
        session.turn_failed = failed and not session.thinking
        session.turn_cut_off = workers.turn_cut_off(session.session_id)
        cut_off_at = workers.turn_cut_off_at(session.session_id)
        session.turn_cut_off_at = (
            datetime.fromtimestamp(cut_off_at, UTC) if cut_off_at > 0.0 else None
        )
        session.revival_due = workers.revival_due(session.session_id)
        session.automatic_retry_at = (
            workers.automatic_retry_at(session.session_id)
            if session.turn_failed
            else None
        )
        session.owned_role = workers.role(session.session_id)
        # A named death only makes sense while the chat is not running; a revived
        # worker clears the reason at spawn anyway (#1546).
        session.worker_exit_reason = (
            "" if session.status is SessionStatus.LIVE
            else workers.exit_reason(session.session_id)
        )
        session.last_progress = progress
        session.show_when_idle = True
        # Keep the read-only capabilities derived from the transcript; strip the
        # whole control set and reapply the current live projection so a stale
        # control capability cannot linger across a runtime becoming unavailable.
        session.capabilities = (session.capabilities - CONTROL_CAPABILITIES) | (
            runtime_control_capabilities(
                available=workers.available,
                # A native background agent keeps the session working, but the
                # completed outer turn is no longer steerable or interruptible.
                active_turn=turn_active,
                actionable_interaction=(
                    interaction is not None
                ),
            )
        )
        if workers.available and workers.bypass_permissions_available(session.session_id):
            session.capabilities |= {Capability.BYPASS_PERMISSIONS}
        if (
            workers.available
            and workers.supports(models_mod.SNAPSHOT_CONTEXT_MANAGEMENT_V1)
            and workers.worker_key(session.session_id) is not None
        ):
            session.capabilities |= {Capability.CONTEXT_ROLLOVER}
        if (
            workers.available
            and workers.supports(models_mod.SNAPSHOT_RELOCATE_V1)
            and workers.worker_key(session.session_id) is not None
        ):
            # The runtime resumes this chat in its repository once its own directory
            # is gone, so that does not stop a person answering it (#2524).
            session.capabilities |= {Capability.RESUME_ELSEWHERE}
        return True

    @staticmethod
    def _delegation_permission_mode(sandbox: str | None) -> str | None:
        # Claude has no Codex-style filesystem sandbox. Plan mode is its native
        # read-only equivalent; everything else — interactive dashboard chats
        # (sandbox is None) AND explicit workspace-write delegations — returns
        # None to inherit the account's configured worker permission_mode (e.g.
        # bypassPermissions for an autonomous account). Forcing "default" here
        # would override that config and leave a headless worker stalling on the
        # first approval-gated tool, since no permission-prompt tool is wired.
        if sandbox == "read-only":
            return "plan"
        return None

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
        worker_key: str | None = None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        if fork_from is not None and not client.supports(models_mod.SNAPSHOT_FORK_V1):
            # An older runtime would drop the fork fields and start an empty chat.
            return InjectResult(False, "the runtime cannot make side chats yet")
        # A retried new-chat request carries the same client-action id. Derive a
        # stable worker key + delivery id from it so the retry maps to exactly one
        # worker (deduped at the deliver layer) instead of spawning a duplicate.
        # Interactive callers without an action id keep a fresh random key.
        client_action_id = current_client_action_id()
        if worker_key:
            # The key a spare CLI was booted under (#3420): the web keeps it for this
            # press, so a retry still names the same worker.
            key = worker_key
        elif client_action_id:
            key = f"chat-{hashlib.sha256(client_action_id.encode()).hexdigest()[:12]}"
        else:
            key = f"chat-{uuid.uuid4().hex[:8]}"
        result = await client.deliver(
            key,
            message,
            cwd=str(cwd),
            fresh=True,
            images=[str(path) for path in images or []],
            model=model,
            effort=effort,
            permission_mode=self._delegation_permission_mode(sandbox),
            delivery_id=client_action_id,
            allow_over_budget=allow_over_budget,
            delegated=delegated,
            session_token=session_token,
            # Only a side chat carries it, so every other start calls exactly as before.
            **({"fork_from": fork_from} if fork_from is not None else {}),
            # Only a deck-started Work agent names a role (#2895 step 5b).
            **({"role": role} if role else {}),
        )
        if not result.accepted and result.reason == "delivery_id_conflict":
            # Same action id, different request payload — a genuine conflict, not
            # a benign retry; surface it distinctly rather than as a raw reject.
            return InjectResult(False, "client_action_conflict")
        return result

    async def _deliver_to_session(
        self,
        account: Account,
        session: Session,
        message: str,
        images: list[Path] | None = None,
        *,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        # A retried dashboard send carries the same client-action id (the injector
        # wraps this call in client_action_context). Pass it as the delivery id so
        # a retry dedups at the deliver layer instead of writing the message twice.
        client_action_id = current_client_action_id()
        kwargs = {
            "images": [str(path) for path in images or []],
            "delivery_id": client_action_id,
            "allow_over_budget": allow_over_budget,
        }
        if require_idle:
            kwargs["require_idle"] = True
            kwargs["expected_last_result_at"] = expected_last_result_at
        result = await client.send(session.session_id, message, **kwargs)
        if not result.accepted and result.reason == "delivery_id_conflict":
            return InjectResult(False, "client_action_conflict")
        return result

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
    ) -> InjectResult:
        return await self._deliver_to_session(
            account,
            session,
            message,
            images,
            allow_over_budget=allow_over_budget,
            require_idle=require_idle,
            expected_last_result_at=expected_last_result_at,
        )

    async def worker_version(self, account: Account, session: Session) -> float | None:
        client = self._workers.get(account.key)
        return (
            await client.worker_version("claude_code", session.session_id)
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
        client = self._workers.get(account.key)
        return (
            await client.delivery_spent("claude_code", session.session_id, delivery_id)
            if client is not None
            else False
        )

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
        allow_over_budget: bool = False,
    ) -> InjectResult:
        return await self._deliver_to_session(
            account, session, message, images, allow_over_budget=allow_over_budget
        )

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.interrupt(session.session_id)

    async def message_worker(
        self, account: Account, key: str, message: str, *, delivery_id: str
    ) -> InjectResult:
        """Send a chat started under worker ``key`` one more message, before it is scanned.

        A deck-started Work's agent starts before its issue exists (#3590) and is told the
        number this way a second later, often while its first turn runs. ``delivery_id``
        makes a retry the same message, never a second one.
        """
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.deliver(key, message, delivery_id=delivery_id, delegated=False)

    async def interrupt_worker(self, account: Account, key: str) -> InjectResult:
        """Interrupt the chat started under worker ``key`` (#3590)."""
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.interrupt_key(key)

    def bypass_permissions_on(self, account: Account, session: Session) -> bool:
        client = self._workers.get(account.key)
        return bool(
            client is not None
            and client.owns(session.session_id)
            and client.bypass_permissions_on(session.session_id)
        )

    async def set_bypass_permissions(
        self, account: Account, session: Session, enabled: bool
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None or not client.owns(session.session_id):
            return InjectResult(False, "this session is not a deck-owned worker")
        return await client.set_bypass_permissions(session.session_id, enabled)

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None or not client.owns(session_id):
            return InjectResult(False, "this session is not a deck-owned worker")
        return await client.wait_for_turn(session_id, timeout_s=timeout_s)

    async def session_result(self, account: Account, session_id: str) -> str | None:
        path = _list_transcripts(account.root).get(session_id)
        if path is None:
            return None
        meta = await asyncio.to_thread(transcripts_mod.transcript_meta, path)
        return meta.last_text if meta.last_role == "agent" else None

    async def _answer_actionable(
        self,
        account: Account,
        session: Session,
        interaction: PendingInteraction,
        *,
        answers,
        decision: str | None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.answer(
            session.session_id, interaction.id, answers=answers, decision=decision
        )

    def _cached_meta(self, path: Path) -> transcripts_mod.TranscriptMeta:
        return self._meta_cache.get(
            path, transcripts_mod.transcript_meta, transcripts_mod.TranscriptMeta()
        )

    def _cached_last_event(self, path: Path) -> TranscriptEvent | None:
        return self._last_ev_cache.get(path, transcripts_mod.last_event, None)

    def _cached_subagent_task(self, path: Path) -> str | None:
        return self._subagent_task_cache.get(path, _subagent_task, None)

    def _cached_task_ledger(self, path: Path) -> transcripts_mod.TaskLedger:
        return self._tasks_cache.get(
            path, transcripts_mod.task_ledger, transcripts_mod.TaskLedger()
        )

    def _background_work_active(self, path: Path | None) -> bool | None:
        """True while this chat has an uncollected background shell task.

        A backgrounded command outlives the turn that started it: the chat ends its
        turn, the CLI wakes it again when the command reports, and in between the
        transcript looks exactly like a finished handoff (#683). The chat's own
        launch/collection ledger is the evidence that it is still waiting.

        Returns ``True`` or ``None``, never ``False``: "no outstanding task" is not
        evidence that the turn is closed, so it must leave transcript structure and
        recency in charge of the Active Turn ladder rather than closing the turn.
        """
        if path is None:
            return None
        return True if self._cached_task_ledger(path).outstanding else None

    def _background_label(
        self,
        tpath: Path | None,
        activity: str | None,
        *,
        background: bool | None,
        runtime_active: bool = False,
    ) -> str | None:
        """Name a chat kept working only by an uncollected background task.

        Without a distinct label, a chat blocked on a backgrounded command or a
        CI wait reads exactly like one the model is actively driving — the #683
        confusion, undiagnosable from the card (#791). Model-driven evidence
        wins: while the runtime reports its own activity, the turn is
        structurally open, or a tool is running, the ordinary label stands.

        This only ever *renames* the generic "Working" label. It must not
        resurrect an activity the turn state declined to give: a stalled or
        idle chat reports ``None`` here, and turning that into a label would
        flip ``thinking`` back on and suppress the stall presentation — the
        same mistake as the unbounded subagent promotion this issue fixes.
        """
        if activity != "Working" or background is not True or runtime_active:
            return activity
        last_ev = self._cached_last_event(tpath) if tpath is not None else None
        if event_turn_open(last_ev) is True:
            return activity
        return BACKGROUND_TASK_ACTIVITY

    def _note_turn_opener(self, session: Session, path: Path | None) -> None:
        """Whether the working turn was opened by a background notification (#2760)."""
        session.background_turn = bool(
            session.thinking
            and path is not None
            and self._opener_cache.get(
                path, transcripts_mod.turn_opened_by_notification, False
            )
        )

    def _cached_context_tokens(self, path: Path) -> int | None:
        return self._ctx_cache.get(path, transcripts_mod.last_context_tokens, None)

    def _prepare_scan_reads(
        self,
        root_paths: list[Path],
        subagent_paths: list[Path],
    ) -> tuple[dict[Path, _RootScanRead], dict[Path, _SubagentScanRead]]:
        """Return every transcript-derived value the build loop will consume.

        Metadata, the last event, context tokens and the background-task ledger
        once were not, so the per-session pass
        below decoded transcript JSON inline on the event loop — the single
        hottest frame in the whole web worker, and seconds of blocked loop per
        scan on an account with thousands of transcripts (#1487). The returned
        records, rather than cache residency, carry those values across the
        thread boundary: a scan may contain more paths than the bounded caches
        can retain at once.
        """
        roots = {
            path: self._scan_read_cache.get(
                path,
                self._root_scan_read,
                _RootScanRead(
                    transcripts_mod.TranscriptMeta(),
                    None,
                    None,
                    transcripts_mod.TaskLedger(),
                ),
            )
            for path in root_paths
        }
        subagents = {
            path: self._scan_read_cache.get(
                path,
                self._subagent_scan_read,
                _SubagentScanRead(transcripts_mod.TranscriptMeta(), None, None),
            )
            for path in subagent_paths
        }
        return roots, subagents

    def _root_scan_read(self, path: Path) -> _RootScanRead:
        return _RootScanRead(
            meta=self._cached_meta(path),
            last_event=self._cached_last_event(path),
            context_tokens=self._cached_context_tokens(path),
            task_ledger=self._cached_task_ledger(path),
        )

    def _subagent_scan_read(self, path: Path) -> _SubagentScanRead:
        return _SubagentScanRead(
            meta=self._cached_meta(path),
            last_event=self._cached_last_event(path),
            task=self._cached_subagent_task(path),
        )

    def _turn_state(
        self,
        is_live: bool,
        tpath: Path | None,
        last_write: datetime | None,
        *,
        lifecycle_active: bool | None = None,
        turn_started_at: datetime | None = None,
    ) -> tuple[str | None, bool, datetime | None, bool]:
        """Normalized own-turn presentation from lifecycle + transcript evidence."""
        if last_write is None:
            return (None, False, last_write, False)
        last_ev = self._cached_last_event(tpath) if tpath is not None else None
        return self._turn_state_from_event(
            is_live,
            last_ev,
            last_write,
            lifecycle_active=lifecycle_active,
            helper_write=_newest_helper_write(tpath) if is_live else None,
            turn_started_at=turn_started_at,
        )

    def _turn_state_from_event(
        self,
        is_live: bool,
        last_ev: TranscriptEvent | None,
        last_write: datetime,
        *,
        lifecycle_active: bool | None = None,
        helper_write: datetime | None = None,
        turn_started_at: datetime | None = None,
    ) -> tuple[str | None, bool, datetime | None, bool]:
        """Derive turn state from an event already read off the event loop.

        ``helper_write`` is the newest line one of the chat's helper agents wrote: their
        work is the chat's progress while it waits on them (#3067). ``turn_started_at`` is
        when the runtime opened the running turn: no turn is stalled on progress from the
        turn before it (#3110), the rule Codex already follows.
        """
        progress = event_progress_at(last_ev, last_write)
        for floor in (helper_write, turn_started_at):
            if floor is not None and (progress is None or floor > progress):
                progress = floor
        age = (datetime.now(UTC) - progress).total_seconds() if progress else 1e9
        stalled = turn_stalled(
            live=is_live,
            lifecycle_active=lifecycle_active,
            last_ev=last_ev,
            age_s=age,
        )
        label = activity_label(
            is_live,
            age < THINKING_WINDOW_S,
            last_ev,
            age,
            lifecycle_active=lifecycle_active,
        )
        # The CLI ran out of retries and wrote its own API-error apology in place
        # of a reply. Nothing is running and nothing will move again, so no
        # timer can catch it — the same rule Cursor got in #778, now that Claude
        # Code's transcript says which "reply" is really a failure (#1463).
        failed = last_ev is not None and last_ev.failed
        return (label, stalled, progress, failed)

    def _activity(self, is_live: bool, tpath: Path | None, last_write) -> str | None:
        """Activity label for a live session, from its open turn (cheap, mtime
        -cached tail read). Same logic as the detail page, so list and detail
        agree."""
        return self._turn_state(is_live, tpath, last_write)[0]

    # --- discovery -----------------------------------------------------

    async def _read_history(self, root: Path) -> dict[str, history_mod.HistoryEntry]:
        """``history.jsonl`` from where the last read stopped, off the event loop (#2865)."""
        index = self._histories.get(str(root))
        if index is None:
            index = self._histories[str(root)] = history_mod.HistoryIndex(root)
        return await asyncio.to_thread(index.read)

    def watch_paths(self, account: Account) -> list[Path]:
        root = account.root
        candidates = (root / "sessions", root / "history.jsonl", root / "projects")
        return [path for path in candidates if path.exists()]

    def watch_roots(self, account: Account) -> list[Path]:
        root = account.root
        return [root] if root.is_dir() else []

    def watch_root_change(self, account: Account, path: Path) -> bool:
        try:
            source = path.relative_to(account.root).parts[0]
        except (IndexError, ValueError):
            return False
        return source in {"history.jsonl", "projects", "sessions"}

    # ``scan_sessions(only=...)`` reads just the named chats (#2841).
    supports_targeted_scan = True

    async def scan_sessions(
        self, account: Account, *, only: frozenset[str] | None = None
    ) -> list[Session]:
        """Every chat this account lists, or with ``only`` just those session ids.

        ``only`` is the live window's one-chat load: each named chat's transcript and
        subagents are found by name instead of inventorying the account, and none of
        the account-wide records a full scan owns change. The next full scan lists them.
        """
        root = account.root
        if only is not None:
            # Nothing account-wide on the page-open path: not the CLI registry, not
            # ``history.jsonl``. A chat old enough to be let go is not running in the
            # CLI, and its title and prompts come from its own transcript.
            entries: dict = {}
            alive: dict[str, bool] = {}
            hist: dict = {}
        else:
            entries = {e.session_id: e for e in registry_mod.read_registry(root)}
            alive = {sid: registry_mod.is_alive(e) for sid, e in entries.items()}
            hist = await self._read_history(root)
        if only is not None:
            transcripts, ranked, subagent_index = await asyncio.to_thread(
                _named_inventory, root, only
            )
        else:
            transcripts, ranked, subagent_index = await asyncio.to_thread(
                _scan_inventory, root
            )
            self._sidechain_parents[account.key] = frozenset(
                parent_uuid for _path, parent_uuid in subagent_index.values()
            )
            self._billable_paths[account.key] = [
                *transcripts.values(),
                *(path for path, _parent in subagent_index.values()),
            ]

        # Universe = local transcripts (capped, most-recent) ∪ live registry sessions.
        keep = set(ranked[:MAX_IDLE_SESSIONS])
        if only is None:
            keep |= {sid for sid, ok in alive.items() if ok}
        # Deck-owned workers are headless (absent from the CLI registry) and a
        # freshly spawned one may not have flushed its transcript yet — keep its
        # session so it appears immediately instead of vanishing until the first
        # transcript write lands.
        #
        # Only while that is still true, though. A worker record outlives the
        # chat it spawned, so an id with neither a transcript nor a live process
        # is a ghost: the title falls back to the first eight characters of the
        # id, there is no activity, nothing to open, and `revive` cannot resume a
        # transcript that no longer exists (#1139). Dropping the row loses nothing.
        workers = self._workers.get(account.key) if only is None else None
        window = live_window.current() if only is None else None
        if workers is not None:
            owned = {
                sid
                for sid in workers.owned_session_ids()
                if sid in transcripts or workers.live(sid)
            }
            if window is not None:
                # The live window (#2841): an owned chat whose transcript is older
                # than the window, and is not running, in a Work still going or just
                # opened, is no longer listed.
                stamps = await asyncio.to_thread(
                    _mtimes, [transcripts[sid] for sid in owned if sid in transcripts]
                )
                owned = {
                    sid
                    for sid in owned
                    if workers.live(sid)
                    or window.keeps(
                        f"{account.key}:{sid}",
                        stamps.get(transcripts[sid]) if sid in transcripts else None,
                    )
                }
            keep |= owned
        if window is not None:
            # An older chat someone opened is listed even when this deck does not own it.
            prefix = f"{account.key}:"
            keep |= {
                key.removeprefix(prefix)
                for key in window.extra_keys()
                if key.startswith(prefix) and key.removeprefix(prefix) in transcripts
            }

        root_paths = [transcripts[sid] for sid in keep if sid in transcripts]
        kept_subagent_paths = [
            path
            for path, parent_uuid in subagent_index.values()
            if parent_uuid in keep
        ]
        # Every per-session read below is transcript JSON. Keep all of that
        # disk/JSON work off the event loop. The returned records are the
        # build inputs, so bounded-cache churn cannot move a parse back here.
        root_reads, subagent_reads = await asyncio.to_thread(
            self._prepare_scan_reads, root_paths, kept_subagent_paths
        )

        # Kanban worker sessions carry a fixed dispatch prompt instead of a real
        # title; resolve the referenced GitHub issue title (cached) up front so
        # the build loop below can substitute it. Best-effort — a miss just
        # leaves the existing title fallback in place.
        krefs = []
        for sid in keep:
            tpath = transcripts.get(sid)
            if tpath is None:
                continue
            first_user = root_reads[tpath].meta.first_prompt
            kref = kanban_mod.parse_ref(first_user)
            if kref is not None:
                krefs.append(kref)
        if krefs:
            try:
                await self._kanban.resolve_missing(krefs, datetime.now(UTC).timestamp())
            except Exception as exc:  # noqa: BLE001 — title polish must never break a scan
                log.debug("kanban title resolve failed: %s", exc)

        sessions: list[Session] = []
        for sid in keep:
            entry = entries.get(sid)
            is_live = alive.get(sid, False)
            h = hist.get(sid)
            tpath = transcripts.get(sid)
            if tpath is not None:
                # This listing already picked the newest file for the id, so hand
                # it to the path cache rather than let the sweep re-derive it.
                self._paths[(str(root), sid)] = tpath
                self._missing_paths.pop((str(root), sid), None)

            # Title: prefer the transcript's AI-generated title, then history,
            # then the first user message; last_prompt/cwd likewise from the
            # transcript when the registry/history don't have them.
            ai_title = last_prompt = first_user = tcwd = last_text = last_role = None
            last_message_at = None
            scan_read = root_reads.get(tpath) if tpath is not None else None
            if tpath is not None:
                assert scan_read is not None
                meta = scan_read.meta
                ai_title = meta.title
                last_prompt = meta.last_prompt
                first_user = meta.first_prompt
                tcwd = meta.cwd
                last_text = meta.last_text
                last_role = meta.last_role
                last_message_at = meta.last_message_at

            cwd: Path | None = None
            if entry and entry.cwd:
                cwd = entry.cwd
            elif h and h.project:
                cwd = Path(h.project)
            elif tcwd:
                # The registry entry is gone once a session is idle; the
                # transcript's cwd is what keeps idle sessions injectable.
                cwd = Path(tcwd)
            elif workers is not None and workers.cwd_for(sid):
                # Freshly spawned owned worker with no transcript yet — take the
                # cwd from the runtime snapshot so the card isn't cwd-less.
                cwd = Path(workers.cwd_for(sid))
            cwd = _moved_cwd(cwd, workers.cwd_for(sid) if workers is not None else None)
            title = ai_title or (h.title if h else None) or first_user
            # A kanban dispatch prompt makes a poor title; prefer the resolved
            # issue title, then fall back to the bare "repo#n" reference (which
            # still beats the raw, near-identical dispatch string).
            kref = kanban_mod.parse_ref(first_user)
            issue_url = issue_status = issue_status_kind = None
            if kref is not None:
                issue_url = kanban_mod.issue_url(kref)
                issue_title = self._kanban.get(kref)
                title = (
                    kanban_mod.format_title(kref, issue_title)
                    if issue_title
                    else (ai_title or kanban_mod.format_title(kref, None))
                )
                badge = self._kanban.get_status(kref)
                if badge is not None:
                    issue_status, issue_status_kind = badge
            last_prompt = last_prompt or (h.last_prompt if h else None)
            # When the agent is paused on your answer, surface the question. An
            # unanswered AskUserQuestion (multiple-choice tool) carries its prompt
            # on the last event, not in any text block — prefer it; otherwise fall
            # back to a plain-text question the agent ended its turn on.
            last_ev = scan_read.last_event if scan_read is not None else None
            question = (last_ev.question if last_ev else None) or (
                transcripts_mod.trailing_question(last_text) if last_role == "agent" else None
            )

            context_tokens = scan_read.context_tokens if scan_read is not None else None

            last_write = _mtime(tpath) if tpath else (entry.started_at if entry else None)

            status = SessionStatus.LIVE if is_live else SessionStatus.IDLE
            background_active = (
                True if scan_read is not None and scan_read.task_ledger.outstanding else None
            )
            activity, stalled, last_progress, failed = (
                self._turn_state_from_event(
                    is_live,
                    last_ev,
                    last_write,
                    lifecycle_active=background_active,
                    helper_write=_newest_helper_write(tpath) if is_live else None,
                )
                if last_write is not None
                else (None, False, last_write, False)
            )
            if is_live:
                if (
                    activity == "Working"
                    and background_active is True
                    and event_turn_open(last_ev) is not True
                ):
                    activity = BACKGROUND_TASK_ACTIVITY
            # A transcript's mtime is not the chat's activity: Claude Code
            # rewrites finished transcripts hours later (index/bookkeeping lines
            # that carry no timestamp), which relabelled a night-old chat "1m
            # ago", floated it back to the top of the deck, and un-hid it (#335).
            # The newest real event is the activity; mtime only covers a
            # transcript with no readable event at all.
            last_activity = last_progress or last_write
            thinking = activity is not None
            # A claude.ai deep link exists only for live, cloud/RC-spawned
            # sessions (the access token lives in the process env).
            deep_link = (
                registry_mod.session_deep_link(entry.pid)
                if (is_live and entry is not None)
                else None
            )

            caps: set[Capability] = set()
            if tpath is not None:
                caps.add(Capability.TRANSCRIPT)  # readable from v0.2
            if deep_link is not None:
                caps.add(Capability.DEEPLINK)

            wtype = worker_type(kref is not None, deep_link is not None)

            session = Session(
                key=f"{account.key}:{sid}",
                account_key=account.key,
                session_id=sid,
                status=status,
                thinking=thinking,
                stalled=stalled,
                turn_failed=failed and not thinking,
                lifecycle_active=background_active,
                activity=activity,
                cwd=cwd,
                title=title,
                initial_prompt=first_user,
                last_prompt=last_prompt,
                last_text=last_text,
                last_role=last_role,
                last_message_at=last_message_at,
                last_event_seq=last_ev.seq if last_ev is not None else 0,
                question=question,
                kind=entry.kind if entry else None,
                worker_type=wtype,
                is_delegated=wtype != "you",
                issue_url=issue_url,
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
                pid=entry.pid if (entry and is_live) else None,
                proc_start=entry.proc_start if entry else None,
                started_at=entry.started_at if entry else None,
                last_activity=last_activity,
                last_progress=last_progress,
                model=meta.model if tpath is not None else None,
                context_tokens=context_tokens,
                deep_link=deep_link,
                deep_link_label="open in claude.ai" if deep_link else None,
                show_when_idle=False,
                capabilities=frozenset(caps),
            )
            self._project_owned_worker(
                account,
                session,
                transcript_path=tpath,
                last_write=last_write,
                scan_read=scan_read,
            )
            self._note_turn_opener(session, tpath)
            sessions.append(session)

        # Subagent pass: Task/Agent-tool subagents (isSidechain) nest under their
        # parent session as compact child rows instead of being invisible. Only
        # nesting under a currently-shown parent already bounds this to recent
        # work — no extra age gate needed.
        built_ids = {s.session_id for s in sessions}
        collected_tasks: dict[str, frozenset[str]] = {}
        for sub_sid, (sub_path, parent_uuid) in subagent_index.items():
            if parent_uuid not in built_ids or sub_sid in built_ids:
                continue  # only nest under a shown parent; never shadow a real session
            self._paths[(str(root), sub_sid)] = sub_path
            self._missing_paths.pop((str(root), sub_sid), None)
            last_write = _mtime(sub_path)
            sub_read = subagent_reads[sub_path]
            sub_meta = sub_read.meta
            ai_title, sub_text, sub_role = sub_meta.title, sub_meta.last_text, sub_meta.last_role
            # transcript_meta drops isSidechain user lines, so read the subagent's
            # own first prompt (the Task description) for a human-readable title.
            task = sub_read.task
            # A spawned agent runs until the parent takes delivery of its result,
            # and that collection — not the subagent's own last line — is what ends
            # its turn (#617). Its transcript cannot say so itself: it goes quiet
            # for minutes while generating one large tool call, and its final
            # report carries no terminal stop reason either, so recency reads a
            # working agent and a finished one exactly alike. Guessing "idle" there
            # drops the row, un-rolls the parent's activity, and hands the operator
            # a chat that is in the middle of a wave. The parent's notification
            # ledger answers it exactly; the stall window still bounds a subagent
            # whose parent died before collecting it.
            parent_transcript = transcripts.get(parent_uuid)
            if parent_transcript is not None and parent_uuid not in collected_tasks:
                collected_tasks[parent_uuid] = root_reads[parent_transcript].task_ledger.collected
            activity, stalled, last_progress, failed = (
                self._turn_state_from_event(
                    True,
                    sub_read.last_event,
                    last_write,
                    lifecycle_active=not _is_collected(
                        sub_sid, collected_tasks.get(parent_uuid, frozenset())
                    ),
                )
                if last_write is not None
                else (None, False, last_write, False)
            )
            last_activity = last_progress or last_write
            thinking = activity is not None
            sessions.append(
                Session(
                    key=f"{account.key}:{sub_sid}",
                    account_key=account.key,
                    session_id=sub_sid,
                    status=SessionStatus.LIVE if thinking else SessionStatus.IDLE,
                    thinking=thinking,
                    stalled=stalled,
                    turn_failed=failed and not thinking,
                    activity=activity,
                    title=ai_title or task or sub_sid,
                    initial_prompt=task,
                    last_text=sub_text,
                    last_role=sub_role,
                    last_message_at=sub_meta.last_message_at,
                    # ``kind="subagent"`` is what keeps this row out of Deckhand
                    # triage entirely — the parent chat owns its handoff.
                    kind="subagent",
                    worker_type="you",
                    # A quiet/finished subagent is background work, not a chat
                    # the operator started.
                    is_delegated=not thinking,
                    parent_session_key=f"{account.key}:{parent_uuid}",
                    last_activity=last_activity,
                    last_progress=last_progress,
                    model=sub_meta.model,
                    show_when_idle=True,
                    capabilities=frozenset({Capability.TRANSCRIPT}),
                )
            )
        return sessions

    async def scan_changed_sessions(
        self,
        account: Account,
        changes: frozenset[tuple[Change, Path]],
        state: AppState,
    ) -> list[Session] | None:
        """Refresh known top-level transcripts without rebuilding the inventory.

        A batch is partitioned rather than poisoned. The scan loop coalesces
        every change that arrives during its interval, so on a busy account a
        batch reliably contains something this path cannot attribute — and
        bailing on the first one meant the incremental path almost never ran
        (#1487). A write that cannot move membership (the CLI's own
        ``history.jsonl``) is now simply skipped; only a change that can add,
        remove or re-rank a session still hands authority back to the full scan.
        """
        billable = self._billable_paths.get(account.key)
        if not changes or billable is None:
            return None
        sidechain_parents = self._sidechain_parents.get(account.key)
        if sidechain_parents is None:
            return None
        paths: list[tuple[str, Path, Session]] = []
        for change, path in changes:
            try:
                relative = path.relative_to(account.root)
            except ValueError:
                continue  # outside the account tree; nothing to attribute
            if not relative.parts:
                continue
            if relative.parts[0] == "sessions":
                return None  # registry entries admit sessions before transcripts exist
            if relative.parts[0] != "projects":
                continue  # history and other account files cannot move membership
            if change is not Change.modified:
                return None  # a transcript appeared or went away
            if (
                len(relative.parts) != 3
                or path.suffix != ".jsonl"
                or not path.is_file()
            ):
                # A sidechain transcript or some other file inside a project
                # directory. Subagent rows are derived from their parent's
                # ledger during a full inventory, so this still falls back.
                return None
            session_id = path.stem
            session = state.sessions.get(f"{account.key}:{session_id}")
            if (
                session is None
                or session.parent_session_key is not None
                or session_id in sidechain_parents
                or self._paths.get((str(account.root), session_id)) != path
            ):
                return None
            paths.append((session_id, path, session))
        if not paths:
            # Every change was noise. Merging nothing is the whole point: the
            # slow reconciliation still runs on its own cadence.
            return []

        entries = {entry.session_id: entry for entry in registry_mod.read_registry(account.root)}
        history = await self._read_history(account.root)
        changed_paths = [path for _sid, path, _session in paths]
        root_reads, _subagent_reads = await asyncio.to_thread(
            self._prepare_scan_reads, changed_paths, []
        )
        krefs = [
            ref
            for _session_id, path, _session in paths
            if (ref := kanban_mod.parse_ref(root_reads[path].meta.first_prompt))
            is not None
        ]
        if krefs:
            try:
                await self._kanban.resolve_missing(
                    krefs, datetime.now(UTC).timestamp()
                )
            except Exception as exc:  # noqa: BLE001 — title polish must never break a scan
                log.debug("kanban title resolve failed: %s", exc)
        return [
            self._refresh_known_root(
                account,
                session,
                entries.get(session_id),
                history.get(session_id),
                path,
                root_reads[path],
            )
            for session_id, path, session in paths
        ]

    def _refresh_known_root(
        self,
        account: Account,
        current: Session,
        entry,
        history,
        path: Path,
        scan_read: _RootScanRead,
    ) -> Session:
        """Rebuild one already-admitted root from its canonical transcript."""
        session = copy.copy(current)
        meta = scan_read.meta
        alive = bool(entry and registry_mod.is_alive(entry))
        workers = self._workers.get(account.key)
        worker_cwd = workers.cwd_for(current.session_id) if workers is not None else None
        cwd = (
            entry.cwd
            if entry and entry.cwd
            else Path(history.project)
            if history and history.project
            else Path(meta.cwd)
            if meta.cwd
            else Path(worker_cwd)
            if worker_cwd
            else None
        )
        cwd = _moved_cwd(cwd, worker_cwd)
        first_user = meta.first_prompt
        kref = kanban_mod.parse_ref(first_user)
        issue_url = issue_status = issue_status_kind = None
        title = meta.title or (history.title if history else None) or first_user
        if kref is not None:
            issue_url = kanban_mod.issue_url(kref)
            issue_title = self._kanban.get(kref)
            title = (
                kanban_mod.format_title(kref, issue_title)
                if issue_title
                else (meta.title or kanban_mod.format_title(kref, None))
            )
            badge = self._kanban.get_status(kref)
            if badge is not None:
                issue_status, issue_status_kind = badge
        last_event = scan_read.last_event
        last_write = _mtime(path)
        background_active = True if scan_read.task_ledger.outstanding else None
        activity, stalled, last_progress, failed = (
            self._turn_state_from_event(
                alive,
                last_event,
                last_write,
                lifecycle_active=background_active,
                helper_write=_newest_helper_write(path) if alive else None,
            )
            if last_write is not None
            else (None, False, last_write, False)
        )
        if alive:
            if (
                activity == "Working"
                and background_active is True
                and event_turn_open(last_event) is not True
            ):
                activity = BACKGROUND_TASK_ACTIVITY
        deep_link = (
            registry_mod.session_deep_link(entry.pid) if alive and entry is not None else None
        )
        capabilities: set[Capability] = {Capability.TRANSCRIPT}
        if deep_link is not None:
            capabilities.add(Capability.DEEPLINK)
        worker = worker_type(kref is not None, deep_link is not None)
        session.status = SessionStatus.LIVE if alive else SessionStatus.IDLE
        session.thinking = activity is not None
        session.stalled = stalled
        session.turn_failed = failed and not session.thinking
        session.lifecycle_active = background_active
        session.activity = activity
        session.cwd = cwd
        session.title = title
        session.initial_prompt = first_user
        session.last_prompt = meta.last_prompt or (history.last_prompt if history else None)
        session.last_text = meta.last_text
        session.last_role = meta.last_role
        session.last_message_at = meta.last_message_at
        session.last_event_seq = last_event.seq if last_event is not None else 0
        session.question = (last_event.question if last_event else None) or (
            transcripts_mod.trailing_question(meta.last_text)
            if meta.last_role == "agent"
            else None
        )
        session.kind = entry.kind if entry else None
        session.worker_type = worker
        session.is_delegated = worker != "you"
        session.issue_url = issue_url
        session.issue_status = issue_status
        session.issue_status_kind = issue_status_kind
        session.pid = entry.pid if entry and alive else None
        session.proc_start = entry.proc_start if entry else None
        session.started_at = entry.started_at if entry else None
        session.last_activity = last_progress or last_write
        session.last_progress = last_progress
        session.model = meta.model
        session.context_tokens = scan_read.context_tokens
        session.deep_link = deep_link
        session.deep_link_label = "open in claude.ai" if deep_link else None
        session.show_when_idle = False
        session.capabilities = frozenset(capabilities)
        self._project_owned_worker(
            account,
            session,
            transcript_path=path,
            last_write=last_write,
            scan_read=scan_read,
        )
        self._note_turn_opener(session, path)
        return session

    def next_liveness_deadline(
        self, account: Account, sessions: list[Session]
    ) -> float | None:
        """The sweep's clocks: a working chat's write recency and its stall (#2865)."""
        return recency_deadline(sessions, (THINKING_WINDOW_S, STALL_S), time.time())

    def sweep_liveness(
        self, account: Account, sessions: list[Session], state: AppState
    ) -> list[Session]:
        """Cheap recheck, run when a transcript or registry file changes, a terminal
        chat's process exits, or ``next_liveness_deadline`` falls due (#2865): flip
        LIVE↔IDLE and refresh the activity (busy) state from the open turn, so both
        update between full scans."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        workers = self._workers.get(account.key)
        transcript_paths: dict[str, Path | None] = {}
        targets: list[Session] = []
        now = datetime.now(UTC)
        missing = object()

        for session in sessions:
            if session.parent_session_key is not None:
                continue
            entry = entries.get(session.session_id)
            owned = workers is not None and workers.owns(session.session_id)
            live_now = workers.live(session.session_id) if owned else bool(
                entry and registry_mod.is_alive(entry)
            )
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            if not live_now:
                if session.status != status_now or (
                    owned
                    and (
                        session.turn_cut_off != workers.turn_cut_off(session.session_id)
                        or session.revival_due != workers.revival_due(session.session_id)
                    )
                ):
                    targets.append(session)
                continue

            path = self._transcript_path(account, session, tolerate_stale_miss=True)
            transcript_paths[session.key] = path
            try:
                signature: object = size_mtime_sig(path) if path is not None else None
            except OSError:
                signature = None
            cache_key = (str(account.root), session.session_id)
            previous = self._liveness_signatures.get(cache_key, missing)
            self._liveness_signatures[cache_key] = signature
            age = (
                (now - session.last_progress).total_seconds()
                if session.last_progress is not None
                else 0.0
            )
            clock_transition = (
                session.thinking
                and age >= STALL_S
                or session.activity is not None
                and session.lifecycle_active is None
                and age >= THINKING_WINDOW_S
            )
            if (
                owned
                or session.status != status_now
                or previous != signature
                or clock_transition
            ):
                targets.append(session)

        def project(s: Session) -> None:
            # Subagents have no process-registry ground truth; the sweep would
            # force them IDLE and flicker their state. Leave them to the full
            # scan (which derives liveness from transcript mtime).
            if s.parent_session_key is not None:
                return
            entry = entries.get(s.session_id)
            owned = workers is not None and workers.owns(s.session_id)
            live_now = workers.live(s.session_id) if owned else bool(
                entry and registry_mod.is_alive(entry)
            )
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            activity_now = None
            stalled_now = False
            # Idle rows get no fresh transcript read this sweep, so a failure the
            # last full scan found must survive it rather than reset to healthy.
            failed_now = s.turn_failed
            last_progress_now = s.last_progress
            last_prompt_now = s.last_prompt
            last_text_now = s.last_text
            last_role_now = s.last_role
            last_message_at_now = s.last_message_at
            model_now = s.model
            context_now = s.context_tokens
            last_ev_now = None
            tp = None
            background_active = None
            if live_now:
                tp = transcript_paths.get(s.key)
                background_active = self._background_work_active(tp)
                activity_now, stalled_now, last_progress_now, failed_now = self._turn_state(
                    True, tp, _mtime(tp) if tp else None,
                    lifecycle_active=background_active,
                )
                activity_now = self._background_label(
                    tp, activity_now, background=background_active
                )
                if tp is not None:
                    # keep the list's messages as fresh as the detail tail — this
                    # is the mtime-cached meta, so idle transcripts cost nothing.
                    tmeta = self._cached_meta(tp)
                    last_prompt_now = tmeta.last_prompt or s.last_prompt
                    last_text_now = tmeta.last_text or s.last_text
                    last_role_now = tmeta.last_role or s.last_role
                    # Unlike the display text fallbacks above, absence is an
                    # authoritative answer for this paired field: compaction
                    # clears the current message, and a timestamp-less newest
                    # message must not inherit the previous message's clock.
                    last_message_at_now = tmeta.last_message_at
                    model_now = tmeta.model or s.model
                    last_ev_now = self._cached_last_event(tp)  # cache hit (_activity read it)
                    context_now = self._cached_context_tokens(tp)
            if last_ev_now is not None and last_ev_now.question:
                # Unanswered AskUserQuestion → surface its prompt (see scan_sessions).
                question_now = last_ev_now.question
            elif live_now:
                question_now = (
                    transcripts_mod.trailing_question(last_text_now)
                    if last_role_now == "agent"
                    else None
                )
            else:
                # Idle: no fresh read this sweep — keep whatever the last full scan
                # surfaced rather than recomputing from stale text.
                question_now = s.question
            thinking_now = activity_now is not None
            pid_now = entry.pid if (entry and live_now) else None
            deep_link_now = (
                registry_mod.session_deep_link(entry.pid) if (entry and live_now) else None
            )
            s.status = status_now
            s.thinking = thinking_now
            s.stalled = stalled_now
            s.turn_failed = failed_now and not thinking_now
            s.lifecycle_active = background_active
            s.activity = activity_now
            # Same rule as the full scan: the newest real event is the activity,
            # the file write only its fallback (#335).
            if tp is not None:
                s.last_activity = last_progress_now or _mtime(tp)
            s.last_progress = last_progress_now
            s.last_prompt = last_prompt_now
            s.last_text = last_text_now
            s.last_role = last_role_now
            s.last_message_at = last_message_at_now
            s.model = model_now
            if last_ev_now is not None:
                s.last_event_seq = last_ev_now.seq
            s.question = question_now
            s.context_tokens = context_now
            s.pid = pid_now
            s.deep_link = deep_link_now
            s.show_when_idle = False
            capabilities = set(s.capabilities)
            capabilities.difference_update(CONTROL_CAPABILITIES)
            s.capabilities = frozenset(capabilities)
            s.kind = entry.kind if entry else s.kind
            self._project_owned_worker(
                account,
                s,
                transcript_path=tp,
                last_write=_mtime(tp) if tp is not None else s.last_activity,
            )
            # An idle row has no fresh read: it is not working, so nothing opened a turn.
            self._note_turn_opener(s, tp)

        return state.apply_session_changes(targets, project)

    # --- usage ---------------------------------------------------------

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await fetch_usage_once(account, client)
        except Exception as exc:  # noqa: BLE001 — one-shot helper, never fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return ClaudeUsagePoller(
            account,
            state,
            interval_s=kwargs.get("interval_s", 300.0),
            cache_dir=kwargs.get("cache_dir"),
        )

    # --- transcripts (v0.2) --------------------------------------------

    def _transcript_path(
        self, account: Account, session: Session, *, tolerate_stale_miss: bool = False
    ) -> Path | None:
        """Resolve a session id to its transcript file.

        ``tolerate_stale_miss`` lets a caller reuse a recent failed resolve
        instead of walking the project tree again. Only the liveness sweep sets
        it; see ``MISSING_TRANSCRIPT_TTL_S``.
        """
        key = (str(account.root), session.session_id)
        cached = self._paths.get(key)
        if cached is not None and cached.is_file():
            return cached
        if tolerate_stale_miss and self._miss_is_fresh(key):
            return None
        projects = account.root / "projects"
        if not projects.is_dir():
            return None
        matches = sorted(
            [
                *projects.glob(f"*/{session.session_id}.jsonl"),
                # subagent (isSidechain) transcripts live one level deeper
                *projects.glob(f"*/*/subagents/{session.session_id}.jsonl"),
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            self._paths.pop(key, None)
            self._remember_miss(key)
            return None
        self._missing_paths.pop(key, None)
        self._paths[key] = matches[0]
        return matches[0]

    def _miss_is_fresh(self, key: tuple[str, str]) -> bool:
        last = self._missing_paths.get(key)
        return last is not None and time.monotonic() - last < MISSING_TRANSCRIPT_TTL_S

    def _remember_miss(self, key: tuple[str, str]) -> None:
        now = time.monotonic()
        if len(self._missing_paths) >= MISSING_TRANSCRIPT_CACHE_MAX:
            self._missing_paths = {
                k: seen
                for k, seen in self._missing_paths.items()
                if now - seen < MISSING_TRANSCRIPT_TTL_S
            }
        self._missing_paths[key] = now

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        """New renderable events with seq > after_seq (used by the live tail)."""
        path = self._transcript_path(account, session)
        if path is None:
            return []
        events = await asyncio.to_thread(self._read_transcript_file, path, after_seq)
        return HIDDEN.visible(session.key, events)

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
        events = self._transcript_cache.get(
            path, lambda item: transcripts_mod.read_events(item).events, []
        )
        return [event for event in events if event.seq > after_seq]

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        if HIDDEN.hides_any(session.key):
            # The tail read numbers no lines, so a leg a rollback cut (#3474) is read
            # whole: its newest line may be one the chat no longer holds.
            shown = await self.read_transcript(account, session)
            return shown[-1] if shown else None
        return await asyncio.to_thread(transcripts_mod.last_event, path)

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        if HIDDEN.hides_any(session.key):
            # As in ``last_event``: a leg a rollback cut is read whole (#3474).
            shown = await self.read_transcript(account, session)
            said = [e for e in shown if e.role in ("user", "assistant") and e.text]
            return transcript_reader_mod._dedupe_queued(said)[-limit:]
        return await asyncio.to_thread(transcripts_mod.recent_conversation, path, limit=limit)

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return (0, 0)
        return await asyncio.to_thread(transcripts_mod.transcript_cursor, path)

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return ([], byte_offset, seq)
        read = await asyncio.to_thread(
            transcripts_mod.read_events, path, byte_offset=byte_offset, seq=seq
        )
        return (HIDDEN.visible(session.key, read.events), read.byte_offset, read.seq)

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        """Full detail bundle. ``before_seq`` returns the window ending just
        before that seq (for "load earlier"); otherwise the most recent window."""
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
        return await asyncio.to_thread(
            self._load_transcript_file,
            account.root,
            session.session_id,
            path,
            before_seq,
            session.key,
        )

    def _load_transcript_file(
        self,
        root: Path,
        session_id: str,
        path: Path,
        before_seq: int | None,
        session_key: str = "",
    ) -> TranscriptDetail:
        """Build a detail window without blocking the caller's event loop."""
        if before_seq is None and not HIDDEN.hides_any(session_key):
            # The common open path: the cached, incrementally-resumed tail.
            tail = self._tail_cache.read_tail(path)
            return TranscriptDetail(
                events=tail.events,
                tokens=tail.tokens,
                    model=tail.model,
                todos=transcripts_mod.load_todos(root, session_id),
                total_events=tail.total_events,
                earliest_seq=tail.events[0].seq if tail.events else 0,
                skipped=tail.skipped,
            )
        # "Load earlier" needs events before the cached tail; reparse fully. So does a
        # leg a rollback cut (#3474): its newest window may be all removed lines.
        read = transcripts_mod.read_events(path)
        all_events = HIDDEN.visible(session_key, read.events)
        events = [
            event for event in all_events if before_seq is None or event.seq < before_seq
        ]
        window = events[-DETAIL_WINDOW:]
        return TranscriptDetail(
            events=window,
            tokens=transcripts_mod.token_totals(all_events),
            model=transcripts_mod.last_model(all_events),
            todos=transcripts_mod.load_todos(root, session_id),
            total_events=len(all_events),
            earliest_seq=window[0].seq if window else 0,
            skipped=read.skipped,
        )
