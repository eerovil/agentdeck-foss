"""Private, bounded command-result artifacts for poller-owned workers (#1484).

The provider hook and capture launcher import this module directly, so it stays
stdlib-only.  Raw command bytes never become an identifier or a public path:
each capture gets a random id, restrictive files, and a separately checked
session/card provenance record.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CONTEXT_DIR_ENV = "AGENTDECK_COMMAND_ARTIFACT_ROOT"
ACCOUNT_KEY_ENV = "AGENTDECK_ACCOUNT_KEY"
WORKER_KEY_ENV = "AGENTDECK_WORKER_KEY"
SESSION_TOKEN_ENV = "AGENTDECK_SESSION_TOKEN"
POLICY_MODE_ENV = "AGENTDECK_COMMAND_ARTIFACT_MODE"
TRUNCATION_VERSION = "command-result-v1"
DEFAULT_SLICE_MAX_BYTES = 64 * 1024
DEFAULT_SLICE_CALL_LIMIT = 8
UNCOVERED_LEDGER_MAX_BYTES = 1024 * 1024
UNCOVERED_SUMMARY_LIMIT = 20
_SAFE_TOOL_NAMES = frozenset({"Bash", "bash", "shell", "shell_command", "unified_exec"})
_UNSAFE_INPUT_FIELDS = frozenset(
    {
        "tty",
        "session_id",
        "stream_stdin",
        "streamStdoutStderr",
        "shell",
        "login",
    }
)
_FAILURE = re.compile(
    rb"(?i)(?:\berror\b|\bfail(?:ed|ure)?\b|\bfatal\b|exception|traceback|denied|refused)"
)
_SECRET_PATTERNS: tuple[tuple[re.Pattern[bytes], bytes], ...] = (
    (re.compile(rb"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s]+"), rb"\1[REDACTED]"),
    (
        re.compile(
            rb'(?i)("(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|'
            rb'session[_-]?token|password|secret(?:[_-]?access[_-]?key)?|token)"\s*:\s*")'
            rb'[^"\\]*(?:\\.[^"\\]*)*(")'
        ),
        rb"\1[REDACTED]\2",
    ),
    (
        re.compile(
            rb"(?i)\b((?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
            rb"session[_-]?token|password|secret(?:[_-]?access[_-]?key)?|token))"
            rb"(\s*[:=]\s*)[^\s,;]+"
        ),
        rb"\1\2[REDACTED]",
    ),
    (re.compile(rb"\b(?:gh[opsu]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})\b"), b"[REDACTED]"),
    (
        re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        b"[REDACTED]",
    ),
)
_KNOWN_NO_ARCHIVE = (
    re.compile(r"(?i)(?:^|[;&|]\s*)(?:env|printenv)\b"),
    re.compile(r"(?i)\b(?:gh\s+auth\s+token|security\s+find-generic-password)\b"),
    re.compile(r"(?i)\b(?:cat|sed|head|tail|rg|grep)\b[^\n;&|]*(?:\.env|credentials|secrets?)\b"),
)


def hook_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "command_output_hook.py"


def approval_cache_hook_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "approval_cache_hook.py"


def hook_groups() -> dict[str, list[dict[str, Any]]]:
    """Every generated hook AgentDeck installs, grouped by Codex event name.

    ``PostToolUse`` carries two handlers: the capture hook that archives the
    result, and the approval cache's observer, which is how a deferred approval
    becomes reusable only once its tool has actually run (#1738). They are
    independent — neither reads the other's output — and Codex runs both.
    """
    capture = {"type": "command", "command": str(hook_script()), "timeout": 120}
    # Short timeout: this one only reads a small JSON file under a lock, and a
    # slow hook would eat the guardian time the cache exists to save.
    approvals = {
        "type": "command",
        "command": str(approval_cache_hook_script()),
        "timeout": 20,
    }
    return {
        "PreToolUse": [{"matcher": "Bash", "hooks": [dict(capture)]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [dict(capture), dict(approvals)]}],
        "PermissionRequest": [{"matcher": "Bash", "hooks": [dict(approvals)]}],
    }


def claude_permission_hook_script() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "claude_permission_hook.py"


# How long the PermissionDenied hook may wait for the boss. Practically forever:
# a blocked chat waits until someone answers (#2271).
PERMISSION_WAIT_TIMEOUT_S = 30 * 24 * 3600

# The tools a classifier block can land on, and so the ones a one-time grant can
# be used up by. Plain reads never reach the classifier, so they skip the hook.
GRANT_TOOL_MATCHER = (
    "Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch|Agent|Task|mcp__.*"
)


def claude_permission_settings(allow_rules: list[str]) -> dict[str, Any]:
    """Settings every owned Claude worker runs with (#2271).

    ``permissions.allow`` lets everyday commands skip the classifier; the two
    hooks bring a classifier block to the boss and use up their "Allow once".
    """
    hook = {"type": "command", "command": str(claude_permission_hook_script())}
    return {
        "permissions": {"allow": list(allow_rules)},
        "hooks": {
            "PermissionDenied": [
                {"matcher": "*", "hooks": [{**hook, "timeout": PERMISSION_WAIT_TIMEOUT_S}]}
            ],
            "PreToolUse": [{"matcher": GRANT_TOOL_MATCHER, "hooks": [{**hook, "timeout": 20}]}],
        },
    }


def ensure_claude_settings(
    root: Path, allow_rules: list[str] | None = None, *, capture: bool = True
) -> Path:
    """Write one of the settings files an owned Claude worker runs with.

    ``capture`` adds the kanban command-capture hooks. ``allow_rules`` is the
    classifier skip list; a chat that does not run in ``auto`` gets none, since
    in ``plan`` or ``default`` mode an allow rule would pre-approve a write the
    mode exists to stop (#2271). Each combination is its own file, so a spawn
    only ever points at one that already says what it needs.
    """
    name = "claude-command-artifact-hooks" if capture else "claude-worker-settings"
    target = root / f"{name}{'' if allow_rules else '-no-skip'}.json"
    settings = claude_permission_settings(allow_rules or [])
    if capture:
        hooks = hook_groups()
        for event, groups in settings["hooks"].items():
            hooks.setdefault(event, []).extend(groups)
        settings["hooks"] = hooks
    _atomic_json(target, settings)
    return target


def redact_bytes(value: bytes) -> bytes:
    """Remove common credential shapes before bytes are retained or delivered."""
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_text(value: str) -> str:
    return redact_bytes(value.encode("utf-8", "replace")).decode("utf-8", "replace")


def command_fingerprint(command: str) -> str:
    """Hash only a redacted, whitespace-normalized command."""
    normalized = " ".join(redact_text(command).split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def no_archive(command: str, configured: tuple[str, ...] = ()) -> bool:
    return any(pattern.search(command) for pattern in _KNOWN_NO_ARCHIVE) or any(
        value and value.casefold() in command.casefold() for value in configured
    )


def safe_to_rewrite(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """Whether a whole shell call can be replaced by the transparent launcher."""
    if tool_name not in _SAFE_TOOL_NAMES:
        return False, "not_shell_tool"
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return False, "command_unreadable"
    if any(tool_input.get(field) not in (None, False, "") for field in _UNSAFE_INPUT_FIELDS):
        return False, "interactive_or_streaming"
    if re.search(r"(?<![&<>])&(?![&>])", command):
        return False, "background_shell"
    return True, "covered"


def _context_name(account_key: str, session_id: str) -> str:
    return hashlib.sha256(f"{account_key}\0{session_id}".encode()).hexdigest() + ".json"


def write_context(
    root: Path,
    *,
    account_key: str,
    session_id: str,
    worker_key: str,
    session_token: str,
    policy: dict[str, Any] | None,
) -> Path:
    """Publish the current host-frozen policy for one provider session."""
    contexts = root / "contexts"
    contexts.mkdir(parents=True, exist_ok=True, mode=0o700)
    contexts.chmod(0o700)
    target = contexts / _context_name(account_key, session_id)
    payload = {
        "account_key": account_key,
        "session_id": session_id,
        "session_key": f"{account_key}:{session_id}",
        "worker_key": worker_key,
        "access_sha256": hashlib.sha256(session_token.encode()).hexdigest(),
        "policy": policy or {"mode": "off"},
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_json(target, payload)
    return target


def load_context(root: Path, account_key: str, session_id: str) -> dict[str, Any] | None:
    try:
        value = json.loads((root / "contexts" / _context_name(account_key, session_id)).read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("session_id") != session_id:
        return None
    return value


def context_identity_from_environment() -> tuple[str, str, str]:
    """Resolve artifact provenance without trusting caller-supplied filesystem paths."""
    root_value = os.environ.get(CONTEXT_DIR_ENV, "").strip()
    account = os.environ.get(ACCOUNT_KEY_ENV, "").strip()
    worker = os.environ.get(WORKER_KEY_ENV, "").strip()
    if not root_value or not account or not worker:
        raise ArtifactError("this shell is not attached to an artifact-enabled worker")
    root = Path(root_value)
    ambient_session = (
        os.environ.get("CODEX_THREAD_ID", "").strip()
        or os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    )
    if ambient_session:
        exact = load_context(root, account, ambient_session)
        if exact is not None and exact.get("worker_key") == worker:
            token = os.environ.get(SESSION_TOKEN_ENV, "")
            if secrets.compare_digest(
                str(exact.get("access_sha256") or ""), hashlib.sha256(token.encode()).hexdigest()
            ):
                return str(exact["session_key"]), worker, token
    matches: list[dict[str, Any]] = []
    for path in (root / "contexts").glob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if (
            isinstance(value, dict)
            and value.get("account_key") == account
            and value.get("worker_key") == worker
            and (value.get("policy") or {}).get("mode") in {"observe", "enforce"}
        ):
            matches.append(value)
    if not matches:
        raise ArtifactError("the current worker's artifact provenance is ambiguous")
    latest = max(matches, key=lambda value: str(value.get("updated_at") or ""))
    token = os.environ.get(SESSION_TOKEN_ENV, "")
    if not token or not secrets.compare_digest(
        str(latest.get("access_sha256") or ""), hashlib.sha256(token.encode()).hexdigest()
    ):
        raise ArtifactError("the current worker's artifact capability is unavailable")
    return str(latest["session_key"]), worker, token


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class CaptureResult:
    metadata: dict[str, Any]
    event: str


@dataclass(frozen=True)
class ArtifactCoverage:
    """One read of the artifact store, answered for any chat (#1553)."""

    visible_bytes: dict[str, int]
    uncovered: dict[str, list[dict[str, str]]]
    ledger_unreadable: bool

    def bytes_for(self, session_key: str) -> int:
        return self.visible_bytes.get(session_key, 0)

    def uncovered_for(self, session_key: str) -> list[dict[str, str]]:
        if self.ledger_unreadable:
            return [{"reason": "unreadable_coverage_ledger", "command_fingerprint": ""}]
        return self.uncovered.get(session_key, [])[-UNCOVERED_SUMMARY_LIMIT:]


class ArtifactError(ValueError):
    pass


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.artifacts = root / "artifacts"
        self.pending = root / "pending"
        self.events = root / "events"
        for directory in (root, self.artifacts, self.pending, self.events):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        lock = root / ".lock"
        lock.touch(mode=0o600, exist_ok=True)
        lock.chmod(0o600)
        self._lock_path = lock

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock_path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def prepare(self, context: dict[str, Any], tool_input: dict[str, Any]) -> str:
        run_id = secrets.token_urlsafe(18)
        payload = {
            "context": context,
            "tool_input": tool_input,
            "prepared_at": datetime.now(UTC).isoformat(),
        }
        key = secrets.token_bytes(32)
        sealed = _seal(json.dumps(payload, separators=(",", ":")).encode(), key)
        _atomic_json(self.pending / f"{run_id}.json", sealed)
        return f"{run_id}.{key.hex()}"

    def take_prepared(self, run_id: str) -> dict[str, Any]:
        identifier, separator, raw_key = run_id.partition(".")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", identifier)
            or not re.fullmatch(r"[a-f0-9]{64}", raw_key)
        ):
            raise ArtifactError("invalid capture id")
        path = self.pending / f"{identifier}.json"
        try:
            sealed = json.loads(path.read_text())
            path.unlink()
            payload = json.loads(_open(sealed, bytes.fromhex(raw_key)))
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactError("capture request is unavailable") from exc
        if not isinstance(payload, dict):
            raise ArtifactError("capture request is malformed")
        return payload

    def capture(
        self,
        *,
        context: dict[str, Any],
        command: str,
        stdout: bytes,
        stderr: bytes,
        exit_code: int | None,
        term_signal: int | None,
        timed_out: bool,
        started_at: datetime,
        finished_at: datetime,
        archive: bool = True,
    ) -> CaptureResult:
        policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
        visible_chars = max(1, int(policy.get("visible_tool_result_chars") or 8_000))
        clean_stdout = redact_bytes(stdout)
        clean_stderr = redact_bytes(stderr)
        artifact_id = secrets.token_urlsafe(24)
        slice_capability = secrets.token_urlsafe(24) if archive else ""
        directory = self.artifacts / artifact_id
        metadata = {
            "artifact_id": artifact_id,
            "chat": str(context.get("session_key") or ""),
            "card": str(context.get("worker_key") or ""),
            "access_sha256": str(context.get("access_sha256") or ""),
            "generation": str(policy.get("generation") or ""),
            "policy_mode": str(policy.get("mode") or "off"),
            "command_fingerprint": command_fingerprint(command),
            "exit_code": exit_code,
            "signal": term_signal,
            "timeout": bool(timed_out),
            "started_at": started_at.astimezone(UTC).isoformat(),
            "finished_at": finished_at.astimezone(UTC).isoformat(),
            "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
            "stdout_bytes": len(stdout),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_bytes": len(stderr),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "retained_stdout_sha256": hashlib.sha256(clean_stdout).hexdigest(),
            "retained_stderr_sha256": hashlib.sha256(clean_stderr).hexdigest(),
            "retained_paths": {},
            "excerpt": "",
            "truncation": TRUNCATION_VERSION,
            "retention_status": "active",
            "retention_days": max(1, int(policy.get("artifact_retention_days") or 30)),
            "slice_max_bytes": max(
                1, int(policy.get("artifact_slice_max_bytes") or DEFAULT_SLICE_MAX_BYTES)
            ),
            "slice_call_limit": max(
                1, int(policy.get("artifact_slice_call_limit") or DEFAULT_SLICE_CALL_LIMIT)
            ),
            "slice_calls": 0,
            "slice_access_sha256": (
                hashlib.sha256(slice_capability.encode()).hexdigest() if archive else ""
            ),
            "archived": bool(archive),
        }
        try:
            if archive:
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
                stdout_path = directory / "stdout.bin"
                stderr_path = directory / "stderr.bin"
                # Redaction happens before persistence, not only before delivery
                # (#1484). Only one copy of each stream is ever written, and it is
                # the redacted one, so a secret shape that `no_archive` did not
                # catch never reaches the disk. The original byte counts and
                # hashes above still describe what the command actually produced.
                _write_bytes(stdout_path, clean_stdout)
                _write_bytes(stderr_path, clean_stderr)
                metadata["retained_paths"] = {
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                }
            if not archive:
                metadata["artifact_id"] = ""
            event = render_event(
                metadata,
                clean_stdout,
                clean_stderr,
                max_chars=visible_chars,
                slice_capability=slice_capability,
            )
            # The retained copy keeps the delivered event's exact length — pressure
            # reads it as model-visible bytes — but never its bearer capability.
            metadata["excerpt"] = (
                event.replace(slice_capability, "*" * len(slice_capability))
                if slice_capability
                else event
            )
            if archive:
                _atomic_json(directory / "metadata.json", metadata)
            else:
                _atomic_json(self.events / f"{secrets.token_urlsafe(18)}.json", metadata)
        except BaseException:
            if archive:
                self._discard_partial(directory)
            raise
        return CaptureResult(metadata, event)

    def metadata(self, artifact_id: str) -> dict[str, Any]:
        path = self._artifact_path(artifact_id) / "metadata.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ArtifactError("unknown artifact") from exc
        if not isinstance(value, dict) or value.get("artifact_id") != artifact_id:
            raise ArtifactError("invalid artifact")
        return value

    def read_slice(
        self,
        artifact_id: str,
        *,
        session_key: str = "",
        card: str = "",
        session_token: str = "",
        slice_capability: str = "",
        stream: str,
        offset: int,
        max_bytes: int,
    ) -> dict[str, Any]:
        if stream not in {"stdout", "stderr"}:
            raise ArtifactError("stream must be stdout or stderr")
        if offset < 0 or max_bytes <= 0:
            raise ArtifactError("offset must be non-negative and max-bytes must be positive")
        with self._locked():
            metadata = self.metadata(artifact_id)
            if not (
                _authorized(metadata, session_key, card, session_token)
                or _slice_authorized(metadata, slice_capability)
            ):
                raise PermissionError("artifact does not belong to this chat and card")
            if max_bytes > int(metadata.get("slice_max_bytes") or 0):
                raise ArtifactError("slice exceeds the artifact byte limit")
            calls = int(metadata.get("slice_calls") or 0)
            if calls >= int(metadata.get("slice_call_limit") or 0):
                raise ArtifactError("artifact slice call limit reached")
            path = self._artifact_path(artifact_id) / f"{stream}.bin"
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read(max_bytes)
                    more = bool(handle.read(1))
            except OSError as exc:
                raise ArtifactError("artifact stream is unavailable") from exc
            metadata["slice_calls"] = calls + 1
            _atomic_json(self._artifact_path(artifact_id) / "metadata.json", metadata)
        return {
            "artifact_id": artifact_id,
            "stream": stream,
            "offset": offset,
            "bytes": len(data),
            "next_offset": offset + len(data),
            "more": more,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "text": data.decode("utf-8", "replace"),
        }

    def read_full(
        self, artifact_id: str, *, session_key: str, card: str, session_token: str
    ) -> dict[str, Any]:
        with self._locked():
            metadata = self.authorized_metadata(
                artifact_id,
                session_key=session_key,
                card=card,
                session_token=session_token,
            )
            directory = self._artifact_path(artifact_id)
            try:
                stdout = (directory / "stdout.bin").read_bytes()
                stderr = (directory / "stderr.bin").read_bytes()
            except OSError as exc:
                raise ArtifactError("artifact streams are unavailable") from exc
        return {
            "metadata": {
                key: value for key, value in metadata.items() if key != "access_sha256"
            },
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        }

    def authorized_metadata(
        self, artifact_id: str, *, session_key: str, card: str, session_token: str
    ) -> dict[str, Any]:
        metadata = self.metadata(artifact_id)
        if not _authorized(metadata, session_key, card, session_token):
            raise PermissionError("artifact does not belong to this chat and card")
        return metadata

    def _metadata_paths(self) -> list[Path]:
        return [
            *(entry / "metadata.json" for entry in self.artifacts.iterdir() if entry.is_dir()),
            *self.events.glob("*.json"),
        ]

    def coverage_index(self, *, since: datetime | None = None) -> ArtifactCoverage:
        """Every chat's model-visible bytes and uncovered rows, in one pass (#1553).

        ``workers_state`` asks both questions for every worker on an account, and the
        account's worker registry is never pruned. Asked per chat, that made the cost
        ``workers x artifacts``: at 643 workers and 382 artifacts one request parsed
        245,626 files on the event loop, so the runtime stopped accepting connections
        and its socket backlog grew without ever draining -- the whole deck looked dead
        while it was merely busy. Reading the store once and answering each worker from
        a dict keeps the request proportional to the store alone.
        """
        visible: dict[str, int] = {}
        for path in self._metadata_paths():
            try:
                metadata = json.loads(path.read_text())
                finished = datetime.fromisoformat(str(metadata["finished_at"]))
            except (KeyError, TypeError, ValueError, OSError):
                continue
            chat = metadata.get("chat")
            if (
                not isinstance(chat, str)
                or metadata.get("policy_mode") != "enforce"
                or (since is not None and finished < since)
            ):
                continue
            visible[chat] = visible.get(chat, 0) + len(
                str(metadata.get("excerpt") or "").encode()
            )

        uncovered: dict[str, list[dict[str, str]]] = {}
        unreadable = False
        try:
            rows = self.root.joinpath("uncovered.jsonl").read_text().splitlines()
        except OSError:
            rows = []
        for raw in rows:
            try:
                row = json.loads(raw)
                observed = datetime.fromisoformat(str(row["observed_at"]))
            except (ValueError, TypeError, KeyError):
                # One unparseable row condemns the whole ledger, exactly as the
                # per-chat read does: coverage it cannot read is not coverage.
                unreadable = True
                break
            chat = row.get("chat")
            if not isinstance(chat, str) or (since is not None and observed < since):
                continue
            uncovered.setdefault(chat, []).append(
                {
                    "reason": str(row.get("reason") or "unknown"),
                    "command_fingerprint": str(row.get("command_fingerprint") or ""),
                }
            )
        return ArtifactCoverage(
            visible_bytes=visible, uncovered=uncovered, ledger_unreadable=unreadable
        )

    def model_visible_bytes(self, session_key: str, *, since: datetime | None = None) -> int:
        total = 0
        for path in self._metadata_paths():
            try:
                metadata = json.loads(path.read_text())
                finished = datetime.fromisoformat(str(metadata["finished_at"]))
            except (KeyError, TypeError, ValueError, OSError):
                continue
            if (
                metadata.get("chat") == session_key
                and metadata.get("policy_mode") == "enforce"
                and (since is None or finished >= since)
            ):
                total += len(str(metadata.get("excerpt") or "").encode())
        return total

    def uncovered_summary(
        self, session_key: str, *, since: datetime | None = None
    ) -> list[dict[str, str]]:
        try:
            rows = self.root.joinpath("uncovered.jsonl").read_text().splitlines()
        except OSError:
            return []
        matches: list[dict[str, str]] = []
        for raw in rows:
            try:
                row = json.loads(raw)
                observed = datetime.fromisoformat(str(row["observed_at"]))
            except (ValueError, TypeError, KeyError):
                return [{"reason": "unreadable_coverage_ledger", "command_fingerprint": ""}]
            if row.get("chat") == session_key and (since is None or observed >= since):
                matches.append(
                    {
                        "reason": str(row.get("reason") or "unknown"),
                        "command_fingerprint": str(row.get("command_fingerprint") or ""),
                    }
                )
        return matches[-UNCOVERED_SUMMARY_LIMIT:]

    def cleanup(self, *, now: datetime | None = None) -> list[str]:
        """Remove every artifact past its own retention (#2154).

        Each artifact carries the retention its policy gave it when it was written, and that
        is the whole rule: nothing outside the artifact decides whether it is still needed.
        """
        with self._locked():
            removed = self._cleanup_locked(now=now)
            self._cleanup_pending_locked(now=now)
            self._cleanup_events_locked(now=now)
            return removed

    def _cleanup_pending_locked(self, *, now: datetime | None) -> None:
        cutoff = (now or datetime.now(UTC)).timestamp() - 3600
        for path in self.pending.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _cleanup_events_locked(self, *, now: datetime | None) -> None:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        for path in self.events.glob("*.json"):
            try:
                metadata = json.loads(path.read_text())
                finished = datetime.fromisoformat(str(metadata["finished_at"]))
                expires = finished + timedelta(days=int(metadata["retention_days"]))
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if expires > moment:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def _cleanup_locked(self, *, now: datetime | None) -> list[str]:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        removed: list[str] = []
        for entry in self.artifacts.iterdir():
            if not entry.is_dir():
                continue
            if not (entry / "metadata.json").is_file():
                try:
                    orphaned = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
                if orphaned + timedelta(hours=1) <= moment:
                    self._discard_partial(entry)
                continue
            try:
                metadata = self.metadata(entry.name)
                finished = datetime.fromisoformat(str(metadata["finished_at"]))
                expires = finished + timedelta(days=int(metadata["retention_days"]))
            except (ArtifactError, KeyError, TypeError, ValueError, OSError):
                continue
            if expires > moment:
                continue
            for name in (
                "stdout.bin",
                "stderr.bin",
                "metadata.json",
            ):
                try:
                    (entry / name).unlink()
                except FileNotFoundError:
                    pass
            try:
                entry.rmdir()
            except OSError:
                continue
            removed.append(entry.name)
        return removed

    @staticmethod
    def _discard_partial(directory: Path) -> None:
        for name in (
            "stdout.bin",
            "stderr.bin",
            "metadata.json",
        ):
            try:
                (directory / name).unlink()
            except OSError:
                pass
        try:
            directory.rmdir()
        except OSError:
            pass

    def observe_uncovered(self, context: dict[str, Any], command: str, reason: str) -> None:
        row = {
            "observed_at": datetime.now(UTC).isoformat(),
            "chat": str(context.get("session_key") or ""),
            "card": str(context.get("worker_key") or ""),
            "command_fingerprint": command_fingerprint(command),
            "reason": reason,
        }
        path = self.root / "uncovered.jsonl"
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with self._locked():
            retained = b""
            try:
                if path.stat().st_size + len(encoded) > UNCOVERED_LEDGER_MAX_BYTES:
                    with path.open("rb") as handle:
                        handle.seek(-min(path.stat().st_size, UNCOVERED_LEDGER_MAX_BYTES // 2), 2)
                        retained = handle.read()
                    newline = retained.find(b"\n")
                    retained = retained[newline + 1 :] if newline >= 0 else b""
            except OSError:
                retained = b""
            if retained:
                temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
                _write_bytes(temporary, retained + encoded)
                os.replace(temporary, path)
            else:
                descriptor = os.open(
                    path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_APPEND
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "ab") as handle:
                    handle.write(encoded)
            path.chmod(0o600)

    def _artifact_path(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", artifact_id):
            raise ArtifactError("invalid artifact id")
        return self.artifacts / artifact_id


def _write_bytes(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _seal(value: bytes, key: bytes) -> dict[str, str]:
    nonce = secrets.token_bytes(16)
    encrypted = _xor_stream(value, key, nonce)
    tag = hmac.new(key, nonce + encrypted, hashlib.sha256).hexdigest()
    return {
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(encrypted).decode(),
        "tag": tag,
        "prepared_at": datetime.now(UTC).isoformat(),
    }


def _open(value: dict[str, Any], key: bytes) -> bytes:
    nonce = base64.b64decode(str(value["nonce"]), validate=True)
    encrypted = base64.b64decode(str(value["ciphertext"]), validate=True)
    expected = hmac.new(key, nonce + encrypted, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(value["tag"])):
        raise ArtifactError("capture request authentication failed")
    return _xor_stream(encrypted, key, nonce)


def _xor_stream(value: bytes, key: bytes, nonce: bytes) -> bytes:
    output = bytearray(len(value))
    for offset in range(0, len(value), 32):
        block = hmac.new(
            key, nonce + (offset // 32).to_bytes(8, "big"), hashlib.sha256
        ).digest()
        chunk = value[offset : offset + 32]
        output[offset : offset + len(chunk)] = bytes(
            left ^ right for left, right in zip(chunk, block, strict=False)
        )
    return bytes(output)


def _authorized(
    metadata: dict[str, Any], session_key: str, card: str, session_token: str
) -> bool:
    expected = str(metadata.get("access_sha256") or "")
    actual = hashlib.sha256(session_token.encode()).hexdigest()
    return (
        metadata.get("chat") == session_key
        and metadata.get("card") == card
        and bool(session_token)
        and secrets.compare_digest(expected, actual)
    )


def _slice_authorized(metadata: dict[str, Any], capability: str) -> bool:
    expected = str(metadata.get("slice_access_sha256") or "")
    actual = hashlib.sha256(capability.encode()).hexdigest()
    return bool(capability) and secrets.compare_digest(expected, actual)


def _lines(value: bytes) -> list[bytes]:
    return value.splitlines() or ([b""] if value else [])


def _decoded(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def render_event(
    metadata: dict[str, Any],
    stdout: bytes,
    stderr: bytes,
    *,
    max_chars: int,
    slice_capability: str = "",
) -> str:
    """Render one deterministic, labelled excerpt inside a hard character bound."""
    status = (
        f"signal {metadata['signal']}"
        if metadata.get("signal") is not None
        else "timeout"
        if metadata.get("timeout")
        else f"exit {metadata.get('exit_code')}"
    )
    artifact = str(metadata.get("artifact_id") or "not archived")
    header = [
        f"AgentDeck command result ({TRUNCATION_VERSION})",
        f"status: {status}; duration_ms={metadata.get('duration_ms', 0)}",
        f"artifact: {artifact}",
        f"stdout: {metadata.get('stdout_bytes', 0)} bytes "
        f"sha256={metadata.get('stdout_sha256', '')}",
        f"stderr: {metadata.get('stderr_bytes', 0)} bytes "
        f"sha256={metadata.get('stderr_sha256', '')}",
    ]
    if artifact != "not archived" and slice_capability:
        header.append(
            f"bounded read: agentdeck artifact slice {artifact} --cap {slice_capability} "
            "--stream stdout "
            "--offset <byte> --max-bytes <count>"
        )
    combined = [(b"stdout", line) for line in _lines(stdout)] + [
        (b"stderr", line) for line in _lines(stderr)
    ]
    failures = [(stream, line) for stream, line in combined if _FAILURE.search(line)][:8]
    failure_text = [
        "failure extraction (labelled extraction; not chronological):",
        *[f"[{_decoded(stream)}] {_decoded(line[:500])}" for stream, line in failures],
    ] if failures else []

    def chronological(name: str, value: bytes) -> list[str]:
        if not value:
            return [f"{name}: empty"]
        window = 900
        head = value[:window]
        tail = value[-window:] if len(value) > window else b""
        omitted = max(0, len(value) - len(head) - len(tail))
        omitted_lines = max(0, len(_lines(value)) - len(_lines(head)) - len(_lines(tail)))
        result = [f"{name} chronological head:", _decoded(head)]
        if omitted:
            result.append(f"[{omitted} bytes and {omitted_lines} lines omitted]")
        if tail:
            result.extend([f"{name} chronological tail:", _decoded(tail)])
        return result

    body = "\n".join(
        [
            *header,
            *failure_text,
            *chronological("stdout", stdout),
            *chronological("stderr", stderr),
        ]
    )
    if len(body) <= max_chars:
        return body
    marker = f"\n[AgentDeck excerpt clipped to {max_chars} characters]"
    clipped = body[: max(0, max_chars - len(marker))] + marker
    return clipped[:max_chars]


def execute_prepared(root: Path, run_id: str) -> int:
    """Run one prepared shell command, retaining streams and printing only its event."""
    store = ArtifactStore(root)
    prepared = store.take_prepared(run_id)
    context = prepared.get("context") if isinstance(prepared.get("context"), dict) else {}
    tool_input = prepared.get("tool_input") if isinstance(prepared.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
    configured = tuple(str(item) for item in policy.get("no_archive_commands") or ())
    archive = not no_archive(command, configured)
    timeout_value = tool_input.get("timeout", tool_input.get("timeout_ms"))
    timeout_s = float(timeout_value) / 1000 if isinstance(timeout_value, (int, float)) else None
    started = datetime.now(UTC)
    process: subprocess.Popen[bytes] | None = None
    forwarded_signal: int | None = None
    previous_handlers: dict[int, Any] = {}

    def forward(signum: int, _frame: Any) -> None:
        nonlocal forwarded_signal
        forwarded_signal = signum
        if process is None:
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    # Install the handlers before the command starts. The command runs in its
    # own session, so a signal that killed this launcher with the default
    # action would leave the whole group running (#2872); one that lands while
    # the command is starting is held and forwarded as soon as it exists.
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, forward)
    timed_out = False
    try:
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        if forwarded_signal is not None:
            try:
                os.killpg(process.pid, forwarded_signal)
            except ProcessLookupError:
                pass
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    finished = datetime.now(UTC)
    return_code = process.returncode
    term_signal = (
        -return_code if not timed_out and return_code is not None and return_code < 0 else None
    )
    capture_args = {
        "context": context,
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": None if term_signal is not None or timed_out else return_code,
        "term_signal": term_signal,
        "timed_out": timed_out,
        "started_at": started,
        "finished_at": finished,
    }
    try:
        result = store.capture(**capture_args, archive=archive)
    except OSError:
        # The command has already run. Preserve its status and keep its output
        # bounded even when durable storage fails; never fall back to raw output.
        result = store.capture(**capture_args, archive=False)
    os.write(1, result.event.encode("utf-8", "replace") + b"\n")
    if forwarded_signal is not None:
        signal.signal(forwarded_signal, signal.SIG_DFL)
        os.kill(os.getpid(), forwarded_signal)
    if term_signal is not None:
        signal.signal(term_signal, signal.SIG_DFL)
        os.kill(os.getpid(), term_signal)
    return 124 if timed_out else int(return_code or 0)
