"""Stop protocol (#176 slice 5, Runtime-owned since #2159).

Stopping an active card must not race the worker. The engine captures the worker's current
generation — the card's Run — as ``stop_waiting_generation``, then mints a fresh lease. That
instantly fences the old Run's lifecycle: its reports are refused from then on.

A lease is not a process, though. Whether the old Run's provider process is still writing the
Work's workspace is the persistent Runtime's fact, not the worker's to confess. So the host asks
the Runtime to stop that Run (``POST /executions/stop``), and the engine reads the Runtime's own
outcome for it (#2200). The stop is released only once that outcome says the stop was asked and
nothing in the Run can still write, on a terminal marker the stopped
generation had already posted, or when the stop awaited no generation at all. There is no
stop-ack from the model and no stale window: a dead or wedged worker is stopped by the Runtime,
and an answer the Runtime cannot give keeps the stop waiting rather than timing it out.

A worker that was already finishing when the stop arrived posts its ordinary terminal marker,
bound to the generation it worked under — the one the stop invalidated. That marker also
carries the status the finished work asked for, so it releases the stop with that status (#299).

Pure, stdlib-only operations over one card's state record. Whether the Runtime has proved the
stop is I/O the caller supplies as ``stopped``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from . import leases

# How long a stop may wait before the instance's health says so (#2073). A report, never a
# release: only the Runtime's answer releases a stop.
STALE_SECONDS = 2 * 3600


class StopDecision(Enum):
    RELEASED = "released"      # the Runtime proved the stopped Run's execution safe
    ENDED = "ended"            # the stopped turn ended on its own, and said what it did
    WAITING = "waiting"        # the Runtime has not proved it yet


def is_active(rec: dict) -> bool:
    """True if a worker is (or may be) running: status In progress, or work in processing."""
    return rec.get("status") == "In progress" or bool(rec.get("processing"))


def request_stop(rec: dict, *, now: datetime) -> bool:
    """Arm a stop on an active card; return True if armed, False if the card is idle.

    Captures the current generation to await, invalidates the lease so the active
    worker is rejected immediately, and marks the card stopping. Idempotent: a card
    already stopping stays as-is.
    """
    if rec.get("stopping"):
        return True
    if not is_active(rec):
        return False
    rec["stop_waiting_generation"] = leases.current(rec)
    leases.invalidate(rec)  # instantly invalidates the active worker's lease
    rec["stopping"] = True
    rec["stop_requested_at"] = now.astimezone(UTC).isoformat()
    return True


def stopped_generation(rec: dict) -> str:
    """The Run the stop is waiting on — the one it invalidated, not the new lease."""
    return rec.get("stop_waiting_generation") or ""


def seconds_since_request(rec: dict, now: datetime) -> float | None:
    requested = rec.get("stop_requested_at")
    try:
        started = datetime.fromisoformat(requested.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (now.astimezone(UTC) - started).total_seconds()


def unresolved(rec: dict) -> bool:
    """Is this card's stop still waiting for evidence of a safe exit?

    ``request_stop`` writes these two fields and ``finalize_stop`` clears them, and nothing
    else touches either — so their presence *is* the question, with no second copy to keep
    in step.
    """
    return bool(rec.get("stopping")) and bool(rec.get("stop_requested_at"))


def stalled(issues: dict, *, now: datetime, stale_after_s: int = STALE_SECONDS) -> list[str]:
    """Cards whose stop has been unresolved for longer than the stale window (#2073).

    Derived from the durable records rather than tallied by a wake, because the wake is the
    thing that may not have looked. A webhook-quiet wake reads no cards at all and a partial
    one reads some; either way these records are unchanged, so the answer is the same one the
    last complete wake would have given. That is what makes a carry-forward, a
    reconciliation-completeness flag and a repair of an already-published count all
    unnecessary: there is no per-wake observation to preserve in the first place.

    A stop with no ``stop_requested_at`` reads as not yet late rather than infinitely late,
    for the same reason it does in ``reconcile_stop``: an unreadable clock is not evidence.
    """
    late = []
    for key, rec in sorted((issues or {}).items()):
        if not isinstance(rec, dict) or not unresolved(rec):
            continue
        waited = seconds_since_request(rec, now)
        if waited is not None and waited > stale_after_s:
            late.append(key)
    return late


def reconcile_stop(rec: dict, *, stopped: bool, ended: bool = False) -> StopDecision:
    """Decide a stopping card's fate from the Runtime's answer and the card's own markers.

    ``ended`` is a terminal marker carrying the stopped generation: the worker finished
    the turn the stop was interrupting. It is checked first because it answers more than
    the Runtime's stop does — the turn is over *and* the work reported a status (#299).
    ``stopped`` is the Runtime's positive answer that the stopped Run's execution can no
    longer write. Nothing else releases a stop: time passing is not evidence (#2159).
    """
    if ended:
        return StopDecision.ENDED
    if stopped:
        return StopDecision.RELEASED
    if not stopped_generation(rec):
        # The stop awaits the empty string: it was armed while the card held no generation
        # at all, so no Run ever started on it (#2111). There is nothing for the Runtime to
        # stop, and a stop that awaits nothing may not hold anything.
        return StopDecision.RELEASED
    return StopDecision.WAITING


def finalize_stop(rec: dict) -> None:
    """Clear stop bookkeeping once released."""
    for field in (
        "stop_waiting_generation",
        "stopping",
        "stop_requested_at",
        "stop_control",
        "release_label",  # written before #2497, when a stop removed the ownership label
    ):
        rec.pop(field, None)
