"""A bounded cache of the parsed transcript tail a detail open renders.

Opening a chat needs only the last ``DETAIL_WINDOW`` events plus whole-file
aggregates (token totals, event count, model), but ``read_events`` parses the
entire JSONL on every open — on the largest chats that parse dominates
detail-open latency (#791). Transcripts are append-only (a provider may touch
bookkeeping, but events only grow), so the reader's resumable byte cursor lets
a reopen parse just the new suffix while the aggregates accumulate.

The memory bound is explicit: at most ``max_entries`` transcripts are cached
(LRU), and each entry retains only the bounded tail window plus scalars, so a
deck with many large chats cannot grow the cache with transcript size.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..models import TokenTotals, TranscriptEvent
from .transcript_reader import TranscriptRead, token_totals


def _last_model(events: list[TranscriptEvent]) -> str | None:
    return next((ev.model for ev in reversed(events) if ev.model), None)


class ReadEvents(Protocol):
    def __call__(
        self, path: Path, *, byte_offset: int = 0, seq: int = 0
    ) -> TranscriptRead: ...


@dataclass
class TailRead:
    """The detail-window slice plus the whole-file aggregates it needs."""

    events: list[TranscriptEvent]
    tokens: TokenTotals
    model: str | None
    total_events: int
    skipped: int


@dataclass
class _TailState:
    signature: tuple[int, int]  # (size, mtime_ns) at the cursor
    byte_offset: int
    seq: int
    window: list[TranscriptEvent]
    tokens: TokenTotals
    model: str | None
    total_events: int
    skipped: int


@dataclass
class TranscriptTailCache:
    """Path-keyed, LRU-bounded incremental tail reader."""

    read_events: ReadEvents
    window_size: int
    max_entries: int = 8
    _store: OrderedDict[str, _TailState] = field(default_factory=OrderedDict)

    def read_tail(self, path: Path) -> TailRead:
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            self._store.pop(key, None)
            return self._full_read(key, path)
        signature = (stat.st_size, stat.st_mtime_ns)
        state = self._store.get(key)
        if state is not None:
            if state.signature == signature:
                self._store.move_to_end(key)
                return self._as_read(state)
            if stat.st_size > state.byte_offset:
                # Grown: parse only the appended suffix. A same-size or shrunk
                # file is not an append and falls through to a full reparse.
                read = self.read_events(
                    path, byte_offset=state.byte_offset, seq=state.seq
                )
                self._advance(state, read, signature)
                self._store.move_to_end(key)
                return self._as_read(state)
            self._store.pop(key, None)
        return self._full_read(key, path)

    def _full_read(self, key: str, path: Path) -> TailRead:
        read = self.read_events(path)
        state = _TailState(
            signature=(0, 0),
            byte_offset=0,
            seq=0,
            window=[],
            tokens=TokenTotals(),
            model=None,
            total_events=0,
            skipped=0,
        )
        try:
            stat = path.stat()
        except OSError:
            # Unstattable files are served but never cached; the next read
            # starts over rather than resuming from an unverifiable cursor.
            self._advance(state, read, None)
            return self._as_read(state)
        self._advance(state, read, (stat.st_size, stat.st_mtime_ns))
        self._store[key] = state
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)
        return self._as_read(state)

    def _advance(
        self,
        state: _TailState,
        read: TranscriptRead,
        signature: tuple[int, int] | None,
    ) -> None:
        new = read.events
        state.window = (state.window + new)[-self.window_size :]
        added = token_totals(new)
        state.tokens = TokenTotals(
            input_tokens=state.tokens.input_tokens + added.input_tokens,
            output_tokens=state.tokens.output_tokens + added.output_tokens,
            cache_read_tokens=state.tokens.cache_read_tokens + added.cache_read_tokens,
            cache_creation_tokens=state.tokens.cache_creation_tokens
            + added.cache_creation_tokens,
        )
        state.model = _last_model(new) or state.model
        state.total_events += len(new)
        state.skipped += read.skipped
        state.byte_offset = read.byte_offset
        state.seq = read.seq
        if signature is not None:
            state.signature = signature

    @staticmethod
    def _as_read(state: _TailState) -> TailRead:
        return TailRead(
            # A copy: callers decorate/filter the list, and the cached window
            # must stay the plain parse.
            events=list(state.window),
            tokens=state.tokens,
            model=state.model,
            total_events=state.total_events,
            skipped=state.skipped,
        )
