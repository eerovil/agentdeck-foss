"""Deck notes in a message's text (#4303): putting them in front, and taking them apart to
show it (#4618). A module of its own: the providers' parsers reach ``deck_notices``, which
needs this, and ``pending_events`` needs the providers."""

from __future__ import annotations

import re
from collections.abc import Sequence

#: The line that opens the notes in front of a message.
NOTES_HEADING = (
    "Deck notes that came while you were not woken (#4303), oldest first."
    " They need nothing from you unless one says so:"
)
#: Between the notes and the message they ride on.
NOTES_END = "--- The message itself follows. ---"


def with_notes(notes: Sequence[str], message: str) -> str:
    """``message`` with ``notes`` in front of it, or ``message`` alone when there are none."""
    if not notes:
        return message
    lines = [NOTES_HEADING, ""]
    for number, note in enumerate(notes, start=1):
        lines.append(f"{number}. {note.strip()}")
    return "\n".join([*lines, "", NOTES_END, "", message])


def split_notes(text: str) -> tuple[list[str], str]:
    """``with_notes`` undone: the notes in front of ``text``, and the message they ride on.

    For showing a message only (#4618). A note's own lines starting with the next number
    would read as one more note; its text is kept either way."""
    head, end, message = text.partition(f"\n\n{NOTES_END}")
    if not end or not head.startswith(f"{NOTES_HEADING}\n\n1. "):
        return [], text
    notes: list[str] = []
    for line in head[len(NOTES_HEADING) :].strip().split("\n"):
        numbered = re.match(r"(\d+)\. ", line)
        if numbered and int(numbered[1]) == len(notes) + 1:
            notes.append(line[numbered.end() :])
        else:
            notes[-1] += f"\n{line}"
    # A provider may strip the end of a message that is only an attached image.
    return notes, message.removeprefix("\n\n")


def without_notes(text: str) -> str:
    return split_notes(text)[1]
