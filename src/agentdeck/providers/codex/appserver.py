"""Persistent Codex app-server client for agentdeck-owned chats."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shlex
import shutil
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ... import gh_shim, github_identity, heavy_shim
from ...approval_cache import LEDGER_PATH_ENV
from ...chat_cwd import continued_chat_cwd, cwd_exists
from ...command_artifacts import (
    ACCOUNT_KEY_ENV,
    CONTEXT_DIR_ENV,
    hook_groups,
)
from ...models import (
    Account,
    ForkFrom,
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
)
from ..instructions import (
    SessionRole,
    agentdeck_session_instructions,
    relocation_notice,
    resolve_session_role,
    wrap_session_guide,
)
from . import (
    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
    LOGIN_SHELL_CONFIG_OVERRIDE,
    NETWORK_ACCESS_CONFIG_OVERRIDE,
    WEB_SEARCH_CONFIG_OVERRIDE,
    WRITABLE_ROOTS_CONFIG_OVERRIDE,
)
from .diagnostics import redact_diagnostic

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 30.0

# The app-server emits one JSON-RPC message per stdout line. asyncio's default
# StreamReader limit is 64 KiB, so a single large event (e.g. a big tool result)
# raises LimitOverrunError ("Separator is found, but chunk is longer than
# limit") and kills the whole turn, dropping Codex's final message. Raise the
# per-line buffer so realistic tool output does not break the transport.
STDOUT_LINE_LIMIT = 16 * 1024 * 1024

# stderr is diagnostic rather than protocol data. Drain it in small chunks so
# an unterminated message cannot block the child. Reassemble complete lines so
# secrets split across reads are still redacted; omit overlong lines while the
# bounded system journal retains and rotates the resulting records.
STDERR_CHUNK_LIMIT = 8 * 1024
STDERR_LINE_LIMIT = 8 * 1024
APP_SERVER_SOCKET_WAIT_S = 10.0
# One app-server hosts every owned thread of an account, and a loaded thread
# holds its lock and rollout files plus its MCP server processes open. Nothing
# unloads a thread a client still subscribes to, so a run of ~170 delegated
# readers exhausted the default 1024 open files and every command failed
# (#2643). Unsubscribe a thread idle this long; Codex unloads it about a minute
# later (`thread_unload_delay_secs`), and `_ensure_loaded` resumes it on demand.
THREAD_IDLE_UNLOAD_S = 600.0
# Headroom above the user manager's default soft limit; the sweep above is the fix.
APP_SERVER_NOFILE = 65536
COMMAND_ARTIFACT_HOOK_VERSION_ENV = "AGENTDECK_CODEX_COMMAND_HOOK_VERSION"
COMMAND_ARTIFACT_HOOK_VERSION = "command-result-v1"


def _hook_handler_toml(handler: Mapping[str, Any]) -> str:
    return (
        f"{{ type = {json.dumps(handler['type'])}, "
        f"command = {json.dumps(handler['command'])}, "
        f"timeout = {int(handler['timeout'])} }}"
    )


def _hook_group_toml(group: Mapping[str, Any]) -> str:
    handlers = ", ".join(_hook_handler_toml(handler) for handler in group["hooks"])
    return f"{{ matcher = {json.dumps(group['matcher'])}, hooks = [{handlers}] }}"


def _hook_config_overrides(states: Mapping[str, str] | None = None) -> tuple[str, ...]:
    # Rendered from hook_groups() rather than from one hardcoded handler, because
    # PostToolUse now carries both the capture hook and the approval cache's
    # observer (#1738) and a second `-c` for the same key would replace the first.
    events = tuple(
        f"hooks.{event}=[" + ", ".join(_hook_group_toml(group) for group in groups) + "]"
        for event, groups in hook_groups().items()
    )
    if not states:
        return events
    state_entries = ", ".join(
        f"{json.dumps(key)} = {{ trusted_hash = {json.dumps(value)} }}"
        for key, value in sorted(states.items())
    )
    return (*events, f"hooks.state={{ {state_entries} }}")


class AppServerError(RuntimeError):
    """The Codex app-server could not complete a request."""

    #: The JSON-RPC ``error`` object, when the app-server answered with one.
    rpc_error: object = None


@dataclass
class _PendingRequest:
    rpc_id: int | str
    method: str
    params: dict[str, Any]
    interaction: PendingInteraction


def _status_type(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"]
    return "notLoaded"


def _usage_limit_refusal(turn: dict[str, Any]) -> str:
    """The account's own refusal of this turn, or ``""`` for anything else (#1519).

    A `turn/completed` whose status is `failed` carries a `TurnError`: a `message` and a
    `codexErrorInfo` naming the cause. One cause means *this account cannot run a turn at
    all right now* — `usageLimitExceeded` — and that is a different fact from every other
    failure, because no work was attempted. The poller acts on it: the card moves account
    instead of spending one of its two restarts on an account that would refuse again.

    Only that one cause is reported. `contextWindowExceeded` and `sessionBudgetExceeded`
    are about the turn or the session, not the account, and a worker that hit one really
    did run. Anything this cannot read — a status that is not `failed`, no error, a cause
    it does not recognise, a shape a later Codex invents — answers `""`, which the poller
    reads as "not proven" and handles exactly as it did before this existed.

    Two spellings are accepted because there are two wires: the app-server's JSON-RPC uses
    `codexErrorInfo`/`usageLimitExceeded`, while the rollout the same turn writes to disk
    uses `codex_error_info`/`usage_limit_exceeded`.
    """
    if str(turn.get("status") or "") != "failed":
        return ""
    error = turn.get("error")
    if not isinstance(error, dict):
        return ""
    info = error.get("codexErrorInfo", error.get("codex_error_info"))
    if isinstance(info, dict):
        # The variants carrying detail are single-key objects keyed by the cause name.
        info = next(iter(info), "")
    if not isinstance(info, str) or info not in ("usageLimitExceeded", "usage_limit_exceeded"):
        return ""
    message = error.get("message")
    detail = f": {message}" if isinstance(message, str) and message else ""
    return f"usage_limit_exceeded{detail}"


def _turn_failure(turn: object, status: object) -> str:
    """Why a waited-for turn did not complete, naming a usage limit when Codex gave one.

    A turn the account's usage limit refused never ran, which a caller such as the upgrade
    check must not mistake for a broken CLI (#3210).
    """
    reason = f"Codex turn ended with status {status or 'unknown'}"
    refusal = _usage_limit_refusal(turn) if isinstance(turn, dict) else ""
    return f"{reason}: {refusal}" if refusal else reason


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in ("http", "https") and parsed.netloc else None


def _current_mcp_config(account_root: Path) -> dict[str, Any]:
    """Load MCP servers for a new thread from the account's current config.

    The app-server is intentionally long-lived, so its process-level Codex
    configuration can predate a newly configured MCP server.  A thread-level
    override makes fresh threads see the account's current servers without
    restarting the shared process and interrupting other chats.
    """
    path = account_root / "config.toml"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AppServerError(f"could not read current Codex config: {exc}") from exc
    servers = config.get("mcp_servers")
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise AppServerError("current Codex config has an invalid mcp_servers table")
    return {"mcp_servers": servers}


def _mcp_bearer_token_env_vars(account_root: Path) -> tuple[str, ...]:
    """Names of environment variables needed by configured HTTP MCP servers."""
    servers = _current_mcp_config(account_root).get("mcp_servers", {})
    names = {
        server.get("bearer_token_env_var")
        for server in servers.values()
        if isinstance(server, dict)
    }
    return tuple(sorted(name for name in names if isinstance(name, str)))


def _turn_input(message: str, images: list[Path] | None = None) -> list[dict[str, str]]:
    """Build app-server user input items."""
    items = [{"type": "text", "text": message}]
    items.extend({"type": "localImage", "path": str(path)} for path in images or [])
    return items


def _questions(params: dict[str, Any]) -> tuple[InteractionQuestion, ...]:
    result = []
    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list):
        return ()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_id = raw.get("id")
        prompt = raw.get("question")
        if not isinstance(question_id, str) or not isinstance(prompt, str):
            continue
        options = []
        for option in raw.get("options") or []:
            if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                continue
            options.append(
                InteractionOption(
                    option["label"],
                    option.get("description") if isinstance(option.get("description"), str) else "",
                )
            )
        result.append(
            InteractionQuestion(
                id=question_id,
                header=raw.get("header") if isinstance(raw.get("header"), str) else "Question",
                prompt=prompt,
                options=tuple(options),
                allow_other=bool(raw.get("isOther")),
                secret=bool(raw.get("isSecret")),
            )
        )
    return tuple(result)


def _mcp_questions(params: dict[str, Any]) -> tuple[InteractionQuestion, ...]:
    schema = params.get("requestedSchema")
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return ()
    required = set(schema.get("required") or [])
    result = []
    for name, raw in schema["properties"].items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        options: list[InteractionOption] = []
        labels = raw.get("enumNames")
        values = raw.get("enum")
        if isinstance(values, list):
            for index, value in enumerate(values):
                if not isinstance(value, str):
                    continue
                label = (
                    labels[index]
                    if isinstance(labels, list)
                    and index < len(labels)
                    and isinstance(labels[index], str)
                    else value
                )
                options.append(InteractionOption(label=label, value=value))
        for option in raw.get("oneOf") or []:
            if isinstance(option, dict) and isinstance(option.get("const"), str):
                options.append(
                    InteractionOption(
                        label=option.get("title")
                        if isinstance(option.get("title"), str)
                        else option["const"],
                        value=option["const"],
                    )
                )
        result.append(
            InteractionQuestion(
                id=name,
                header=raw.get("title") if isinstance(raw.get("title"), str) else name,
                prompt=raw.get("description")
                if isinstance(raw.get("description"), str)
                else name,
                options=tuple(options),
                allow_other=not options,
                secret=False,
            )
        )
        if name not in required and result[-1].prompt == name:
            result[-1] = InteractionQuestion(
                **{**result[-1].__dict__, "prompt": f"{name} (optional)"}
            )
    return tuple(result)


class CodexAppServer:
    """One long-lived, single-owner app-server process for a Codex account."""

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[str], None] | None = None,
        base_url: str | None = None,
        process_factory=None,
        persistent_backend: bool = True,
        backend_scope: str = "",
        permission_prompts_allowed: Callable[[str], bool] | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.account = account
        # Loopback base for AgentDeck's HTTP API, injected as AGENTDECK_URL so
        # threads hosted by this app-server can reach their own transcript
        # without guessing a host. One process hosts many threads, so a
        # per-session key cannot be injected here. None only in unexercised tests.
        self._base_url = base_url
        self._on_change = on_change or (lambda _thread_id: None)
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._persistent_backend = persistent_backend
        self._backend_scope = backend_scope
        self._permission_prompts_allowed = permission_prompts_allowed or (
            lambda _thread_id: False
        )
        self._artifact_root = artifact_root
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._backend_lifecycle_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._thread_operation_locks: dict[str, asyncio.Lock] = {}
        self._next_id = 1
        self._responses: dict[int, asyncio.Future] = {}
        self._owned: set[str] = set()
        self._loaded: set[str] = set()
        # When each loaded thread last became idle with a rollout on disk, on the
        # monotonic clock. A thread not yet given a turn has no rollout and could
        # not be resumed, so it is never here and never unloaded.
        self._idle_since: dict[str, float] = {}
        self._threads: dict[str, dict[str, Any]] = {}
        self._approval_policies: dict[str, str | dict[str, Any]] = {}
        self._active_turn: dict[str, str] = {}
        self._turn_started_waiters: dict[str, asyncio.Future] = {}
        self._turn_waiters: dict[str, asyncio.Future] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._last_results: dict[str, tuple[float, str, str]] = {}
        self._interactions: dict[str, _PendingRequest] = {}
        self._backend_env_stale = False
        self._artifact_hooks_trusted = False
        self._artifact_hooks_requested = False
        self._artifact_hook_states: dict[str, str] = {}

    async def start(self) -> None:
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            env = os.environ.copy()
            # An inherited AGENTDECK_SESSION_KEY would name whichever session
            # started this process, and one app-server hosts many threads, so
            # it is wrong for every one of them. Drop both and inject only the
            # base this server actually serves.
            env.pop("AGENTDECK_URL", None)
            env.pop("AGENTDECK_SESSION_KEY", None)
            env["CODEX_HOME"] = str(self.account.root)
            # One app-server hosts many threads, so account-level attribution is
            # the narrowest truthful consumer name available to the gh shim.
            env["AGENTDECK_GH_CONSUMER"] = self.account.key
            if self._artifact_root is not None:
                env[CONTEXT_DIR_ENV] = str(self._artifact_root)
                env[ACCOUNT_KEY_ENV] = self.account.key
                if self._artifact_hooks_requested:
                    env[COMMAND_ARTIFACT_HOOK_VERSION_ENV] = COMMAND_ARTIFACT_HOOK_VERSION
                    # One ledger per account: a grant is already keyed to a single
                    # thread, so sharing the file across an account's threads
                    # cannot let one worker reuse another's approval (#1738).
                    env[LEDGER_PATH_ENV] = str(
                        self._artifact_root / "approval-cache" / f"{self.account.key}.json"
                    )
            # Same caching, rate-limited `gh` the Claude workers get (#814):
            # every owned agent reads GitHub through one shared budget.
            env["PATH"] = gh_shim.worker_path(env)
            # Keep #1346's PATH/Python queue interception even though current
            # Codex also supports per-thread hooks: queue safety must not depend
            # on an optional artifact policy or one provider hook generation.
            env["PATH"] = heavy_shim.worker_path(env)
            # PATH order is not enough on its own: Codex runs commands in a
            # login shell, whose profile can put the real `pytest` back in
            # front of the shim. Every Python the worker starts imports this.
            env["PYTHONPATH"] = heavy_shim.worker_pythonpath(env)
            # Every GitHub transport goes through the owner rule — AgentDeck's
            # credential helper, never the host's `gh` login (#2289).
            github_identity.configure_github_git(env)
            if self._base_url:
                env["AGENTDECK_URL"] = self._base_url
            if self._persistent_backend:
                socket_path = await self._ensure_persistent_backend(env)
                command = (
                    "codex",
                    "app-server",
                    "proxy",
                    "--sock",
                    str(socket_path),
                )
            else:
                hook_config_args = (
                    sum(
                        (
                            ("--config", value)
                            for value in _hook_config_overrides(self._artifact_hook_states)
                        ),
                        (),
                    )
                    if self._artifact_root is not None and self._artifact_hooks_requested
                    else ()
                )
                command = (
                    "codex",
                    "app-server",
                    "--config",
                    WEB_SEARCH_CONFIG_OVERRIDE,
                    "--config",
                    NETWORK_ACCESS_CONFIG_OVERRIDE,
                    "--config",
                    WRITABLE_ROOTS_CONFIG_OVERRIDE,
                    "--config",
                    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
                    "--config",
                    LOGIN_SHELL_CONFIG_OVERRIDE,
                    *hook_config_args,
                    "--stdio",
                )
            try:
                self._process = await self._process_factory(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                    limit=STDOUT_LINE_LIMIT,
                )
            except (OSError, ValueError) as exc:
                self._process = None
                raise AppServerError(f"could not start Codex app-server: {exc}") from exc
            self._reader_task = asyncio.create_task(
                self._read_loop(), name=f"codex-app-server:{self.account.key}"
            )
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(), name=f"codex-app-server-stderr:{self.account.key}"
            )
            try:
                await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "agentdeck",
                            "title": "agentdeck",
                            "version": "0.3.1",
                        },
                        "capabilities": {
                            "experimentalApi": True,
                            "mcpServerOpenaiFormElicitation": True,
                        },
                    },
                )
                await self._notify("initialized", {})
                await self._recover_owned()
            except BaseException:
                await self.stop()
                raise

    def _backend_identity(self) -> tuple[str, Path]:
        identity = f"{self._backend_scope}\0{self.account.root.resolve()}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        unit = f"agentdeck-codex-app-server-{digest}.service"
        runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        return unit, runtime_dir / f"agentdeck-app-server-{digest}" / "control.sock"

    async def _ensure_persistent_backend(self, env: dict[str, str]) -> Path:
        """Start the account app-server outside AgentDeck's restartable cgroup."""
        unit, socket_path = self._backend_identity()
        active = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "is-active",
            "--quiet",
            unit,
        )
        if await active.wait() == 0:
            self._backend_env_stale = await self._persistent_backend_env_stale(
                env, unit, include_worker_paths=True
            ) or await self._persistent_backend_nofile_stale(unit) or (
                await self._persistent_backend_args_stale(unit)
            )
        else:
            self._backend_env_stale = False
            codex = shutil.which("codex", path=env.get("PATH")) or "codex"
            command = [
                "systemd-run",
                "--user",
                "--collect",
                f"--unit={unit}",
                "--property=Restart=on-failure",
                "--property=RestartSec=2",
                # The direct child path redacts Codex diagnostics before they
                # reach AgentDeck's journal. A detached service has no such
                # filter, so do not let its raw output leak credentials.
                "--property=StandardOutput=null",
                "--property=StandardError=null",
                f"--property=LimitNOFILE={APP_SERVER_NOFILE}",
                f"--property=RuntimeDirectory={socket_path.parent.name}",
                "--property=RuntimeDirectoryMode=0700",
                f"--setenv=CODEX_HOME={self.account.root}",
            ]
            for name in (
                "AGENTDECK_GH_CONSUMER",
                "AGENTDECK_URL",
                ACCOUNT_KEY_ENV,
                CONTEXT_DIR_ENV,
                COMMAND_ARTIFACT_HOOK_VERSION_ENV,
                LEDGER_PATH_ENV,
                "PATH",
                "PYTHONPATH",
                "GIT_CONFIG_COUNT",
            ):
                if name in env:
                    command.append(f"--setenv={name}={env[name]}")
            # systemd starts the persistent backend outside this process's
            # environment. Import only credentials explicitly named by the
            # account's MCP config, and keep their values off the command line.
            for name in _mcp_bearer_token_env_vars(self.account.root):
                if name in env:
                    command.append(f"--setenv={name}")
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            for index in range(count):
                for prefix in ("GIT_CONFIG_KEY", "GIT_CONFIG_VALUE"):
                    name = f"{prefix}_{index}"
                    if name in env:
                        command.append(f"--setenv={name}={env[name]}")
            hook_config_args = (
                sum(
                    (
                        ("--config", value)
                        for value in _hook_config_overrides(self._artifact_hook_states)
                    ),
                    (),
                )
                if self._artifact_root is not None and self._artifact_hooks_requested
                else ()
            )
            command.extend(
                [
                    codex,
                    "app-server",
                    "--config",
                    WEB_SEARCH_CONFIG_OVERRIDE,
                    "--config",
                    NETWORK_ACCESS_CONFIG_OVERRIDE,
                    "--config",
                    WRITABLE_ROOTS_CONFIG_OVERRIDE,
                    "--config",
                    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
                    "--config",
                    LOGIN_SHELL_CONFIG_OVERRIDE,
                    *hook_config_args,
                    "--listen",
                    f"unix://{socket_path}",
                ]
            )
            started = await asyncio.create_subprocess_exec(*command, env=env)
            if await started.wait() != 0:
                raise AppServerError(f"could not start persistent Codex app-server {unit}")

        deadline = asyncio.get_running_loop().time() + APP_SERVER_SOCKET_WAIT_S
        while not socket_path.exists():
            if asyncio.get_running_loop().time() >= deadline:
                raise AppServerError(f"persistent Codex app-server {unit} did not become ready")
            await asyncio.sleep(0.05)
        return socket_path

    async def _persistent_backend_env_stale(
        self,
        env: dict[str, str],
        unit: str,
        *,
        include_worker_paths: bool = False,
    ) -> bool:
        required = set(_mcp_bearer_token_env_vars(self.account.root)) & env.keys()
        if include_worker_paths:
            # A persistent app-server survives web deploys. Detect an older
            # backend that predates newly generated worker interception, then
            # replace it through the existing idle-only refresh path.
            required |= {
                ACCOUNT_KEY_ENV,
                CONTEXT_DIR_ENV,
                COMMAND_ARTIFACT_HOOK_VERSION_ENV,
                LEDGER_PATH_ENV,
                "PATH",
                "PYTHONPATH",
            } & env.keys()
        if not required:
            return False
        shown = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "show",
            "--property=Environment",
            "--value",
            unit,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await shown.communicate()
        if shown.returncode != 0:
            raise AppServerError(f"could not inspect persistent Codex app-server {unit}")
        inherited = {
            name: value
            for item in shlex.split(stdout.decode(errors="replace"))
            for name, separator, value in (item.partition("="),)
            if separator
        }
        return any(inherited.get(name) != env[name] for name in required)

    async def _persistent_backend_nofile_stale(self, unit: str) -> bool:
        """True when a running backend predates ``APP_SERVER_NOFILE`` (#2643).

        Read once when the client attaches: the limit cannot change while the
        backend runs, so the idle-only refresh replaces it at the next turn start.
        An unreadable answer is not a reason to replace a working backend.
        """
        shown = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "show",
            "--property=LimitNOFILESoft",
            "--value",
            unit,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await shown.communicate()
        if shown.returncode != 0:
            return False
        try:
            return int(stdout.decode(errors="replace").strip()) < APP_SERVER_NOFILE
        except ValueError:
            return False

    async def _persistent_backend_args_stale(self, unit: str) -> bool:
        """True when a running backend was started without the login-shell override (#3350).

        Like the file limit, a backend's command line cannot change while it runs, so the
        idle-only refresh replaces it. An unreadable answer keeps a working backend.
        """
        shown = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "show",
            "--property=ExecStart",
            "--value",
            unit,
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await shown.communicate()
        if shown.returncode != 0:
            return False
        started = stdout.decode(errors="replace")
        return "app-server" in started and LOGIN_SHELL_CONFIG_OVERRIDE not in started

    async def _refresh_persistent_backend_if_idle(self) -> bool:
        """Replace a credential-stale backend only after all its threads are idle."""
        if not self._persistent_backend:
            return True
        if not self._backend_env_stale:
            unit, _ = self._backend_identity()
            self._backend_env_stale = await self._persistent_backend_env_stale(
                os.environ, unit
            )
        if not self._backend_env_stale:
            return True
        if any(self.thread_status(thread_id) == "active" for thread_id in self._owned):
            return False
        await self.stop()
        unit, _ = self._backend_identity()
        stopped = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "stop", unit
        )
        if await stopped.wait() != 0:
            raise AppServerError(f"could not refresh persistent Codex app-server {unit}")
        self._backend_env_stale = False
        await self.start()
        return True

    async def stop(self) -> None:
        self._artifact_hooks_trusted = False
        process = self._process
        self._process = None
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        readers = (self._reader_task, self._stderr_task)
        self._reader_task = None
        self._stderr_task = None
        readers = tuple(
            reader
            for reader in readers
            if reader is not None and reader is not asyncio.current_task()
        )
        for reader in readers:
            reader.cancel()
        if readers:
            await asyncio.gather(*readers, return_exceptions=True)
        self._fail_pending(AppServerError("Codex app-server stopped"))

    def _fail_pending(self, exc: Exception) -> None:
        for future in (
            *self._responses.values(),
            *self._turn_started_waiters.values(),
            *self._turn_waiters.values(),
        ):
            if not future.done():
                future.set_exception(exc)
        self._responses.clear()
        self._turn_started_waiters.clear()
        self._turn_waiters.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError("Codex app-server is not running")
        async with self._write_lock:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()

    async def _request(
        self, method: str, params: dict[str, Any], *, timeout_s: float = REQUEST_TIMEOUT_S
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            result = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            raise AppServerError(f"Codex app-server {method} timed out") from exc
        finally:
            self._responses.pop(request_id, None)
        if not isinstance(result, dict):
            raise AppServerError(f"Codex app-server returned an invalid {method} result")
        return result

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id in self._responses and "method" not in message:
                    future = self._responses[request_id]
                    if "error" in message:
                        error = AppServerError(str(message["error"]))
                        error.rpc_error = message["error"]
                        future.set_exception(error)
                    else:
                        future.set_result(message.get("result"))
                    continue
                method = message.get("method")
                params = message.get("params")
                if isinstance(method, str) and isinstance(params, dict):
                    if request_id is not None:
                        self._server_request(request_id, method, params)
                    else:
                        self._notification(method, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- reader failure is reported to callers
            log.exception("Codex app-server reader failed for %s", self.account.key)
            self._fail_pending(AppServerError(str(exc)))
        finally:
            if self._process is process:
                self._process = None
                # Loaded is a property of this JSON-RPC connection. A replacement
                # app-server must resume an idle thread before accepting settings
                # updates or a new turn.
                self._loaded.clear()
                self._idle_since.clear()

    async def _stderr_loop(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        buffered = bytearray()
        dropped_bytes = 0
        try:
            while chunk := await process.stderr.read(STDERR_CHUNK_LIMIT):
                offset = 0
                while offset < len(chunk):
                    newline = chunk.find(b"\n", offset)
                    end = newline + 1 if newline >= 0 else len(chunk)
                    part = chunk[offset:end]
                    offset = end
                    if dropped_bytes:
                        dropped_bytes += len(part)
                        if newline >= 0:
                            log.warning(
                                "Codex app-server stderr for %s: omitted oversized "
                                "stderr line (%d bytes)",
                                self.account.key,
                                dropped_bytes,
                            )
                            dropped_bytes = 0
                        continue
                    if len(buffered) + len(part) > STDERR_LINE_LIMIT:
                        dropped_bytes = len(buffered) + len(part)
                        buffered.clear()
                        if newline >= 0:
                            log.warning(
                                "Codex app-server stderr for %s: omitted oversized "
                                "stderr line (%d bytes)",
                                self.account.key,
                                dropped_bytes,
                            )
                            dropped_bytes = 0
                        continue
                    buffered.extend(part)
                    if newline < 0:
                        continue
                    diagnostic = buffered.rstrip(b"\r\n").decode(
                        "utf-8", errors="replace"
                    )
                    buffered.clear()
                    if diagnostic:
                        log.warning(
                            "Codex app-server stderr for %s: %s",
                            self.account.key,
                            redact_diagnostic(diagnostic),
                        )
            if dropped_bytes:
                log.warning(
                    "Codex app-server stderr for %s: omitted oversized stderr line (%d bytes)",
                    self.account.key,
                    dropped_bytes,
                )
            elif buffered:
                diagnostic = buffered.decode("utf-8", errors="replace")
                if diagnostic:
                    log.warning(
                        "Codex app-server stderr for %s: %s",
                        self.account.key,
                        redact_diagnostic(diagnostic),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- diagnostics must not affect JSON-RPC
            log.exception(
                "Codex app-server stderr reader failed for %s", self.account.key
            )

    async def _list_persisted_threads(self) -> list[Any]:
        """Return every persisted thread, following ``nextCursor`` to the end.

        A single page used to be read, so a deck-owned chat older than the 200
        newest threads lost its send path after a restart (#2650).
        """
        threads: list[Any] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            # Codex currently persists threads started through app-server with
            # source kind ``vscode``. Include both persisted kinds; the list
            # response loses ``threadSource`` after restart, so ownership must
            # be recovered from AgentDeck's durable transcript originator.
            params: dict[str, Any] = {
                "sourceKinds": ["appServer", "vscode"],
                "limit": 200,
            }
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("thread/list", params)
            threads.extend(result.get("data") or [])
            cursor = result.get("nextCursor")
            # A repeated cursor would loop forever; stop rather than spin.
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return threads
            seen.add(cursor)

    async def _recover_owned(self) -> None:
        owned = []
        for thread in await self._list_persisted_threads():
            if not isinstance(thread, dict) or not self._was_started_by_agentdeck(thread):
                continue
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._owned.add(thread_id)
                self._threads[thread_id] = thread
                if _status_type(thread.get("status")) == "active":
                    owned.append(thread_id)
        # The persistent app-server replays unresolved approvals and questions
        # when a replacement AgentDeck client resumes the thread.
        for thread_id in owned:
            resumed = await self._request("thread/resume", self._resume_params(thread_id))
            thread = resumed.get("thread")
            if isinstance(thread, dict):
                self._threads[thread_id] = thread
            self._record_approval_policy(thread_id, resumed)
            self._loaded.add(thread_id)
            if thread_id not in self._active_turn:
                self._idle_since[thread_id] = time.monotonic()

    def _was_started_by_agentdeck(self, thread: dict[str, Any]) -> bool:
        """Check the persisted session marker without trusting arbitrary paths."""
        raw_path = thread.get("path")
        if not isinstance(raw_path, str):
            return False
        try:
            path = Path(raw_path).resolve()
            sessions_root = (self.account.root / "sessions").resolve()
            path.relative_to(sessions_root)
            with path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline())
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        payload = first.get("payload") if isinstance(first, dict) else None
        source = payload.get("source") if isinstance(payload, dict) else None
        return (
            first.get("type") == "session_meta"
            and isinstance(payload, dict)
            and payload.get("originator") == "agentdeck"
            and payload.get("thread_source") != "subagent"
            and not (isinstance(source, dict) and "subagent" in source)
        )

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                thread_id = thread["id"]
                self._threads[thread_id] = thread
                self._loaded.add(thread_id)
        elif method == "thread/status/changed" and isinstance(thread_id, str):
            self._threads.setdefault(thread_id, {})["status"] = params.get("status")
        elif method == "thread/settings/updated" and isinstance(thread_id, str):
            settings = params.get("threadSettings")
            if isinstance(settings, dict):
                self._record_approval_policy(thread_id, settings)
        elif method == "thread/closed" and isinstance(thread_id, str):
            self._loaded.discard(thread_id)
            self._idle_since.pop(thread_id, None)
        elif method == "turn/started" and isinstance(thread_id, str):
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                turn_id = turn["id"]
                self._active_turn[thread_id] = turn_id
                self._idle_since.pop(thread_id, None)
                waiter = self._turn_started_waiters.pop(thread_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(turn_id)
        elif method == "turn/completed" and isinstance(thread_id, str):
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                turn_id = turn["id"]
                self._active_turn.pop(thread_id, None)
                if thread_id in self._loaded:
                    self._idle_since[thread_id] = time.monotonic()
                self._last_results[thread_id] = (
                    time.time(),
                    str(turn.get("status") or "unknown"),
                    _usage_limit_refusal(turn),
                )
                waiter = self._turn_waiters.pop(turn_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(turn)
                else:
                    self._completed_turns[turn_id] = turn
        elif method == "serverRequest/resolved":
            resolved = params.get("requestId")
            for token, request in list(self._interactions.items()):
                if request.rpc_id == resolved:
                    del self._interactions[token]
        if isinstance(thread_id, str):
            self._on_change(thread_id)

    def _server_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or thread_id not in self._owned:
            return
        token = secrets.token_urlsafe(18)
        turn_id = params.get("turnId") if isinstance(params.get("turnId"), str) else None
        if method == "item/tool/requestUserInput":
            interaction = PendingInteraction(
                id=token,
                kind="question",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Codex needs your answer",
                questions=_questions(params),
            )
        elif method == "item/commandExecution/requestApproval":
            network = params.get("networkApprovalContext")
            network_message = None
            if isinstance(network, dict):
                protocol = network.get("protocol", "network")
                network_message = f"{protocol}://{network.get('host', '')}"
            interaction = PendingInteraction(
                id=token,
                kind="command_approval",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Approve command?",
                message=network_message
                or (params.get("reason") if isinstance(params.get("reason"), str) else None),
                command=params.get("command") if isinstance(params.get("command"), str) else None,
                cwd=str(params["cwd"]) if params.get("cwd") is not None else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "item/fileChange/requestApproval":
            interaction = PendingInteraction(
                id=token,
                kind="file_approval",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Approve file changes?",
                message=params.get("reason") if isinstance(params.get("reason"), str) else None,
                cwd=params.get("grantRoot")
                if isinstance(params.get("grantRoot"), str)
                else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "item/permissions/requestApproval":
            interaction = PendingInteraction(
                id=token,
                kind="permission",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Grant additional permissions?",
                message=params.get("reason") if isinstance(params.get("reason"), str) else None,
                cwd=params.get("cwd") if isinstance(params.get("cwd"), str) else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "mcpServer/elicitation/request":
            mode = params.get("mode")
            interaction = PendingInteraction(
                id=token,
                kind="mcp_url" if mode == "url" else "mcp_form",
                thread_id=thread_id,
                turn_id=turn_id,
                title=f"Input requested by {params.get('serverName', 'an app')}",
                message=params.get("message") if isinstance(params.get("message"), str) else None,
                questions=_mcp_questions(params),
                url=_safe_http_url(params.get("url")),
                decisions=("accept", "decline", "cancel"),
            )
        else:
            return
        if turn_id is not None:
            self._active_turn[thread_id] = turn_id
        self._interactions[token] = _PendingRequest(request_id, method, params, interaction)
        self._on_change(thread_id)

    def owns(self, thread_id: str) -> bool:
        return thread_id in self._owned

    def owned_threads(self) -> frozenset[str]:
        """Return a stable snapshot for the external runtime API."""
        return frozenset(self._owned)

    def adopt_owned(self, thread_ids: set[str]) -> None:
        """Recover persisted keyed threads that ``thread/list`` did not return.

        A durable worker record is a lead, not authorization by itself. Re-check the rollout's
        AgentDeck origin marker before adding it to the owned set.
        """
        sessions = self.account.root / "sessions"
        for thread_id in thread_ids - self._owned:
            matches = list(sessions.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
            if not matches:
                continue
            path = max(matches, key=lambda candidate: candidate.stat().st_mtime)
            thread = {"id": thread_id, "path": str(path)}
            if self._was_started_by_agentdeck(thread):
                self._owned.add(thread_id)
                self._threads.setdefault(thread_id, thread)

    def active_turn(self, thread_id: str) -> str | None:
        return self._active_turn.get(thread_id)

    def thread_status(self, thread_id: str) -> str:
        if thread_id in self._active_turn:
            return "active"
        return _status_type(self._threads.get(thread_id, {}).get("status"))

    def interaction(self, thread_id: str) -> PendingInteraction | None:
        for request in reversed(tuple(self._interactions.values())):
            if request.interaction.thread_id == thread_id:
                return request.interaction
        return None

    def _resume_params(self, thread_id: str) -> dict[str, Any]:
        params = {
            "threadId": thread_id,
            "approvalsReviewer": (
                "user" if self._permission_prompts_allowed(thread_id) else "auto_review"
            ),
        }
        if self._permission_prompts_allowed(thread_id):
            params["approvalPolicy"] = "on-request"
        return params

    def _record_approval_policy(self, thread_id: str, payload: dict[str, Any]) -> None:
        policy = payload.get("approvalPolicy")
        if isinstance(policy, (str, dict)):
            self._approval_policies[thread_id] = policy

    def approval_policy(self, thread_id: str) -> str | dict[str, Any] | None:
        return self._approval_policies.get(thread_id)

    async def current_approval_policy(
        self, thread_id: str
    ) -> str | dict[str, Any] | None:
        """Load an owned thread and return the policy that must be restored later."""
        await self.start()
        async with self._thread_operation_lock(thread_id):
            if thread_id not in self._owned:
                return None
            await self._ensure_loaded(thread_id)
            return self.approval_policy(thread_id)

    def last_result(self, thread_id: str) -> tuple[float, str, str] | None:
        """When this thread's last turn finished, its status, and any refusal it named."""
        return self._last_results.get(thread_id)

    async def create_thread(
        self,
        cwd: Path,
        *,
        sandbox: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
        delegated: bool = False,
        role: SessionRole | None = None,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> InjectResult:
        resolved_role = resolve_session_role(delegated=delegated, role=role)
        await self.start()
        async with self._backend_lifecycle_lock:
            if not await self._refresh_persistent_backend_if_idle():
                return InjectResult(
                    False,
                    "Codex app-server environment changed while an existing thread is active; "
                    "retry after it becomes idle",
                )
            thread_config = _current_mcp_config(self.account.root)
            params = {
                "cwd": str(cwd),
                "developerInstructions": agentdeck_session_instructions(
                    role=resolved_role,
                    provider="codex",
                    session_token=session_token,
                ),
                "ephemeral": False,
                "threadSource": "agentdeck",
            }
            if thread_config:
                params["config"] = thread_config
            if sandbox is not None:
                params["sandbox"] = sandbox
            if model is not None:
                params["model"] = model
            if approval_policy is not None:
                params["approvalPolicy"] = approval_policy
            if fork_from is not None:
                # A side chat (#2485): a new thread holding the source's turns up to and
                # including the chosen one. Its rollout records only its own turns and
                # names the source in `forked_from_id`.
                params.update(
                    threadId=fork_from.session_id,
                    lastTurnId=fork_from.ref,
                    excludeTurns=True,
                )
            result = await self._request(
                "thread/fork" if fork_from is not None else "thread/start",
                params,
            )
            thread = result.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                return InjectResult(False, "Codex did not return a new thread id")
            thread_id = thread["id"]
            self._owned.add(thread_id)
            self._loaded.add(thread_id)
            self._threads[thread_id] = thread
            self._record_approval_policy(thread_id, result)
            self._on_change(thread_id)
            return InjectResult(True, session_id=thread_id)

    async def start_thread(
        self,
        cwd: Path,
        message: str,
        *,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        delegated: bool = False,
        role: SessionRole | None = None,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> InjectResult:
        try:
            created = await self.create_thread(
                cwd,
                sandbox=sandbox,
                model=model,
                approval_policy=approval_policy,
                delegated=delegated,
                role=role,
                session_token=session_token,
                fork_from=fork_from,
            )
        except AppServerError as exc:
            if fork_from is None:
                raise
            # Nothing was written: the fork itself was refused, so say why.
            rpc_error = getattr(exc, "rpc_error", None)
            detail = (
                rpc_error.get("message")
                if isinstance(rpc_error, dict) and isinstance(rpc_error.get("message"), str)
                else str(exc)
            )
            return InjectResult(False, f"Codex could not make the side chat: {detail}")
        if not created.accepted or not created.session_id:
            return created
        thread_id = created.session_id
        initial = await self.start_turn(
            thread_id, message, images=images, effort=effort, wait=False
        )
        if not initial.accepted:
            return initial
        return InjectResult(True, session_id=thread_id)

    def _relocation(self, thread_id: str) -> tuple[str, str] | None:
        """``(gone directory, repository)`` when this thread's directory is gone (#2524).

        The thread resumes in the repository that held its deleted worktree, as the
        same conversation; ``None`` keeps it where it is.
        """
        cwd = self._threads.get(thread_id, {}).get("cwd")
        if not isinstance(cwd, str) or not cwd or cwd_exists(Path(cwd)):
            return None
        target = continued_chat_cwd(Path(cwd))
        return (cwd, str(target)) if target is not None else None

    async def _ensure_loaded(self, thread_id: str, *, cwd: str | None = None) -> None:
        if thread_id in self._loaded:
            return
        params = self._resume_params(thread_id)
        if cwd is not None:
            params["cwd"] = cwd
        resumed = await self._request("thread/resume", params)
        self._record_approval_policy(thread_id, resumed)
        self._loaded.add(thread_id)
        if thread_id not in self._active_turn:
            self._idle_since[thread_id] = time.monotonic()

    def _unloadable(self, thread_id: str) -> bool:
        return (
            thread_id in self._owned
            and thread_id in self._loaded
            and self.thread_status(thread_id) != "active"
            and self.interaction(thread_id) is None
        )

    async def unload_idle(
        self, *, idle_for: float = THREAD_IDLE_UNLOAD_S, now: float | None = None
    ) -> list[str]:
        """Unsubscribe owned threads idle for ``idle_for`` seconds (#2643).

        Codex unloads a thread nobody subscribes to, releasing its files and MCP
        servers. A thread waiting on a person, mid-turn, or mid-operation is
        skipped; the next turn's ``_ensure_loaded`` resumes an unloaded one.
        """
        process = self._process
        if process is None or process.returncode is not None:
            return []
        now = time.monotonic() if now is None else now
        unloaded = []
        for thread_id, since in list(self._idle_since.items()):
            if now - since < idle_for or not self._unloadable(thread_id):
                continue
            lock = self._thread_operation_lock(thread_id)
            if lock.locked():
                continue
            async with lock:
                since = self._idle_since.get(thread_id)
                if since is None or now - since < idle_for or not self._unloadable(thread_id):
                    continue
                try:
                    await self._request("thread/unsubscribe", {"threadId": thread_id})
                except AppServerError as exc:
                    log.warning("could not unload idle Codex thread %s: %s", thread_id, exc)
                    continue
                self._loaded.discard(thread_id)
                self._idle_since.pop(thread_id, None)
                unloaded.append(thread_id)
        if unloaded:
            log.info(
                "unloaded %d idle Codex thread(s) for %s: %s",
                len(unloaded),
                self.account.key,
                ", ".join(unloaded),
            )
        return unloaded

    async def set_permission_prompts(
        self,
        thread_id: str,
        enabled: bool,
        *,
        previous_policy: str | dict[str, Any] | None = None,
    ) -> InjectResult:
        """Route later approval requests to the boss or back to auto-review."""
        await self.start()
        async with self._thread_operation_lock(thread_id):
            if thread_id not in self._owned:
                return InjectResult(False, "agentdeck does not own this Codex thread")
            await self._ensure_loaded(thread_id)
            params: dict[str, Any] = {
                "threadId": thread_id,
                "approvalsReviewer": "user" if enabled else "auto_review",
            }
            if enabled:
                params["approvalPolicy"] = "on-request"
            elif previous_policy is not None:
                params["approvalPolicy"] = previous_policy
            await self._request(
                "thread/settings/update",
                params,
            )
            if enabled:
                self._approval_policies[thread_id] = "on-request"
            elif previous_policy is not None:
                # Nothing to put back means no policy was sent, so leave the
                # recorded one alone rather than claim a value we never asserted.
                self._approval_policies[thread_id] = previous_policy
        self._on_change(thread_id)
        return InjectResult(True)

    async def set_command_artifacts(
        self,
        thread_id: str,
        *,
        worker_key: str,
        session_token: str,
        artifact_root: Path,
        artifact_policy: dict[str, Any],
    ) -> InjectResult:
        """Trust only AgentDeck's process-level generated hooks before the next turn."""
        if thread_id not in self._owned:
            return InjectResult(False, "agentdeck does not own this Codex thread")
        if str(artifact_policy.get("mode") or "off") == "off":
            return InjectResult(True, session_id=thread_id)
        if self._artifact_root is None:
            return InjectResult(False, "command artifact storage is unavailable")
        if artifact_root.resolve() != self._artifact_root.resolve():
            return InjectResult(False, "command artifact storage identity changed")
        prepared = await self._ensure_command_artifact_hooks()
        if not prepared.accepted:
            return prepared
        return InjectResult(True, session_id=thread_id)

    @staticmethod
    def _expected_generated_hooks() -> frozenset[tuple[str, str]]:
        """The (event, command) pairs AgentDeck asked Codex to generate.

        Derived from ``hook_groups()`` so the handshake can never drift from what
        was actually installed. Event names come back from Codex camel-cased.
        """
        return frozenset(
            (f"{event[0].lower()}{event[1:]}", str(handler["command"]))
            for event, groups in hook_groups().items()
            for group in groups
            for handler in group["hooks"]
        )

    @classmethod
    def _generated_artifact_hooks(cls, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Index the generated hooks Codex reports, keyed by event *and* command.

        Keyed by both because one event now carries two distinct handlers, and
        keying on the event alone would let one of them stand in for the other.
        """
        expected = cls._expected_generated_hooks()
        found: dict[str, dict[str, Any]] = {}
        for entry in payload.get("data", []):
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                identity = (str(hook.get("eventName")), str(hook.get("command")))
                if (
                    identity in expected
                    and hook.get("source") == "sessionFlags"
                    and hook.get("handlerType") == "command"
                    and hook.get("matcher") == "Bash"
                    and hook.get("enabled") is True
                ):
                    found[f"{identity[0]}:{identity[1]}"] = hook
        return found

    async def _ensure_command_artifact_hooks(self) -> InjectResult:
        if self._artifact_root is None:
            return InjectResult(False, "command artifact storage is unavailable")
        async with self._backend_lifecycle_lock:
            if self._artifact_hooks_trusted:
                return InjectResult(True)
            if any(self.thread_status(thread_id) == "active" for thread_id in self._owned):
                return InjectResult(
                    False,
                    "generated Codex capture hooks require all account threads to be idle",
                )
            if not self._artifact_hooks_requested:
                self._artifact_hooks_requested = True
                if self._process is not None and self._process.returncode is None:
                    if self._persistent_backend:
                        self._backend_env_stale = True
                        if not await self._refresh_persistent_backend_if_idle():
                            self._artifact_hooks_requested = False
                            return InjectResult(
                                False,
                                "generated Codex capture hooks require all account "
                                "threads to be idle",
                            )
                    else:
                        await self.stop()
                        await self.start()
            await self.start()
            hooks = self._generated_artifact_hooks(
                await self._request("hooks/list", {"cwds": []})
            )
            if set(hooks) != {
                f"{event}:{command}" for event, command in self._expected_generated_hooks()
            }:
                return InjectResult(False, "generated Codex capture hooks are unavailable")
            states = {
                str(hook["key"]): str(hook["currentHash"])
                for hook in hooks.values()
                if hook.get("key") and hook.get("currentHash")
            }
            if len(states) != len(hooks):
                return InjectResult(False, "generated Codex capture hooks are invalid")
            self._artifact_hook_states = states
            if self._persistent_backend:
                self._backend_env_stale = True
                if not await self._refresh_persistent_backend_if_idle():
                    return InjectResult(
                        False,
                        "generated Codex capture hooks require all account threads to be idle",
                    )
            else:
                await self.stop()
                await self.start()
            trusted = self._generated_artifact_hooks(
                await self._request("hooks/list", {"cwds": []})
            )
            if set(trusted) != set(hooks) or any(
                hook.get("trustStatus") != "trusted"
                or states.get(str(hook.get("key"))) != hook.get("currentHash")
                for hook in trusted.values()
            ):
                return InjectResult(False, "generated Codex capture hooks are not trusted")
            self._artifact_hooks_trusted = True
            return InjectResult(True)

    def _thread_operation_lock(self, thread_id: str) -> asyncio.Lock:
        return self._thread_operation_locks.setdefault(thread_id, asyncio.Lock())

    async def _start_turn_unlocked(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
        effort: str | None = None,
        wait: bool = True,
        on_started: Callable[[str], None] | None = None,
    ) -> InjectResult:
        async with self._backend_lifecycle_lock:
            if not await self._refresh_persistent_backend_if_idle():
                return InjectResult(
                    False,
                    "Codex app-server environment changed while an existing thread is active; "
                    "retry after it becomes idle",
                )
            if thread_id not in self._owned:
                return InjectResult(False, "agentdeck does not own this Codex thread")
            relocated = (
                self._relocation(thread_id) if self.active_turn(thread_id) is None else None
            )
            await self._ensure_loaded(
                thread_id, cwd=relocated[1] if relocated is not None else None
            )
            if self.active_turn(thread_id) is not None:
                return InjectResult(False, "Codex is already working on this thread")
            if relocated is not None:
                # Told once, in the guide block the transcript reader strips, so the
                # turn still reads as the person's own message (#2524).
                message = wrap_session_guide(relocation_notice(*relocated), message)
            params = {"threadId": thread_id, "input": _turn_input(message, images)}
            if relocated is not None:
                params["cwd"] = relocated[1]
            if effort is not None:
                params["effort"] = effort
            result = await self._request("turn/start", params)
            if relocated is not None:
                log.info(
                    "codex thread %s: %s is gone; resumed in %s",
                    thread_id,
                    *relocated,
                )
                self._threads.setdefault(thread_id, {})["cwd"] = relocated[1]
            turn = result.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                # The request already crossed the app-server boundary. A malformed response
                # does not prove the message was rejected; treating it as cleanly retryable
                # could run the same instruction twice.
                raise AppServerError("Codex turn start response was ambiguous")
            turn_id = turn["id"]
            self._active_turn[thread_id] = turn_id
            if on_started is not None:
                on_started(turn_id)
            self._on_change(thread_id)
        if not wait:
            return InjectResult(True)
        completed = await self._wait_for_turn(turn_id)
        status = completed.get("status") if isinstance(completed, dict) else None
        if status in ("completed", "interrupted"):
            reason = None if status == "completed" else "turn interrupted"
            return InjectResult(status == "completed", reason)
        return InjectResult(False, _turn_failure(completed, status))

    async def start_turn(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
        effort: str | None = None,
        wait: bool = True,
        on_started: Callable[[str], None] | None = None,
    ) -> InjectResult:
        await self.start()
        async with self._thread_operation_lock(thread_id):
            return await self._start_turn_unlocked(
                thread_id,
                message,
                images=images,
                effort=effort,
                wait=wait,
                on_started=on_started,
            )

    async def _wait_for_turn(self, turn_id: str) -> dict[str, Any]:
        completed = self._completed_turns.pop(turn_id, None)
        if completed is None:
            future = self._turn_waiters.get(turn_id)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._turn_waiters[turn_id] = future
            completed = await future
        return completed

    async def queue_turn(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
        effort: str | None = None,
        maintain_context: bool = False,
        after_maintenance: Callable[[str, str | None], None] | None = None,
        before_start: Callable[[], None] | None = None,
        on_started: Callable[[str], None] | None = None,
        require_idle: bool = False,
    ) -> InjectResult:
        """Wait for the active turn, then start one ordinary follow-up turn."""
        await self.start()
        async with self._thread_operation_lock(thread_id):
            active = self.active_turn(thread_id)
            if active is not None:
                if require_idle:
                    return InjectResult(False, "active_turn")
                await self._wait_for_turn(active)
            if maintain_context:
                maintained = await self._compact_unlocked(thread_id)
                if after_maintenance is not None:
                    after_maintenance(
                        "succeeded" if maintained.accepted else "failed",
                        maintained.reason,
                    )
            if before_start is not None:
                before_start()
            # The message is sent once turn/start is accepted. Do not make the web
            # queue say "Sending" throughout the assistant's entire response.
            return await self._start_turn_unlocked(
                thread_id,
                message,
                images=images,
                effort=effort,
                wait=False,
                on_started=on_started,
            )

    async def _compact_unlocked(self, thread_id: str) -> InjectResult:
        if thread_id not in self._owned:
            return InjectResult(False, "agentdeck does not own this Codex thread")
        active = self.active_turn(thread_id)
        if active is not None:
            await self._wait_for_turn(active)
        async with self._backend_lifecycle_lock:
            await self._ensure_loaded(thread_id)
            started = asyncio.get_running_loop().create_future()
            self._turn_started_waiters[thread_id] = started
            try:
                await self._request("thread/compact/start", {"threadId": thread_id})
                try:
                    turn_id = await asyncio.wait_for(started, timeout=REQUEST_TIMEOUT_S)
                except TimeoutError as exc:
                    raise AppServerError("Codex compaction did not start") from exc
            finally:
                if self._turn_started_waiters.get(thread_id) is started:
                    self._turn_started_waiters.pop(thread_id, None)

        completed = await self._wait_for_turn(turn_id)
        status = completed.get("status") if isinstance(completed, dict) else None
        if status == "completed":
            return InjectResult(True)
        if status == "interrupted":
            return InjectResult(False, "compaction interrupted")
        return InjectResult(False, f"Codex compaction ended with status {status or 'unknown'}")

    async def compact(self, thread_id: str) -> InjectResult:
        """Wait for existing work, compact the thread, and wait for completion."""
        await self.start()
        async with self._thread_operation_lock(thread_id):
            return await self._compact_unlocked(thread_id)

    async def wait_for_thread(self, thread_id: str) -> InjectResult:
        turn_id = self.active_turn(thread_id)
        if turn_id is None:
            return InjectResult(True)
        completed = await self._wait_for_turn(turn_id)
        status = completed.get("status") if isinstance(completed, dict) else None
        if status == "completed":
            return InjectResult(True)
        if status == "interrupted":
            return InjectResult(False, "turn interrupted")
        return InjectResult(False, _turn_failure(completed, status))

    async def steer(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        await self.start()
        target_turn_id = self.active_turn(thread_id)
        async with self._thread_operation_lock(thread_id):
            turn_id = self.active_turn(thread_id)
            if (
                thread_id not in self._owned
                or target_turn_id is None
                or turn_id != target_turn_id
            ):
                return InjectResult(False, "there is no agentdeck-owned active turn")
            await self._request(
                "turn/steer",
                {
                    "threadId": thread_id,
                    "expectedTurnId": turn_id,
                    "input": _turn_input(message, images),
                },
            )
            return InjectResult(True)

    async def interrupt(self, thread_id: str) -> InjectResult:
        await self.start()
        turn_id = self.active_turn(thread_id)
        if thread_id not in self._owned or turn_id is None:
            return InjectResult(False, "there is no agentdeck-owned active turn")
        await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return InjectResult(True)

    async def answer(
        self,
        thread_id: str,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None = None,
    ) -> InjectResult:
        request = self._interactions.get(interaction_id)
        if request is None or request.interaction.thread_id != thread_id:
            return InjectResult(False, "this interaction is no longer pending")
        method = request.method
        if method == "item/tool/requestUserInput":
            result: dict[str, Any] = {
                "answers": {key: {"answers": values} for key, values in answers.items()}
            }
        elif method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            allowed = request.interaction.decisions
            if decision not in allowed:
                return InjectResult(False, "invalid approval decision")
            result = {"decision": decision}
        elif method == "item/permissions/requestApproval":
            allowed = request.interaction.decisions
            if decision not in allowed:
                return InjectResult(False, "invalid permission decision")
            result = {
                "permissions": request.params.get("permissions", {})
                if decision in ("accept", "acceptForSession")
                else {},
                "scope": "session" if decision == "acceptForSession" else "turn",
            }
        elif method == "mcpServer/elicitation/request":
            if decision not in request.interaction.decisions:
                return InjectResult(False, "invalid elicitation decision")
            content = {
                key: values[0] if len(values) == 1 else values for key, values in answers.items()
            }
            result = {
                "action": decision,
                "content": content if decision == "accept" else None,
            }
        else:
            return InjectResult(False, "unsupported interaction")
        await self._write({"id": request.rpc_id, "result": result})
        del self._interactions[interaction_id]
        self._on_change(thread_id)
        if decision == "cancel" and method == "item/permissions/requestApproval":
            await self.interrupt(thread_id)
        return InjectResult(True)
