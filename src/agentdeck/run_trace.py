"""The Run trace: where one Work's execution has got to, and why (#2141).

A **Run** is one execution epoch of a Work, and its id is the Poller generation (#2142). This
module is a read model over facts other parts already keep. It stores nothing, writes nothing
and decides nothing: the Poller still chooses the next Run and the Runtime still owns the
execution. It reads, strongest first:

1. the Poller card's durable record: its current lease, its outstanding dispatch, a refusal the
   host gave that exact dispatch, and whether a spawn that never landed parked the card;
2. the Runtime's frozen delivery route for the Run (#2146);
3. the Runtime's last recorded execution outcome for the Run (#2159);
4. ``session_run_bindings``: which exact session legs took part in the Run (#2142). A delivery
   left at ``prepared_before_write`` never binds, so it never shows as an attached session;
5. explicit verification jobs naming the Work and Run (#2148);
6. the card's own column, blockers and pull request.

The display state (``preparing``, ``waiting``, ``running`` ...) is presentation. It is
derived on every read and never persisted, and an explicit fact always beats recency.
``next_expected`` names what the deck is waiting for only where a fact says so; otherwise it
stays empty. A Run with none of the first three facts is old history from before explicit
correlation, and is labelled sparse rather than filled in by inference. A current Run the
Poller still holds that started before the Runtime recorded any Run at all is ``historical``:
its execution details were never going to exist, so it is not a live warning (#2201).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .run_execution import ENDED, OPEN_OUTCOMES, REPLACED_RUNS, USAGE_LIMIT
from .work import WorkRef
from .work_workspace import RunVerification, run_verifications

# Display states. Presentation only: nothing schedules on them.
PREPARING = "preparing"
WAITING = "waiting"
RUNNING = "running"
NEEDS_ATTENTION = "needs_attention"
FINISHED = "finished"
FAILED = "failed"
UNCERTAIN = "uncertain"
SUPERSEDED = "superseded"
# A Run from before the Runtime recorded execution facts: its outcome is unknown, not wrong.
HISTORICAL = "historical"

# What the deck expects next.
NEXT_RUNTIME_ACCEPTANCE = "Runtime acceptance"
NEXT_CAPACITY = "worker capacity"
NEXT_SESSION = "session attachment"
NEXT_FIRST_ACTIVITY = "first worker activity"
NEXT_ACTIVITY = "worker activity"
NEXT_STOP = "worker stop"
NEXT_VERIFICATION = "verification result"
NEXT_REVIEW = "review decision"
NEXT_HUMAN = "human decision"
# The reason of a Run whose worker stopped on a question, an approval or a blocked command.
PERSON_WAIT = "the worker is waiting for a person"
NEXT_RETRY = "automatic retry"
NEXT_BLOCKERS = "blockers to close"

# Host refusals (``poller_adapter.worker.CLEAN_REFUSALS``) that mean "no room right now".
CAPACITY_REFUSALS = frozenset({
    "at_capacity",
    "over_budget",
    "session_over_budget",
    "no_candidate_accounts",
    "all_accounts_hard_ineligible",
})
# Host refusals that clear once some other execution stops.
_STOP_REFUSALS = frozenset({"active_turn", "prior_run_executing"})


# --- facts ----------------------------------------------------------------------------------


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
    """This Work's recorded Run outcomes, read-only.

    Read straight from the table rather than through the Runtime's ``/executions`` route,
    because that route records a fresh observation on every read and this view must not
    write. ``[]`` when nothing was ever recorded; None when the store could not be read,
    which the trace shows as unknown, never as "not running".
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


def read_evidence_since(db_path: Path | str) -> datetime | None:
    """When the Runtime first recorded anything about any Run, read-only (#2201).

    The earliest delivery route, session binding or execution outcome in the deck's store. A
    Run that started before this could never have had one, so its missing facts are history
    rather than an incident. None when nothing was ever recorded or the store cannot be read:
    then no Run is excused, and a missing fact stays a warning.
    """
    path = Path(db_path).expanduser()  # the configured path may start with ~
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    found: list[datetime] = []
    try:
        for table, column in (
            ("delivery_routes", "decided_at"),
            ("session_run_bindings", "bound_at"),
            ("run_execution_outcomes", "observed_at"),
        ):
            try:
                # The deck writes these as UTC ISO text (or epoch seconds), so the column's
                # own MIN is the earliest; one row back, however long the history.
                (value,) = conn.execute(f"SELECT MIN({column}) FROM {table}").fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    continue
                return None
            stamp = _when(value)
            if stamp is not None:
                found.append(stamp)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return min(found, default=None)


def read_current_executions(db_path: Path | str) -> frozenset[str] | None:
    """Works with a Run the Runtime last saw as possibly still executing (#2201).

    Read-only, one query for the whole deck that reads only the open outcomes (a partial
    index) and returns one row per current Work. A Run a later Run replaced does not count,
    however recently it was re-observed. None when unreadable.
    """
    path = Path(db_path).expanduser()  # the configured path may start with ~
    if not path.exists():
        return frozenset()
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        tables = {
            str(name)
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "run_execution_outcomes" not in tables:
            return frozenset()
        # Without routes no Run can be ordered, so every open outcome counts.
        replaced = REPLACED_RUNS if "delivery_routes" in tables else ""
        rows = conn.execute(
            "SELECT DISTINCT o.work_id FROM run_execution_outcomes AS o"
            f" WHERE {OPEN_OUTCOMES}{replaced}"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return frozenset(str(work_id) for (work_id,) in rows)


def read_deferral(card: Any) -> str:
    """The host's positive pre-write refusal of the card's outstanding dispatch, or ``""``.

    The same receipt the engine reads (``dispatch_deferred``), keyed by the exact dispatch id,
    so a refusal of an earlier dispatch can never be read as this one's.
    """
    dispatch = getattr(card, "dispatch", None)
    state_path = getattr(card, "state_path", "")
    key = getattr(card, "state_key", "")
    if dispatch is None or not state_path or "#" not in key:
        return ""
    from .poller_engine.github import read_receipt, receipt_path
    from .poller_engine.state import EnginePaths

    repo, _, number = key.partition("#")
    if not number.isdigit():
        return ""
    directory = EnginePaths.from_base(state_path).dispatch_defer_dir
    body = read_receipt(receipt_path(directory, repo, int(number), dispatch.id))
    return str((body or {}).get("reason") or "")


@dataclass(frozen=True)
class RunTraceFacts:
    """Everything the trace reads for one Work, gathered before anything is derived."""

    work: WorkRef
    card: Any = None  # poller_ops.BoardCard, or None when no instance tracks this Work
    board_readable: bool = True
    deferral: str = ""
    routes: tuple[Any, ...] = ()  # db.DeliveryRoute
    routes_readable: bool = True
    outcomes: tuple[StoredOutcome, ...] = ()
    outcomes_readable: bool = True
    bindings: tuple[Any, ...] = ()  # db.SessionRunBinding
    sessions: tuple[tuple[str, str], ...] = ()  # (session key, chat id)
    jobs: tuple[dict, ...] = ()
    # When the Runtime began recording Run facts (``read_evidence_since``); None: unknown.
    evidence_since: datetime | None = None

    @property
    def known(self) -> bool:
        return bool(
            self.card is not None
            or self.routes
            or self.outcomes
            or self.bindings
            or any(job.get("work_id") == self.work.work_id for job in self.jobs)
        )


def gather_run_trace(
    state: Any,
    work: WorkRef,
    *,
    board: Any = None,
    jobs: Iterable[dict] = (),
    outcomes: list[StoredOutcome] | None = None,
    evidence_since: datetime | None = None,
) -> RunTraceFacts:
    """Collect this Work's Run facts. Reads only; ``outcomes`` None means unreadable."""
    card = board.card(work.repository, work.number) if board is not None else None
    # An unreadable instance only matters when it could be the one holding this card.
    board_readable = card is not None or board is None or bool(getattr(board, "complete", True))
    routes: tuple[Any, ...] = ()
    routes_readable = True
    reader = getattr(getattr(state, "budget_db", None), "latest_delivery_routes_by_run", None)
    if reader is not None:
        try:
            routes = tuple(reader(work.work_id))
        except Exception:  # noqa: BLE001 -- an unreadable route store is unknown, not empty
            routes_readable = False
    bindings = tuple(state.db.session_run_bindings(work_id=work.work_id))
    chats = state.chat_snapshot() if hasattr(state, "chat_snapshot") else None
    sessions = tuple(
        (key, state.chat_identity_key(key, chats))
        for key in dict.fromkeys(binding.session_key for binding in bindings)
    )
    return RunTraceFacts(
        work=work,
        card=card,
        board_readable=board_readable,
        deferral=read_deferral(card) if card is not None else "",
        routes=routes,
        routes_readable=routes_readable,
        outcomes=tuple(outcomes or ()),
        outcomes_readable=outcomes is not None,
        bindings=bindings,
        sessions=sessions,
        jobs=tuple(job for job in jobs if isinstance(job, dict)),
        evidence_since=evidence_since,
    )


# --- current Runs, for many Works at once (#2219) -----------------------------------------------


@dataclass(frozen=True)
class CurrentRunFacts:
    """What the store records about one Work's current Run: its newest route, its outcome and
    its session bindings. Only that Run's rows, never the Work's history."""

    routes: tuple[Any, ...] = ()  # db.DeliveryRoute
    outcomes: tuple[StoredOutcome, ...] = ()
    bindings: tuple[Any, ...] = ()  # db.SessionRunBinding


def _pairs_clause(pairs: list[tuple[str, str]]) -> tuple[str, list[str]]:
    marks = ", ".join("(?, ?)" for _ in pairs)
    return f"(work_id, run_id) IN (VALUES {marks})", [value for pair in pairs for value in pair]


def read_current_run_facts(
    db_path: Path | str, runs: dict[str, str]
) -> dict[str, CurrentRunFacts] | None:
    """The current Run's facts for every ``work_id -> run_id`` asked for, read-only.

    One connection and three queries per 300 Runs, however many Works: the front page
    classifies every candidate Work on a few-second cadence and must not read per Work.
    None when the store cannot be read, which a trace shows as unknown, never as "not
    running". A Work whose Run has no rows gets empty facts.
    """
    from .db import DeliveryRoute, SessionRunBinding

    pairs = [(work_id, run_id) for work_id, run_id in runs.items() if run_id]
    found: dict[str, dict[str, list[Any]]] = {
        work_id: {"routes": [], "outcomes": [], "bindings": []} for work_id, _ in pairs
    }
    path = Path(db_path).expanduser()
    if not pairs or not path.exists():
        return {work_id: CurrentRunFacts() for work_id in found}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        tables = {
            str(name)
            for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for start in range(0, len(pairs), 300):
            where, params = _pairs_clause(pairs[start : start + 300])
            if "run_execution_outcomes" in tables:
                for row in conn.execute(
                    f"SELECT work_id, {_OUTCOME_COLUMNS} FROM run_execution_outcomes"
                    f" WHERE {where} ORDER BY observed_at DESC",
                    params,
                ):
                    found[str(row[0])]["outcomes"].append(
                        StoredOutcome(
                            run_id=str(row[1]),
                            execution_state=str(row[2]),
                            reason_code=str(row[3] or ""),
                            last_activity_at=row[4],
                            terminal_result_at=row[5],
                            stop_requested_at=row[6],
                            stop_reason=str(row[7] or ""),
                            stopped_at=row[8],
                            observed_at=float(row[9] or 0.0),
                        )
                    )
            if "delivery_routes" in tables:
                for row in conn.execute(
                    "SELECT delivery_id, request_fingerprint, work_id, run_id, provider, account,"
                    f" worker_key, decided_at FROM delivery_routes WHERE {where}"
                    " ORDER BY decided_at DESC, rowid DESC",
                    params,
                ):
                    routes = found[str(row[2])]["routes"]
                    if not routes:  # the newest route of the Run, as the Work's own reader
                        routes.append(DeliveryRoute(*(str(value or "") for value in row)))
            if "session_run_bindings" in tables:
                for row in conn.execute(
                    "SELECT session_key, work_id, run_id, first_delivery_id, bound_at"
                    f" FROM session_run_bindings WHERE {where} ORDER BY bound_at, session_key",
                    params,
                ):
                    found[str(row[1])]["bindings"].append(
                        SessionRunBinding(
                            str(row[0]), str(row[1]), str(row[2]), row[3], str(row[4])
                        )
                    )
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return {
        work_id: CurrentRunFacts(
            tuple(rows["routes"]), tuple(rows["outcomes"]), tuple(rows["bindings"])
        )
        for work_id, rows in found.items()
    }


def current_run_id(work: WorkRef, card: Any) -> str:
    """The Run the Poller holds for this Work now, as ``build_run_trace`` picks it."""
    dispatch = getattr(card, "dispatch", None)
    if dispatch is not None and dispatch.work_id and dispatch.work_id != work.work_id:
        dispatch = None
    return (dispatch.run_id if dispatch is not None else "") or str(
        getattr(card, "generation", "") or ""
    )


def current_run_traces(
    db_path: Path | str | None,
    works: Iterable[tuple[WorkRef, Any]],
    *,
    board: Any = None,
    jobs: Iterable[dict] = (),
    evidence_since: datetime | None = None,
) -> dict[str, RunTrace]:
    """Each Work's trace of its current Run alone, from one batched read (#2219).

    ``build_run_trace`` over only the current Run's rows, so the current Run's state,
    reason and next step are exactly the full trace's — one definition of "stuck" — while
    earlier Runs, the deferral receipt and chat identities, which a state never reads, are
    left to the full trace. A Work with no Run fact at all gets no trace, as in the full one.
    """
    works = list(works)
    runs = {work.work_id: current_run_id(work, card) for work, card in works}
    facts = read_current_run_facts(db_path, runs) if db_path is not None else {}
    jobs = tuple(job for job in jobs if isinstance(job, dict))
    traces: dict[str, RunTrace] = {}
    for work, card in works:
        mine = (facts or {}).get(work.work_id, CurrentRunFacts())
        board_readable = card is not None or board is None or bool(getattr(board, "complete", True))
        known = RunTraceFacts(
            work=work,
            card=card,
            board_readable=board_readable,
            routes=mine.routes,
            outcomes=mine.outcomes,
            outcomes_readable=facts is not None,
            routes_readable=facts is not None,
            bindings=mine.bindings,
            sessions=tuple((b.session_key, b.session_key) for b in mine.bindings),
            jobs=jobs,
            evidence_since=evidence_since,
        )
        if known.known:
            traces[work.work_id] = build_run_trace(known)
    return traces


# --- the trace ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Milestone:
    """One thing that happened to a Run, from one named source."""

    kind: str  # stable machine name, e.g. "dispatch", "placed", "session", "execution"
    label: str
    at: datetime | None = None
    detail: str = ""
    problem: bool = False


@dataclass(frozen=True)
class RunSession:
    """A session leg that took part in the Run, by exact binding."""

    session_key: str
    chat_id: str
    provider: str
    account: str
    bound_at: datetime | None


@dataclass(frozen=True)
class RunView:
    run_id: str
    state: str
    reason: str = ""
    # The host's own code for the refusal the state rests on (``at_capacity`` ...), or "".
    refusal: str = ""
    next_expected: str = ""
    current: bool = False
    sparse: bool = False  # no Poller, route or execution fact: old history
    provider: str = ""
    account: str = ""
    sessions: tuple[RunSession, ...] = ()
    milestones: tuple[Milestone, ...] = ()
    verifications: tuple[RunVerification, ...] = ()
    latest_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.state in {FINISHED, FAILED, SUPERSEDED}


def waits_on_person(run: RunView | None) -> bool:
    """The Run is live and its worker waits on a person's prompt (``waiting_human``, #2896).

    It shares ``needs_attention`` with a Run nothing restarts, but nothing about it failed.
    """
    return run is not None and run.state == NEEDS_ATTENTION and run.reason == PERSON_WAIT


@dataclass(frozen=True)
class RunTrace:
    work_id: str
    current: RunView | None = None
    previous: tuple[RunView, ...] = ()
    # Before any Run, or between Runs: why the Work is not running (blockers, not started).
    preflight: str = ""
    preflight_next: str = ""
    # Sources that could not be read this time. Shown, so absence is never mistaken for "no".
    unreadable: tuple[str, ...] = ()
    pull_request_url: str = ""
    card_status: str = ""


def _when(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC) if value > 0 else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _execution_at(outcome: Any) -> datetime | None:
    """When the Run's execution reached the state it is in, or None while it is still moving.

    Never ``observed_at``: every re-observation rewrites it, so an ended Run would read as
    active every sweep and each card's "N ago" would be the deck's last look (#2332).
    """
    state = outcome.execution_state
    if state == "stopped":
        return _when(outcome.stopped_at) or _when(outcome.terminal_result_at)
    if state in {"ended", "died"}:
        return _when(outcome.terminal_result_at)
    return None


def _words(code: str) -> str:
    return code.replace("_", " ").strip()


def _stop_words(stop_reason: str) -> str:
    """A stop reason in words. ``superseded_by:<run>`` names a raw Run id, which stays in the
    collapsed detail rather than the compact view."""
    if stop_reason.startswith("superseded_by"):
        return "a later Run replaced it"
    return _words(stop_reason)


def _predates_recording(card: Any, evidence_since: datetime | None) -> bool:
    """The card's Run began before the Runtime recorded any Run, by the card's own clock.

    Both stamps must be known; an unknown one keeps the Run a warning. Age alone never makes
    a Run historical: one started after recording began stays uncertain however old it is.
    """
    started = _when(str(getattr(card, "started_at", "") or ""))
    return started is not None and evidence_since is not None and started < evidence_since


def _refusal_next(reason: str) -> str:
    if reason in CAPACITY_REFUSALS:
        return NEXT_CAPACITY
    if reason in _STOP_REFUSALS:
        return NEXT_STOP
    return NEXT_RUNTIME_ACCEPTANCE


def _split_key(session_key: str) -> tuple[str, str]:
    provider, _, rest = session_key.partition(":")
    account, _, _ = rest.partition(":")
    return provider, account


def _execution(
    outcome: StoredOutcome, *, current: bool
) -> tuple[str, str, str]:
    """State, reason and next step from the Runtime's recorded outcome alone."""
    state, reason = outcome.execution_state, outcome.reason_code
    stopping = outcome.stop_requested_at is not None and outcome.stopped_at is None
    if outcome.stop_reason.startswith("superseded_by") and outcome.stopped_at is not None:
        return SUPERSEDED, _stop_words(outcome.stop_reason), ""
    if state == "running":
        if stopping:
            return RUNNING, "stop requested", NEXT_STOP
        if reason == "turn_pending":
            return RUNNING, "delivered, turn about to start", NEXT_FIRST_ACTIVITY
        return RUNNING, _words(reason) or "turn active", NEXT_ACTIVITY if current else NEXT_STOP
    if state == "waiting_human":
        return NEEDS_ATTENTION, PERSON_WAIT, NEXT_HUMAN
    if state == "abandoned":
        return NEEDS_ATTENTION, "no worker activity for hours", NEXT_STOP
    if state == "died":
        return FAILED, f"the worker process died ({_words(reason) or 'no reason given'})", ""
    if state == "stopped":
        return (SUPERSEDED if not current else FINISHED), "stopped by the Runtime", ""
    if state == "not_started":
        return FAILED, "refused before it started", ""
    if state == "ended":
        if reason.startswith("refused:"):
            return FAILED, f"the provider refused the turn ({_words(reason[8:])})", ""
        if reason == "usage_limit":
            # The CLI calls this turn a success; it is one the account's limit cut short (#2862).
            return FAILED, "the account's usage limit cut the turn short", ""
        return FINISHED, "the worker's turn ended", ""
    if state == "uncertain":
        return UNCERTAIN, _words(reason) or "the Runtime cannot tell", NEXT_STOP if stopping else ""
    return UNCERTAIN, _words(state), ""


# Endings of the current Run the engine restarts by itself while restarts are left.
_REVIVED = frozenset({"died", "abandoned"})


def _revived_ending(outcome: Any) -> bool:
    """An ending the engine restarts: a death, or a turn the usage limit cut short (#2862)."""
    return outcome.execution_state in _REVIVED or (
        outcome.execution_state == ENDED and outcome.reason_code == USAGE_LIMIT
    )


def _revives_itself(card: Any) -> bool:
    """The card still works In progress under the engine and has restarts left."""
    return bool(
        card is not None
        and not getattr(card, "done", False)
        and not getattr(card, "in_review", False)
        and not getattr(card, "status_role", "")
        and int(getattr(card, "revives_left", 0) or 0) > 0
    )


def build_run_trace(facts: RunTraceFacts) -> RunTrace:
    """Derive the trace. Pure over ``facts``."""
    work_id = facts.work.work_id
    card = facts.card
    generation = str(getattr(card, "generation", "") or "")
    dispatch = getattr(card, "dispatch", None)
    if dispatch is not None and dispatch.work_id and dispatch.work_id != work_id:
        dispatch = None  # never attribute another Work's dispatch
    current_id = (dispatch.run_id if dispatch is not None else "") or generation

    routes = {route.run_id: route for route in facts.routes if route.work_id == work_id}
    outcomes = {row.run_id: row for row in facts.outcomes}
    bound: dict[str, list[Any]] = {}
    for binding in facts.bindings:
        if binding.work_id == work_id:
            bound.setdefault(binding.run_id, []).append(binding)
    chat_of = dict(facts.sessions)
    # Every retained verification job, not the Work card's newest five: older Runs keep theirs.
    verifications = run_verifications(facts.jobs, work_id, limit=None)
    by_run: dict[str, list[RunVerification]] = {}
    for check in verifications:
        by_run.setdefault(check.run_id, []).append(check)
    jobs_by_id = {str(job.get("id") or ""): job for job in facts.jobs}

    run_ids = list(
        dict.fromkeys(
            [rid for rid in (current_id,) if rid]
            + list(routes)
            + list(outcomes)
            + list(bound)
            + list(by_run)
        )
    )
    views = [
        _run_view(
            run_id,
            current=run_id == current_id,
            card=card,
            dispatch=dispatch if dispatch is not None and dispatch.run_id == run_id else None,
            deferral=facts.deferral,
            route=routes.get(run_id),
            outcome=outcomes.get(run_id),
            bindings=bound.get(run_id, ()),
            chat_of=chat_of,
            verifications=tuple(by_run.get(run_id, ())),
            jobs_by_id=jobs_by_id,
            evidence_since=facts.evidence_since,
        )
        for run_id in run_ids
    ]
    current = next((view for view in views if view.current), None)
    floor = datetime.min.replace(tzinfo=UTC)
    previous = tuple(
        sorted(
            (view for view in views if not view.current),
            key=lambda view: view.latest_at or floor,
            reverse=True,
        )
    )

    preflight = preflight_next = ""
    blockers = tuple(getattr(card, "blocked_by", ()) or ())
    if card is not None and blockers and (current is None or current.terminal):
        preflight = "blocked by " + ", ".join(blockers)
        preflight_next = NEXT_BLOCKERS
    elif card is None and facts.board_readable and current is None:
        preflight = "no Poller instance tracks this Work"

    unreadable = tuple(
        name
        for name, readable in (
            ("Poller state", facts.board_readable),
            ("delivery routes", facts.routes_readable),
            ("execution outcomes", facts.outcomes_readable),
        )
        if not readable
    )
    return RunTrace(
        work_id=work_id,
        current=current,
        previous=previous,
        preflight=preflight,
        preflight_next=preflight_next,
        unreadable=unreadable,
        pull_request_url=str(getattr(card, "pull_request_url", "") or ""),
        card_status=str(getattr(card, "status", "") or ""),
    )


def _run_view(
    run_id: str,
    *,
    current: bool,
    card: Any,
    dispatch: Any,
    deferral: str,
    route: Any,
    outcome: StoredOutcome | None,
    bindings: Iterable[Any],
    chat_of: dict[str, str],
    verifications: tuple[RunVerification, ...],
    jobs_by_id: dict[str, dict],
    evidence_since: datetime | None = None,
) -> RunView:
    milestones: list[Milestone] = []
    sessions = tuple(
        RunSession(
            session_key=binding.session_key,
            chat_id=chat_of.get(binding.session_key, binding.session_key),
            provider=_split_key(binding.session_key)[0],
            account=_split_key(binding.session_key)[1],
            bound_at=_when(binding.bound_at),
        )
        for binding in bindings
    )

    # 1. The Poller's durable dispatch for this Run.
    refusal = ""
    if dispatch is not None:
        what = "follow-up" if dispatch.steer else (dispatch.action or "dispatch")
        tries = f", attempt {dispatch.attempts}" if dispatch.attempts > 1 else ""
        milestones.append(
            Milestone(
                "dispatch",
                f"Dispatch prepared ({_words(what)}{tries})",
                _when(dispatch.sent_at),
                "sent to the Runtime" if dispatch.sent_at else "not sent yet",
            )
        )
        # The deferral receipt answers this attempt; ``refusal`` on the dispatch is the latest
        # one any attempt got, and may be older than the send now in flight.
        refusal = deferral
        if deferral or dispatch.refusal:
            milestones.append(
                Milestone(
                    "refused",
                    f"Runtime refused: {_words(deferral or dispatch.refusal)}",
                    detail="" if deferral else "an earlier attempt",
                    problem=True,
                )
            )
    parked = current and bool(getattr(card, "dispatch_failed", False))
    parked_refusal = str(getattr(card, "dispatch_failed_refusal", "") or "") if parked else ""
    # The park's refusal is the latest *positive* one. An ambiguous dispatch keeps it from an
    # earlier attempt after a later one timed out, so it is the park's cause only when the
    # final attempt was that refusal. A clean park refused every attempt, the last included.
    parked_cause = parked_refusal and bool(
        getattr(card, "dispatch_failed_refusal_final", False)
        or getattr(card, "dispatch_failed_clean", False)
    )
    if parked:
        if parked_refusal:
            milestones.append(
                Milestone(
                    "refused",
                    f"Runtime refused: {_words(parked_refusal)}",
                    detail="the last attempt" if parked_cause else "an earlier attempt",
                    problem=True,
                )
            )
        milestones.append(
            Milestone(
                "parked",
                "Delivery never landed; card parked",
                _when(getattr(card, "dispatch_failed_at", "")),
                "every attempt was refused before any write"
                if getattr(card, "dispatch_failed_clean", False)
                else "an attempt's outcome is unknown",
                problem=True,
            )
        )

    # 2. The Runtime's frozen route.
    provider = account = ""
    if route is not None:
        provider, account = route.provider, route.account
        milestones.append(
            Milestone(
                "placed",
                f"Runtime placed it on {route.provider}/{route.account}",
                _when(route.decided_at),
            )
        )

    # 4. Exact session participation.
    for session in sessions:
        milestones.append(
            Milestone(
                "session",
                f"Session attached ({session.provider}/{session.account})",
                session.bound_at,
            )
        )
        provider = provider or session.provider
        account = account or session.account

    # 3. The Runtime's recorded execution outcome.
    if outcome is not None:
        if outcome.last_activity_at:
            milestones.append(
                Milestone("activity", "Last worker activity", _when(outcome.last_activity_at))
            )
        if outcome.stop_requested_at:
            milestones.append(
                Milestone(
                    "stop",
                    "Stop requested",
                    _when(outcome.stop_requested_at),
                    _stop_words(outcome.stop_reason),
                )
            )
        milestones.append(
            Milestone(
                "execution",
                f"Execution {_words(outcome.execution_state)}",
                _execution_at(outcome),
                _words(outcome.reason_code),
                problem=outcome.execution_state
                in {"died", "abandoned", "uncertain", "not_started", "waiting_human"},
            )
        )

    # 5. Verification evidence.
    pending_check = False
    for check in verifications:
        job = jobs_by_id.get(check.job_id, {})
        at = _when(job.get("finished_at")) or _when(job.get("submitted_at"))
        milestones.append(
            Milestone(
                "verification",
                f"Verification {check.status}",
                at,
                f"job {check.job_id}",
                problem=check.status not in {"verified", "queued", "running"},
            )
        )
        pending_check = pending_check or check.state in {"queued", "running"}

    # The display state: explicit facts first, strongest source wins.
    sparse = dispatch is None and route is None and outcome is None and not parked and not (
        current and card is not None
    )
    # An ambiguous park means an attempt's outcome is unknown, and an unacknowledged attempt may
    # have started a worker whose acknowledgement was lost (``dispatch.observe_attempt``). Exact
    # participation from the Runtime is the stronger fact, so it drives the state; the park
    # stays in the milestones and the reason names the contradiction.
    # ``not_started`` is the Runtime saying no worker ever took part, so it is not evidence.
    participated = bool(sessions) or (
        outcome is not None and outcome.execution_state != "not_started"
    )
    if parked and not participated:
        clean = bool(getattr(card, "dispatch_failed_clean", False))
        state = NEEDS_ATTENTION
        reason = "the delivery never landed"
        if parked_cause:
            reason += f"; Runtime refused: {_words(parked_refusal)}"
        reason += "; every attempt was refused" if clean else "; an attempt's outcome is unknown"
        if parked_refusal and not parked_cause:
            reason += f"; an earlier attempt was refused: {_words(parked_refusal)}"
        refusal = parked_refusal if parked_cause else ""
        # Only a clean park with its one free retry unspent comes back on its own
        # (``dispatch.rearmable``); anything else waits for a person's new instruction.
        rearms = clean and not getattr(card, "dispatch_rearmed", False)
        next_expected = NEXT_RETRY if rearms else NEXT_HUMAN
    elif dispatch is not None and dispatch.steer:
        # A follow-up keeps the Run's lease (``dispatch.new_steer``), so this Run's outcome is
        # the turn before it. Until the follow-up is accepted or settled it is what the Run is
        # waiting on; the earlier outcome stays in the milestones as history.
        if refusal:
            state, reason = WAITING, f"Runtime refused the follow-up: {_words(refusal)}"
            next_expected = _refusal_next(refusal)
        elif dispatch.sent_at:
            state, reason, next_expected = (
                WAITING,
                "follow-up sent; the Runtime has not answered",
                NEXT_RUNTIME_ACCEPTANCE,
            )
        else:
            state, reason, next_expected = (
                PREPARING,
                "follow-up prepared, not sent yet",
                NEXT_RUNTIME_ACCEPTANCE,
            )
    elif outcome is not None and not (
        dispatch is not None and outcome.execution_state == "not_started"
    ):
        # A Run whose dispatch the Poller still holds has not been refused for good: no worker
        # took part *yet*, and the next wake sends it again. Read as failed, it put a revive
        # waiting for its own chat's turn to end into Needs retry (#2902).
        state, reason, next_expected = _execution(outcome, current=current)
        if current and _revived_ending(outcome) and _revives_itself(card):
            # The engine restarts a worker that died, was abandoned or hit its usage limit
            # mid-turn while its card has restarts left (``revive.py``); only past them does
            # it ask a person (#2235).
            next_expected = NEXT_RETRY
    elif sessions:
        # Participation is explicit; its execution was not recorded yet.
        state, reason, next_expected = (
            (WAITING, "session attached; no execution recorded yet", NEXT_ACTIVITY)
            if current
            else (SUPERSEDED, "a later Run replaced it", "")
        )
    elif dispatch is not None and not dispatch.steer and (refusal or route is None):
        if refusal:
            state, reason = WAITING, f"Runtime refused: {_words(refusal)}"
            next_expected = _refusal_next(refusal)
        elif dispatch.sent_at:
            state, reason, next_expected = (
                WAITING,
                "sent; the Runtime has not answered",
                NEXT_RUNTIME_ACCEPTANCE,
            )
        else:
            state, reason, next_expected = (
                PREPARING,
                "dispatch prepared, not sent yet",
                NEXT_RUNTIME_ACCEPTANCE,
            )
    elif route is not None:
        state, reason, next_expected = (
            (WAITING, "placed; no session has taken part yet", NEXT_SESSION)
            if current
            else (SUPERSEDED, "a later Run replaced it", "")
        )
    elif current and card is not None and _predates_recording(card, evidence_since):
        # Nothing names this Run because nothing was recording when it began. Its outcome is
        # unknown, so it is neither finished nor a live incident (#2201).
        state, reason, next_expected = HISTORICAL, "execution details unavailable", ""
        sparse = True
    elif current and card is not None:
        state, reason, next_expected = (
            UNCERTAIN,
            "the Poller holds this Run but no Runtime fact names it",
            "",
        )
    elif not current:
        state, reason, next_expected = SUPERSEDED, "a later Run replaced it", ""
    else:
        state, reason, next_expected = UNCERTAIN, "no Runtime fact names this Run", ""

    # A finished turn on the current Run still has verification and review ahead of it.
    if parked and participated:
        reason = (
            f"{reason}; the Poller parked this Run as never landed, "
            "but the Runtime shows it took part"
        )
    if current and state == FINISHED:
        if pending_check:
            state, next_expected = WAITING, NEXT_VERIFICATION
            reason = "the turn ended; verification is running"
        elif getattr(card, "in_review", False):
            state, next_expected = WAITING, NEXT_REVIEW
            reason = "the turn ended; the card is in review"
        elif getattr(card, "done", False):
            reason = "the Work is done"
    if current and getattr(card, "in_review", False):
        milestones.append(Milestone("review", "In review", detail=str(card.status)))

    stamped = [item.at for item in milestones if item.at is not None]
    milestones.sort(key=lambda item: item.at or datetime.max.replace(tzinfo=UTC))
    return RunView(
        run_id=run_id,
        state=state,
        reason=reason,
        refusal=refusal if state in {WAITING, NEEDS_ATTENTION} else "",
        next_expected=next_expected,
        current=current,
        sparse=sparse,
        provider=provider,
        account=account,
        sessions=sessions,
        milestones=tuple(milestones),
        verifications=verifications,
        latest_at=max(stamped) if stamped else None,
    )
