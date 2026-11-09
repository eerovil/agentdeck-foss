"""What a chat's next message will cost, and what it used in the last day (#4163, #4168).

The colour dot on chat cards, Work rows and the chat page. Its colour is the chat's context
size, because that is what the next message pays for: it looks forward, so a compact turns
a red dot green and an old quiet chat still warns what waking it costs. The size comes from
the scan the deck already does.

Behind the dot stays the share of the week the chat used in the last 24 hours. #2864 removed
per-chat pricing so the deck would stop reading every transcript in the background; the owner
accepted this smaller read back: a pass a minute that reads only the bytes added since the
last one, of chats the provider can read, and keeps nothing in the database. A web restart
starts over from the transcripts.

Standard library only: ``scripts/token_report.py`` imports the weights from here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# % of the main week per token, fitted over 2026-10-05 to 2026-11-04 (R2 0.97-0.99).
READ, WRITE, OUTPUT, INPUT = 0.0065e-6, 0.28e-6, 1.4e-6, 0.28e-6

WINDOW_S = 24 * 3600
INTERVAL_S = 60.0
# Context sizes, also the ``ctx`` badge's: #4059 compacts near 200k, and 352 real compacts
# seen on 2026-11-07 all fired between 160k and 175k, so red is a chat about to compact.
CONTEXT_MIDDLING = 100_000
CONTEXT_EXPENSIVE = 150_000


def size_level(tokens: int) -> str:
    """The dot's colour for a context size."""
    if tokens >= CONTEXT_EXPENSIVE:
        return "expensive"
    if tokens >= CONTEXT_MIDDLING:
        return "middling"
    return "cheap"


def percent(value: float) -> str:
    """A small share of the week, in as few digits as still say something."""
    return f"{max(value, 0.0001):.2g}"


@dataclass(frozen=True)
class ChatUse:
    """One chat's reading. ``context`` and ``share`` are rounded to what a reader sees, so a
    chat is announced as changed only when something visible moved. ``context`` is None just
    after a compact, and ``share`` when the chat used nothing in the last day."""

    level: str
    context: int | None = None
    share: float | None = None
    scale: float = 1.0  # the account's week_scale

    @property
    def lines(self) -> tuple[str, ...]:
        """What the dot stands for, in words: its tooltip, and the panel on the chat page."""
        lines = []
        if self.context is None:
            lines.append("Just compacted: the exact size shows after the next message.")
        else:
            warm = percent(self.context * READ * self.scale)
            cold = percent(self.context * WRITE * self.scale)
            lines.append(f"{self.context // 1000}k in context.")
            lines.append(
                f"Next message, without its reply: {warm} % of the week if it follows soon, "
                f"{cold} % after a pause."
            )
        if self.share is not None:
            amount = f"{self.share:.1f} %" if self.share >= 0.1 else "under 0.1 %"
            lines.append(f"Last 24 hours: {amount} of the week.")
        return tuple(lines)

    @property
    def title(self) -> str:
        return f"{self.level.capitalize()}: {' '.join(self.lines)}"


def reading(
    context: int | None, compacted: bool, share: float | None, scale: float = 1.0
) -> ChatUse | None:
    """One chat's reading, None when its size is not known.

    A compact writes no size until the next message, so a just-compacted chat counts as
    cheap: all 352 real compacts seen on 2026-11-07 came back at 54k to 89k.
    """
    if context:
        return ChatUse(size_level(context), round(context, -3), share, scale)
    return ChatUse("cheap", None, share, scale) if compacted else None


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp()


def _count(usage: Mapping, name: str) -> int:
    value = usage.get(name)
    return value if isinstance(value, int) else 0


@dataclass
class _Call:
    path: Path
    at: float
    share: float  # before the account's week_scale


def _share(usage: Mapping) -> float:
    return (
        _count(usage, "cache_read_input_tokens") * READ
        + _count(usage, "cache_creation_input_tokens") * WRITE
        + _count(usage, "input_tokens") * INPUT
        + _count(usage, "output_tokens") * OUTPUT
    )


class ChatUseMeter:
    """Model calls of the last day, per transcript, read a file's new bytes at a time."""

    def __init__(self) -> None:
        self._offsets: dict[Path, int] = {}
        # One entry per model call: streamed rows repeat a call, and a resumed chat copies
        # its history into a new file, which must not count it twice.
        self._calls: dict[tuple, _Call] = {}

    def _read_new(self, path: Path, now: float) -> None:
        try:
            stat = path.stat()
        except OSError:
            return
        offset = self._offsets.get(path)
        if offset is None and now - stat.st_mtime > WINDOW_S:
            self._offsets[path] = stat.st_size  # nothing in it can count
            return
        offset = offset or 0
        if stat.st_size < offset:  # rewritten: what was counted from it may be gone
            self._calls = {key: call for key, call in self._calls.items() if call.path != path}
            offset = 0
        if stat.st_size == offset:
            return
        try:
            with path.open("rb") as lines:
                lines.seek(offset)
                for line in lines:
                    if not line.endswith(b"\n"):  # still being written: the next pass reads it
                        break
                    if b'"usage"' in line:
                        self._read_line(line, path, offset, now)
                    offset += len(line)
        except OSError:
            pass
        self._offsets[path] = offset

    def _read_line(self, line: bytes, path: Path, offset: int, now: float) -> None:
        try:
            row = json.loads(line)
        except ValueError:
            return
        message = row.get("message") if isinstance(row, dict) else None
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            return
        usage = message.get("usage")
        at = _timestamp(row.get("timestamp"))
        if not isinstance(usage, dict) or at is None or now - at > WINDOW_S:
            return
        ids = (message.get("id"), row.get("requestId"))
        key = ids if any(ids) else (path, offset)
        call = self._calls.get(key)
        if call is None:
            self._calls[key] = _Call(path, at, _share(usage))
        elif call.path == path:
            # A later row of the same call carries its final output count.
            call.share = max(call.share, _share(usage))

    def measure(
        self, transcripts: Mapping[str, tuple[Path, float]], *, now: float
    ) -> dict[str, float]:
        """Each chat's share of the week in the last day, in percent, to what a reader sees:
        ``transcripts`` maps a session key to (file, its week scale)."""
        for path, _scale in transcripts.values():
            self._read_new(path, now)
        self._calls = {key: call for key, call in self._calls.items() if now - call.at <= WINDOW_S}
        totals: dict[Path, float] = {}
        for call in self._calls.values():
            totals[call.path] = totals.get(call.path, 0.0) + call.share
        return {
            key: round(share, 1)
            for key, (path, scale) in transcripts.items()
            if (share := totals.get(path, 0.0) * scale) > 0
        }
