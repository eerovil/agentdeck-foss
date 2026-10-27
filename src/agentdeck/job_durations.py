"""How long a queued job may run before the queue stops it (#3038).

A suite that hangs used to hold its slot until someone noticed: a pod run has
``kube_pytest.sh``'s own silence watchdog and deadline, but a job on this host
had nothing at all, and a 30-minute run of a suite that normally takes eight
was found by a person looking at the queue.

The limit is learned from the project rather than configured: three times the
slowest of its last passed runs, never under fifteen minutes, and an hour for a
project the queue has not seen pass yet. "Project" is the repository the job
tested on the lane it ran on, because one suite takes very different times in
a pod and on this host: a full agentdeck suite is 6-10 minutes in a pod and
about 31 on this host. A lane with no passed run of the project yet borrows the
project's runs on another lane, scaled by how much slower or faster the lane is
(``LANE_SLOWDOWN``), before falling back to the unknown-project hour. That is
the case that cost a real run (#3038): a suite spilled to this host on a night
when the desktop was down must not be judged by its pod times. A submitter who knows
better asks with ``agentdeck job submit --max-minutes``; ``0`` means no limit.

Small on purpose, like ``verification_baselines``: one JSON file next to
``jobs.json``, and losing it costs only the default limit, so every failure
path degrades quietly rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_FILENAME", "DurationLedger", "TimeLimit"]

DEFAULT_FILENAME = "job_durations.json"
# How many passed runs per project the limit is learned from.
HISTORY = 20
MULTIPLIER = 3
FLOOR_S = 15 * 60.0
# A project with no passed run yet: generous, because a wrong guess here kills a
# suite that was fine, and the next pass teaches the real number.
UNKNOWN_S = 60 * 60.0
# How many times slower a suite runs on a lane than in a pod, for borrowing one lane's history
# for another. This host is four shared cores behind one heavy slot; the pods are alike.
LANE_SLOWDOWN = {"local": 6.0}
# How many projects are remembered at all, so a stream of scratch checkouts
# cannot grow the file for ever.
MAX_PROJECTS = 200


@dataclass(frozen=True)
class TimeLimit:
    """One job's limit in seconds (None: no limit) and why it is that."""

    seconds: float | None
    basis: str


def project_key(repository: str, lane: str) -> str:
    return f"{repository.strip().lower()} {lane}"


def _slowdown(lane: str) -> float:
    return LANE_SLOWDOWN.get(lane, 1.0)


def limit_for(
    by_lane: dict[str, list[float]], *, repository: str, lane: str, max_minutes: int | None
) -> TimeLimit:
    """The submitter's limit when it named one, else the one learned for ``lane``.

    ``by_lane`` holds the repository's passed-run durations on every lane. Each lane's runs
    are scaled to ``lane`` by ``LANE_SLOWDOWN`` and the most generous answer wins: a lane's
    own history can be only quick focused runs, and a whole suite on it must not be judged
    by those when another lane has seen the whole suite pass.
    """
    if max_minutes is not None:
        if max_minutes <= 0:
            return TimeLimit(None, "no limit, as submitted")
        return TimeLimit(max_minutes * 60.0, "as submitted with --max-minutes")
    candidates = []
    for source, runs in by_lane.items():
        if not runs:
            continue
        scale = _slowdown(lane) / _slowdown(source)
        slowest = max(runs)
        basis = (
            f"{MULTIPLIER}x the slowest of the last {len(runs)} passed runs of {repository} "
            f"on {source} ({slowest / 60:.0f} min)"
        )
        if source != lane:
            basis += f", scaled {scale:g}x for {lane}"
        # The job's own lane first among equals, so its basis is the one named on a tie.
        candidates.append((MULTIPLIER * slowest * scale, source == lane, basis))
    if not candidates:
        return TimeLimit(UNKNOWN_S, f"no passed run of {repository} yet")
    seconds, _, basis = max(candidates)
    if seconds < FLOOR_S:
        return TimeLimit(FLOOR_S, f"{basis}, raised to the {FLOOR_S / 60:.0f} min floor")
    return TimeLimit(seconds, basis)


class DurationLedger:
    """The last passed runs' durations per project, newest last, deduplicated by job id."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def durations(self, key: str) -> list[float]:
        entries = self._read().get(key) or []
        return [float(entry["seconds"]) for entry in entries][-HISTORY:]

    def record(self, key: str, job_id: str, seconds: float) -> None:
        if seconds <= 0:
            return
        data = self._read()
        entries = [entry for entry in data.get(key) or [] if entry.get("job_id") != job_id]
        entries.append({"job_id": job_id, "seconds": round(seconds, 1)})
        data.pop(key, None)
        # Re-inserted last, so the least recently passed project is the one trimmed.
        data[key] = entries[-HISTORY:]
        while len(data) > MAX_PROJECTS:
            data.pop(next(iter(data)))
        self._write(data)

    def seed(self, runs: Iterable[tuple[str, str, float]]) -> None:
        """Start the file from ``(key, job_id, seconds)`` runs, only if there is none yet."""
        if self.path.exists():
            return
        data: dict[str, list[dict[str, Any]]] = {}
        for key, job_id, seconds in runs:
            if seconds > 0:
                data.setdefault(key, []).append({"job_id": job_id, "seconds": round(seconds, 1)})
        self._write({key: entries[-HISTORY:] for key, entries in data.items()})

    def _read(self) -> dict[str, list[dict[str, Any]]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("jobs: could not read %s; starting with no durations", self.path)
            return {}
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, list[dict[str, Any]]] = {}
        for key, entries in raw.items():
            if not isinstance(entries, list):
                continue
            kept = []
            for entry in entries:
                try:
                    kept.append(
                        {"job_id": str(entry["job_id"]), "seconds": float(entry["seconds"])}
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            clean[str(key)] = kept
        return clean

    def _write(self, data: dict[str, list[dict[str, Any]]]) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            log.exception("jobs: could not write %s", self.path)
