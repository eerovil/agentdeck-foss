"""Deterministic, bounded rendering for a context-rollover handoff.

The renderer is intentionally pure.  Callers collect transcript and workspace
facts separately, off the request event loop, before any successor reservation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

MARKER = "AGENTDECK CONTEXT HANDOFF v1"
_ASSIGNED_SECRET = re.compile(
    r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+|"
    r"(?:api[_-]?key|token|password|secret)\s*[=:]\s*)\S+"
)
_TOKEN_SECRET = re.compile(
    r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"sk-[A-Za-z0-9_-]{12,})\b"
)


@dataclass(frozen=True)
class HandoffMessage:
    role: str
    text: str


@dataclass(frozen=True)
class HandoffInput:
    why_continued: str
    logical_chat: str
    source_leg: str
    current_work: str
    workspace_snapshot: str = ""
    references: tuple[str, ...] = ()
    previous_summary: str = ""
    recent_messages: tuple[HandoffMessage, ...] = ()
    completed_verification: tuple[str, ...] = ()
    do_not_repeat: tuple[str, ...] = ()
    next_action: str = ""


def render_context_handoff(
    value: HandoffInput,
    *,
    max_chars: int = 16_000,
    recent_message_limit: int = 8,
    previous_summary_limit: int = 6_000,
) -> str:
    """Render fixed-order sections, dropping oldest history before truncating."""
    messages = value.recent_messages[-recent_message_limit:]
    sections = [
        ("Why continued", value.why_continued),
        ("Logical chat/source leg", f"{value.logical_chat}\n{value.source_leg}"),
        ("Current dispatched work", value.current_work),
        ("Workspace/git snapshot", value.workspace_snapshot),
        ("Issue/PR references", "\n".join(value.references)),
        (
            "Previous worker summary",
            _historical_summary(value.previous_summary[:previous_summary_limit])
            if value.previous_summary
            else "",
        ),
        (
            "Recent decisions/messages",
            "\n".join(_historical_message(m) for m in messages),
        ),
        ("Completed verification", "\n".join(value.completed_verification)),
        ("Do not repeat", "\n".join(value.do_not_repeat)),
        ("Next action", value.next_action),
    ]
    required = [sections[index] for index in (0, 1, 2, 9)]
    if len(_render(required)) > max_chars:
        raise ValueError("mandatory context handoff metadata exceeds handoff_max_chars")
    rendered = _render(sections)
    while len(rendered) > max_chars and messages:
        messages = messages[1:]
        sections[6] = (
            "Recent decisions/messages",
            "\n".join(_historical_message(m) for m in messages),
        )
        rendered = _render(sections)
    # Preserve the mandatory identity and next-action sections. Optional
    # evidence disappears in a deterministic order instead of letting a final
    # string slice cut the handoff into a misleading partial document.
    for index in (5, 3, 4, 7, 8):
        if len(rendered) <= max_chars:
            break
        sections[index] = (sections[index][0], "")
        rendered = _render(sections)
    if len(rendered) > max_chars:
        raise ValueError("context handoff cannot fit within handoff_max_chars")
    return rendered


def _render(sections: list[tuple[str, str]]) -> str:
    body = [MARKER]
    for title, content in sections:
        if content:
            body.extend(("", f"## {title}", _redact(content)))
    return "\n".join(body)


def _historical_message(message: HandoffMessage) -> str:
    role = escape(message.role, quote=True)
    text = "\n".join(
        f"| {line}" for line in escape(_redact(message.text), quote=False).splitlines()
    )
    return (
        f'<historical-message role="{role}" evidence-only="true">\n'
        f"{text}\n</historical-message>"
    )


def _historical_summary(summary: str) -> str:
    text = "\n".join(
        f"| {line}" for line in escape(_redact(summary), quote=False).splitlines()
    )
    return (
        '<historical-summary evidence-only="true">\n'
        f"{text}\n</historical-summary>"
    )


def _redact(value: str) -> str:
    return _TOKEN_SECRET.sub("[REDACTED]", _ASSIGNED_SECRET.sub(r"\1[REDACTED]", value))
