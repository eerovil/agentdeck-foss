"""The public edition's stand-in for the Mac command-approval routes (#3623): no pages."""

from __future__ import annotations

from fastapi import APIRouter

PUBLIC_STUB = True
router = APIRouter()


def session_context(request, session_key: str, *, error: str = "", pending: str = "") -> dict:
    return {"mac_runner": {"configured": False, "session_key": session_key, "approval_url": ""}}
