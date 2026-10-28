"""Every change of a Work's front-page status, kept as a log (#2751).

The overview pass reclassifies every Work every few seconds and keeps only the newest
answer, so a Work that flashed into Needs you and straight back out left no trace. This
module turns each pass into the rows that *changed*: a Work's section (Needs you, Hidden,
Active, Finished, Quiet, Paused, or gone from the overview), its Needs you category, or its
status kind or tone. Each row carries the facts that decided it (``why``) and the sources
that failed during that pass (``degraded``), so a wrong or flapping status can be traced to
the fact that moved, a read that failed, or a deploy that changed the reading.

Nothing here changes how a status is decided; it is a diagnostic record of this deck only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .work_stops import facts_mark

# Rows older than this are dropped when the database opens.
RETENTION = timedelta(days=30)
# A Work that left the overview altogether: no longer built, so no section.
GONE = "gone"
HIDDEN = "hidden"
NEEDS_YOU = "needs_you"
# The chats listed in one row's ``why``; a Work rarely has more.
MAX_CHATS = 8
QUESTION_CHARS = 120


@dataclass(frozen=True)
class StatusPoint:
    """One Work's status as one pass saw it, and the one it replaced."""

    work_id: str
    at: datetime
    section: str
    category: str = ""
    kind: str = ""
    tone: str = ""
    headline: str = ""
    prev_section: str = ""
    prev_category: str = ""
    prev_kind: str = ""
    prev_tone: str = ""
    facts_mark: str | None = None
    why: Mapping[str, Any] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()
    pid: int | None = None
    revision: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        """What a row is written for: a headline alone changing is not a new status."""
        return (self.section, self.category, self.kind, self.tone)

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "at": self.at.isoformat(),
            "section": self.section,
            "category": self.category,
            "kind": self.kind,
            "tone": self.tone,
            "headline": self.headline,
            "prev_section": self.prev_section,
            "prev_category": self.prev_category,
            "prev_kind": self.prev_kind,
            "prev_tone": self.prev_tone,
            "facts_mark": self.facts_mark,
            "why": dict(self.why),
            "degraded": list(self.degraded),
            "pid": self.pid,
            "revision": self.revision,
        }


def why(card: Any, status: Any) -> dict[str, Any]:
    """The facts ``work_story`` reads a Work's section from, compactly; empty ones left out."""
    verification = getattr(card, "verification", None)
    chats = [
        {
            key: value
            for key, value in (
                ("session_key", item.session_key),
                ("state", item.state),
                ("asks", item.asks),
                ("question", (item.question or "")[:QUESTION_CHARS]),
                ("interaction", item.interaction),
                ("recovering", item.recovering),
            )
            if value
        }
        for item in tuple(getattr(card, "attention", ()))[:MAX_CHATS]
    ]
    found: dict[str, Any] = {
        "you": getattr(status, "you", ""),
        "working_count": getattr(card, "working_count", 0),
        "working_chats": [href for _title, href in getattr(card, "working_chats", ())],
        # Its chats work only on turns a background notification opened (#2760).
        "woke_itself": getattr(card, "woke_itself", False),
        "chats": chats,
        "work_state": getattr(card, "work_state", ""),
        "paused": getattr(card, "paused", False),
        "awaiting_finish": getattr(card, "awaiting_finish", False),
        "finished": getattr(card, "finished_at", None) is not None,
        "verification": getattr(verification, "status", ""),
    }
    return {key: value for key, value in found.items() if value}


def point(
    row: Any,
    *,
    at: datetime,
    hidden: bool = False,
    degraded: Sequence[str] = (),
    pid: int | None = None,
    revision: str | None = None,
) -> StatusPoint:
    """A front-page row (``work_story.FrontRow``) as the status it shows."""
    section = row.section
    if section == NEEDS_YOU and hidden:
        section = HIDDEN  # out of Needs you on the front page, on its Hidden list
    attention = row.attention
    return StatusPoint(
        work_id=row.card.work_id,
        at=at,
        section=section,
        category=attention.category if attention is not None else "",
        kind=row.status.kind,
        tone=row.status.tone,
        headline=attention.label if attention is not None else row.status.headline,
        facts_mark=facts_mark(row.card),
        why=why(row.card, row.status),
        degraded=tuple(degraded),
        pid=pid,
        revision=revision,
    )


def changes(
    last: Mapping[str, StatusPoint],
    rows: Iterable[Any],
    *,
    now: datetime,
    hidden: frozenset[str] = frozenset(),
    degraded: Sequence[str] = (),
    pid: int | None = None,
    revision: str | None = None,
) -> list[StatusPoint]:
    """The rows to log for one pass: each Work whose status changed since ``last``, and each
    Work ``last`` holds that this pass no longer built (``GONE``)."""
    found: list[StatusPoint] = []
    seen: set[str] = set()
    for row in rows:
        work_id = row.card.work_id
        seen.add(work_id)
        present = point(
            row,
            at=now,
            hidden=work_id in hidden,
            degraded=degraded,
            pid=pid,
            revision=revision,
        )
        before = last.get(work_id)
        if before is not None and before.key == present.key:
            continue
        found.append(_after(present, before))
    for work_id, before in last.items():
        if work_id in seen or before.section == GONE:
            continue
        gone = StatusPoint(work_id, now, GONE, degraded=tuple(degraded), pid=pid, revision=revision)
        found.append(_after(gone, before))
    return found


def _after(present: StatusPoint, before: StatusPoint | None) -> StatusPoint:
    if before is None:
        return present
    return replace(
        present,
        prev_section=before.section,
        prev_category=before.category,
        prev_kind=before.kind,
        prev_tone=before.tone,
    )


def flaps(points: Iterable[StatusPoint], *, within: timedelta) -> list[dict[str, Any]]:
    """Each short trip a Work made, shorter than ``within``, oldest first.

    - ``into_needs_you``: it entered Needs you and left it again, wherever it came from or
      went, and whatever category changes happened inside the visit.
    - ``out_of_needs_you``: it left Needs you and came back.
    - ``bounce``: any other trip somewhere and straight back to the same section and
      category, such as one Needs you category and back, or Active to Finished and back.

    Rows that change only the kind or tone split nothing.
    """
    by_work: dict[str, list[StatusPoint]] = {}
    for item in sorted(points, key=lambda p: p.at):
        by_work.setdefault(item.work_id, []).append(item)
    found = []
    for work_id, items in by_work.items():
        found.extend(_needs_you_trips(work_id, items, within))
        found.extend(_bounces(work_id, items, within))
    return sorted(found, key=lambda flap: flap["entered_at"])


def _needs_you_trips(
    work_id: str, items: Sequence[StatusPoint], within: timedelta
) -> list[dict[str, Any]]:
    """Visits into Needs you and absences from it, at section level."""
    # Each row where membership in Needs you changed, with the status it came from.
    turns: list[tuple[StatusPoint, tuple[str, str]]] = []
    inside = items[0].prev_section == NEEDS_YOU if items else False
    before = (items[0].prev_section, items[0].prev_category) if items else ("", "")
    for item in items:
        now_inside = item.section == NEEDS_YOU
        if now_inside != inside:
            turns.append((item, before))
            inside = now_inside
        before = (item.section, item.category)
    found = []
    for (start, came_from), (end, _) in zip(turns, turns[1:], strict=False):
        lasted = end.at - start.at
        if lasted > within:
            continue
        kind = "into_needs_you" if start.section == NEEDS_YOU else "out_of_needs_you"
        found.append(_flap(work_id, kind, start, end, lasted, came_from=came_from))
    return found


def _bounces(work_id: str, items: Sequence[StatusPoint], within: timedelta) -> list[dict[str, Any]]:
    """Trips to another (section, category) and straight back that never crossed Needs you's
    edge: crossing it is a Needs you trip above."""
    spans: list[StatusPoint] = []  # the row that opened each span
    for item in items:
        if spans and (spans[-1].section, spans[-1].category) == (item.section, item.category):
            continue
        spans.append(item)
    found = []
    for before, blip, after in zip(spans, spans[1:], spans[2:], strict=False):
        lasted = after.at - blip.at
        if lasted > within:
            continue
        if (before.section, before.category) != (after.section, after.category):
            continue
        if (before.section == NEEDS_YOU) != (blip.section == NEEDS_YOU):
            continue
        found.append(
            _flap(
                work_id, "bounce", blip, after, lasted, came_from=(before.section, before.category)
            )
        )
    return found


def _flap(
    work_id: str,
    kind: str,
    start: StatusPoint,
    end: StatusPoint,
    lasted: timedelta,
    *,
    came_from: tuple[str, str],
) -> dict[str, Any]:
    return {
        "work_id": work_id,
        "kind": kind,
        "from": {"section": came_from[0], "category": came_from[1]},
        "to": {"section": start.section, "category": start.category},
        "then": {"section": end.section, "category": end.category},
        "entered_at": start.at.isoformat(),
        "left_at": end.at.isoformat(),
        "seconds": round(lasted.total_seconds(), 1),
        "entered": start.as_dict(),
        "left": end.as_dict(),
    }
