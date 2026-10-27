"""Ownership provenance (#176 slice 4).

The single persistent ``claude`` label means *ownership*, not activity: adding it
starts (or restarts) a card, removing it is a safe stop, and it stays on across a
worker's whole life. The hazard is state recovery — after the engine's durable
state is lost or rolled back, an old, long-standing label must **not** be
misread as a fresh start.

The engine guards against that with an ownership ledger recording, per card, the
id of the most recent ``claude`` *labeled* event it has accounted for. A card is a
genuine new start only when its latest labeled event id differs from the recorded
one (a remove + re-add produces a new labeled event). On first initialization the
ledger simply adopts the current world without classifying anything as a start.

Pure, stdlib-only operations over the ledger dict + label-event ids the caller
fetched. (The reference engine's one-time migration-baseline path for older pollers is
deliberately omitted: migrating existing pollers is out of scope for this map.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class LabelEvent:
    """The stable identity and GitHub time of one ownership-label addition."""

    id: str
    created_at: str = ""


@dataclass(frozen=True)
class LabelRemoval:
    """When the ownership label last came off an issue, and who took it off (#2417).

    Empty ``created_at`` means the issue's history was read and shows no such removal. An
    unreadable history is not this at all: the port answers ``None`` for that.

    GitHub stamps both a removal and a comment to the second, so two stamps cannot say which
    came first when they share one. The issue's timeline can: ``comments_before`` and
    ``comments_after`` are the ids of the comments it lists on each side of this removal, within
    the page that was read. A comment in neither falls back to its stamp.
    """

    created_at: str = ""
    actor: str = ""
    comments_before: frozenset[str] = frozenset()
    comments_after: frozenset[str] = frozenset()


def event_id(event: str | LabelEvent) -> str:
    """Return the durable id stored in the JSON ledger."""
    return event if isinstance(event, str) else event.id


def _event_parts(event: str | LabelEvent) -> tuple[str, datetime | None, bool]:
    if isinstance(event, str):
        return event, None, False
    try:
        stamp = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
    except ValueError:
        return event.id, None, True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return event.id, stamp.astimezone(UTC), True


def _scan_time(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def new_ledger() -> dict:
    return {"initialized": False, "events": {}}


def detect_starts(
    ledger: dict,
    card_events: dict[str, list[str | LabelEvent]],
    *,
    all_scanned_ok: bool = True,
    scanned_at: datetime | None = None,
) -> set[str]:
    """Update the ledger from freshly-scanned label events and return genuine new starts.

    ``card_events`` maps a card key (``repo#number``) to its ``claude`` labeled events
    in chronological order; a card the caller could not scan is simply absent, and the
    caller reports whether every managed card was scanned via ``all_scanned_ok``.

    Before the ledger is initialized nothing is classified as a start (an existing
    label is part of the adopted world), and it flips to initialized only after a
    fully successful scan, so a partial bootstrap safely retries. Once initialized,
    the time of each complete scan distinguishes a newly discovered label added since
    the preceding scan from old standing state that a damaged bootstrap omitted.
    """
    events = ledger.setdefault("events", {})
    initialized = bool(ledger.get("initialized"))
    previous_scan = _scan_time(ledger.get("last_complete_scan_at"))
    starts: set[str] = set()
    for card, event_ids in card_events.items():
        if not event_ids:
            continue
        latest, added_at, timestamped = _event_parts(event_ids[-1])
        known = card in events
        if (
            initialized
            and latest != events.get(card)
            and (
                known
                or not timestamped  # compatibility for legacy/test ports that report ids only
                or (
                    added_at is not None
                    and previous_scan is not None
                    and added_at >= previous_scan
                )
            )
        ):
            starts.add(card)
        events[card] = latest
    if not initialized and all_scanned_ok:
        ledger["initialized"] = True
    if all_scanned_ok and scanned_at is not None:
        ledger["last_complete_scan_at"] = scanned_at.astimezone(UTC).isoformat()
    return starts
