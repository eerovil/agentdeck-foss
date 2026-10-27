"""Hidden Works: the operator's "not now" on a Needs you row (#2238).

Hide is a display preference of this deck and nothing else. It takes a Work out of the front
page's **Needs you** and puts it on the Hidden list (``/works?view=hidden``); the source issue,
the Poller card, its Runs and every other section are untouched, so a hidden Work that becomes
Active or finishes shows there as usual.

A hide lasts until something new happens on the Work, read from two marks taken when it was
hidden and compared on every overview pass:

- **Any new comment on the source issue.** ``comments`` is the issue's comment count from its
  cached record (``kanban._ISSUE_JQ``; the webhook write-through keeps it current on
  ``issue_comment``). A higher count expires the hide; what the comment says is never read.
  A lower count (a deleted comment) lowers the stored baseline, so a comment after it counts.
- **The Poller card moved.** ``card_mark`` is a fingerprint of the card facts that mean the
  board really changed (``card_mark`` below), never of timestamps the engine rewrites on
  every look.
- **A chat carried on in a finished Work after it was archived** (#2357). Archive on a
  Recently finished row is this same hide; ``continued`` is when a chat moved well after the
  Work finished (``work_overview.continued_at``), and one later than ``hidden_at`` expires it.

Missing data never brings a Work back: a record without a count, or no card at all, keeps the
hide as it is. A card appearing where there was none when the Work was hidden is a change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# The Bring back action id; Hide is ``work_attention.HIDE``, offered on every Needs you row.
UNHIDE = "unhide"


@dataclass(frozen=True)
class HiddenWork:
    """One hidden Work and the marks it was hidden under; ``None`` is "not known then"."""

    work_id: str
    hidden_at: str
    comments: int | None = None
    card_mark: str | None = None


def issue_comments(record: Any) -> int | None:
    """The source issue's comment count from its cached record, or None when not known."""
    value = record.get("comments") if isinstance(record, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def card_mark(card: Any) -> str | None:
    """The Poller card's board state in one string, or None without a card.

    Only what moves when the board materially changes: the column (by role, else its
    spelling), done, the lease generation, the outstanding dispatch, a spawn that never
    landed, the engine's durable intent to hand the release to a person
    (``release_blocked``) and the park that follows it while current (``release_parked``),
    and how many times the engine restarted the worker. Never ``last_live_check``,
    ``started_at`` or any other stamp.
    """
    if card is None:
        return None
    dispatch = getattr(card, "dispatch", None)
    parts = (
        str(getattr(card, "status_role", "") or getattr(card, "status", "") or ""),
        "done" if getattr(card, "done", False) else "",
        str(getattr(card, "generation", "") or ""),
        str(getattr(dispatch, "id", "") or "") if dispatch is not None else "",
        "dispatch_failed" if getattr(card, "dispatch_failed", False) else "",
        str(getattr(card, "release_blocked", "") or ""),
        str(getattr(card, "release_parked", "") or ""),
        str(int(getattr(card, "revives", 0) or 0)),
    )
    return "|".join(parts)


def still_hidden(
    hidden: HiddenWork,
    comments: int | None,
    mark: str | None,
    continued: datetime | None = None,
) -> bool:
    """Whether the hide still holds against the Work's current marks.

    ``comments`` and ``mark`` are the Work's now (``WorkCard.comments``/``card_mark``). A
    current value that is not known keeps the hide; so does a count the hide was taken
    without, since there is nothing to compare it against. ``continued`` (#2357) ends it
    when later than ``hidden_at``; an unreadable ``hidden_at`` keeps it.
    """
    if comments is not None and hidden.comments is not None and comments > hidden.comments:
        return False
    if continued is not None:
        try:
            hidden_at = datetime.fromisoformat(hidden.hidden_at)
        except (TypeError, ValueError):
            hidden_at = None
        if hidden_at is not None and hidden_at.tzinfo is not None and continued > hidden_at:
            return False
    return mark is None or mark == hidden.card_mark
