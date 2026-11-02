"""Move a chat's page to another chat, and back, by asking (#3816, #3857).

In voice mode the owner asks the chat they are in to jump to another chat ("open the
chat on #3814"). That chat finds it (``GET /api/view/chats`` lists them) and calls
``POST /api/view/open-chat``, naming itself as ``from``; the deck chat may leave it out.
The jump is published on that chat's own jump topic (``jump_topic``), which every
stream showing a chat subscribes to for its chat alone, so only pages showing the chat
that asked move; a page showing anything else is left alone. The page opens the chat
in place (``static/chat_jump.js``), and voice mode carries on there as it does into any
chat opened (#3548), once the asking chat's reply has been read.

The way back is the fast agent's (``voice_helper``, by the owner's decision on #3816):
in any other chat it spots a request to go back to the deck chat, in any words, and the
page opens the deck chat without sending the words to the chat it is in.

A jump is sent only when it is published, never as a stream's prime: a page that
reconnects later must not be moved by an old request. A page not connected at that
moment misses it; the owner can ask again.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

JUMP_TOPIC = "chat_jump"
# A stream that renders a jump later than this (a page stalled mid-burst) drops it.
FRESH_S = 60.0
# How many chats ``GET /api/view/chats`` lists, newest first.
MAX_LISTED = 60


@dataclass(frozen=True)
class ChatJump:
    id: str
    # The chat page's own address, as the deck links it.
    path: str
    title: str
    at: float


def jump_topic(chat: str) -> str:
    """The topic a jump away from ``chat`` (its ``chat_identity_key``) is published on."""
    return f"{JUMP_TOPIC}:{chat}"


class ChatJumps:
    def __init__(self, bus) -> None:
        self._bus = bus
        # Each chat's latest jump, by its ``chat_identity_key``.
        self._latest: dict[str, ChatJump] = {}

    def jump(self, chat: str, path: str, title: str) -> ChatJump:
        """Move every page showing ``chat`` to ``path``."""
        now = time.monotonic()
        jump = ChatJump(uuid.uuid4().hex, path, title, now)
        self._latest = {key: kept for key, kept in self._latest.items() if now - kept.at <= FRESH_S}
        self._latest[chat] = jump
        self._bus.publish(jump_topic(chat))
        return jump

    def fresh(self, chat: str) -> ChatJump | None:
        jump = self._latest.get(chat)
        if jump is None or time.monotonic() - jump.at > FRESH_S:
            return None
        return jump

    def listening(self, chat: str) -> int:
        """How many streams showing ``chat`` would hear a jump now."""
        return self._bus.subscribers(jump_topic(chat))


def work_matches(cards, ref: str) -> list:
    """The Work cards ``ref`` names: its Work id, ``owner/repo#n``, ``repo#n``, ``#n`` or
    ``n``, in any case. A bare number can name a Work in several projects."""
    wanted = ref.strip().casefold()
    want_repo, _, want_number = wanted.rpartition("#")
    found = []
    for card in cards:
        work_id = card.work_id.casefold()
        body = work_id.split(":", 1)[-1]
        owner_repo, _, number = body.rpartition("#")
        if wanted in (work_id, body) or (
            want_number
            and number == want_number
            and want_repo in ("", owner_repo, owner_repo.rpartition("/")[2])
        ):
            found.append(card)
    return found
