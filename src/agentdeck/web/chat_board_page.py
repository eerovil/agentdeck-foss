"""The public edition's stand-in for the chat board page (#3623)."""

from __future__ import annotations

PUBLIC_STUB = True


def chat_board_context(request, **kwargs) -> dict:
    return {"request": request, "enabled": False}
