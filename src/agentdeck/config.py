"""TOML config loading + validation.

Real config lives outside the repo (default ~/.config/agentdeck/config.toml,
overridable via the AGENTDECK_CONFIG env var). The config file contains no
secrets — credentials come from provider stores or separately permissioned files.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import default_db, worker_limits
from .github_webhook_log import KEEP_ROTATIONS, ROTATE_BYTES
from .models import Account

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.config/agentdeck/config.toml")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
class ServerConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = 8756


class PollingConfig(BaseModel):
    usage_interval_s: float = 300.0
    scan_interval_s: float = 15.0
    liveness_interval_s: float = 10.0
    # The topbar CPU/memory meter. 30 s since #2864: sampling every 3 s woke the deck for a
    # number nobody reads to the second.
    host_interval_s: float = 30.0
    # Chats kept live in the web process (#2841): those active within this many days,
    # plus any in a Work still going or opened in the last hour. 0 keeps every chat.
    live_window_days: float = 7.0


def _default_poller_root() -> str:
    """The poller root ``github_accounts.poller_root()`` reads, unexpanded (#2624).

    Read when a config is built, not at import, so the suite's per-test
    ``AGENTDECK_POLLER_ROOT`` keeps a default app off this host's real boards.
    """
    from .github_accounts import DEFAULT_POLLER_ROOT, POLLER_ROOT_ENV

    return os.environ.get(POLLER_ROOT_ENV) or DEFAULT_POLLER_ROOT


class PollerConfig(BaseModel):
    """Where this host keeps its Poller Instances (#178's layout).

    Observation is reading those files, so an operator surface needs only this one path — never a
    live channel to a poller (#107).
    """

    root: str = Field(default_factory=_default_poller_root)
    # The wake interval instance freshness is judged against: the poller timer's
    # `OnUnitInactiveSec` (`poller_engine.health.WAKE_INTERVAL_S`, 120 s since #3010).
    interval_s: int = 120
    # How often the deck refreshes the declared repositories' issue and pull
    # request lists into the shared GitHub cache. The board strip reads that
    # cache and never refreshes it, so without this loop the front page shows
    # whatever the last refresher left behind (#1115). Zero turns it off.
    issue_refresh_interval_s: float = 600.0


class UsageConfig(BaseModel):
    shared_cache_dir: str = ""  # default: $XDG_RUNTIME_DIR/agentdeck


class HistoryConfig(BaseModel):  # used from v0.2
    # Read when a config is built rather than when this module loads, so the test suite
    # can point every default config at a temporary file instead of the live deck's (#3153).
    db_path: str = Field(default_factory=lambda: default_db.DEFAULT_DB_PATH)
    usage_retention_days: int = 30

    @model_validator(mode="before")
    @classmethod
    def _refuse_history_off(cls, data: object) -> object:
        """The SQLite store is required (#2601); refuse the old switch that turned it off.

        ``enabled = true`` is what every existing config says, so it is accepted and
        dropped rather than breaking a deck that never asked for anything else.
        """
        if isinstance(data, dict) and "enabled" in data:
            if data["enabled"] is not True:
                raise ValueError(
                    "[history] enabled = false is no longer supported: AgentDeck requires"
                    " its SQLite store (#2601). Remove the setting."
                )
            data = {key: value for key, value in data.items() if key != "enabled"}
        return data


class ContextManagementConfig(BaseModel):
    """Policy limits for poller-owned context maintenance (#1471).

    ``off`` is deliberately the code default.  The runtime capability is also
    negotiated separately, so merely deploying this configuration cannot start
    maintenance against an older runtime.
    """

    mode: str = "off"
    compact_at_tokens: int = 90_000
    rollover_at_tokens: int = 120_000
    new_generation_rollover_at_tokens: int = 90_000
    compact_rearm_below_tokens: int = 60_000
    handoff_max_chars: int = 16_000
    handoff_recent_messages: int = 8
    max_previous_summary_chars: int = 6_000
    retention_days: int = 30

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, value: str) -> str:
        if value not in {"off", "observe", "enforce"}:
            raise ValueError("context_management.mode must be off, observe, or enforce")
        return value

    @field_validator(
        "compact_at_tokens",
        "rollover_at_tokens",
        "new_generation_rollover_at_tokens",
        "compact_rearm_below_tokens",
        "handoff_max_chars",
        "handoff_recent_messages",
        "max_previous_summary_chars",
        "retention_days",
    )
    @classmethod
    def _positive_values(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("context_management limits must be positive")
        return value

    @model_validator(mode="after")
    def _ordered_limits(self) -> ContextManagementConfig:
        if not (
            self.compact_rearm_below_tokens
            < self.compact_at_tokens
            < self.rollover_at_tokens
        ):
            raise ValueError(
                "context_management token limits must satisfy "
                "compact_rearm_below_tokens < compact_at_tokens < rollover_at_tokens"
            )
        if self.handoff_max_chars > 32_000:
            raise ValueError("context_management.handoff_max_chars must be <= 32000")
        if self.handoff_max_chars < 512:
            raise ValueError("context_management.handoff_max_chars must be >= 512")
        if self.handoff_recent_messages > 20:
            raise ValueError("context_management.handoff_recent_messages must be <= 20")
        from .providers.delivery import DELIVERY_RECEIPT_TTL_S

        if self.retention_days * 86_400 <= DELIVERY_RECEIPT_TTL_S:
            raise ValueError("context_management.retention_days must exceed receipt TTL")
        return self


DEFAULT_DELEGATION_TIMEOUT_S = 14_400.0


class InjectConfig(BaseModel):
    enabled: bool = False
    timeout_s: float = 900.0
    # A delegated cross-account subagent runs a whole task, not a message
    # round-trip, so it cannot share ``timeout_s``: the clock that should expire
    # in minutes when an injected message fails to land would otherwise cap a
    # legitimate multi-hour delegation and report the still-running child as
    # failed. Left unset, delegation waits use this generous default.
    delegation_timeout_s: float = DEFAULT_DELEGATION_TIMEOUT_S
    # Unset means no limit (#2677). Codex and Claude both read a message over their
    # own input channel, never a command line, so nothing here needs a cap; a
    # /dream reader's brief carries a whole group of transcripts (~400k characters).
    # Codex refuses input past its own maximum with its own error. Removed for every
    # message, not just delegations, on the owner's explicit decision on #2677 (a
    # delegation-only limit was offered and declined); set a value to cap again.
    max_message_chars: int | None = None
    # Kept under their original names for config compatibility. They cap images, which
    # reach the model inline, and every file a New work form commits to GitHub.
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_total_bytes: int = 20 * 1024 * 1024
    max_images: int = 4
    # Any other file attached in a chat is saved to disk and read by the agent from its
    # path, so it may be much bigger: a whole book as a PDF (#3374). The total covers
    # the message's images too.
    max_file_bytes: int = 100 * 1024 * 1024
    max_file_total_bytes: int = 200 * 1024 * 1024
    # When set, always prefills the New-chat "working directory" field with this
    # path (overriding the remembered last-used cwd). Empty = remember last-used.
    new_chat_default_cwd: str = ""

    @field_validator("timeout_s", "delegation_timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float, info) -> float:
        if value <= 0:
            raise ValueError(f"inject.{info.field_name} must be positive")
        return value

    @field_validator(
        "max_message_chars",
        "max_image_bytes",
        "max_image_total_bytes",
        "max_images",
        "max_file_bytes",
        "max_file_total_bytes",
    )
    @classmethod
    def _positive_limit(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("inject limits must be positive")
        return value

    def too_long(self, text: str) -> bool:
        """Whether `text` is over a configured message limit; no limit is never over."""
        return self.max_message_chars is not None and len(text) > self.max_message_chars


class AssistantConfig(BaseModel):
    enabled: bool = False
    # Read by nothing since Deckhand stopped making a periodic full pass (#2865); kept so
    # a config file that still sets it loads.
    refresh_interval_s: float = 60.0

    @field_validator("refresh_interval_s")
    @classmethod
    def _positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("assistant intervals must be positive")
        return value


class ClaudeWorkerOverrides(BaseModel):
    """Per-account overrides for [claude_workers]; unset fields inherit the base.

    Lets e.g. one account run with a different permission_mode than the base
    section's "auto".
    """

    max_workers: int | None = None
    permission_mode: str | None = None
    model: str | None = None
    usage_ceiling_pct: float | None = None
    stall_after_s: float | None = None
    idle_after_s: float | None = None
    memory_max: str | None = None
    interactive_prompts: bool | None = None
    start_bypassed: bool | None = None

    @field_validator("max_workers")
    @classmethod
    def _positive_max_workers(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("claude_workers accounts max_workers must be positive")
        return value

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100:
            raise ValueError(
                "claude_workers accounts usage_ceiling_pct must be between 0 and 100"
            )
        return value

    @field_validator("stall_after_s")
    @classmethod
    def _non_negative_stall(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("claude_workers accounts stall_after_s must be non-negative")
        return value

    @field_validator("idle_after_s")
    @classmethod
    def _non_negative_idle(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("claude_workers accounts idle_after_s must be non-negative")
        return value


class ClaudeWorkersConfig(BaseModel):
    """Deck-owned Claude worker processes (spawn/steer/revive via the runtime API)."""

    enabled: bool = False
    max_workers: int = 4  # per account; delivery to a live worker is exempt
    # Claude's own classifier reviews tool calls; a call it blocks is brought to
    # the boss as a pending prompt rather than refused silently (#2271). "" = CLI
    # default.
    permission_mode: str = "auto"
    # Start each new chat with its "Bypass permissions" switch on (#2319), which
    # overrides permission_mode until someone switches it off. Off by default, so a
    # chat runs in the mode named above (#3748); a saved chat keeps its own switch.
    start_bypassed: bool = False
    # Extra ``Bash(...)`` allow rules, on top of the built-in list in
    # ``providers/claude_code/permission_rules.py``, for commands that should skip
    # the classifier. One list for every account: the settings file they land in
    # is shared. A rule that would let ``gh``, ``curl``, ``git push`` or another
    # guarded command through is dropped with a warning.
    classifier_skip_rules: list[str] = []
    model: str = ""  # "" = account default model
    # Refuse (re)spawns at/above the fullest reported usage window %; 0 disables.
    usage_ceiling_pct: float = 0.0
    stall_after_s: float = (
        0.0  # flag a live worker stalled after this many silent seconds; 0 disables
    )
    # Collect a live worker that has been quiet this long, whatever its role; 0
    # disables. Unlike max_workers this is not admission pressure — nothing has to
    # want the slot — so it collects a worker nobody competes with (#1226).
    # The worker's durable record survives, so its chat revives on next delivery.
    idle_after_s: float = 6 * 60 * 60
    # Spawn workers with `--permission-prompt-tool stdio` so AskUserQuestion and
    # (on non-bypass accounts) permission gates surface to the user. Set false to
    # revert to the old auto-run spawn without a code change / service redeploy.
    interactive_prompts: bool = True
    # MemoryMax for each worker's own cgroup, in systemd's notation ("8G", "512M",
    # "infinity"); "" spawns uncapped as before (#1546). Generous but bounded: a
    # healthy worker rests at 200-400 MB, and one that reached 25 GB OOM-killed
    # the whole host. Hosts without a usable `systemd-run --user` ignore it.
    memory_max: str = worker_limits.DEFAULT_MEMORY_MAX
    # Run each worker's CLI under a small holder process that keeps its pipes, so a
    # runtime restart reconnects to the chat instead of cutting its turn (#3248). Needs
    # the worker's own scope (memory_max above); false spawns the CLI directly, as before.
    hold_across_restarts: bool = True
    state_dir: str = "~/.local/share/agentdeck/claude-workers"
    # Keyed by account label: [claude_workers.accounts.<label>]
    accounts: dict[str, ClaudeWorkerOverrides] = {}

    def for_account(self, label: str) -> ClaudeWorkersConfig:
        """Effective settings for one account (base + that account's overrides)."""
        override = self.accounts.get(label)
        if override is None:
            return self
        merged = {
            field: value
            for field, value in override.model_dump().items()
            if value is not None
        }
        return self.model_copy(update=merged)

    @field_validator("max_workers")
    @classmethod
    def _positive_max_workers(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("claude_workers.max_workers must be positive")
        return value

    @field_validator("idle_after_s")
    @classmethod
    def _non_negative_idle(cls, value: float) -> float:
        if value < 0:
            raise ValueError("claude_workers.idle_after_s must be non-negative")
        return value

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("claude_workers.usage_ceiling_pct must be between 0 and 100")
        return value

    @field_validator("stall_after_s")
    @classmethod
    def _non_negative_stall(cls, value: float) -> float:
        if value < 0:
            raise ValueError("claude_workers.stall_after_s must be non-negative")
        return value

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


class KeyedWorkerOverrides(BaseModel):
    usage_ceiling_pct: float | None = None

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100:
            raise ValueError("keyed_workers usage_ceiling_pct must be between 0 and 100")
        return value


class KeyedWorkersConfig(BaseModel):
    """Provider-neutral durable state for keyed worker adapters."""

    state_dir: str = "~/.local/share/agentdeck/keyed-workers"
    usage_ceiling_pct: float = 0.0
    # Opt-in: run each Codex app-server as a persistent `--listen` backend reached
    # through a `codex app-server proxy`, so in-flight interactions survive a runtime
    # restart. Off by default because that proxy/--listen path currently hangs on
    # `initialize` (codex-cli 0.144.3 & 0.147.0), crash-looping the runtime; the
    # default `--stdio` path is the one that works. See issue #992.
    persistent_backend: bool = False
    accounts: dict[str, KeyedWorkerOverrides] = {}

    def for_account(self, label: str) -> KeyedWorkersConfig:
        override = self.accounts.get(label)
        if override is None:
            return self
        return self.model_copy(
            update={
                key: value
                for key, value in override.model_dump().items()
                if value is not None
            }
        )

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("keyed_workers.usage_ceiling_pct must be between 0 and 100")
        return value

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


class RestartNudgeConfig(BaseModel):
    """Registry of chats to nudge after the next persistent-runtime restart.

    A restart of the runtime tears down every owned agent process group, so a
    chat that wants to keep working on the freshly deployed code registers
    itself here first. The file is durable (it must survive the restart it is
    about) and shared: agents write it through the web API or the
    ``agentdeck nudge-on-restart`` CLI, and the runtime consumes it on boot.
    Give the canary its own path — it shares real agent account directories, and
    consuming the live registry would nudge live chats.
    """

    file: str = "~/.local/share/agentdeck/restart-nudge.json"
    # An entry that has waited this long for a restart no longer describes work
    # worth resuming; it is dropped rather than delivered. Generous on purpose:
    # an unattended runtime restart is idle-gated and can stay pending for hours.
    ttl_s: float = 86_400.0

    @field_validator("ttl_s")
    @classmethod
    def _positive_ttl(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("restart_nudge.ttl_s must be positive")
        return value

    @property
    def path(self) -> Path:
        return Path(self.file).expanduser()


class JobsConfig(BaseModel):
    """The runtime's queue for heavy commands (#1287).

    An agent submits ``pytest`` here instead of running it, and at most
    ``slots`` of them run at once anywhere on the host. Give the canary its own
    directory: it shares the machine but not the queue, and reading the live
    one would let it report on jobs it never started. ``slots`` of 0 means the
    ``host_slots`` default, which is what the ``AGENTDECK_HEAVY_SLOTS``
    environment override also feeds — a direct ``pytest`` has no config file to
    read, so the two must agree without one.
    """

    enabled: bool = True
    state_dir: str = "~/.local/share/agentdeck/jobs"
    slots: int = 0

    @field_validator("slots")
    @classmethod
    def _non_negative_slots(cls, value: int) -> int:
        if value < 0:
            raise ValueError("jobs.slots must be non-negative")
        return value

    @property
    def path(self) -> Path:
        return Path(self.state_dir).expanduser()


class GitHubWebhookConfig(BaseModel):
    """The raw per-repository webhook streams on this host (#1601).

    ``state_dir`` holds one append-only ``<owner>/<repo>.jsonl`` per repository.
    Nothing consumes these yet; they exist so a later stage can. Give the canary
    its own directory — it shares the machine, and writing into the live streams
    would put deliveries the live deck never received into its record.

    The shared webhook secret is read from ``secret_env`` and never from this
    file, which is the same rule the rest of the config follows: no credentials
    in committed-adjacent configuration.

    ``rotate_bytes`` and ``keep_rotations`` bound what one repository can cost on
    disk (#1622): a stream that reaches ``rotate_bytes`` is gzipped aside as
    ``<repo>.jsonl.1.gz``, older archives shift down, and the last one is dropped.
    Set ``rotate_bytes`` to 0 to keep the pre-#1622 behaviour of never rotating,
    and ``keep_rotations`` to 0 to discard a full stream instead of archiving it.
    """

    enabled: bool = True
    state_dir: str = "~/.local/share/agentdeck/github-webhooks"
    secret_env: str = "AGENTDECK_WEBHOOK_SECRET"
    rotate_bytes: int = ROTATE_BYTES
    keep_rotations: int = KEEP_ROTATIONS

    @field_validator("rotate_bytes", "keep_rotations")
    @classmethod
    def _not_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("github_webhook sizes and counts must be non-negative")
        return value

    @property
    def path(self) -> Path:
        return Path(self.state_dir).expanduser()


class ChatBoardConfig(BaseModel):
    """The issue-as-chat board for one repository, and the nudges that work it (#1807).

    Every issue in ``repository`` is one ChatGPT web chat (#1804). A worker used to be a
    ChatGPT **scheduled task**, and #1992 replaced that: a worker is now one **new chat in
    ``project_url``**, started by sending ``.`` into it. The board is read from that
    repository's webhook stream, so it costs no GitHub quota beyond one read to build the
    store and one after a gap.

    ``project_url`` is the ChatGPT project whose own instructions carry the worker brief. The
    paths below are the same two facts the poller's own ``chatgpt-nudge`` extension needs: where to
    keep the press markers, and what program drives the browser. ``nudge_state_file`` is only
    an anchor — the markers and receipts are written beside it, and nothing here ever writes
    the file itself.

    Off unless a deck asks for it. This board is tied to one specific private
    repository, and the store's first build is a real ``gh`` call — a deck that
    has no review-chat repository should spend nothing on it. Empty by default (#3623): a
    deck that uses one names it here, and with none the board is off whatever ``enabled`` says.
    """

    enabled: bool = False
    repository: str = ""
    closed_tail: int = 10
    store_file: str = "~/.local/share/agentdeck/chat-board.json"
    project_url: str = ""
    nudge_state_file: str = "~/.local/share/agentdeck/chat-board-nudge.json"
    nudge_program: str = "~/agentdeck/scripts/chatgpt-nudge-run.sh"

    # The deck's own automatic pressing (`auto_nudge` and its two timings) was removed in
    # #2864; an old config that still sets them loads, because unknown keys are ignored.
    # When a claimed chat counts as stalled: `in-progress` and nothing has happened on
    # the issue for this long. Thirty minutes, which the requester set on 2026-09-24: every
    # review a first run finished that day took 15 minutes or less from claim to close, and
    # the workers' own PROTOCOL.md frees a claim after the same 30 silent minutes.
    auto_nudge_stalled_after_s: float = 30 * 60.0
    # The Claude fallback reviewer (#2736), used only while its switch is on. The checkout is
    # where the reviewer runs and reads ChatGPT's own instructions from; the account is empty
    # for the `agentdeck` Poller instance's worker account, which the owner asked for.
    review_fallback_checkout: str = ""
    review_fallback_account: str = ""
    # How many Claude reviewers the switch runs at once. Two at first (D5); raised to four by
    # the owner on 2026-10-08 when eight review chats queued behind two. A setting so the next
    # change needs no deploy. A person's Agent review press is never refused by it.
    review_fallback_max_reviewers: int = 4
    # The Codex reviewer beside Claude (#2766), sharing the waiting chats. The account is empty
    # for the deck's first Codex account; zero reviewers leaves Claude alone. Off since #3224:
    # one Codex review used millions of tokens, and two at once filled the Codex 5-hour limit
    # in minutes. `review-fallback only codex` still runs one.
    review_fallback_codex_account: str = ""
    review_fallback_codex_max_reviewers: int = 0

    @field_validator("closed_tail")
    @classmethod
    def _not_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("chat_board.closed_tail must be non-negative")
        return value

    @field_validator("review_fallback_max_reviewers")
    @classmethod
    def _one_reviewer_at_least(cls, value: int) -> int:
        if value < 1:
            raise ValueError("chat_board.review_fallback_max_reviewers must be at least one")
        return value

    @field_validator("review_fallback_codex_max_reviewers")
    @classmethod
    def _codex_reviewers_not_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("chat_board.review_fallback_codex_max_reviewers must be non-negative")
        return value

    @field_validator("auto_nudge_stalled_after_s")
    @classmethod
    def _above_zero(cls, value: float, info) -> float:
        if value <= 0:
            raise ValueError(f"chat_board.{info.field_name} must be greater than zero")
        return value

    @property
    def store_path(self) -> Path:
        return Path(self.store_file).expanduser()


class ReviewConfig(BaseModel):
    """Who reviews each new ready head of a Work's pull request (#3640).

    ``chatgpt`` is a ChatGPT review chat (``[chat_board]``); an edition without review chats
    reads it as ``off``, where a person's Merge press stands for the review. ``claude`` and
    ``codex`` start a deck-owned review chat of that provider in a throwaway checkout of the
    head (``agent_review``), on ``account`` (empty: the deck's first account of that provider),
    at most ``max_reviewers`` at once host-wide.
    """

    reviewer: Literal["chatgpt", "claude", "codex", "off"] = "chatgpt"
    account: str = ""
    max_reviewers: int = 2
    ledger_file: str = "~/.local/share/agentdeck/agent-reviews.json"

    @field_validator("max_reviewers")
    @classmethod
    def _one_at_least(cls, value: int) -> int:
        if value < 1:
            raise ValueError("review.max_reviewers must be at least one")
        return value

    @property
    def ledger_path(self) -> Path:
        return Path(self.ledger_file).expanduser()


class AccountConfig(BaseModel):
    provider: str
    label: str
    config_dir: str

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        from .providers import PROVIDERS  # local import: providers never import config

        if v not in PROVIDERS:
            raise ValueError(f"unknown provider {v!r}; known: {sorted(PROVIDERS)}")
        return v

    @field_validator("label")
    @classmethod
    def _slug_label(cls, v: str) -> str:
        if not _LABEL_RE.match(v):
            raise ValueError(
                f"label {v!r} must be a lowercase slug (it appears in URLs and cache filenames)"
            )
        return v

    @property
    def root(self) -> Path:
        return Path(self.config_dir).expanduser()


class PushConfig(BaseModel):
    """Web Push (PWA notifications, issue #7). The VAPID keypair is generated
    once and stored in the DB — never here — so this file stays secret-free.
    ``subject`` is the VAPID contact claim (a ``mailto:`` or ``https:`` URL that
    push services can reach you at), which is not a secret."""

    enabled: bool = False
    subject: str = ""


class AutoDreamConfig(BaseModel):
    """Run /dream on an account's leftover weekly usage before its week resets (#3336).

    Off unless a deck asks for it (#3623): it starts chats that spend the account's leftover
    week, which nobody installing the deck has agreed to yet.
    """

    enabled: bool = False


class SpeechConfig(BaseModel):
    """Voice mode's speech service on this host (#3409).

    ``socket`` is the Unix socket ``agentdeck-speech.socket`` listens on
    (``scripts/speech_setup.sh`` prints it). Empty, the default, leaves 🎤 dictation
    on the browser's own recognizer and shows no voice mode, which uses only this
    service (#3470).

    ``language`` is what the host listens for: ``"auto"``, the default since the
    Parakeet engine (#3509), hears English or Finnish and asks for the answer in the
    one heard. ``"fi"`` is for an owner who always speaks Finnish, mixing in
    English words: every message asks for a Finnish answer and the Finnish voice
    reads every reply, whatever its words look like (#3525). ``"en"`` transcribes
    every phrase as English and never asks for a Finnish answer, for Whisper
    ``small``, which took English speech for Finnish (#3417).

    ``idle_listen_seconds`` is how long voice mode listens with no words heard
    before it waits for "Hey Jarvis" (#3424, #3425, #3476).

    ``wake_socket`` is the wake word detector's socket (``agentdeck-wake.socket``,
    #3476): openWakeWord's "hey jarvis" on the CPU. ``wake`` says who decides a
    waiting phrase woke voice mode: ``"detector"`` asks it first and runs Whisper
    only on a phrase it woke on; ``"shadow"``, the default while the two are
    compared, lets Whisper decide as before and logs the detector's answer beside
    it in ``wake_log_dir`` (``scripts/wake_report.py`` reads it); ``"whisper"`` never
    asks the detector. With no ``wake_socket`` it is always ``"whisper"``.
    ``wake_threshold`` is the detector score that wakes.

    ``voice_en`` and ``voice_fi`` are the Piper voices the host reads replies with
    (#3455); ``scripts/speech_setup.sh`` downloads the ones named here, so a new
    voice needs that script run again and no code change. ``speed_en`` and
    ``speed_fi`` scale how fast Piper speaks each; the pauses between sentences do
    not shrink, so the Finnish default ``1.5`` reads a reply about a third faster
    than the voice's own pace (#3543).

    ``keep_audio_days`` keeps every transcribed phrase's audio and Whisper's words
    in ``keep_audio_dir`` for that many days, for ``scripts/speech_bench.py`` to
    compare speech-to-text models on (#3499). ``0``, the default, keeps nothing.

    ``engine`` and ``parakeet_model`` are read by the speech service itself when it
    starts, not by the deck (#3509): ``"parakeet"``, the default, runs the Parakeet
    TDT model in the folder ``parakeet_model`` (Orukeet, which
    ``scripts/speech_setup.sh`` downloads) on the CPU, writing each word in the
    language spoken; ``"whisper"`` is Whisper ``small`` on the graphics card, best
    with ``language = "en"``. A change takes effect when the
    service next starts, at most ``--idle-exit`` after its last phrase.

    ``tts`` is who reads replies aloud (#3519): ``"piper"``, the default, is Piper in
    the speech service; ``"chatterbox"`` asks Chatterbox Multilingual on the card
    (``scripts/tts_server.py`` behind ``tts_socket``) first, and Piper reads any
    sentence it fails on, is slow with or is still loading for. ``tts_exaggeration``
    and ``tts_cfg_en``/``tts_cfg_fi`` are Chatterbox's own settings for each language.
    ``tts_dtype`` is read by that service when it starts: ``"float16"`` when
    ``scripts/tts_bench.py speed`` finds float32 leaves under 300 MiB of the card
    free beside speech to text.
    """

    socket: str = ""
    language: Literal["en", "fi", "auto"] = "auto"
    idle_listen_seconds: int = Field(default=15, ge=3, le=600)
    wake_socket: str = ""
    wake: Literal["shadow", "detector", "whisper"] = "shadow"
    wake_threshold: float = Field(default=0.5, gt=0, lt=1)
    wake_log_dir: str = "~/.local/share/agentdeck/speech/wake"
    voice_en: str = Field(default="en_US-ryan-high", pattern=r"^[A-Za-z0-9_-]+$")
    voice_fi: str = Field(default="fi_FI-harri-medium", pattern=r"^[A-Za-z0-9_-]+$")
    speed_en: float = Field(default=1.0, ge=0.5, le=3)
    speed_fi: float = Field(default=1.5, ge=0.5, le=3)
    keep_audio_days: int = Field(default=0, ge=0, le=90)
    keep_audio_dir: str = "~/.local/share/agentdeck/speech/kept"
    engine: Literal["whisper", "parakeet"] = "parakeet"
    parakeet_model: str = "~/.local/share/agentdeck/speech/models/orukeet"
    tts: Literal["piper", "chatterbox"] = "piper"
    tts_socket: str = ""
    tts_exaggeration: float = Field(default=0.5, ge=0, le=2)
    tts_cfg_en: float = Field(default=0.5, ge=0, le=2)
    tts_cfg_fi: float = Field(default=0.5, ge=0, le=2)
    tts_dtype: Literal["float32", "float16"] = "float32"


class MetaChatConfig(BaseModel):
    """The deck chat: one always-on chat for steering the deck itself (#3403).

    ``account`` is an account label; unset means the first Claude account. After
    ``idle_reset_minutes`` without a message the next one starts the chat's next leg
    with a clean context.
    """

    account: str | None = None
    cwd: str = "~"
    idle_reset_minutes: int = 60

    @field_validator("idle_reset_minutes")
    @classmethod
    def _positive_minutes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("meta_chat.idle_reset_minutes must be positive")
        return value


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    polling: PollingConfig = PollingConfig()
    usage: UsageConfig = UsageConfig()
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    context_management: ContextManagementConfig = ContextManagementConfig()
    inject: InjectConfig = InjectConfig()
    assistant: AssistantConfig = AssistantConfig()
    claude_workers: ClaudeWorkersConfig = ClaudeWorkersConfig()
    keyed_workers: KeyedWorkersConfig = KeyedWorkersConfig()
    restart_nudge: RestartNudgeConfig = RestartNudgeConfig()
    jobs: JobsConfig = JobsConfig()
    push: PushConfig = PushConfig()
    poller: PollerConfig = Field(default_factory=PollerConfig)
    github_webhook: GitHubWebhookConfig = GitHubWebhookConfig()
    chat_board: ChatBoardConfig = ChatBoardConfig()
    review: ReviewConfig = ReviewConfig()
    auto_dream: AutoDreamConfig = AutoDreamConfig()
    meta_chat: MetaChatConfig = MetaChatConfig()
    speech: SpeechConfig = SpeechConfig()
    accounts: list[AccountConfig] = []

    @field_validator("accounts", mode="before")
    @classmethod
    def _drop_retired_providers(cls, value: object) -> object:
        """Skip accounts of a provider the deck no longer has, with a warning.

        Refusing them would stop both services from starting on a host whose
        config still lists one when the deploy that removed it lands (#2600).
        Any other unknown provider is still refused by ``AccountConfig``.
        """
        from .providers import RETIRED_PROVIDERS  # local import: providers never import config

        if not isinstance(value, list):
            return value
        kept = []
        for item in value:
            provider = item.get("provider") if isinstance(item, dict) else None
            if provider in RETIRED_PROVIDERS:
                log.warning(
                    "ignoring account %s:%s: the %s provider was removed (#2600); "
                    "delete it from the config",
                    provider,
                    item.get("label"),
                    provider,
                )
                continue
            kept.append(item)
        return kept

    @model_validator(mode="after")
    def _unique_labels(self) -> AppConfig:
        seen: set[tuple[str, str]] = set()
        for acc in self.accounts:
            k = (acc.provider, acc.label)
            if k in seen:
                raise ValueError(f"duplicate account label {acc.label!r} for {acc.provider}")
            seen.add(k)
        return self

    def usage_ceiling_pct(self, provider_id: str, label: str) -> float:
        """Read the retired ceiling setting for backward-compatible config tooling."""
        sections = {"claude_code": self.claude_workers}
        section = sections.get(provider_id, self.keyed_workers)
        return section.for_account(label).usage_ceiling_pct

    def native_model_override(self, provider_id: str, label: str) -> str | None:
        """The deck-configured default model for one account, or None.

        Same one-map shape as ``usage_ceiling_pct``: each provider's worker
        section owns its configured model, and every reader (currently the New
        chat defaults) resolves it here instead of naming a provider. Codex has
        no configured model section, so its answer is None — the account-native
        default applies.
        """
        sections = {"claude_code": self.claude_workers}
        section = sections.get(provider_id)
        return (section.for_account(label).model or None) if section is not None else None

    def build_accounts(self) -> list[Account]:
        return [
            Account(
                key=f"{acc.provider}:{acc.label}",
                provider_id=acc.provider,
                label=acc.label,
                root=acc.root,
            )
            for acc in self.accounts
        ]


def config_path() -> Path:
    env = os.environ.get("AGENTDECK_CONFIG")
    return Path(env).expanduser() if env else DEFAULT_CONFIG_PATH.expanduser()


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.model_validate(data)


def owned_session_base_url(config: AppConfig) -> str:
    """Loopback base URL an AgentDeck-owned session uses to reach the local HTTP
    API (reading its own transcript, pinning). Injected into owned sessions as
    ``AGENTDECK_URL`` so agents never have to guess a host or port. Honors an
    explicit ``AGENTDECK_URL`` in the runtime environment; otherwise targets the
    configured web port on loopback. This is a private-API base for the agent's
    own calls, not the public host to put in user-facing links."""
    override = os.environ.get("AGENTDECK_URL")
    if override and override.strip():
        return override.strip().rstrip("/")
    return f"http://127.0.0.1:{config.server.port}"
