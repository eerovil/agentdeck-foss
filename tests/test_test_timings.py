"""Per-test start/stop times a queued suite leaves beside its log (#2297)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentdeck.test_timings import (
    TIMINGS_ENV,
    TimingRecorder,
    read_timings,
    summarize,
    timings_path_for_log,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_timings_file_sits_beside_the_log():
    assert timings_path_for_log("/jobs/logs/abc123.log") == Path("/jobs/logs/abc123.tests.jsonl")


def test_a_test_s_first_failing_phase_decides_its_outcome(tmp_path):
    path = tmp_path / "t.jsonl"
    recorder = TimingRecorder(path, "gw0")
    for nodeid, phases in {
        "a": [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
        "b": [("setup", "passed"), ("call", "failed"), ("teardown", "passed")],
        "c": [("setup", "failed"), ("teardown", "passed")],
        "d": [("setup", "skipped"), ("teardown", "passed")],
        "e": [("setup", "passed"), ("call", "passed"), ("teardown", "failed")],
    }.items():
        recorder.start(nodeid)
        for when, outcome in phases:
            recorder.report(nodeid, when, outcome)
        recorder.stop(nodeid)

    outcomes = {timing.test: timing.outcome for timing in read_timings(path)}
    assert outcomes == {"a": "passed", "b": "failed", "c": "error", "d": "skipped", "e": "error"}


def test_the_summary_names_what_runs_now_and_what_was_slow(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        {"event": "start", "test": "t::slow", "worker": "gw0", "at": 100.0},
        {"event": "start", "test": "t::quick", "worker": "gw1", "at": 101.0},
        {"event": "stop", "test": "t::quick", "worker": "gw1", "at": 102.0, "outcome": "passed"},
        {"event": "stop", "test": "t::slow", "worker": "gw0", "at": 140.0, "outcome": "failed"},
        {"event": "start", "test": "t::stuck", "worker": "gw1", "at": 150.0},
    ]
    # A half-written last line is what a reader sees while a worker appends.
    path.write_text("\n".join(json.dumps(line) for line in lines) + '\n{"event": "sta')

    summary = summarize(read_timings(path), now=200.0, slowest=5)

    assert "Finished: 2 tests (1 failed, 1 passed), 100s since the first started" in summary
    running, slowest = summary.split("Slowest finished")
    assert "Running now: 1" in running
    assert "50.0s" in running and "gw1" in running and "t::stuck" in running
    assert slowest.index("t::slow") < slowest.index("t::quick")
    assert "40.0s" in slowest and "failed" in slowest


def test_no_file_is_an_answer_not_an_error(tmp_path):
    assert "No test timings" in summarize(read_timings(tmp_path / "missing"), now=0.0)


def test_a_real_parallel_run_records_every_test_once(tmp_path):
    """The conftest hooks, run by pytest itself across two xdist workers, and
    not inherited by a suite a test starts in a subprocess."""
    timings = tmp_path / "timings.jsonl"
    env = {**os.environ, TIMINGS_ENV: str(timings), "AGENTDECK_HEAVY_SLOT_HELD": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "tests/test_test_timings.py",
            "-k",
            "summary or outcome or beside",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    recorded = read_timings(timings)
    assert sorted(timing.test.rsplit("::", 1)[1] for timing in recorded) == [
        "test_a_test_s_first_failing_phase_decides_its_outcome",
        "test_the_summary_names_what_runs_now_and_what_was_slow",
        "test_the_timings_file_sits_beside_the_log",
    ]
    assert all(timing.stopped is not None and timing.outcome == "passed" for timing in recorded)
    assert {timing.worker for timing in recorded} <= {"gw0", "gw1"}

    # Both workers said how many they collected, under the one run id they share.
    records = [json.loads(line) for line in timings.read_text().splitlines()]
    collected = [record for record in records if record["event"] == "collected"]
    assert {record["count"] for record in collected} == {3}
    assert len({record["run"] for record in records}) == 1
