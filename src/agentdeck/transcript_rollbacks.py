"""Transcript lines a person rolled out of a chat (#3474).

A rollback takes one of the person's messages, and everything after it, out of the
chat. Claude does its half by branching the same transcript file at the line before
that message, so the removed lines stay in the file. The deck records them in the
``transcript_rollbacks`` table, and every transcript read of that leg leaves them out
through ``HIDDEN``, which the provider consults. The web process loads it from the store
when its state is built and adds to it on each rollback.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from .models import TranscriptEvent

E = TypeVar("E", bound=TranscriptEvent)


class HiddenLines:
    """Per leg, the inclusive ``(first, last)`` line ranges a rollback removed."""

    def __init__(self) -> None:
        self._ranges: dict[str, tuple[tuple[int, int], ...]] = {}

    def replace_all(self, rows: Iterable[tuple[str, int, int]]) -> None:
        ranges: dict[str, list[tuple[int, int]]] = {}
        for session_key, first, last in rows:
            ranges.setdefault(session_key, []).append((first, last))
        self._ranges = {key: tuple(sorted(found)) for key, found in ranges.items()}

    def add(self, session_key: str, first: int, last: int) -> None:
        held = set(self._ranges.get(session_key, ()))
        held.add((first, last))
        self._ranges[session_key] = tuple(sorted(held))

    def hides_any(self, session_key: str) -> bool:
        return session_key in self._ranges

    def hides(self, session_key: str, seq: int) -> bool:
        return any(first <= seq <= last for first, last in self._ranges.get(session_key, ()))

    def visible(self, session_key: str, events: list[E]) -> list[E]:
        """``events`` without the ones a rollback removed; the same list when none were."""
        if session_key not in self._ranges:
            return events
        return [event for event in events if not self.hides(session_key, event.seq)]


HIDDEN = HiddenLines()
