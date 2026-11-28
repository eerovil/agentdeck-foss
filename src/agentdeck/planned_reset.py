"""A person's planned usage reset of one account (#3379).

The operator can reset an account's usage by hand at a time of their choosing. Until then the
budget left on that account is about to be wasted, so the deck should send it work first and
spend it to the full, not to the usual target. A plan is one time per account in the deck's
database (``planned_resets``), which the web process and the runtime both open; each account
chooser reads it and hands the plan to the usage reader. A plan whose time has passed is
ignored. It once lived in a file in the shared usage cache folder, but that folder is the
runtime service's ``RuntimeDirectory``, which systemd deletes on every runtime restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta


def plan_for(plans: Mapping[str, datetime] | None, account: str) -> datetime | None:
    """This account's plan from ``Db.planned_resets``; a bare label is Claude's, as the
    Poller has always read it."""
    if not plans:
        return None
    return plans.get(account if ":" in account else f"claude_code:{account}")


def parse_time(text: str, *, now: datetime | None = None) -> datetime:
    """``HH:MM`` on the host's clock (its next occurrence), or an ISO date and time.

    An ISO time with no zone is on the host's clock too: that is what the account page's
    date-and-time field sends (#4717). Raises ``ValueError`` for anything else, or for an
    instant already past.
    """
    text = (text or "").strip()
    local_now = (now or datetime.now(UTC)).astimezone()
    try:
        clock = datetime.strptime(text, "%H:%M")
    except ValueError:
        clock = None
    if clock is None:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            moment = None
        if moment is not None and _is_date_alone(text):
            moment = None  # midnight is not what a person naming a day meant
        if moment is None:
            raise ValueError("give a time as HH:MM or a date and time (YYYY-MM-DDTHH:MM)")
        if moment.tzinfo is None:
            moment = _on_host_clock(moment, local_now) or moment.astimezone()
        if moment <= local_now:
            raise ValueError("that time has already passed")
        return moment
    # Built without a zone and placed on the host's clock afterwards, so a tomorrow across a
    # clock change is still that clock time.
    wall = local_now.replace(
        tzinfo=None, hour=clock.hour, minute=clock.minute, second=0, microsecond=0
    )
    return _on_host_clock(wall, local_now) or (wall + timedelta(days=1)).astimezone()


def _on_host_clock(wall: datetime, after: datetime) -> datetime | None:
    """A zone-less time placed on the host's clock, still ahead of ``after``, or ``None``.

    An hour the clocks repeat has two readings (``fold``); the earlier one still ahead is
    the one meant, for a bare ``HH:MM`` and for a date and time alike.
    """
    for candidate in (wall.replace(fold=0), wall.replace(fold=1)):
        moment = candidate.astimezone()
        if moment > after:
            return moment
    return None


def _is_date_alone(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def apply(db, state, account: str, text: str, *, now: datetime | None = None) -> datetime | None:
    """Set one account's plan from what a person typed (empty clears it), and redraw the bars.

    Raises ``ValueError`` for a time ``parse_time`` refuses.
    """
    at = parse_time(text, now=now) if (text or "").strip() else None
    db.record_planned_reset(account, at)
    state.usage_changed()
    return at
