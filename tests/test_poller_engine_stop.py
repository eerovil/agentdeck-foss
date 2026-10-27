"""Slice 5 of #176: the stop protocol, released only on the Runtime's proof (#2159)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.poller_engine import leases, stop
from agentdeck.poller_engine.stop import StopDecision

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)


def test_is_active():
    assert stop.is_active({"status": "In progress"})
    assert stop.is_active({"processing": ["c1"]})
    assert not stop.is_active({"status": "Todo"})


def test_request_stop_on_active_invalidates_lease_and_arms():
    rec: dict = {"status": "In progress"}
    old = leases.bump(rec)
    assert stop.request_stop(rec, now=_NOW) is True
    assert rec["stopping"] is True
    assert "release_label" not in rec
    assert rec["stop_requested_at"] == _NOW.isoformat()
    # The worker's lease is invalidated immediately.
    assert leases.current(rec) != old
    assert not leases.holds(rec, old)


def test_the_stop_waits_on_the_invalidated_run_not_the_fresh_lease():
    rec: dict = {"status": "In progress"}
    old = leases.bump(rec)
    stop.request_stop(rec, now=_NOW)
    # The Runtime is asked to stop the OLD (invalidated) Run, not the fresh lease.
    assert stop.stopped_generation(rec) == old
    assert stop.stopped_generation(rec) != leases.current(rec)


def test_request_stop_on_idle_does_nothing():
    rec: dict = {"status": "Todo"}
    assert stop.request_stop(rec, now=_NOW) is False
    assert "stopping" not in rec


def test_request_stop_is_idempotent_while_stopping():
    rec: dict = {"status": "In progress"}
    leases.bump(rec)
    stop.request_stop(rec, now=_NOW)
    captured = rec["stop_waiting_generation"]
    later = _NOW + timedelta(minutes=5)
    assert stop.request_stop(rec, now=later) is True
    assert rec["stop_waiting_generation"] == captured  # not re-captured
    assert rec["stop_requested_at"] == _NOW.isoformat()


def test_reconcile_released_on_the_runtimes_proof():
    rec: dict = {"stop_waiting_generation": "g1", "stop_requested_at": _NOW.isoformat()}
    assert stop.reconcile_stop(rec, stopped=True) is StopDecision.RELEASED


def test_reconcile_waits_until_the_runtime_proves_the_stop():
    got = stop.reconcile_stop({"stop_waiting_generation": "g1"}, stopped=False)
    assert got is StopDecision.WAITING


def test_no_amount_of_waiting_releases_a_stop():
    """A dead or wedged worker is stopped by the Runtime, not released by a clock (#2159).

    The old two-hour window released a stop nothing had proved. A stop requested days ago
    with no Runtime answer is still waiting.
    """
    rec = {
        "stop_waiting_generation": "g1",
        "stop_requested_at": (_NOW - timedelta(days=3)).isoformat(),
    }
    assert stop.reconcile_stop(rec, stopped=False) is StopDecision.WAITING
    assert not hasattr(StopDecision, "TIMED_OUT")


def test_a_stop_that_awaits_no_generation_releases_now():
    """A stop armed on a card with no generation awaits the empty string: no Run ever
    started, so there is nothing for the Runtime to stop. #1608 and #1749 each held a worker
    slot for ~13 days in exactly this state (#2111)."""
    rec = {"stop_waiting_generation": ""}
    assert stop.reconcile_stop(rec, stopped=False) is StopDecision.RELEASED


def test_seconds_since_request():
    rec = {"stop_requested_at": _NOW.isoformat()}
    later = _NOW + timedelta(seconds=42)
    assert stop.seconds_since_request(rec, later) == 42
    assert stop.seconds_since_request({}, later) is None


def test_finalize_stop_clears_bookkeeping():
    rec: dict = {"status": "In progress", "branch": "feat/x"}
    leases.bump(rec)
    stop.request_stop(rec, now=_NOW)
    rec["stop_control"] = "c9"
    stop.finalize_stop(rec)
    cleared = (
        "stopping",
        "stop_waiting_generation",
        "stop_requested_at",
        "stop_control",
        "release_label",
    )
    for field in cleared:
        assert field not in rec
    # Unrelated fields (a resumable branch) survive for a later restart.
    assert rec["branch"] == "feat/x"


def test_a_terminal_marker_on_the_stopped_generation_ends_the_stop():
    """A worker already finishing when the stop arrives finishes (#299).

    Its terminal marker carries the generation the stop invalidated, which is the exact
    fact the stop is waiting for, and it also carries the status the work asked for.
    """
    rec: dict = {"status": "In progress"}
    leases.bump(rec)
    stop.request_stop(rec, now=_NOW)

    assert stop.reconcile_stop(rec, stopped=False, ended=True) is StopDecision.ENDED


def test_an_ended_turn_answers_more_than_the_runtime_so_it_is_read_first():
    rec: dict = {"status": "In progress", "stopping": True, "stop_waiting_generation": "g1"}
    assert stop.reconcile_stop(rec, stopped=True, ended=True) is StopDecision.ENDED
