"""Structured health: the engine's self-report and the operator's pause note (#176 slice 8).

A Poller Instance is a oneshot timer job — it wakes, works, and exits — so there is
no resident process to ask how it is doing (#107, #116). Every wake therefore writes
a structured health record to durable state, and an observer reconstructs the whole
picture from that record alone. Nothing here needs a log file: the reference
launcher logged a nonzero cycle result and exited 0, which made failure invisible
unless someone tailed ``/tmp`` (the #106 gap-4 fix).

Two rules make the record trustworthy:

* **Freshness is a first-class signal, orthogonal to state.** The record says what
  the instance last reported; freshness says whether that is still believable, from
  the record's age against the expected wake interval. An overdue instance is
  reported as *stale — last seen N ago*, never as current, so a dead poller can
  never show a green light.
* **A paused instance is not a broken one.** Pause is a durable note the next wake
  honours while continuing to wake and report, so a deliberately paused instance
  stays fresh and visibly paused instead of looking crashed.

Health state itself is derived, not stored: fail-closed drift outranks everything,
an error streak or a degraded worker backend is degradation rather than failure, and
a budget hold is normal operation (queued work waiting its turn), never ill health.

Pure, stdlib-only operations over the engine state payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

# How often a wake happens (systemd/agentdeck-poller@.timer: OnUnitInactiveSec=120, #3010).
WAKE_INTERVAL_S = 120
# Freshness is judged against the timer's own wake interval, so it moves with it (#3010).
DEFAULT_INTERVAL_S = WAKE_INTERVAL_S
# An instance is late after a couple of missed wakes and unbelievable after many.
LATE_AFTER_INTERVALS = 2
STALE_AFTER_INTERVALS = 10
# Consecutive failing cycles before the instance is called degraded rather than unlucky.
ERROR_STREAK_THRESHOLD = 3

_HEALTH_KEY = "health"
_PAUSE_KEY = "paused"


class Freshness(Enum):
    FRESH = "fresh"  # reported within the expected interval
    LATE = "late"    # a wake or two missed
    STALE = "stale"  # overdue enough that the report is no longer believable


class HealthState(Enum):
    OK = "ok"
    PAUSED = "paused"      # deliberately held by an operator, still reporting
    DEGRADED = "degraded"  # cycling, but something needs attention
    FAILING = "failing"    # fail-closed: the instance cannot safely run
    STALE = "stale"        # last report is too old to believe (may be down)


class HealthReason(Enum):
    CONFIG_DRIFT = "config-drift"                        # Manifest/contract mismatch (#108/#116)
    ENGINE_DRIFT = "engine-drift"                        # activated engine hash mismatch (#116)
    STUCK_LOCK = "stuck-lock"                            # the state lock could not be taken
    ERROR_STREAK = "error-streak"                        # repeated failing cycles
    TIMER_NOT_FIRING = "timer-not-firing"                # overdue with no pause to explain it
    WORKER_DELIVERY_DEGRADED = "worker-delivery-degraded"  # the worker backend is unreachable
    EXTENSION_DEGRADED = "extension-degraded"            # an extension was skipped or said degraded
    WORKER_ENDED_WITHOUT_REPORT = "worker-ended-without-report"  # a lease can no longer resolve
    STATE_FALLBACK = "state-fallback"                    # durable state was read from the backup


@dataclass
class CycleReport:
    """What one wake did, and what went wrong while doing it.

    Counters the caller fills in as the cycle runs; ``errors`` collects the cycle's
    failures so a failing wake is visible in durable state rather than only in a log.
    """

    scans: int = 0
    scan_failures: int = 0
    commands_ingested: int = 0
    queue_depth: int = 0
    cursor_age_s: float | None = None
    dispatch_attempts: int = 0
    deliveries_failed: int = 0
    releases: int = 0
    deploys: int = 0
    budget_holds: int = 0
    extension_failures: int = 0
    # Cards whose worker's turn the host saw end with no report of its own, and which the
    # engine could not restart itself (#279). A lease nothing will resolve without a person,
    # which is what ``WORKER_ENDED_WITHOUT_REPORT`` means — so a card the engine *did* revive
    # is counted below instead, and does not degrade health.
    reportless_ends: int = 0
    # Cards restarted after their worker was killed mid-turn (#802). Recovery, not failure:
    # reported so the rate is visible, but health stays healthy.
    revives: int = 0
    active_workers: int = 0
    worker_backend_ok: bool = True
    state_fallback: bool = False
    stuck_lock: bool = False
    extension_health: dict = field(default_factory=dict)
    drift: str | None = None
    errors: list[str] = field(default_factory=list)

    def failed(self, detail: str) -> None:
        """Record a cycle failure (an exception, a nonzero helper, a refused write)."""
        self.errors.append(detail)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.stuck_lock and self.drift is None


_COUNTERS = (
    "scans",
    "scan_failures",
    "commands_ingested",
    "dispatch_attempts",
    "deliveries_failed",
    "releases",
    "deploys",
    "budget_holds",
    "extension_failures",
    "reportless_ends",
    "revives",
)


def record(state: dict) -> dict:
    """The durable health record (created empty on first access)."""
    return state.setdefault(_HEALTH_KEY, {})


def record_cycle(state: dict, report: CycleReport, *, now: datetime) -> dict:
    """Publish one wake's health into durable state and return the updated record.

    Cumulative totals answer "how has this instance behaved", the last report answers
    "what happened just now", and the error streak is what distinguishes one unlucky
    cycle from an instance that needs attention. A clean cycle clears the streak.
    """
    current = record(state)
    totals = current.setdefault("totals", {})
    for counter in _COUNTERS:
        totals[counter] = int(totals.get(counter) or 0) + int(getattr(report, counter))
    current["cycles"] = int(current.get("cycles") or 0) + 1
    current["last_cycle_at"] = now.astimezone(UTC).isoformat()
    current["last_report"] = {
        "queue_depth": report.queue_depth,
        "cursor_age_s": report.cursor_age_s,
        "active_workers": report.active_workers,
        "worker_backend_ok": report.worker_backend_ok,
        "state_fallback": report.state_fallback,
        "stuck_lock": report.stuck_lock,
        "drift": report.drift,
        "extensions": dict(report.extension_health),
        "errors": list(report.errors),
        **{counter: getattr(report, counter) for counter in _COUNTERS},
    }
    if report.ok:
        current["error_streak"] = 0
    else:
        current["error_streak"] = int(current.get("error_streak") or 0) + 1
        current["last_error"] = (report.errors or [report.drift or "state lock unavailable"])[-1]
        current["last_error_at"] = current["last_cycle_at"]
    return current


# --- the operator's pause note ---

def set_paused(state: dict, paused: bool, *, note: str | None = None) -> None:
    """Set or clear the durable pause note the next wake honours.

    The timer keeps waking a paused instance — the no-op wakes are cheap and keep it
    fresh and visibly paused rather than indistinguishable from a crashed host.
    """
    if paused:
        state[_PAUSE_KEY] = {"note": note or ""}
    else:
        state.pop(_PAUSE_KEY, None)


def is_paused(state: dict) -> bool:
    return bool(state.get(_PAUSE_KEY))


def pause_note(state: dict) -> str:
    value = state.get(_PAUSE_KEY) or {}
    return value.get("note") or ""


# --- derived health ---

@dataclass(frozen=True)
class HealthVerdict:
    """Effective health: what was reported, crossed with whether it is still believable."""

    state: HealthState
    freshness: Freshness
    age_seconds: float | None
    reasons: tuple[HealthReason, ...] = ()


def age_seconds(current: dict, now: datetime) -> float | None:
    """Seconds since the last reported cycle, or None if the instance never reported."""
    stamp = current.get("last_cycle_at")
    try:
        last = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now.astimezone(UTC) - last).total_seconds()


def freshness(
    current: dict,
    *,
    now: datetime,
    interval_s: int = DEFAULT_INTERVAL_S,
    late_after_intervals: int = LATE_AFTER_INTERVALS,
    stale_after_intervals: int = STALE_AFTER_INTERVALS,
) -> tuple[Freshness, float | None]:
    """Classify how believable the last report is, from its age against the wake interval."""
    age = age_seconds(current, now)
    if age is None:
        # Never reported: treat as stale rather than healthy — nothing has proven it runs.
        return Freshness.STALE, None
    if age > interval_s * stale_after_intervals:
        return Freshness.STALE, age
    if age > interval_s * late_after_intervals:
        return Freshness.LATE, age
    return Freshness.FRESH, age


def evaluate(
    state: dict,
    *,
    now: datetime,
    interval_s: int = DEFAULT_INTERVAL_S,
    error_streak_threshold: int = ERROR_STREAK_THRESHOLD,
    **freshness_kwargs,
) -> HealthVerdict:
    """Derive effective health from the durable record (see module docstring)."""
    current = record(state)
    fresh, age = freshness(current, now=now, interval_s=interval_s, **freshness_kwargs)
    last = current.get("last_report") or {}
    paused = is_paused(state)

    reasons: list[HealthReason] = []
    drift = last.get("drift")
    if drift == "engine":
        reasons.append(HealthReason.ENGINE_DRIFT)
    elif drift:
        reasons.append(HealthReason.CONFIG_DRIFT)
    if last.get("stuck_lock"):
        reasons.append(HealthReason.STUCK_LOCK)
    if int(current.get("error_streak") or 0) >= error_streak_threshold:
        reasons.append(HealthReason.ERROR_STREAK)
    if not last.get("worker_backend_ok", True):
        reasons.append(HealthReason.WORKER_DELIVERY_DEGRADED)
    if last.get("state_fallback"):
        reasons.append(HealthReason.STATE_FALLBACK)
    if int(last.get("reportless_ends") or 0) > 0:
        # The card cannot advance on its own from here, and it is invisible otherwise: this is
        # the whole point of noticing in minutes instead of after the stale window (#279).
        reasons.append(HealthReason.WORKER_ENDED_WITHOUT_REPORT)
    if any(
        (value or {}).get("state") == "degraded"
        for value in (last.get("extensions") or {}).values()
    ):
        # A skipped extension is degradation, never failure: the cycle itself ran (#112).
        reasons.append(HealthReason.EXTENSION_DEGRADED)
    if fresh is Freshness.STALE and not paused:
        reasons.append(HealthReason.TIMER_NOT_FIRING)

    if drift or last.get("stuck_lock"):
        # Fail-closed conditions outrank freshness: this instance must not run at all.
        health = HealthState.FAILING
    elif fresh is Freshness.STALE:
        # An overdue report is never presented as current, paused or not.
        health = HealthState.STALE
    elif paused:
        health = HealthState.PAUSED
    elif reasons:
        health = HealthState.DEGRADED
    else:
        health = HealthState.OK
    return HealthVerdict(
        state=health, freshness=fresh, age_seconds=age, reasons=tuple(reasons)
    )
