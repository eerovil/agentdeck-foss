"""What a Work is called and what it decided, owned by the Work itself (#2328).

Two pieces of Work metadata live in the control DB's ``work_metadata`` table, one row per Work,
so they survive a restart and are queryable from SQL rather than rebuilt from chat history:

- **A custom title.** The operator may rename a Work on its page. The name is this deck's own:
  the GitHub issue keeps its title. With none set, the Work is called what ``work_title`` has
  always derived (the issue's cached title, then a chat's), and clearing the name restores that.
- **The resolution**, a short engineering decision record in four parts: the problem and its
  root cause, the solution, why this solution rather than the alternatives, and why it is
  believed correct. The worker that runs the final verification writes it while the suite
  runs, and writes it again when the result is known or a fix changes the solution. Every
  write replaces the whole record, so a retried write or a later round never leaves a stale
  or duplicate summary behind; the Run and chat that wrote it last are kept as provenance.
  The page shows each part in full, broken into its paragraphs and bullet lists (#2459).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# Long enough for a real issue title, short enough to stay one line on a phone.
MAX_TITLE = 200
# Each part of the resolution is a few paragraphs at most; this bounds a runaway write.
MAX_PART = 8000

# The four parts, in the order the Work page shows them: (field, heading).
PARTS = (
    ("problem", "Problem"),
    ("solution", "Solution"),
    ("rationale", "Why this solution"),
    ("correctness", "Why we believe it is correct"),
)


@dataclass(frozen=True)
class WorkResolution:
    work_id: str
    problem: str
    solution: str
    rationale: str
    correctness: str
    updated_at: datetime | None = None
    run_id: str = ""
    session_key: str = ""

    def parts(self) -> tuple[tuple[str, str, str], ...]:
        """(field, heading, text) for every part, in page order."""
        return tuple((name, heading, getattr(self, name)) for name, heading in PARTS)

    def display_parts(self) -> tuple[tuple[str, str, tuple[Block, ...]], ...]:
        """(field, heading, blocks) for every part, in page order, as the Work page shows it."""
        return tuple(
            (name, heading, resolution_blocks(text)) for name, heading, text in self.parts()
        )


# One displayed block of a resolution part: ("p", text) or ("ul", (item, ...)).
Block = tuple[str, "str | tuple[str, ...]"]

_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_BULLET = re.compile(r"^[ \t]*[-*][ \t]+")


def resolution_blocks(text: str) -> tuple[Block, ...]:
    """Split one part into paragraphs and bullet lists (#2459), and nothing more.

    A blank line ends a paragraph, and a run of lines starting with ``- `` or ``* `` is a
    list. An indented line right after a bullet continues that bullet. No other markup is
    read, so the text stays plain and the template still escapes all of it.
    """
    blocks: list[Block] = []
    for chunk in _BLANK_LINE.split(str(text or "").replace("\r\n", "\n").strip()):
        lines: list[str] = []
        items: list[str] = []
        for line in chunk.split("\n"):
            bullet = _BULLET.match(line)
            if bullet and line[bullet.end():].strip():
                if lines:
                    blocks.append(("p", "\n".join(lines)))
                    lines = []
                items.append(line[bullet.end():].strip())
            elif items and line[:1] in (" ", "\t") and line.strip():
                items[-1] = f"{items[-1]} {line.strip()}"
            elif line.strip():
                if items:
                    blocks.append(("ul", tuple(items)))
                    items = []
                lines.append(line.strip())
        if lines:
            blocks.append(("p", "\n".join(lines)))
        if items:
            blocks.append(("ul", tuple(items)))
    return tuple(blocks)


class ResolutionError(ValueError):
    """A resolution write that cannot be stored as asked."""


def normalize_title(value: object) -> str:
    """One line of at most ``MAX_TITLE`` characters; empty means "no custom title"."""
    text = " ".join(str(value or "").split())
    return text[:MAX_TITLE].rstrip()


def normalize_part(name: str, value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        raise ResolutionError(f"{name} must not be empty")
    if len(text) > MAX_PART:
        raise ResolutionError(f"{name} is longer than {MAX_PART} characters")
    return text


# What the Work's agent found when it checked its merged change live (#3141).
LIVE_VERIFIED = "verified"
LIVE_FAILED = "failed"
LIVE_NOTHING = "nothing_to_check"
# Merged, but some of it is not running yet, such as a repository this deck does not deploy
# (#3220): the Work stays open rather than reading as verified.
LIVE_NOT_LIVE = "not_live"
LIVE_RESULTS = (LIVE_VERIFIED, LIVE_FAILED, LIVE_NOTHING, LIVE_NOT_LIVE)
# One line on a card; the details go on the issue.
MAX_LIVE_NOTE = 500


@dataclass(frozen=True)
class LiveCheck:
    """The agent's live check of a merged Work (#3141): its result, one line, and when."""

    work_id: str
    result: str
    note: str = ""
    checked_at: datetime | None = None
    session_key: str = ""

    @property
    def line(self) -> str:
        """The result as a card shows it: ``checked live: <note>``."""
        if self.result == LIVE_NOTHING:
            return "nothing to check live" + (f": {self.note}" if self.note else "")
        if self.result == LIVE_FAILED:
            return "live check failed" + (f": {self.note}" if self.note else "")
        if self.result == LIVE_NOT_LIVE:
            return "not live yet" + (f": {self.note}" if self.note else "")
        return "checked live" + (f": {self.note}" if self.note else "")


def normalize_live_check(result: object, note: object) -> tuple[str, str]:
    """A known result and a one-line note, or ``ResolutionError``."""
    value = str(result or "").strip().casefold()
    if value not in LIVE_RESULTS:
        raise ResolutionError(f"result must be one of {', '.join(LIVE_RESULTS)}")
    text = " ".join(str(note or "").split())
    if len(text) > MAX_LIVE_NOTE:
        raise ResolutionError(f"note is longer than {MAX_LIVE_NOTE} characters")
    return value, text
