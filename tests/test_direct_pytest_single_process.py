"""A direct `pytest` on this host runs as one process; a queued one keeps xdist (#2666).

`pyproject.toml` asks for `-n auto`, which made a one-file check four workers beside
the deck. The conftest turns that off unless the queue started the run or the caller
asked for `-n` themselves.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest

from agentdeck.host_slots import QUEUED_JOB_ENV

REPO = Path(__file__).resolve().parents[1]


def _config(args=(), numprocesses="auto"):
    return SimpleNamespace(
        option=SimpleNamespace(numprocesses=numprocesses, dist="load", tx=["popen"] * 4),
        invocation_params=SimpleNamespace(args=tuple(args)),
    )


def test_a_direct_run_is_one_process(monkeypatch):
    monkeypatch.delenv(QUEUED_JOB_ENV, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    config = _config(["-q", "tests/test_jobs.py"])

    conftest._run_single_process_unless_queued(config)

    assert config.option.dist == "no"
    assert config.option.numprocesses == 0
    assert config.option.tx == []


def test_a_queued_run_keeps_its_workers(monkeypatch):
    monkeypatch.setenv(QUEUED_JOB_ENV, "1")
    config = _config(["-q"])

    conftest._run_single_process_unless_queued(config)

    assert config.option.dist == "load"


@pytest.mark.parametrize(
    "args",
    [["-n", "4"], ["-n4"], ["-nauto"], ["--numprocesses=2"], ["--numprocesses", "2"]],
)
def test_asking_for_workers_is_honoured(monkeypatch, args):
    monkeypatch.delenv(QUEUED_JOB_ENV, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    config = _config(args)

    conftest._run_single_process_unless_queued(config)

    assert config.option.dist == "load"


def test_workers_asked_for_through_pytest_addopts_are_honoured(monkeypatch):
    monkeypatch.delenv(QUEUED_JOB_ENV, raising=False)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-n 3")
    config = _config(["-q"])

    conftest._run_single_process_unless_queued(config)

    assert config.option.dist == "load"


def test_a_run_without_xdist_is_left_alone(monkeypatch):
    monkeypatch.delenv(QUEUED_JOB_ENV, raising=False)
    config = _config(["-p", "no:xdist"], numprocesses=None)

    conftest._run_single_process_unless_queued(config)

    assert config.option.dist == "load"


@pytest.mark.parametrize("queued", [False, True])
def test_a_real_direct_run_starts_no_workers(queued):
    """Through pytest and xdist themselves, not just the helper: no `gw` worker appears,
    while the same run started by the queue still gets them."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (QUEUED_JOB_ENV, "PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER")
    }
    env["AGENTDECK_HEAVY_SLOT_HELD"] = "1"  # this run is inside one already
    # `-n auto` stays what the run asks for; one worker is enough to see one, where
    # a pod's core count would start dozens, each importing the whole conftest.
    env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = "1"
    if queued:
        env[QUEUED_JOB_ENV] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "-p",
            "no:cacheprovider",
            f"{Path(__file__).relative_to(REPO)}::test_a_queued_run_keeps_its_workers",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert ("[gw" in result.stdout) is queued
