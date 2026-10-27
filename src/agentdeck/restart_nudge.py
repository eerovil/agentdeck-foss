"""Durable "nudge me after the next runtime restart" registry (issue #453).

Restarting the persistent runtime tears down every owned agent process group, so
a chat that was working when the runtime restarts is simply gone — including the
chat whose merge triggered the deploy. This registry is how such a chat asks to
be woken up again:

* any agent registers its own AgentDeck session key (through
  ``PUT /api/sessions/<key>/restart-nudge`` or ``agentdeck nudge-on-restart``);
* the fresh runtime consumes the registry once on boot, delivers one fixed nudge
  per registered session, and clears the file.

The registry is a *set of session keys*, not a queue of messages: one fixed
built-in nudge is delivered, entries deduplicate by key, and startup consumes
everything it finds — a session that cannot be found or refuses delivery is
logged and dropped rather than retained, so a stale entry can never nudge a chat
on some unrelated future restart. This replaces the per-session
``restart-continue`` markers, which only ever worked for the one Claude session
that ran ``agentdeck restart-runtime`` itself.

Full session keys (``claude_code:main:<id>``, ``codex:main:<id>``) are stored
rather than bare provider session ids: an id alone is ambiguous across providers
and accounts, and the runtime needs the provider and account to know how to
deliver.
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# The one message every post-restart nudge delivers. Callers do not get to
# choose it: the registry is a set of chats, and every entry means the same
# thing — your process was replaced, pick your task back up on the new code.
NUDGE_MESSAGE = (
    "The AgentDeck runtime has been restarted and the current code is now live. "
    "Continue the task you were working on before the restart."
)



@dataclass(frozen=True)
class NudgeEntry:
    """One session that asked to be nudged after the next runtime restart."""

    session_key: str
    registered_at: float

    @property
    def delivery_id(self) -> str:
        """Stable at-most-once identity for this entry's nudge turn."""
        return f"restart-nudge:{self.session_key}:{int(self.registered_at)}"

    def is_stale(self, now: float, ttl_s: float) -> bool:
        return now - self.registered_at > ttl_s

    @property
    def parts(self) -> tuple[str, str, str] | None:
        return session_key_parts(self.session_key)


def session_key_parts(session_key: str) -> tuple[str, str, str] | None:
    """``(provider_id, label, session_id)``, or None if the key is malformed.

    A session key is ``<provider>:<label>:<session_id>``; only the first two
    separators are structural, so a session id containing ``:`` survives.
    """
    pieces = session_key.split(":", 2)
    if len(pieces) != 3 or not all(piece.strip() for piece in pieces):
        return None
    provider_id, label, session_id = (piece.strip() for piece in pieces)
    return provider_id, label, session_id


def _lock_path(path: Path) -> Path:
    # A lock on the registry file's own inode would not exclude a writer that
    # opens the path after consume() has renamed that inode away.
    return path.with_name(path.name + ".lock")


@contextmanager
def _exclusive(path: Path):
    """Hold a cross-process lock for one read-modify-write of the registry."""
    lock = _lock_path(path)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EROFS, errno.ENOENT):
            raise
        # No lock available (read-only or missing parent we cannot create):
        # proceed unlocked rather than refuse an agent's registration.
        log.warning("restart-nudge: cannot lock %s (%s); proceeding unlocked", lock, exc)
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


class RestartNudgeRegistry:
    """The shared registry file: written by agents, consumed by runtime boot."""

    def __init__(self, path: Path, *, ttl_s: float) -> None:
        self.path = Path(path)
        self.ttl_s = ttl_s

    # --- reading -----------------------------------------------------------

    def _load(self, path: Path | None = None) -> dict[str, float]:
        """Session key -> registration time; a damaged file reads as empty."""
        source = self.path if path is None else path
        try:
            raw = json.loads(source.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("restart-nudge: ignoring unreadable registry at %s", source)
            return {}
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if not isinstance(sessions, dict):
            log.warning("restart-nudge: ignoring malformed registry at %s", source)
            return {}
        loaded: dict[str, float] = {}
        for session_key, record in sessions.items():
            if not isinstance(session_key, str) or not session_key.strip():
                continue
            registered_at = record.get("registered_at") if isinstance(record, dict) else None
            if not isinstance(registered_at, int | float):
                continue
            loaded[session_key.strip()] = float(registered_at)
        return loaded

    def _write(self, sessions: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "sessions": {
                session_key: {"registered_at": registered_at}
                for session_key, registered_at in sorted(sessions.items())
            }
        }
        temporary.write_text(json.dumps(payload, indent=1))
        temporary.replace(self.path)

    @staticmethod
    def _entries(sessions: dict[str, float]) -> list[NudgeEntry]:
        return [
            NudgeEntry(session_key=session_key, registered_at=registered_at)
            for session_key, registered_at in sorted(sessions.items())
        ]

    def entries(self) -> list[NudgeEntry]:
        """Currently registered sessions, oldest registration untouched."""
        return self._entries(self._load())

    def is_registered(self, session_key: str) -> bool:
        return session_key.strip() in self._load()

    # --- writing -----------------------------------------------------------

    def register(self, session_key: str, *, now: float | None = None) -> NudgeEntry:
        """Ask for a nudge after the next restart. Idempotent per session key.

        Re-registering refreshes the timestamp: the caller is saying *now* that
        it is waiting, and its entry should outlive the TTL from this moment.
        """
        key = session_key.strip()
        if not key:
            raise ValueError("session_key must not be empty")
        stamp = time.time() if now is None else now
        with _exclusive(self.path):
            sessions = self._load()
            sessions[key] = stamp
            self._write(sessions)
        return NudgeEntry(session_key=key, registered_at=stamp)

    def unregister(self, session_key: str) -> bool:
        """Withdraw a pending request. Returns whether one was registered."""
        key = session_key.strip()
        with _exclusive(self.path):
            sessions = self._load()
            existed = sessions.pop(key, None) is not None
            if existed:
                self._write(sessions)
        return existed

    def consume(self) -> list[NudgeEntry]:
        """Claim every entry and clear the registry, atomically.

        Called once per runtime boot. The file is renamed under the lock before
        it is read, so a registration racing this boot lands in a fresh registry
        (and waits for the *next* restart) instead of being read and dropped.
        """
        claim = self.path.with_name(self.path.name + ".consuming")
        with _exclusive(self.path):
            try:
                self.path.replace(claim)
            except FileNotFoundError:
                return []
            except OSError:
                log.exception("restart-nudge: could not claim registry at %s", self.path)
                return []
        try:
            return self._entries(self._load(claim))
        finally:
            claim.unlink(missing_ok=True)
