"""Asking the persistent Runtime to stop the Runs the engine is waiting on (#279, #2159, #2200).

A card leaves ``In progress`` only when the worker posts a lease-bound terminal marker. One in
three once did not, and the card then held a lease that could never resolve: the next
instruction sat ``pending`` forever and *nothing reported a problem*.

Whether a Run's worker is still running, ended, died, is wedged, or has been stopped is the
Runtime's fact (``agentdeck.run_execution``), and the engine now reads it from the Runtime
itself (``GitHubAdapter.turn_ended``). This host used to ask on the engine's behalf and write
the answer down as turn-watch and turn-end receipts; it no longer keeps either.

One thing is still done here, after the engine's cycle: for every card the engine has marked
stopping, ``stop_runs`` asks the Runtime to stop the Run the stop invalidated. The Runtime ends
the provider process itself; nothing waits for the model to acknowledge anything. The engine's
stop fence then reads the Runtime's outcome for that Run, which records that the stop was
asked and whether anything in the Run can still write.

Silence is still not ``in_review``: the worker is the only participant that knows whether the
work is done, so nothing here publishes a status.
"""

from __future__ import annotations

import json
import subprocess

from ..poller_engine.dispatch import work_id

# How long one stop is given. The Runtime ends the provider process before it answers.
RUNTIME_STOP_TIMEOUT_S = 60.0

# What ``POST /executions/stop`` answers when nothing can write any more.
STOP_PROVED = frozenset({"stopped", "already_stopped"})


def _runtime(
    socket: str,
    path: str,
    *,
    body: dict | None = None,
    curl: str = "curl",
    timeout: float = RUNTIME_STOP_TIMEOUT_S,
) -> dict | None:
    """One JSON call to the persistent runtime over its socket, or None when it did not answer."""
    command = [
        curl, "--fail-with-body", "--silent", "--show-error",
        "--max-time", str(int(timeout)),
        "--unix-socket", socket,
    ]
    if body is not None:
        command += [
            "-X", "POST", "-H", "content-type: application/json", "--data", json.dumps(body),
        ]
    command.append(f"http://agentdeck-runtime{path}")
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout + 5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        payload = json.loads(done.stdout or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def stop_execution(
    socket: str, work: str, run_id: str, reason: str, *, curl: str = "curl"
) -> dict | None:
    """Ask the Runtime to stop one Run's execution; its answer, or None when it did not answer."""
    return _runtime(
        socket,
        "/executions/stop",
        body={"work_id": work, "run_id": run_id, "reason": reason},
        curl=curl,
    )


def _card_parts(key: str) -> tuple[str, int] | None:
    repo, _, number = str(key).rpartition("#")
    try:
        return repo, int(number)
    except ValueError:
        return None


def stop_runs(
    *,
    socket: str,
    owner: str,
    issues: dict,
    stop=stop_execution,
    curl: str = "curl",
) -> list[str]:
    """Have the Runtime stop every Run the engine is waiting on. Returns log lines.

    The engine marks a card stopping and invalidates its lease; the stop itself is the
    Runtime's (#2159). The Runtime records the ask on the Run's outcome, and its positive
    answer — ``stopped``, or ``already_stopped`` for a Run that was over anyway — is exactly
    what the engine's stop fence reads there on its next wake. Asking again is safe: a stopped
    Run answers ``already_stopped``. Any other answer leaves the stop waiting: unknown is not
    stopped, and no timeout stands in for proof.
    """
    lines: list[str] = []
    for key, rec in sorted((issues or {}).items()):
        if not isinstance(rec, dict) or not rec.get("stopping"):
            continue
        parts = _card_parts(key)
        generation = str(rec.get("stop_waiting_generation") or "")
        if parts is None or not generation or not rec.get("stop_requested_at"):
            # No generation: nothing ever ran, and the engine releases that stop itself.
            continue
        repo, number = parts
        answer = stop(socket, work_id(owner, repo, number), generation, "poller_stop", curl=curl)
        result = (answer or {}).get("result")
        if result not in STOP_PROVED:
            lines.append(
                f"{key}: stop of Run {generation} not yet proved by the runtime "
                f"({result or 'no answer'})"
            )
            continue
        lines.append(f"{key}: runtime {result.replace('_', ' ')} Run {generation}")
    return lines
