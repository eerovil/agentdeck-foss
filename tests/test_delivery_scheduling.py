"""Which account a new piece of work starts on: ``choose_account`` (#2146, #2757).

It was the runtime's selection for the Poller's ``ensure_delivery`` until the card dispatcher
went (#3741); starting a Work and moving a chat to another account still choose with it. The
parity matrix pins what the Poller's own selector answered for each input before it was
removed (#2150): the move was characterized, not approximated, and the answers must stay.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck.db import Db
from agentdeck.delivery_scheduling import choose_account
from agentdeck.providers import account_health

MAIN, ALT = "claude_code:main", "claude_code:alt"


def _weekly(
    cache: Path,
    account: str,
    *,
    pct: float,
    resets_in: timedelta,
    five_hour: tuple[float, timedelta] | None = None,
) -> None:
    provider, _, label = account.partition(":")
    now = datetime.now(UTC)
    reading = {
        "account": account,
        "fetched_at": now.isoformat(),
        "seven_day_pct": pct,
        "seven_day_resets_at": (now + resets_in).isoformat(),
    }
    if five_hour is not None:
        reading["five_hour_pct"] = five_hour[0]
        reading["five_hour_resets_at"] = (now + five_hour[1]).isoformat()
    (cache / f"usage-{provider}-{label}.json").write_text(json.dumps(reading))


# --- parity: the runtime selects exactly as the Poller did -----------------------------------


EXPIRING_ALT = [(MAIN, 40.0, timedelta(days=5)), (ALT, 85.0, timedelta(hours=12))]

# ``""`` is a withheld delivery: every account has an open hard-quota circuit, or none is given.
PARITY = {
    "operator order": dict(expect=MAIN),
    "hard circuit on the first": dict(expect=ALT, circuits=[MAIN]),
    "every account circuited": dict(expect="", circuits=[MAIN, ALT]),
    "expiring budget leads": dict(expect=ALT, weekly=EXPIRING_ALT),
    "soft refusal passed over": dict(expect=ALT, weekly=[(MAIN, 100.0, timedelta(days=2))]),
    "every account refuses, earliest clears": dict(
        expect=ALT,
        weekly=[(MAIN, 100.0, timedelta(days=3)), (ALT, 100.0, timedelta(days=1))]
    ),
    "no accounts at all": dict(expect="", accounts=[]),
    "codex first in the operator order": dict(expect="codex:work", accounts=["codex:work", MAIN]),
}


@pytest.mark.parametrize("case", list(PARITY), ids=list(PARITY))
def test_the_runtime_selects_exactly_as_the_poller_did(tmp_path, case):
    """Each ``expect`` is what ``poller_wake.legacy_account_for`` answered for the same inputs,
    recorded by running both side by side before that selector was removed (#2150)."""
    spec = PARITY[case]
    cache = tmp_path / "usage"
    cache.mkdir()
    for account in spec.get("circuits", []):
        assert account_health.trip_hard_quota(cache, account, reason="window exhausted")
    for account, pct, resets_in in spec.get("weekly", []):
        _weekly(cache, account, pct=pct, resets_in=resets_in)

    chosen = choose_account(list(spec.get("accounts", [MAIN, ALT])), cache, [], cold=True)

    assert chosen == spec["expect"]


# --- a new card starts where the most budget is left per hour (#2757) -----------------------

# The operator's reading on 2026-10-09: main 48% with five days to its reset, alt 11% with
# three and a half. Alt has 84 points over 84 hours, main 47 over 119.
UNEVEN = [(MAIN, 48.0, timedelta(hours=119)), (ALT, 11.0, timedelta(hours=84))]


class _Placement:
    def __init__(self, account: str, notes: list[str]) -> None:
        self.account, self.notes = account, notes


def _place(tmp_path, *, weekly=(), accounts=(MAIN, ALT), circuits=(), five=None):
    cache = tmp_path / "usage"
    cache.mkdir(parents=True, exist_ok=True)
    for account in circuits:
        assert account_health.trip_hard_quota(cache, account, reason="window exhausted")
    for account, pct, resets_in in weekly:
        _weekly(cache, account, pct=pct, resets_in=resets_in, five_hour=(five or {}).get(account))
    db = Db(tmp_path / "control.db", 30)
    notes: list[str] = []
    chosen = choose_account(
        list(accounts),
        cache,
        notes,
        cold=True,
        planned_resets=db.planned_resets(),
    )
    db.close()
    return _Placement(chosen, notes)


def test_a_new_card_starts_on_the_account_with_the_most_budget_left_per_hour(tmp_path):
    placement = _place(tmp_path, weekly=UNEVEN)

    assert placement.account == ALT
    assert any("1.0 points/hour" in note and "0.4" in note for note in placement.notes)


def test_a_new_card_skips_an_account_whose_five_hour_window_is_nearly_full(tmp_path):
    """#3580: the budget pace sent every card to alt until its five-hour window stopped them."""
    hour = timedelta(hours=1)
    placement = _place(tmp_path / "full", weekly=UNEVEN, five={ALT: (92.0, hour)})
    assert placement.account == MAIN
    assert any("five-hour window is 92% full" in note for note in placement.notes)

    assert _place(tmp_path / "room", weekly=UNEVEN, five={ALT: (89.0, hour)}).account == ALT
    # A window already reset has refilled, and with no account having room the order stands.
    assert _place(tmp_path / "reset", weekly=UNEVEN, five={ALT: (95.0, -hour)}).account == ALT
    both = {ALT: (95.0, hour), MAIN: (91.0, hour)}
    assert _place(tmp_path / "both", weekly=UNEVEN, five=both).account == ALT
    # A full account that does not lead moves behind one with room, and the leader stays.
    third = "claude_code:third"
    level = [(name, 85.0, timedelta(hours=160)) for name in (MAIN, ALT, third)]
    three = _place(
        tmp_path / "three",
        weekly=level,
        accounts=(MAIN, ALT, third),
        five={ALT: (92.0, hour), third: (50.0, hour)},
    )
    assert three.account == MAIN and not three.notes


def test_a_new_card_skips_an_account_whose_week_is_nearly_spent(tmp_path):
    """#3977: alt at 91% of its week, resetting in 29 hours, was 4 points short of its target,
    so the expiring-budget rule put it ahead of main at 60% with 64 hours to go."""
    weekly = [(MAIN, 60.0, timedelta(hours=64)), (ALT, 91.0, timedelta(hours=29))]
    placement = _place(tmp_path / "issue", weekly=weekly, accounts=(ALT, MAIN))
    assert placement.account == MAIN
    assert any("seven-day window is 91% full" in note for note in placement.notes)
    assert _place(tmp_path / "order", weekly=weekly).account == MAIN
    # A week that already reset has refilled, so it no longer counts as nearly spent.
    reset = [(MAIN, 60.0, timedelta(hours=64)), (ALT, 91.0, -timedelta(hours=1))]
    assert _place(tmp_path / "reset", weekly=reset, accounts=(ALT, MAIN)).account == ALT

    # With real room left the sooner reset still leads.
    roomy = [(MAIN, 60.0, timedelta(hours=64)), (ALT, 80.0, timedelta(hours=29))]
    assert _place(tmp_path / "roomy", weekly=roomy).account == ALT
    # When both are nearly spent, the order and the expiring rule stand.
    both = [(MAIN, 92.0, timedelta(hours=64)), (ALT, 91.0, timedelta(hours=29))]
    assert _place(tmp_path / "both", weekly=both).account == ALT


def test_equal_paces_keep_the_operators_order(tmp_path):
    weekly = [(MAIN, 45.0, timedelta(hours=100)), (ALT, 45.0, timedelta(hours=100))]

    placement = _place(tmp_path, weekly=weekly)

    assert placement.account == MAIN
    assert not placement.notes

    # 0.45 against 0.5 points/hour: within the tie band, so still the operator's order
    close = [(MAIN, 50.0, timedelta(hours=100)), (ALT, 45.0, timedelta(hours=100))]
    assert _place(tmp_path / "close", weekly=close).account == MAIN


def test_an_account_past_its_target_ranks_last(tmp_path):
    weekly = [(MAIN, 96.0, timedelta(hours=10)), (ALT, 50.0, timedelta(hours=150))]
    assert _place(tmp_path / "past", weekly=weekly).account == ALT


def test_a_missing_reading_leaves_the_operators_order(tmp_path):
    """Ignorance must not move work, even towards the account that does report."""
    assert _place(tmp_path, weekly=UNEVEN[1:]).account == MAIN


def test_another_providers_empty_meter_never_jumps_the_queue(tmp_path):
    """Codex reads 0% whatever it has spent; it keeps its fallback slot."""
    codex = "codex:codex"
    weekly = [*UNEVEN, (codex, 0.0, timedelta(hours=160))]

    placement = _place(tmp_path, weekly=weekly, accounts=(MAIN, codex, ALT))

    assert placement.account == ALT


def test_a_full_account_is_still_passed_over_after_ranking(tmp_path):
    weekly = [(MAIN, 48.0, timedelta(hours=119)), (ALT, 100.0, timedelta(hours=84))]
    assert _place(tmp_path, weekly=weekly).account == MAIN
    assert _place(tmp_path / "circuit", weekly=UNEVEN, circuits=[ALT]).account == MAIN


def test_an_expiring_budget_still_has_the_final_say(tmp_path):
    """Main at 80% with ten hours left out-ranks alt's faster pace: that budget is about to go."""
    weekly = [(MAIN, 80.0, timedelta(hours=10)), (ALT, 0.0, timedelta(hours=60))]
    # paces: main 1.5/h, alt 1.6/h; only main is inside its last quarter, 15 points short
    assert _place(tmp_path, weekly=weekly).account == MAIN


def test_a_planned_reset_sends_its_account_work_first_until_then(tmp_path):
    """#3379: the person will reset alt's week in 15 hours, so alt is filled to 100% first."""
    weekly = [(MAIN, 22.0, timedelta(days=6)), (ALT, 25.0, timedelta(days=5))]
    assert _place(tmp_path / "none", weekly=weekly).account == MAIN

    # Kept in the deck's database, which is reopened here as the runtime would after
    # a restart (a file in the usage folder was deleted by every runtime restart).
    now = datetime.now(UTC)
    for case in ("cold", "past", "full", "days"):
        (tmp_path / case).mkdir()
        db = Db(tmp_path / case / "control.db", 30)
        ahead = timedelta(days=3) if case == "days" else timedelta(hours=15)
        at = now - timedelta(hours=1) if case == "past" else now + ahead
        db.record_planned_reset(ALT, at)
        db.close()
    assert _place(tmp_path / "cold", weekly=weekly).account == ALT
    assert _place(tmp_path / "past", weekly=weekly).account == MAIN
    # Still nothing goes to a window that would refuse the turn.
    full = [(MAIN, 22.0, timedelta(days=6)), (ALT, 100.0, timedelta(days=5))]
    assert _place(tmp_path / "full", weekly=full).account == MAIN
    # #4717: a plan days ahead, past the usual last-quarter horizon, still sends a nearly
    # full account work first (its slow pace alone would lose to main's).
    nearly = [(MAIN, 22.0, timedelta(days=6)), (ALT, 85.0, timedelta(days=5))]
    assert _place(tmp_path / "none", weekly=nearly).account == MAIN
    assert _place(tmp_path / "days", weekly=nearly).account == ALT


def test_a_bare_clock_time_is_the_hosts_next_one_across_a_clock_change(monkeypatch):
    """#4717: tomorrow across a clock change, and the second reading of a repeated hour."""
    from agentdeck.planned_reset import parse_time

    monkeypatch.setenv("TZ", "Europe/Helsinki")
    time.tzset()
    try:
        spring = parse_time("12:00", now=datetime(2027, 3, 27, 12, 0, tzinfo=UTC))
        # 03:10 for the second time on the night the clocks go back; 03:30 is 20 minutes on.
        repeated = parse_time("03:30", now=datetime(2026, 10, 25, 1, 10, tzinfo=UTC))
        dated = parse_time("2026-11-29T14:30", now=datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
        # The same repeated hour, picked as a date and time: before it, the first 03:30; in
        # its second run, the one still ahead; after both, refused.
        second_run = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
        dated_first = parse_time("2026-10-25T03:30", now=second_run - timedelta(hours=2))
        dated_repeated = parse_time("2026-10-25T03:30", now=second_run)
        with pytest.raises(ValueError, match="already passed"):
            parse_time("2026-10-25T03:30", now=second_run + timedelta(hours=1))
        with pytest.raises(ValueError, match="date and time"):
            parse_time("2026-11-29", now=datetime(2026, 11, 26, 12, 0, tzinfo=UTC))
    finally:
        monkeypatch.undo()
        time.tzset()
    assert spring.astimezone(UTC) == datetime(2027, 3, 28, 9, 0, tzinfo=UTC)
    assert repeated.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert dated.astimezone(UTC) == datetime(2026, 11, 29, 12, 30, tzinfo=UTC)
    assert dated_first.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert dated_repeated.astimezone(UTC) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
