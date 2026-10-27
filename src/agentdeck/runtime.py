"""Long-lived Codex control plane, separate from the restartable web UI."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import run_execution, runtime_feed
from . import work_workspace_claims as work_claims
from .command_artifacts import ArtifactError, ArtifactStore
from .config import AppConfig, owned_session_base_url
from .db import Db, open_db
from .delivery_scheduling import (
    ROUTING_UNAVAILABLE,
    Placement,
    Selector,
    deliver_scheduled,
)
from .ensure_delivery import (
    ENSURE_DELIVERY_CONTRACT,
    ENSURE_DELIVERY_PATH,
    PRIOR_RUN_EXECUTING,
    EnsureDeliveryRequest,
    ensure_delivery,
    resumes_behind_its_chat,
)
from .flaky_quarantine import FILENAME as QUARANTINE_FILENAME
from .flaky_quarantine import Quarantine
from .host_slots import MemoryGate, describe_heavy_slots, heavy_slot_paths
from .jobs import LOCAL as JOB_LANE_LOCAL
from .jobs import QUEUED as JOB_QUEUED
from .jobs import RUNNING as JOB_RUNNING
from .jobs import Job, JobError, JobQueue, NotifyOutcome, configure_jobs_slice, rows_for_work
from .models import (
    SNAPSHOT_CAPABILITIES_KEY,
    Account,
    ForkFrom,
    InjectResult,
)
from .providers import PROVIDERS
from .providers.base import unknown_account_detail
from .providers.claude_code.worker import ClaudeWorkerHost
from .providers.codex.appserver import CodexAppServer
from .providers.codex.runtime_client import runtime_socket_path
from .providers.codex.worker import CodexWorkerHost
from .providers.delivery import (
    DeliverResult,
    DeliveryCorrelation,
    KeyedWorkerHost,
    SessionDeliveryFence,
    delivery_participated,
)
from .providers.instructions import SessionRole, resolve_session_role
from .providers.usage import shared_cache_dir
from .restart_nudge import (
    NUDGE_MESSAGE,
    NudgeEntry,
    RestartNudgeRegistry,
    session_key_parts,
)
from .revival import (
    BACKGROUND,
    CUT_OFF,
    LOOP_ATTENTION_DETAIL,
    RevivalLedger,
    RevivalRecord,
    RuntimeExitMarker,
    SessionCandidate,
    Survivor,
    find_session_survivors,
    find_survivors,
    late_revival_message,
    revival_looped,
    revival_message,
    revival_paths,
)
from .session_secrets import SessionSecretStore
from .slot_leases import LeaseError, SlotLeases
from .stray_db import merge_stray_db

log = logging.getLogger(__name__)

# How often to look for workers to collect. Well below any sane idle_after_s, so
# the ceiling is what decides a worker's fate rather than the sweep's phase.
IDLE_COLLECT_INTERVAL_S = 300.0
# How often each Codex app-server is asked to let go of idle threads (#2643).
IDLE_UNLOAD_INTERVAL_S = 60.0
ARTIFACT_CLEANUP_INTERVAL_S = 3600.0
# How often a Work workspace whose release is still pending (a live worker, a dirty
# tree) is tried again (#2149). Nothing is lost by waiting; a person can see it pending.
WORK_WORKSPACE_RELEASE_RETRY_S = 900.0
# How often every Run still recorded as executing is observed again (#2586). A worker reports
# from inside its turn, so nothing else re-reads a Run once its report has moved the card.
RUN_OUTCOME_REFRESH_S = 30.0
# How often a boot delivery an account refused for want of a slot is tried again,
# until the restart-nudge window closes (#3009). Before this a refused revival waited
# for the next restart, and a person's chat sat unanswered behind it.
REFUSED_BOOT_DELIVERY_RETRY_S = 30.0
# The change-feed key of a Claude account's owed revivals, beside its worker keys (#3008).
# A tuple, so no worker key (always a string) can collide with it.
REVIVAL_FEED_KEY = ("revival",)
# What a queued boot message's chat did since boot, read before each retry.
_UNTOUCHED = "untouched"
_BUSY = "busy"
_MOVED_ON = "moved_on"
# The worker host's refusal when every account-local slot is busy.
AT_CAPACITY = "at_capacity"


def _nudge_result(result: Any) -> str:
    """The outcome of a fenced delivery: its action, or why it was refused.

    Text for a log line, and only that.
    """
    if result.accepted:
        return result.action
    return f"rejected:{result.reason}"


def _refused_for_capacity(result: Any) -> bool:
    """Refused for want of a slot, with nothing written: safe to try again."""
    return (
        not getattr(result, "accepted", False)
        and not _wrote_to_session(result)
        and getattr(result, "reason", None) == AT_CAPACITY
    )


@dataclass
class _RefusedBootDelivery:
    """One boot message an account turned away at the cap, kept to try again."""

    session_key: str
    message: str
    delivery_id: str
    # "revival" or "restart-nudge", for the log lines.
    kind: str
    # Set for a revival, which is recorded in the ledger once it lands.
    survivor: Survivor | None = None
    # What a revival says instead once another message reached the chat first
    # (#3073). None for a restart nudge, which that other message makes moot.
    late_message: str | None = None


def _keyed_session_keys(
    snapshots: Mapping[tuple[str, str], Mapping[str, Any]],
) -> set[str]:
    """Every session a keyed worker record covers, from the same boot snapshot."""
    keys: set[str] = set()
    for (provider_id, label), snapshot in snapshots.items():
        workers = snapshot.get("workers") if isinstance(snapshot, Mapping) else None
        if not isinstance(workers, Mapping):
            continue
        for worker in workers.values():
            session_id = worker.get("session_id") if isinstance(worker, Mapping) else None
            if isinstance(session_id, str) and session_id.strip():
                keys.add(f"{provider_id}:{label}:{session_id.strip()}")
    return keys


def _epoch(value: datetime | None) -> float:
    """A provider timestamp as seconds, or 0.0 when the chat never moved."""
    return value.timestamp() if value is not None else 0.0


def _wrote_to_session(result: Any) -> bool:
    """Whether this delivery may have put a message into the chat (#1547).

    An acceptance is a write. A refusal only proves *nothing* was written when
    the provider never started one, which is what ``provider_write_state`` says:
    anything other than ``not_started`` may already have landed, so no second
    path may write into that chat on the same boot.
    """
    if getattr(result, "accepted", False):
        return True
    return getattr(result, "provider_write_state", "not_started") != "not_started"


class ActionRequest(BaseModel):
    client_action_id: str | None = None


class SessionSecretRequest(ActionRequest):
    session_key: str
    name: str
    value: str = Field(repr=False)


class SessionSecretClaimRequest(BaseModel):
    ticket: str


class JobSubmitRequest(BaseModel):
    command: list[str]
    cwd: str
    # Where to run it: this host, or a dev-cluster pod (#1368). Defaulted rather
    # than required, because a CLI older than the lane still submits without it.
    lane: str = JOB_LANE_LOCAL
    # True when the CLI picked the lane because the submitter named none, which
    # is what lets a full dev cluster move the job to the Mac (#2170).
    lane_defaulted: bool = False
    # The chat to tell when the job ends. Optional: a person at a terminal has
    # no chat, and `agentdeck job submit --wait` watches the job itself.
    session_key: str | None = None
    label: str = ""
    # An explicit verification job and the Work/Run it is evidence for (#2154).
    purpose: str = ""
    work_id: str = ""
    run_id: str = ""
    # Other checkouts a verification job also tests (#2148).
    workspaces: list[str] = Field(default_factory=list, max_length=16)
    # How long it may run (#3038): None lets the queue learn it from the project, 0 is no limit.
    max_minutes: int | None = Field(default=None, ge=0)


class WorkWorkspaceReleaseRequest(BaseModel):
    work_id: str
    # The paths the caller would otherwise remove itself; omitted means every claim.
    paths: list[str] | None = None


class SlotLeaseRequest(BaseModel):
    """An ask for one of the host's heavy slots.

    Everything is optional: a caller that just wants a slot on the default terms
    posts an empty body. ``label`` is for the operator reading the deck, not for
    the pool — nothing branches on it.
    """

    label: str = ""
    # None means the pool's default; anything above its ceiling is clamped, not
    # refused, so a caller asking for too long still gets a working lease.
    ttl_s: float | None = None
    # How long to hold this request open waiting for a slot. Zero — the default
    # — answers immediately, which is what a caller with its own retry wants.
    wait_s: float = 0.0


class TurnRequest(ActionRequest):
    thread_id: str
    message: str
    images: list[str] = Field(default_factory=list)
    effort: str | None = None


class ThreadRequest(ActionRequest):
    thread_id: str


class PermissionPromptsRequest(ThreadRequest):
    enabled: bool


class StartRequest(ActionRequest):
    cwd: str
    message: str
    images: list[str] = Field(default_factory=list)
    sandbox: str | None = None
    model: str | None = None
    effort: str | None = None
    approval_policy: str | None = None
    # Direct runtime callers are machine dispatchers unless an AgentDeck
    # primary-chat provider explicitly says otherwise.
    delegated: bool = True
    # Deck-minted self-identification token for the thread being started.
    session_token: str | None = None
    # Start the thread as a copy of this one through turn ``fork_turn_id`` (#2485).
    fork_thread_id: str | None = Field(default=None, max_length=256)
    fork_turn_id: str | None = Field(default=None, max_length=256)


class AnswerRequest(ActionRequest):
    thread_id: str
    interaction_id: str
    answers: dict[str, list[str]] = Field(default_factory=dict)
    decision: str | None = None


def _result(result: InjectResult) -> dict[str, Any]:
    return result.to_wire()


class DeliverRequest(ActionRequest):
    key: str
    message: str
    cwd: str | None = None
    fresh: bool = False
    images: list[str] = Field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    delivery_id: str | None = None
    allow_over_budget: bool = False
    require_idle: bool = False
    require_active: bool = False
    expected_last_result_at: float | None = None
    expected_session_id: str | None = None
    context_maintenance: bool = False
    context_rollover: bool = False
    # Direct runtime callers are machine dispatchers unless an AgentDeck
    # primary-chat provider explicitly says otherwise.
    delegated: bool = True
    role: SessionRole | None = None
    # Deck-minted self-identification token for a freshly spawned session.
    session_token: str | None = None
    command_artifacts: dict[str, Any] | None = None
    # Start the fresh session as a copy of this one, cut after ``fork_at`` (#2485).
    fork_session_id: str | None = Field(default=None, max_length=256)
    fork_at: str | None = Field(default=None, max_length=256)
    # Which Work and Run this delivery belongs to (#2142). Machine metadata: frozen onto the
    # delivery receipt before any provider write and echoed back, never put in the prompt.
    # Both or neither; half a correlation is ignored.
    work_id: str | None = Field(default=None, max_length=512)
    run_id: str | None = Field(default=None, max_length=256)


class ArtifactSliceRequest(BaseModel):
    session_key: str = Field(default="", max_length=512)
    card: str = Field(default="", max_length=512)
    session_token: str = Field(default="", max_length=512, repr=False)
    slice_capability: str = Field(default="", max_length=512, repr=False)
    stream: str
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=64 * 1024, gt=0, le=1024 * 1024)


class ArtifactFullRequest(BaseModel):
    session_key: str = Field(min_length=1, max_length=512)
    card: str = Field(min_length=1, max_length=512)
    session_token: str = Field(min_length=16, max_length=512, repr=False)


class SpareRequest(BaseModel):
    """A booted Claude CLI to keep waiting for one kind of new chat (#3420)."""

    shape: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=128)
    session_token: str = Field(min_length=1, max_length=128)
    cwd: str
    model: str | None = None
    effort: str | None = None
    # A deck-started Work's spare boots with the ``work`` role (#3590); None is a plain chat.
    role: SessionRole | None = None


class WorkerKeyRequest(ActionRequest):
    # Keys are opaque and may contain '#' or '/' (e.g. "owner/repo#12"), which
    # are unsafe in a URL path — always carry the key in the body.
    key: str


class ContextDeliveryResolutionRequest(WorkerKeyRequest):
    delivery_id: str
    choice: Literal["accepted", "not_written", "abandoned"]
    rationale: str


class WorkerBypassPermissionsRequest(WorkerKeyRequest):
    enabled: bool


class WorkerRewindRequest(WorkerKeyRequest):
    # The transcript line the chat's next turn continues from (#3474).
    at: str = Field(min_length=1)
    # The chat's legs, and when its removed part began: their jobs from then on are
    # cancelled in the same call, unannounced.
    session_keys: list[str] = Field(default_factory=list, max_length=64)
    since: float | None = None


class ClaudeBlockRequest(BaseModel):
    """A tool call Claude's classifier blocked, reported by the worker's own
    PermissionDenied hook (#2271). Addressed by the account key and worker key
    the runtime put in that worker's environment."""

    account_key: str
    key: str
    session_token: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class WorkerSessionReviveRequest(ActionRequest):
    session_id: str
    message: str
    images: list[str] = Field(default_factory=list)
    effort: str | None = None
    delivery_id: str | None = None
    expected_last_result_at: float


class WorkerAnswerRequest(ActionRequest):
    key: str
    interaction_id: str
    answers: dict[str, list[str]] = Field(default_factory=dict)
    decision: str | None = None


class ClaudeWorkerRuntime:
    """Own deck-managed Claude worker hosts, one per claude_code account."""

    def __init__(self, config: AppConfig, budget_db: Db) -> None:
        self.settings = config.claude_workers
        self._base_url = owned_session_base_url(config)
        self._artifact_root = config.keyed_workers.state_path / "command-artifacts"
        self._usage_cache_dir = shared_cache_dir(config.usage.shared_cache_dir)
        self._budget_db = budget_db
        self.accounts = {
            account.label: account
            for account in config.build_accounts()
            if account.provider_id == "claude_code"
        }
        self.hosts: dict[str, ClaudeWorkerHost] = {}
        # Told of each host as it is created, so the change feed can watch it (#2865).
        self.on_host: Callable[[str, ClaudeWorkerHost], None] = lambda _label, _host: None
        self._collect_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Begin collecting idle workers on a timer.

        A worker is only ever reclaimed under admission pressure, which never
        arrives for a kanban root (it holds no account-local slot). Without a timer
        nothing collects one at all — 39 of them sat live for up to 20 days holding
        ~300 MB each (#1226).
        """
        if self._collect_task is None and self.settings.enabled:
            self._collect_task = asyncio.create_task(
                self._collect_idle_forever(), name="claude-worker-idle-collect"
            )
        if self.settings.enabled:
            await self.reattach_held_workers()

    async def reattach_held_workers(self) -> None:
        """Reconnect to the workers the last runtime left running under holders (#3248).

        Before anything revives a chat, so a reconnected one reads as live, not cut off.
        Only accounts that left holders behind are loaded, as the idle sweep does.
        """
        for label in self.accounts:
            state_path = self.settings.for_account(label).state_path
            if not (state_path / f"{label}.holders.json").exists():
                continue
            try:
                await self.host(label).reattach()
            except Exception:  # noqa: BLE001 -- one account must not stop the boot
                log.exception("could not reattach held workers for %s", label)

    async def _collect_idle_forever(self) -> None:
        while True:
            await asyncio.sleep(IDLE_COLLECT_INTERVAL_S)
            # Only hosts already loaded: a host with live workers is loaded by
            # definition, and touching the rest would spawn state for accounts
            # this runtime has never used.
            for host in list(self.hosts.values()):
                try:
                    await host.collect_idle()
                except Exception:  # one bad host must not end the sweep
                    log.exception("idle collection failed for %s", host.account.key)

    def host(self, label: str) -> ClaudeWorkerHost:
        if not self.settings.enabled:
            raise HTTPException(status_code=404, detail="claude workers are disabled")
        account = self.accounts.get(label)
        if account is None:
            raise HTTPException(
                status_code=404,
                detail=unknown_account_detail("Claude", label, self.accounts),
            )
        host = self.hosts.get(label)
        if host is None:
            effective = self.settings.for_account(label)
            host = ClaudeWorkerHost(
                account,
                state_dir=effective.state_path,
                max_workers=effective.max_workers,
                permission_mode=effective.permission_mode or None,
                model=effective.model or None,
                usage_cache_dir=self._usage_cache_dir,
                stall_after_s=effective.stall_after_s,
                idle_after_s=effective.idle_after_s,
                interactive_prompts=effective.interactive_prompts,
                classifier_skip_rules=list(self.settings.classifier_skip_rules),
                memory_max=effective.memory_max,
                hold_processes=effective.hold_across_restarts,
                base_url=self._base_url,
                bind_session_token=self._budget_db.record_session_token,
                artifact_root=self._artifact_root,
            )
            self.hosts[label] = host
            self.on_host(label, host)
        return host

    def active_turn_count(self) -> int:
        """Number of workers with a turn or Claude-native background agent active."""
        return sum(
            bool(worker.get("effective_activity", worker.get("turn_active")))
            for host in self.hosts.values()
            for worker in host.snapshot().get("workers", {}).values()
        )

    def turns_a_restart_would_cut(self) -> int:
        """Claude workers mid-turn that a restart would end: those without a holder."""
        return sum(host.turns_a_restart_would_cut() for host in self.hosts.values())

    def active_session_keys(self) -> set[str]:
        """Full keys of Claude chats that must resume after a runtime restart."""
        keys: set[str] = set()
        for label, host in self.hosts.items():
            for worker_key, worker in host.snapshot().get("workers", {}).items():
                if not worker.get("effective_activity", worker.get("turn_active")):
                    continue
                session_id = worker.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError(
                        f"active Claude worker {label}:{worker_key} has no session id"
                    )
                keys.add(f"claude_code:{label}:{session_id}")
        return keys

    def all_hosts(self) -> dict[str, ClaudeWorkerHost]:
        """Load every configured account so global admission sees all active workers."""
        if not self.settings.enabled:
            return {}
        return {label: self.host(label) for label in self.accounts}

    async def stop(self) -> None:
        if self._collect_task is not None:
            self._collect_task.cancel()
            await asyncio.gather(self._collect_task, return_exceptions=True)
            self._collect_task = None
        await asyncio.gather(
            *(host.stop() for host in self.hosts.values()), return_exceptions=True
        )
        self.hosts.clear()


async def _timed_action[ActionResult: (InjectResult, DeliverResult)](
    action: str,
    label: str,
    client_action_id: str | None,
    operation: Callable[[], Awaitable[ActionResult]],
) -> ActionResult:
    started = time.perf_counter_ns()
    try:
        result = await operation()
    except BaseException:
        if client_action_id:
            log.debug(
                "runtime_action action_id=%s action=%s account=%s outcome=error elapsed_ms=%.1f",
                client_action_id,
                action,
                label,
                (time.perf_counter_ns() - started) / 1_000_000,
            )
        raise
    if client_action_id:
        log.debug(
            "runtime_action action_id=%s action=%s account=%s accepted=%s elapsed_ms=%.1f",
            client_action_id,
            action,
            label,
            result.accepted,
            (time.perf_counter_ns() - started) / 1_000_000,
        )
    return result


class CodexRuntime:
    """Own all app-server processes and pending interactions across web deploys."""

    def __init__(self, config: AppConfig, budget_db: Db) -> None:
        self.accounts = {
            account.label: account
            for account in config.build_accounts()
            if account.provider_id == "codex"
        }
        self._base_url = owned_session_base_url(config)
        self._artifact_root = config.keyed_workers.state_path / "command-artifacts"
        self._backend_scope = str(runtime_socket_path().resolve())
        self._persistent_backend = config.keyed_workers.persistent_backend
        self._budget_db = budget_db
        self.clients: dict[str, CodexAppServer] = {}
        self._unload_task: asyncio.Task | None = None
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._worker_change: Callable[[str, str], None] = lambda _label, _thread: None
        self._change_listeners: list[Callable[[str, str], None]] = []
        self._session_deliveries = SessionDeliveryFence(
            config.keyed_workers.state_path / "codex-session-deliveries.json"
        )

    async def deliver_to_session(
        self,
        label: str,
        thread_id: str,
        message: str,
        *,
        images: list[Path],
        effort: str | None,
        operation: str,
        delivery_id: str | None,
        send: Callable[[], Awaitable[InjectResult]],
    ) -> DeliverResult:
        """Fence one session-addressed Codex write by its client action id."""
        return await self._session_deliveries.deliver(
            label,
            thread_id,
            message,
            images=images,
            effort=effort,
            operation=operation,
            delivery_id=delivery_id,
            send=send,
        )

    def set_worker_change_callback(self, callback: Callable[[str, str], None]) -> None:
        self._worker_change = callback

    def add_change_listener(self, callback: Callable[[str, str], None]) -> None:
        """Also tell ``callback(label, thread_id)`` of every thread change (#2865)."""
        self._change_listeners.append(callback)

    def _thread_changed(self, label: str, thread_id: str) -> None:
        self._worker_change(label, thread_id)
        for listener in self._change_listeners:
            listener(label, thread_id)

    async def start(self) -> None:
        try:
            for label, account in self.accounts.items():
                client = CodexAppServer(
                    account,
                    base_url=self._base_url,
                    backend_scope=self._backend_scope,
                    persistent_backend=self._persistent_backend,
                    on_change=lambda thread_id, account_label=label: self._thread_changed(
                        account_label, thread_id
                    ),
                    permission_prompts_allowed=lambda thread_id, account=account: (
                        self._budget_db.permission_prompts_allowed(
                            f"{account.key}:{thread_id}"
                        )
                    ),
                    artifact_root=self._artifact_root,
                )
                await client.start()
                self.clients[label] = client
        except Exception:
            await self.stop()
            raise
        if self._unload_task is None:
            self._unload_task = asyncio.create_task(
                self._unload_idle_forever(), name="codex-idle-thread-unload"
            )

    async def _unload_idle_forever(self) -> None:
        """Let each account's app-server unload threads nobody is using (#2643).

        The Claude-worker idle collector's counterpart (#1226): without it every
        thread a run ever touched stays loaded until its file limit is gone.
        """
        while True:
            await asyncio.sleep(IDLE_UNLOAD_INTERVAL_S)
            for client in list(self.clients.values()):
                try:
                    await client.unload_idle()
                except Exception:  # one bad account must not end the sweep
                    log.exception("idle thread unload failed for %s", client.account.key)

    async def stop(self) -> None:
        if self._unload_task is not None:
            self._unload_task.cancel()
            await asyncio.gather(self._unload_task, return_exceptions=True)
            self._unload_task = None
        cleanup = list(self._cleanup_tasks)
        for task in cleanup:
            task.cancel()
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
            self._cleanup_tasks.clear()
        await asyncio.gather(
            *(client.stop() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()

    def defer_upload_cleanup(
        self, client: CodexAppServer, thread_id: str, root: Path
    ) -> None:
        """Keep runtime-owned image copies until the accepted turn finishes."""

        async def cleanup() -> None:
            try:
                await client.wait_for_thread(thread_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                shutil.rmtree(root, ignore_errors=True)

        task = asyncio.create_task(cleanup(), name=f"upload-cleanup:{thread_id}")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def client(self, label: str) -> CodexAppServer:
        client = self.clients.get(label)
        if client is None:
            # Name what the runtime does know: renaming a label and restarting
            # only one service splits the namespace, and a bare 404 reads as
            # "runtime down" instead of "restart both services" (#788).
            raise HTTPException(
                status_code=404,
                detail=unknown_account_detail("Codex", label, self.clients),
            )
        return client

    async def set_permission_prompts(
        self, label: str, thread_id: str, enabled: bool
    ) -> InjectResult:
        account = self.accounts[label]
        session_key = f"{account.key}:{thread_id}"
        client = self.client(label)
        was_enabled = self._budget_db.permission_prompts_allowed(session_key)
        previous_policy = self._budget_db.permission_prompts_previous_policy(session_key)
        if enabled:
            previous_policy = await client.current_approval_policy(thread_id)
            if previous_policy is None:
                return InjectResult(False, "Codex approval policy is not available yet")
        result = await client.set_permission_prompts(
            thread_id, enabled, previous_policy=previous_policy
        )
        if result.accepted:
            persisted = self._budget_db.set_permission_prompts(
                session_key, enabled, previous_policy
            )
            if not persisted:
                restored = await client.set_permission_prompts(
                    thread_id,
                    was_enabled,
                    previous_policy=previous_policy,
                )
                reason = "could not save permission prompt setting"
                if not restored.accepted:
                    reason += "; could not restore the prior runtime setting"
                return InjectResult(False, reason)
        return result

    async def nudge_session(
        self, label: str, thread_id: str, delivery_id: str, message: str = NUDGE_MESSAGE
    ) -> DeliverResult:
        """Queue one fenced message onto a runtime-owned Codex thread.

        ``message`` defaults to the restart nudge this path was built for; the
        job queue passes its own completion notice down the same fence.
        """
        client = self.clients.get(label)
        if client is None:
            return DeliverResult(
                False, "rejected", reason=f"unknown Codex account {label!r}"
            )
        return await self.deliver_to_session(
            label,
            thread_id,
            message,
            images=[],
            effort=None,
            operation="queue",
            delivery_id=delivery_id,
            send=lambda: client.queue_turn(thread_id, message),
        )

    def active_turn_count(self) -> int:
        """Number of Codex turns doing work rather than awaiting an interaction."""
        return sum(
            client.active_turn(thread_id) is not None
            and client.interaction(thread_id) is None
            for client in self.clients.values()
            for thread_id in client.owned_threads()
        )

    def active_session_keys(self) -> set[str]:
        """Full keys of working Codex threads, excluding chats awaiting input."""
        return {
            f"codex:{label}:{thread_id}"
            for label, client in self.clients.items()
            for thread_id in client.owned_threads()
            if client.active_turn(thread_id) is not None and client.interaction(thread_id) is None
        }

    def snapshot(self, label: str) -> dict[str, Any]:
        client = self.client(label)
        account = self.accounts[label]
        threads = {
            thread_id: self._thread_row(client, account, thread_id)
            for thread_id in sorted(client.owned_threads())
        }
        return {"threads": threads}

    def thread_row(self, label: str, thread_id: str) -> dict[str, Any] | None:
        """One owned thread's snapshot row, or None; for the change feed (#2865)."""
        client = self.clients.get(label)
        if client is None or not client.owns(thread_id):
            return None
        return self._thread_row(client, self.accounts[label], thread_id)

    def owned_threads(self, label: str) -> frozenset[str]:
        client = self.clients.get(label)
        return client.owned_threads() if client is not None else frozenset()

    def _thread_row(
        self, client: CodexAppServer, account: Account, thread_id: str
    ) -> dict[str, Any]:
        interaction = client.interaction(thread_id)
        return {
            "active_turn": client.active_turn(thread_id),
            "status": client.thread_status(thread_id),
            "interaction": asdict(interaction) if interaction is not None else None,
            "permission_prompts": self._budget_db.permission_prompts_allowed(
                f"{account.key}:{thread_id}"
            ),
            "permission_prompts_available": True,
        }


class CodexWorkerRuntime:
    """Lazily expose durable keyed workers over CodexRuntime's app-server clients."""

    def __init__(self, config: AppConfig, runtime: CodexRuntime) -> None:
        self.runtime = runtime
        self.settings = config.keyed_workers
        self.usage_cache_dir = shared_cache_dir(config.usage.shared_cache_dir)
        self.state_dir = config.keyed_workers.state_path / "codex"
        self.hosts: dict[str, CodexWorkerHost] = {}
        # Told of each host as it is created, so the change feed can watch it (#2865).
        self.on_host: Callable[[str, CodexWorkerHost], None] = lambda _label, _host: None
        runtime.set_worker_change_callback(self._thread_changed)

    def _thread_changed(self, label: str, thread_id: str) -> None:
        host = self.hosts.get(label)
        if host is not None:
            host.thread_changed(thread_id)

    def host(self, label: str) -> CodexWorkerHost:
        account = self.runtime.accounts.get(label)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown Codex account")
        host = self.hosts.get(label)
        if host is None:
            host = CodexWorkerHost(
                account,
                self.runtime.client(label),
                state_dir=self.state_dir,
                bind_session_token=self.runtime._budget_db.record_session_token,
                artifact_root=self.runtime._artifact_root,
            )
            self.hosts[label] = host
            self.on_host(label, host)
        return host

    def all_hosts(self) -> dict[str, CodexWorkerHost]:
        """Load every configured account so global admission sees all active workers."""
        return {label: self.host(label) for label in self.runtime.accounts}


# Host-wide admission for one chosen target, with the lock already held.
Admit = Callable[
    [str, str, DeliverRequest, Callable[[], Awaitable[DeliverResult]]], Awaitable[DeliverResult]
]


class KanbanCapacity:
    """Race-safe host-wide admission for active provider-neutral kanban roots."""

    def __init__(
        self,
        *,
        limit: int,
        workers: dict[str, Any],
    ) -> None:
        self.limit = limit
        # provider_id -> a worker runtime exposing ``all_hosts()``. Keyed rather
        # than one named argument per provider: the ceiling is host-wide and
        # provider-neutral by definition, so adding a provider must not mean
        # editing an admission signature (#722).
        self.workers = workers
        self._lock = asyncio.Lock()
        # An uncertain provider result may have started a turn that is not observable yet.
        # Retain that slot until positive activity, terminal, release, or forget evidence
        # resolves the ambiguity. Time alone is unsafe: a delayed write may still start.
        self._reservations: dict[tuple[str, str, str], float] = {}

    def _hosts(self) -> dict[tuple[str, str], KeyedWorkerHost]:
        return {
            (provider, label): host
            for provider, runtime in self.workers.items()
            for label, host in runtime.all_hosts().items()
        }

    @staticmethod
    def _active(
        hosts: dict[tuple[str, str], KeyedWorkerHost],
    ) -> set[tuple[str, str, str]]:
        return {
            (provider, label, key)
            for (provider, label), host in hosts.items()
            for key in host.active_role_keys("kanban")
        }

    def _reconcile_reservations(
        self,
        hosts: dict[tuple[str, str], KeyedWorkerHost],
        active: set[tuple[str, str, str]],
        *,
        now: float,
    ) -> None:
        for identity, reserved_at in list(self._reservations.items()):
            provider, label, key = identity
            if identity in active:
                self._reservations.pop(identity, None)
                continue
            host = hosts.get((provider, label))
            if host is None or host.role_last_result_at(key, "kanban") >= reserved_at:
                self._reservations.pop(identity, None)

    @staticmethod
    def _active_reason(host: KeyedWorkerHost, key: str) -> str:
        worker = host.snapshot().get("workers", {}).get(key, {})
        background_count = int(worker.get("background_agent_count") or 0)
        if background_count:
            return f"background_agents={background_count}"
        if worker.get("turn_active"):
            return "turn"
        return "active"

    def _holder_descriptions(
        self,
        hosts: dict[tuple[str, str], KeyedWorkerHost],
        active: set[tuple[str, str, str]],
        *,
        now: float,
    ) -> list[str]:
        holders = []
        for provider, label, key in sorted(active | set(self._reservations)):
            identity = (provider, label, key)
            if identity in active:
                reason = self._active_reason(hosts[(provider, label)], key)
            else:
                age_s = max(0.0, now - self._reservations[identity])
                reason = f"reservation={age_s:.0f}s"
            holders.append(f"{provider}:{label}:{key}({reason})")
        return holders

    async def deliver(
        self,
        provider: str,
        label: str,
        body: DeliverRequest,
        send: Callable[[], Awaitable[DeliverResult]],
    ) -> DeliverResult:
        async with self._lock:
            return await self._admit(provider, label, body, send)

    def hosts(self) -> dict[tuple[str, str], KeyedWorkerHost]:
        """Every account host a delivery can reach, keyed ``(provider, label)``."""
        return self._hosts()

    async def schedule(self, run: Callable[[Admit], Awaitable[Any]]) -> Any:
        """Hold admission while the scheduler selects, freezes and delivers (#2146).

        ``run`` receives the admission check itself, to call once it has chosen a target. Taking
        the choice and the slot under one lock is what keeps two deliveries from both being
        placed against the last free slot.
        """
        async with self._lock:
            return await run(self._admit)

    async def _admit(
        self,
        provider: str,
        label: str,
        body: DeliverRequest,
        send: Callable[[], Awaitable[DeliverResult]],
    ) -> DeliverResult:
        """Admission with the lock already held."""
        if resolve_session_role(delegated=body.delegated, role=body.role) != "kanban":
            return await send()

        identity = (provider, label, body.key)
        hosts = self._hosts()
        active = self._active(hosts)
        now = time.time()
        self._reconcile_reservations(hosts, active, now=now)
        host = hosts.get((provider, label))
        if host is None:
            # A provider whose runtime is disabled reports no hosts, so its
            # kanban roots cannot hold or be admitted to a slot; the caller's
            # own route already 404s an unknown one.
            return await send()
        if host.delivery_is_finalized(body.key, body.delivery_id):
            # A finalized replay returns its cached result and starts no new work.
            return await send()
        occupied = active | set(self._reservations)
        if identity not in occupied and len(occupied) >= self.limit:
            holders = self._holder_descriptions(hosts, active, now=now)
            log.warning(
                "at_capacity: refused %s:%s:%s; %d/%d kanban slots held by %s",
                provider,
                label,
                body.key,
                len(occupied),
                self.limit,
                ", ".join(holders) or "none (limit is zero)",
            )
            return DeliverResult(False, "rejected", reason="at_capacity")

        created_reservation = identity not in self._reservations
        reserved_at = self._reservations.setdefault(identity, time.time())
        result = await send()
        if (
            not result.accepted
            and created_reservation
            and self._reservations.get(identity) == reserved_at
        ):
            self._reservations.pop(identity, None)
        elif result.accepted:
            active_after = self._active(hosts)
            if identity in active_after or host.role_last_result_at(
                body.key, "kanban"
            ) >= reserved_at:
                self._reservations.pop(identity, None)
        return result

    async def release(
        self,
        provider: str,
        label: str,
        key: str,
        send: Callable[[], Awaitable[DeliverResult]],
        inactive: Callable[[], bool] | None = None,
    ) -> DeliverResult:
        """Serialize lifecycle release with admission and return any reserved slot."""
        async with self._lock:
            return await self.release_held(provider, label, key, send, inactive)

    async def release_held(
        self,
        provider: str,
        label: str,
        key: str,
        send: Callable[[], Awaitable[DeliverResult]],
        inactive: Callable[[], bool] | None = None,
    ) -> DeliverResult:
        """``release`` for a caller already holding the admission lock (#2159)."""
        result = await send()
        if result.accepted or (inactive is not None and inactive()):
            self._reservations.pop((provider, label, key), None)
        return result

    async def forget(
        self,
        provider: str,
        label: str,
        key: str,
        send: Callable[[], Awaitable[DeliverResult]],
        known: Callable[[], bool],
    ) -> DeliverResult:
        """Serialize lineage removal with admission."""
        async with self._lock:
            result = await send()
            if result.accepted or not known():
                self._reservations.pop((provider, label, key), None)
            return result


class RestartNudges:
    """Wake every chat this runtime's boot owes a message to (#453, #1547).

    Two sources, one pass, one delivery per chat:

    * the **registry** — chats that asked in advance to be woken (#453). Consumed
      once on runtime boot. Whatever happens to an individual entry — delivered,
      unknown session, refused, or raising — the registry ends up empty, so a chat
      is never nudged twice and a leftover entry cannot fire on an unrelated
      future restart. This is the only way to reach a chat that was deliberately
      *idle* across the restart, which no amount of scanning can infer.
    * the **survivors** — chats the last death interrupted, read off the durable
      worker records at boot with nothing registered in advance (#1547). See
      ``revival.py``.

    A chat reached by the registry is not also reached as a survivor: the pass
    tracks the keys it has already written to, so the two sources cannot put two
    messages into one chat.

    """

    def __init__(
        self,
        config: AppConfig,
        *,
        session_runtimes: dict[str, Any],
        workers: dict[str, Any],
        kanban_capacity: KanbanCapacity,
        budget_db: Any = None,
    ) -> None:
        self.registry = RestartNudgeRegistry(
            config.restart_nudge.path, ttl_s=config.restart_nudge.ttl_s
        )
        # Beside the registry rather than in their own config keys, so an instance
        # that isolates the registry path (the canary does) isolates these too.
        marker_path, ledger_path = revival_paths(config.restart_nudge.path)
        self.exit_marker = RuntimeExitMarker(marker_path)
        self.ledger = RevivalLedger(ledger_path)
        # None until begin_boot() reads the marker: unknown, which is reported as a
        # death rather than as a restart we cannot prove happened cleanly.
        self._previous_exit_clean: bool | None = None
        self._budget_db = budget_db
        # One identity for every nudge this boot delivers, so the at-most-once
        # fence deduplicates a repeat inside this process without ever swallowing
        # the next restart's nudge.
        self._boot_stamp = time.time()
        # provider_id -> app-server runtime for dashboard-started sessions with
        # no worker key. Each exposes nudge_session(label, session_id, entry).
        self._session_runtimes = session_runtimes
        self._workers = workers
        self._kanban_capacity = kanban_capacity
        # Whether a worker row's Run has had a stop requested (#2159). Set once the Run
        # executions exist; a keyed delivery that names no Run of its own is refused for
        # such a worker, so a revival or a notice cannot resume a Run the Runtime stopped.
        self.stop_fence: Callable[[object], bool] | None = None
        # Boot messages an account refused at its cap, by session key, tried again
        # every ``retry_interval_s`` until they land or the window closes (#3009).
        self._refused: dict[str, _RefusedBootDelivery] = {}
        self.retry_interval_s = REFUSED_BOOT_DELIVERY_RETRY_S
        # Whether this boot's survivor scan has run. Until it has, every cut-off chat is
        # still owed its revival; after it, only the ones in ``_refused`` are (#3008).
        self._revival_scanned = False
        # Told whenever ``revivals_due`` may have moved, so the change feed compares it:
        # the deck re-reads a workers snapshot only when the feed says it moved (#3008).
        self.on_revivals_change: Callable[[], None] | None = None

    def _revivals_changed(self) -> None:
        if self.on_revivals_change is not None:
            try:
                self.on_revivals_change()
            except Exception:  # noqa: BLE001 -- a feed mark must never stop a revival
                log.exception("revival: could not mark the change feed")

    def revivals_due(self) -> dict[str, Any]:
        """Which cut-off chats this runtime still owes a revival, for the deck (#3008).

        ``scanned`` false: the boot's scan has not run, so any chat cut off is still due.
        Once it has, ``due`` lists the session keys a full account turned away that are
        being tried again; any other cut-off chat (a worker that died on its own while the
        runtime stayed up, one the loop guard stopped, one past the window) gets nothing
        more from this runtime and is a person's to start again.
        """
        return {
            "scanned": self._revival_scanned,
            "due": sorted(
                key for key, pending in self._refused.items() if pending.kind == "revival"
            ),
        }

    def active_session_keys(self) -> list[str]:
        """Return every active owned chat that a runtime restart would interrupt."""
        keys: set[str] = set()
        for runtime in (*self._session_runtimes.values(), *self._workers.values()):
            active = getattr(runtime, "active_session_keys", None)
            if active is not None:
                keys.update(active())
        return sorted(keys)

    def begin_boot(self) -> None:
        """Read how the last runtime exited and mark this one running (#1547)."""
        try:
            self._previous_exit_clean = self.exit_marker.claim()
        except Exception:  # noqa: BLE001 -- boot must survive an unusable marker
            log.exception("revival: could not claim the exit marker")
            self._previous_exit_clean = None

    def end_boot(self) -> None:
        """Record that this runtime is exiting cleanly, from the lifespan teardown.

        Only a graceful stop reaches here. An OOM ``SIGKILL`` skips the teardown
        entirely, which is exactly what leaves the marker saying "running" and
        tells the next boot it is looking at a crash.
        """
        try:
            self.exit_marker.mark_clean()
        except Exception:  # noqa: BLE001 -- shutdown must not fail on a marker
            log.exception("revival: could not mark a clean exit")

    async def deliver_all(self) -> None:
        """Never raises: a bad registry must not take the runtime down with it."""
        try:
            entries = self.registry.consume()
        except Exception:  # noqa: BLE001 -- boot must survive an unreadable registry
            log.exception("restart-nudge: could not consume the registry")
            # Not a return: the survivor pass below needs nothing from the
            # registry, and an unreadable file must not stop it running.
            entries = []
        now = time.time()
        # Every chat this boot has actually written to. One message per chat, so a
        # registered chat that is also a cut-off survivor is not told twice — but
        # only a delivery that reached the chat counts. A stale entry we dropped,
        # or one the provider refused without starting a write, put no message
        # anywhere, and suppressing the survivor pass on it would leave a genuinely
        # cut-off chat down for the whole boot.
        delivered: set[str] = set()
        # Registrations an account refused at its cap. Queued only after the survivor
        # pass, so a chat that is also a cut-off survivor gets its revival instead.
        refused: list[NudgeEntry] = []
        for entry in entries:
            try:
                if entry.is_stale(now, self.registry.ttl_s):
                    log.info(
                        "restart-nudge: %s registered %.0fs ago is stale; dropping",
                        entry.session_key,
                        now - entry.registered_at,
                    )
                    continue
                outcome = await self._deliver(entry)
                if _wrote_to_session(outcome):
                    delivered.add(entry.session_key)
                elif _refused_for_capacity(outcome):
                    refused.append(entry)
                log.info(
                    "restart-nudge: %s -> %s",
                    entry.session_key,
                    _nudge_result(outcome),
                )
            except Exception:  # noqa: BLE001 -- one bad entry must not skip the rest
                # A delivery that raised is not a refusal: it may have written
                # before it failed. At-most-once wins over a second attempt down
                # a different path, so this chat is left to the next boot.
                delivered.add(entry.session_key)
                log.exception("restart-nudge: failed to nudge %s", entry.session_key)
        try:
            self.reconcile_run_bindings()
            await self.revive_survivors(delivered)
        finally:
            self._revival_scanned = True
            self._revivals_changed()
        for entry in refused:
            if entry.session_key in delivered or entry.session_key in self._refused:
                continue
            self._refused[entry.session_key] = _RefusedBootDelivery(
                session_key=entry.session_key,
                message=NUDGE_MESSAGE,
                delivery_id=entry.delivery_id,
                kind="restart-nudge",
            )
        await self.retry_refused()

    async def retry_refused(self) -> None:
        """Try the boot messages an account refused at its cap until each lands (#3009).

        A slot frees when a turn ends or an idle worker is reclaimed, and nothing
        announces it, so this simply asks again every ``retry_interval_s``. The same
        delivery id goes every time, and only a refusal that wrote nothing is kept,
        so a retry can never put a second copy into a chat. It gives up when the
        restart-nudge window closes; a later restart scans for survivors afresh.
        Never raises.
        """
        deadline = self._boot_stamp + self.registry.ttl_s
        while self._refused:
            await asyncio.sleep(self.retry_interval_s)
            if time.time() >= deadline:
                for pending in self._refused.values():
                    log.warning(
                        "%s: gave up on %s; still refused at capacity after %.0fs",
                        pending.kind,
                        pending.session_key,
                        self.registry.ttl_s,
                    )
                self._refused.clear()
                self._revivals_changed()
                return
            for session_key, pending in list(self._refused.items()):
                since_boot = self._since_boot(session_key)
                message = pending.message
                if since_boot != _UNTOUCHED and pending.late_message is None:
                    # A newer message woke the chat, which is all a restart nudge
                    # asks for.
                    del self._refused[session_key]
                    log.info(
                        "%s: %s moved on since boot; dropping the queued message",
                        pending.kind,
                        session_key,
                    )
                    continue
                if since_boot == _BUSY:
                    # Another message woke it and that turn is still running.
                    # Written now, the note would sit behind it in the CLI's queue
                    # and land at a random point; it waits for the turn to end.
                    continue
                if since_boot == _MOVED_ON:
                    # The boot text would call that newer message part of the
                    # restart (#3009), and dropping it left the chat never told
                    # why its turn stopped (#3073).
                    message = pending.late_message or message
                try:
                    outcome = await self.deliver_to_session(
                        session_key,
                        message=message,
                        delivery_id=pending.delivery_id,
                    )
                except Exception:  # noqa: BLE001 -- one bad chat must not skip the rest
                    # It may have written before it failed: never again from here.
                    log.exception("%s: retry failed for %s", pending.kind, session_key)
                    del self._refused[session_key]
                    continue
                if _refused_for_capacity(outcome):
                    continue
                del self._refused[session_key]
                log.info(
                    "%s: %s (retried%s) -> %s",
                    pending.kind,
                    session_key,
                    ", late" if message is not pending.message else "",
                    _nudge_result(outcome),
                )
                if outcome.accepted and pending.survivor is not None:
                    self._record_revival(pending.survivor, pending.delivery_id)
            # Whatever left ``_refused`` this round is no longer owed (#3008).
            self._revivals_changed()

    def reconcile_run_bindings(self) -> int:
        """Restore session↔Run bindings a death kept the web side from writing (#2142).

        The web process binds a session to its delivery's Work and Run when the runtime
        answers. A death between the worker's durable receipt and that answer loses the
        binding but not the receipt, so each worker's latest correlated delivery is bound
        again from the receipt itself. Idempotent: an existing binding stands unchanged.
        Never raises — boot must survive an unreadable host or database.
        """
        bind = getattr(self._budget_db, "bind_session_run", None)
        if bind is None:
            return 0
        try:
            snapshots = self._worker_snapshots()
        except Exception:  # noqa: BLE001 -- boot must survive a refusing host
            log.exception("run-bindings: could not read the worker records")
            return 0
        bound = 0
        for (provider_id, label), snapshot in sorted(snapshots.items()):
            workers = snapshot.get("workers") if isinstance(snapshot, Mapping) else None
            if not isinstance(workers, Mapping):
                continue
            for worker in workers.values():
                if not isinstance(worker, Mapping):
                    continue
                session_id = str(worker.get("run_session_id") or "").strip()
                correlation = DeliveryCorrelation.of(worker.get("work_id"), worker.get("run_id"))
                if not session_id or correlation is None:
                    continue
                try:
                    bind(
                        f"{provider_id}:{label}:{session_id}",
                        correlation.work_id,
                        correlation.run_id,
                        delivery_id=worker.get("run_delivery_id"),
                    )
                    bound += 1
                except Exception:  # noqa: BLE001 -- one bad row must not skip the rest
                    log.exception("run-bindings: could not bind %s:%s", provider_id, label)
        return bound

    # --- reviving chats the last death interrupted (#1547) -----------------

    async def revive_survivors(self, delivered: set[str] | None = None) -> None:
        """Wake every chat the previous runtime's death cut off.

        Nothing here needs a registration: the survivors are read off the durable
        worker records the death left behind. ``delivered`` is the set of chats
        this boot has already written to, which is how the registry pass and this
        one stay at one message per chat.
        """
        already = delivered if delivered is not None else set()
        try:
            snapshots = self._worker_snapshots()
        except Exception:  # noqa: BLE001 -- boot must survive a refusing host
            log.exception("revival: could not read the worker records")
            return
        try:
            previous = self.ledger.load()
        except Exception:  # noqa: BLE001 -- an unreadable ledger is not fatal
            log.exception("revival: could not read the ledger")
            previous = {}
        now = time.time()
        try:
            survivors = find_survivors(
                snapshots,
                now=now,
                ttl_s=self.registry.ttl_s,
                background_tasks=self._background_tasks,
            )
        except Exception:  # noqa: BLE001 -- a malformed snapshot must not end boot
            log.exception("revival: could not scan for survivors")
            return
        # The chats the dashboard started have no worker record at all, so they are
        # read off the providers' own session scan instead. Keyed sessions are
        # excluded whatever the keyed scan decided about them: one chat is judged
        # by one rule, and a live or idle keyed worker is not a survivor twice.
        try:
            candidates = await self._session_candidates()
        except Exception:  # noqa: BLE001 -- boot must survive an unreadable provider
            log.exception("revival: could not scan sessions for survivors")
            candidates = []
        try:
            survivors += find_session_survivors(
                candidates,
                now=now,
                ttl_s=self.registry.ttl_s,
                exclude=already | _keyed_session_keys(snapshots),
            )
        except Exception:  # noqa: BLE001 -- a malformed session must not end boot
            log.exception("revival: could not scan for session survivors")
        for survivor in survivors:
            if survivor.session_key in already:
                continue
            try:
                wrote = await self._revive(survivor, previous.get(survivor.session_key))
            except Exception:  # noqa: BLE001 -- one bad chat must not skip the rest
                # As above: a delivery that raised may have written, so this chat
                # counts as written to for the rest of the boot.
                wrote = True
                log.exception("revival: failed to revive %s", survivor.session_key)
            if wrote:
                already.add(survivor.session_key)

    async def _session_candidates(self) -> list[SessionCandidate]:
        """Deck-owned chats with no worker key, read off the providers' transcripts.

        Only the providers with a session runtime are scanned: a chat this pass
        cannot deliver to is not worth discovering. Ownership comes from the
        deck's own durable session-token record rather than from the transcript,
        because a chat somebody ran in their own terminal is not ours to write
        into — and a crashed chat's control capabilities are long gone by now.
        """
        owned = self._owned_session_keys()
        if not owned:
            return []
        candidates: list[SessionCandidate] = []
        for provider_id, session_runtime in self._session_runtimes.items():
            provider = PROVIDERS.get(provider_id)
            if provider is None:
                continue
            try:
                live = set(session_runtime.active_session_keys())
            except Exception:  # noqa: BLE001 -- one bad runtime must not end the scan
                log.exception("revival: could not read live %s sessions", provider_id)
                live = set()
            accounts = getattr(session_runtime, "accounts", None) or {}
            for label, account in accounts.items():
                try:
                    sessions = await provider.scan_sessions(account)
                except Exception:  # noqa: BLE001 -- one bad account must not end the scan
                    log.exception(
                        "revival: could not scan %s:%s sessions", provider_id, label
                    )
                    continue
                for session in sessions:
                    session_key = f"{provider_id}:{label}:{session.session_id}"
                    if session_key not in owned:
                        continue
                    candidates.append(
                        SessionCandidate(
                            session_key=session_key,
                            provider_id=provider_id,
                            label=str(label),
                            mid_turn=bool(session.thinking or session.stalled),
                            live=session_key in live,
                            last_activity=_epoch(session.last_activity),
                        )
                    )
        return candidates

    def _owned_session_keys(self) -> set[str]:
        """Every chat the deck minted a session token for — its ownership record."""
        load = getattr(self._budget_db, "load_session_tokens", None)
        if load is None:
            return set()
        try:
            return set(load().values())
        except Exception:  # noqa: BLE001 -- boot must survive an unreadable database
            log.exception("revival: could not read the deck's session tokens")
            return set()

    async def _revive(self, survivor: Survivor, previous: RevivalRecord | None) -> bool:
        if revival_looped(
            survivor, previous, previous_exit_clean=self._previous_exit_clean
        ):
            if survivor.reason == BACKGROUND:
                # Nothing died on a revival here: the chat is still waiting on a
                # background task we already told it about, which no restart will
                # bring back. Telling it again each boot is noise, and flagging it
                # as stopped is false (#3009).
                log.info(
                    "revival: %s was already told its background tasks died; not again",
                    survivor.session_key,
                )
                return False
            # It died on the revival we gave it, in a crash. Revive it again and a chat
            # that eats the host's memory takes the host down once per boot, forever.
            log.warning(
                "revival: %s was cut off again on its own revival; leaving it alone",
                survivor.session_key,
            )
            self._flag_revival_loop(survivor)
            return False
        delivery_id = survivor.delivery_id(self._boot_stamp)
        unread = self._unread_messages(survivor)
        dropped = self._unread_dropped(survivor)
        message = revival_message(
            survivor.reason,
            previous_exit_clean=self._previous_exit_clean,
            unread=unread,
            dropped=dropped,
        )
        outcome = await self.deliver_to_session(
            survivor.session_key, message=message, delivery_id=delivery_id
        )
        log.info(
            "revival: %s (%s) -> %s",
            survivor.session_key,
            survivor.reason,
            _nudge_result(outcome),
        )
        if not outcome.accepted:
            # Not recorded, so a refusal is never remembered as a revival that
            # happened. One refused at the account's cap is tried again on this boot
            # as slots free (#3009); any other waits for the next boot. It still
            # counts as written to for this boot when the provider may have started
            # the write.
            if _refused_for_capacity(outcome):
                self._refused[survivor.session_key] = _RefusedBootDelivery(
                    session_key=survivor.session_key,
                    message=message,
                    delivery_id=delivery_id,
                    kind="revival",
                    survivor=survivor,
                    late_message=late_revival_message(
                        survivor.reason,
                        previous_exit_clean=self._previous_exit_clean,
                        cut_off_at=self._boot_stamp,
                        unread=unread,
                        dropped=dropped,
                    ),
                )
            return _wrote_to_session(outcome)
        self._record_revival(survivor, delivery_id)
        return True

    def _record_revival(self, survivor: Survivor, delivery_id: str) -> None:
        self.ledger.record(
            RevivalRecord(
                session_key=survivor.session_key,
                delivery_id=delivery_id,
                # Stamped after the delivery, so it is never earlier than the
                # host's own delivery clock and the loop guard reads true.
                delivered_at=time.time(),
                boot_stamp=self._boot_stamp,
                background_tasks=survivor.background_tasks,
            ),
            now=time.time(),
            ttl_s=self.registry.ttl_s,
        )

    def _since_boot(self, session_key: str) -> str:
        """What happened to a keyed chat since boot: untouched, busy or moved on.

        Moved on means its worker is live again or it was handed a message after
        boot — a refusal at the cap writes nothing and stamps nothing, so any
        delivery newer than the boot stamp is somebody else's. Busy is moved on
        with that turn still running. Never raises; a chat this cannot read is
        assumed untouched.
        """
        parts = session_key_parts(session_key)
        if parts is None:
            return _UNTOUCHED
        provider_id, label, session_id = parts
        runtime = self._workers.get(provider_id)
        all_hosts = getattr(runtime, "all_hosts", None) if runtime is not None else None
        if all_hosts is None:
            return _UNTOUCHED
        try:
            host = all_hosts().get(label)
            if host is None:
                return _UNTOUCHED
            key = host.find_key_by_session(session_id)
            row = host.snapshot().get("workers", {}).get(key) if key is not None else None
        except Exception:  # noqa: BLE001 -- an unreadable host must not end the retries
            log.exception("revival: could not read %s before a retry", session_key)
            return _UNTOUCHED
        if not isinstance(row, Mapping):
            return _UNTOUCHED
        last_delivery_at = row.get("last_delivery_at") or 0.0
        moved_on = bool(row.get("live")) or (
            isinstance(last_delivery_at, (int, float)) and last_delivery_at > self._boot_stamp
        )
        if not moved_on:
            return _UNTOUCHED
        return _BUSY if row.get("live") and row.get("turn_active") else _MOVED_ON

    def _flag_revival_loop(self, survivor: Survivor) -> None:
        """Put the stopped chat on the deck, where a person will see it."""
        record = getattr(self._budget_db, "record_session_attention_report", None)
        if record is None:
            return
        try:
            record(survivor.session_key, "waiting", LOOP_ATTENTION_DETAIL)
        except Exception:  # noqa: BLE001 -- boot must survive an unwritable database
            log.exception("revival: could not flag %s on the deck", survivor.session_key)

    def _worker_snapshots(self) -> dict[tuple[str, str], dict[str, Any]]:
        snapshots: dict[tuple[str, str], dict[str, Any]] = {}
        for provider_id, runtime in self._workers.items():
            all_hosts = getattr(runtime, "all_hosts", None)
            if all_hosts is None:
                continue
            try:
                hosts = all_hosts()
            except Exception:  # noqa: BLE001 -- a disabled provider must not end the scan
                log.exception("revival: could not load %s hosts", provider_id)
                continue
            for label, host in hosts.items():
                try:
                    snapshots[(provider_id, label)] = host.snapshot()
                except Exception:  # noqa: BLE001 -- one bad host must not end the scan
                    log.exception("revival: could not read %s:%s", provider_id, label)
        return snapshots

    def _background_tasks(
        self, provider_id: str, label: str, worker_key: str
    ) -> frozenset[str]:
        """Outstanding background shell tasks, for the providers that track them.

        Only Claude does: a background ``Bash`` command is launched and collected
        through its own transcript ledger rather than over the runtime stream, and
        that ledger is what says a chat ended its turn waiting for something.
        """
        runtime = self._workers.get(provider_id)
        all_hosts = getattr(runtime, "all_hosts", None) if runtime is not None else None
        if all_hosts is None:
            return frozenset()
        host = all_hosts().get(label)
        outstanding = getattr(host, "outstanding_background_tasks", None)
        if outstanding is None:
            return frozenset()
        return frozenset(outstanding(worker_key))

    def _unread_messages(self, survivor: Survivor) -> list[dict[str, Any]]:
        """Messages the cut-off turn held in its queue and never read (#2898).

        Only a keyed worker host that keeps them answers, which today is Claude's:
        a Codex thread keeps its own queue in the app-server, which outlives the
        runtime. A host that cannot answer costs the chat its quotes, never its
        revival.
        """
        if survivor.reason != CUT_OFF or not survivor.worker_key:
            return []
        runtime = self._workers.get(survivor.provider_id)
        all_hosts = getattr(runtime, "all_hosts", None) if runtime is not None else None
        if all_hosts is None:
            return []
        try:
            host = all_hosts().get(survivor.label)
            unread = getattr(host, "unread_messages", None)
            return list(unread(survivor.worker_key)) if unread is not None else []
        except Exception:  # noqa: BLE001 -- an unreadable record must not stop the revival
            log.exception("revival: could not read unread messages for %s", survivor.session_key)
            return []

    def _unread_dropped(self, survivor: Survivor) -> int:
        """How many queued messages the host let go unquoted (#2898)."""
        if survivor.reason != CUT_OFF or not survivor.worker_key:
            return 0
        runtime = self._workers.get(survivor.provider_id)
        all_hosts = getattr(runtime, "all_hosts", None) if runtime is not None else None
        if all_hosts is None:
            return 0
        try:
            host = all_hosts().get(survivor.label)
            dropped = getattr(host, "unread_dropped", None)
            count = dropped(survivor.worker_key) if dropped is not None else 0
            return count if isinstance(count, int) else 0
        except Exception:  # noqa: BLE001 -- an unreadable record must not stop the revival
            log.exception("revival: could not read dropped messages for %s", survivor.session_key)
            return 0

    async def _deliver(self, entry: NudgeEntry) -> DeliverResult:
        return await self.deliver_to_session(
            entry.session_key,
            message=NUDGE_MESSAGE,
            delivery_id=entry.delivery_id,
        )

    async def deliver_to_session(
        self,
        session_key: str,
        *,
        message: str,
        delivery_id: str,
    ) -> DeliverResult:
        """Write one fenced message into any owned chat, by session key.

        This is the restart nudge's own routing — keyed worker first, then the
        provider's session runtime for a chat the dashboard started — reused by
        the job queue, which has exactly the same problem: it knows a session key
        and a sentence, and must not deliver it twice.
        """
        parts = session_key_parts(session_key)
        if parts is None:
            return DeliverResult(False, "rejected", reason="malformed session key")
        provider_id, label, session_id = parts
        runtime = self._workers.get(provider_id)
        if runtime is None:
            return DeliverResult(
                False,
                "rejected",
                reason=f"provider {provider_id!r} cannot be nudged",
            )
        settings = getattr(runtime, "settings", None)
        if settings is not None and not getattr(settings, "enabled", True):
            return DeliverResult(
                False, "rejected", reason=f"{provider_id} workers are disabled"
            )
        try:
            host = runtime.host(label)
        except HTTPException as exc:
            return DeliverResult(False, "rejected", reason=str(exc.detail))
        key = host.find_key_by_session(session_id)
        if key is not None:
            role = host.worker_role(key)
            return await self._deliver_keyed(
                provider_id,
                label,
                host,
                key,
                role,
                message=message,
                delivery_id=delivery_id,
            )
        session_runtime = self._session_runtimes.get(provider_id)
        if session_runtime is not None:
            # A dashboard-started thread/agent has no worker key, so it is written
            # as a fenced queue_turn on the provider's app-server runtime.
            return await session_runtime.nudge_session(
                label, session_id, delivery_id, message
            )
        return DeliverResult(
            False, "rejected", reason="no worker record for this session"
        )

    async def _deliver_keyed(
        self,
        provider: str,
        label: str,
        host: KeyedWorkerHost,
        key: str,
        role: SessionRole | None,
        *,
        message: str,
        delivery_id: str,
    ) -> DeliverResult:
        body = DeliverRequest(
            key=key,
            message=message,
            delivery_id=delivery_id,
            delegated=True,
            role=role,
        )
        async def send() -> DeliverResult:
            # Checked under the admission lock, immediately before the write, which is the
            # same lock a Run stop parks under: this delivery lands before the park, or it
            # sees the stop and is refused (#2159).
            fence = self.stop_fence
            if fence is not None:
                row = host.snapshot().get("workers", {}).get(key)
                if fence(row):
                    log.info("not resuming %s:%s:%s: its Run was stopped", provider, label, key)
                    return DeliverResult(False, "rejected", reason=run_execution.RUN_STOPPED)
            return await host.deliver(
                key,
                message,
                delivery_id=delivery_id,
                delegated=True,
                role=role,
                # Every message this path carries is the deck's own, never a person's,
                # so it waits for a slot even in a person's chat (#3009).
                automated=True,
            )

        return await self._kanban_capacity.deliver(provider, label, body, send)


def _paths(values: list[str]) -> list[Path]:
    return [Path(value) for value in values]


def _preserve_images(account: Account, values: list[str]) -> tuple[list[Path], Path | None]:
    """Copy uploads before a web restart can remove its temporary originals."""
    if not values:
        return [], None
    root = runtime_socket_path().parent / "uploads" / account.label / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    copied = []
    try:
        for value in values:
            source = Path(value)
            target = root / source.name
            shutil.copy2(source, target)
            copied.append(target)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return copied, root


def _wire_change_feed(
    *,
    runtime: CodexRuntime,
    claude_workers: ClaudeWorkerRuntime,
    codex_workers: CodexWorkerRuntime,
    jobs: JobQueue,
    slot_leases: SlotLeases,
    executions: run_execution.RunExecutions | None,
    budget_db: Db,
    restart_nudges: RestartNudges | None = None,
) -> runtime_feed.ChangeFeed:
    """Tell the change feed wherever the state the web reads can move (#2865).

    Workers are compared, not trusted: a stdout line, a process exit or a saved record
    only *marks* a worker, and the feed bumps when what the web reads of it differs.
    The job queue and Run outcomes bump directly, because they are told only on a real
    change. Every control request also marks everything once it is answered (the
    middleware in ``create_runtime_app``), which is what covers the state a request
    changes without passing any of these points.
    """
    feed = runtime_feed.ChangeFeed()

    def watch_claude(label: str, host: ClaudeWorkerHost) -> None:
        item = runtime_feed.workers_item("claude_code", label)

        def changed(key: str | None) -> None:
            feed.mark(item, runtime_feed.ALL if key is None else key)
            if key is None:
                # A saved record is where the runtime binds a chat's token.
                feed.mark(runtime_feed.RELATIONS)

        host.on_state_change = changed

        def keys() -> set[Any]:
            found = set(host.change_keys())
            return found | {REVIVAL_FEED_KEY} if restart_nudges is not None else found

        def entry(key: Any) -> object:
            # The revivals this runtime still owes ride along in the snapshot (#3008), so
            # they are compared here too: a scan that ends, or a revival given up, moves
            # what the deck reads without any worker changing.
            if key == REVIVAL_FEED_KEY and restart_nudges is not None:
                due = restart_nudges.revivals_due()
                prefix = f"claude_code:{label}:"
                return (due["scanned"], tuple(k for k in due["due"] if k.startswith(prefix)))
            return host.change_entry(key)

        feed.add_check(
            runtime_feed.ChangeCheck(item, keys, entry, deadline=host.next_change_at)
        )

    claude_workers.on_host = watch_claude
    for label, host in claude_workers.hosts.items():
        watch_claude(label, host)
    if restart_nudges is not None:

        def revivals_changed() -> None:
            for label in list(claude_workers.hosts):
                feed.mark(runtime_feed.workers_item("claude_code", label), REVIVAL_FEED_KEY)

        restart_nudges.on_revivals_change = revivals_changed

    def codex_item(label: str) -> str:
        return runtime_feed.workers_item("codex", label)

    def codex_check(label: str) -> runtime_feed.ChangeCheck:
        # One Codex account's ``/state``: its owned threads and the keyed workers on them,
        # compared thread by thread. Read through ``hosts`` rather than ``host()`` so a
        # check never creates a host; creating one marks the account (``watch_codex``).
        def keys() -> set[str]:
            host = codex_workers.hosts.get(label)
            return set(runtime.owned_threads(label)) | (host.change_keys() if host else set())

        def entry(thread_id: Any) -> object:
            host = codex_workers.hosts.get(label)
            workers = host.change_entry(thread_id) if host is not None else ()
            row = runtime.thread_row(label, thread_id)
            return None if row is None and not workers else (row, workers)

        return runtime_feed.ChangeCheck(codex_item(label), keys, entry)

    for label in runtime.accounts:
        feed.add_check(codex_check(label))
    runtime.add_change_listener(
        lambda label, thread_id: feed.mark(codex_item(label), thread_id)
    )

    def watch_codex(label: str, host: CodexWorkerHost) -> None:
        def changed() -> None:
            feed.mark(codex_item(label))
            feed.mark(runtime_feed.RELATIONS)

        host.on_state_change = changed
        feed.mark(codex_item(label))

    codex_workers.on_host = watch_codex
    for label, host in codex_workers.hosts.items():
        watch_codex(label, host)

    # The queue bumps on its own saves; the slot holders shown beside it are compared,
    # since a lease is taken by a request and dropped by the reaper.
    jobs.on_change = lambda: feed.bump(runtime_feed.JOBS)
    slot_leases.on_change = lambda: feed.mark(runtime_feed.JOBS)
    feed.add_check(
        runtime_feed.ChangeCheck(
            runtime_feed.JOBS,
            lambda: ("heavy_slots",),
            lambda _key: describe_heavy_slots(
                jobs.heavy_slots(),
                jobs=jobs.held_slot_inodes(),
                leases=slot_leases.held_slot_inodes(),
            ),
        )
    )
    # Tokens bound and cards registered by the runtime itself (#2144).
    feed.add_check(
        runtime_feed.ChangeCheck(
            runtime_feed.RELATIONS,
            lambda: ("stamp",),
            lambda _key: budget_db.runtime_relation_stamp(),
        )
    )
    if executions is not None:
        executions.on_change = lambda: feed.bump(runtime_feed.RUNS)
    return feed


def create_runtime_app(config: AppConfig) -> FastAPI:
    budget_db = open_db(
        config.history.db_path,
        config.history.usage_retention_days,
        config.context_management.retention_days,
    )
    runtime = CodexRuntime(config, budget_db)
    claude_workers = ClaudeWorkerRuntime(config, budget_db)
    codex_workers = CodexWorkerRuntime(config, runtime)
    worker_runtimes: dict[str, Any] = {
        "claude_code": claude_workers,
        "codex": codex_workers,
    }
    kanban_capacity = KanbanCapacity(
        limit=config.keyed_workers.max_kanban_workers,
        workers=worker_runtimes,
    )
    restart_nudges = RestartNudges(
        config,
        session_runtimes={"codex": runtime},
        workers=worker_runtimes,
        kanban_capacity=kanban_capacity,
        budget_db=budget_db,
    )
    session_secrets = SessionSecretStore()
    artifact_store = ArtifactStore(config.keyed_workers.state_path / "command-artifacts")
    # Claims and outcomes once landed under a literal ``~`` inside cwd (#2199). While that
    # file still holds state the real one lacks, neither store opens: running on the real
    # file alone would drop active claims and let conflicting ones be written.
    stray_state = merge_stray_db(config.history.db_path)
    # The Work workspace service (#2149): this process is the only writer of claims.
    try:
        if stray_state == "failed":
            raise RuntimeError("the stray control database has not been merged")
        work_workspaces: work_claims.WorkWorkspaceService | None = (
            work_claims.WorkWorkspaceService(work_claims.ClaimStore(config.history.db_path))
        )
    except Exception:  # noqa: BLE001 -- without it the routes answer 503, never guess
        log.exception("work workspace claims are unavailable")
        work_workspaces = None

    async def _clean_artifacts_forever() -> None:
        while True:
            await asyncio.sleep(ARTIFACT_CLEANUP_INTERVAL_S)
            try:
                artifact_store.cleanup()
            except Exception:  # noqa: BLE001 -- retention failure must not stop the runtime
                log.exception("command artifact cleanup failed")

    # Run execution outcome and stop (#2159): whether a Run's concrete worker is still
    # running is this process's fact, keyed by the Work/Run the Poller already records.
    async def _park(provider: str, label: str, key: str) -> DeliverResult:
        return await kanban_capacity.release(
            provider, label, key, lambda: _worker_host(provider, label).park_worker(key)
        )

    async def _park_held(provider: str, label: str, key: str) -> DeliverResult:
        """The same park, for a stop made while the admission lock is already held."""
        return await kanban_capacity.release_held(
            provider, label, key, lambda: _worker_host(provider, label).park_worker(key)
        )

    try:
        if stray_state == "failed":
            raise RuntimeError("the stray control database has not been merged")
        executions: run_execution.RunExecutions | None = run_execution.RunExecutions(
            run_execution.OutcomeStore(config.history.db_path),
            hosts=kanban_capacity.hosts,
            db=budget_db,
            park=_park,
        )
    except Exception:  # noqa: BLE001 -- without it the routes answer 503, never guess
        log.exception("run execution outcomes are unavailable")
        executions = None
    # One rule for every Work-linked consumer (#2159): without Run execution state nothing
    # can be proved stopped, so it reads as unknown — never as safe.
    restart_nudges.stop_fence = (
        executions.stop_requested_for
        if executions is not None
        else run_execution.serves_a_run
    )

    async def _prior_run_in_the_way(
        request: EnsureDeliveryRequest, provider: str, label: str, own_token: str | None
    ) -> DeliverResult | None:
        """Supersede, stop, then admit — decided inside the admission lock (#2159).

        Called once the target account is known, immediately before the route is frozen and
        the provider written, so no other delivery can be placed between this check and the
        write. Stops made here use the lock-free park. ``None`` means the write may go ahead.
        Without Run execution state there is no proof an older Run stopped, so a Work/Run
        delivery is refused rather than admitted blind.
        """
        if executions is None:
            log.warning(
                "ensure_delivery %s refused: run execution state is unavailable",
                request.delivery_id,
            )
            return DeliverResult(False, "rejected", reason=PRIOR_RUN_EXECUTING)
        try:
            blocking = await executions.admit(
                request.work_id,
                request.run_id,
                worker_key=request.worker_key,
                target=(provider, label),
                park=_park_held,
                own_token=own_token,
            )
        except Exception:  # noqa: BLE001 -- unreadable is not "nothing running"
            log.exception("could not read the other Runs of %s", request.work_id)
            return DeliverResult(False, "rejected", reason=PRIOR_RUN_EXECUTING)
        if blocking is None:
            return None
        log.info(
            "ensure_delivery %s refused: Run %s of %s is %s",
            request.delivery_id, blocking.run_id, request.work_id, blocking.execution_state,
        )
        return DeliverResult(False, "rejected", reason=PRIOR_RUN_EXECUTING)

    def _fresh_if_behind_its_chat(
        request: EnsureDeliveryRequest, delivery: DeliverRequest, provider: str, label: str
    ) -> DeliverRequest:
        """Start the keyed worker fresh when it holds a leg its chat has moved past (#2884).

        A resume into a chat a person continued would otherwise land on the Run's own
        worker, still on the old leg, and pick the work up from before the continuation. A
        worker mid-turn, or a steer, is left as asked: replacing it would end that turn.
        """
        if delivery.fresh or delivery.require_active:
            return delivery
        try:
            snapshot = _worker_host(provider, label).snapshot()
        except Exception:  # noqa: BLE001 -- unreadable keeps the delivery as it was asked
            log.exception("could not read %s:%s workers for %s", provider, label, delivery.key)
            return delivery
        row = (snapshot.get("workers") or {}).get(delivery.key)
        if not isinstance(row, dict) or row.get("turn_active"):
            return delivery
        leg = f"{provider}:{label}:{row.get('session_id')}" if row.get("session_id") else ""
        if not resumes_behind_its_chat(
            budget_db, request.continues_session_url, delivery.session_token, leg
        ):
            return delivery
        log.info(
            "ensure_delivery %s starts %s fresh: its leg %s is behind its chat",
            request.delivery_id, delivery.key, leg,
        )
        return delivery.model_copy(update={"fresh": True})

    def _continued_since(
        request: EnsureDeliveryRequest, since: datetime, own_token: str | None
    ) -> DeliverResult | None:
        """Refuse a just-frozen delivery a deck Continue raced (#2862).

        Admission looked before the freeze; a Continue of this Work's chat reserved in
        between was not there to see. A Continue fences its reservation on its Run still
        being the newest, in one transaction, so whichever wrote first, one side sees the
        other: either the Continue is refused, or this asks after the freeze and finds it.
        Unreadable is refused too. The caller releases the route; the Poller retries.
        """
        read = getattr(budget_db, "continuations_reserved_since", None)
        if read is None or not request.work_id:
            return None
        try:
            raced = read(
                request.work_id, since, max_legs=run_execution.MAX_CONTINUATIONS,
                excluding=own_token,
            )
        except Exception:  # noqa: BLE001 -- unreadable is not "nobody continued it"
            log.exception("could not read the continued chats of %s", request.work_id)
            raced = ["unreadable"]
        if not raced:
            return None
        log.info(
            "ensure_delivery %s refused: a Continue of %s raced it (%s)",
            request.delivery_id, request.work_id, ", ".join(raced),
        )
        return DeliverResult(False, "rejected", reason=PRIOR_RUN_EXECUTING)

    def _execution_active():
        """Whether a Work's workspace may still be written, by path (#2149, #2159).

        Asked of the Work's own Run executions: a claim names its Work, and the Runtime knows
        whether any Run of that Work can still write. Unreadable answers None, which defers
        the release rather than deleting under a worker.
        """

        def active(path: str) -> bool | None:
            claim = work_workspaces.store.holding(path) if work_workspaces else None
            if claim is None:
                # No active claim names a Work to ask, so nothing proves the path idle.
                return None
            if executions is None:
                # A Work's workspace with no execution state to consult: unknown, and the
                # release waits rather than trusting a snapshot of an idle-looking row.
                return None
            try:
                return executions.work_active(claim.work_id)
            except Exception:  # noqa: BLE001 -- unreadable defers a release
                log.exception("run executions of %s could not be read", claim.work_id)
                return None

        return active

    async def _retry_work_workspace_releases_forever() -> None:
        while True:
            await asyncio.sleep(WORK_WORKSPACE_RELEASE_RETRY_S)
            if work_workspaces is None:
                continue
            try:
                outcomes = await asyncio.to_thread(
                    work_workspaces.release_pending, execution_active=_execution_active()
                )
                for item in outcomes:
                    if not item.released:
                        log.info("work workspace %s still held: %s", item.path, item.pending)
            except Exception:  # noqa: BLE001 -- a failed retry must not stop the runtime
                log.exception("work workspace release retry failed")

    async def _refresh_run_outcomes_forever() -> None:
        while True:
            await asyncio.sleep(RUN_OUTCOME_REFRESH_S)
            if executions is None:
                continue
            try:
                await asyncio.to_thread(executions.refresh_open)
            except Exception:  # noqa: BLE001 -- a failed refresh must not stop the runtime
                log.exception("run execution refresh failed")

    async def _tell_submitter(job: Job, message: str) -> NotifyOutcome:
        outcome = await restart_nudges.deliver_to_session(
            job.session_key or "",
            message=message,
            # Stable and job-scoped: a retry of the same finished job replays
            # rather than posting a second copy into the chat.
            delivery_id=f"job-finished:{job.id}",
        )
        log.info(
            "jobs: told %s about job %s -> %s",
            job.session_key,
            job.id,
            _nudge_result(outcome),
        )
        # The reason travels with the verdict: the queue decides how long to keep
        # trying from *why* it was refused, and a full worker pool is not the same
        # answer as a chat that no longer exists (#1610).
        return NotifyOutcome(outcome.accepted, outcome.reason or "")

    async def _tell_progress(job: Job, message: str, number: int) -> NotifyOutcome:
        outcome = await restart_nudges.deliver_to_session(
            job.session_key or "",
            message=message,
            # One id per progress message (#3038), so each is delivered at most once.
            delivery_id=f"job-progress:{job.id}:{number}",
        )
        log.info(
            "jobs: told %s how job %s goes (%d) -> %s",
            job.session_key,
            job.id,
            number,
            _nudge_result(outcome),
        )
        return NotifyOutcome(outcome.accepted, outcome.reason or "")

    def _ownership_label(repository: str) -> str:
        """The label that hands an issue in ``repository`` to a worker, from the manifests."""
        from .poller_ops import issue_label_targets

        for target in issue_label_targets(config.poller.root):
            if target.repository.lower() == repository.lower() and target.ownership_labels:
                return target.ownership_labels[0]
        return ""

    quarantine = Quarantine(
        Path(config.jobs.path) / QUARANTINE_FILENAME, labels=_ownership_label
    )
    # One for the queue and the leases alike: both start heavy work on this host (#3614).
    memory_gate = MemoryGate()
    jobs = JobQueue(
        state_dir=config.jobs.path,
        quarantine=quarantine,
        # The deck's starred Works (#3116), from the history DB the web writes them to.
        starred=budget_db.starred_works,
        notify=_tell_submitter,
        notify_progress=_tell_progress,
        slot_paths=(
            heavy_slot_paths(config.jobs.slots) if config.jobs.slots else None
        ),
        # A restart takes running jobs back rather than cutting them off (#3248).
        survive_restart=True,
        # Local jobs run at low priority with a memory cap (#3614).
        configure_slice=configure_jobs_slice,
        memory_gate=memory_gate,
    )

    # Not gated on config.jobs.enabled: the slot pool is the host's, and a
    # deck with the job queue turned off still shares its cores with whatever
    # else is running. Leases are opt-in by the caller either way.
    slot_leases = SlotLeases(
        slot_paths=(heavy_slot_paths(config.jobs.slots) if config.jobs.slots else None),
        # The queue's gate, so a lease and a suite cannot both spend the same free memory.
        memory_gate=memory_gate,
    )
    change_feed = _wire_change_feed(
        runtime=runtime,
        claude_workers=claude_workers,
        codex_workers=codex_workers,
        jobs=jobs,
        slot_leases=slot_leases,
        executions=executions,
        budget_db=budget_db,
        restart_nudges=restart_nudges,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Before anything else can be started or stopped: this reads how the last
        # runtime exited and claims the marker for this one.
        restart_nudges.begin_boot()
        artifact_store.cleanup()
        artifact_cleanup_task = asyncio.create_task(
            _clean_artifacts_forever(), name="command-artifact-cleanup"
        )
        release_retry_task = asyncio.create_task(
            _retry_work_workspace_releases_forever(), name="work-workspace-release-retry"
        )
        outcome_refresh_task = asyncio.create_task(
            _refresh_run_outcomes_forever(), name="run-outcome-refresh"
        )
        await runtime.start()
        await claude_workers.start()
        quarantine_task = None
        if config.jobs.enabled:
            await jobs.start()
            quarantine_task = asyncio.create_task(
                quarantine.run_forever(), name="test-quarantine-sweep"
            )
        await slot_leases.start()
        # Codex recovered its owned threads in ``runtime.start()`` without telling anyone;
        # record them before the first reader connects (#2865).
        change_feed.mark_all()
        # Wake every chat that asked to continue after this restart, and every chat
        # the last death cut off without getting to ask. Backgrounded so a
        # `claude --resume` spawn never delays boot; started after runtime.start()
        # so Codex thread ownership is already recovered — a Codex turn that
        # survived the restart must read as live rather than as a cut-off one.
        nudge_task = asyncio.create_task(
            restart_nudges.deliver_all(),
            name="restart-nudge-delivery",
        )
        try:
            yield
        finally:
            artifact_cleanup_task.cancel()
            release_retry_task.cancel()
            outcome_refresh_task.cancel()
            await asyncio.gather(
                artifact_cleanup_task, release_retry_task, outcome_refresh_task,
                return_exceptions=True,
            )
            nudge_task.cancel()
            await asyncio.gather(nudge_task, return_exceptions=True)
            if quarantine_task is not None:
                quarantine_task.cancel()
                await asyncio.gather(quarantine_task, return_exceptions=True)
            change_feed.close()
            await slot_leases.stop()
            await jobs.stop()
            await runtime.stop()
            await claude_workers.stop()
            budget_db.close()
            if work_workspaces is not None:
                work_workspaces.store.close()
            # Last, and only on a graceful stop: an OOM kill never reaches here,
            # which is what tells the next boot it is looking at a crash.
            restart_nudges.end_boot()

    app = FastAPI(title="agentdeck-codex-runtime", lifespan=lifespan)
    app.state.change_feed = change_feed

    @app.middleware("http")
    async def mark_after_control(request, call_next):
        response = await call_next(request)
        # A control request can change what the web reads without passing any of the
        # points ``_wire_change_feed`` hooks (an interrupt, an answer, a lease); the
        # checks compare, so marking everything costs a comparison and no false bump.
        if request.method != "GET":
            change_feed.mark_all()
        return response

    @app.get(runtime_feed.CHANGES_PATH)
    async def changes(
        since: int | None = None, epoch: str | None = None, wait: float = 0.0
    ) -> dict[str, Any]:
        """Held until the change number moves past ``since`` (#2865); see ``runtime_feed``."""
        return await change_feed.wait(
            since, epoch, timeout=max(0.0, min(wait, runtime_feed.KEEPALIVE_S))
        )

    app.state.runtime = runtime
    app.state.claude_workers = claude_workers
    app.state.codex_workers = codex_workers
    app.state.restart_nudges = restart_nudges
    app.state.kanban_capacity = kanban_capacity
    app.state.command_artifacts = artifact_store
    app.state.budget_db = budget_db
    app.state.session_secrets = session_secrets
    app.state.run_executions = executions
    app.state.jobs = jobs
    app.state.slot_leases = slot_leases
    app.state.work_workspaces = work_workspaces

    def _require_jobs() -> JobQueue:
        if not config.jobs.enabled:
            raise HTTPException(status_code=404, detail="the job queue is disabled")
        return jobs

    def _job_or_404(job_id: str) -> Job:
        found = _require_jobs().get(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
        return found

    @app.post("/jobs")
    async def submit_job(body: JobSubmitRequest) -> dict[str, Any]:
        try:
            job = _require_jobs().submit(
                command=body.command,
                cwd=body.cwd,
                lane=body.lane,
                lane_defaulted=body.lane_defaulted,
                session_key=body.session_key,
                label=body.label,
                purpose=body.purpose,
                work_id=body.work_id,
                run_id=body.run_id,
                workspaces=body.workspaces,
                max_minutes=body.max_minutes,
            )
        except JobError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return job.to_dict()

    @app.get("/jobs")
    async def list_jobs(work_id: str = "", active: bool = False) -> dict[str, Any]:
        queue = _require_jobs()
        jobs = queue.list()
        if active:
            # The topbar pill asks only for what is running or waiting (#2864): the retained
            # finished rows were most of a 930 KB answer it read every two seconds.
            jobs = [job for job in jobs if job.state in (JOB_QUEUED, JOB_RUNNING)]
        rows = [job.to_dict() for job in jobs]
        # Each waiting job's place in line (#3116): reruns and a starred Work's jobs go ahead
        # of earlier submissions, so the submission time alone no longer gives the order.
        places = {job.id: index for index, job in enumerate(queue.waiting())}
        for row in rows:
            if row["id"] in places:
                row["place"] = places[row["id"]]
        if work_id:
            # One Work's page reads only its own rows: the whole queue is over a megabyte of
            # retained finished jobs, read on every open and every refresh (#2837).
            return {"jobs": rows_for_work(rows, work_id)}
        return {
            "jobs": rows,
            # Rides along with the queue read the panel already makes, so
            # naming the slot holder costs no extra socket round trip (#1391).
            "heavy_slots": describe_heavy_slots(
                queue.heavy_slots(),
                jobs=queue.held_slot_inodes(),
                leases=slot_leases.held_slot_inodes(),
            ),
            # How many local jobs wait for memory while slots are free (#3614).
            "memory_waiting": queue.memory_waiting,
        }

    @app.get("/job-evidence")
    async def job_evidence(work_id: str = "") -> dict[str, Any]:
        """Finished verification jobs' evidence, kept past the queue's retention (#2148)."""
        return {"evidence": _require_jobs().evidence(work_id=work_id)}

    def _require_work_workspaces() -> work_claims.WorkWorkspaceService:
        if work_workspaces is None:
            raise HTTPException(status_code=503, detail="work workspace claims are unavailable")
        return work_workspaces

    def _require_executions() -> run_execution.RunExecutions:
        if executions is None:
            raise HTTPException(status_code=503, detail="run execution outcomes are unavailable")
        return executions

    def _work_or_422(work_id: str) -> str:
        try:
            return run_execution.canonical_work_id(work_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get(run_execution.EXECUTION_OUTCOME_PATH)
    async def run_execution_outcome(work_id: str, run_id: str) -> dict[str, Any]:
        """One Run's concrete execution, as the Runtime sees it now (#2159).

        With an ended turn's uncollected background commands, which the Poller reads as the
        chat still waiting rather than finished (#2890).
        """
        work_id = _work_or_422(work_id)
        outcome = await asyncio.to_thread(
            _require_executions().outcome, work_id, run_id, background=True
        )
        return outcome.as_json()

    @app.get(run_execution.EXECUTIONS_PATH)
    async def run_execution_outcomes(work_id: str) -> dict[str, Any]:
        """Every Run of one Work the Runtime knows, newest first (#2159)."""
        work_id = _work_or_422(work_id)
        outcomes = await asyncio.to_thread(_require_executions().outcomes, work_id)
        return {"work_id": work_id, "executions": [item.as_json() for item in outcomes]}

    @app.post(run_execution.EXECUTION_STOP_PATH)
    async def stop_run_execution(body: run_execution.StopExecutionRequest) -> dict[str, Any]:
        """Stop one Run's concrete execution; needs nothing from the model (#2159)."""
        result, outcome = await _require_executions().stop(
            body.work_id, body.run_id, body.reason
        )
        log.info(
            "run execution %s/%s stop (%s): %s, now %s",
            body.work_id, body.run_id, body.reason, result, outcome.execution_state,
        )
        return {"result": result, "execution": outcome.as_json()}

    @app.post(work_claims.ENSURE_PATH)
    async def ensure_work_workspace(body: dict[str, Any]) -> dict[str, Any]:
        """Resolve, reserve or adopt, and prepare one Work repository workspace (#2149)."""
        service = _require_work_workspaces()
        try:
            request = work_claims.EnsureRequest.from_json(body)
            result = await asyncio.to_thread(service.ensure, request)
        except work_claims.ClaimError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return result.as_json()

    @app.post(work_claims.RELEASE_PATH)
    async def release_work_workspace(body: WorkWorkspaceReleaseRequest) -> dict[str, Any]:
        """The Work is finished: release its claims, or the named ones (#2149)."""
        service = _require_work_workspaces()
        outcomes, unclaimed = await asyncio.to_thread(
            service.release, body.work_id, body.paths, execution_active=_execution_active()
        )
        return {
            "released": [item.as_json() for item in outcomes],
            "unclaimed": unclaimed,
        }

    @app.get(work_claims.CLAIMS_PATH)
    async def work_workspace_claims(work_id: str = "") -> dict[str, Any]:
        store = _require_work_workspaces().store
        claims = store.active(work_id) if work_id else store.active()
        return {"claims": [claim.as_json() for claim in claims]}

    @app.get("/jobs/{job_id}")
    async def read_job(job_id: str) -> dict[str, Any]:
        return _job_or_404(job_id).to_dict()

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        _job_or_404(job_id)
        cancelled = await _require_jobs().cancel(job_id)
        return cancelled.to_dict() if cancelled is not None else {}

    @app.post("/heavy-slots")
    async def acquire_heavy_slot(body: SlotLeaseRequest) -> dict[str, Any]:
        try:
            lease = await slot_leases.acquire(
                label=body.label, ttl_s=body.ttl_s, wait_s=body.wait_s
            )
        except LeaseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        if lease is None:
            # Not an error on the caller's part: every slot is busy. 503 so a
            # client's ordinary retry-on-unavailable does the right thing.
            detail = (
                "this host is short of memory; retry"
                if slot_leases.short_of_memory
                else "no heavy slot is free; retry"
            )
            raise HTTPException(status_code=503, detail=detail)
        return slot_leases.payload(lease)

    @app.get("/heavy-slots")
    async def list_heavy_slots() -> dict[str, Any]:
        return {"leases": slot_leases.payloads(), "slots": slot_leases.slot_count()}

    @app.post("/heavy-slots/{lease_id}/heartbeat")
    async def renew_heavy_slot(lease_id: str) -> dict[str, Any]:
        lease = slot_leases.heartbeat(lease_id)
        if lease is None:
            raise HTTPException(
                status_code=404,
                detail=f"lease {lease_id} is not held: it expired, was released, "
                "or the runtime restarted",
            )
        return slot_leases.payload(lease)

    @app.delete("/heavy-slots/{lease_id}")
    async def release_heavy_slot(lease_id: str) -> dict[str, Any]:
        return {"lease": lease_id, "released": slot_leases.release(lease_id)}

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "accounts": sorted(runtime.clients)}

    @app.post("/session-secrets")
    async def issue_session_secret(body: SessionSecretRequest) -> dict[str, str]:
        if not body.client_action_id:
            raise HTTPException(status_code=422, detail="client action id required")
        try:
            issued = session_secrets.register(
                session_key=body.session_key,
                action_id=body.client_action_id,
                name=body.name,
                value=body.value,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"ticket": issued.ticket, "name": issued.name}

    @app.post("/session-secrets/claim")
    async def claim_session_secret(
        body: SessionSecretClaimRequest,
    ) -> dict[str, str]:
        claimed = session_secrets.claim(body.ticket)
        if claimed is None:
            raise HTTPException(status_code=404, detail="secret unavailable")
        return {"name": claimed.name, "value": claimed.value}

    @app.post("/session-secrets/revoke")
    async def revoke_session_secret(body: SessionSecretClaimRequest) -> dict[str, bool]:
        return {"revoked": session_secrets.discard(body.ticket)}

    def _live_worker_process_count() -> int:
        """Owned worker child processes that stopping this unit would destroy.

        Only Claude workers are counted: they are processes in this unit's
        control group, so a stop kills them for good. A Codex thread is owned
        state that ``runtime.start()`` recovers on the way back
        up, and treating that ownership as live would mean the runtime could
        never be stopped again once any session had ever been started.
        """
        return sum(
            bool(worker.get("live"))
            for host in claude_workers.all_hosts().values()
            for worker in host.snapshot().get("workers", {}).values()
        )

    def _unfinished_job_count() -> int:
        if not config.jobs.enabled:
            return 0
        return sum(1 for job in jobs.list() if not job.is_final)

    @app.get("/activity")
    async def activity() -> dict[str, Any]:
        """Restart-safety signal for host automation such as the kanban poller.

        ``active`` answers "is a turn running right now". The counts beside it
        answer the wider question the idle web watchdog asks before letting this
        runtime be stopped as unneeded: would stopping it destroy anything?
        """
        counts = {
            "codex": runtime.active_turn_count(),
            "claude": claude_workers.active_turn_count(),
        }
        # What a restart would actually end (#3248): held Claude workers and jobs in their
        # own scopes outlive it; Codex turns survive only on a persistent backend (#992).
        cut = claude_workers.turns_a_restart_would_cut()
        if not config.keyed_workers.persistent_backend:
            cut += counts["codex"]
        if config.jobs.enabled:
            cut += jobs.running_jobs_a_restart_would_cut()
        return {
            "active": any(counts.values()),
            "cut_by_restart": cut,
            "active_turns": counts,
            "active_sessions": restart_nudges.active_session_keys(),
            "live_worker_processes": _live_worker_process_count(),
            "unfinished_jobs": _unfinished_job_count(),
        }

    # --- deck-owned Claude workers (config-gated) ----------------------

    def _deliver_result(result: DeliverResult) -> dict[str, Any]:
        payload = asdict(result)
        context_result = any(
            (
                result.successor_created,
                result.retryable_same_delivery,
                result.context_maintenance_status is not None,
                result.context_maintenance_reason is not None,
                result.context_rollover,
            )
        )
        if not context_result:
            for field in (
                "provider_write_state",
                "successor_created",
                "retryable_same_delivery",
                "context_maintenance_status",
                "context_maintenance_reason",
                "context_rollover",
            ):
                payload.pop(field)
        for field in (
            "delivery_stage",
            "provider_turn_id",
            "work_id",
            "run_id",
            "run_participation",
        ):
            if payload.get(field) is None:
                payload.pop(field, None)
        return payload

    def _worker_host(provider: str, label: str) -> KeyedWorkerHost:
        worker_runtime = worker_runtimes.get(provider)
        if worker_runtime is None:
            raise HTTPException(status_code=404, detail="unknown worker provider")
        return worker_runtime.host(label)

    async def _deliver_worker(
        provider: str,
        label: str,
        body: DeliverRequest,
        *,
        action: str,
    ) -> DeliverResult:
        return await kanban_capacity.deliver(
            provider, label, body, _worker_send(provider, label, body, action=action)
        )

    def _worker_send(
        provider: str,
        label: str,
        body: DeliverRequest,
        *,
        action: str,
    ) -> Callable[[], Awaitable[DeliverResult]]:
        """The keyed-worker write for one delivery, for admission to call once it admits."""
        host = _worker_host(provider, label)
        correlation = (
            DeliveryCorrelation.of(body.work_id, body.run_id) if body.delivery_id else None
        )

        async def send() -> DeliverResult:
            if (body.fork_session_id or body.fork_at) and (
                provider != "claude_code" or not (body.fork_session_id and body.fork_at)
            ):
                # Only Claude's keyed worker cuts a copy at a line; anywhere else the
                # fields would be dropped and the "side chat" would start empty.
                return DeliverResult(False, "rejected", reason="fork_unsupported")
            version = (
                {"expected_last_result_at": body.expected_last_result_at}
                if body.expected_last_result_at is not None
                else {}
            )
            result = await _timed_action(
                action,
                f"{provider}:{label}",
                body.client_action_id,
                lambda: host.deliver(
                    body.key,
                    body.message,
                    cwd=body.cwd,
                    fresh=body.fresh,
                    images=body.images,
                    model=body.model,
                    effort=body.effort,
                    permission_mode=body.permission_mode,
                    delivery_id=body.delivery_id,
                    allow_over_budget=body.allow_over_budget,
                    require_idle=body.require_idle,
                    require_active=body.require_active,
                    context_maintenance=body.context_maintenance,
                    context_rollover=body.context_rollover,
                    expected_session_id=body.expected_session_id,
                    delegated=body.delegated,
                    role=body.role,
                    session_token=body.session_token,
                    **(
                        {"command_artifacts": body.command_artifacts}
                        if body.command_artifacts is not None
                        else {}
                    ),
                    **({"correlation": correlation} if correlation is not None else {}),
                    **(
                        {"fork_from": ForkFrom(body.fork_session_id, body.fork_at)}
                        if body.fork_session_id and body.fork_at
                        else {}
                    ),
                    **version,
                ),
            )
            if correlation is not None:
                # The durable receipt decides, not this answer: a rejection can carry a session
                # id whether or not its write began, and only the receipt tells the two apart.
                # The pair is reported exactly when the receipt proves participation, and
                # `run_participation` says so explicitly, so the deck never has to guess.
                receipt = host.delivery_receipt(body.key, str(body.delivery_id))
                stored = DeliveryCorrelation.from_receipt(receipt)
                participated = stored is not None and delivery_participated(receipt)
                result.work_id = stored.work_id if participated else None
                result.run_id = stored.run_id if participated else None
                result.run_participation = participated
            return result

        return send

    async def _deliver_idle_worker_session(
        provider: str,
        label: str,
        session_id: str,
        message: str,
        *,
        images: list[str],
        effort: str | None,
        delivery_id: str | None,
        expected_last_result_at: float,
    ) -> InjectResult:
        """Address one keyed worker by session id and refuse an active turn.

        A revival arrives session-addressed from the web model, while the atomic
        idle fence lives on the keyed worker host. Resolve the durable key here,
        inside the persistent runtime, and use that existing fence rather than
        trusting a collector snapshot from the web process.
        """
        host = _worker_host(provider, label)
        key = host.find_key_by_session(session_id)
        role = host.worker_role(key) if key is not None else None
        if key is None or role != "kanban":
            return InjectResult(False, "this session is not an addressable owned worker")
        result = await _deliver_worker(
            provider,
            label,
            DeliverRequest(
                key=key,
                message=message,
                images=images,
                effort=effort,
                delivery_id=delivery_id,
                client_action_id=delivery_id,
                require_idle=True,
                expected_last_result_at=expected_last_result_at,
                delegated=role != "primary",
                role=role,
            ),
            action="routine_revival",
        )
        return InjectResult(result.accepted, result.reason, result.session_id)

    @app.get("/workers/accounts/{provider}/{label}/workers")
    async def workers_state(
        provider: str, label: str, since: float | None = None
    ) -> dict[str, Any]:
        snapshot = _worker_host(provider, label).snapshot()
        since_at = datetime.fromtimestamp(since, tz=UTC) if since is not None else None
        workers = snapshot.get("workers") if isinstance(snapshot, dict) else None
        if isinstance(workers, dict) and workers:
            # One read of the store for the whole account, off the loop (#1553).
            # Asked per worker this cost `workers x artifacts` synchronous parses on
            # the event loop -- 245,626 of them for one request at 643 registered
            # workers -- and the runtime stopped accepting connections entirely.
            coverage = await asyncio.to_thread(artifact_store.coverage_index, since=since_at)
            for worker in workers.values():
                session_id = worker.get("session_id") if isinstance(worker, dict) else None
                if isinstance(session_id, str) and session_id:
                    session_key = f"{provider}:{label}:{session_id}"
                    uncovered = coverage.uncovered_for(session_key)
                    worker["model_visible_tool_bytes"] = (
                        None if uncovered else coverage.bytes_for(session_key)
                    )
                    worker["command_artifact_coverage"] = {
                        "complete": not uncovered,
                        "uncovered": uncovered,
                    }
        return snapshot

    @app.post("/command-artifacts/{artifact_id}/slice")
    async def command_artifact_slice(
        artifact_id: str, body: ArtifactSliceRequest
    ) -> dict[str, Any]:
        try:
            return artifact_store.read_slice(
                artifact_id,
                session_key=body.session_key,
                card=body.card,
                session_token=body.session_token,
                slice_capability=body.slice_capability,
                stream=body.stream,
                offset=body.offset,
                max_bytes=body.max_bytes,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/command-artifacts/{artifact_id}/full")
    async def command_artifact_full(
        artifact_id: str, body: ArtifactFullRequest
    ) -> dict[str, Any]:
        try:
            return artifact_store.read_full(
                artifact_id,
                session_key=body.session_key,
                card=body.card,
                session_token=body.session_token,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/workers/accounts/{provider}/{label}/delivery-receipt")
    async def worker_delivery_receipt(
        provider: str, label: str, key: str, delivery_id: str
    ) -> dict[str, Any]:
        host = _worker_host(provider, label)
        return {
            "receipt": host.delivery_receipt(key, delivery_id),
            "worker": host.snapshot().get("workers", {}).get(key),
        }

    @app.put("/workers/accounts/{provider}/{label}/context-delivery-resolution")
    async def worker_context_delivery_resolution(
        provider: str, label: str, body: ContextDeliveryResolutionRequest
    ) -> dict[str, Any]:
        result = await _worker_host(provider, label).resolve_context_delivery(
            body.key, body.delivery_id, body.choice, body.rationale
        )
        return _deliver_result(result)

    @app.get("/workers/accounts/{provider}/{label}/session-version")
    async def worker_session_version(
        provider: str, label: str, session_id: str
    ) -> dict[str, Any]:
        host = _worker_host(provider, label)
        key = host.find_key_by_session(session_id)
        role = host.worker_role(key) if key is not None else None
        if key is None or role not in {"kanban", "primary"}:
            raise HTTPException(status_code=404, detail="addressable owned worker not found")
        worker = host.snapshot().get("workers", {}).get(key, {})
        return {"last_result_at": float(worker.get("last_result_at", 0.0))}

    @app.post("/workers/accounts/{provider}/{label}/deliver")
    async def worker_deliver(
        provider: str, label: str, body: DeliverRequest
    ) -> dict[str, Any]:
        result = await _deliver_worker(provider, label, body, action="worker_deliver")
        return _deliver_result(result)

    @app.post(ENSURE_DELIVERY_PATH)
    async def ensure_delivery_route(body: EnsureDeliveryRequest) -> dict[str, Any]:
        """The Poller's direct machine delivery, with no web process in the path (#2144)."""

        async def schedule(
            request: EnsureDeliveryRequest, fields: dict[str, Any]
        ) -> tuple[DeliverResult, Placement]:
            """Choose the target, take the slot and freeze the route under one lock (#2146)."""
            delivery = DeliverRequest(**fields)

            async def run(admit: Admit) -> tuple[DeliverResult, Placement]:
                async def deliver_to(
                    provider: str, label: str, freeze: Callable[[], bool]
                ) -> DeliverResult:
                    here = await asyncio.to_thread(
                        _fresh_if_behind_its_chat, request, delivery, provider, label
                    )
                    send = _worker_send(provider, label, here, action="ensure_delivery")

                    async def frozen_send() -> DeliverResult:
                        # A second of slack: the reservation's clock is the web process's.
                        looked_at = datetime.now(UTC) - timedelta(seconds=1)
                        # Before the freeze: a refusal here leaves no route behind.
                        refused = await _prior_run_in_the_way(
                            request, provider, label, delivery.session_token
                        )
                        if refused is not None:
                            return refused
                        if not freeze():
                            return DeliverResult(False, "rejected", reason=ROUTING_UNAVAILABLE)
                        raced = await asyncio.to_thread(
                            _continued_since, request, looked_at, delivery.session_token
                        )
                        if raced is not None:
                            # Nothing was written: the route goes, and the next try decides.
                            budget_db.release_delivery_route(request.delivery_id)
                            return raced
                        return await send()

                    return await admit(provider, label, here, frozen_send)

                return await deliver_scheduled(
                    request,
                    request.requirements,
                    selector=Selector(
                        hosts=kanban_capacity.hosts(),
                        db=budget_db,
                        usage_cache=shared_cache_dir(config.usage.shared_cache_dir),
                    ),
                    deliver_to=deliver_to,
                    receipt_of=lambda provider, label: _worker_host(
                        provider, label
                    ).delivery_receipt(request.worker_key, request.delivery_id),
                )

            return await kanban_capacity.schedule(run)

        result, session_key, placement = await ensure_delivery(
            body,
            db=budget_db,
            context_mode=config.context_management.mode,
            schedule=schedule,
        )
        payload = _deliver_result(result)
        payload["contract"] = ENSURE_DELIVERY_CONTRACT
        if session_key:
            payload["session_key"] = session_key
        if placement is not None:
            # The Run trace's view of where this delivery went, or why nowhere (#2146).
            payload["schedule"] = placement.trace()
        return payload

    @app.post("/workers/accounts/{provider}/{label}/deliver-command-artifacts-v1")
    async def worker_deliver_with_command_artifacts(
        provider: str, label: str, body: DeliverRequest
    ) -> dict[str, Any]:
        if not isinstance(body.command_artifacts, dict) or str(
            body.command_artifacts.get("mode") or "off"
        ) not in {"observe", "enforce"}:
            raise HTTPException(status_code=422, detail="active command artifact policy required")
        result = await _deliver_worker(
            provider, label, body, action="worker_deliver_command_artifacts_v1"
        )
        return _deliver_result(result)

    @app.post("/workers/accounts/{provider}/{label}/revive-session")
    async def worker_revive_session(
        provider: str, label: str, body: WorkerSessionReviveRequest
    ) -> dict[str, Any]:
        return _result(
            await _deliver_idle_worker_session(
                provider,
                label,
                body.session_id,
                body.message,
                images=body.images,
                effort=body.effort,
                delivery_id=body.delivery_id or body.client_action_id,
                expected_last_result_at=body.expected_last_result_at,
            )
        )

    @app.post("/workers/accounts/{provider}/{label}/release")
    async def worker_release(
        provider: str, label: str, body: WorkerKeyRequest
    ) -> dict[str, Any]:
        # The Run this worker last served, read before the record goes: once released, the
        # worker row that proved its identity is gone (#2159).
        row = _worker_host(provider, label).snapshot().get("workers", {}).get(body.key)
        result = await kanban_capacity.release(
            provider,
            label,
            body.key,
            lambda: _worker_host(provider, label).release_worker(body.key),
        )
        if result.accepted and executions is not None and isinstance(row, dict):
            work_id, run_id = row.get("work_id"), row.get("run_id")
            if isinstance(work_id, str) and work_id and isinstance(run_id, str) and run_id:
                try:
                    await asyncio.to_thread(executions.outcome, work_id, run_id)
                except Exception:  # noqa: BLE001 -- the release already happened
                    log.exception("could not record %s/%s after release", work_id, run_id)
        return _deliver_result(result)

    @app.get("/claude/accounts/{label}/workers")
    async def claude_workers_state(label: str) -> dict[str, Any]:
        # The revivals still owed ride along, so the deck can tell a cut-off turn the
        # runtime will bring back from one nobody will (#3008).
        snapshot = claude_workers.host(label).snapshot()
        return {**snapshot, "revival": restart_nudges.revivals_due()}

    @app.post("/claude/accounts/{label}/deliver")
    async def claude_deliver(label: str, body: DeliverRequest) -> dict[str, Any]:
        result = await _deliver_worker(
            "claude_code", label, body, action="claude_deliver"
        )
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/spare")
    async def claude_spare(label: str, body: SpareRequest) -> dict[str, Any]:
        state = await claude_workers.host(label).prepare_spare(
            body.shape,
            body.key,
            session_token=body.session_token,
            cwd=body.cwd,
            model=body.model,
            effort=body.effort,
            role=body.role,
        )
        return {"state": state}

    @app.delete("/claude/accounts/{label}/spare")
    async def claude_drop_spare(label: str, shape: str) -> dict[str, Any]:
        return {"state": await claude_workers.host(label).drop_spare(shape)}

    @app.post("/claude/accounts/{label}/interrupt")
    async def claude_interrupt(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return _deliver_result(await claude_workers.host(label).interrupt(body.key))

    @app.post("/claude/accounts/{label}/bypass-permissions")
    async def claude_bypass_permissions(
        label: str, body: WorkerBypassPermissionsRequest
    ) -> dict[str, Any]:
        return _deliver_result(
            await claude_workers.host(label).set_bypass_permissions(body.key, body.enabled)
        )

    def _claude_host_for(account_key: str) -> ClaudeWorkerHost:
        for label, account in claude_workers.accounts.items():
            if account.key == account_key:
                return claude_workers.host(label)
        raise HTTPException(status_code=404, detail="unknown Claude account")

    @app.post("/claude/blocks")
    async def claude_report_block(body: ClaudeBlockRequest) -> dict[str, Any]:
        block_id, refusal = await _claude_host_for(body.account_key).report_block(
            body.key,
            session_token=body.session_token,
            tool_name=body.tool_name,
            tool_input=body.tool_input,
            reason=body.reason,
        )
        return {"id": block_id, "reason": refusal}

    @app.get("/claude/blocks/{block_id}")
    async def claude_wait_block(
        block_id: str, account_key: str, key: str, wait: float = 25.0
    ) -> dict[str, Any]:
        state = await _claude_host_for(account_key).wait_block(
            key, block_id, timeout_s=max(0.0, wait)
        )
        return {"state": state}

    @app.post("/claude/accounts/{label}/answer")
    async def claude_answer(label: str, body: WorkerAnswerRequest) -> dict[str, Any]:
        return _deliver_result(
            await claude_workers.host(label).answer(
                body.key,
                body.interaction_id,
                answers=body.answers,
                decision=body.decision,
            )
        )

    @app.post("/claude/accounts/{label}/stop")
    async def claude_stop(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        host = claude_workers.host(label)
        result = await kanban_capacity.release(
            "claude_code",
            label,
            body.key,
            lambda: host.stop_worker(body.key),
            inactive=lambda: not host.has_live_worker(body.key),
        )
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/rewind")
    async def claude_rewind(label: str, body: WorkerRewindRequest) -> dict[str, Any]:
        result = await kanban_capacity.release(
            "claude_code",
            label,
            body.key,
            lambda: claude_workers.host(label).rewind_worker(
                body.key, body.at, since=body.since
            ),
        )
        if result.accepted and body.since is not None and config.jobs.enabled:
            await jobs.silence(body.session_keys or [], body.since)
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/park")
    async def claude_park(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        result = await kanban_capacity.release(
            "claude_code",
            label,
            body.key,
            lambda: claude_workers.host(label).park_worker(body.key),
        )
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/release")
    async def claude_release(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        result = await kanban_capacity.release(
            "claude_code",
            label,
            body.key,
            lambda: claude_workers.host(label).release_worker(body.key),
        )
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/forget")
    async def claude_forget(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        host = claude_workers.host(label)
        result = await kanban_capacity.forget(
            "claude_code",
            label,
            body.key,
            lambda: host.forget(body.key),
            lambda: host.has_worker(body.key),
        )
        return {
            "removed": result.accepted,
            **({"reason": result.reason} if result.reason else {}),
        }

    @app.get("/accounts/{label}/state")
    async def state(label: str) -> dict[str, Any]:
        snapshot = runtime.snapshot(label)
        worker_snapshot = codex_workers.host(label).snapshot()
        snapshot["workers"] = worker_snapshot.get("workers", {})
        snapshot[SNAPSHOT_CAPABILITIES_KEY] = worker_snapshot.get(
            SNAPSHOT_CAPABILITIES_KEY, []
        )
        return snapshot

    @app.post("/accounts/{label}/start")
    async def start(label: str, body: StartRequest) -> dict[str, Any]:
        client = runtime.client(label)
        return _result(
            await _timed_action(
                "new_session",
                label,
                body.client_action_id,
                lambda: client.start_thread(
                    Path(body.cwd),
                    body.message,
                    images=_paths(body.images),
                    sandbox=body.sandbox,
                    model=body.model,
                    effort=body.effort,
                    approval_policy=body.approval_policy,
                    delegated=body.delegated,
                    session_token=body.session_token,
                    fork_from=(
                        ForkFrom(body.fork_thread_id, body.fork_turn_id)
                        if body.fork_thread_id and body.fork_turn_id
                        else None
                    ),
                ),
            )
        )

    @app.post("/accounts/{label}/queue")
    async def queue(label: str, body: TurnRequest) -> dict[str, Any]:
        client = runtime.client(label)
        images, root = _preserve_images(client.account, body.images)
        cleanup_now = True
        try:
            result = await _timed_action(
                "send",
                label,
                body.client_action_id,
                lambda: runtime.deliver_to_session(
                    label,
                    body.thread_id,
                    body.message,
                    images=images,
                    effort=body.effort,
                    operation="queue",
                    delivery_id=body.client_action_id,
                    send=lambda: client.queue_turn(
                        body.thread_id,
                        body.message,
                        images=images,
                        effort=body.effort,
                    ),
                ),
            )
            if root is not None and result.accepted:
                runtime.defer_upload_cleanup(client, body.thread_id, root)
                cleanup_now = False
            return _deliver_result(result)
        finally:
            if root is not None and cleanup_now:
                shutil.rmtree(root, ignore_errors=True)

    @app.post("/accounts/{label}/wait")
    async def wait(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).wait_for_thread(body.thread_id))

    @app.post("/accounts/{label}/compact")
    async def compact(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).compact(body.thread_id))

    @app.post("/accounts/{label}/permission-prompts")
    async def permission_prompts(
        label: str, body: PermissionPromptsRequest
    ) -> dict[str, Any]:
        return _result(
            await runtime.set_permission_prompts(label, body.thread_id, body.enabled)
        )

    @app.post("/accounts/{label}/steer")
    async def steer(label: str, body: TurnRequest) -> dict[str, Any]:
        return _deliver_result(
            await _timed_action(
                "steer",
                label,
                body.client_action_id,
                lambda: runtime.deliver_to_session(
                    label,
                    body.thread_id,
                    body.message,
                    images=_paths(body.images),
                    effort=None,
                    operation="steer",
                    delivery_id=body.client_action_id,
                    send=lambda: runtime.client(label).steer(
                        body.thread_id, body.message, images=_paths(body.images)
                    ),
                ),
            )
        )

    @app.post("/accounts/{label}/interrupt")
    async def interrupt(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(
            await _timed_action(
                "stop",
                label,
                body.client_action_id,
                lambda: runtime.client(label).interrupt(body.thread_id),
            )
        )

    @app.post("/accounts/{label}/answer")
    async def answer(label: str, body: AnswerRequest) -> dict[str, Any]:
        return _result(
            await _timed_action(
                "interaction",
                label,
                body.client_action_id,
                lambda: runtime.client(label).answer(
                    body.thread_id,
                    body.interaction_id,
                    answers=body.answers,
                    decision=body.decision,
                ),
            )
        )

    return app

