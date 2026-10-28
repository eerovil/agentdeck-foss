"""When a Work stopped, and whether the operator has looked since (#2283).

A Work *stops* when it enters one of Needs you's categories (``work_attention``): the agent
work is over and a person is next. The front page lists the newest few under **Recently
stopped**, as a shortcut to the category each landed in, and marks a Work *unseen* while it
stopped after the operator last opened its page.

Both facts are this deck's own records in the control DB, never a GitHub or board write:

- ``work_stops`` holds each Work's current category and when it entered it. The overview pass
  compares what it just classified with the stored row (``track_stops``): a new category is
  stamped now, the same one keeps its stamp, and a Work that left Needs you keeps a row with
  an empty category, so entering again later is a new stop.
- ``work_opened`` holds when the Work's page was last opened (``/works/{id}``).

The first pass cannot know when the Works already waiting stopped. It dates each by its last
activity and counts it as seen, so turning this on does not mark every old question new. That
pass is recorded (``work_stops_started``) even when nothing was waiting, so a deck that starts
with an empty Needs you still marks its first real stop new.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# A Work that left Needs you keeps its row this long, then the row is dropped.
FORGET_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class WorkStop:
    """A Work's current Needs you category ("" once it left) and when it got there.

    ``mark`` is the Work's ``facts_mark`` when the row was last written, None for a row from
    before marks were kept (#2332).
    """

    work_id: str
    category: str
    stopped_at: datetime
    mark: str | None = None


def facts_mark(card: Any) -> str:
    """The facts a Work's category is read from, in one short string (#2332).

    Each output's state and head, and the chats' attention states. A category that
    changes while this does not was changed by how the deck reads the facts — a deploy of new
    classification code — not by anything that happened to the Work.
    """
    outputs = sorted(
        (
            output.label,
            output.state,
            output.head,
            output.conflicting,
        )
        for output in getattr(card, "outputs", ())
    )
    parts = (
        repr(outputs),
        ",".join(sorted(item.state for item in getattr(card, "attention", ()))),
    )
    return hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:16]


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def track_stops(
    previous: Mapping[str, WorkStop],
    present: Mapping[str, str],
    *,
    now: datetime,
    seed: Mapping[str, datetime | None] | None = None,
    bootstrap: bool = False,
    marks: Mapping[str, str] | None = None,
    changed_since: datetime | None = None,
) -> dict[str, WorkStop]:
    """The rows that change, given what the pass classified.

    ``present`` is every Work the pass built, mapped to its Needs you category, or "" when
    it is not waiting on a person. A Work absent from it is left as it is. On the first pass
    (``bootstrap``) a new stop is dated by ``seed`` (its last activity), never later than
    ``now``; otherwise it is dated ``now``.

    ``marks`` is each Work's ``facts_mark`` (#2332). A category that changed while the mark
    stayed the stored one is a reclassification, not a stop: it keeps the Work's last real
    activity (``seed``), else its old date, so a deploy never makes old Works look freshly
    stopped. A row with no stored mark counts as a real change once. The mark is kept current
    on every row, so a Work's facts moving without its category moving is remembered too.

    ``changed_since`` is when the first change after the previous pass arrived, for a pass
    that caught up a gap nobody watched (#2865). A real stop found then happened inside
    ``[changed_since, now]``, so it is dated by its last activity clamped into that span
    rather than ``now``; with no activity in it, the gap's start is the earliest it can be.
    """
    now = utc(now)
    marks = marks or {}
    changed: dict[str, WorkStop] = {}
    for work_id, category in present.items():
        before = previous.get(work_id)
        mark = marks.get(work_id)
        if before is not None and before.category == category:
            if mark is not None and before.mark != mark:
                changed[work_id] = WorkStop(work_id, category, before.stopped_at, mark)
            continue
        if before is None and not category:
            if mark is not None:
                # Never stopped, but its facts are remembered for the day it does (#2332).
                changed[work_id] = WorkStop(work_id, "", now, mark)
            continue
        when = now
        seeded = (seed or {}).get(work_id)
        if bootstrap and category:
            if seeded is not None:
                when = min(utc(seeded), now)
        elif before is not None and mark is not None and before.mark == mark:
            when = min(utc(seeded) if seeded is not None else utc(before.stopped_at), now)
        elif changed_since is not None and category:
            start = min(utc(changed_since), now)
            when = min(max(utc(seeded), start), now) if seeded is not None else start
        changed[work_id] = WorkStop(work_id, category, when, mark)
    return changed


def is_unseen(stop: WorkStop | None, opened_at: datetime | None) -> bool:
    """The Work stopped after its page was last opened; never opened counts as unseen."""
    if stop is None or not stop.category:
        return False
    return opened_at is None or utc(stop.stopped_at) > utc(opened_at)
