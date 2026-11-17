"""The public edition's stand-in for the ChatGPT chat board (#3623).

The author's deck reads a board of ChatGPT web chats from a private repository. The
public copy leaves that feature out; this keeps the names the rest of the deck imports,
and reports the board as switched off, a state the deck already handles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

PUBLIC_STUB = True
TOPIC = "chat_board"
ESCALATED = "escalated"
NEEDS_YOU_LABELS = ("needs-more-info", "blocked")


@dataclass(frozen=True)
class ChatRow:
    number: int
    title: str
    state: str = "open"
    labels: tuple[str, ...] = ()
    url: str = ""
    created_at: str = ""
    updated_at: str = ""
    closed_at: str = ""
    claimed_at: str = ""

    def closed_seconds_ago(self, *, now: float) -> float | None:
        at = _moment(self.closed_at or self.updated_at)
        return None if at is None else max(0.0, now - at.timestamp())

    def idle_for(self, *, now: float) -> float | None:
        at = _moment(self.updated_at)
        return None if at is None else max(0.0, now - at.timestamp())


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def verdict_lines(body: str) -> list[str]:
    return []


class ChatBoardService:
    """Always off: no board, nothing started."""

    def __init__(self, *args, **kwargs) -> None:
        self.enabled = False
        self.board = None

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None
