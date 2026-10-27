"""How far each usage window has run, and the projected-overspend flag (#1137, #2181).

The dollar capacity estimate this file once tested was removed in #2864; what is left
reads only the provider's own meter and reset time.
"""

from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.models import FIVE_HOUR, MONTH, SEVEN_DAY
from agentdeck.usage_pace import elapsed_share, overspend_pace

T0 = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
RESET = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _pace(pct, *, remaining, window=FIVE_HOUR):
    return overspend_pace(window, pct=pct, resets_at=T0 + remaining, now=T0)


def test_a_meter_matching_the_elapsed_share_of_its_window_is_not_flagged():
    assert _pace(20.0, remaining=timedelta(hours=4)) is None
    assert _pace(30.0, remaining=timedelta(hours=4)) is None  # 1.5x is the edge, not over it


def test_a_meter_running_ahead_of_its_window_warns_then_goes_critical():
    warn = _pace(31.0, remaining=timedelta(hours=4))
    crit = _pace(51.0, remaining=timedelta(hours=4))

    assert warn.level == "warn"
    assert (warn.part, warn.on_pace_pct) == (2, 20.0)
    assert crit.level == "crit"


def test_a_late_window_is_allowed_to_be_nearly_full():
    """The whole point of pacing: 80% with an hour of a five-hour window left is a
    window being spent, not an overspend. A flat threshold cannot tell them apart."""
    assert _pace(80.0, remaining=timedelta(hours=1)) is None
    assert _pace(45.0, remaining=timedelta(hours=4, minutes=30)).level == "crit"


def test_the_weekly_window_is_paced_against_its_own_length():
    early = overspend_pace(
        SEVEN_DAY, pct=60.0, resets_at=T0 + timedelta(days=6), now=T0
    )
    late = overspend_pace(SEVEN_DAY, pct=60.0, resets_at=T0 + timedelta(days=2), now=T0)

    assert (early.part, early.on_pace_pct) == (2, 20.0)  # a day in of seven
    assert early.level == "crit"
    assert late is None  # five days in, on pace is ~80%


def test_pacing_needs_a_meter_a_boundary_and_a_known_window_length():
    assert _pace(None, remaining=timedelta(hours=4)) is None
    assert overspend_pace(FIVE_HOUR, pct=90.0, resets_at=None, now=T0) is None
    # Cursor's billing cycle has no nominal length, so it is never flagged rather
    # than flagged against a guessed month (#730).
    assert overspend_pace(MONTH, pct=99.0, resets_at=T0 + timedelta(days=28), now=T0) is None


def test_elapsed_share_is_how_far_through_the_window_the_clock_is():
    """#2181. The time layer drawn inside each meter."""
    assert elapsed_share(FIVE_HOUR, resets_at=T0 + timedelta(hours=4), now=T0) == pytest.approx(0.2)
    assert elapsed_share(SEVEN_DAY, resets_at=T0 + timedelta(days=7), now=T0) == 0.0
    # No boundary, no known length, or a boundary our clock disagrees with: no guess.
    assert elapsed_share(FIVE_HOUR, resets_at=None, now=T0) is None
    assert elapsed_share(MONTH, resets_at=T0 + timedelta(days=28), now=T0) is None
    assert elapsed_share(FIVE_HOUR, resets_at=T0 - timedelta(minutes=1), now=T0) is None
    assert elapsed_share(FIVE_HOUR, resets_at=T0 + timedelta(hours=6), now=T0) is None


def test_a_boundary_our_clock_disagrees_with_is_not_paced():
    """Providers recompute the reset time from their own clock on every poll. A
    boundary in the past, or further out than a whole window, means we cannot say
    how far into the window we are — and a guess would flag accounts at random."""
    assert _pace(99.0, remaining=timedelta(hours=-1)) is None
    assert _pace(99.0, remaining=timedelta(hours=6)) is None
