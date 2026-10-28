"""The Runtime owns whether a Run's worker is still running, and stops it (#2159).

Every test here goes through the identity #2142 and #2146 already record — the frozen delivery
route and the worker row's own ``work_id``/``run_id`` — and never through a second mapping. The
store keeps only the outcome, which is what lets a restarted Runtime still say how a Run ended.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import run_execution as rx
from agentdeck.config import AccountConfig, AppConfig
from agentdeck.db import Db, DeliveryRoute
from agentdeck.models import SNAPSHOT_BOOTED_AT_KEY
from agentdeck.providers.delivery import DeliverResult

pytestmark = pytest.mark.anyio

WORK = "github_issue:eerovil/agentdeck#2159"
KEY = "agentdeck#2159"
T0 = 1_800_000_000.0
HOUR = 3600.0


class Host:
    """A keyed worker host reduced to what the Runtime reads and does for a stop."""

    def __init__(self, workers: dict | None = None, *, booted_at: float = T0 - HOUR):
        self.workers = workers or {}
        self.booted_at = booted_at
        self.parked: list[str] = []
        self.unreadable = False
        # What parking does to the row: Claude terminates the process.
        self.on_park = lambda row: row.update(
            live=False, turn_active=False, effective_activity=False
        )

    def snapshot(self) -> dict:
        if self.unreadable:
            raise OSError("host is not answering")
        return {"workers": self.workers, SNAPSHOT_BOOTED_AT_KEY: self.booted_at}

    async def park_worker(self, key: str) -> DeliverResult:
        self.parked.append(key)
        row = self.workers.get(key)
        if row is not None:
            self.on_park(row)
        return DeliverResult(True, "parked")


def _row(run_id: str = "run-1", **overrides) -> dict:
    row = {
        "session_id": "s1",
        "cwd": "/w/issue-2159",
        "live": True,
        "turn_active": True,
        "effective_activity": True,
        "last_delivery_at": T0,
        "last_activity_at": T0 + 60,
        "last_result_at": 0.0,
        "last_result_subtype": "",
        "pending_interaction": None,
        "work_id": WORK,
        "run_id": run_id,
        "run_session_id": "s1",
        "run_delivery_id": f"poller:{run_id}",
    }
    row.update(overrides)
    return row


def _db(tmp_path, *runs: str) -> Db:
    db = Db(tmp_path / "deck.db", 30)
    for index, run_id in enumerate(runs):
        assert db.freeze_delivery_route(
            DeliveryRoute(
                delivery_id=f"poller:{run_id}",
                request_fingerprint="f",
                work_id=WORK,
                run_id=run_id,
                provider="claude_code",
                account="main",
                worker_key=KEY,
                decided_at=datetime.fromtimestamp(T0 + index, UTC).isoformat(),
            )
        )
    return db


def _executions(tmp_path, host: Host, db: Db, *, now: float = T0 + 120) -> rx.RunExecutions:
    async def park(provider, account, key):
        return await host.park_worker(key)

    return rx.RunExecutions(
        rx.OutcomeStore(tmp_path / "deck.db"),
        hosts=lambda: {("claude_code", "main"): host},
        db=db,
        park=park,
        clock=lambda: now,
    )


# --- what the Runtime reports (#2159 test 6) --------------------------------


@pytest.mark.parametrize(
    "row,state,reason",
    [
        (_row(), rx.RUNNING, "turn_active"),
        (_row(pending_interaction={"id": "q"}), rx.WAITING_HUMAN, "pending_interaction"),
        (
            _row(turn_active=False, effective_activity=False, last_result_at=T0 + 90,
                 last_result_subtype="success"),
            rx.ENDED,
            "success",
        ),
        (
            _row(turn_active=False, effective_activity=False, live=False),
            rx.DIED,
            "process_gone",
        ),
        (
            _row(turn_active=False, effective_activity=False, last_result_at=T0 + 90,
                 last_result_refusal="usage_limit"),
            rx.ENDED,
            "refused:usage_limit",
        ),
    ],
)
def test_the_runtime_reports_each_ending_directly(row, state, reason):
    got, why, _activity, _ended = rx.classify(row, booted_at=T0 - HOUR, now=T0 + 120)
    assert (got, why) == (state, reason)


def test_a_result_older_than_this_delivery_is_not_this_runs_ending():
    row = _row(turn_active=False, effective_activity=False, last_result_at=T0 - 10)
    assert rx.classify(row, booted_at=T0 - HOUR, now=T0 + 120)[0] == rx.RUNNING


def test_an_open_turn_silent_for_hours_is_abandoned_not_running():
    """#1400: a Codex thread wedges without moving any field but the clock."""
    row = _row(last_activity_at=T0)
    assert rx.classify(row, booted_at=T0 - HOUR, now=T0 + 3 * HOUR)[0] == rx.ABANDONED
    assert rx.classify(row, booted_at=T0 - HOUR, now=T0 + 60)[0] == rx.RUNNING


def test_a_row_that_never_saw_a_turn_is_not_running_forever():
    row = _row(turn_active=False, effective_activity=False, last_delivery_at=0.0,
               last_activity_at=0.0)
    assert rx.classify(row, booted_at=T0 - HOUR, now=T0)[0] == rx.ENDED


def test_a_worker_blocked_on_a_person_is_never_abandoned():
    row = _row(last_activity_at=T0, pending_interaction={"id": "q"})
    assert rx.classify(row, booted_at=T0 - HOUR, now=T0 + 9 * HOUR)[0] == rx.WAITING_HUMAN


def test_a_runtime_restart_is_uncertain_never_dead_and_never_stopped():
    """#1659, #2159 test 9: the restart leaves the same fingerprint a kill does. Unknown after
    a restart is not stopped, so it must not read as either."""
    row = _row(turn_active=False, effective_activity=False, live=False)
    got = rx.classify(row, booted_at=T0 + 30, now=T0 + 120)
    assert got[:2] == (rx.UNCERTAIN, "runtime_restarted")
    assert rx.UNCERTAIN not in rx.SAFE_STATES


async def test_a_run_is_found_through_its_route_and_its_rows_own_correlation(tmp_path):
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    outcome = executions.outcome(WORK, "run-1")

    assert (outcome.execution_state, outcome.provider, outcome.account) == (
        rx.RUNNING, "claude_code", "main",
    )
    assert (outcome.worker_key, outcome.session_id) == (KEY, "s1")
    assert outcome.last_activity_at == T0 + 60


async def test_the_outcome_carries_the_refusal_its_route_was_frozen_with(tmp_path):
    """#2200: the Poller reads this instead of a watch file it once stamped itself."""
    db = _db(tmp_path)
    assert db.freeze_delivery_route(
        DeliveryRoute(
            "poller:run-1", "f", WORK, "run-1", "claude_code", "main", KEY,
            datetime.fromtimestamp(T0, UTC).isoformat(), "seven_day window at 100%",
        )
    )
    host = Host({KEY: _row(turn_active=False, effective_activity=False, live=False)})
    executions = _executions(tmp_path, host, db)

    outcome = executions.outcome(WORK, "run-1")

    assert outcome.execution_state == rx.DIED
    assert outcome.as_json()["refused_at_delivery"] == "seven_day window at 100%"
    # Settled, it is still carried: the stored answer is read with the route's identity.
    host.workers.clear()
    assert executions.outcome(WORK, "run-1").refused_at_delivery == "seven_day window at 100%"


async def test_an_older_run_on_the_same_worker_reads_as_over_not_running(tmp_path):
    """The worker's row names run-2 now: whatever it is doing, it is not run-1."""
    host = Host({KEY: _row("run-2")})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1", "run-2"))

    assert executions.outcome(WORK, "run-1").execution_state == rx.ENDED
    assert executions.outcome(WORK, "run-2").execution_state == rx.RUNNING


async def test_a_run_nothing_was_ever_routed_to_has_not_started(tmp_path):
    executions = _executions(tmp_path, Host(), _db(tmp_path))
    assert executions.outcome(WORK, "run-9").execution_state == rx.NOT_STARTED


async def test_an_unreadable_host_is_uncertain(tmp_path):
    host = Host({KEY: _row()})
    host.unreadable = True
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    assert executions.outcome(WORK, "run-1").execution_state == rx.UNCERTAIN


# --- durable and restart-safe (#2159 test 9) --------------------------------


async def test_the_outcome_survives_a_runtime_restart(tmp_path):
    db = _db(tmp_path, "run-1")
    host = Host({KEY: _row(turn_active=False, effective_activity=False,
                           last_result_at=T0 + 90, last_result_subtype="success")})
    _executions(tmp_path, host, db).outcome(WORK, "run-1")

    # A fresh Runtime: new store connection, and the worker has since been released.
    reborn = _executions(tmp_path, Host({}), db, now=T0 + 5 * HOUR)
    outcome = reborn.outcome(WORK, "run-1")

    assert outcome.execution_state == rx.ENDED
    assert outcome.reason_code == "success"
    assert outcome.terminal_result_at == T0 + 90


async def test_a_running_run_that_loses_its_process_to_a_restart_stays_uncertain(tmp_path):
    db = _db(tmp_path, "run-1")
    host = Host({KEY: _row()})
    _executions(tmp_path, host, db).outcome(WORK, "run-1")

    host.workers[KEY].update(live=False, turn_active=False, effective_activity=False)
    host.booted_at = T0 + 200
    outcome = _executions(tmp_path, host, db, now=T0 + 300).outcome(WORK, "run-1")

    assert outcome.execution_state == rx.UNCERTAIN
    assert not outcome.safe


# --- stop (#2159 tests 1, 2, 3) ---------------------------------------------


async def test_stopping_a_running_worker_ends_its_process_and_records_it(tmp_path):
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    result, outcome = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_STOPPED
    assert host.parked == [KEY]
    assert outcome.execution_state == rx.STOPPED
    assert outcome.reason_code == "superseded"
    # Durable: a later read still says stopped, not the death the stop caused.
    assert executions.outcome(WORK, "run-1").execution_state == rx.STOPPED


async def test_a_dead_worker_is_already_stopped_and_needs_nothing_from_anyone(tmp_path):
    host = Host({KEY: _row(turn_active=False, effective_activity=False, live=False)})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    result, _outcome = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_ALREADY_STOPPED
    assert host.parked == []


async def test_a_stop_is_idempotent(tmp_path):
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    await executions.stop(WORK, "run-1", "superseded")

    again, _ = await executions.stop(WORK, "run-1", "superseded")

    assert again == rx.STOP_ALREADY_STOPPED
    assert host.parked == [KEY]


async def test_a_turn_that_will_not_end_is_still_stopping_never_stopped(tmp_path):
    host = Host({KEY: _row()})
    host.on_park = lambda row: None  # the provider accepted the interrupt; the turn went on
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    result, outcome = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_STILL_STOPPING
    assert not outcome.safe


async def test_a_stop_the_runtime_cannot_see_is_uncertain(tmp_path):
    host = Host({KEY: _row()})
    host.unreadable = True
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    result, _ = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_UNCERTAIN
    assert host.parked == []


async def test_stopping_an_old_run_never_touches_the_worker_now_serving_a_newer_one(tmp_path):
    host = Host({KEY: _row("run-2")})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1", "run-2"))

    result, _ = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_ALREADY_STOPPED
    assert host.parked == []


# --- admission and workspace release (#2159 tests 4, 5) ---------------------


async def test_an_uncertain_older_run_blocks_a_new_run_on_the_same_work(tmp_path):
    host = Host({KEY: _row(turn_active=False, effective_activity=False, live=False)})
    host.booted_at = T0 + 30  # restarted after the delivery: uncertain
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    blocking = executions.conflicting(WORK, "run-2")

    assert blocking is not None and blocking.execution_state == rx.UNCERTAIN


async def test_a_proved_stop_lets_the_next_run_use_the_same_workspace(tmp_path):
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    assert executions.conflicting(WORK, "run-2") is not None
    assert executions.work_active(WORK) is True

    await executions.stop(WORK, "run-1", "superseded")

    assert executions.conflicting(WORK, "run-2") is None
    assert executions.work_active(WORK) is False


async def test_the_same_run_is_never_its_own_conflict(tmp_path):
    executions = _executions(tmp_path, Host({KEY: _row()}), _db(tmp_path, "run-1"))
    assert executions.conflicting(WORK, "run-1") is None


async def test_a_work_the_runtime_never_routed_is_not_running(tmp_path):
    """No route, no outcome, no worker row: no Run ever reached a provider (#2197)."""
    executions = _executions(tmp_path, Host(), _db(tmp_path))
    assert executions.work_active(WORK) is False


async def test_a_worker_row_alone_is_not_a_run(tmp_path):
    """Every Run that reached a provider froze a route first; a row naming a Run with no
    route is not one of the Work's Runs (#2214)."""
    host = Host({KEY: _row("run-unrouted")})
    executions = _executions(tmp_path, host, _db(tmp_path))

    assert executions.outcomes(WORK) == []
    assert executions.work_active(WORK) is False
    assert executions.outcome(WORK, "run-unrouted").execution_state == rx.NOT_STARTED


async def test_an_unreadable_host_holding_a_routed_run_is_not_read_as_nothing_ran(tmp_path):
    host = Host({KEY: _row()})
    host.unreadable = True
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    assert executions.work_active(WORK) is None
    blocking = executions.conflicting(WORK, "run-2")
    assert blocking is not None and blocking.execution_state == rx.UNCERTAIN
    assert await executions.admit(WORK, "run-2") is not None


async def test_an_unreadable_host_with_no_route_does_not_block_a_settled_work(tmp_path):
    """A host this Work was never routed to holds none of its Runs (#2214)."""
    readable = Host({KEY: _row("run-1", live=False, turn_active=False, effective_activity=False)})
    silent = Host()
    silent.unreadable = True
    executions = _executions(tmp_path, readable, _db(tmp_path, "run-1"))
    executions.hosts = lambda: {
        ("claude_code", "main"): readable, ("claude_code", "alt"): silent,
    }
    await executions.stop(WORK, "run-1", "superseded")

    assert all(o.safe for o in executions.outcomes(WORK))
    assert executions.work_active(WORK) is False
    assert executions.conflicting(WORK, "run-2") is None
    assert await executions.admit(WORK, "run-2") is None


def _runtime_config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "control.db")},
            "accounts": [
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path)
                ).model_dump()
            ],
        }
    )


class AppHost(Host):
    """``Host`` with the rest of what the runtime's admission reads and writes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delivered: list[str] = []

    async def deliver(self, key, message, **kwargs):
        self.delivered.append(kwargs.get("delivery_id") or "")
        return DeliverResult(True, "steered", session_id="s1")

    def delivery_receipt(self, *_args):
        return None


def _runtime(tmp_path, host: AppHost, *runs: str, now: float = T0 + 120):
    from agentdeck.runtime import create_runtime_app

    app = create_runtime_app(_runtime_config(tmp_path))
    app.state.claude_workers.hosts["main"] = host
    executions = app.state.run_executions
    executions.hosts = lambda: {("claude_code", "main"): host}
    executions.db = _db(tmp_path, *runs)
    executions.clock = lambda: now
    return app


async def test_a_new_run_landing_in_the_same_live_worker_is_not_a_conflict(tmp_path):
    """#2146's mid-turn pin sends the new Run into the card's own worker, which takes it
    itself: no second execution in the workspace, so nothing is stopped. That is only known
    once the target is — without one, nothing is exempt."""
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    main = ("claude_code", "main")

    assert await executions.admit(WORK, "run-2", worker_key=KEY, target=main) is None
    assert host.parked == []
    # Written to another account, it would be a second worker in the same workspace.
    assert await executions.admit(WORK, "run-2", worker_key=KEY, target=("codex", "x")) is None
    assert host.parked == [KEY]


async def test_an_unknown_target_is_never_proof_the_run_lands_in_the_old_worker(tmp_path):
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    await executions.admit(WORK, "run-2", worker_key=KEY)

    assert host.parked == [KEY]


async def test_a_pending_turn_is_not_exempt_on_another_account(tmp_path):
    """The review's race: a delivered turn that has not started yet reads `running` but no
    scheduler pins it. A new Run placed elsewhere must stop it first."""
    pending = _row(turn_active=False, effective_activity=False)
    host = Host({KEY: pending})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    assert executions.outcome(WORK, "run-1").reason_code == "turn_pending"

    await executions.admit(WORK, "run-2", worker_key=KEY, target=("claude_code", "alt"))

    assert host.parked == [KEY]


async def test_a_row_naming_another_work_is_never_this_works_run(tmp_path):
    host = Host({KEY: _row(run_id="", work_id="github_issue:eerovil/other#1")})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    assert executions.outcome(WORK, "run-1").execution_state == rx.ENDED


async def test_an_older_run_never_stops_a_newer_one(tmp_path):
    host = Host({KEY: _row("run-2")})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1", "run-2"))

    blocking = await executions.admit(WORK, "run-1")

    assert blocking is not None and blocking.run_id == "run-2"
    assert host.parked == []


async def test_a_stop_after_a_runtime_restart_is_still_a_stop(tmp_path):
    """The stop ended whatever ran; the restart's uncertainty does not outlive it."""
    host = Host({KEY: _row()})
    host.booted_at = T0 + 30
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    result, outcome = await executions.stop(WORK, "run-1", "superseded")

    assert result == rx.STOP_STOPPED
    assert executions.outcome(WORK, "run-1").execution_state == rx.STOPPED


def test_a_misspelled_work_is_refused_rather_than_read_as_never_started():
    with pytest.raises(ValueError):
        rx.canonical_work_id("github_issue:Eerovil/Agentdeck#2159")
    with pytest.raises(ValueError):
        rx.StopExecutionRequest(work_id="github_issue:/agentdeck#1", run_id="r")
    assert rx.canonical_work_id(WORK) == WORK


# --- a restart revival cannot resume a stopped Run (review of #2189) ---------


async def test_a_revival_after_the_stop_is_refused_under_the_same_lock(tmp_path):
    """The boot revival snapshots survivors first and delivers later. A stop landing in
    between records its request before it parks, and the revival re-checks under the
    admission lock immediately before writing — so it is refused, and the stop that turned
    the restart's uncertainty into STOPPED stays true."""
    row = _row(turn_active=False, effective_activity=False, live=False)
    host = AppHost({KEY: row}, booted_at=T0 + 30)
    app = _runtime(tmp_path, host, "run-1")
    executions = app.state.run_executions
    assert executions.outcome(WORK, "run-1").execution_state == rx.UNCERTAIN

    result, _ = await executions.stop(WORK, "run-1", "superseded")
    revived = await app.state.restart_nudges._deliver_keyed(
        "claude_code", "main", host, KEY, "delegated",
        message="the runtime restarted", delivery_id="revive:x",
    )

    assert result == rx.STOP_STOPPED
    assert (revived.accepted, revived.reason) == (False, rx.RUN_STOPPED)
    assert host.delivered == []
    assert executions.outcome(WORK, "run-1").execution_state == rx.STOPPED


async def test_a_revival_that_lands_before_the_stop_is_ended_by_it(tmp_path):
    row = _row(turn_active=False, effective_activity=False, live=False)
    host = AppHost({KEY: row}, booted_at=T0 + 30)
    app = _runtime(tmp_path, host, "run-1")

    revived = await app.state.restart_nudges._deliver_keyed(
        "claude_code", "main", host, KEY, "delegated",
        message="the runtime restarted", delivery_id="revive:x",
    )
    row.update(live=True, turn_active=True, effective_activity=True, last_delivery_at=T0 + 60)
    result, _ = await app.state.run_executions.stop(WORK, "run-1", "superseded")

    assert revived.accepted and host.delivered == ["revive:x"]
    assert (result, host.parked) == (rx.STOP_STOPPED, [KEY])


async def test_a_worker_serving_a_run_nobody_stopped_is_still_revived(tmp_path):
    host = AppHost({KEY: _row()}, booted_at=T0 + 30)
    app = _runtime(tmp_path, host, "run-1")

    revived = await app.state.restart_nudges._deliver_keyed(
        "claude_code", "main", host, KEY, "delegated",
        message="the runtime restarted", delivery_id="revive:x",
    )

    assert revived.accepted


# --- the read API a Run view can consume (#2141) ----------------------------


async def test_the_runtime_serves_the_outcome_and_the_stop_over_its_api(tmp_path):
    from agentdeck.runtime import create_runtime_app

    app = create_runtime_app(_runtime_config(tmp_path))
    host = Host({KEY: _row()})
    app.state.claude_workers.hosts["main"] = host
    executions = app.state.run_executions
    executions.hosts = lambda: {("claude_code", "main"): host}
    executions.db = _db(tmp_path, "run-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        one = await client.get(
            rx.EXECUTION_OUTCOME_PATH, params={"work_id": WORK, "run_id": "run-1"}
        )
        stopped = await client.post(
            rx.EXECUTION_STOP_PATH,
            json={"work_id": WORK, "run_id": "run-1", "reason": "superseded"},
        )
        every = await client.get(rx.EXECUTIONS_PATH, params={"work_id": WORK})

    assert one.json()["execution_state"] == rx.RUNNING
    assert set(one.json()) >= {
        "work_id", "run_id", "execution_state", "last_activity_at", "terminal_result_at",
        "reason_code", "provider", "account", "session_id",
    }
    assert stopped.json()["result"] == rx.STOP_STOPPED
    assert [item["execution_state"] for item in every.json()["executions"]] == [rx.STOPPED]


async def test_the_outcome_api_names_an_ended_turns_uncollected_background_commands(tmp_path):
    """#2890: a chat that ended its turn while its own background command runs is waiting for
    that command to wake it, so the Poller is told which commands are still out."""
    from agentdeck.runtime import create_runtime_app

    ended = _row(
        turn_active=False, effective_activity=False, last_result_at=T0 + 90,
        last_result_subtype="success",
    )
    host = Host({KEY: ended})
    asked: list[str] = []

    def outstanding(key):
        asked.append(key)
        return frozenset({"bexm7vbjv", "a1"})

    host.outstanding_background_tasks = outstanding
    app = create_runtime_app(_runtime_config(tmp_path))
    app.state.claude_workers.hosts["main"] = host
    executions = app.state.run_executions
    executions.hosts = lambda: {("claude_code", "main"): host}
    executions.db = _db(tmp_path, "run-1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        one = await client.get(
            rx.EXECUTION_OUTCOME_PATH, params={"work_id": WORK, "run_id": "run-1"}
        )

    assert one.json()["execution_state"] == rx.ENDED
    assert one.json()["background_tasks"] == ["a1", "bexm7vbjv"]
    assert asked == [KEY]
    # Only the API asks: the store and every other caller never pay the transcript read.
    assert executions.outcome(WORK, "run-1").background_tasks == ()
    assert asked == [KEY]


def test_background_commands_are_read_only_for_an_ended_turn(tmp_path):
    host = Host({KEY: _row()})
    host.outstanding_background_tasks = lambda key: pytest.fail("a running turn was read")
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    assert executions.outcome(WORK, "run-1", background=True).background_tasks == ()


def test_unreadable_background_commands_hold_nothing(tmp_path):
    ended = _row(
        turn_active=False, effective_activity=False, last_result_at=T0 + 90,
        last_result_subtype="success",
    )
    host = Host({KEY: ended})

    def broken(_key):
        raise OSError("transcript is gone")

    host.outstanding_background_tasks = broken
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    outcome = executions.outcome(WORK, "run-1", background=True)
    assert (outcome.execution_state, outcome.background_tasks) == (rx.ENDED, ())


def test_a_chat_whose_process_is_gone_waits_on_no_background_command(tmp_path):
    """Nothing is left to wake: its ledger's uncollected commands hold nothing."""
    ended = _row(
        live=False, turn_active=False, effective_activity=False, last_result_at=T0 + 90,
        last_result_subtype="success",
    )
    host = Host({KEY: ended})
    host.outstanding_background_tasks = lambda key: frozenset({"b1"})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))

    outcome = executions.outcome(WORK, "run-1", background=True)
    assert (outcome.execution_state, outcome.background_tasks) == (rx.ENDED, ())


async def test_without_execution_state_a_revival_of_a_runs_worker_is_refused(
    tmp_path, monkeypatch
):
    """Review of #2189, round 4: "unavailable" is one rule. A keyed revival or notice to a
    worker serving a Work/Run cannot be checked against a stop, so it is refused; a worker
    serving no Run is an ordinary chat and is still delivered."""
    from agentdeck.runtime import create_runtime_app

    def broken(*_args, **_kwargs):
        raise OSError("outcome store could not open")

    monkeypatch.setattr(rx, "OutcomeStore", broken)
    app = create_runtime_app(_runtime_config(tmp_path))
    assert app.state.run_executions is None
    host = AppHost({KEY: _row(), "plain-chat": {**_row(), "work_id": None, "run_id": None}})
    nudges = app.state.restart_nudges

    fenced = await nudges._deliver_keyed(
        "claude_code", "main", host, KEY, "delegated", message="restarted", delivery_id="r:1"
    )
    plain = await nudges._deliver_keyed(
        "claude_code", "main", host, "plain-chat", "primary", message="restarted",
        delivery_id="r:2",
    )

    assert (fenced.accepted, fenced.reason) == (False, rx.RUN_STOPPED)
    assert plain.accepted
    assert host.delivered == ["r:2"]


def test_an_unreadable_stop_record_fences_rather_than_resuming(tmp_path):
    executions = _executions(tmp_path, Host(), _db(tmp_path))

    def broken(*_args):
        raise OSError("locked")

    executions.store.get = broken
    assert executions.stop_requested_for(_row()) is True


# --- the Runtime looks again at every open Run (#2586) ----------------------


async def test_a_turn_seen_running_reads_ended_once_it_has_ended(tmp_path):
    """#2586: the worker reported from inside its turn, so the only observation was running."""
    host = Host({KEY: _row()})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    assert executions.outcome(WORK, "run-1").execution_state == rx.RUNNING

    host.workers[KEY].update(turn_active=False, effective_activity=False,
                             last_result_at=T0 + 90, last_result_subtype="success")

    assert executions.refresh_open() == 1
    assert executions.store.get(WORK, "run-1").execution_state == rx.ENDED
    assert executions.store.open_runs() == []
    assert executions.refresh_open() == 0


async def test_the_refresh_leaves_settled_and_replaced_runs_alone(tmp_path):
    db = _db(tmp_path, "run-1", "run-2", "run-3")
    executions = _executions(tmp_path, Host({KEY: _row("run-3")}), db)
    executions.store.record(rx.ExecutionOutcome(WORK, "run-1", rx.RUNNING, observed_at=T0))
    executions.store.record(rx.ExecutionOutcome(WORK, "run-2", rx.ENDED, observed_at=T0))
    executions.store.record(rx.ExecutionOutcome(WORK, "run-3", rx.RUNNING, observed_at=T0))
    seen: list[tuple[str, str]] = []
    observe = executions.outcome

    def outcome(work_id, run_id):
        seen.append((work_id, run_id))
        return observe(work_id, run_id)

    executions.outcome = outcome

    assert executions.refresh_open() == 0
    # run-1 was replaced by a later Run and run-2 has ended: only run-3 is asked again.
    assert seen == [(WORK, "run-3")]


async def test_one_run_that_cannot_be_refreshed_does_not_stop_the_rest(tmp_path):
    other = "github_issue:eerovil/agentdeck#2586"
    host = Host({KEY: _row(turn_active=False, effective_activity=False,
                           last_result_at=T0 + 90, last_result_subtype="success")})
    executions = _executions(tmp_path, host, _db(tmp_path, "run-1"))
    executions.store.record(rx.ExecutionOutcome(other, "run-9", rx.RUNNING, observed_at=T0))
    executions.store.record(rx.ExecutionOutcome(WORK, "run-1", rx.RUNNING, observed_at=T0))
    observe = executions.outcome

    def outcome(work_id, run_id):
        if work_id == other:
            raise OSError("store is locked")
        return observe(work_id, run_id)

    executions.outcome = outcome

    assert executions.refresh_open() == 1
    assert executions.store.get(WORK, "run-1").execution_state == rx.ENDED
    assert executions.store.get(other, "run-9").execution_state == rx.RUNNING


# --- a Run's chat continued on another account (#2862) ----------------------
#
# Seen on #2861: the worker's account hit its usage limit, a person pressed Continue in new chat
# on another account, and 40 seconds later the Poller read the old leg's "success", revived the
# card, and two workers did the same work side by side.

ALT_SESSION = "claude_code:alt:s1"
NEW_SESSION = "claude_code:main:c1"


def _limited(**overrides) -> dict:
    """The row a turn the usage limit cut short leaves: the CLI calls it a success."""
    return _row(
        turn_active=False, effective_activity=False, last_result_at=T0 + 90,
        last_result_subtype="success",
        last_result_limit="You've hit your session limit · resets 12:30pm",
        **overrides,
    )


def _continuation(**overrides) -> dict:
    """The continued chat's worker: a deck chat, so it names no Work or Run of its own."""
    row = _row(session_id="c1", run_session_id="", last_delivery_at=T0 + 100,
               last_activity_at=T0 + 110)
    row.pop("work_id")
    row.pop("run_id")
    row.update(overrides)
    return row


def _alt_db(tmp_path) -> Db:
    db = Db(tmp_path / "deck.db", 30)
    assert db.freeze_delivery_route(
        DeliveryRoute(
            "poller:run-1", "f", WORK, "run-1", "claude_code", "alt", KEY,
            datetime.fromtimestamp(T0, UTC).isoformat(),
        )
    )
    return db


def _continue(db: Db, *, link: bool = True, token: str = "a" * 32) -> None:
    """What Continue in new chat writes: reserve, then link once the new chat names itself."""
    assert db.reserve_chat_successor(token, ALT_SESSION) == "new"
    if link:
        db.record_session_token(token, NEW_SESSION)


def _two_accounts(tmp_path, alt: Host, main: Host, db: Db, *, now: float = T0 + 120):
    async def park(provider, account, key):
        return await {"alt": alt, "main": main}[account].park_worker(key)

    return rx.RunExecutions(
        rx.OutcomeStore(tmp_path / "deck.db"),
        hosts=lambda: {("claude_code", "alt"): alt, ("claude_code", "main"): main},
        db=db,
        park=park,
        clock=lambda: now,
    )


def test_a_turn_the_usage_limit_cut_short_is_not_a_clean_success():
    got = rx.classify(_limited(), booted_at=T0 - HOUR, now=T0 + 120)
    assert got[:2] == (rx.ENDED, rx.USAGE_LIMIT)


async def test_a_continued_chat_keeps_its_run_running_on_the_new_account(tmp_path):
    db = _alt_db(tmp_path)
    alt, main = Host({KEY: _limited()}), Host({})
    executions = _two_accounts(tmp_path, alt, main, db)
    assert executions.outcome(WORK, "run-1").reason_code == rx.USAGE_LIMIT

    _continue(db)
    main.workers["deck-chat"] = _continuation()
    outcome = executions.outcome(WORK, "run-1")

    assert outcome.execution_state == rx.RUNNING
    assert (outcome.account, outcome.worker_key, outcome.session_id) == ("main", "deck-chat", "c1")
    assert not outcome.safe


async def test_a_continuation_that_ends_is_the_runs_ending(tmp_path):
    db = _alt_db(tmp_path)
    _continue(db)
    alt = Host({KEY: _limited()})
    main = Host({"deck-chat": _continuation(
        turn_active=False, effective_activity=False, last_result_at=T0 + 200,
        last_result_subtype="success",
    )})

    outcome = _two_accounts(tmp_path, alt, main, db, now=T0 + 300).outcome(WORK, "run-1")

    assert (outcome.execution_state, outcome.reason_code) == (rx.ENDED, "success")
    assert outcome.worker_key == "deck-chat"


async def test_a_continuation_still_starting_holds_the_run_until_it_goes_stale(tmp_path):
    db = _alt_db(tmp_path)
    _continue(db, link=False)
    alt, main = Host({KEY: _limited()}), Host({})
    reserved = db.chat_successor(ALT_SESSION)[1].timestamp()

    starting = _two_accounts(tmp_path, alt, main, db, now=reserved + 5).outcome(WORK, "run-1")
    assert (starting.execution_state, starting.reason_code) == (
        rx.RUNNING, "continuation_starting",
    )

    stale = reserved + rx.CONTINUATION_START_S + 1
    later = _two_accounts(tmp_path, alt, main, db, now=stale).outcome(WORK, "run-1")
    assert (later.execution_state, later.reason_code) == (rx.ENDED, rx.USAGE_LIMIT)


async def test_a_continued_leg_serving_another_run_is_not_this_runs(tmp_path):
    db = _alt_db(tmp_path)
    _continue(db)
    alt = Host({KEY: _limited()})
    main = Host({"deck-chat": _continuation(work_id=WORK, run_id="run-2")})

    outcome = _two_accounts(tmp_path, alt, main, db).outcome(WORK, "run-1")

    assert (outcome.execution_state, outcome.account) == (rx.ENDED, "alt")


async def test_a_continued_leg_naming_no_run_is_not_an_older_runs(tmp_path):
    """A leg that names no Run belongs to the Run the Work was last sent to, and to no older
    one: once run-2 took the Work, run-1's continued chat no longer keeps run-1 running."""
    db = _alt_db(tmp_path)
    _continue(db)
    assert db.freeze_delivery_route(
        DeliveryRoute(
            "poller:run-2", "f", WORK, "run-2", "claude_code", "alt", KEY,
            datetime.fromtimestamp(T0 + 60, UTC).isoformat(),
        )
    )
    alt = Host({KEY: _limited()})
    main = Host({"deck-chat": _continuation()})

    outcome = _two_accounts(tmp_path, alt, main, db).outcome(WORK, "run-1")

    assert (outcome.execution_state, outcome.account) == (rx.ENDED, "alt")


def _reserved_at(db: Db, at: float) -> None:
    """Move every Continue's reservation to ``at`` on the test's clock."""
    assert db._write(
        "UPDATE chat_successors SET reserved_at = ?",
        (datetime.fromtimestamp(at, UTC).isoformat(),),
    )


async def test_a_stopped_run_stays_stopped_when_its_chat_was_continued_before(tmp_path):
    """The stop parked whatever its Continue had started; that chat is not the Run's now."""
    db = _alt_db(tmp_path)
    alt, main = Host({KEY: _limited()}), Host({})
    executions = _two_accounts(tmp_path, alt, main, db)
    _continue(db)
    _reserved_at(db, T0 + 100)
    await executions.stop(WORK, "run-1", "card_stopped")
    main.workers["deck-chat"] = _continuation()

    assert executions.outcome(WORK, "run-1").safe


async def test_a_chat_continued_after_its_run_stopped_is_parked_by_the_next_run(tmp_path):
    """Review of #2863: a Continue made after the stop was never followed, so the stopped Run
    read safe and the next Run started beside it. Now it is followed, and parked."""
    db = _alt_db(tmp_path)
    alt, main = Host({KEY: _limited()}), Host({})
    executions = _two_accounts(tmp_path, alt, main, db)
    await executions.stop(WORK, "run-1", "card_stopped")
    _continue(db)
    _reserved_at(db, T0 + 121)
    main.workers["deck-chat"] = _continuation()

    assert executions.outcome(WORK, "run-1").execution_state == rx.RUNNING
    assert await executions.admit(WORK, "run-2", worker_key=KEY) is None
    assert main.parked == ["deck-chat"]


async def test_a_continuation_still_starting_blocks_a_new_run_rather_than_parking(tmp_path):
    """Review of #2863: a chat still starting has no worker yet, and parking the ended leg in
    its place would let it start beside the new Run. The new Run waits instead."""
    db = _alt_db(tmp_path)
    _continue(db, link=False)
    alt, main = Host({KEY: _limited()}), Host({})
    reserved = db.chat_successor(ALT_SESSION)[1].timestamp()
    executions = _two_accounts(tmp_path, alt, main, db, now=reserved + 5)

    blocking = await executions.admit(WORK, "run-2", worker_key=KEY)

    assert blocking is not None and blocking.reason_code == rx.CONTINUATION_STARTING
    assert alt.parked == [] and main.parked == []


def _chain(db: Db, legs: int) -> None:
    """Continue the Run's chat ``legs`` times over, onto legs no worker holds."""
    source = ALT_SESSION
    for index in range(legs):
        token = f"{index:032d}"
        assert db.reserve_chat_successor(token, source) == "new"
        target = f"claude_code:main:gone{index}"
        db.record_session_token(token, target)
        source = target


async def test_a_continuation_chain_past_the_cap_is_uncertain(tmp_path):
    """Review of #2863: past the walk's cap the work is somewhere unknown, never ended."""
    at_cap, past_cap = tmp_path / "eight", tmp_path / "nine"
    for path, legs in ((at_cap, rx.MAX_CONTINUATIONS), (past_cap, rx.MAX_CONTINUATIONS + 1)):
        path.mkdir()
        db = _alt_db(path)
        _chain(db, legs)
        outcome = _two_accounts(
            path, Host({KEY: _limited()}), Host({}), db
        ).outcome(WORK, "run-1")
        if legs == rx.MAX_CONTINUATIONS:
            assert (outcome.execution_state, outcome.reason_code) == (rx.ENDED, rx.USAGE_LIMIT)
        else:
            assert (outcome.execution_state, outcome.reason_code) == (
                rx.UNCERTAIN, "continuation_chain_too_long",
            )


async def test_an_unreadable_continuation_store_is_uncertain(tmp_path):
    """Review of #2863: a store error is not "nobody continued it"."""
    db = _alt_db(tmp_path)

    def broken(_source, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    db.chat_successor = broken
    outcome = _two_accounts(tmp_path, Host({KEY: _limited()}), Host({}), db).outcome(
        WORK, "run-1"
    )

    assert (outcome.execution_state, outcome.reason_code) == (
        rx.UNCERTAIN, "continuation_unreadable",
    )
    assert not outcome.safe


def test_a_continue_after_a_new_runs_route_froze_is_superseded(tmp_path):
    """The other order of that race: the route froze first, so the reservation refuses."""
    db = _alt_db(tmp_path)
    assert db.freeze_delivery_route(
        DeliveryRoute(
            "poller:run-2", "f", WORK, "run-2", "claude_code", "main", KEY,
            datetime.fromtimestamp(T0 + 60, UTC).isoformat(),
        )
    )

    assert db.reserve_chat_successor(
        "a" * 32, ALT_SESSION, newest_run=(WORK, "run-1")
    ) == "superseded"
    assert db.chat_successor(ALT_SESSION) is None
    assert db.reserve_chat_successor(
        "a" * 32, ALT_SESSION, newest_run=(WORK, "run-2")
    ) == "new"


async def test_a_new_run_stops_the_continuation_before_it_starts(tmp_path):
    """One worker per card: whichever way the race goes, the newer Run's admission ends the
    continued chat rather than running beside it."""
    db = _alt_db(tmp_path)
    _continue(db)
    alt = Host({KEY: _limited()})
    main = Host({"deck-chat": _continuation()})
    executions = _two_accounts(tmp_path, alt, main, db)

    target = ("claude_code", "main")
    assert await executions.admit(WORK, "run-2", worker_key=KEY, target=target) is None

    assert main.parked == ["deck-chat"]
    assert executions.outcome(WORK, "run-1").execution_state == rx.STOPPED
