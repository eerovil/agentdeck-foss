"""Safe formatting helpers for Codex runtime diagnostics."""

from __future__ import annotations

import re
from types import TracebackType

_SECRET_PATTERN = re.compile(
    r"(?P<authorization>\bauthorization\s*[=:]\s*(?:(?:Bearer|Basic)\s+)?)"
    r"[^\s,;]+"
    r"|(?P<auth>\b(?:Bearer|Basic)\s+)[A-Za-z0-9+/=._\-]+"
    r"|(?P<assignment>\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|cookie)\s*[=:]\s*)[^\s,;]+"
    r"|(?:sk-(?:ant-)?[A-Za-z0-9._\-]+|github_pat_[A-Za-z0-9_]+|"
    r"gh[pousr]_[A-Za-z0-9_]+|glpat-[A-Za-z0-9_\-]+|"
    r"xox[baprs]-[A-Za-z0-9_\-]+|AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)


def redact_diagnostic(value: str) -> str:
    """Remove common credential forms before writing provider output to logs."""

    def replacement(match: re.Match[str]) -> str:
        prefix = (
            match.group("authorization")
            or match.group("auth")
            or match.group("assignment")
            or ""
        )
        return f"{prefix}[redacted]"

    return _SECRET_PATTERN.sub(replacement, value)


def redacted_exc_info(
    exc: Exception,
) -> tuple[type[RuntimeError], RuntimeError, TracebackType | None]:
    """Preserve traceback frames while replacing arbitrary exception text."""
    safe = RuntimeError(f"{type(exc).__name__}: {redact_diagnostic(str(exc))}")
    return RuntimeError, safe, exc.__traceback__
