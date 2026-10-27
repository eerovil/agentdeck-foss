"""Slice 8 of #176: structured health record, freshness, and the durable pause note."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.poller_engine import health
from agentdeck.poller_engine.health import (
    CycleReport,
    Freshness,
    HealthReason,
    HealthState,
)

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_INTERVAL = health.DEFAULT_INTERVAL_S


def _cycle(state: dict, *, at: datetime = _NOW, **kwargs) -> dict:
    report = CycleReport(**kwargs)
    return health.record_cycle(state, report, now=at)


# --- the durable record ---

def test_a_clean_cycle_is_recorded_with_counters_and_no_streak():
    state: dict = {}
    current = _cycle(
        state, scans=3, commands_ingested=2, queue_depth=1, dispatch_attempts=1, releases=1
    )
    assert current["cycles"] == 1
    assert current["last_cycle_at"] == _NOW.isoformat()
    assert current["totals"]["scans"] == 3
    assert current["totals"]["releases"] == 1
    assert current["last_report"]["queue_depth"] == 1
    assert current["error_streak"] == 0
    assert "last_error" not in current


def test_totals_accumulate_across_cycles_while_the_last_report_is_this_wake_only():
    state: dict = {}
    _cycle(state, scans=2, queue_depth=5)
    current = _cycle(state, scans=1, queue_depth=0, at=_NOW + timedelta(seconds=30))
    assert current["cycles"] == 2
    assert current["totals"]["scans"] == 3
    assert current["last_report"]["queue_depth"] == 0


def test_a_failing_cycle_is_visible_in_durable_state_not_just_a_log():
    state: dict = {}
    report = CycleReport(scans=1)
    report.failed("gh: server error 502")
    current = health.record_cycle(state, report, now=_NOW)
    assert current["error_streak"] == 1
    assert current["last_error"] == "gh: server error 502"
    assert current["last_error_at"] == _NOW.isoformat()
    assert current["last_report"]["errors"] == ["gh: server error 502"]


def test_a_clean_cycle_clears_the_error_streak():
    state: dict = {}
    for _ in range(2):
        report = CycleReport()
        report.failed("boom")
        health.record_cycle(state, report, now=_NOW)
    assert health.record(state)["error_streak"] == 2
    current = _cycle(state)
    assert current["error_streak"] == 0


def test_a_stuck_lock_or_drift_counts_as_a_failed_cycle_without_an_error_string():
    state: dict = {}
    current = _cycle(state, stuck_lock=True)
    assert current["error_streak"] == 1
    assert current["last_error"] == "state lock unavailable"

    other: dict = {}
    current = _cycle(other, drift="engine")
    assert current["error_streak"] == 1
    assert current["last_error"] == "engine"


# --- freshness ---

def test_freshness_tracks_the_expected_wake_interval():
    state: dict = {}
    _cycle(state)
    assert health.freshness(health.record(state), now=_NOW, interval_s=_INTERVAL)[0] is (
        Freshness.FRESH
    )
    late = _NOW + timedelta(seconds=_INTERVAL * 3)
    assert health.freshness(health.record(state), now=late, interval_s=_INTERVAL)[0] is (
        Freshness.LATE
    )
    stale = _NOW + timedelta(seconds=_INTERVAL * 11)
    fresh, age = health.freshness(health.record(state), now=stale, interval_s=_INTERVAL)
    assert fresh is Freshness.STALE
    assert age == _INTERVAL * 11


def test_an_instance_that_never_reported_is_stale_not_healthy():
    fresh, age = health.freshness({}, now=_NOW)
    assert fresh is Freshness.STALE
    assert age is None


# --- effective health = state x freshness ---

def test_a_clean_fresh_cycle_is_ok():
    state: dict = {}
    _cycle(state, scans=1)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.OK
    assert verdict.freshness is Freshness.FRESH
    assert verdict.reasons == ()


def test_a_dead_poller_can_never_show_a_green_light():
    state: dict = {}
    _cycle(state, scans=1)
    verdict = health.evaluate(state, now=_NOW + timedelta(hours=1))
    assert verdict.state is HealthState.STALE
    assert verdict.freshness is Freshness.STALE
    assert HealthReason.TIMER_NOT_FIRING in verdict.reasons
    assert verdict.age_seconds == 3600


def test_lateness_is_reported_without_calling_the_instance_unhealthy():
    state: dict = {}
    _cycle(state, scans=1)
    verdict = health.evaluate(state, now=_NOW + timedelta(seconds=_INTERVAL * 3))
    assert verdict.state is HealthState.OK
    assert verdict.freshness is Freshness.LATE


def test_drift_is_fail_closed_and_outranks_everything():
    state: dict = {}
    _cycle(state, drift="contract")
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.FAILING
    assert HealthReason.CONFIG_DRIFT in verdict.reasons

    other: dict = {}
    _cycle(other, drift="engine")
    assert HealthReason.ENGINE_DRIFT in health.evaluate(other, now=_NOW).reasons


def test_a_stuck_lock_fails_closed():
    state: dict = {}
    _cycle(state, stuck_lock=True)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.FAILING
    assert HealthReason.STUCK_LOCK in verdict.reasons


def test_a_repeated_failure_streak_is_degradation():
    state: dict = {}
    for _ in range(health.ERROR_STREAK_THRESHOLD):
        report = CycleReport()
        report.failed("gh timeout")
        health.record_cycle(state, report, now=_NOW)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.DEGRADED
    assert HealthReason.ERROR_STREAK in verdict.reasons


def test_one_unlucky_cycle_is_not_degradation():
    state: dict = {}
    report = CycleReport()
    report.failed("gh timeout")
    health.record_cycle(state, report, now=_NOW)
    assert health.evaluate(state, now=_NOW).state is HealthState.OK


def test_an_unreachable_worker_backend_is_degradation_not_failure():
    state: dict = {}
    _cycle(state, worker_backend_ok=False)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.DEGRADED
    assert HealthReason.WORKER_DELIVERY_DEGRADED in verdict.reasons


def test_reading_state_from_the_backup_is_surfaced():
    state: dict = {}
    _cycle(state, state_fallback=True)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.DEGRADED
    assert HealthReason.STATE_FALLBACK in verdict.reasons


def test_a_budget_hold_is_normal_operation_not_ill_health():
    state: dict = {}
    _cycle(state, budget_holds=2, queue_depth=4)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.OK
    assert verdict.reasons == ()
    # The hold is still counted, so an observer can explain the waiting queue.
    assert health.record(state)["totals"]["budget_holds"] == 2


# --- the pause note ---

def test_a_paused_instance_stays_fresh_and_visibly_paused():
    state: dict = {}
    health.set_paused(state, True, note="release freeze")
    _cycle(state, scans=1)
    verdict = health.evaluate(state, now=_NOW)
    assert verdict.state is HealthState.PAUSED
    assert verdict.freshness is Freshness.FRESH
    assert health.is_paused(state) is True
    assert health.pause_note(state) == "release freeze"


def test_a_paused_instance_that_stopped_reporting_is_still_stale():
    state: dict = {}
    health.set_paused(state, True)
    _cycle(state, scans=1)
    verdict = health.evaluate(state, now=_NOW + timedelta(hours=1))
    assert verdict.state is HealthState.STALE
    # Pause explains silence only while the instance keeps waking, so no timer reason.
    assert HealthReason.TIMER_NOT_FIRING not in verdict.reasons


def test_resuming_clears_the_pause_note():
    state: dict = {}
    health.set_paused(state, True, note="freeze")
    health.set_paused(state, False)
    assert health.is_paused(state) is False
    assert health.pause_note(state) == ""
