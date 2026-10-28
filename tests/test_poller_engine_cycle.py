"""Slice 9 of #176: the wake — one cycle composing the engine modules behind one port.

Since #3732 the engine holds no cards: the deck runs every Work, so a wake starts nothing from
a label or a ``/claude`` comment and only does the project's housekeeping.
"""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import health
from agentdeck.poller_engine.health import HealthState
from agentdeck.poller_engine.ports import EnginePort


def test_port_protocol_is_satisfied_by_the_fake():
    assert isinstance(FakePort(), EnginePort)


def test_a_card_left_in_an_old_state_file_is_neither_read_nor_rewritten(tmp_path):
    initial = {"issues": {"store#5": {"status": "In progress", "generation": "g"}}}
    store = store_for(tmp_path, initial)
    port = FakePort()

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.halted is None
    assert store.read()["issues"] == initial["issues"]
    assert outcome.effects == []


def test_exhausted_graphql_budget_defers_the_live_wake(tmp_path):
    store = store_for(tmp_path)
    port = FakePort(graphql_deferred=True)

    outcome = run_wake(store, port, now=_NOW)

    assert outcome.halted == "graphql-budget"
    assert "GraphQL reserve reached" in " ".join(outcome.log)
    assert "last_live_wake" not in store.read()


def test_exhausted_core_budget_defers_live_wake_with_a_distinct_reason(tmp_path):
    port = FakePort(core_deferred=True)

    outcome = run_wake(store_for(tmp_path), port, now=_NOW)

    assert outcome.halted == "core-budget"
    assert "REST core reserve reached" in " ".join(outcome.log)


def test_healthy_budget_runs_the_live_wake_and_stamps_it(tmp_path):
    store = store_for(tmp_path)
    port = FakePort()

    outcome = run_wake(store, port, now=_NOW)
    later = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert outcome.halted is None and later.halted is None
    assert port.live_query_checks == 2
    assert store.read()["last_live_wake"] == (_NOW + timedelta(seconds=30)).isoformat()


# --- refusal to run ---


def test_drift_refuses_the_cycle_and_still_reports_health(tmp_path):
    port = FakePort()
    outcome = run_wake(store_for(tmp_path), port, now=_NOW, drift="engine")
    assert outcome.halted == "drift"
    assert outcome.verdict.state is HealthState.FAILING
    assert port.live_query_checks == 0


def test_a_paused_instance_does_no_work_but_stays_fresh(tmp_path):
    store = store_for(tmp_path)
    with store.transaction() as state:
        health.set_paused(state, True, note="release freeze")
    port = FakePort()
    outcome = run_wake(store, port, now=_NOW)
    assert outcome.halted == "paused"
    assert outcome.verdict.state is HealthState.PAUSED
    assert port.live_query_checks == 0
