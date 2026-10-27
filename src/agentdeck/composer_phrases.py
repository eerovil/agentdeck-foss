"""One-tap steering phrases for the chat composer.

The deck is used mostly from a phone, where the recurring messages are short
steering nudges rather than new instructions — the transcripts are full of
"status", "ok", "continue", "do it", "try again". Typed that tersely they are
also the messages agents most often misread: "ok" alone does not say whether to
implement, report, or stop.

So the button's label stays as short as the word people would have typed, while
the text it actually sends spells the intent out. Keep this list short and
provider-neutral: it is a steering vocabulary, not a command palette (the
composer's slash/skill autocomplete already covers those).

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
    ComposerPhrase(
        "Status",
        "Status check, no new work: in a few sentences tell me what you have "
        "finished, what you are doing right now, and what is still left.",
    ),
    ComposerPhrase(
        "Go ahead",
        "Yes, go ahead with that. Implement it fully, verify it yourself, and "
        "report back when it is done — don't stop to confirm each step.",
    ),
    ComposerPhrase(
        "Continue",
        "Continue from where you left off and finish the remaining work.",
    ),
    ComposerPhrase(
        "Explain",
        "Explain what you just did and why, in plain language. Don't change any "
        "code while answering this.",
    ),
    ComposerPhrase(
        "Test it",
        "Run the relevant tests now and show me the real output. Report any "
        "failure verbatim instead of summarizing it.",
    ),
    ComposerPhrase(
        "Try again",
        "That didn't work. Find the actual cause before changing anything, then "
        "take a different approach and tell me what you changed.",
    ),
    ComposerPhrase(
        "Push & PR",
        "Commit what you have, push the branch, and open a pull request "
        "describing the change. Never push to the main branch directly.",
    ),
    ComposerPhrase(
        "Wrap up",
        "Finish what is in flight and then stop. Summarize the outcome, what is "
        "left undone, and anything I need to decide.",
    ),
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
        raise ComposerPhraseError(
            "Keep at least one phrase, or press Reset to defaults."
        )
    if len(phrases) > MAX_PHRASES:
        raise ComposerPhraseError(
            f"That is {len(phrases)} phrases; keep it to {MAX_PHRASES} or fewer."
        )
    folded = [phrase.label.casefold() for phrase in phrases]
    duplicate = next(
        (label for label in folded if folded.count(label) > 1), None
    )
    if duplicate is not None:
        raise ComposerPhraseError(
            "Two phrases share a label; each chip needs its own."
        )
    return tuple(phrases)


def composer_phrases(db) -> tuple[ComposerPhrase, ...]:
    """The phrases the composer offers: this deck's saved set, else the built-ins."""
    return db.load_composer_phrases() or DEFAULT_COMPOSER_PHRASES
