"""The public edition's stand-in for relaying answers to ChatGPT chats (#3623)."""

from __future__ import annotations

from pathlib import Path

PUBLIC_STUB = True


def answers_parked_card(*, db, issues, session_key: str) -> bool:
    return False


def leave_answer(
    *, db, issues, session_key: str, text: str, request_id, chat_url: str = ""
) -> Path | None:
    return None
