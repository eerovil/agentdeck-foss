"""The Runtime's own answer to "is this Run's execution still running?" (#2159).

The Poller used to work this out for itself. It read worker snapshots, its own receipt files and
how quiet the GitHub issue had been, and it stopped a worker by rolling the card's lease and
waiting for the worker to run ``stop-ack``. So a dead or wedged worker had to run one more
model command to prove it had stopped, and the only fallback was a two-hour timeout.

The process lives here, so the facts about it live here too. The Poller still decides whether
a Work gets another Run. This module decides whether one concrete execution is running, has
ended, died, is wedged, or has been stopped. It is also what can stop one.

No new identity is minted. A Run's execution is found the way #2142 and #2146 already record
it:

* ``delivery_routes`` names the provider, account and worker key each Run was sent to;
* the worker's snapshot row carries the ``work_id``/``run_id`` of the newest delivery that
  actually took part (a receipt left at ``prepared_before_write`` does not count).

The table here holds only the *outcome*: the last state observed and whether a stop was asked
for. It is not a second Work/Run → worker map, and every read works the identity out again
from the two sources above.

Being unable to read something is never read as "stopped". A host that cannot answer, and a
worker that lost its process to a runtime restart, both answer ``uncertain``. Anything that
must be safe before it goes ahead — a new Run on the same Work, deleting the Work's worktree —
treats ``uncertain`` the same as ``running``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import SNAPSHOT_BOOTED_AT_KEY, IssueRef
from .sqlite_open import create_schema, use_wal
from .work import work_id_for

log = logging.getLogger(__name__)

EXECUTIONS_PATH = "/executions"
EXECUTION_OUTCOME_PATH = "/executions/outcome"
EXECUTION_STOP_PATH = "/executions/stop"

RUNNING = "running"
WAITING_HUMAN = "waiting_human"
ENDED = "ended"
DIED = "died"
ABANDONED = "abandoned"
STOPPED = "stopped"
UNCERTAIN = "uncertain"
# The Run has a route but no worker ever took part in it: it was refused before it started.
NOT_STARTED = "not_started"

EXECUTION_STATES = (
    RUNNING, WAITING_HUMAN, ENDED, DIED, ABANDONED, STOPPED, UNCERTAIN, NOT_STARTED,
)
# Nothing in one of these states can still write the Work's workspace. Everything else,
# ``uncertain`` included, is treated as if it could.
SAFE_STATES = frozenset({ENDED, DIED, STOPPED, NOT_STARTED})
# What ``stop`` answers.
STOP_STOPPED = "stopped"
STOP_ALREADY_STOPPED = "already_stopped"
STOP_STILL_STOPPING = "still_stopping"
STOP_UNCERTAIN = "uncertain"

# How long a worker must have done nothing before it counts as wedged. Two hours, the same bar
# the Poller's own watch used (#1400): a long single tool call is the false alarm to avoid, and
# no ordinary one takes that long.
ABANDONED_QUIET_S = 2 * 3600.0
# How long one stop waits for the provider before it answers ``still_stopping``. Claude's own
# terminate escalates to SIGKILL after five seconds; a Codex turn that does not
# settle inside this is left for the next ask rather than holding the Poller's wake.
STOP_WAIT_S = 30.0

REFUSED_PREFIX = "refused:"
# An ``ended`` reason: the turn ran and then stopped because the account's usage window ran out.
# The CLI calls that turn ``success`` (#2862), so the worker row says it separately.
USAGE_LIMIT = "usage_limit"
# How long a Continue that has not named its new chat yet still counts as starting. A start
# that failed leaves its reservation behind (#1620), and that must not hold a Run open forever.
CONTINUATION_START_S = 600.0
# The reason a Run reads while the chat a Continue made for it is still starting.
CONTINUATION_STARTING = "continuation_starting"
# The longest chain of Continues one observation follows.
MAX_CONTINUATIONS = 8
# Refusal of a delivery that names no Run of its own (a restart revival, a notice) to a
# worker whose Run the Runtime was asked to stop.
RUN_STOPPED = "run_stopped"

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_execution_outcomes (
    work_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    execution_state TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    last_activity_at REAL,
    terminal_result_at REAL,
    stop_requested_at REAL,
    stop_reason TEXT NOT NULL DEFAULT '',
    stopped_at REAL,
    observed_at REAL NOT NULL,
    PRIMARY KEY (work_id, run_id)
);
-- The outcomes that may still be executing, so asking which Works are current reads only
-- those rows and never the retained history (#2201). ``open_runs`` repeats this WHERE clause
-- word for word: SQLite uses a partial index only then.
CREATE INDEX IF NOT EXISTS run_execution_outcomes_open
    ON run_execution_outcomes (work_id, run_id)
    WHERE stopped_at IS NULL
    AND execution_state IN ('abandoned', 'running', 'uncertain', 'waiting_human');
"""

# Outcomes in which a worker may still be doing something, or the Runtime cannot say it is
# not: word for word the WHERE clause of the partial index above.
OPEN_OUTCOMES = (
    "o.stopped_at IS NULL"
    " AND o.execution_state IN ('abandoned', 'running', 'uncertain', 'waiting_human')"
)
# ...minus any Run a later Run of the same Work has replaced. Runs are ordered by when the
# Runtime first placed them (``delivery_routes.decided_at``, frozen once written), never by
# ``observed_at``, which every re-observation rewrites. Any route of another Run decided after
# this Run's first one means a later Run: a lease only moves forward, so an older Run is never
# placed again once a newer one was. A Run with no route cannot be ordered and stays current.
REPLACED_RUNS = (
    " AND NOT EXISTS (SELECT 1 FROM delivery_routes AS later"
    " WHERE later.work_id = o.work_id AND later.run_id <> o.run_id"
    " AND later.decided_at > (SELECT MIN(mine.decided_at) FROM delivery_routes AS mine"
    " WHERE mine.work_id = o.work_id AND mine.run_id = o.run_id))"
)


@dataclass(frozen=True)
class ExecutionOutcome:
    """One Run's execution as the Runtime last saw it. Provider-neutral."""

    work_id: str
    run_id: str
    execution_state: str
    reason_code: str = ""
    last_activity_at: float | None = None
    terminal_result_at: float | None = None
    provider: str = ""
    account: str = ""
    worker_key: str = ""
    session_id: str = ""
    observed_at: float = 0.0
    stop_requested_at: float | None = None
    stop_reason: str = ""
    stopped_at: float | None = None
    # Why the account this Run was routed to was already unable to run a turn when the route
    # was frozen, or "". The Poller reads it to tell a refused turn from a dead worker when
    # the provider itself gives no ``refused:`` reason (#1519, #2200).
    refused_at_delivery: str = ""
    # Background shell commands the Run's chat launched and has not collected yet, read only
    # when asked for and only for an ``ended`` turn: such a chat is waiting for one of them
    # to wake it, not finished (#2890). Never stored; empty for providers that track none.
    background_tasks: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        """Can nothing in this execution still write the Work's workspace?"""
        return self.execution_state in SAFE_STATES

    @property
    def refused(self) -> str:
        """Why the provider refused this Run's turn before it ran, or ``""``."""
        if self.reason_code.startswith(REFUSED_PREFIX):
            return self.reason_code[len(REFUSED_PREFIX):]
        return ""

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Stored:
    """The durable part of an outcome: what was last seen and whether a stop was asked for."""

    execution_state: str
    reason_code: str
    last_activity_at: float | None
    terminal_result_at: float | None
    stop_requested_at: float | None
    stop_reason: str
    stopped_at: float | None
    observed_at: float


class OutcomeStore:
    """``run_execution_outcomes`` in the deck's SQLite file, on its own connection.

    Same arrangement as the Work workspace claims: this runtime is the only writer, and a
    write never waits on ``Db``'s lock.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=30, isolation_level=None, check_same_thread=False
        )
        use_wal(self._conn)
        with self._lock:
            create_schema(self._conn, SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, work_id: str, run_id: str) -> Stored | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT execution_state, reason_code, last_activity_at, terminal_result_at,"
                " stop_requested_at, stop_reason, stopped_at, observed_at"
                " FROM run_execution_outcomes WHERE work_id = ? AND run_id = ?",
                (work_id, run_id),
            ).fetchone()
        return Stored(*row) if row is not None else None

    def runs(self, work_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM run_execution_outcomes WHERE work_id = ?"
                " ORDER BY observed_at DESC",
                (work_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def open_runs(self) -> list[tuple[str, str]]:
        """``(work_id, run_id)`` of every current Run whose last observation is still open."""
        with self._lock:
            routed = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'delivery_routes'"
            ).fetchone()
            rows = self._conn.execute(
                "SELECT o.work_id, o.run_id FROM run_execution_outcomes AS o"
                f" WHERE {OPEN_OUTCOMES}{REPLACED_RUNS if routed else ''}"
            ).fetchall()
        return [(str(work_id), str(run_id)) for work_id, run_id in rows]

    def record(self, outcome: ExecutionOutcome) -> None:
        """Keep the latest observation. The stop fields are only ever set, never cleared."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_execution_outcomes (work_id, run_id, execution_state,"
                " reason_code, last_activity_at, terminal_result_at, stop_requested_at,"
                " stop_reason, stopped_at, observed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(work_id, run_id) DO UPDATE SET"
                " execution_state = excluded.execution_state,"
                " reason_code = excluded.reason_code,"
                " last_activity_at = excluded.last_activity_at,"
                " terminal_result_at = COALESCE(excluded.terminal_result_at,"
                "   run_execution_outcomes.terminal_result_at),"
                " stop_requested_at = COALESCE(run_execution_outcomes.stop_requested_at,"
                "   excluded.stop_requested_at),"
                " stop_reason = CASE WHEN run_execution_outcomes.stop_reason = ''"
                "   THEN excluded.stop_reason ELSE run_execution_outcomes.stop_reason END,"
                " stopped_at = COALESCE(run_execution_outcomes.stopped_at, excluded.stopped_at),"
                " observed_at = excluded.observed_at",
                (
                    outcome.work_id,
                    outcome.run_id,
                    outcome.execution_state,
                    outcome.reason_code,
                    outcome.last_activity_at,
                    outcome.terminal_result_at,
                    outcome.stop_requested_at,
                    outcome.stop_reason,
                    outcome.stopped_at,
                    outcome.observed_at or time.time(),
                ),
            )


def _number(value: object) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 0.0


def _attributed(row: dict, work_id: str, run_id: str, *, latest_run: bool) -> bool:
    """Is this worker row this Run's execution?

    The row names the Run of the newest delivery that took part. A row that names none —
    its receipts were pruned after a day, or it predates #2142 — is attributed to the Run the
    Work was most recently sent to, and to no older one. A row naming another Work under the
    same key is never this Work's (the key is only a compatibility name, #2185).
    """
    named_work = row.get("work_id")
    if isinstance(named_work, str) and named_work and named_work != work_id:
        return False
    named = row.get("run_id")
    if isinstance(named, str) and named:
        return named == run_id
    return latest_run


def classify(
    row: dict | None,
    *,
    booted_at: float,
    now: float,
    abandoned_after_s: float = ABANDONED_QUIET_S,
) -> tuple[str, str, float | None, float | None]:
    """What one worker row attributed to a Run says. Returns state, reason, activity, end.

    Pure over the snapshot row, so the whole rule is testable without a runtime. Ordered so a
    turn that produced a result is never read as a death, and a process lost to a runtime
    restart is never read as one either.
    """
    if row is None:
        # The runtime holds no worker for this key any more: it was released, which
        # terminates the process. Nothing is left that could write.
        return ENDED, "worker_released", None, None
    delivered = _number(row.get("last_delivery_at"))
    activity = _number(row.get("last_activity_at")) or None
    result_at = _number(row.get("last_result_at"))
    if row.get("pending_interaction"):
        return WAITING_HUMAN, "pending_interaction", activity, None
    quiet_since = max(activity or 0.0, delivered)
    wedged = quiet_since > 0 and now - quiet_since >= abandoned_after_s
    if row.get("turn_active") or row.get("effective_activity"):
        # An open turn that has done nothing for hours is stuck, not slow. For Codex the open
        # turn never closes by itself (#1400), so waiting for it to would wait forever.
        if wedged:
            return ABANDONED, "no_activity", activity, None
        return RUNNING, "turn_active", activity, None
    if result_at > 0 and result_at >= delivered:
        refusal = row.get("last_result_refusal")
        subtype = str(row.get("last_result_subtype") or "")
        if isinstance(refusal, str) and refusal:
            return ENDED, REFUSED_PREFIX + refusal, activity, result_at
        if row.get("last_result_limit"):
            return ENDED, USAGE_LIMIT, activity, result_at
        return ENDED, subtype or "result", activity, result_at
    if row.get("live") is False:
        # The record is here, the process is not, and it never produced a result. A kill does
        # that — and so does a runtime restart, whose fresh process then wakes the chat again
        # (#1659). Only the second is not a death, and it cannot be told apart from here.
        if booted_at > 0 and delivered > 0 and booted_at > delivered:
            return UNCERTAIN, "runtime_restarted", activity, None
        return DIED, str(row.get("exit_reason") or "process_gone"), activity, None
    if wedged:
        return ABANDONED, "no_activity", activity, None
    if quiet_since <= 0:
        # No delivery stamp and no activity clock, and nothing running: no turn was ever seen
        # on this row, so nothing on it can be writing. Waiting on it would block the Work's
        # next Run forever on a record that will never move.
        return ENDED, "no_turn_observed", None, None
    # Delivered, nothing running yet, no result: the turn is about to start.
    return RUNNING, "turn_pending", activity, None


def _ended_by_stop(state: str, reason: str) -> bool:
    """Would a worker this Runtime stopped look like this afterwards?

    Ended or dead, or — after a Runtime restart — a record with no process. The stop itself
    ended whatever was running, so the restart's uncertainty no longer applies to it. A turn
    running again (a later revival) is not covered, and reads as running.
    """
    return state in {ENDED, DIED} or (state == UNCERTAIN and reason == "runtime_restarted")


@dataclass(frozen=True)
class _Route:
    provider: str
    account: str
    worker_key: str
    refused_at_delivery: str = ""


Park = Callable[[str, str, str], Awaitable[Any]]


class RunExecutions:
    """Run execution outcome and stop, keyed by the canonical ``work_id``/``run_id``.

    ``hosts`` answers every keyed worker host by ``(provider, account)``. ``db`` is the
    deck's store holding ``delivery_routes``. ``park`` stops one worker key's execution
    without forgetting its session, serialized with admission by the caller.
    """

    def __init__(
        self,
        store: OutcomeStore,
        *,
        hosts: Callable[[], dict[tuple[str, str], Any]],
        db: Any,
        park: Park | None = None,
        clock: Callable[[], float] = time.time,
        abandoned_after_s: float = ABANDONED_QUIET_S,
    ) -> None:
        self.store = store
        self.hosts = hosts
        self.db = db
        self.park = park
        self.clock = clock
        self.abandoned_after_s = abandoned_after_s
        self.on_change: Callable[[], None] = lambda: None

    # --- identity -------------------------------------------------------------------------

    def _routes(self, work_id: str) -> list[Any]:
        read = getattr(self.db, "latest_delivery_routes_by_run", None)
        return list(read(work_id)) if read is not None else []

    def _holder(self, work_id: str, run_id: str) -> _Route | None:
        """Where this Run went: the route frozen before its provider write (#2214)."""
        for route in self._routes(work_id):
            if route.run_id == run_id:
                return _Route(
                    route.provider,
                    route.account,
                    route.worker_key,
                    getattr(route, "refused_at_delivery", "") or "",
                )
        return None

    # --- observation ----------------------------------------------------------------------

    def outcome(
        self,
        work_id: str,
        run_id: str,
        *,
        own_token: str | None = None,
        background: bool = False,
    ) -> ExecutionOutcome:
        """This Run's execution now, recorded so a restart remembers it.

        ``own_token`` is the asking delivery's session token: a Continue it reserved itself
        is not read as this Run's chat carrying on (#2884). ``background`` also reads an
        ended turn's uncollected background commands, which costs a transcript read (#2890).
        """
        now = self.clock()
        stored = self.store.get(work_id, run_id)
        try:
            outcome = self._observe(work_id, run_id, stored, now, own_token=own_token)
        except Exception as exc:  # noqa: BLE001 -- unreadable is uncertain, never stopped
            log.warning("run execution %s/%s could not be read: %s", work_id, run_id, exc)
            outcome = self._from_store(
                work_id, run_id, stored, now, fallback=(UNCERTAIN, "unreadable")
            )
        self.store.record(outcome)
        if stored is None or (
            stored.execution_state,
            stored.reason_code,
            stored.terminal_result_at,
            stored.stop_requested_at,
            stored.stopped_at,
        ) != (
            outcome.execution_state,
            outcome.reason_code,
            outcome.terminal_result_at,
            outcome.stop_requested_at,
            outcome.stopped_at,
        ):
            # A Run outcome the Work overview reads moved (#2865). Often from a thread:
            # the feed's bump is thread-safe.
            self.on_change()
        if background and outcome.execution_state == ENDED:
            outcome = replace(outcome, background_tasks=self._background_tasks(outcome))
        return outcome

    def _background_tasks(self, outcome: ExecutionOutcome) -> tuple[str, ...]:
        """The ended turn's uncollected background commands, or ``()`` when none can be read.

        Only while the chat's process is alive: a command can wake only a chat still running.
        """
        if not outcome.worker_key:
            return ()
        try:
            host = self.hosts().get((outcome.provider, outcome.account))
            read = getattr(host, "outstanding_background_tasks", None)
            if read is None:
                return ()
            row = host.snapshot().get("workers", {}).get(outcome.worker_key)
            if not isinstance(row, dict) or row.get("live") is not True:
                return ()
            return tuple(sorted(str(task) for task in read(outcome.worker_key)))
        except Exception:  # noqa: BLE001 -- unreadable proves no wait, so nothing is held
            log.exception(
                "background tasks of %s/%s could not be read", outcome.work_id, outcome.run_id
            )
            return ()

    def refresh_open(self) -> int:
        """Observe again every current Run still recorded as open; how many changed state.

        Nothing else asks once a Run's own report moved its card (#2586): a worker reports
        from inside its turn, so the observation made then reads the turn as running, and
        without this the Work would read Working long after the turn ended.
        """
        changed = 0
        for work_id, run_id in self.store.open_runs():
            try:
                before = self.store.get(work_id, run_id)
                after = self.outcome(work_id, run_id)
            except Exception:  # noqa: BLE001 -- one unreadable Run must not stop the rest
                log.exception("run execution %s/%s could not be refreshed", work_id, run_id)
                continue
            if before is None or before.execution_state != after.execution_state:
                changed += 1
                log.info(
                    "run execution %s/%s re-observed: %s -> %s", work_id, run_id,
                    before.execution_state if before else "none", after.execution_state,
                )
        return changed

    def outcomes(
        self, work_id: str, *, own_token: str | None = None
    ) -> list[ExecutionOutcome]:
        """Every Run of this Work the Runtime knows, newest route first.

        Every Run that reached a provider froze a route first, so routes and recorded
        outcomes are the whole set; a worker host that cannot be read makes its own Run
        uncertain rather than hiding one (#2214).
        """
        seen: list[str] = []
        for route in self._routes(work_id):
            if route.run_id not in seen:
                seen.append(route.run_id)
        for run_id in self.store.runs(work_id):
            if run_id not in seen:
                seen.append(run_id)
        return [self.outcome(work_id, run_id, own_token=own_token) for run_id in seen]

    def _from_store(
        self,
        work_id: str,
        run_id: str,
        stored: Stored | None,
        now: float,
        *,
        fallback: tuple[str, str],
        route: _Route | None = None,
    ) -> ExecutionOutcome:
        """What is known when no worker row speaks for the Run."""
        identity = asdict(route) if route is not None else {}
        if stored is not None and stored.execution_state in SAFE_STATES:
            # A settled answer stays settled: an ended Run does not come back to life
            # because its worker was later released or its route pruned.
            return ExecutionOutcome(
                work_id=work_id,
                run_id=run_id,
                execution_state=stored.execution_state,
                reason_code=stored.reason_code,
                last_activity_at=stored.last_activity_at,
                terminal_result_at=stored.terminal_result_at,
                observed_at=now,
                stop_requested_at=stored.stop_requested_at,
                stop_reason=stored.stop_reason,
                stopped_at=stored.stopped_at,
                **identity,
            )
        state, reason = fallback
        return ExecutionOutcome(
            work_id=work_id,
            run_id=run_id,
            execution_state=state,
            reason_code=reason,
            last_activity_at=stored.last_activity_at if stored else None,
            observed_at=now,
            stop_requested_at=stored.stop_requested_at if stored else None,
            stop_reason=stored.stop_reason if stored else "",
            stopped_at=stored.stopped_at if stored else None,
            **identity,
        )

    def _observe(
        self,
        work_id: str,
        run_id: str,
        stored: Stored | None,
        now: float,
        *,
        own_token: str | None = None,
    ) -> ExecutionOutcome:
        hosts = self.hosts()
        route = self._holder(work_id, run_id)
        if route is None:
            # No route and no worker: nothing was ever sent, or it was refused before any
            # provider write (a refused route is released). Nothing can be running.
            return self._from_store(
                work_id, run_id, stored, now, fallback=(NOT_STARTED, "no_execution")
            )
        host = hosts.get((route.provider, route.account))
        if host is None:
            return self._from_store(
                work_id, run_id, stored, now,
                fallback=(UNCERTAIN, "host_unavailable"), route=route,
            )
        snapshot = host.snapshot()
        booted_at = _number(snapshot.get(SNAPSHOT_BOOTED_AT_KEY))
        row = snapshot.get("workers", {}).get(route.worker_key)
        routes = self._routes(work_id)
        latest_run = not routes or routes[0].run_id == run_id
        if isinstance(row, dict) and not _attributed(row, work_id, run_id, latest_run=latest_run):
            # The worker has since taken a later Run's delivery, so this Run's own turn is
            # over — only ever by being replaced, never by still running.
            state = STOPPED if stored and stored.stop_requested_at else ENDED
            return self._settle(
                work_id, run_id, stored, now, route, row,
                (state, "superseded_by_later_run", None, None),
            )
        facts = classify(
            row if isinstance(row, dict) else None,
            booted_at=booted_at,
            now=now,
            abandoned_after_s=self.abandoned_after_s,
        )
        if isinstance(row, dict) and facts[0] in {ENDED, DIED}:
            # It may have been carried on in another chat. A stopped Run's stop parked the
            # chats continued before it; one continued after it is still that Run's.
            moved = self._continuation(
                work_id, run_id, route, row, hosts, now, latest_run=latest_run,
                after=stored.stop_requested_at if stored else None,
                own_token=own_token,
            )
            if moved is not None:
                route, row, facts = moved
        if row is None and stored is not None and stored.execution_state in SAFE_STATES:
            return self._from_store(
                work_id, run_id, stored, now, fallback=facts[:2], route=route
            )
        return self._settle(work_id, run_id, stored, now, route, row, facts)

    def _continuation(
        self,
        work_id: str,
        run_id: str,
        route: _Route,
        row: dict,
        hosts: dict[tuple[str, str], Any],
        now: float,
        *,
        latest_run: bool,
        after: float | None = None,
        own_token: str | None = None,
    ) -> tuple[_Route, dict | None, tuple[str, str, float | None, float | None]] | None:
        """Where a deck Continue carried this Run's chat, and what it is doing there (#2862).

        A person can move a Run's chat to another account once its turn ends — on a usage
        limit, typically. The new chat picks the same work up under the same lease, so the
        Run is still being worked on. Reading the old leg's ending instead is what let the
        Poller start a second worker beside it. Follows the chain of Continues to the newest
        leg whose worker serves this Run, and answers that leg's identity and facts. None
        when nobody continued the chat, or no continued leg holds a worker. A leg whose
        worker names another Run is not this Run's, and ends the walk; so is a leg naming no
        Run once a later Run has taken the Work (``latest_run`` is False). A Continue made
        at or before ``after`` (the Run's stop) is not followed: the stop parked it. Nor is
        the reservation ``own_token`` made: that is the asking delivery's own leg (#2884).

        What cannot be read is uncertain, never "nobody continued it": a store error, or a
        chain longer than ``MAX_CONTINUATIONS``.
        """
        read = getattr(self.db, "chat_successor", None)
        session_id = str(row.get("run_session_id") or row.get("session_id") or "")
        if read is None or not session_id:
            return None
        source = f"{route.provider}:{route.account}:{session_id}"
        found: tuple[_Route, dict | None, tuple[str, str, float | None, float | None]] | None
        found = None
        for hop in range(MAX_CONTINUATIONS + 1):
            try:
                successor = read(source, excluding=own_token)
            except Exception:  # noqa: BLE001 -- unreadable is not "nobody continued it"
                log.exception("continuations of %s could not be read", source)
                return route, row, (UNCERTAIN, "continuation_unreadable", None, None)
            if successor is None:
                break
            target, reserved_at = successor
            if hop == 0 and after is not None and reserved_at.timestamp() <= after:
                break
            if hop == MAX_CONTINUATIONS:
                # Past the cap a leg is still continued: where the work is now is unknown.
                return route, row, (UNCERTAIN, "continuation_chain_too_long", None, None)
            if target is None:
                if now - reserved_at.timestamp() >= CONTINUATION_START_S:
                    break
                # The new chat is still starting: about to work, not finished.
                at_route, at_row = (found[0], found[1]) if found else (route, row)
                return at_route, at_row, (RUNNING, CONTINUATION_STARTING, None, None)
            provider, _, rest = target.partition(":")
            account, _, leg_session = rest.partition(":")
            host = hosts.get((provider, account))
            if host is None:
                return (
                    _Route(provider, account, ""),
                    None,
                    (UNCERTAIN, "host_unavailable", None, None),
                )
            snapshot = host.snapshot()
            source = target
            match = next(
                (
                    (key, leg)
                    for key, leg in snapshot.get("workers", {}).items()
                    if isinstance(leg, dict) and leg.get("session_id") == leg_session
                ),
                None,
            )
            if match is None:
                continue
            key, leg = match
            if not _attributed(leg, work_id, run_id, latest_run=latest_run):
                break
            facts = classify(
                leg,
                booted_at=_number(snapshot.get(SNAPSHOT_BOOTED_AT_KEY)),
                now=now,
                abandoned_after_s=self.abandoned_after_s,
            )
            found = (_Route(provider, account, key), leg, facts)
        return found

    def _settle(
        self,
        work_id: str,
        run_id: str,
        stored: Stored | None,
        now: float,
        route: _Route,
        row: object,
        facts: tuple[str, str, float | None, float | None],
    ) -> ExecutionOutcome:
        state, reason, activity, ended_at = facts
        if stored is not None and stored.stopped_at and _ended_by_stop(state, reason):
            # This runtime stopped it; that is the story, not the exit it caused.
            state, reason = STOPPED, stored.stop_reason or "stop_requested"
        session_id = ""
        if isinstance(row, dict):
            session_id = str(row.get("run_session_id") or row.get("session_id") or "")
        if ended_at is None and state in SAFE_STATES:
            ended_at = (stored.terminal_result_at if stored else None) or now
        return ExecutionOutcome(
            work_id=work_id,
            run_id=run_id,
            execution_state=state,
            reason_code=reason,
            last_activity_at=activity,
            terminal_result_at=ended_at,
            provider=route.provider,
            account=route.account,
            worker_key=route.worker_key,
            session_id=session_id,
            refused_at_delivery=route.refused_at_delivery,
            observed_at=now,
            stop_requested_at=stored.stop_requested_at if stored else None,
            stop_reason=stored.stop_reason if stored else "",
            stopped_at=stored.stopped_at if stored else None,
        )

    # --- the stop fence for undirected deliveries -------------------------------------------
    # With no execution state at all, the runtime uses ``serves_a_run`` instead: any worker
    # serving a Work/Run is fenced, because nothing can say whether that Run was stopped.


    def stop_requested_for(self, row: object) -> bool:
        """Has a stop been asked for the Run this worker row is serving?

        A delivery that names no Run of its own — a restart revival, a job-finished notice —
        resumes whatever Run the worker last served. Once that Run's stop was requested it
        must not be resumed, or the stop the Runtime proved is quietly undone (#2159). The
        stop records its request *before* it parks, and both the park and these deliveries
        run under the admission lock, so a delivery checked here either lands before the
        park (which then ends it) or is refused.
        """
        if not isinstance(row, dict):
            return False
        work_id, run_id = row.get("work_id"), row.get("run_id")
        if not (isinstance(work_id, str) and work_id and isinstance(run_id, str) and run_id):
            return False
        try:
            stored = self.store.get(work_id, run_id)
        except Exception:  # noqa: BLE001 -- unknown is not "nobody stopped it"
            log.exception("stop fence: %s/%s could not be read", work_id, run_id)
            return True
        return bool(stored and stored.stop_requested_at)

    # --- admission ------------------------------------------------------------------------

    def _older_runs(self, work_id: str, run_id: str) -> set[str]:
        """Runs of this Work routed before this one; every known Run when it has no route yet.

        A Run with no route yet is being delivered for the first time, and the engine only
        delivers under its current generation — so every other Run it has is older.
        """
        order = [route.run_id for route in reversed(self._routes(work_id))]  # oldest first
        if run_id not in order:
            return (set(order) | set(self.store.runs(work_id))) - {run_id}
        return set(order[: order.index(run_id)])

    async def admit(
        self,
        work_id: str,
        run_id: str,
        *,
        worker_key: str = "",
        target: tuple[str, str] | None = None,
        park: Park | None = None,
        own_token: str | None = None,
    ) -> ExecutionOutcome | None:
        """Make room for a new Run of this Work, or say which Run is in its way.

        A new Run means the Poller has superseded the older ones: their generations are
        already fenced. So the Runtime stops each older Run that may still be writing the
        shared workspace before the new one is admitted — supersede, stop, then admit
        (#2159). A Run newer than this one is never stopped by it; it simply blocks it.

        One older Run is not in the way: a live worker under the same key on exactly the
        ``target`` account the new Run is about to be written to. That worker takes the new
        Run itself (a steer, a queue, or #2146's mid-turn pin), so there is no second
        execution in the workspace. Without a known target nothing is exempt: this must be
        called once the target is chosen, inside the admission lock, immediately before the
        route is frozen and the provider written — ``park`` is then the lock-free park.
        A wedged or unreadable Run is still stopped or refused. Returns ``None`` when the
        new Run may go ahead. ``own_token`` is this delivery's session token: the successor it
        reserved to continue a chat is not an older Run's Continue still starting (#2884).
        """
        outcomes = await asyncio.to_thread(self.outcomes, work_id, own_token=own_token)
        older = await asyncio.to_thread(self._older_runs, work_id, run_id)
        for outcome in outcomes:
            if outcome.run_id == run_id or outcome.safe:
                continue
            if (
                worker_key
                and target is not None
                and outcome.worker_key == worker_key
                and outcome.execution_state in {RUNNING, WAITING_HUMAN}
                and target == (outcome.provider, outcome.account)
            ):
                continue
            if outcome.run_id not in older or outcome.reason_code == CONTINUATION_STARTING:
                # A chat still starting has no worker to stop yet; parking the ended leg
                # before it would leave it to start beside the new Run (#2862).
                return outcome
            result, after = await self.stop(
                work_id, outcome.run_id, f"superseded_by:{run_id}", park=park
            )
            if result not in {STOP_STOPPED, STOP_ALREADY_STOPPED}:
                return after
        return None

    def conflicting(self, work_id: str, run_id: str) -> ExecutionOutcome | None:
        """Another Run of this Work that may still be writing its workspace, if any.

        A Work's Runs share one workspace (#2149), so a new Run may not start while an older
        one is running, wedged, or cannot be read. ``None`` when every other Run is safe.
        """
        for outcome in self.outcomes(work_id):
            if outcome.run_id != run_id and not outcome.safe:
                return outcome
        return None

    def work_active(self, work_id: str) -> bool | None:
        """Can any Run of this Work still write its workspace? None when that is unreadable.

        A Work with no Run anywhere -- no route and no recorded outcome -- never reached a
        provider, so it is not running (#2197). A route is frozen before provider I/O may
        start, so any Run that got that far is asked about instead (#2214).
        """
        outcomes = self.outcomes(work_id)
        if any(not o.safe and o.execution_state != UNCERTAIN for o in outcomes):
            return True
        if not outcomes:
            return False
        if any(o.execution_state == UNCERTAIN for o in outcomes):
            # Unknown is not stopped: the caller waits exactly as it would for a running one.
            return None
        return False

    # --- stop -----------------------------------------------------------------------------

    async def stop(
        self, work_id: str, run_id: str, reason: str, *, park: Park | None = None
    ) -> tuple[str, ExecutionOutcome]:
        """Stop this Run's execution. Answers ``stopped``, ``already_stopped``,
        ``still_stopping`` or ``uncertain`` with the outcome after the attempt.

        Needs no cooperation from the model: the provider process or turn is ended from here.
        Safe to repeat — a second call on a stopped Run answers ``already_stopped``.
        """
        now = self.clock()
        stored = self.store.get(work_id, run_id)
        before = await asyncio.to_thread(self.outcome, work_id, run_id)
        if before.safe:
            if stored is None or not stored.stop_requested_at:
                self.store.record(
                    replace(before, stop_requested_at=now, stop_reason=reason)
                )
            return STOP_ALREADY_STOPPED, before
        requested = replace(
            before,
            stop_requested_at=(stored.stop_requested_at if stored else None) or now,
            stop_reason=(stored.stop_reason if stored else "") or reason,
        )
        self.store.record(requested)
        if before.execution_state == UNCERTAIN and not before.worker_key:
            return STOP_UNCERTAIN, requested
        park = park or self.park
        if park is None or not before.worker_key:
            return STOP_UNCERTAIN, requested
        parked = False
        try:
            answer = await asyncio.wait_for(
                park(before.provider, before.account, before.worker_key),
                timeout=STOP_WAIT_S,
            )
            parked = bool(getattr(answer, "accepted", False))
        except TimeoutError:
            log.warning("stopping %s/%s is still in progress", work_id, run_id)
        except Exception:  # noqa: BLE001 -- the next ask tells what happened
            log.exception("stopping %s/%s failed", work_id, run_id)
        after = await asyncio.to_thread(self.outcome, work_id, run_id)
        if after.execution_state == STOPPED or (
            parked and _ended_by_stop(after.execution_state, after.reason_code)
        ):
            stopped = replace(
                after,
                execution_state=STOPPED,
                reason_code=requested.stop_reason or "stop_requested",
                stopped_at=self.clock(),
                terminal_result_at=after.terminal_result_at or self.clock(),
            )
            self.store.record(stopped)
            return STOP_STOPPED, stopped
        if after.safe:
            return STOP_ALREADY_STOPPED, after
        if after.execution_state == UNCERTAIN:
            return STOP_UNCERTAIN, after
        return STOP_STILL_STOPPING, after


def canonical_work_id(work_id: str) -> str:
    """``work_id`` when it is spelled exactly as the engine saves it, else ``ValueError``.

    A misspelled Work has no routes, and a Run with no route reads as ``not_started`` — which
    is safe, so a stop would answer ``already_stopped`` for a worker it never looked at. Every
    entry point therefore refuses anything but the canonical spelling.
    """
    kind, _, source = work_id.partition(":")
    ref = IssueRef.parse(source) if kind == "github_issue" else None
    if ref is None or work_id_for(ref) != work_id:
        raise ValueError(f"{work_id!r} is not a canonical Work id")
    return work_id


def serves_a_run(row: object) -> bool:
    """The stop fence when Run execution state is unavailable (#2159).

    A worker serving a Work/Run may have had its Run stopped, and nothing can say it was not,
    so a delivery that would resume it is refused. A worker serving no Run — an ordinary
    deck chat — is not a Work's, and is left alone.
    """
    if not isinstance(row, dict):
        return False
    work_id, run_id = row.get("work_id"), row.get("run_id")
    return bool(isinstance(work_id, str) and work_id and isinstance(run_id, str) and run_id)


class StopExecutionRequest(BaseModel):
    """``POST /executions/stop``: stop one Run's concrete execution."""

    model_config = ConfigDict(extra="forbid")

    work_id: str = Field(min_length=1, max_length=512)
    run_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="stop_requested", min_length=1, max_length=256)

    @model_validator(mode="after")
    def _canonical(self) -> StopExecutionRequest:
        canonical_work_id(self.work_id)
        if self.run_id != self.run_id.strip():
            raise ValueError("run_id must be sent exactly as saved")
        return self


@dataclass(frozen=True)
class StoredOutcome:
    """One row of ``run_execution_outcomes``: the Runtime's last recorded observation."""

    run_id: str
    execution_state: str
    reason_code: str = ""
    last_activity_at: float | None = None
    terminal_result_at: float | None = None
    stop_requested_at: float | None = None
    stop_reason: str = ""
    stopped_at: float | None = None
    observed_at: float = 0.0


_OUTCOME_COLUMNS = (
    "run_id, execution_state, reason_code, last_activity_at, terminal_result_at,"
    " stop_requested_at, stop_reason, stopped_at, observed_at"
)


def read_outcomes(db_path: Path | str, work_id: str) -> list[StoredOutcome] | None:
    """This Work's recorded Run outcomes, read-only, from the web process.

    Read straight from the table rather than through the Runtime's ``/executions`` route,
    because that route records a fresh observation on every read and this must not write.
    ``[]`` when nothing was ever recorded; None when the store could not be read, which is
    unknown, never "not running".
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute(
            f"SELECT {_OUTCOME_COLUMNS} FROM run_execution_outcomes WHERE work_id = ?"
            " ORDER BY observed_at DESC",
            (work_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return [] if "no such table" in str(exc) else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [
        StoredOutcome(
            run_id=str(row[0]),
            execution_state=str(row[1]),
            reason_code=str(row[2] or ""),
            last_activity_at=row[3],
            terminal_result_at=row[4],
            stop_requested_at=row[5],
            stop_reason=str(row[6] or ""),
            stopped_at=row[7],
            observed_at=float(row[8] or 0.0),
        )
        for row in rows
    ]
