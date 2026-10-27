"""Per-test start and stop times for a queued suite, readable while it runs.

A queued suite's log is a wall of dots, so "what is it doing right now" and
"what has been slow so far" had no answer until the run ended. When
``AGENTDECK_TEST_TIMINGS`` names a file, the suite's conftest appends one JSON
line when each test starts and one when it stops; ``agentdeck job tests <id>``
reads that file back as the tests running now and the slowest finished ones.

Deliberately beside the job's log rather than in it: the log's tail is what the
submitting chat is sent, and eight thousand timing lines would bury the result.

Each test process also writes one ``collected`` line once collection is done,
giving the number of tests the run will go through, and every line names the
run it belongs to. The job-queue panel used to follow that as a live progress bar
(#2299); #2864 removed it, so only ``agentdeck job tests`` reads the file now.

The queue points the variable at ``<job id>.tests.jsonl`` next to the job's
log. A pod cannot write there, so ``scripts/kube_pytest.sh`` has the pod write
its own copy and streams it out while the suite runs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TIMINGS_ENV = "AGENTDECK_TEST_TIMINGS"


def timings_path_for_log(log_path: str | Path) -> Path:
    """Where a job whose output goes to ``log_path`` keeps its test timings."""
    path = Path(log_path)
    return path.with_name(f"{path.stem}.tests.jsonl")


class TimingRecorder:
    """Appends start/stop lines for one test process (an xdist worker or a
    plain run). Each line is one ``write`` on an ``O_APPEND`` descriptor, so
    workers sharing the file do not interleave inside a line."""

    def __init__(self, path: str | Path, worker: str, run: str = "") -> None:
        self._path = Path(path)
        self._worker = worker
        # Shared by every worker of one pytest invocation, and different for the
        # next one, even when that appends to the same file.
        self._run = run
        self._fd: int | None = None
        self._outcomes: dict[str, str] = {}

    def _write(self, record: dict) -> None:
        try:
            if self._fd is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            record = {**record, "run": self._run}
            os.write(self._fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
        except OSError:
            # A diagnostic must never fail the suite it is watching.
            pass

    def collected(self, count: int) -> None:
        """How many tests this run will go through, once collection is done.

        Every xdist worker collects the whole suite, so each writes the same
        count; the reader takes one of them rather than adding them up.
        """
        self._write(
            {"event": "collected", "count": count, "worker": self._worker, "at": time.time()}
        )

    def start(self, nodeid: str) -> None:
        self._write({"event": "start", "test": nodeid, "worker": self._worker, "at": time.time()})

    def report(self, nodeid: str, when: str, outcome: str) -> None:
        # The first phase that did not pass decides the result, as it does in
        # pytest's own summary; a failing setup or teardown is an error.
        if self._outcomes.get(nodeid, "passed") != "passed":
            return
        if outcome == "failed" and when != "call":
            outcome = "error"
        self._outcomes[nodeid] = outcome

    def stop(self, nodeid: str) -> None:
        self._write(
            {
                "event": "stop",
                "test": nodeid,
                "worker": self._worker,
                "at": time.time(),
                "outcome": self._outcomes.pop(nodeid, "unknown"),
            }
        )


@dataclass(frozen=True)
class Timing:
    test: str
    worker: str
    started: float
    stopped: float | None = None
    outcome: str = ""

    def seconds(self, now: float) -> float:
        return (self.stopped if self.stopped is not None else now) - self.started


def read_timings(path: str | Path) -> list[Timing]:
    """Every test the file has seen, in start order. Unreadable lines are
    skipped: the file is read while workers are still appending to it."""
    started: dict[tuple[str, str], Timing] = {}
    finished: list[Timing] = []
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
            event, test = record["event"], str(record["test"])
            worker, at = str(record.get("worker", "")), float(record["at"])
        except (ValueError, KeyError, TypeError):
            continue
        key = (worker, test)
        if event == "start":
            started[key] = Timing(test=test, worker=worker, started=at)
        elif event == "stop" and key in started:
            begun = started.pop(key)
            finished.append(
                Timing(
                    test=test,
                    worker=worker,
                    started=begun.started,
                    stopped=at,
                    outcome=str(record.get("outcome", "")),
                )
            )
    return sorted([*finished, *started.values()], key=lambda timing: timing.started)


def summarize(timings: list[Timing], *, now: float, slowest: int = 15) -> str:
    """The tests running now, then the slowest finished ones."""
    if not timings:
        return "No test timings recorded (yet) for this job."
    running = [timing for timing in timings if timing.stopped is None]
    done = [timing for timing in timings if timing.stopped is not None]
    counts: dict[str, int] = {}
    for timing in done:
        counts[timing.outcome] = counts.get(timing.outcome, 0) + 1
    tally = ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items()))
    first = min(timing.started for timing in timings)
    lines = [
        f"Finished: {len(done)} tests ({tally or 'none'}), "
        f"{now - first:.0f}s since the first started"
    ]
    lines.append(f"Running now: {len(running)}")
    for timing in sorted(running, key=lambda timing: timing.started):
        lines.append(
            f"  {timing.seconds(now):7.1f}s  since {_clock(timing.started)}  "
            f"{timing.worker or '-':>5}  {timing.test}"
        )
    if done:
        lines.append(f"Slowest finished (top {min(slowest, len(done))}):")
        for timing in sorted(done, key=lambda timing: timing.seconds(now), reverse=True)[:slowest]:
            span = f"{_clock(timing.started)}-{_clock(timing.stopped)}"
            lines.append(
                f"  {timing.seconds(now):7.1f}s  {span}  {timing.outcome:<7}  {timing.test}"
            )
    return "\n".join(lines)


def _clock(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M:%S") if at is not None else "-"
