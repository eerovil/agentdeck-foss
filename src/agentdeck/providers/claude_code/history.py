"""history.jsonl → session titles / last prompts.

Each line is one submitted prompt:
{"display": "...", "project": "/path", "sessionId": "...", "timestamp": ...}

Title = first prompt of the session, last_prompt = most recent one.
Malformed lines are skipped, never fatal.

The file only ever grows, by one line per prompt the CLI records, and every scan
reads it. :class:`HistoryIndex` keeps what it has read and its byte offset, so a
scan reads only the lines appended since the last one (#2865); a file that shrank
or was replaced is read again from the start.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryEntry:
    session_id: str
    title: str | None = None
    last_prompt: str | None = None
    project: str | None = None


def _apply(entries: dict[str, HistoryEntry], data: bytes) -> int:
    """Fold complete JSONL lines into ``entries``; how many were malformed."""
    skipped = 0
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            sid = str(item["sessionId"])
            display = item.get("display")
        except (ValueError, TypeError, KeyError):
            skipped += 1
            continue
        entry = entries.get(sid) or HistoryEntry(session_id=sid)
        updated = entry
        if isinstance(display, str) and display:
            updated = replace(updated, title=updated.title or display, last_prompt=display)
        project = item.get("project")
        if updated.project is None and isinstance(project, str):
            updated = replace(updated, project=project)
        if updated is not entry or sid not in entries:
            entries[sid] = updated
    return skipped


def _parses(data: bytes) -> bool:
    try:
        json.loads(data)
    except ValueError:
        return False
    return True


class HistoryIndex:
    """One ``history.jsonl`` read incrementally by byte offset (#2865).

    Thread-safe, because a scan reads it through ``asyncio.to_thread``. Entries are
    frozen and ``read`` returns a copy of the map, so a caller holding one never sees
    it change under it.
    """

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / "history.jsonl"
        self._lock = threading.Lock()
        self._entries: dict[str, HistoryEntry] = {}
        self._identity: tuple[int, int] | None = None
        self._offset = 0

    def read(self) -> dict[str, HistoryEntry]:
        with self._lock:
            self._catch_up()
            return dict(self._entries)

    def _catch_up(self) -> None:
        try:
            handle = self.path.open("rb")
        except OSError:
            self._reset(None)
            return
        with handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self._identity or stat.st_size < self._offset:
                # Replaced (rotated) or truncated: what was read no longer describes it.
                self._reset(identity)
            if stat.st_size == self._offset:
                return
            handle.seek(self._offset)
            appended = handle.read()
        end = appended.rfind(b"\n")
        complete = appended[: end + 1]
        skipped = _apply(self._entries, complete)
        self._offset += len(complete)
        tail = appended[end + 1 :]
        if tail.strip() and _parses(tail):
            # A last line with no newline yet. Applied, but the offset stays before it,
            # so the next read applies it again in order with whatever follows.
            _apply(self._entries, tail)
        if skipped:
            log.debug("history.jsonl: skipped %d malformed lines", skipped)

    def _reset(self, identity: tuple[int, int] | None) -> None:
        self._entries = {}
        self._identity = identity
        self._offset = 0


def load_history(config_dir: Path) -> dict[str, HistoryEntry]:
    """Map sessionId → HistoryEntry, in file (chronological) order. A one-off read."""
    return HistoryIndex(config_dir).read()
