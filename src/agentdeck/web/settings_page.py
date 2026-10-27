"""Context for `/settings`, the deck-wide (not account-scoped) settings page.

Account defaults, calibration and login stay on `/accounts/{key}`; this page owns
the settings that belong to the deck itself. Today that is the ⚡ quick-phrase
vocabulary (#743).

The editor submits the whole list every time, so a row is a position rather than
a record: emptying both of a row's fields deletes that phrase, and the blank rows
under the saved ones are how a new phrase is added. That is what lets the page
work with no JavaScript at all, which matters because it is reached from a phone.
"""

from __future__ import annotations

from fastapi import Request
from starlette.datastructures import FormData

from ..composer_phrases import (
    MAX_LABEL_CHARS,
    MAX_PHRASES,
    MAX_TEXT_CHARS,
    ComposerPhrase,
    composer_phrases,
)
from .deps import get_db

# Spare rows offered under the saved list, so adding a phrase costs no round trip.
BLANK_PHRASE_ROWS = 2


def phrase_edit_rows(phrases: tuple[ComposerPhrase, ...]) -> list[ComposerPhrase]:
    """The saved phrases plus a couple of empty rows to grow the list into."""
    rows = list(phrases)
    spare = max(0, min(BLANK_PHRASE_ROWS, MAX_PHRASES - len(rows)))
    return rows + [ComposerPhrase("", "")] * spare


def submitted_phrase_rows(form: FormData) -> list[ComposerPhrase]:
    """Read the posted rows back in the order they were rendered.

    Indexes are the rendered positions, so a form that skips one (a browser that
    dropped a field, a hand-built post) still yields a well-ordered list.
    """
    indexes = sorted(
        int(key[len("label__") :])
        for key in form
        if key.startswith("label__") and key[len("label__") :].isdigit()
    )
    return [
        ComposerPhrase(
            str(form.get(f"label__{index}") or ""),
            str(form.get(f"text__{index}") or ""),
        )
        for index in indexes
    ]


def composer_phrase_context(
    request: Request,
    *,
    rows: list[ComposerPhrase] | None = None,
    error: str | None = None,
    saved: str | None = None,
) -> dict[str, object]:
    """Render context for the quick-phrase editor.

    ``rows`` is passed only when re-rendering a refused edit: the fields must come
    back carrying what was typed, not the list still stored in the database.
    """
    db = get_db(request)
    stored = db.load_composer_phrases()
    # The editor always shows what the composer is actually offering, so an
    # uncustomized deck starts from the built-ins rather than from blank rows —
    # tweaking one of them is the likeliest first edit.
    live = composer_phrases(db)
    return {
        "phrase_rows": phrase_edit_rows(live) if rows is None else rows,
        "phrases_customized": bool(stored),
        "phrase_count": len(live),
        "phrase_label_max": MAX_LABEL_CHARS,
        "phrase_text_max": MAX_TEXT_CHARS,
        "phrase_max": MAX_PHRASES,
        "phrase_error": error,
        "phrase_saved": saved,
    }


def settings_context(request: Request) -> dict[str, object]:
    return {
        **composer_phrase_context(request),
        "guessing_enabled": get_db(request).load_guessing_enabled(),
    }
