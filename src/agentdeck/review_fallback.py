"""The public edition's stand-in for the Claude/Codex stand-in reviewer (#3623).

That reviewer works the ChatGPT review chats the public copy leaves out, so here it is
always off and its switch refuses to turn on.
"""

from __future__ import annotations

from pathlib import Path

PUBLIC_STUB = True
DEFAULT_HOURS = 6.0
KINDS = {"claude_code": "Claude", "codex": "Codex"}
REOPEN_WITHIN_S = 3600.0
_OFF = "this edition has no review chats for a stand-in reviewer to work"


class SwitchError(ValueError):
    """Why the switch could not be set."""


class ReviewFallbackService:
    """Always off."""

    enabled = False

    def __init__(self, *args, **kwargs) -> None:
        return None

    def bind(self, **kwargs) -> None:
        return None

    def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def active(path: Path, now: float | None = None):
    return None


def awaiting_reopen(board, *, now: float) -> list[tuple[int, str, float]]:
    return []


def busy_refusal(note: str) -> bool:
    return False


def deck_hooks(config, **kwargs) -> dict:
    return {}


def is_reviewer_prompt(prompt: str | None) -> bool:
    return False


def limit_to(path: Path, kind: str, until: float, *, by: str = "", now: float | None = None):
    raise SwitchError(_OFF)


def names(config, *, only: str = "") -> str:
    return ""


def parse_until(text: str, *, now: float | None = None) -> float:
    raise SwitchError(_OFF)


def starred_check(state, is_starred):
    return lambda repository, number: False


def start_failed_on(row, start) -> str:
    return ""


def switch_path(config) -> Path:
    return Path("/nonexistent/agentdeck-review-fallback")


def turn_off(path: Path) -> bool:
    return False


def turn_on(path: Path, until: float, *, by: str = "", now: float | None = None):
    raise SwitchError(_OFF)
