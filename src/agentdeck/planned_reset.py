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
from datetime import UTC, datetime, timedelta


def plan_for(plans: Mapping[str, datetime] | None, account: str) -> datetime | None:
    """This account's plan from ``Db.planned_resets``; a bare label is Claude's, as the
    Poller has always read it."""
    if not plans:
        return None
    return plans.get(account if ":" in account else f"claude_code:{account}")


def parse_time(text: str, *, now: datetime | None = None) -> datetime:
    """``HH:MM`` on the host's clock (its next occurrence) or an ISO instant with a zone.

    Raises ``ValueError`` for anything else, or for an instant already past.
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
        if moment is None or moment.tzinfo is None:
            raise ValueError("give a time as HH:MM or an ISO time with a zone")
        if moment <= local_now:
            raise ValueError("that time has already passed")
        return moment
    moment = local_now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if moment <= local_now:
        moment += timedelta(days=1)
    return moment


def apply(db, state, account: str, text: str, *, now: datetime | None = None) -> datetime | None:
    """Set one account's plan from what a person typed (empty clears it), and redraw the bars.

    Raises ``ValueError`` for a time ``parse_time`` refuses.
    """
    at = parse_time(text, now=now) if (text or "").strip() else None
    db.record_planned_reset(account, at)
    state.usage_changed()
    return at
