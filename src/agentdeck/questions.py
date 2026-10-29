"""Durable, deck-derived questions that remain after interactions clear."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from .models import TranscriptEvent

# Shortest fragment we'll treat as a real question (filters ternaries / "y?").
_QUESTION_MIN_LEN = 8
# Split on sentence terminators OR line breaks — a bulleted / colon-terminated
# block has no full stops, so a newline is often the only boundary before a
# trailing "So, what's next?".
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
# Markdown emphasis / quote / bracket characters that can wrap a question so it
# ends in "?**" or '?"' rather than a bare "?". Stripped from both ends before
# judging (and off the returned text) so a bolded question still registers.
_QUESTION_WRAP = "*_`~\"'”’)]}>"
# Pipe used as a markdown table cell delimiter (leading pipe, or " | " between
# cells). A table row an agent wrote *about* its own work reads like a question
# once the sentence split cuts it after a cell's "?" — "| Signal | Fires on
# client-side open? |" became a card question on a finished, merged chat (#352).
# The same shape catches a shell pipeline, which is code, not prose either.
_TABLE_CELL = re.compile(r"^\||\s\|\s")


# The keys are whole agent replies, so this holds message text rather than
# identifiers — but it has to cover every session the deck holds, not just the
# live ones. ``_derived_question`` asks this of *every* session on every
# liveness sweep, so a cache smaller than the deck's session count evicts each
# answer before the next sweep asks for it again and the memo never hits. At
# 512 against a 3585-session deck that was a full re-parse of every reply every
# ten seconds, measured as ~13% of the web worker's event-loop thread (#1310).
@lru_cache(maxsize=8192)
def trailing_question(text: str | None) -> str | None:
    """The last natural-language question in an agent message, or None.

    Heuristic: split into sentences (also on line breaks) and return the final
    one ending in ``?`` — skipping short or code-like fragments (a lone token, a
    ternary, a URL query string, a markdown table row) that end in a question
    mark without being a question. A trailing markdown wrapper (``**…?**``,
    ``_…?_``, ``"…?"``) is peeled off first, so an emphasised question is not
    missed. Used to surface "the agent is waiting on an answer" on the card.

    Memoized on the message text, which is the only thing it reads. The liveness
    sweep re-asks this of every session's newest reply every ten seconds — idle
    ones included, because the sweep derives a question for the whole account —
    and the reply only changes when the agent speaks, so almost every one of
    those regex splits was re-deriving an answer it already had (#1303).
    """
    if not text or "?" not in text:
        return None
    for part in reversed(_SENTENCE_SPLIT.split(text)):
        candidate = " ".join(part.split())  # flatten any internal whitespace
        if _TABLE_CELL.search(candidate):  # tabular/piped text is not prose
            continue
        core = candidate.rstrip(_QUESTION_WRAP)  # peel "…?**" -> "…?"
        # A real question ends in a word? (not a ternary's "x ?" or URL query).
        if core.endswith("?") and len(core) >= _QUESTION_MIN_LEN and core[-2] != " ":
            return core.lstrip(_QUESTION_WRAP + " ")
    return None


@dataclass(frozen=True)
class OpenQuestion:
    session_key: str
    question: str
    asked_at: datetime
    source: str
    asked_seq: int
    observed_seq: int
    answered_seq: int | None = None
    # Transcript sequence numbers are local to one concrete chat leg. The
    # question row stays attributed to the leg where it was asked, while this
    # key says which leg ``observed_seq`` belongs to after the chat moves.
    # ``None`` is the legacy shape and means ``session_key``.
    observed_session_key: str | None = None
    answered_session_key: str | None = None


def event_question(event: TranscriptEvent) -> str | None:
    return event.question or (trailing_question(event.text) if event.role == "assistant" else None)
