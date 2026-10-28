"""One-tap steering phrases for the chat composer.

The deck is used mostly from a phone, where the recurring messages are short
steering nudges rather than new instructions — the transcripts are full of
"status", "ok", "continue", "do it", "try again". Typed that tersely they are
also the messages agents most often misread: "ok" alone does not say whether to
implement, report, or stop.

So the button's label stays as short as the word people would have typed. The
built-ins are the owner's own set, used daily (#3623, owner's choice): some spell
the intent out, and a few ("Continue", "Try again") send the word itself, because
in a Work's chat the agent already knows what it was doing. Keep the list short:
it is a steering vocabulary, not a command palette (the composer's slash/skill
autocomplete already covers those).

What ships here is the *starting* vocabulary: `/settings` edits the list, and a
saved set replaces the built-ins whole (#743). Saving an empty set is refused so
that "no rows stored" keeps exactly one meaning — nobody has customized this
deck — and Reset to defaults stays the single way back to these.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ComposerPhrase:
    label: str
    text: str


DEFAULT_COMPOSER_PHRASES: tuple[ComposerPhrase, ...] = (
    ComposerPhrase("Status", "Status check"),
    ComposerPhrase("Progress", "Can we make this progress? Ask me questions if you have any."),
    ComposerPhrase("Continue", "Continue"),
    ComposerPhrase("Bullets", "Post bullets points on what you have done or decided"),
    ComposerPhrase(
        "Ask",
        "Ask me here. Give full briefing on related facts, I don't remember anything from "
        "codebase or past issues.",
    ),
    ComposerPhrase("Try again", "Try again"),
    ComposerPhrase(
        "Good spec", "I approve the spec. Remove possible spec-only label and start implementing."
    ),
    ComposerPhrase("Review", "Run review-fix-pr. If you only have a spec, run it on the spec."),
)

# A chip has to stay thumb-sized on a phone, so the label is capped hard; the
# built-ins above stay under 12 characters as the exemplars of that. The message
# is a steering nudge rather than a brief, and more than two dozen chips no
# longer read as a vocabulary on a phone-width panel.
MAX_LABEL_CHARS = 16
MAX_TEXT_CHARS = 1000
MAX_PHRASES = 24


class ComposerPhraseError(ValueError):
    """A refused edit, carrying the sentence the settings page shows verbatim."""


def normalize_composer_phrases(
    rows: Iterable[tuple[str, str]],
) -> tuple[ComposerPhrase, ...]:
    """Validate one submitted list; a row with both fields cleared is a deletion.

    The whole list is submitted every time, so the editor needs no per-row
    delete action: emptying a row drops it, and the trailing blank rows are how
    a new phrase is added.
    """
    phrases: list[ComposerPhrase] = []
    for raw_label, raw_text in rows:
        # A chip is one line whatever was pasted into the label field.
        label = " ".join(raw_label.split())
        text = raw_text.strip()
        if not label and not text:
            continue
        if not label:
            raise ComposerPhraseError("Give every phrase a short button label.")
        if not text:
            raise ComposerPhraseError(f"“{label}” has no message to send.")
        if len(label) > MAX_LABEL_CHARS:
            raise ComposerPhraseError(
                f"Keep every label to {MAX_LABEL_CHARS} characters or fewer "
                f"so the chips stay tappable — “{label}” is longer."
            )
        if len(text) > MAX_TEXT_CHARS:
            raise ComposerPhraseError(
                f"Keep every message to {MAX_TEXT_CHARS} characters or fewer "
                f"— the one under “{label}” is longer."
            )
        phrases.append(ComposerPhrase(label, text))
    if not phrases:
        raise ComposerPhraseError("Keep at least one phrase, or press Reset to defaults.")
    if len(phrases) > MAX_PHRASES:
        raise ComposerPhraseError(
            f"That is {len(phrases)} phrases; keep it to {MAX_PHRASES} or fewer."
        )
    folded = [phrase.label.casefold() for phrase in phrases]
    duplicate = next((label for label in folded if folded.count(label) > 1), None)
    if duplicate is not None:
        raise ComposerPhraseError("Two phrases share a label; each chip needs its own.")
    return tuple(phrases)


def composer_phrases(db) -> tuple[ComposerPhrase, ...]:
    """The phrases the composer offers: this deck's saved set, else the built-ins."""
    return db.load_composer_phrases() or DEFAULT_COMPOSER_PHRASES
