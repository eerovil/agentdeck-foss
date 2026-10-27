"""The public edition's stand-in for the chat board's routes (#3623): no pages."""

from __future__ import annotations

from fastapi import APIRouter

from ..chat_review import ReviewError

PUBLIC_STUB = True
router = APIRouter()


def chat_board_of(app) -> object:
    return getattr(app.state, "chat_board", None)


def releases_of(app):
    return None


def note_review_head(app, outcome) -> str:
    return ""


async def request_review(app, target: str, *, work: str = "", note: str = "", again: bool = False):
    raise ReviewError("this edition has no review chats; press Merge on the Work instead")
