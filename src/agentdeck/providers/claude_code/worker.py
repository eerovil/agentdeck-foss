"""Deck-owned Claude Code worker processes (stream-json control channel).

Each worker is one long-lived ``claude -p --input-format stream-json`` process.
The host keys workers by an opaque *dedupe key* and exposes one idempotent
primitive — ``deliver(key, delivery_id, message)`` — which steers a live worker, revives a
finished session (``--resume``), or spawns a fresh one. Callers (e.g. an
external board poller) never track process state themselves.

Transcripts are written by the CLI into ``<account root>/projects/``, so the
ordinary ClaudeCodeProvider scan picks deck-owned workers up with no extra
plumbing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ... import classifier_grants, claude_env, gh_shim, github_identity, worker_limits
from ...chat_cwd import continued_chat_cwd, cwd_exists
from ...command_artifacts import (
    ACCOUNT_KEY_ENV,
    CONTEXT_DIR_ENV,
    POLICY_MODE_ENV,
    SESSION_TOKEN_ENV,
    WORKER_KEY_ENV,
    ensure_claude_settings,
)
from ...images import UnsupportedImage, read_supported_image
from ...models import (
    OPEN_QUESTIONS_PREFIX,
    RUNTIME_BOOTED_AT,
    SNAPSHOT_BOOTED_AT_KEY,
    SNAPSHOT_CAPABILITIES_KEY,
    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
    SNAPSHOT_FORK_V1,
    SNAPSHOT_RELOCATE_V1,
    SNAPSHOT_REWIND_V1,
    SNAPSHOT_SPARE_V1,
    WORKER_SNAPSHOT_CAPABILITIES,
    Account,
    ForkFrom,
    open_questions_id,
)
from ...worker_limits import EXIT_MEMORY_CAP
from .. import account_health
from ..delivery import (
    DeliverResult,
    DeliveryCorrelation,
    DeliveryReceipts,
)
from ..instructions import (
    SessionRole,
    agentdeck_session_instructions,
    relocation_notice,
    resolve_session_role,
    stored_session_role,
)
from ..usage import read_shared_cache
from . import permission_rules, registry
from . import transcripts as transcripts_mod
from .held import (
    OFFSET_SAVE_INTERVAL_S,
    HeldProcess,
    HolderInfo,
    HolderStore,
    kill_holder,
    spawn_holder,
)

log = logging.getLogger(__name__)

# One JSON event per stdout line; large tool results can exceed asyncio's
# default 64 KiB StreamReader limit (same rationale as the Codex app-server).
STDOUT_LINE_LIMIT = 16 * 1024 * 1024

INTERRUPT_TIMEOUT_S = 15.0
INIT_TIMEOUT_S = 30.0

# The ``result`` subtype the CLI emits when a turn is abandoned rather than
# finished — including the one it writes on its way out when the runtime's death
# tears the process down. It is the only result that leaves the chat still owing
# an answer, so it is the only one that does not close ``turn_open_at`` (#1844).
RESULT_ABORTED = "error_during_execution"

# What an open question's tool call returns when a message reaches the chat before anyone
# picked an option (#3121), in place of the CLI's own "the user doesn't want to proceed".
# The question itself stays open on the deck (#3451), so the agent must not ask it again.
QUESTION_SUPERSEDED = (
    "A new message arrived before the question was answered. The question stays on the"
    " person's screen and their answer will come as a later message. Do not ask it again;"
    " read the new message and carry on, or end your turn if you need the answer first."
)
# How many answered or dismissed questions' request ids a chat remembers, so a held
# worker's log read again after a restart cannot open them a second time (#3451).
CLOSED_QUESTIONS_MAX = 50

# The mode a chat runs in while its "Bypass permissions" switch is on (#2285):
# every tool call runs without a permission check, the classifier included. The
# CLI only accepts a live switch to it from a process started with
# ``--allow-dangerously-skip-permissions``, which every owned worker gets; that
# flag makes the mode available without turning it on.
BYPASS_PERMISSIONS_MODE = "bypassPermissions"
# Where a new owned chat's switch starts is the host's ``start_bypassed``
# (#2319, a setting since #3748 so a public install runs in its configured mode).
# Only a new record takes it: a saved record keeps its own choice, and one
# written before #2285 loads as off, the behaviour it was created under.
# Modes a new chat keeps even when it starts bypassed: "plan" is how a read-only
# delegation stays read-only, and bypass would lift it.
NO_DEFAULT_BYPASS_MODES = frozenset({"plan"})

# The id prefix of a classifier block shown as a pending prompt (#2271). A block
# is not a `can_use_tool` control_request: Claude refused the call inside the CLI
# and the deck's PermissionDenied hook reported it, so its answer goes back to
# that hook rather than over the control channel.
BLOCK_ID_PREFIX = "block-"
# The longest one long poll from the hook is held open.
BLOCK_WAIT_MAX_S = 60.0

# Published usage snapshots older than this are treated as unknown rather than
# trusted — a stalled collector must not permanently block (or greenlight) work.
USAGE_MAX_AGE_S = 1800.0

_TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
_TASK_STATUS_RE = re.compile(r"<status>([^<]+)</status>")

# A background Agent id whose stop notification never arrives (the parent turn
# was interrupted, the CLI died mid-task, a notification shape we don't parse)
# would otherwise count as "effective activity" for the life of the worker
# process — and one such id defers every unattended runtime deploy (#786). No
# real Claude-native Task on this host runs anywhere near this long, so an id
# older than this is treated as leaked and dropped.
BACKGROUND_AGENT_MAX_AGE_S = 2 * 3600.0
# A worker handed a message this recently is never reclaimed as idle to make room
# for another (#3009). A resumed CLI can end a turn of its own right after the
# write, which reads as idle while the message it was just given is still in its
# queue: a person's message was killed two seconds after it landed that way.
IDLE_RECLAIM_GRACE_S = 60.0

# How many messages queued behind one running turn a chat's record keeps for
# revival (#2898). A person sends a handful; the newest are the ones that matter.
MAX_UNREAD_MESSAGES = 10
# Transcript lines are not written in strict time order, and the record's stamp
# is taken just after the CLI took the message.
UNREAD_CLOCK_SLACK_S = 5.0


# What the CLI actually says when the account's own window is spent. Observed on this host as
# a *synthetic* assistant message — `model: "<synthetic>"`, `isApiErrorMessage: true` — which
# the CLI then reports as the turn's ordinary answer:
#
#   {"type": "result", "subtype": "success", "is_error": false,
#    "result": "You've hit your weekly limit · resets 11am (Europe/Helsinki)"}
#
# So a hard quota must NOT be looked for only on an error result. `error_during_execution` is
# what the CLI emits when it throws (a crash), and the limit is not a crash: the turn finishes,
# having said nothing but this. Requiring `is_error` here is what would make the whole circuit
# a no-op against the only shape production produces (#1733).
_HARD_QUOTA_MARKERS = re.compile(
    r"""
      you'?ve\ hit\ your\ (?:weekly|usage|session|5-hour|five-hour)\ limit
    | you\ have\ hit\ your\ (?:weekly|usage|session|5-hour|five-hour)\ limit
    | (?:claude\ )?usage\ limit\ reached
    | (?:reached|hit)\ your\ (?:weekly\ )?usage\ limit
    | (?:weekly|5-hour|five-hour)\ (?:usage\ )?limit\ reached
    | maximum\ weekly\ usage
    | usage_limit_exceeded
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The synthetic refusal is one short line and nothing else. A worker's own prose can quote the
# same words — a turn spent working on this very issue does — and that must not cost the
# account its eligibility, so text the provider did not label an error has to *be* the refusal
# rather than merely contain it.
_SYNTHETIC_REFUSAL_MAX_CHARS = 200


def _plain_apostrophes(value: str) -> str:
    """The CLI renders this line with either apostrophe; match one alphabet, not two."""
    return value.replace("’", "'")


def _hard_quota_evidence(event: dict[str, Any]) -> str | None:
    """Conservatively identify account-global Claude quota exhaustion.

    Generic 429/rate-limit wording is intentionally excluded: transient throttling must not
    poison the account. Two shapes count, and only these two:

    * a field the provider itself labels an error (``errors``, ``error``) saying the account's
      usage window is exhausted — wording may sit anywhere inside it, because the provider
      already vouched that the string is its own verdict and not the agent's prose; and
    * a successful result whose whole answer *is* the synthetic refusal line, which is how the
      CLI delivers a spent window.

    Nothing else in the event is read. Scanning every nested string would let a worker's own
    transcript — tool output, a quoted issue body — trip its account's breaker.
    """
    if event.get("type") != "result":
        return None

    provider_errors: list[str] = []
    stack: list[object] = [event.get("errors"), event.get("error")]
    while stack:
        current = stack.pop()
        if isinstance(current, str):
            provider_errors.append(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    for value in provider_errors:
        if _HARD_QUOTA_MARKERS.search(_plain_apostrophes(value)):
            return value[:500]

    subtype = str(event.get("subtype") or "").lower()
    answer = event.get("result")
    if not isinstance(answer, str):
        return None
    text = answer.strip()
    if not text:
        return None
    normalized = _plain_apostrophes(text)
    if event.get("is_error") is True or subtype in {"error", "error_during_execution"}:
        # An error result's text is the provider's own message, so the marker may sit anywhere.
        return text[:500] if _HARD_QUOTA_MARKERS.search(normalized) else None
    # A successful result's text is whatever the agent said. Only the synthetic refusal shape
    # counts: one line, short, and opening with the refusal itself.
    if "\n" in text or len(text) > _SYNTHETIC_REFUSAL_MAX_CHARS:
        return None
    match = _HARD_QUOTA_MARKERS.search(normalized)
    if match is None or match.start() > 0:
        return None
    return text[:500]


# A turn the CLI gave up on because this host could not reach the model at all (#3086). The
# CLI retries the request ten times over about twenty minutes, then answers the turn with one
# synthetic line such as "API Error: Can't reach the API server — check your internet or DNS
# (EAI_AGAIN)" and reports an ordinary successful result. A night-long network outage left
# twelve chats stopped that way with nothing to restart them. Only the network codes count: an
# overloaded or refused request is the provider's answer, not a lost connection.
_NETWORK_FAILURE_MARKERS = re.compile(
    r"\b(?:ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|ENETUNREACH|EHOSTUNREACH)\b"
    r"|can't reach the api server",
    re.IGNORECASE,
)
# How long after a lost connection the deck asks the chat to continue. The CLI already spent
# its own retries, so this is about the network coming back, not about a blip.
NETWORK_RETRY_DELAY_S = 600.0
# A turn the CLI ended at once because it could not refresh the account's sign-in token
# while another Claude process on this host was refreshing the same one (#3165). Every deck
# chat on an account shares one credentials file, so a wake that lands as another chat's
# token refresh is under way loses the race. The CLI answers with one synthetic line such as
# "Failed to refresh OAuth token: another Claude Code process is refreshing it or exited
# mid-refresh. This is usually transient; retry in a minute, ..." and reports success.
# Only that race counts: a revoked or expired sign-in fails the same refresh for good and
# needs a person, so retrying it would only repeat the failure.
_LOGIN_REFRESH_FAILURE = re.compile(
    r"^failed to refresh oauth token: another claude code process is refreshing it",
    re.IGNORECASE,
)
# The CLI's own advice is a minute; two leave the other refresh time to land.
LOGIN_REFRESH_RETRY_DELAY_S = 120.0


def _transient_failure_retry_delay(event: dict[str, Any]) -> float | None:
    """How long to wait before continuing a turn the CLI ended on a passing failure.

    Two failures count: its own "API Error" line for a connection it never made, and its
    sign-in refresh losing a race with another process. Like the quota refusal, the line has
    to *be* the answer: one short line opening with the CLI's own words. A worker's prose
    that quotes such a line is not the failure. ``None`` when the result is neither.
    """
    if event.get("type") != "result":
        return None
    answer = event.get("result")
    if not isinstance(answer, str):
        return None
    text = _plain_apostrophes(answer.strip())
    # The sign-in line runs to about 210 characters, a little over the usual cap.
    if "\n" in text or len(text) > _SYNTHETIC_REFUSAL_MAX_CHARS * 2:
        return None
    if _LOGIN_REFRESH_FAILURE.search(text):
        return LOGIN_REFRESH_RETRY_DELAY_S
    if len(text) > _SYNTHETIC_REFUSAL_MAX_CHARS:
        return None
    if text.startswith("API Error:") and _NETWORK_FAILURE_MARKERS.search(text):
        return NETWORK_RETRY_DELAY_S
    return None


class WorkerError(RuntimeError):
    """A Claude worker process could not complete a request.

    ``write_started`` marks a failure that happened *after* the user message was
    already written to the child — the payload may have been delivered, so the
    caller must not re-send it (that would run the task twice)."""

    def __init__(self, *args: object, write_started: bool = False) -> None:
        super().__init__(*args)
        self.write_started = write_started


@dataclass
class WorkerRecord:
    """Durable per-key state — survives runtime restarts via the state file."""

    key: str
    cwd: str
    session_id: str | None = None
    last_delivery_at: float = 0.0
    last_result_at: float = 0.0
    last_result_subtype: str | None = None
    # The account's own "usage limit reached" line when the last result was one, else "".
    # The CLI calls that turn ``success``, so without this a turn the limit cut short reads
    # as one that finished (#2862).
    last_result_limit: str = ""
    # When the deck should continue this chat by itself, as a UTC epoch: the parsed provider
    # reset for ``last_result_limit``, or a short wait after a lost connection (#3086). Zero
    # when the latest result was neither, or a refusal without the supported reset shape.
    last_result_limit_reset_at: float = 0.0
    # When the turn now in flight opened, or 0.0 when this chat owes no answer.
    # Revival cannot infer that from the two stamps above: a worker torn down
    # mid-turn emits a final ``result`` on its way out, so the death writes a
    # result *newer* than the delivery and an interrupted turn reads exactly like
    # a finished one (#1844). Recording the open turn instead of deducing it also
    # sees a delivery that dies in the same second it arrived, which no
    # comparison of the two can. Opened at the provider-write boundary, because a
    # death inside the write or the initialization wait still leaves a turn in the
    # child. Cleared by a result that genuinely ended the turn —
    # ``error_during_execution`` is what a teardown writes, so it clears nothing —
    # and by ``_close_turn_marker`` for a turn somebody ended on purpose.
    turn_open_at: float = 0.0
    # When somebody deliberately ended this chat's turn — a stop, a park, an
    # acknowledged interrupt. Zeroing ``turn_open_at`` is not enough on its own:
    # revival's fallback deduction (delivery newer than result) still reads true
    # when no terminal ``result`` ever arrived, which is exactly what a SIGKILL
    # escalation leaves behind. A positive stamp is also what keeps a record
    # written before this field existed out of it: absent means "nothing
    # deliberate was recorded", not "cancelled" (#1844).
    turn_closed_at: float = 0.0
    # Spawn-time settings reused on every revive so a follow-up cannot silently
    # change the chat's policy or reasoning level.
    permission_mode: str | None = None
    # Whether the boss switched this chat to BYPASS_PERMISSIONS_MODE (#2285). On,
    # the process runs in that mode; off, in ``permission_mode`` above, which the
    # switch never overwrites, so switching off restores it. Durable so a revive
    # keeps the choice. A new chat's record starts at the host's ``start_bypassed``.
    bypass_permissions: bool = False
    # Trusted creation provenance. Persist it so a revived worker receives the
    # same orchestration role as the original process.
    delegated: bool = False
    role: SessionRole | None = None
    # Deck-minted self-identification token, recorded at spawn so a revived
    # worker resolves the same session key it was first told about.
    session_token: str | None = None
    model: str | None = None
    effort: str | None = None
    command_artifacts: dict[str, Any] | None = None
    deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_context_delivery_id: str | None = None
    pending_context_operation: str | None = None
    pending_context_source_session_id: str | None = None
    # Why this chat's worker process last died, when the runtime could name it.
    # Only ``EXIT_MEMORY_CAP`` today (#1546): an ordinary exit says nothing here,
    # because "the process ended" is already what ``live: false`` reports. Durable
    # so the chat still explains itself after a runtime restart, and cleared on
    # the next successful spawn.
    last_exit_reason: str = ""
    last_exit_at: float = 0.0
    # The directory this chat ran in before the runtime moved it to the repository
    # because that directory was gone (#2524). Set by the move, told to the resumed
    # process once in its system prompt, and cleared by that spawn.
    relocated_from: str | None = None
    # The transcript line the next turn resumes at, after a person rolled the chat back
    # to before one of their messages (#3474). The CLI branches the same session file
    # there, and a plain resume afterwards follows that branch, so it is cleared once a
    # message has been written to a process started with it. The session it names
    # beside it: a cut means nothing to any other session this record moves on to.
    resume_at: str | None = None
    resume_at_session: str | None = None
    # Messages written into this chat while a turn was running, oldest first, as
    # ``{"delivery_id", "text", "images", "at"}``. The CLI only queues such a
    # message in memory until the turn gets to it, so a death before then loses
    # it — and a resumed CLI does not replay its queue (#2898). Kept until a turn
    # ends cleanly, so revival can quote the ones the transcript never shows read.
    unread_messages: list[dict[str, Any]] = field(default_factory=list)
    # How many older entries the bound above pushed out of ``unread_messages``
    # since the turn opened, so revival can say they were lost (#2898).
    unread_dropped: int = 0
    # The questions this chat asked with AskUserQuestion that nobody has answered or
    # dismissed yet, oldest first, one entry per question (#3451). They outlive the tool
    # call, the turn and the runtime: a message reaching the chat or the turn ending no
    # longer takes a question off the person's screen. See ``_question_items``.
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    # Request ids of questions already answered or dismissed, newest last (#3451).
    closed_question_requests: list[str] = field(default_factory=list)

    @property
    def receipts(self) -> DeliveryReceipts:
        """At-most-once fence over this record's durable ``deliveries`` dict. A
        property (not a field) so it stays out of ``vars()`` and the on-disk
        state shape is unchanged."""
        return DeliveryReceipts(self.deliveries)


@dataclass
class _Block:
    """One classifier block waiting for the boss. ``decision`` resolves to
    ``"allowed"`` or ``"denied"``, or ``"gone"`` when the turn or process ends
    first."""

    id: str
    tool_name: str
    tool_input: dict[str, Any]
    reason: str
    decision: asyncio.Future[str]


@dataclass
class _LiveWorker:
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task
    initialized: asyncio.Future[str]
    turn_active: bool = False
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    # request_ids of outbound control_requests (interrupt, permission-mode
    # change) awaiting a control_response on this worker, so the reader can fail
    # them fast if the process exits before acking.
    pending_control_requests: set[str] = field(default_factory=set)
    # The current interactive `can_use_tool` control_request the agent is blocked
    # on (AskUserQuestion or a permission gate), or None. Its `request_id` is the
    # interaction id the web layer answers against. Only one is open at a time —
    # the turn is paused until we write the matching control_response.
    pending_interaction: dict[str, Any] | None = None
    # Tool calls Claude's classifier blocked, each waiting for the boss (#2271),
    # oldest first. The deck's PermissionDenied hook holds the CLI until the
    # block is answered, so these outlive neither the process nor the turn.
    blocks: dict[str, _Block] = field(default_factory=dict)
    # Claude-native async Agent tasks outlive the outer turn's `result` event.
    # Keep their ephemeral lifecycle here so deploy safety reflects effective
    # activity rather than only the top-level turn. Maps agent id → the time we
    # observed its launch, so an id whose stop notification never arrives ages
    # out (BACKGROUND_AGENT_MAX_AGE_S) instead of deferring deploys forever.
    background_agents: dict[str, float] = field(default_factory=dict)
    transcript_path: Path | None = None
    transcript_offset: int = 0
    # The transient systemd scope holding this process's memory cap, or "" when
    # the host could not give it one (#1546). Read once after the process dies:
    # a scope killed by its cap is the only thing that can tell a cap kill from
    # any other SIGKILL.
    scope_unit: str = ""
    # A held worker's key, bytes of its holder's log read so far, and when that position
    # was last saved (#3248). ``detached`` is a held worker this runtime let go of on its
    # way down: still running, and the next runtime's to reconnect to.
    holder_key: str = ""
    read_offset: int = 0
    offset_saved_at: float = 0.0
    detached: bool = False


# Where held workers' sockets live, beside -- never inside -- the runtime's own directory.
HOLDER_RUN_DIR = "agentdeck-holders"
# How long a held worker's holder may take to exit after its output ends (#3248).
HOLDER_EXIT_WAIT_S = 10.0
# A booted CLI waiting for a new chat's first message is replaced after this long, so
# it picks up CLAUDE.md, skill and CLI updates (#3420).
SPARE_MAX_AGE_S = 30 * 60.0
# How long to wait before booting again a waiting CLI that died on its own.
SPARE_REBOOT_DELAY_S = 30.0


@dataclass
class _Spare:
    """A Claude CLI booted ahead of a new chat, waiting for its first message (#3420).

    Booting the CLI costs about two seconds of CPU before it reads anything, which was
    most of the wait for a new chat. A spare is spawned exactly as that chat's fresh
    worker would be -- same key, token, argv, directory and environment -- so a start
    that asks for exactly this takes it over and only writes the message. It has no
    record and is not live, so nothing counts, lists or admits it until then.
    """

    shape: str
    key: str
    session_token: str
    cwd: str
    model: str | None
    effort: str | None
    command: list[str]
    env: dict[str, str]
    scope_unit: str
    process: Any
    holder: HolderInfo | None
    watcher: asyncio.Task | None = None
    # The role a deck-started Work's spare is booted with (#3590); None is a plain chat's.
    role: SessionRole | None = None
    # Taken over by a chat: its process is that chat's now, never the spare's to stop.
    adopted: bool = False
# Events after which a held worker's read position is saved at once (#3248): each changes
# what the next runtime would have to rebuild, so a restart never asks a question twice.
_HOLDER_SAVE_EVENTS = frozenset({"system", "result", "control_request"})


def _event_text(event: dict[str, Any]) -> str | None:
    """Extract task-notification text from Claude's stream/transcript shapes."""
    content = event.get("content")
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(texts) if texts else None
    return None


def _update_background_agents(live: _LiveWorker, event: dict[str, Any]) -> None:
    """Apply Claude-native async Agent launch, stop, and resume transitions."""
    tool_result = event.get("toolUseResult", event.get("tool_use_result"))
    if isinstance(tool_result, dict):
        agent_id = tool_result.get("agentId", tool_result.get("agent_id"))
        if tool_result.get("status") == "async_launched" and isinstance(agent_id, str):
            live.background_agents[agent_id] = time.time()
        resumed_id = tool_result.get(
            "resumedAgentId", tool_result.get("resumed_agent_id")
        )
        if isinstance(resumed_id, str):
            live.background_agents[resumed_id] = time.time()

    text = _event_text(event)
    if not text or "<task-notification>" not in text:
        return
    task_ids = _TASK_ID_RE.findall(text)
    status = _TASK_STATUS_RE.search(text)
    # The notification itself means the Agent run stopped; the raw status is a
    # reason, and Claude may add new reason spellings in future versions.
    if status is None:
        return
    for task_id in task_ids:
        live.background_agents.pop(task_id.strip(), None)


def _preview_tool_input(tool_input: object) -> str | None:
    """A compact one-line preview of a permission-gated tool's key input, so a
    non-Bash approval (Write/WebFetch/…) isn't answered blind."""
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "path", "url", "pattern", "query", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}: {value}"[:200]
    return None


def _normalize_block(block: _Block, session_id: str | None) -> dict[str, Any]:
    """A classifier block in the PendingInteraction shape (#2271)."""
    command = block.tool_input.get("command")
    description = block.tool_input.get("description")
    message = block.reason or (
        description if isinstance(description, str) and description else None
    )
    if not isinstance(command, str):
        command = None
        message = message or _preview_tool_input(block.tool_input)
    return {
        "id": block.id,
        "kind": "classifier_block",
        "thread_id": session_id or "",
        "title": f"Claude's safety check blocked {block.tool_name or 'a tool call'}",
        "message": message,
        "command": command,
        "decisions": ["accept", "decline"],
    }


def _normalize_interaction(
    pending: object, session_id: str | None
) -> dict[str, Any] | None:
    """Map a raw `can_use_tool` control_request to the provider-neutral
    PendingInteraction shape the shared widget renders. Lives in the runtime (which
    owns the CLI control channel) so the web process never parses Claude's wire
    schema — symmetric to how the Codex app-server normalizes before the socket."""
    if not isinstance(pending, dict):
        return None
    request_id = pending.get("request_id")
    if not isinstance(request_id, str):
        return None
    tool_name = pending.get("tool_name")
    tool_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
    if tool_name == "AskUserQuestion":
        questions = []
        for index, q in enumerate(tool_input.get("questions") or []):
            if not isinstance(q, dict) or not isinstance(q.get("question"), str):
                continue
            options = [
                {"label": o["label"], "description": o.get("description") or ""}
                for o in (q.get("options") or [])
                if isinstance(o, dict) and isinstance(o.get("label"), str)
            ]
            questions.append(
                {
                    "id": str(index),
                    "header": q.get("header") or "",
                    "prompt": q["question"],
                    "options": options,
                    "allow_other": True,  # Claude always lets you write your own
                    "multiselect": bool(q.get("multiSelect")),
                }
            )
        return {
            "id": request_id,
            "kind": "question",
            "thread_id": session_id or "",
            "title": "Claude is asking",
            "questions": questions,
        }
    description = pending.get("description")
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else None
    message = (
        description
        if isinstance(description, str) and description
        else _preview_tool_input(tool_input)
    )
    return {
        "id": request_id,
        "kind": "permission",
        "thread_id": session_id or "",
        "title": f"Allow {pending.get('display_name') or tool_name or 'this tool'}?",
        "message": message,
        "command": command,
        # No "acceptForSession": our control_response can't durably grant a
        # session-scoped rule, so we don't offer a button that wouldn't be honored.
        "decisions": ["accept", "decline", "cancel"],
    }


def _question_items(request_id: str, tool_input: dict[str, Any]) -> list[dict[str, Any]]:
    """One open-question entry per question an AskUserQuestion call asks (#3451).

    Each keeps its own id, ``<request id>.<index>``, so it can be answered or dismissed
    apart from the others in the same call.
    """
    items = []
    for index, q in enumerate(tool_input.get("questions") or []):
        if not isinstance(q, dict) or not isinstance(q.get("question"), str):
            continue
        items.append(
            {
                "id": f"{request_id}.{index}",
                "request_id": request_id,
                "index": index,
                "header": q.get("header") or "",
                "prompt": q["question"],
                "options": [
                    {"label": o["label"], "description": o.get("description") or ""}
                    for o in (q.get("options") or [])
                    if isinstance(o, dict) and isinstance(o.get("label"), str)
                ],
                "multiselect": bool(q.get("multiSelect")),
            }
        )
    return items


def _question_key(item: dict[str, Any]) -> tuple[str, str]:
    """What makes two open questions the same question: header and wording (#3637)."""
    def norm(value: object) -> str:
        return " ".join(str(value or "").split()).casefold()

    return norm(item.get("header")), norm(item.get("prompt"))


def _repeated_question_ids(items: list[dict[str, Any]]) -> set[str]:
    """Open questions asked again word for word by a later ask; only the newest stays.

    A chat cut off while it waited re-asks, and every copy stayed open beside the
    others (#3637). Copies inside one ask are both kept: only older asks give way.
    """
    newest: dict[tuple[str, str], str] = {}
    repeated: set[str] = set()
    for item in items:  # oldest ask first
        key = _question_key(item)
        older = newest.get(key)
        if older is not None and older != item["request_id"]:
            repeated |= {
                i["id"] for i in items if i["request_id"] == older and _question_key(i) == key
            }
        newest[key] = item["request_id"]
    return repeated


def _open_questions_view(
    items: list[dict[str, Any]], session_id: str | None
) -> dict[str, Any] | None:
    """A chat's open questions as one interaction for the shared widget.

    The newest ask comes first; the questions within one ask keep the order it gave them.
    """
    if not items:
        return None
    asks = list(dict.fromkeys(item["request_id"] for item in items))
    newest_first = [
        item for request_id in reversed(asks) for item in items
        if item["request_id"] == request_id
    ]
    return {
        "id": open_questions_id(item["id"] for item in newest_first),
        "kind": "question",
        "thread_id": session_id or "",
        "title": "Claude is asking",
        "dismissible": True,
        "questions": [
            {
                "id": item["id"],
                "header": item.get("header") or "",
                "prompt": item.get("prompt") or "",
                "options": item.get("options") or [],
                "allow_other": True,  # Claude always lets you write your own
                "multiselect": bool(item.get("multiselect")),
            }
            for item in newest_first
        ],
    }


def _answers_note(
    answered: list[tuple[dict[str, Any], list[str]]], still_open: list[dict[str, Any]]
) -> str:
    """What the agent reads when answers reach it other than as one whole tool answer."""
    lines = ["The person answered from the question box:"]
    lines += [f"- «{item['prompt']}»: {', '.join(values)}" for item, values in answered]
    if still_open:
        lines.append(
            "Still open on their screen; their answers will come as later messages, so do"
            " not ask these again:"
        )
        lines += [f"- «{item['prompt']}»" for item in still_open]
    return "\n".join(lines)


# The only decisions the UI sends to approve a tool call. Anything else denies (#427).
_APPROVE_DECISIONS = frozenset({"accept", "acceptForSession"})


class ClaudeWorkerHost:
    """Own all deck-managed Claude worker processes for one account."""

    def __init__(
        self,
        account: Account,
        *,
        state_dir: Path,
        max_workers: int = 4,
        permission_mode: str | None = "auto",
        start_bypassed: bool = False,
        model: str | None = None,
        usage_ceiling_pct: float = 0.0,
        usage_cache_dir: Path | None = None,
        stall_after_s: float = 0.0,
        idle_after_s: float = 0.0,
        interactive_prompts: bool = True,
        classifier_skip_rules: list[str] | None = None,
        memory_max: str = "",
        hold_processes: bool = False,
        base_url: str | None = None,
        process_factory=None,
        usage_reader=None,
        session_over_budget: Callable[[str], bool] | None = None,
        bind_session_token: Callable[[str, str], None] | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        del usage_ceiling_pct, session_over_budget
        self.account = account
        # Loopback base for AgentDeck's HTTP API, injected into each spawned
        # worker as AGENTDECK_URL so the agent can reach its own transcript
        # without guessing a host. None only in tests that never exercise it.
        self._base_url = base_url
        self.max_workers = max_workers
        self.permission_mode = permission_mode
        self.start_bypassed = start_bypassed
        self.model = model
        # Whether to spawn with `--permission-prompt-tool stdio` (surfaces
        # AskUserQuestion + permission gates to the user). Config escape hatch:
        # set false to revert to the old auto-run spawn without a code change.
        self.interactive_prompts = interactive_prompts
        self.stall_after_s = stall_after_s
        # Per-worker MemoryMax, or "" to spawn uncapped (#1546). Defaults to
        # uncapped so a directly-constructed host — every test double — keeps the
        # plain spawn unless it opts in; the runtime passes the configured value.
        self.memory_max = memory_max
        # Run each CLI under a holder so it outlives a runtime restart (#3248). Off for a
        # directly-constructed host, so a test double never starts one; the runtime turns
        # it on. It needs the worker's own scope: without one the holder would sit in the
        # runtime's cgroup and die with it, so an uncapped spawn is not held.
        self.hold_processes = hold_processes
        # Defaults to 0 (disabled) so a directly-constructed host — every test
        # double — keeps the old never-collect behaviour unless it opts in.
        self.idle_after_s = idle_after_s
        self._usage_cache_dir = usage_cache_dir
        self._usage_reader = usage_reader or self._read_published_usage
        self._bind_session_token = bind_session_token or (lambda _token, _key: None)
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._state_path = state_dir / f"{account.label}.json"
        self._artifact_root = artifact_root or state_dir / "command-artifacts"
        allow_rules = permission_rules.allow_rules(classifier_skip_rules or ())
        # Keyed by whether the chat runs in auto: only an auto chat gets the skip list.
        self._settings_files = {
            auto: ensure_claude_settings(
                self._artifact_root, allow_rules if auto else None, capture=False
            )
            for auto in (True, False)
        }
        # Answers to classifier blocks the hook has not collected yet, by block id.
        self._block_results: dict[str, str] = {}
        # Told that a worker (a key) or any of them (None) may have changed, so the
        # runtime's change feed can compare and tell the web (#2865). Called on every
        # stdout event, a process exit, and every save of the records.
        self.on_state_change: Callable[[str | None], None] = lambda _key: None
        self._records: dict[str, WorkerRecord] = {}
        self._live: dict[str, _LiveWorker] = {}
        self._control_waiters: dict[str, asyncio.Future] = {}
        # One answer to a chat's open questions at a time, so a double tap finds them
        # already answered rather than sending the same answer twice (#3451).
        self._question_locks: dict[str, asyncio.Lock] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._holder_store = HolderStore(state_dir / f"{account.label}.holders.json")
        self._holder_dir = state_dir / "holders" / account.label
        self._holders: dict[str, HolderInfo] = self._holder_store.load()
        # Booted CLIs waiting for a new chat, one per kind of start (#3420).
        self._spares: dict[str, _Spare] = {}
        self._load_state()
        for record in self._records.values():
            if record.session_token and record.session_id:
                self._bind_session_token(
                    record.session_token, f"{self.account.key}:{record.session_id}"
                )
                self._publish_artifact_context(record)
        self._reconcile_orphans()

    @staticmethod
    def _publish_artifact_context(record: WorkerRecord) -> bool:
        """Whether this record's command-artifact policy can be honoured.

        Only the poller's ``kanban`` workers ever had a capture context published; that
        role went in #3741, so an ``enforce`` policy, which needs one, is refused.
        """
        return str((record.command_artifacts or {}).get("mode") or "off") != "enforce"

    def _key_lock(self, key: str) -> asyncio.Lock:
        """Per-key serialization lock.

        Deliveries and lifecycle ops (stop/park/release) on ONE key are mutually
        exclusive, but operations on *different* keys run concurrently — so
        stopping a stalled worker never blocks behind another key's up-to-30s
        spawn. Host-wide admission (``max_workers``) is therefore advisory under
        this scheme; a transient off-by-one at the cap is immaterial on a single
        host and never destroys a *progressing* worker (the fresh-replace path
        checks admission before terminating anything, and slot reclamation takes
        only workers with no active turn — or a turn silent past
        ``stall_after_s`` — skipping any whose key lock a concurrent delivery
        already holds).
        """
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    # --- admission policy --------------------------------------------------

    def _read_published_usage(self) -> float | None:
        """The fullest reported window's utilization % from the shared usage cache,
        or None if missing/unparseable/stale — the collector publishes
        provider-qualified usage files. Which windows exist is the snapshot's own
        answer (``UsageSnapshot.percentages``), not this reader's."""
        if self._usage_cache_dir is None:
            return None
        snapshot = read_shared_cache(self.account, self._usage_cache_dir)
        if snapshot is None:
            return None
        if time.time() - snapshot.fetched_at.timestamp() > USAGE_MAX_AGE_S:
            return None
        return snapshot.max_pct()

    @staticmethod
    def _waiting_on_person(live: _LiveWorker) -> bool:
        """Whether this worker's only activity is a prompt waiting for a person.

        A turn blocked on a question, a permission gate or a classifier block
        spends nothing until someone answers, so it does not hold an admission
        slot. Four unanswered questions used to fill an account's cap and refuse
        every other chat's test results and wakes for as long as nobody answered
        (#3026). The process stays alive, so the answer still goes straight to
        it, and the worker counts again the moment the prompt clears. Background
        agents do run while the outer turn waits, so a worker with any keeps
        its slot.
        """
        return (
            (live.pending_interaction is not None or bool(live.blocks))
            and not live.background_agents
        )

    def _interactive_live(
        self, exclude: _LiveWorker | None = None
    ) -> list[tuple[str, _LiveWorker]]:
        """Running workers that hold an account-local admission slot."""
        return [
            (key, worker)
            for key, worker in self._live.items()
            if worker is not exclude
            and worker.process.returncode is None
            and not self._waiting_on_person(worker)
        ]

    async def _admit_interactive(
        self,
        key: str,
        role: SessionRole,
        *,
        exclude: _LiveWorker | None = None,
        automated: bool = False,
    ) -> bool:
        """Whether this (re)spawn may take an account-local slot, cleaning up first.

        The single place the cap's policy lives, so who it binds cannot drift
        between the two admission sites:

        * every chat waits for a free slot, and at the cap gets one reclaimed if
          an idle or stalled worker can be cleaned up;
        * a ``primary`` chat is a person pressing Start chat in the deck. It still
          asks the pool first — so an idle worker is reclaimed exactly as before —
          but is never refused. The cap exists to stop machines from filling an
          account, and refusing the boss was #559. An ``automated`` delivery to
          one — a revival, a restart nudge, a job notice — is no person asking,
          so it waits like anything else: revivals alone pushed an account to
          7/4 on 2026-10-13 (#3009).

        Refusals are logged, so ``at_capacity`` never leaves the journal silent.
        """
        if len(self._interactive_live(exclude=exclude)) < self.max_workers:
            return True
        if await self._free_admission_slot(key) is not None:
            return True
        if role == "primary" and not automated:
            log.warning(
                "admitting %s:%s past the %d-slot cap: a person asked for this chat",
                self.account.key,
                key,
                self.max_workers,
            )
            return True
        self._log_at_capacity(key)
        return False

    def _quiet_for(self, key: str, live: _LiveWorker, now: float) -> float:
        """Seconds since this worker last received input or produced output."""
        rec = self._records.get(key)
        return now - max(live.last_event_at, rec.last_delivery_at if rec else 0.0)

    def _is_stalled(self, key: str, live: _LiveWorker, now: float) -> bool:
        """Whether this worker's turn has gone silent past ``stall_after_s``.

        One definition, shared by the ``stalled`` badge in ``snapshot`` and the
        admission cleanup, so what the deck shows and what it reaps can never
        disagree. A worker blocked on a pending interaction is waiting on the
        user, not hung; ``stall_after_s = 0`` disables the notion entirely.
        """
        rec = self._records.get(key)
        return (
            live.turn_active
            and live.process.returncode is None
            and not (
                rec
                and rec.pending_context_delivery_id
            )
            and live.pending_interaction is None
            and not live.blocks
            and self.stall_after_s > 0
            and self._quiet_for(key, live, now) > self.stall_after_s
        )

    async def _free_admission_slot(self, admit_key: str) -> str | None:
        """Reclaim one admission slot, or None when every other slot is doing
        real work. Returns the reclaimed key.

        Two passes, safest first. An *idle* worker (alive, no active turn) is
        always safe to reclaim, so a new chat is never blocked by
        finished-but-resident sessions. Only when there is no idle worker do we
        clean up a *stalled* one: a turn that has neither received nor produced
        anything for longer than ``stall_after_s`` and is not waiting on a user
        decision. Without that second pass a few wedged turns pin the account at
        the cap and every later chat is refused ``at_capacity`` until the runtime
        restarts.

        Either way the victim's durable record (incl. ``session_id``) survives,
        so its chat revives on the next delivery.
        """
        evicted = await self._reclaim_slot(
            self._idle_victims(admit_key, time.time()),
            self._is_reclaimable_idle,
            admit_key,
            "idle",
        )
        if evicted is not None:
            return evicted
        return await self._reclaim_slot(
            self._stalled_victims(admit_key, time.time()),
            self._is_stalled,
            admit_key,
            "stalled",
        )

    def _is_idle(self, key: str, live: _LiveWorker, now: float) -> bool:
        rec = self._records.get(key)
        return (
            live.process.returncode is None
            and not live.turn_active
            and not (
                rec
                and rec.pending_context_delivery_id
            )
        )

    def _is_reclaimable_idle(self, key: str, live: _LiveWorker, now: float) -> bool:
        """Idle, and not handed a message within ``IDLE_RECLAIM_GRACE_S`` (#3009)."""
        rec = self._records.get(key)
        delivered_at = rec.last_delivery_at if rec else 0.0
        return self._is_idle(key, live, now) and now - delivered_at > IDLE_RECLAIM_GRACE_S

    def _idle_victims(self, admit_key: str, now: float) -> list[tuple[str, _LiveWorker]]:
        """Live workers with no active turn, least recently used first."""
        return sorted(
            (
                (key, live)
                for key, live in self._live.items()
                if key != admit_key and self._is_reclaimable_idle(key, live, now)
            ),
            key=lambda item: -self._quiet_for(item[0], item[1], now),
        )

    def _stalled_victims(self, admit_key: str, now: float) -> list[tuple[str, _LiveWorker]]:
        """Live workers whose turn is stalled, longest-silent first.

        A worker with Claude-native background agents is excluded: those run on
        past the outer turn's ``result`` and report through the parent transcript
        rather than stdout, so silence on stdout is not evidence that it is hung.
        """
        victims = []
        for key, live in self._live.items():
            if key == admit_key:
                continue
            if not self._is_stalled(key, live, now):
                continue
            rec = self._records.get(key)
            if rec is not None:
                self._sync_background_agents(rec, live)
            if live.background_agents:
                continue
            victims.append((key, live))
        return sorted(victims, key=lambda item: -self._quiet_for(item[0], item[1], now))

    async def _claim_victim(
        self,
        victim_key: str,
        victim: _LiveWorker,
        eligible,
        now: float,
    ) -> bool:
        """Take a victim out of ``self._live`` if it is still eligible.

        Never *waits* on another key's lock: a victim currently being delivered
        to is skipped rather than risk racing a fresh turn or deadlocking against
        a concurrent eviction. ``eligible`` is re-checked under that lock, because
        the victim may have woken up between the scan and the claim.

        Claiming is separate from terminating so both callers — admission-pressure
        reclaim and the idle sweep — get the same race protection without one
        having to be written in terms of the other's "stop after the first" rule.

        ``now`` comes from the caller rather than being read here so the re-check
        agrees with the scan that nominated this victim. Reading the clock again
        would make a quiet-time predicate answer differently on the two sides of
        the lock for reasons that have nothing to do with the worker's state.
        """
        lock = self._key_locks.get(victim_key)
        if lock is not None and lock.locked():
            return False  # a concurrent delivery owns it; don't race its turn
        # No await between the locked() check and acquire(): atomic on the loop.
        if lock is not None:
            await lock.acquire()
        try:
            current = self._live.get(victim_key)
            if current is not victim or not eligible(victim_key, current, now):
                return False  # raced: state changed under us
            self._live.pop(victim_key, None)
        finally:
            if lock is not None:
                lock.release()
        return True

    async def _reclaim_slot(
        self,
        victims: list[tuple[str, _LiveWorker]],
        eligible,
        admit_key: str,
        why: str,
    ) -> str | None:
        """Terminate the first still-eligible victim, in the order offered."""
        for victim_key, victim in victims:
            if not await self._claim_victim(victim_key, victim, eligible, time.time()):
                continue
            # Reclaiming an idle worker is routine; killing a stalled turn ends
            # work somebody started, so that one is worth a warning.
            log.log(
                logging.WARNING if why == "stalled" else logging.INFO,
                "reclaimed %s worker %s:%s (quiet %.0fs) to admit %s",
                why,
                self.account.key,
                victim_key,
                self._quiet_for(victim_key, victim, time.time()),
                admit_key,
            )
            await self._terminate(victim)
            return victim_key
        return None

    def _is_collectable(self, key: str, live: _LiveWorker, now: float) -> bool:
        """Whether this worker has been quiet past ``idle_after_s`` with nothing running.

        Independent of admission pressure: without this timer a worker nobody
        competes with is never collected, which once left one live for 20 days (#1226).

        Claude-native background agents run on past the outer turn's ``result``, so
        an idle-looking worker with live agents is excluded exactly as it is from
        stall reclaim.
        """
        if self.idle_after_s <= 0 or not self._is_idle(key, live, now):
            return False
        if self._quiet_for(key, live, now) <= self.idle_after_s:
            return False
        rec = self._records.get(key)
        if rec is not None:
            self._sync_background_agents(rec, live)
        return not live.background_agents

    async def collect_idle(self, now: float | None = None) -> list[str]:
        """Terminate every worker quiet past ``idle_after_s``. Returns their keys.

        The counterpart to ``_free_admission_slot``: that one runs only when a new
        worker needs a slot, so a fleet nobody is competing with is never collected.
        This runs on a timer instead and takes every eligible worker, not just the
        one needed to make room.
        """
        if self.idle_after_s <= 0:
            return []
        now = time.time() if now is None else now
        candidates = [
            (key, live)
            for key, live in list(self._live.items())
            if self._is_collectable(key, live, now)
        ]
        collected = []
        for key, victim in candidates:
            if not await self._claim_victim(key, victim, self._is_collectable, now):
                continue
            log.info(
                "collected idle worker %s:%s (quiet %.0fs, role %s)",
                self.account.key,
                key,
                self._quiet_for(key, victim, now),
                self.worker_role(key),
            )
            await self._terminate(victim)
            collected.append(key)
        return collected

    def _log_at_capacity(self, admit_key: str) -> None:
        """Say who is holding the slots. ``at_capacity`` is otherwise invisible in
        the journal — it is only ever a field in one HTTP response.
        """
        now = time.time()
        holders = [
            f"{key}(turn={'y' if live.turn_active else 'n'},"
            f"quiet={self._quiet_for(key, live, now):.0f}s)"
            for key, live in self._interactive_live()
        ]
        log.warning(
            "at_capacity: %s refused %s; %d/%d slots held by %s",
            self.account.key,
            admit_key,
            len(holders),
            self.max_workers,
            ", ".join(holders) or "nothing",
        )

    # --- state persistence -------------------------------------------------

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        for item in raw.get("workers", []):
            try:
                # Legacy state cannot prove a worker was a primary coordinator.
                # Never grant recursive delegation merely because provenance is
                # absent after an upgrade.
                item.setdefault("delegated", True)
                # Before #2259 the switch moved the record to another mode and kept
                # the original here. Put that original back. The switch itself is
                # gone (#2271); the recorded mode is what the chat spawns with.
                if "permission_mode_before_prompts" in item:
                    before = item.pop("permission_mode_before_prompts")
                    if item.get("permission_prompts"):
                        item["permission_mode"] = before
                item.pop("permission_prompts", None)
                # A record from before #2285 never chose; it ran with the switch
                # off, so the new default must not turn it on (#2319).
                item.setdefault("bypass_permissions", False)
                # A chat the poller started under its retired ``kanban`` role (#3741).
                item["role"] = stored_session_role(item.get("role"))
                rec = WorkerRecord(**item)
            except TypeError:
                continue
            self._records[rec.key] = rec
        # Copies of one question kept open before #3637 close here, once.
        for rec in self._records.values():
            self._drop_questions(rec, _repeated_question_ids(rec.open_questions))

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workers": [vars(rec) for rec in self._records.values()]}
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(self._state_path)
        self.on_state_change(None)

    def _reconcile_orphans(self) -> None:
        """Kill workers left running by a previously-crashed runtime incarnation.

        After an ungraceful exit (OOM/``kill -9``) the lifespan shutdown never
        ran, so detached worker process groups may still be alive and registered
        under ``<root>/sessions/<pid>.json`` even though our in-memory ``_live``
        map starts empty. Their stdio belonged to the dead runtime, so they can't
        be re-adopted, and the next ``deliver`` for a key would ``--resume`` a
        *second* process on the same transcript. Any still-alive process whose
        session id matches one of our records is therefore killed; the record
        (session lineage) is kept so the next deliver revives cleanly as the sole
        owner.
        """
        owned = {rec.session_id for rec in self._records.values() if rec.session_id}
        # A held CLI is not an orphan: its holder keeps its pipes, and ``reattach`` takes it
        # back (#3248).
        held = {
            rec.session_id
            for key, info in self._holders.items()
            if (rec := self._records.get(key)) is not None and info.alive()
        }
        owned -= held
        if not owned:
            return
        try:
            entries = registry.read_registry(self.account.root)
        except Exception:  # noqa: BLE001 -- reconcile must never break host init
            log.exception("orphan reconcile: registry read failed (%s)", self.account.key)
            return
        for entry in entries:
            if entry.session_id not in owned or not registry.is_alive(entry):
                continue
            # SIGKILL, not SIGTERM-then-wait: these are not our children (we can't
            # reap them) and must be gone before any revive; the JSONL transcript
            # tolerates a truncated trailing line on --resume. Workers are spawned
            # start_new_session=True, so pid is the group leader (pgid == pid) -- unless
            # a holder led the group, which is gone or the CLI would be held (#3248).
            try:
                os.killpg(entry.pid, signal.SIGKILL)
            except ProcessLookupError:
                try:
                    os.kill(entry.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    continue
            except PermissionError:
                continue
            log.warning(
                "orphan reconcile: killed stale worker pid=%s session=%s (%s)",
                entry.pid,
                entry.session_id,
                self.account.key,
            )

    # --- held workers (#3248) ---------------------------------------------

    def _new_holder(self, scope_unit: str) -> HolderInfo:
        """Fresh paths for one holder: its socket in the runtime dir, its log on disk.

        Not under ``$XDG_RUNTIME_DIR/agentdeck``: that is the runtime service's own
        ``RuntimeDirectory``, which systemd empties every time the service stops, and a
        holder whose socket is gone cannot be reconnected to.
        """
        token = uuid.uuid4().hex[:16]
        run_dir = (
            Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
            / HOLDER_RUN_DIR
        )
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._holder_dir.mkdir(parents=True, exist_ok=True)
        return HolderInfo(
            socket=str(run_dir / f"{token}.sock"),
            log=str(self._holder_dir / f"{token}.log"),
            exit=str(self._holder_dir / f"{token}.exit"),
            pid=0,
            scope_unit=scope_unit,
        )

    def _save_holder(self, key: str, live: _LiveWorker) -> None:
        """Save how far this worker's log was read, with what a restart must rebuild."""
        info = self._holders.get(key)
        if info is None:
            return
        info.offset = live.read_offset
        info.pending_interaction = (
            dict(live.pending_interaction) if live.pending_interaction else None
        )
        info.background_agents = dict(live.background_agents)
        live.offset_saved_at = time.monotonic()
        self._holder_store.save(self._holders)

    async def _forget_holder(self, key: str, live: _LiveWorker) -> None:
        """The held CLI is gone and its log fully read: drop its record and files.

        Only once the holder has exited and its exit code was read: the socket closes a
        moment before the holder writes the exit file and leaves, and removing the file in
        that moment would turn a clean exit into an unexplained kill.
        """
        info = self._holders.get(key)
        process = live.process
        if info is None or not isinstance(process, HeldProcess) or process.info is not info:
            return
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), HOLDER_EXIT_WAIT_S)
        except TimeoutError:
            log.warning("worker holder %s outlived its stream; keeping its files", info.pid)
            return
        if self._holders.get(key) is not info:
            return
        # An exiting holder sends what is left for a few seconds at most; anything this
        # reader did not get is still in the log, so read it before the log goes.
        info.offset = live.read_offset
        self._read_rest(key, info)
        del self._holders[key]
        info.remove_files()
        self._holder_store.save(self._holders)

    async def reattach(self) -> list[str]:
        """Take back the workers a previous runtime left running under holders.

        Run once at boot, before anything revives a chat: a reattached worker is live, so
        revival leaves it alone. A holder that is gone had its CLI exit while no runtime
        was reading; what it printed meanwhile is read now, so a turn that finished is
        recorded as finished. Returns the keys reattached.
        """
        taken: list[str] = []
        for key, info in list(self._holders.items()):
            rec = self._records.get(key)
            if rec is None or not rec.session_id or key in self._live:
                kill_holder(info)
                info.remove_files()
                del self._holders[key]
                continue
            if not info.alive():
                self._read_rest(key, info)
                self._settle_writes(rec, info)
                info.remove_files()
                del self._holders[key]
                continue
            process = await HeldProcess.attach(info, limit=STDOUT_LINE_LIMIT)
            if process is None:
                # Not one this runtime can talk to: stop it, and the chat is revived.
                kill_holder(info)
                info.remove_files()
                del self._holders[key]
                continue
            self._settle_writes(rec, info)
            initialized: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            initialized.set_result(rec.session_id)
            live = _LiveWorker(
                process=process,  # type: ignore[arg-type]
                reader_task=None,  # type: ignore[arg-type]
                initialized=initialized,
                turn_active=bool(rec.turn_open_at),
                started_at=info.started_at,
                pending_interaction=info.pending_interaction,
                background_agents=dict(info.background_agents),
                scope_unit=info.scope_unit,
                holder_key=key,
                read_offset=info.offset,
            )
            live.reader_task = asyncio.create_task(
                self._read_loop(key, live), name=f"claude-worker:{self.account.key}:{key}"
            )
            self._live[key] = live
            taken.append(key)
            log.info("reattached held worker %s:%s (holder %s)", self.account.key, key, info.pid)
            self.on_state_change(key)
        self._holder_store.save(self._holders)
        self._sweep_holder_files()
        return taken

    def _settle_writes(self, rec: WorkerRecord, info: HolderInfo) -> None:
        """Settle the deliveries the last runtime was writing when it went down (#3248).

        A receipt whose write may have started is normally fenced for good: nobody can say
        whether the CLI got it. The holder can. Every id it accepted is in its ledger,
        recorded before the line could reach the CLI, so a listed one may have been read
        and counts as delivered, and a missing one provably was not, so its receipt is
        dropped and the same delivery may be sent again. Only receipts from this holder's
        lifetime are settled; an older one may have reached a CLI before it.
        """
        written = info.written()
        if written is None:
            # Unreadable: every in-flight receipt stays fenced as uncertain.
            return
        changed = False
        for delivery_id, receipt in list(rec.deliveries.items()):
            if receipt.get("action") or receipt.get("delivery_stage") != "write_may_have_started":
                continue
            if float(receipt.get("at") or 0.0) < info.started_at:
                continue
            if delivery_id in written:
                rec.receipts.finalize(
                    delivery_id,
                    str(receipt.get("fingerprint")),
                    DeliverResult(
                        True,
                        "queued",
                        session_id=rec.session_id,
                        provider_write_state="started",
                    ),
                )
            else:
                rec.receipts.forget(delivery_id, write_started=False)
            changed = True
        if changed:
            self._save_state()

    def _read_rest(self, key: str, info: HolderInfo) -> None:
        """Act on what a held CLI printed after the last runtime stopped reading.

        One line at a time, each capped like the live reader's, so a long turn missed
        while no runtime ran costs no more memory than its longest line.
        """
        try:
            handle = open(info.log, "rb")  # noqa: SIM115 -- closed below
        except OSError:
            return
        initialized: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        live = _LiveWorker(
            process=None,  # type: ignore[arg-type]
            reader_task=None,  # type: ignore[arg-type]
            initialized=initialized,
        )
        read_any = False
        with handle:
            try:
                handle.seek(info.offset)
            except OSError:
                return
            while line := handle.readline(STDOUT_LINE_LIMIT):
                if not line.endswith(b"\n") and len(line) == STDOUT_LINE_LIMIT:
                    # Longer than the live reader accepts: skip the rest of it.
                    while (more := handle.readline(STDOUT_LINE_LIMIT)) and not more.endswith(b"\n"):
                        pass
                    continue
                read_any = True
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(event, dict):
                    self._handle_event(key, live, event)
        if not initialized.done():
            initialized.cancel()
        if read_any:
            self.on_state_change(key)

    def _sweep_holder_files(self) -> None:
        """Remove logs no holder record names, once they are a day old."""
        known = {Path(info.log).stem for info in self._holders.values()}
        cutoff = time.time() - 24 * 60 * 60
        try:
            entries = list(self._holder_dir.iterdir())
        except OSError:
            return
        for path in entries:
            if path.stem in known:
                continue
            with contextlib.suppress(OSError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()

    # --- public API --------------------------------------------------------

    def find_key_by_session(self, session_id: str) -> str | None:
        """Return the worker key whose session lineage matches, if any."""
        for key, rec in self._records.items():
            if rec.session_id == session_id:
                return key
        return None

    def worker_role(self, key: str) -> SessionRole | None:
        record = self._records.get(key)
        return record.role if record is not None else None

    def has_worker(self, key: str) -> bool:
        return key in self._records

    def has_live_worker(self, key: str) -> bool:
        live = self._live.get(key)
        return live is not None and live.process.returncode is None

    def delivery_receipt(self, key: str, delivery_id: str) -> dict[str, Any] | None:
        rec = self._records.get(key)
        receipt = rec.deliveries.get(delivery_id) if rec is not None else None
        return dict(receipt) if receipt is not None else None

    async def resolve_context_delivery(
        self, key: str, delivery_id: str, choice: str, rationale: str
    ) -> DeliverResult:
        async with self._key_lock(key):
            rec = self._records.get(key)
            if rec is None:
                return DeliverResult(False, "rejected", reason="worker_not_found")
            result = rec.receipts.resolve(
                delivery_id, choice, rationale, session_id=rec.session_id
            )
            if not result.accepted:
                return result
            if rec.pending_context_delivery_id == delivery_id:
                rec.pending_context_delivery_id = None
                rec.pending_context_operation = None
                rec.pending_context_source_session_id = None
            self._save_state()
            return result

    def outstanding_background_tasks(self, key: str) -> frozenset[str]:
        """Background shell task ids this key launched and never collected.

        Deliberately not in ``snapshot()``: answering it reads the worker's
        transcript from disk, and the snapshot is taken often enough that paying
        for that every time would be absurd. The boot-time revival scan (#1547)
        asks once per key, for the chats that ended a turn waiting on work the
        dead runtime was going to report back.
        """
        rec = self._records.get(key)
        if rec is None:
            return frozenset()
        live = self._live.get(key)
        return self._background_shell_tasks(
            rec, live if live is not None and live.process.returncode is None else None
        )

    def unread_messages(self, key: str) -> list[dict[str, Any]]:
        """Messages written into a running turn that the chat never read (#2898).

        Read for a chat whose process is gone: the boot-time revival quotes these
        back to it, because the dead CLI's queue was their only copy. A message the
        transcript shows read — as a user line or delivered into the turn — is
        left out, so nothing the chat already has is handed to it twice. Like
        ``outstanding_background_tasks``, kept out of ``snapshot()`` because it
        reads the transcript.
        """
        rec = self._records.get(key)
        if rec is None or not rec.unread_messages:
            return []
        live = self._live.get(key)
        if live is not None and live.process.returncode is None:
            return []
        entries = [entry for entry in rec.unread_messages if isinstance(entry, dict)]
        since = min(
            (
                float(entry["at"])
                for entry in entries
                if isinstance(entry.get("at"), int | float)
            ),
            default=0.0,
        )
        path = self._transcript_path(rec, None)
        read: Counter[str] = Counter()
        if path is not None:
            read = transcripts_mod.read_message_texts(
                path, since=since - UNREAD_CLOCK_SLACK_S
            )
        unread: list[dict[str, Any]] = []
        # Oldest first, one transcript read per sent message: the same text sent
        # twice and read once leaves the later copy unread.
        for entry in entries:
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if read[text.strip()] > 0:
                read[text.strip()] -= 1
                continue
            unread.append(dict(entry))
        return unread

    def unread_dropped(self, key: str) -> int:
        """How many older queued messages the bounded list let go (#2898).

        Revival cannot quote these, so it says how many there were and asks for
        them again rather than letting them vanish without a word.
        """
        rec = self._records.get(key)
        return rec.unread_dropped if rec is not None else 0

    def _remember_unread(
        self, rec: WorkerRecord, delivery_id: str | None, text: str, images: int
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "delivery_id": delivery_id,
            "text": text,
            "images": images,
            "at": time.time(),
        }
        rec.unread_messages.append(entry)
        overflow = len(rec.unread_messages) - MAX_UNREAD_MESSAGES
        if overflow > 0:
            del rec.unread_messages[:overflow]
            rec.unread_dropped += overflow
        return entry

    @staticmethod
    def _forget_unread(
        rec: WorkerRecord,
        entry: dict[str, Any] | None,
        before: tuple[list[dict[str, Any]], int] | None,
    ) -> bool:
        """Undo ``_remember_unread`` for a write that provably reached nothing.

        Puts back what the bound pushed out for it, unless a turn end already
        emptied the list while the write was pending; then there is nothing to
        undo. Identity, not equality: the same text may be queued twice.
        """
        if entry is None or before is None:
            return False
        if not any(kept is entry for kept in rec.unread_messages):
            return False
        rec.unread_messages, rec.unread_dropped = before
        return True

    def change_keys(self) -> list[str]:
        """Every worker key, for the runtime's change feed (#2865)."""
        return list(self._records)

    def change_entry(self, key: str) -> dict[str, Any] | None:
        """What the web reads of one worker, for the runtime's change feed (#2865).

        ``last_activity_at`` moves with every line a working turn prints and nothing in
        the web reads it, so it is left out: comparing it would turn every streamed
        token into a snapshot fetch.
        """
        rec = self._records.get(key)
        if rec is None:
            return None
        entry = self._snapshot_rows([(key, rec)])[key]
        entry.pop("last_activity_at", None)
        return entry

    def next_change_at(self) -> float | None:
        """When a snapshot row next changes with no event to say so (#2865).

        Two clocks: ``stalled`` turns on ``stall_after_s`` after a working turn's last
        event, and a background agent id with no stop notification is dropped after
        ``BACKGROUND_AGENT_MAX_AGE_S``. The change feed sleeps until the earliest.
        """
        now = time.time()
        due: list[float] = []
        for key, live in self._live.items():
            if live.process.returncode is not None:
                continue
            rec = self._records.get(key)
            if self.stall_after_s > 0 and live.turn_active:
                quiet_from = max(live.last_event_at, rec.last_delivery_at if rec else 0.0)
                due.append(quiet_from + self.stall_after_s)
            due.extend(
                seen + BACKGROUND_AGENT_MAX_AGE_S for seen in live.background_agents.values()
            )
        future = [value for value in due if value > now]
        return min(future) if future else None

    def turns_a_restart_would_cut(self) -> int:
        """Live workers with work in progress that a runtime restart would end (#3248).

        A held worker outlives the restart, so only unheld ones count.
        """
        rows = self._snapshot_rows(self._records.items())
        return sum(
            1
            for key, row in rows.items()
            if row.get("effective_activity")
            and not isinstance(getattr(self._live.get(key), "process", None), HeldProcess)
        )

    def snapshot(self) -> dict[str, Any]:
        workers = self._snapshot_rows(self._records.items())
        return self._snapshot_envelope(workers)

    def _snapshot_rows(
        self, records: Iterable[tuple[str, WorkerRecord]]
    ) -> dict[str, dict[str, Any]]:
        now = time.time()
        workers = {}
        for key, rec in records:
            live = self._live.get(key)
            alive = live is not None and live.process.returncode is None
            if alive:
                self._sync_background_agents(rec, live)
            turn_active = bool(alive and live.turn_active)
            background_agent_count = len(live.background_agents) if alive else 0
            effective_activity = bool(turn_active or background_agent_count)
            pending = live.pending_interaction if alive else None
            block = next(iter(live.blocks.values()), None) if alive else None
            stalled = alive and self._is_stalled(key, live, now)
            workers[key] = {
                "session_id": rec.session_id,
                "session_token": rec.session_token,
                "cwd": rec.cwd,
                "live": alive,
                "turn_active": turn_active,
                "background_agent_count": background_agent_count,
                "effective_activity": effective_activity,
                "stalled": stalled,
                "last_delivery_at": rec.last_delivery_at,
                "last_result_at": rec.last_result_at,
                "last_result_subtype": rec.last_result_subtype,
                "last_result_limit": rec.last_result_limit,
                "last_result_limit_reset_at": rec.last_result_limit_reset_at,
                # The durable "this chat owes an answer" marker revival reads
                # (#1844). Published from the record, not from ``turn_active``,
                # because the point of it is to outlive the process.
                "turn_open_at": rec.turn_open_at,
                # When this chat's turn was ended on purpose, so revival can tell
                # a cancellation from a death even with no terminal result.
                "turn_closed_at": rec.turn_closed_at,
                # The clock ``_quiet_for`` already keeps, published so the poller can tell a
                # wedged worker from a working one (#1400). Only meaningful while the process
                # is alive; a dead one is a *death*, which ``live: false`` already says and
                # says better, so it reports the unknown clock rather than a stale one.
                "last_activity_at": (
                    max(live.last_event_at, rec.last_delivery_at) if alive else 0.0
                ),
                # Normalized here (in the runtime that owns the CLI control
                # channel) so the web side never parses raw Claude wire schema.
                # A `can_use_tool` gate first: the CLI is blocked on it. A
                # classifier block shows once no gate is open (#2271).
                "pending_interaction": (
                    _normalize_interaction(pending, rec.session_id)
                    if pending is not None or block is None
                    else _normalize_block(block, rec.session_id)
                ),
                # Every question still waiting on the person, whether or not a turn is
                # inside it (#3451). Apart from ``pending_interaction``, which says the
                # CLI is blocked right now and pins the chat as working.
                "open_questions": _open_questions_view(rec.open_questions, rec.session_id),
                "pending_context_delivery_id": rec.pending_context_delivery_id,
                "pending_context_operation": rec.pending_context_operation,
                **DeliveryCorrelation.snapshot_fields(rec.receipts),
                # Why the process died, when we could name it (#1546). A live
                # worker never carries one: the reason belongs to a death, and a
                # revived chat clears it at spawn.
                "exit_reason": "" if alive else rec.last_exit_reason,
                "model": rec.model,
                "effort": rec.effort,
                "permission_mode": rec.permission_mode,
                "role": rec.role,
                "delegated": rec.delegated,
                "bypass_permissions": rec.bypass_permissions,
                # A chat whose own mode already bypasses has nothing to switch.
                "bypass_permissions_available": (
                    self._base_permission_mode(rec) != BYPASS_PERMISSIONS_MODE
                ),
            }
        return workers

    def _snapshot_envelope(self, workers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "workers": workers,
            # When this runtime started. A restart leaves every owned worker's record on disk
            # with no process behind it, which is the same shape an OOM leaves; this is how a
            # reader tells the two apart instead of calling a revived chat dead (#1659).
            SNAPSHOT_BOOTED_AT_KEY: RUNTIME_BOOTED_AT,
            # What these records can be read for. A poller from a newer deployment uses it
            # to tell an absent field from a runtime that cannot publish one (#1407).
            SNAPSHOT_CAPABILITIES_KEY: [
                *WORKER_SNAPSHOT_CAPABILITIES,
                SNAPSHOT_CONTEXT_MANAGEMENT_V1,
                SNAPSHOT_FORK_V1,
                SNAPSHOT_RELOCATE_V1,
                SNAPSHOT_REWIND_V1,
                SNAPSHOT_SPARE_V1,
                        ],
            "live_count": sum(
                1 for w in self._live.values() if w.process.returncode is None
            ),
            "usage_pct": self._usage_reader(),
        }

    def _background_shell_tasks(
        self, rec: WorkerRecord, live: _LiveWorker | None
    ) -> frozenset[str]:
        """Outstanding shell task ids from the worker's durable transcript.

        Unlike Claude-native Agent tasks, background Bash commands do not appear
        on the runtime stream. They can also execute outside this process group
        (for example through ``docker exec``), so the launch/collection ledger is
        the only reliable activity signal a revived poller worker can adopt.
        """
        path = self._transcript_path(rec, live)
        if path is None:
            return frozenset()
        try:
            limit = path.stat().st_size
        except OSError:
            return frozenset()
        return transcripts_mod.task_ledger(path, limit=limit).outstanding

    def _has_transcript(self, session_id: str) -> bool:
        """Whether this account still holds ``session_id``'s transcript."""
        return any((self.account.root / "projects").glob(f"*/{session_id}.jsonl"))

    def _transcript_path(self, rec: WorkerRecord, live: _LiveWorker | None) -> Path | None:
        session_id = rec.session_id
        if not session_id:
            return None
        path = live.transcript_path if live is not None else None
        if path is None or not path.exists():
            matches = list(
                (self.account.root / "projects").glob(f"*/{session_id}.jsonl")
            )
            if not matches:
                return None
            try:
                path = max(matches, key=lambda candidate: candidate.stat().st_mtime_ns)
            except OSError:
                return None
            if live is not None:
                live.transcript_path = path
        return path

    @staticmethod
    def _background_task_brief(task_ids: frozenset[str]) -> str:
        names = ", ".join(sorted(task_ids))
        return (
            "## Existing background work\n\n"
            "The previous turn launched background shell work that has not yet been "
            f"collected (task IDs: {names}). Do not launch another copy. Inspect or "
            "wait for these existing tasks and use their results before continuing."
        )

    def _sync_background_agents(self, rec: WorkerRecord, live: _LiveWorker) -> None:
        """Replay newly appended parent-transcript events into agent lifecycle state."""
        # Before anything else — the transcript may never append another line:
        # an id with no stop notification (interrupted parent, unparsed shape)
        # must not hold "effective activity", and with it every runtime deploy,
        # forever (#786).
        cutoff = time.time() - BACKGROUND_AGENT_MAX_AGE_S
        stale = [agent_id for agent_id, seen in live.background_agents.items() if seen < cutoff]
        for agent_id in stale:
            del live.background_agents[agent_id]
            log.warning(
                "dropping leaked background agent id %s for %s:%s "
                "(no stop notification within %.0fs)",
                agent_id,
                self.account.key,
                rec.key,
                BACKGROUND_AGENT_MAX_AGE_S,
            )
        session_id = rec.session_id
        if not session_id:
            return
        path = live.transcript_path
        if path is None or not path.exists():
            matches = list(
                (self.account.root / "projects").glob(f"*/{session_id}.jsonl")
            )
            if not matches:
                return
            path = max(matches, key=lambda candidate: candidate.stat().st_mtime)
            live.transcript_path = path
            live.transcript_offset = 0
            live.background_agents.clear()
        try:
            size = path.stat().st_size
            if size < live.transcript_offset:
                live.transcript_offset = 0
                live.background_agents.clear()
            with path.open("rb") as transcript:
                transcript.seek(live.transcript_offset)
                appended = transcript.read()
        except OSError:
            log.debug("failed to read Claude transcript lifecycle events", exc_info=True)
            return
        final_newline = appended.rfind(b"\n")
        if final_newline < 0:
            return
        complete = appended[: final_newline + 1]
        live.transcript_offset += len(complete)
        for raw_line in complete.splitlines():
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                _update_background_agents(live, event)

    async def deliver(
        self,
        key: str,
        message: str,
        *,
        cwd: str | None = None,
        fresh: bool = False,
        images: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        delivery_id: str | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        require_active: bool = False,
        expected_last_result_at: float | None = None,
        expected_session_id: str | None = None,
        context_maintenance: bool = False,
        context_rollover: bool = False,
        delegated: bool,
        role: SessionRole | None = None,
        session_token: str | None = None,
        command_artifacts: dict[str, Any] | None = None,
        correlation: DeliveryCorrelation | None = None,
        fork_from: ForkFrom | None = None,
        automated: bool = False,
    ) -> DeliverResult:
        """Idempotent work delivery: steer if live, revive if finished, else spawn.

        ``delegated`` has no default here (see ``KeyedWorkerHost``): it decides the
        session's trust level, so every caller must state it. ``automated`` says no
        person sent this message, so admission holds even a primary chat to the cap.

        ``delivery_id`` is optional for interactive callers, but durable dispatchers
        should always provide one. An accepted id is persisted with a fingerprint and
        its original result before the HTTP response is returned, so an ambiguous retry
        cannot write the same message twice or replace a successfully spawned worker.
        """
        role = resolve_session_role(delegated=delegated, role=role)
        fingerprint = DeliveryReceipts.fingerprint(
            message,
            cwd=cwd,
            fresh=fresh,
            images=images or [],
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            allow_over_budget=allow_over_budget,
            operation=(
                "context_rollover"
                if context_rollover
                else "context_maintenance" if context_maintenance else None
            ),
            require_idle=require_idle,
            require_active=require_active,
            command_artifacts=command_artifacts,
            fork=(f"{fork_from.session_id}@{fork_from.ref}" if fork_from else None),
        )
        async with self._key_lock(key):
            if fork_from is not None and (
                not fresh or require_idle or require_active or context_maintenance
                or context_rollover
            ):
                # A side chat is only ever a brand-new spawn (#2485).
                return DeliverResult(False, "rejected", reason="fork_requires_fresh")
            if (context_maintenance or context_rollover) and not delivery_id:
                return DeliverResult(
                    False, "rejected", reason="context_delivery_id_required"
                )
            if context_maintenance and (not require_idle or fresh or context_rollover):
                return DeliverResult(
                    False,
                    "rejected",
                    reason="context_maintenance_requires_idle",
                )
            if context_rollover and (
                not require_idle or not fresh or require_active or context_maintenance
            ):
                return DeliverResult(False, "rejected", reason="context_rollover_invalid")
            rec = self._records.get(key)
            context_operation = (
                "rollover" if context_rollover else "compact" if context_maintenance else None
            )
            retrying_context = bool(
                rec is not None
                and delivery_id
                and rec.pending_context_delivery_id == delivery_id
                and rec.pending_context_operation == context_operation
            )
            pending_rollover_target = bool(
                retrying_context
                and context_rollover
                and rec is not None
                and rec.session_id != rec.pending_context_source_session_id
            )
            if rec is not None and rec.receipts.adopt_correlation(
                delivery_id, fingerprint, correlation
            ):
                # A receipt from before #2142 takes this delivery's Work and Run; durable
                # before the replay below answers with it.
                self._save_state()
            cached = (
                rec.receipts.lookup(
                    delivery_id,
                    fingerprint,
                    live_session_id=rec.session_id,
                    correlation=correlation,
                )
                if rec is not None
                else None
            )
            if cached is not None:
                return cached
            if rec is not None and rec.pending_context_delivery_id:
                if not retrying_context:
                    return DeliverResult(
                        False, "rejected", reason="context_operation_pending"
                    )
            if expected_session_id is not None and (
                rec is None
                or (
                    rec.session_id != expected_session_id
                    and not (
                        retrying_context
                        and rec.pending_context_source_session_id
                        == expected_session_id
                    )
                )
            ):
                return DeliverResult(False, "rejected", reason="worker_changed")
            if (
                expected_last_result_at is not None
                and not pending_rollover_target
                and (rec.last_result_at if rec is not None else 0.0)
                != expected_last_result_at
            ):
                return DeliverResult(False, "rejected", reason="worker_changed")
            live = self._live.get(key)
            if require_active and (
                live is None
                or live.process.returncode is not None
                or not (live.turn_active or live.background_agents)
            ):
                return DeliverResult(False, "rejected", reason="worker_inactive")
            if require_idle and rec is not None:
                if live is not None:
                    self._sync_background_agents(rec, live)
                if live is not None and (live.turn_active or live.background_agents):
                    return DeliverResult(False, "rejected", reason="active_turn")
                background_tasks = self._background_shell_tasks(rec, live)
                if background_tasks:
                    if context_maintenance or context_rollover:
                        return DeliverResult(False, "rejected", reason="active_turn")
                    message = (
                        f"{message}\n\n{self._background_task_brief(background_tasks)}"
                    )
            if rec is not None and self._relocation_target(rec, live, fresh=fresh, cwd=cwd):
                live = await self._relocate(key, rec, live)
                # A caller that named the deleted directory meant this chat, not a
                # new one in a directory that is not there.
                cwd = None
            try:
                image_blocks = [
                    await asyncio.to_thread(self._image_block, path) for path in images or []
                ]
            except WorkerError as exc:
                return DeliverResult(False, "rejected", reason=str(exc))
            if (
                live is not None
                and live.process.returncode is None
                and (not fresh or pending_rollover_target)
            ):
                # Image loading above yields to the reader task. Re-check activity at the last
                # point before the synchronous stdin write, so a result read in that gap cannot
                # turn an active-only steer into a queued new turn.
                if require_active and not (live.turn_active or live.background_agents):
                    return DeliverResult(False, "rejected", reason="worker_inactive")
                previous_artifacts = rec.command_artifacts
                rec.command_artifacts = (
                    dict(command_artifacts) if command_artifacts is not None else None
                )
                if not self._publish_artifact_context(rec):
                    rec.command_artifacts = previous_artifacts
                    self._publish_artifact_context(rec)
                    return DeliverResult(
                        False, "rejected", reason="artifact_policy_unavailable"
                    )
                rec.receipts.prepare(
                    delivery_id,
                    fingerprint,
                    session_id=rec.session_id,
                    correlation=correlation,
                )
                self._save_state()
                rec.receipts.begin_write(
                    delivery_id, fingerprint, session_id=rec.session_id
                )
                # The turn is open from the moment the payload is about to enter
                # the child's stdin, not from the moment that write returns. A
                # death inside the write or drain would otherwise leave a turn in
                # the child and nothing on disk that says so (#1844).
                opened_here = not rec.turn_open_at
                if opened_here:
                    rec.turn_open_at = time.time()
                # A message for a running turn is kept on disk before it enters
                # the child's stdin, for the same reason: a death between the
                # write and the save after it would leave the CLI's in-memory
                # queue its only copy (#2898).
                unread_before = (
                    (list(rec.unread_messages), rec.unread_dropped)
                    if live.turn_active
                    else None
                )
                unread_entry = (
                    self._remember_unread(rec, delivery_id, message, len(image_blocks))
                    if live.turn_active
                    else None
                )
                self._save_state()
                try:
                    await self._close_pending_question(live)
                    await self._write_user_message(
                        live, message, image_blocks, delivery_id=delivery_id
                    )
                except WorkerError as exc:
                    unread_undone = False
                    if not exc.write_started:
                        if opened_here:
                            # The payload provably reached nothing, so this delivery
                            # opened no turn for anyone to resume.
                            rec.turn_open_at = 0.0
                        unread_undone = self._forget_unread(
                            rec, unread_entry, unread_before
                        )
                    if rec.receipts.forget(delivery_id, write_started=exc.write_started):
                        self._save_state()
                    elif (opened_here and not exc.write_started) or unread_undone:
                        self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason=str(exc),
                        session_id=rec.session_id,
                        successor_created=bool(context_rollover),
                        retryable_same_delivery=bool(context_operation),
                        context_rollover=context_rollover,
                    )
                rec.last_delivery_at = time.time()
                # Already open from the pre-write stamp above; only a turn that
                # was open before this delivery keeps its own, earlier, date.
                rec.turn_open_at = rec.turn_open_at or rec.last_delivery_at
                action = "steered" if require_active or live.turn_active else "queued"
                live.turn_active = True
                result = DeliverResult(
                    True,
                    action,
                    session_id=rec.session_id,
                    provider_write_state="started",
                    successor_created=context_rollover,
                    context_rollover=context_rollover,
                )
                rec.receipts.finalize(delivery_id, fingerprint, result)
                if context_operation:
                    rec.pending_context_delivery_id = None
                    rec.pending_context_operation = None
                    rec.pending_context_source_session_id = None
                self._save_state()
                return result

            admission_checked = False
            # A fresh delivery replaces the existing process atomically. Check
            # admission first so a rejected replacement never destroys the
            # healthy worker it was meant to replace.
            if live is not None:
                if not await self._admit_interactive(
                    key, role, exclude=live, automated=automated
                ):
                    return DeliverResult(False, "rejected", reason="at_capacity")
                admission_checked = True
                self._live.pop(key, None)
                await self._terminate(live)

            # Not live (or fresh requested) — this is a (re)spawn: admission
            # policy applies here only. Steering a live worker is always allowed
            # so in-flight work finishes even at the cap or over budget.
            if not admission_checked:
                if not await self._admit_interactive(key, role, automated=automated):
                    return DeliverResult(False, "rejected", reason="at_capacity")
            source_record = rec
            if (
                rec is None
                or (fresh and not pending_rollover_target)
                or rec.cwd != (cwd or (rec.cwd if rec else None))
            ):
                if cwd is None and rec is None:
                    return DeliverResult(False, "rejected", reason="cwd_required")
                previous = rec
                preserved = previous if context_rollover else None
                rec = WorkerRecord(
                    key=key,
                    cwd=cwd or (previous.cwd if previous else ""),
                    permission_mode=(preserved.permission_mode if preserved else None),
                    bypass_permissions=(
                        preserved.bypass_permissions
                        if preserved
                        else self._new_chat_bypass(permission_mode)
                    ),
                    delegated=(preserved.delegated if preserved else delegated),
                    role=(preserved.role if preserved else role),
                    # The web proxy always mints a token, but a caller reaching the
                    # runtime directly may omit one. Mint the fallback here (as the
                    # Codex host does) so every keyed worker gets the same
                    # self-identification contract, and persist it on the record so a
                    # revive hands the same token back to the same session lineage.
                    session_token=session_token or uuid.uuid4().hex,
                    model=(
                        model if model is not None else (preserved.model if preserved else None)
                    ),
                    effort=(
                        effort if effort is not None else (preserved.effort if preserved else None)
                    ),
                    command_artifacts=(
                        dict(command_artifacts)
                        if command_artifacts is not None
                        else None
                    ),
                    deliveries=preserved.deliveries if preserved else {},
                )
                self._records[key] = rec

            if context_operation and not retrying_context:
                rec.pending_context_delivery_id = delivery_id
                rec.pending_context_operation = context_operation
                rec.pending_context_source_session_id = expected_session_id
                self._save_state()

            def restore_source_after_clean_rollover_failure() -> None:
                if not context_rollover or source_record is None or rec.session_id:
                    return
                source_record.pending_context_delivery_id = delivery_id
                source_record.pending_context_operation = "rollover"
                source_record.pending_context_source_session_id = expected_session_id
                self._records[key] = source_record
                self._save_state()

            # Records written by older runtimes may predate the token fallback.
            # Repair them before any revive so the resumed process receives a
            # durable identity too.
            if rec.session_token is None:
                rec.session_token = session_token or uuid.uuid4().hex

            resume_id = None if fresh and not pending_rollover_target else rec.session_id
            resumed = resume_id is not None

            def successor_exists() -> bool:
                return bool(
                    context_rollover
                    and rec.session_id
                    and rec.session_id != expected_session_id
                )
            # Reuse the spawn-time permission mode on a revive so a follow-up that
            # brings back a finished worker can't silently change policy (e.g.
            # escalate a manual "default" chat to bypassPermissions); a fresh spawn
            # records its mode for future revives.
            if resumed and rec.permission_mode is not None:
                spawn_permission_mode = rec.permission_mode
            elif context_rollover:
                spawn_permission_mode = rec.permission_mode
            else:
                spawn_permission_mode = permission_mode
                rec.permission_mode = permission_mode
            if rec.bypass_permissions:
                # The switch is on; the recorded mode is what switching off restores.
                spawn_permission_mode = BYPASS_PERMISSIONS_MODE
            if resumed:
                spawn_effort = rec.effort
            else:
                spawn_effort = rec.effort if effort is None else effort
                rec.effort = spawn_effort
            spawn_model = rec.model if model is None else model
            rec.model = spawn_model
            rec.receipts.prepare(
                delivery_id,
                fingerprint,
                session_id=resume_id,
                correlation=correlation,
            )
            rec.command_artifacts = (
                dict(command_artifacts) if command_artifacts is not None else None
            )
            if rec.session_id and not self._publish_artifact_context(rec):
                if rec.receipts.forget(delivery_id, write_started=False):
                    self._save_state()
                return DeliverResult(
                    False, "rejected", reason="artifact_policy_unavailable"
                )
            self._save_state()

            opened_here = False

            def begin_provider_write() -> None:
                """The payload is about to enter the child's stdin."""
                nonlocal opened_here
                changed = rec.receipts.begin_write(
                    delivery_id, fingerprint, session_id=rec.session_id or resume_id
                )
                # A fresh spawn then waits up to INIT_TIMEOUT_S for the CLI's
                # init event. A death inside that wait used to leave both the
                # delivery stamp and the marker at zero, so the turn already in
                # the child was invisible to the next boot (#1844).
                if not rec.turn_open_at:
                    rec.turn_open_at = time.time()
                    opened_here = True
                    changed = True
                if changed:
                    self._save_state()

            def close_unwritten_turn() -> None:
                """This delivery provably reached nothing, so it opened no turn."""
                nonlocal opened_here
                if opened_here:
                    rec.turn_open_at = 0.0
                    opened_here = False
                    self._save_state()

            try:
                live = await self._spawn_and_deliver(
                    key,
                    rec,
                    message,
                    image_blocks,
                    resume_id=resume_id,
                    model=spawn_model,
                    effort=spawn_effort,
                    permission_mode=spawn_permission_mode,
                    before_write=begin_provider_write,
                    delivery_id=delivery_id,
                    # Only the first spawn copies the source. ``resume_id`` is None for
                    # it, so a failure below never falls back to an empty fresh chat.
                    fork_from=fork_from if resume_id is None else None,
                )
            except WorkerError as exc:
                if exc.write_started:
                    # The message already reached a worker; re-sending it (or fresh
                    # -spawning and sending again) would run the task twice. Keep the
                    # prepared receipt so a same-id retry replays as uncertain, and
                    # never re-send — progress requires a new delivery id.
                    return DeliverResult(
                        True,
                        "uncertain",
                        session_id=rec.session_id,
                        provider_write_state="unknown",
                        successor_created=successor_exists(),
                        context_rollover=context_rollover,
                    )
                restore_source_after_clean_rollover_failure()
                if resume_id is not None:
                    if retrying_context:
                        close_unwritten_turn()
                        if rec.receipts.forget(delivery_id, write_started=False):
                            self._save_state()
                        return DeliverResult(
                            False,
                            "rejected",
                            reason=str(exc),
                            session_id=rec.session_id,
                            successor_created=successor_exists(),
                            retryable_same_delivery=bool(context_operation),
                            context_rollover=context_rollover,
                        )
                    # Revive failed *before* the write → nothing was sent. A chat
                    # whose transcript is still there is tried once more and then
                    # refused: a fresh spawn would answer the message with no memory
                    # of the chat and strand its transcript, which no record names
                    # any more (#3079). Only a chat with nothing left to resume
                    # starts fresh.
                    fresh = not self._has_transcript(resume_id)
                    if fresh:
                        log.warning("revive of %s failed (%s); spawning fresh", key, exc)
                        rec.session_id = None
                        resumed = False
                        self._save_state()
                    else:
                        log.warning("revive of %s failed (%s); retrying the resume", key, exc)
                    try:
                        live = await self._spawn_and_deliver(
                            key,
                            rec,
                            message,
                            image_blocks,
                            resume_id=None if fresh else resume_id,
                            model=model if fresh else spawn_model,
                            effort=spawn_effort,
                            permission_mode=spawn_permission_mode,
                            before_write=begin_provider_write,
                            delivery_id=delivery_id,
                        )
                    except WorkerError as fallback_exc:
                        if fallback_exc.write_started:
                            return DeliverResult(
                                True,
                                "uncertain",
                                session_id=rec.session_id,
                                provider_write_state="unknown",
                                successor_created=successor_exists(),
                                context_rollover=context_rollover,
                            )
                        close_unwritten_turn()
                        if rec.receipts.forget(
                            delivery_id, write_started=fallback_exc.write_started
                        ):
                            self._save_state()
                        reason = str(fallback_exc)
                        if not fresh:
                            log.warning(
                                "revive of %s failed again (%s); refusing the delivery",
                                key,
                                fallback_exc,
                            )
                            reason = f"could not resume this chat: {fallback_exc}"
                        return DeliverResult(
                            False,
                            "rejected",
                            reason=reason,
                            session_id=rec.session_id,
                            successor_created=successor_exists(),
                            retryable_same_delivery=bool(context_operation),
                            context_rollover=context_rollover,
                        )
                else:
                    close_unwritten_turn()
                    if rec.receipts.forget(delivery_id, write_started=exc.write_started):
                        self._save_state()
                    return DeliverResult(
                        False,
                        "rejected",
                        reason=str(exc),
                        session_id=rec.session_id,
                        successor_created=successor_exists(),
                        retryable_same_delivery=bool(context_operation),
                        context_rollover=context_rollover,
                    )

            rec.last_delivery_at = time.time()
            # Already open from the pre-write stamp; a turn that was open before
            # this delivery keeps its own, earlier, date.
            rec.turn_open_at = rec.turn_open_at or rec.last_delivery_at
            action = "revived" if resumed else "spawned"
            result = DeliverResult(
                True,
                action,
                session_id=rec.session_id,
                provider_write_state="started",
                successor_created=successor_exists(),
                context_rollover=context_rollover,
            )
            rec.receipts.finalize(delivery_id, fingerprint, result)
            if context_operation:
                rec.pending_context_delivery_id = None
                rec.pending_context_operation = None
                rec.pending_context_source_session_id = None
            self._save_state()
            return result

    async def _control_request(
        self, live: _LiveWorker, request: dict[str, Any], *, timeout_s: float
    ) -> dict[str, Any] | None | str:
        """Send one control_request and wait for the CLI's matching response.

        Returns the ``response`` body on success, ``None`` if the process exited
        before acking, or a failure reason string (timeout, write failure, or an
        ``error`` control_response). Shared by every outbound control request so
        each one gets the same exit-fast and correlation handling.
        """
        request_id = f"ctl-{uuid.uuid4().hex[:8]}"
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._control_waiters[request_id] = waiter
        live.pending_control_requests.add(request_id)
        try:
            await self._write(
                live,
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": request,
                },
            )
            # The reader resolves the waiter with the control_response, or with
            # None if the process exits first (see _read_loop) — so a turn that
            # finishes concurrently returns immediately instead of hanging.
            response = await asyncio.wait_for(waiter, timeout=timeout_s)
        except TimeoutError:
            return f"{request.get('subtype')}_timeout"
        except WorkerError as exc:
            return str(exc)
        finally:
            self._control_waiters.pop(request_id, None)
            live.pending_control_requests.discard(request_id)
        if response is None:
            return None
        if response.get("subtype") != "success":
            # The CLI refuses a control request it cannot serve in this context.
            error = response.get("error")
            return str(error) if error else "control_request_refused"
        return response

    async def interrupt(self, key: str) -> DeliverResult:
        live = self._live.get(key)
        if live is None or live.process.returncode is not None:
            return DeliverResult(False, "rejected", reason="not_live")
        response = await self._control_request(
            live, {"subtype": "interrupt"}, timeout_s=INTERRUPT_TIMEOUT_S
        )
        if isinstance(response, str):
            return DeliverResult(False, "rejected", reason=response)
        if response is None:
            return DeliverResult(False, "rejected", reason="worker_exited")
        # An interrupted turn produces no task-notification for its in-flight
        # background agents, so their ids would otherwise sit in this set for
        # the life of the process and defer every runtime deploy (#786). The
        # interrupt aborts them with the turn — consume the transcript up to
        # now (so a not-yet-replayed launch cannot resurrect them), then clear.
        rec = self._records.get(key)
        if rec is not None:
            self._sync_background_agents(rec, live)
        live.background_agents.clear()
        # The interrupt ended any hook still waiting on a block with the turn.
        self._drop_blocks(live)
        # The turn was ended on purpose, by whoever asked for the interrupt. Only
        # here, after the CLI has acked it: a refused or failed interrupt returned
        # above with the turn — and its claim on a revival — untouched.
        self._close_turn_marker(rec)
        return DeliverResult(True, "interrupted", session_id=rec.session_id if rec else None)

    def _base_permission_mode(self, rec: WorkerRecord) -> str:
        """The mode this chat runs in with the bypass switch off. A record's None
        means "inherit the account's mode"; the CLI has no such inherit."""
        return rec.permission_mode or self.permission_mode or "default"

    def _new_chat_bypass(self, permission_mode: str | None) -> bool:
        """Where a new record's bypass switch starts (#2319)."""
        mode = permission_mode or self.permission_mode
        return self.start_bypassed and mode not in NO_DEFAULT_BYPASS_MODES

    async def set_bypass_permissions(self, key: str, enabled: bool) -> DeliverResult:
        """Switch this chat into BYPASS_PERMISSIONS_MODE, or back to its own mode
        (#2285).

        A live worker is switched in place over the control channel, so the turn
        already running changes too; a parked or finished one only records the
        choice, which the next spawn honours. The recorded ``permission_mode`` is
        never changed, so switching off restores it exactly. A gate or classifier
        block already waiting stays for the boss to answer.
        """
        async with self._key_lock(key):
            rec = self._records.get(key)
            if rec is None:
                return DeliverResult(False, "rejected", reason="unknown_worker")
            if self._base_permission_mode(rec) == BYPASS_PERMISSIONS_MODE:
                return DeliverResult(
                    False, "rejected", reason="this chat already bypasses permissions"
                )
            if enabled == rec.bypass_permissions:
                return DeliverResult(True, "unchanged", session_id=rec.session_id)
            live = self._live.get(key)
            is_live = live is not None and live.process.returncode is None
            if is_live:
                mode = BYPASS_PERMISSIONS_MODE if enabled else self._base_permission_mode(rec)
                response = await self._control_request(
                    live,
                    {"subtype": "set_permission_mode", "mode": mode},
                    timeout_s=INTERRUPT_TIMEOUT_S,
                )
                if isinstance(response, str):
                    return DeliverResult(False, "rejected", reason=response)
                if response is None:
                    return DeliverResult(False, "rejected", reason="worker_exited")
            rec.bypass_permissions = enabled
            self._save_state()
            return DeliverResult(
                True, "updated" if is_live else "recorded", session_id=rec.session_id
            )

    async def report_block(
        self,
        key: str,
        *,
        session_token: str,
        tool_name: str,
        tool_input: dict[str, Any],
        reason: str,
    ) -> tuple[str, str]:
        """Show a tool call Claude's classifier blocked as this chat's pending
        prompt (#2271), returning ``(block_id, "")`` or ``("", refusal)``.

        The deck's PermissionDenied hook reports it and then waits on
        ``wait_block`` for the boss's answer; the CLI waits on the hook."""
        rec = self._records.get(key)
        live = self._live.get(key)
        if rec is None or live is None or live.process.returncode is not None:
            return "", "not_live"
        if rec.session_token and session_token != rec.session_token:
            return "", "session_token_mismatch"
        block = _Block(
            id=f"{BLOCK_ID_PREFIX}{uuid.uuid4().hex}",
            tool_name=tool_name,
            tool_input=tool_input,
            reason=reason,
            decision=asyncio.get_running_loop().create_future(),
        )
        live.blocks[block.id] = block
        return block.id, ""

    async def wait_block(self, key: str, block_id: str, *, timeout_s: float) -> str:
        """``allowed``, ``denied``, ``gone``, or ``pending`` when ``timeout_s``
        passed first. Collecting an answer forgets it."""
        if block_id in self._block_results:
            return self._block_results.pop(block_id)
        live = self._live.get(key)
        block = live.blocks.get(block_id) if live is not None else None
        if block is None:
            return "gone"
        try:
            await asyncio.wait_for(
                asyncio.shield(block.decision), timeout=min(timeout_s, BLOCK_WAIT_MAX_S)
            )
        except TimeoutError:
            return "pending"
        return self._block_results.pop(block_id, block.decision.result())

    def _settle_block(self, live: _LiveWorker, block: _Block, state: str) -> None:
        live.blocks.pop(block.id, None)
        if not block.decision.done():
            block.decision.set_result(state)
            self._block_results[block.id] = state
            # A hook that died never collects its answer; keep only recent ones.
            while len(self._block_results) > 256:
                self._block_results.pop(next(iter(self._block_results)))

    def _drop_blocks(self, live: _LiveWorker) -> None:
        """The turn or the process ended with blocks still open: nobody can
        retry them now, so their prompts go away."""
        for block in list(live.blocks.values()):
            self._settle_block(live, block, "gone")

    def _answer_block(
        self, key: str, live: _LiveWorker, block: _Block, decision: str | None
    ) -> None:
        allowed = decision in ("accept", "acceptForSession")
        if allowed:
            # Written before the hook hears the answer, so the retry finds it.
            classifier_grants.write_grant(
                self._artifact_root, self.account.key, key, block.tool_name, block.tool_input
            )
        self._settle_block(live, block, "allowed" if allowed else "denied")

    async def answer(
        self,
        key: str,
        interaction_id: str,
        *,
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> DeliverResult:
        """Resolve the worker's open `can_use_tool` control_request.

        ``answers`` is keyed by the question's positional id ("0", "1", …). For
        AskUserQuestion an "allow" carries the selected labels back as the
        ``answers`` map (question text → answer string) inside ``updatedInput``;
        for a permission gate the decision maps to allow/deny.

        An id from ``open_questions_id`` answers the chat's open questions instead
        (#3451): see ``_answer_open_questions``.
        """
        if interaction_id.startswith(OPEN_QUESTIONS_PREFIX):
            async with self._question_locks.setdefault(key, asyncio.Lock()):
                return await self._answer_open_questions(key, answers, decision)
        live = self._live.get(key)
        if live is None or live.process.returncode is not None:
            return DeliverResult(False, "rejected", reason="not_live")
        block = live.blocks.get(interaction_id)
        if block is not None:
            try:
                self._answer_block(key, live, block, decision)
            except OSError as exc:
                return DeliverResult(
                    False, "rejected", reason=f"could not record the approval: {exc}"
                )
            rec = self._records.get(key)
            return DeliverResult(True, "answered", session_id=rec.session_id if rec else None)
        pending = live.pending_interaction
        if pending is None or pending.get("request_id") != interaction_id:
            return DeliverResult(False, "rejected", reason="interaction_not_pending")
        response = self._build_tool_decision(pending, answers, decision)
        try:
            await self._write(
                live,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": interaction_id,
                        "response": response,
                    },
                },
            )
        except WorkerError as exc:
            return DeliverResult(False, "rejected", reason=str(exc))
        live.pending_interaction = None
        # Answered: the next runtime must not ask it again (#3248).
        self._save_holder(key, live)
        rec = self._records.get(key)
        if rec is not None and pending.get("tool_name") == "AskUserQuestion":
            self._close_questions(
                rec, {i["id"] for i in rec.open_questions if i["request_id"] == interaction_id}
            )
        return DeliverResult(True, "answered", session_id=rec.session_id if rec else None)

    def _open_questions(self, rec: WorkerRecord, request_id: str, tool_input: object) -> None:
        """Keep a new AskUserQuestion's questions open beside any older ones (#3451)."""
        if request_id in rec.closed_question_requests or any(
            item["request_id"] == request_id for item in rec.open_questions
        ):
            return  # a held worker's log read again after a restart
        items = _question_items(request_id, tool_input if isinstance(tool_input, dict) else {})
        if not items:
            return
        # When it was asked, so a rollback to before then closes it (#3474).
        asked_at = time.time()
        items = [{**item, "asked_at": asked_at} for item in items]
        # No limit: a question leaves only when answered, dismissed or asked again
        # (#3451 review, #3637).
        rec.open_questions = rec.open_questions + items
        if not self._drop_questions(rec, _repeated_question_ids(rec.open_questions)):
            self._save_state()

    def _close_questions(self, rec: WorkerRecord, ids: set[str]) -> None:
        """Take answered or dismissed questions off the chat's open list (#3451)."""
        self._drop_questions(rec, ids)

    def _drop_questions(self, rec: WorkerRecord, ids: set[str]) -> bool:
        """Close ``ids`` and save; False when none of them was open."""
        closed = [item for item in rec.open_questions if item["id"] in ids]
        if not closed:
            return False
        rec.open_questions = [item for item in rec.open_questions if item["id"] not in ids]
        still_asked = {item["request_id"] for item in rec.open_questions}
        for request_id in dict.fromkeys(item["request_id"] for item in closed):
            if request_id not in still_asked and request_id not in rec.closed_question_requests:
                rec.closed_question_requests.append(request_id)
        rec.closed_question_requests = rec.closed_question_requests[-CLOSED_QUESTIONS_MAX:]
        self._save_state()
        return True

    async def _answer_open_questions(
        self, key: str, answers: dict[str, list[str]], decision: str | None
    ) -> DeliverResult:
        """Answer or dismiss any of a chat's open questions, leaving the rest open (#3451).

        ``answers`` is keyed by question id (``<request id>.<index>``); a ``decision`` of
        ``dismiss:<question id>`` dismisses that one question and answers nothing. While
        the turn is still inside one of the questions, the whole submit goes back as that
        tool call's answer: the CLI's own shape when every part of it was answered and
        nothing else, otherwise a note naming each answer and what stays open. With no
        turn inside a question, the note reaches the chat as an ordinary message.
        """
        rec = self._records.get(key)
        if rec is None:
            return DeliverResult(False, "rejected", reason="interaction_not_pending")
        by_id = {item["id"]: item for item in rec.open_questions}
        dismissed = (
            decision.removeprefix("dismiss:")
            if isinstance(decision, str) and decision.startswith("dismiss:")
            else None
        )
        if dismissed is not None:
            if dismissed not in by_id:
                return DeliverResult(False, "rejected", reason="interaction_not_pending")
            answered: list[tuple[dict[str, Any], list[str]]] = []
            resolved = {dismissed}
        else:
            answered = [
                (item, values) for item in rec.open_questions if (values := answers.get(item["id"]))
            ]
            if not answered:
                return DeliverResult(False, "rejected", reason="interaction_not_pending")
            resolved = {item["id"] for item, _ in answered}
        still_open = [item for item in rec.open_questions if item["id"] not in resolved]
        live = self._live.get(key)
        alive = live is not None and live.process.returncode is None
        pending = live.pending_interaction if alive else None
        asking = (
            pending.get("request_id")
            if pending is not None and pending.get("tool_name") == "AskUserQuestion"
            else None
        )
        asked_now = [item for item in rec.open_questions if item["request_id"] == asking]
        if asked_now and (answered or not any(i in still_open for i in asked_now)):
            # The CLI's own answer only when this submit answers every question the call
            # asked; one dismissed earlier would be missing from it (#3451 review).
            asked_ids = {
                item["id"]
                for item in _question_items(
                    asking, pending["input"] if isinstance(pending.get("input"), dict) else {}
                )
            }
            if answered and {item["id"] for item, _ in answered} == asked_ids:
                response = self._build_tool_decision(
                    pending, {str(item["index"]): values for item, values in answered}, "accept"
                )
            elif answered:
                response = {"behavior": "deny", "message": _answers_note(answered, still_open)}
            else:
                response = self._build_tool_decision(pending, {}, "decline")
            try:
                await self._write(
                    live,
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": asking,
                            "response": response,
                        },
                    },
                )
            except WorkerError as exc:
                return DeliverResult(False, "rejected", reason=str(exc))
            live.pending_interaction = None
            self._save_holder(key, live)
        elif answered:
            note = _answers_note(answered, still_open)
            # Keyed by the questions it answers as well as the words, so the same answer to
            # a question asked again later is a new message, while a repeated tap is not.
            fence = hashlib.sha256(
                "\n".join([*sorted(resolved), note]).encode()
            ).hexdigest()[:16]
            result = await self.deliver(
                key,
                note,
                delivery_id=f"question-answer:{fence}",
                delegated=rec.delegated,
                role=rec.role,
            )
            if not result.accepted:
                return result
        self._close_questions(rec, resolved)
        return DeliverResult(True, "answered", session_id=rec.session_id)

    @staticmethod
    def _build_tool_decision(
        pending: dict[str, Any],
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> dict[str, Any]:
        tool_name = pending.get("tool_name")
        tool_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
        deny = decision in ("decline", "cancel")
        if tool_name == "AskUserQuestion":
            if deny:
                return {"behavior": "deny", "message": "The user dismissed the questions."}
            questions = tool_input.get("questions")
            answer_map: dict[str, str] = {}
            if isinstance(questions, list):
                for index, question in enumerate(questions):
                    values = answers.get(str(index)) or []
                    if not isinstance(question, dict) or not values:
                        continue
                    text = question.get("question")
                    if isinstance(text, str):
                        answer_map[text] = ", ".join(values)
            return {"behavior": "allow", "updatedInput": {**tool_input, "answers": answer_map}}
        # Permission gate on a regular tool. This one fails CLOSED (#427): the
        # control plane has no authentication, so "allow" must require an explicit
        # accept rather than being whatever is left after the deny check. A missing
        # or misspelled decision used to fall through to allow, which let any local
        # caller approve any pending tool call unattended.
        #
        # The AskUserQuestion branch above deliberately keeps the old shape: there,
        # "allow" only submits answers, so an absent decision is harmless — and
        # failing closed would dismiss the boss's question instead of answering it,
        # breaking the deck's own answer path (#416).
        if decision in _APPROVE_DECISIONS:
            return {"behavior": "allow", "updatedInput": tool_input}
        deny_body: dict[str, Any] = {"behavior": "deny", "message": "Denied by the user."}
        if decision == "cancel":
            deny_body["interrupt"] = True
        elif decision not in ("decline", "cancel"):
            deny_body["message"] = (
                "No explicit approval was recorded, so the tool call was denied."
            )
        return deny_body

    def _close_turn_marker(self, rec: WorkerRecord | None) -> None:
        """Record that this chat's turn was ended deliberately, not by a death.

        Stopping, parking and an acknowledged interrupt all end a turn because
        somebody asked them to, and they all go through the same SIGTERM as the
        runtime's own teardown — so the CLI may write the same aborted ``result``
        on its way out. Without this the next boot would read the open marker and
        resume work an operator had just cancelled. Deliberately not inside
        ``_terminate``: that is also the whole-runtime shutdown path, where the
        open turn is exactly what revival is for (#1844).

        The positive ``turn_closed_at`` stamp is the authoritative part. Clearing
        the open marker alone would still leave revival's fallback deduction
        saying "delivered, never answered" whenever no terminal ``result`` arrived
        — and none is guaranteed, because ``_terminate`` escalates to SIGKILL and
        drops the reader once the process is gone.
        """
        if rec is None:
            return
        rec.turn_open_at = 0.0
        rec.turn_closed_at = time.time()
        # Whatever was queued behind the turn went with it, and was not what the
        # person ended; a later death must not bring it back (#2898).
        rec.unread_messages = []
        rec.unread_dropped = 0
        self._save_state()

    async def stop_worker(self, key: str) -> DeliverResult:
        async with self._key_lock(key):
            live = self._live.pop(key, None)
            if live is None:
                return DeliverResult(False, "rejected", reason="not_live")
            await self._terminate(live)
            rec = self._records.get(key)
            self._close_turn_marker(rec)
            return DeliverResult(True, "stopped", session_id=rec.session_id if rec else None)

    async def rewind_worker(
        self, key: str, at: str, *, since: float | None = None
    ) -> DeliverResult:
        """Roll an idle chat back so its next turn continues from line ``at`` (#3474).

        The process holds the conversation in memory, so it is ended; the record keeps
        everything else, and the next spawn resumes the same session at ``at``. A chat
        mid-turn is refused rather than stopped: the person stops it first. Questions
        asked from ``since`` on are closed: the agent no longer remembers asking them.
        """
        async with self._key_lock(key):
            rec = self._records.get(key)
            if rec is None or not rec.session_id:
                return DeliverResult(False, "rejected", reason="unknown_worker")
            live = self._live.get(key)
            if live is not None and live.process.returncode is None and (
                live.turn_active or live.background_agents
            ):
                return DeliverResult(
                    False, "rejected", reason="worker_busy", session_id=rec.session_id
                )
            if live is not None:
                self._live.pop(key, None)
                await self._terminate(live)
            self._close_turn_marker(rec)
            rec.resume_at, rec.resume_at_session = at, rec.session_id
            if since is not None:
                # A question recorded before asked_at was kept is left open: its time is
                # unknown, and closing a question the agent still waits on strands it.
                self._close_questions(
                    rec,
                    {
                        item["id"]
                        for item in rec.open_questions
                        if item.get("asked_at", 0.0) >= since
                    },
                )
            self._save_state()
            return DeliverResult(True, "rewound", session_id=rec.session_id)

    async def park_worker(self, key: str) -> DeliverResult:
        """Terminate a process while retaining resumable session lineage.

        Parking is idempotent: an already parked known key is accepted so callers
        can safely retry after losing the response.
        """
        async with self._key_lock(key):
            rec = self._records.get(key)
            live = self._live.pop(key, None)
            if live is not None:
                await self._terminate(live)
            self._close_turn_marker(rec)
            # Unknown is already safely parked. Accept it so a caller that lost
            # the original response (or survived state repair) never deadlocks
            # its own durable lifecycle transition.
            return DeliverResult(
                True, "parked", session_id=rec.session_id if rec else None
            )

    async def release_worker(self, key: str) -> DeliverResult:
        """Terminate and forget a key atomically; safe to retry after success."""
        async with self._key_lock(key):
            rec = self._records.get(key)
            if rec is not None and rec.pending_context_delivery_id:
                return DeliverResult(
                    False, "rejected", reason="context_operation_pending"
                )
            live = self._live.pop(key, None)
            if live is not None:
                await self._terminate(live)
            if rec is not None:
                self._records.pop(key, None)
                self._save_state()
            return DeliverResult(
                True, "released", session_id=rec.session_id if rec else None
            )

    async def forget(self, key: str) -> DeliverResult:
        """Drop a finished key's record (its session lineage) from the registry."""
        async with self._key_lock(key):
            rec = self._records.get(key)
            if rec and rec.pending_context_delivery_id:
                return DeliverResult(
                    False,
                    "rejected",
                    reason="context_operation_pending",
                    session_id=rec.session_id,
                )
            if key in self._live:
                return DeliverResult(
                    False,
                    "rejected",
                    reason="worker_live",
                    session_id=rec.session_id if rec else None,
                )
            removed = self._records.pop(key, None) is not None
            if removed:
                self._save_state()
            return DeliverResult(
                removed,
                "forgotten" if removed else "rejected",
                reason=None if removed else "unknown_worker",
                session_id=rec.session_id if rec else None,
            )

    async def stop(self) -> None:
        # A spare has no chat to keep; a held one left running would only be killed
        # by the next runtime's reattach, so stop it now.
        for spare in list(self._spares.values()):
            await self._retire_spare(spare)
        live = list(self._live.items())
        self._live.clear()
        ending: list[_LiveWorker] = []
        for key, worker in live:
            process = worker.process
            if isinstance(process, HeldProcess) and process.returncode is None:
                # A held worker outlives this runtime (#3248): save where its log was read
                # up to, and let go of it for the next runtime to reconnect to.
                worker.detached = True
                self._save_holder(key, worker)
                process.detach()
                if worker.reader_task is not None:
                    worker.reader_task.cancel()
                    ending.append(worker)
            else:
                ending.append(worker)
        await asyncio.gather(
            *(
                asyncio.gather(w.reader_task, return_exceptions=True)
                if w.detached
                else self._terminate(w)
                for w in ending
            ),
            return_exceptions=True,
        )

    def _relocation_target(
        self,
        rec: WorkerRecord,
        live: _LiveWorker | None,
        *,
        fresh: bool,
        cwd: str | None,
    ) -> Path | None:
        """The repository this chat resumes in, when its own directory is gone (#2524).

        Only for the chat's next turn: a fresh spawn or a caller naming a directory
        chooses for itself, and a turn in flight keeps the process it has.
        """
        if fresh or (cwd is not None and cwd != rec.cwd) or not rec.cwd:
            return None
        if live is not None and live.process.returncode is None and (
            live.turn_active or live.background_agents
        ):
            return None
        recorded = Path(rec.cwd)
        if cwd_exists(recorded):
            return None
        return continued_chat_cwd(recorded)

    async def _relocate(
        self, key: str, rec: WorkerRecord, live: _LiveWorker | None
    ) -> _LiveWorker | None:
        """Move this chat to its repository; the next spawn resumes it there.

        The record keeps its session, receipts and settings: only the directory
        changes. An idle process still sitting in the deleted directory is ended so
        the turn starts where the chat now lives.
        """
        target = continued_chat_cwd(Path(rec.cwd))
        if target is None:
            return live
        log.info(
            "claude worker %s: %s is gone; resuming in %s", key, rec.cwd, target
        )
        rec.relocated_from = rec.cwd
        rec.cwd = str(target)
        self._save_state()
        if live is not None:
            self._live.pop(key, None)
            await self._terminate(live)
        return None

    # --- process management ------------------------------------------------

    def _command(
        self,
        resume_id: str | None,
        *,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        base_permission_mode: str | None = None,
        delegated: bool = False,
        role: SessionRole | None = None,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
        relocated: tuple[str, str] | None = None,
        resume_at: str | None = None,
    ) -> list[str]:
        instructions = agentdeck_session_instructions(
            role=resolve_session_role(delegated=delegated, role=role),
            provider="claude_code",
            session_token=session_token,
        )
        if relocated is not None:
            instructions = f"{instructions}\n\n{relocation_notice(*relocated)}"
        cmd = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--append-system-prompt",
            instructions,
        ]
        if self.interactive_prompts:
            # Route interactive decisions (AskUserQuestion and permission gates)
            # to us over the control channel as `can_use_tool` control_requests.
            # Also the *only* way AskUserQuestion becomes available to a headless
            # `-p` worker. In `auto` a gate is the classifier's own fallback after
            # repeated blocks; it reaches the boss like any other (#2271).
            cmd += ["--permission-prompt-tool", "stdio"]
        effective_model = model or self.model
        effective_permission_mode = permission_mode or self.permission_mode
        if effective_model:
            cmd += ["--model", effective_model]
        if effort:
            cmd += ["--effort", effort]
        if effective_permission_mode:
            cmd += ["--permission-mode", effective_permission_mode]
        # Lets the "Bypass permissions" switch move this process into
        # BYPASS_PERMISSIONS_MODE later; on its own it changes nothing (#2285).
        cmd += ["--allow-dangerously-skip-permissions"]
        # Every chat gets the block hooks, and an auto chat the skip list too (#2271).
        # Chosen by the chat's own mode, not the bypass it may start in, so
        # switching bypass off lands in an auto chat with its skip list (#2319).
        settings_mode = base_permission_mode or effective_permission_mode
        settings = self._settings_files[settings_mode == "auto"]
        cmd += ["--settings", str(settings)]
        if resume_id:
            cmd += ["--resume", resume_id]
            if resume_at:
                # A rollback (#3474): the same session, continued from that line.
                cmd += ["--resume-session-at", resume_at]
        elif fork_from is not None:
            # A side chat: a new session id holding the source's history up to and
            # including the chosen line (#2485). `--resume-session-at` is hidden from
            # `--help` but is what the Agent SDK's `resumeSessionAt` sends.
            cmd += [
                "--resume",
                fork_from.session_id,
                "--fork-session",
                "--resume-session-at",
                fork_from.ref,
            ]
        return cmd

    async def _memory_scope(self, key: str) -> tuple[str, list[str]]:
        """The transient scope this spawn runs in, as ``(unit, argv prefix)``.

        ``("", [])`` when no cap applies — either none is configured or this host
        cannot run one. Both mean today's plain spawn: a worker that cannot be
        capped still starts, because refusing to work is worse than working
        uncapped.
        """
        if not self.memory_max:
            return "", []
        if not await worker_limits.available(self.memory_max):
            return "", []
        unit = worker_limits.scope_unit(self.account.label, key)
        return unit, worker_limits.scope_prefix(unit, self.memory_max)

    async def _settle_scope(self, key: str, live: _LiveWorker) -> None:
        """Record why a dead worker died, and tidy its scope away.

        systemd leaves a cap-killed scope behind in ``failed`` state saying
        ``oom-kill``; a scope that ended any other way has already collected
        itself and answers with nothing. That is the whole difference between
        "this chat asked for more memory than it may have" and an ordinary
        death, so it is worth one ``systemctl show`` per worker exit.
        """
        unit, live.scope_unit = live.scope_unit, ""
        if not unit:
            return
        try:
            result = await worker_limits.scope_result(unit)
            if result == worker_limits.RESULT_OOM_KILL:
                rec = self._records.get(key)
                if rec is not None:
                    rec.last_exit_reason = EXIT_MEMORY_CAP
                    rec.last_exit_at = time.time()
                    self._save_state()
                log.warning(
                    "worker %s:%s killed by its %s memory cap",
                    self.account.key,
                    key,
                    self.memory_max,
                )
            if result:
                await worker_limits.clear_scope(unit)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- naming a death must never break teardown
            log.exception("could not settle memory scope %s", unit)

    # --- spares: CLIs booted ahead of a new chat (#3420) ---------------------

    async def prepare_spare(
        self,
        shape: str,
        key: str,
        *,
        session_token: str,
        cwd: str,
        model: str | None = None,
        effort: str | None = None,
        role: SessionRole | None = None,
    ) -> str:
        """Keep a booted CLI waiting for a new chat started under ``key``.

        Answers ``kept`` when this shape already waits with exactly this, ``booted``
        after replacing whatever waited for the shape before, and a refusal otherwise.
        The fields are the ones a fresh interactive chat is spawned from; anything a
        start sends differently spawns cold and replaces the spare instead.
        """
        async with self._key_lock(key):
            if key in self._records or key in self._live:
                return "key_in_use"
            if not Path(cwd).is_dir():
                return "cwd_missing"
            current = self._spares.get(shape)
            if (
                current is not None
                and current.process.returncode is None
                and (current.key, current.session_token, current.cwd, current.model,
                     current.effort, current.role) == (key, session_token, cwd, model, effort, role)
            ):
                return "kept"
            for other in list(self._spares.values()):
                if other.shape == shape or other.key == key:
                    await self._retire_spare(other)
            return await self._boot_spare(
                shape,
                key,
                session_token=session_token,
                cwd=cwd,
                model=model,
                effort=effort,
                role=role,
            )

    async def drop_spare(self, shape: str) -> str:
        """Stop the CLI waiting for ``shape``: nothing asks for that start any more."""
        spare = self._spares.get(shape)
        if spare is None:
            return "none"
        async with self._key_lock(spare.key):
            await self._retire_spare(spare)
        return "dropped"

    async def _boot_spare(
        self,
        shape: str,
        key: str,
        *,
        session_token: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        role: SessionRole | None = None,
    ) -> str:
        # The record a fresh interactive deliver would make for this key.
        rec = WorkerRecord(
            key=key,
            cwd=cwd,
            bypass_permissions=self._new_chat_bypass(None),
            delegated=False,
            role=resolve_session_role(delegated=False, role=role),
            session_token=session_token,
            model=model,
            effort=effort,
        )
        env, scope_unit, scope_prefix, command = await self._spawn_plan(
            key,
            rec,
            resume_id=None,
            model=model,
            effort=effort,
            permission_mode=BYPASS_PERMISSIONS_MODE if rec.bypass_permissions else None,
        )
        try:
            process, holder = await self._launch(
                command, scope_unit, scope_prefix, cwd=cwd, env=env
            )
        except WorkerError as exc:
            log.warning("could not boot a spare for %s:%s: %s", self.account.key, shape, exc)
            return "spawn_failed"
        if holder is not None:
            # Recorded at once, so a runtime that dies now leaves nothing unknown behind:
            # the next one's reattach stops a holder no chat record names.
            self._holders[key] = holder
            self._holder_store.save(self._holders)
        spare = _Spare(
            shape=shape,
            key=key,
            session_token=session_token,
            cwd=cwd,
            model=model,
            effort=effort,
            command=command,
            env=env,
            scope_unit=scope_unit,
            process=process,
            holder=holder,
            role=role,
        )
        spare.watcher = asyncio.create_task(
            self._watch_spare(spare), name=f"claude-spare:{self.account.key}:{shape}"
        )
        # Another boot for this shape may have finished while this one spawned: one
        # spare per shape, the newest.
        previous = self._spares.get(shape)
        self._spares[shape] = spare
        if previous is not None:
            await self._retire_spare(previous)
        return "booted"

    async def _watch_spare(self, spare: _Spare) -> None:
        """Boot the spare again once it is old enough to be stale, or after it dies."""
        try:
            await asyncio.wait_for(asyncio.shield(spare.process.wait()), SPARE_MAX_AGE_S)
            delay = SPARE_REBOOT_DELAY_S
            log.warning(
                "spare %s:%s exited on its own; booting another", self.account.key, spare.shape
            )
        except TimeoutError:
            delay = 0.0
        if delay:
            await asyncio.sleep(delay)
        async with self._key_lock(spare.key):
            if self._spares.get(spare.shape) is not spare:
                return
            spare.watcher = None
            await self._retire_spare(spare)
            if (
                spare.shape in self._spares
                or spare.key in self._records
                or spare.key in self._live
            ):
                return
            await self._boot_spare(
                spare.shape,
                spare.key,
                session_token=spare.session_token,
                cwd=spare.cwd,
                model=spare.model,
                effort=spare.effort,
                role=spare.role,
            )

    def _take_spare(
        self, key: str, *, command: list[str], cwd: str, env: dict[str, str]
    ) -> _Spare | None:
        """The spare booted for exactly this spawn, now no longer a spare; or None."""
        spare = next((item for item in self._spares.values() if item.key == key), None)
        if (
            spare is None
            or spare.process.returncode is not None
            or (spare.command, spare.cwd, spare.env) != (command, cwd, env)
        ):
            return None
        del self._spares[spare.shape]
        spare.adopted = True
        if spare.watcher is not None:
            spare.watcher.cancel()
            spare.watcher = None
        return spare

    async def _drop_spare_for_key(self, key: str) -> None:
        """A cold spawn of ``key`` replaces a spare booted for it with other settings."""
        spare = next((item for item in self._spares.values() if item.key == key), None)
        if spare is not None:
            await self._retire_spare(spare)

    async def _retire_spare(self, spare: _Spare) -> None:
        if spare.adopted:
            return
        if self._spares.get(spare.shape) is spare:
            del self._spares[spare.shape]
        if spare.watcher is not None and spare.watcher is not asyncio.current_task():
            spare.watcher.cancel()
        spare.watcher = None
        await self._terminate(
            _LiveWorker(
                process=spare.process,
                reader_task=None,  # type: ignore[arg-type]
                initialized=asyncio.get_running_loop().create_future(),
            )
        )
        if spare.holder is not None:
            if self._holders.get(spare.key) is spare.holder:
                del self._holders[spare.key]
                self._holder_store.save(self._holders)
            spare.holder.remove_files()
        if spare.scope_unit:
            with contextlib.suppress(Exception):
                if await worker_limits.scope_result(spare.scope_unit):
                    await worker_limits.clear_scope(spare.scope_unit)

    async def _spawn(
        self,
        key: str,
        rec: WorkerRecord,
        *,
        resume_id: str | None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> _LiveWorker:
        started = time.monotonic()
        env, scope_unit, scope_prefix, command = await self._spawn_plan(
            key,
            rec,
            resume_id=resume_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            fork_from=fork_from,
        )
        spare = (
            self._take_spare(key, command=command, cwd=rec.cwd, env=env)
            if resume_id is None and fork_from is None
            else None
        )
        if spare is not None:
            process, holder, scope_unit = spare.process, spare.holder, spare.scope_unit
        else:
            await self._drop_spare_for_key(key)
            process, holder = await self._launch(
                command, scope_unit, scope_prefix, cwd=rec.cwd, env=env
            )
        return self._register_spawn(
            key,
            rec,
            process,
            holder,
            scope_unit,
            warm=None if resume_id is not None or fork_from is not None else spare is not None,
            started=started,
        )

    async def _spawn_plan(
        self,
        key: str,
        rec: WorkerRecord,
        *,
        resume_id: str | None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> tuple[dict[str, str], str, list[str], list[str]]:
        """The environment, memory scope and argv one spawn of ``rec`` runs with."""
        env = os.environ.copy()
        # This process may itself be running inside an owned session, whose
        # AGENTDECK_* point at *that* session. Inheriting them would hand the
        # worker another session's identity, so drop them before deciding what
        # this spawn is entitled to.
        env.pop("AGENTDECK_URL", None)
        env.pop("AGENTDECK_SESSION_KEY", None)
        claude_env.point_at(env, self.account.root)  # macOS keeps ~/.claude unset
        env["AGENTDECK_GH_CONSUMER"] = f"{self.account.key}:{key}"
        env[CONTEXT_DIR_ENV] = str(self._artifact_root)
        env[ACCOUNT_KEY_ENV] = self.account.key
        env[WORKER_KEY_ENV] = key
        if rec.session_token:
            env[SESSION_TOKEN_ENV] = rec.session_token
        env[POLICY_MODE_ENV] = str((rec.command_artifacts or {}).get("mode") or "off")
        # Reads through the caching, rate-limited `gh` rather than straight at
        # GitHub, so a worker cannot spend the reserve the poller holds for its
        # own wakes (#814). Writes still reach the real binary untouched.
        env["PATH"] = gh_shim.worker_path(env)
        # Every GitHub push and fetch goes through AgentDeck's credential helper,
        # which answers per repository owner with a non-human authority and
        # refuses everything else; no inherited token survives (#2289).
        github_identity.configure_github_git(env)
        if self._base_url:
            env["AGENTDECK_URL"] = self._base_url
            # Only a resume knows this session's id at spawn; a fresh worker
            # gets its id after init, so leave the key unset there rather than
            # inject a wrong one. session.key is f"{account.key}:{session_id}".
            if resume_id:
                env["AGENTDECK_SESSION_KEY"] = f"{self.account.key}:{resume_id}"
        # Each worker gets a cgroup of its own, so a runaway one is killed alone
        # instead of taking the runtime and every other chat with it (#1546).
        scope_unit, scope_prefix = await self._memory_scope(key)
        command = self._command(
            resume_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            base_permission_mode=self._base_permission_mode(rec),
            delegated=rec.delegated,
            role=rec.role,
            session_token=rec.session_token,
            fork_from=fork_from,
            relocated=(
                (rec.relocated_from, rec.cwd) if rec.relocated_from and resume_id else None
            ),
            resume_at=(
                rec.resume_at if resume_id and resume_id == rec.resume_at_session else None
            ),
        )
        return env, scope_unit, scope_prefix, command

    async def _launch(
        self,
        command: list[str],
        scope_unit: str,
        scope_prefix: list[str],
        *,
        cwd: str,
        env: dict[str, str],
    ) -> tuple[Any, HolderInfo | None]:
        """Start the CLI, under a holder when this host holds its workers (#3248)."""
        holder: HolderInfo | None = None
        try:
            if self.hold_processes and scope_prefix:
                holder = self._new_holder(scope_unit)
                process = await spawn_holder(
                    self._process_factory,
                    scope_prefix,
                    sys.executable,
                    holder,
                    command,
                    cwd=cwd,
                    env=env,
                    limit=STDOUT_LINE_LIMIT,
                )
            else:
                process = await self._process_factory(
                    *scope_prefix,
                    *command,
                    cwd=cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                    limit=STDOUT_LINE_LIMIT,
                )
        except (OSError, ValueError) as exc:
            if holder is not None:
                holder.remove_files()
            raise WorkerError(f"could not start claude worker: {exc}") from exc
        return process, holder

    def _register_spawn(
        self,
        key: str,
        rec: WorkerRecord,
        process: Any,
        holder: HolderInfo | None,
        scope_unit: str,
        *,
        warm: bool | None,
        started: float,
    ) -> _LiveWorker:
        """Make a started CLI this chat's live worker and start reading it."""
        # The chat is running again, so an older death has stopped being the
        # thing to say about it.
        if rec.last_exit_reason:
            rec.last_exit_reason = ""
            rec.last_exit_at = 0.0
            self._save_state()
        # The move has been told to the process that runs in the new directory.
        if rec.relocated_from:
            rec.relocated_from = None
            self._save_state()
        initialized: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        live = _LiveWorker(
            process=process,
            reader_task=None,  # type: ignore[arg-type]
            initialized=initialized,
            scope_unit=scope_unit,
        )
        if holder is not None:
            live.holder_key = key
            old = self._holders.pop(key, None)
            if old is not None and old is not holder:
                kill_holder(old)
                old.remove_files()
            self._holders[key] = holder
            self._save_holder(key, live)
        live.reader_task = asyncio.create_task(
            self._read_loop(key, live), name=f"claude-worker:{self.account.key}:{key}"
        )
        self._live[key] = live
        self.on_state_change(key)
        if warm is not None:
            # How long a new chat waited for its agent, warm or cold, so the wait a
            # person sees can be read from the journal (#3420).
            def log_start(done: asyncio.Future[str]) -> None:
                if done.cancelled() or done.exception() is not None:
                    return
                log.info(
                    "start %s:%s %s init_ms=%d",
                    self.account.key,
                    key,
                    "warm" if warm else "cold",
                    (time.monotonic() - started) * 1000,
                )

            initialized.add_done_callback(log_start)
        return live

    async def _spawn_and_deliver(
        self,
        key: str,
        rec: WorkerRecord,
        message: str,
        image_blocks: list[dict[str, Any]],
        *,
        resume_id: str | None,
        model: str | None,
        effort: str | None,
        permission_mode: str | None,
        before_write: Callable[[], None] | None = None,
        fork_from: ForkFrom | None = None,
        delivery_id: str | None = None,
    ) -> _LiveWorker:
        live = await self._spawn(
            key,
            rec,
            resume_id=resume_id,
            model=model,
            effort=effort,
            permission_mode=permission_mode,
            fork_from=fork_from,
        )
        wrote = False
        try:
            if before_write is not None:
                before_write()
            await self._write_user_message(live, message, image_blocks, delivery_id=delivery_id)
            wrote = True  # payload is now in the child's stdin — never re-send it
            if resume_id is not None and resume_id == rec.resume_at_session:
                # The message hangs off the rollback point now; a later plain resume
                # follows that branch (#3474). Kept until here, so a revive that died
                # before the write retries at the same point.
                rec.resume_at = rec.resume_at_session = None
                self._save_state()
            live.turn_active = True
            await asyncio.wait_for(asyncio.shield(live.initialized), timeout=INIT_TIMEOUT_S)
            return live
        except (TimeoutError, WorkerError, OSError) as exc:
            if self._live.get(key) is live:
                self._live.pop(key, None)
            await self._terminate(live)
            if live.initialized.done() and not live.initialized.cancelled():
                live.initialized.exception()  # consume shutdown error after timeout
            # Only an init *timeout* leaves the child possibly alive and mid-turn
            # after our write — then re-sending could run the task twice, so fence
            # it. If the process *died* (WorkerError/OSError, e.g. a gone --resume
            # session that exits without processing), the write never took effect,
            # so recovery may safely re-send under a fresh spawn.
            started = wrote and isinstance(exc, TimeoutError)
            if isinstance(exc, WorkerError):
                exc.write_started = exc.write_started or started
                raise
            raise WorkerError(
                f"claude worker failed to initialize: {exc}", write_started=started
            ) from exc

    async def _terminate(self, live: _LiveWorker) -> None:
        process = live.process
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            pid = getattr(process, "pid", None)
            try:
                if isinstance(pid, int):
                    os.killpg(pid, signal.SIGTERM)
                else:  # test doubles without a pid
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    if isinstance(pid, int):
                        os.killpg(pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if live.reader_task is not None and live.reader_task is not asyncio.current_task():
            live.reader_task.cancel()
            await asyncio.gather(live.reader_task, return_exceptions=True)

    async def _write(
        self, live: _LiveWorker, message: dict[str, Any], *, delivery_id: str | None = None
    ) -> None:
        process = live.process
        if process.stdin is None or process.returncode is not None:
            raise WorkerError("worker process is not running")
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            if delivery_id and isinstance(process, HeldProcess):
                # The holder records the id before the CLI can read the line (#3248).
                process.stdin.write(payload, delivery_id=delivery_id)
            else:
                process.stdin.write(payload)
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            # Nothing was handed to the transport, so this delivery reached
            # nothing and is cleanly retryable.
            raise WorkerError(f"could not write to claude worker: {exc}") from exc
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            # The payload is already in the transport's buffer and may well have
            # reached the child, so this is uncertain rather than refused: the
            # receipt stays prepared, the open turn keeps its claim on a revival,
            # and nothing re-sends it (#1844).
            raise WorkerError(
                f"could not flush a write to the claude worker: {exc}",
                write_started=True,
            ) from exc

    @staticmethod
    def _image_block(path_value: str) -> dict[str, Any]:
        try:
            media_type, data = read_supported_image(path_value)
        except UnsupportedImage as exc:
            raise WorkerError(str(exc)) from exc
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }

    async def _close_pending_question(self, live: _LiveWorker) -> None:
        """Close a question still open in the box before a message reaches the chat (#3121).

        A message written to the child while it waits on AskUserQuestion makes the CLI cancel
        the question with its own "the user doesn't want to proceed … STOP" result. A person
        who typed a reply instead of picking an option refused nothing, but the agent read
        two such cancels as the owner turning down the question box and asked its next
        question in plain text, which left the Work in Needs retry. So the deck answers the
        question itself first, saying what happened; the message then reaches the turn as an
        ordinary steer.
        """
        pending = live.pending_interaction
        if pending is None or pending.get("tool_name") != "AskUserQuestion":
            return
        try:
            await self._write(
                live,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": pending.get("request_id"),
                        "response": {"behavior": "deny", "message": QUESTION_SUPERSEDED},
                    },
                },
            )
        except WorkerError as exc:
            # The message itself was never written, so its delivery reached nothing.
            raise WorkerError(str(exc)) from exc
        if live.pending_interaction is pending:
            live.pending_interaction = None
            if live.holder_key:
                self._save_holder(live.holder_key, live)

    async def _write_user_message(
        self,
        live: _LiveWorker,
        text: str,
        image_blocks: list[dict[str, Any]],
        *,
        delivery_id: str | None = None,
    ) -> None:
        content = list(image_blocks)
        content.append({"type": "text", "text": text})
        await self._write(
            live,
            {
                "type": "user",
                "message": {"role": "user", "content": content},
            },
            delivery_id=delivery_id,
        )

    async def _read_loop(self, key: str, live: _LiveWorker) -> None:
        process = live.process
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                live.read_offset += len(line)
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                live.last_event_at = time.time()
                refusal = self._bypass_safety_refusal(key, event)
                if refusal is not None:
                    try:
                        await self._write(live, refusal)
                        continue
                    except WorkerError:
                        # Not answered, so the boss is asked after all.
                        log.warning("could not refuse a safety check for %s", key)
                self._handle_event(key, live, event)
                if live.holder_key and (
                    event.get("type") in _HOLDER_SAVE_EVENTS
                    or time.monotonic() - live.offset_saved_at >= OFFSET_SAVE_INTERVAL_S
                ):
                    self._save_holder(key, live)
                self.on_state_change(key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- reader failure only affects one worker
            log.exception("claude worker reader failed for %s:%s", self.account.key, key)
        finally:
            # Let go on the way down, not ended: the holder keeps the CLI running, with
            # its question, its grants and its scope, for the next runtime (#3248).
            if not live.detached:
                await self._reader_ended(key, live)

    async def _reader_ended(self, key: str, live: _LiveWorker) -> None:
        """Tidy up after a worker whose output ended: its process is gone or going."""
        process = live.process
        if not live.initialized.done():
            live.initialized.set_exception(
                WorkerError("claude worker exited before session initialization")
            )
        # The process is gone: fail any control request still waiting on an
        # ack for this worker with None, so its caller returns at once
        # instead of blocking for the full timeout.
        for request_id in list(live.pending_control_requests):
            waiter = self._control_waiters.get(request_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
        live.pending_control_requests.clear()
        if self._live.get(key) is live:
            del self._live[key]
            live.turn_active = False
            # The reader is tearing down while still the registered worker.
            # On a clean process exit the child is already gone (returncode
            # set) and its session lineage is kept for a later --resume. But
            # if the reader itself died with the child still alive (e.g. an
            # oversized stdout line raising past STDOUT_LINE_LIMIT), the
            # detached process group would survive as an orphan and the next
            # deliver would --resume a *second* process on the same
            # transcript — so reap it here.
            if process.returncode is None:
                await self._terminate(live)
        # No hook of this process can wait on a block any more, and no
        # retry can use a grant it was given.
        self._drop_blocks(live)
        classifier_grants.clear_grants(self._artifact_root, self.account.key, key)
        # Last, and outside the registered-worker branch: whatever else
        # happened, this process's scope has to be read before it is
        # forgotten, or a cap kill goes unexplained (#1546).
        self.on_state_change(key)
        if live.holder_key:
            await self._forget_holder(key, live)
        await self._settle_scope(key, live)

    def _bypass_safety_refusal(self, key: str, event: dict[str, Any]) -> dict[str, Any] | None:
        """The answer to a Claude safety check in a chat whose bypass is on (#2342).

        Bypass does not lift the CLI's own safety checks (``rm`` on a variable
        that may be empty, for one): they still arrive as a ``can_use_tool``
        gate. The switch means nobody is asked, so the runtime refuses the call
        itself and hands Claude the check's reason, which says how to rewrite
        the command. Anything else, a question included, is left to the boss.
        """
        if event.get("type") != "control_request":
            return None
        request = event.get("request")
        request_id = event.get("request_id")
        if (
            not isinstance(request, dict)
            or request.get("subtype") != "can_use_tool"
            or request.get("decision_reason_type") != "safetyCheck"
            or not isinstance(request_id, str)
        ):
            return None
        rec = self._records.get(key)
        if rec is None or not (
            rec.bypass_permissions or self._base_permission_mode(rec) == BYPASS_PERMISSIONS_MODE
        ):
            return None
        reason = request.get("decision_reason")
        detail = reason.strip() if isinstance(reason, str) and reason.strip() else ""
        log.info("refused a safety check for %s:%s: %s", self.account.key, key, detail)
        message = (
            "Claude Code's safety check stopped this call. Permissions are bypassed "
            "in this chat, so nobody was asked and the call was refused."
            + (f" The check said: {detail}" if detail else "")
            + " Rewrite the command so the check no longer applies instead of "
            "retrying it unchanged."
        )
        return {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": "deny", "message": message},
            },
        }

    def _handle_event(self, key: str, live: _LiveWorker, event: dict[str, Any]) -> None:
        etype = event.get("type")
        rec = self._records.get(key)
        # A helper agent's own messages carry the id of the Agent call that started it.
        # A background one keeps writing after the chat's turn ended; those are not the
        # chat working again (#3529).
        if etype in ("assistant", "user") and not event.get("parent_tool_use_id"):
            # Claude can start another turn without a new stdin delivery. A
            # background shell task finishing is delivered back to the CLI as a
            # user message, and the CLI may immediately continue with assistant
            # output after the preceding turn already emitted ``result``. Treat
            # either message as the new turn's lifecycle edge; otherwise the
            # runtime advertises idle while Claude is executing that continuation
            # and an idle-only revival can be written into active work (#907).
            live.turn_active = True
            # A continuation turn opens without a delivery of ours, so the marker
            # has to be opened here too, or a chat the runtime kills while it is
            # working through a finished background task reads as idle (#1844).
            if rec is not None and not rec.turn_open_at:
                rec.turn_open_at = time.time()
                self._save_state()
        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if rec is not None and isinstance(session_id, str):
                rec.session_id = session_id
                self._publish_artifact_context(rec)
                self._save_state()
                if rec.session_token:
                    self._bind_session_token(
                        rec.session_token, f"{self.account.key}:{session_id}"
                    )
                if not live.initialized.done():
                    live.initialized.set_result(session_id)
        elif etype == "result":
            live.turn_active = False
            quota_evidence = _hard_quota_evidence(event)
            quota_reset_at = (
                account_health.structured_reset_at(event)
                or (
                    account_health.stated_reset_at(quota_evidence)
                    if quota_evidence is not None
                    else None
                )
            )
            if quota_evidence is not None:
                persisted = account_health.trip_hard_quota(
                    self._usage_cache_dir,
                    self.account.key,
                    reason="Claude account usage window exhausted",
                    evidence=quota_evidence,
                    clears_at=quota_reset_at,
                )
                if persisted:
                    log.warning("hard quota circuit opened for %s", self.account.key)
                else:
                    log.error("could not persist hard quota circuit for %s", self.account.key)
            # The turn is over (finished or interrupted). Any interaction the CLI
            # was blocked on is abandoned — drop it so the widget clears and a late
            # answer can't write a control_response for a request that's gone.
            live.pending_interaction = None
            if rec is not None:
                rec.last_result_at = time.time()
                subtype = event.get("subtype")
                rec.last_result_subtype = subtype if isinstance(subtype, str) else None
                rec.last_result_limit = quota_evidence or ""
                rec.last_result_limit_reset_at = (
                    quota_reset_at.timestamp() if quota_reset_at is not None else 0.0
                )
                retry_delay = _transient_failure_retry_delay(event)
                if not rec.last_result_limit_reset_at and retry_delay is not None:
                    # Not a quota, but the same remedy: the deck continues the chat once
                    # the network or the sign-in has had time to come back (#3086, #3165).
                    rec.last_result_limit_reset_at = rec.last_result_at + retry_delay
                if subtype != RESULT_ABORTED:
                    # The turn really ended, so this chat owes nothing. An aborted
                    # result is the CLI being torn down mid-turn: it ends the
                    # process, not the turn, so the marker stays open for revival
                    # to find after the restart (#1844).
                    rec.turn_open_at = 0.0
                    # The CLI empties its queue as soon as a turn ends, writing
                    # each message to the transcript as it takes it (#2898).
                    rec.unread_messages = []
                    rec.unread_dropped = 0
                self._save_state()
        elif etype == "control_request":
            # Inbound interactive decision (from --permission-prompt-tool stdio):
            # the agent is blocked until we write a matching control_response.
            request = event.get("request")
            request_id = event.get("request_id")
            if (
                isinstance(request, dict)
                and request.get("subtype") == "can_use_tool"
                and isinstance(request_id, str)
            ):
                live.pending_interaction = {"request_id": request_id, **request}
                if rec is not None and request.get("tool_name") == "AskUserQuestion":
                    self._open_questions(rec, request_id, request.get("input"))
        elif etype == "control_response":
            response = event.get("response")
            request_id = response.get("request_id") if isinstance(response, dict) else None
            waiter = self._control_waiters.get(request_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(response)
