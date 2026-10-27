"""sessions/<pid>.json registry parsing + /proc liveness.

Claude Code writes one registry file per process under
$CLAUDE_CONFIG_DIR/sessions/. Files linger after process death, so liveness
must be verified against /proc: the pid is alive *and* /proc/<pid>/stat
field 22 (starttime) matches the recorded ``procStart`` (pid-reuse guard).

Observed schema (CLI v2.1.198), all fields optional for us:
{pid, sessionId, cwd, startedAt, procStart, version, kind, entrypoint}
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

PROC_ROOT = Path("/proc")


@dataclass
class RegistryEntry:
    pid: int
    session_id: str
    cwd: Path | None
    started_at: datetime | None
    proc_start: str | None
    version: str | None
    kind: str | None
    entrypoint: str | None
    raw: dict = field(default_factory=dict, repr=False)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_registry(config_dir: Path) -> list[RegistryEntry]:
    """Parse all sessions/*.json; malformed files are skipped, never fatal."""
    sessions_dir = config_dir / "sessions"
    entries: list[RegistryEntry] = []
    if not sessions_dir.is_dir():
        return entries
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            pid = int(data["pid"])
            session_id = str(data["sessionId"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            log.debug("skipping malformed registry file %s: %s", path.name, exc)
            continue
        cwd = data.get("cwd")
        proc_start = data.get("procStart")
        entries.append(
            RegistryEntry(
                pid=pid,
                session_id=session_id,
                cwd=Path(cwd) if isinstance(cwd, str) else None,
                started_at=_parse_ts(data.get("startedAt")),
                proc_start=str(proc_start) if proc_start is not None else None,
                version=data.get("version"),
                kind=data.get("kind"),
                entrypoint=data.get("entrypoint"),
                raw=data,
            )
        )
    return entries


def proc_starttime(pid: int, proc_root: Path = PROC_ROOT) -> str | None:
    """Read /proc/<pid>/stat field 22 (starttime); None if the pid is gone."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens — split after the last ')'
    _, _, rest = stat.rpartition(")")
    fields = rest.split()
    if len(fields) < 20:  # fields[0] is field 3; starttime (22) is fields[19]
        return None
    return fields[19]


def session_deep_link(pid: int, proc_root: Path = PROC_ROOT) -> str | None:
    """The claude.ai/code URL for a live session, or None.

    Derived from the process's ``CLAUDE_CODE_SESSION_ACCESS_TOKEN`` — an
    ``sk-ant-si-<jwt>`` whose payload carries ``session_id`` as ``cse_<id>``;
    the web URL uses the ``session_<id>`` form. Only cloud/RC-spawned sessions
    carry the token, so a plain local ``claude`` returns None (no link). Mirrors
    the kanban poller's ``kanban_board.py:_session_url`` decode.

    We read the target's own environment (same uid), never a shared credential —
    this is navigation, not the send path.
    """
    try:
        environ = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    tok = ""
    for kv in environ.split(b"\0"):
        if kv.startswith(b"CLAUDE_CODE_SESSION_ACCESS_TOKEN="):
            tok = kv.split(b"=", 1)[1].decode("utf-8", "replace")
            break
    if "sk-ant-si-" not in tok:
        return None
    parts = tok.split("sk-ant-si-")[-1].split(".")
    if len(parts) < 2:
        return None
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        sid = json.loads(base64.urlsafe_b64decode(pad)).get("session_id", "")
    except (ValueError, json.JSONDecodeError):
        return None
    return f"https://claude.ai/code/session_{sid.split('_', 1)[-1]}" if sid else None


def is_alive(entry: RegistryEntry, proc_root: Path = PROC_ROOT) -> bool:
    """True iff the pid exists AND its starttime matches procStart."""
    starttime = proc_starttime(entry.pid, proc_root)
    if starttime is None:
        return False
    if entry.proc_start is None:
        return False  # can't rule out pid reuse — treat as dead (conservative)
    return starttime == entry.proc_start
