"""Normalization for chat titles authored by sessions themselves."""

from __future__ import annotations

import hashlib
import json
import re

from .models import Session

MAX_TITLE_CHARS = 80
_SPACE_RE = re.compile(r"\s+")
_MODE_SUFFIX_RE = re.compile(r"\s+\((?:review|merge-fix|merge-arm|resume)\)$", re.I)


def _clean_text(value: str | None) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def title_evidence_signature(session: Session) -> str:
    """Identity of user intent recorded alongside an agent-authored title."""
    evidence = {
        "first": _clean_text(session.initial_prompt),
        "latest": _clean_text(session.last_prompt),
    }
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_agent_title(session: Session | None, value: object) -> str | None:
    """Clean a title while leaving project/issue identity to AppState.

    ``session`` is ``None`` for a chat not scanned yet (#2627): its issue is
    unknown, so an issue-shaped prefix is stripped anyway, and the project-name
    prefix is left alone because the project is unknown too.
    """
    if not isinstance(value, str):
        return None
    title = _clean_text(value).strip(" \t\r\n\"'“”‘’")
    if session is None or session.primary_issue is not None:
        title = re.sub(r"^[A-Za-z0-9_.-]+#\d+\s*(?:·|[-:])\s*", "", title)
        title = _MODE_SUFFIX_RE.sub("", title)
    if session is not None and session.project_name:
        title = re.sub(
            rf"^{re.escape(session.project_name)}\s*(?:·|[-:])\s*",
            "",
            title,
            count=1,
            flags=re.IGNORECASE,
        )
    if not title:
        return None
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1].rstrip(" .,:;-") + "…"
    return title
