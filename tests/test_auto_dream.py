"""Auto dream before the weekly reset (#3336): when it starts a dream, and with what budget."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck import auto_dream
from agentdeck.models import InjectResult, UsageSnapshot

NOW = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)


def reading(key="claude_code:main", *, left=timedelta(hours=4), week=80.0, five=20.0, age_s=60):
    label = key.partition(":")[2]
    snapshot = UsageSnapshot(
        account_key=key,
        five_hour_pct=five,
        five_hour_resets_at=None,
        seven_day_pct=week,
        seven_day_resets_at=NOW + left,
        fetched_at=NOW - timedelta(seconds=age_s),
    )
    return (key, label, snapshot)


def due(*readings, state=None):
    runs, _skipped = auto_dream.candidates(readings, state or {}, NOW)
    return [(run.account_key, run.kind, run.budget) for run in runs]


@pytest.mark.parametrize(
    ("left", "week", "expected"),
    [
        (timedelta(hours=4, minutes=59), 90.0, [("claude_code:main", "early", 5)]),
        (timedelta(hours=5, minutes=1), 50.0, []),
        (timedelta(hours=4), 91.0, []),
        (timedelta(minutes=59), 80.4, [("claude_code:main", "final", 18)]),
        (timedelta(minutes=59), 98.0, []),
        (timedelta(minutes=-1), 10.0, []),
    ],
)
def test_the_early_and_final_runs_are_due_by_the_reset_and_the_week_left(left, week, expected):
    assert due(reading(left=left, week=week)) == expected


def test_each_run_happens_once_a_window_and_skips_say_why():
    early = reading()
    window = auto_dream.window_key(early[2].seven_day_resets_at)
    # A reset reported a fraction of a second later is the same window.
    jitter = early[2].seven_day_resets_at + timedelta(seconds=0.4)
    assert auto_dream.window_key(jitter) == window
    assert due(early, state={"claude_code:main": {"early": window}}) == []
    final = reading(left=timedelta(minutes=30))
    final_window = auto_dream.window_key(final[2].seven_day_resets_at)
    assert due(final, state={"claude_code:main": {"early": final_window}}) == [
        ("claude_code:main", "final", 19)
    ]

    runs, skipped = auto_dream.candidates(
        [
            reading(age_s=3600),
            reading("claude_code:alt", five=None),
            reading("codex:codex", five=75.0),
            reading("cursor:cursor"),
        ],
        {},
        NOW,
    )
    assert runs == []
    assert skipped == [
        "claude_code:main: the usage reading is stale",
        "claude_code:alt: the 5-hour meter is missing",
        "codex:codex: the 5-hour meter is at 75%",
    ]
    # Codex may have no 5-hour window at all (#2560): its week decides alone.
    assert due(reading("codex:codex", five=None)) == [("codex:codex", "early", 5)]


def test_a_final_run_goes_before_an_early_one_then_the_earlier_reset():
    assert due(
        reading("claude_code:alt", left=timedelta(hours=2)),
        reading("claude_code:main", left=timedelta(hours=3)),
        reading("codex:codex", left=timedelta(minutes=40), week=90.0),
    ) == [
        ("codex:codex", "final", 9),
        ("claude_code:alt", "early", 5),
        ("claude_code:main", "early", 5),
    ]


def test_a_start_is_recorded_only_once_accepted_and_never_while_a_dream_runs(tmp_path):
    answers = [(False, "already starting"), (True, "")]
    started = []

    async def start(run):
        started.append(run.message)
        return answers.pop(0)

    loop = auto_dream.AutoDream(readings=lambda: [reading()], start=start, directory=tmp_path)
    lock = tmp_path / "dream.lock"
    mine = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[-1].split()[19]
    lock.write_text(json.dumps({"pid": os.getpid(), "start": mine, "heartbeat": NOW.timestamp()}))
    assert asyncio.run(loop.check(NOW)) is None
    assert started == []

    lock.unlink()
    assert asyncio.run(loop.check(NOW)) is None
    assert not loop.state_path.exists()
    run = asyncio.run(loop.check(NOW))
    message = f"/dream main 5 --week-ends {(NOW + timedelta(hours=4)).isoformat()}"
    assert run is not None and started == [message, message]
    assert json.loads(loop.state_path.read_text()) == {"claude_code:main": {"early": run.window}}
    assert asyncio.run(loop.check(NOW)) is None
    assert len(started) == 2


def test_only_a_clean_refusal_of_the_queued_start_leaves_the_run_for_later():
    async def answer(value):
        if isinstance(value, Exception):
            raise value
        return value

    def settle(value):
        return asyncio.run(auto_dream.settled(answer(value)))

    assert settle(InjectResult(False, "at_capacity")) == (False, "at_capacity")
    assert settle(InjectResult(True))[0] is True
    # Unknown, or refused after a session existed: it may have launched, so it counts.
    assert settle(None)[0] is True
    assert settle(RuntimeError("lost"))[0] is True
    assert settle(InjectResult(False, "late", session_id="s1"))[0] is True
