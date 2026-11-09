"""Whether to ask before a message wakes a costly chat whose cache has gone (#4169).

A chat with a large context is cheap to answer while its prompt cache lives and costly
once it has gone: the next message writes the whole context to the cache again. The chat
page then asks first, and offers a new chat, the next part of the same chat, that starts
small and reads the old one in parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .chat_use import WRITE, percent
from .models import Capability, Session


@dataclass(frozen=True)
class ColdChat:
    cost: str  # what the next message costs, in % of the week
    # Which cache this asks about: a "Wake it" answer holds until it changes, which is
    # when the chat has had a new turn.
    stamp: str


def cold_chat(
    session: Session,
    *,
    now: datetime,
    busy: bool,
    question_open: bool,
    can_continue: bool,
) -> ColdChat | None:
    """The question to ask before the next message, None when there is none.

    ``session`` is a display copy, the one that carries ``chat_use``. Nothing is asked
    while a turn runs (a message then steers it, and the cache is warm), while the chat
    asks a question of its own, or when the new chat could not start.
    """
    use = session.chat_use
    expires = session.cache_expires_at
    if (
        not can_continue
        or busy
        or question_open
        or Capability.STEER in session.capabilities
        or use is None
        or use.level != "expensive"
        or use.context is None
        or expires is None
        or now < expires
    ):
        return None
    return ColdChat(percent(use.context * WRITE * use.scale), expires.isoformat())
