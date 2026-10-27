"""How far each usage limit's window has run, and whether its meter is ahead of pace.

What is left of ``capacity.py`` after #2864 removed the dollar estimate of each
window's capacity: the limit bars still draw how much of the window has elapsed
and flag a meter that is burning faster than the window (#1137). Both read only
the provider's own meter percentage and reset time, never transcript spend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import FIVE_HOUR, MONTH, SEVEN_DAY

# Nominal length of each quota window, to tell how far through it the clock is.
# A billing cycle has no fixed length, so it is left out and never paced.
WINDOW_LENGTH = {
    FIVE_HOUR: timedelta(hours=5),
    SEVEN_DAY: timedelta(days=7),
}

# The meter row keeps the established short forms; diagnostics have room to say
# what the weekly window actually is. ``MONTH`` is a billing cycle, not 30 rolling
# days, so it says "cycle" rather than borrowing the rolling-window vocabulary.
WINDOW_LABELS = {FIVE_HOUR: "5h", SEVEN_DAY: "7d", MONTH: "mo"}
WINDOW_TITLES = {FIVE_HOUR: "5h", SEVEN_DAY: "Week", MONTH: "Billing cycle"}

# Projected overspend (#1137). A percentage on its own says nothing about whether
# the window will hold: 40% burned twenty minutes into a five-hour window runs
# out long before the reset, while the same 40% with twenty minutes left is a
# window well spent. So the elapsed share of the window is cut into ten parts and
# being in part N is read as an on-pace budget of N*10%.
#
# Ten coarse parts rather than the exact elapsed fraction, because the reset
# boundary is recomputed from the provider's own clock on every poll: a
# fine-grained pace would flip the flag back and forth on rounding alone.
PACE_PARTS = 10
# Multiples of the on-pace budget at which the flag appears. From the card's
# worked example: two parts into a five-hour window the pace is 20%, and 30% is
# to be a warning while 50% is critical.
PACE_WARN_RATIO = 1.5
PACE_CRIT_RATIO = 2.5


@dataclass(frozen=True)
class OverspendPace:
    """How one window's meter compares with how far into the window we are.

    Only constructed when the meter is actually ahead of pace — ``level`` is
    always ``"warn"`` or ``"crit"``, never an all-clear, so a caller can treat
    the whole object as the alarm.
    """

    level: str
    part: int
    on_pace_pct: float
    pct: float


def elapsed_share(window: str, *, resets_at: datetime | None, now: datetime) -> float | None:
    """How far through its window ``now`` is, from 0 to 1, or ``None`` if unknown.

    Needs a reported reset boundary and a window whose nominal length the deck
    knows. A boundary further out than a whole window, or already in the past,
    means the reported reset time and our clock disagree about which window
    this is, so no share is guessed from it.
    """
    length = WINDOW_LENGTH.get(window)
    if length is None or resets_at is None:
        return None
    remaining = (resets_at - now).total_seconds()
    total = length.total_seconds()
    if not 0 < remaining <= total:
        return None
    return (total - remaining) / total


def overspend_pace(
    window: str,
    *,
    pct: float | None,
    resets_at: datetime | None,
    now: datetime,
) -> OverspendPace | None:
    """This window's projected-overspend flag, or ``None`` when it is on pace.

    Needs a numeric meter, a reported reset boundary, and a window whose nominal
    length the deck knows. A billing cycle has no fixed length, so it is
    never flagged rather than flagged against a guessed month.
    """
    share = elapsed_share(window, resets_at=resets_at, now=now)
    if pct is None or share is None:
        return None
    part = max(1, math.ceil(share * PACE_PARTS))
    on_pace_pct = part * 100 / PACE_PARTS
    if pct > on_pace_pct * PACE_CRIT_RATIO:
        level = "crit"
    elif pct > on_pace_pct * PACE_WARN_RATIO:
        level = "warn"
    else:
        return None
    return OverspendPace(level=level, part=part, on_pace_pct=on_pace_pct, pct=pct)
