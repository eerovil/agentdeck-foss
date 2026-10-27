"""The public edition's stand-in for the ChatGPT chat board (#3623).

The author's deck reads a board of ChatGPT web chats from a private repository. The
public copy leaves that feature out; this keeps the names the rest of the deck imports,
and reports the board as switched off, a state the deck already handles.
"""

from __future__ import annotations

from dataclasses import dataclass

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
