"""A queued job's time limit and its progress messages (#3038)."""

from __future__ import annotations

import asyncio
import signal
import sys

from test_jobs import _queue, _settle

from agentdeck import __main__ as cli
from agentdeck import job_durations
from agentdeck.job_durations import DurationLedger, limit_for
from agentdeck.jobs import DONE, FAILED, Job, quiet_output_hint


def _limit(by_lane, lane="desktop", max_minutes=None):
    return limit_for(by_lane, repository="eerovil/agentdeck", lane=lane, max_minutes=max_minutes)


def test_the_limit_is_three_times_the_slowest_passed_run_with_a_floor():
    assert _limit({}).seconds == job_durations.UNKNOWN_S
    assert "no passed run of eerovil/agentdeck yet" in _limit({"local": []}).basis
    eight = _limit({"desktop": [300.0, 480.0, 420.0]})
    assert eight.seconds == 3 * 480.0
    assert "slowest of the last 3 passed runs of eerovil/agentdeck on desktop" in eight.basis
    assert "scaled" not in eight.basis
    quick = _limit({"desktop": [60.0]})
    assert quick.seconds == job_durations.FLOOR_S
    assert "floor" in quick.basis


def test_the_submitter_s_limit_wins_and_zero_means_none():
    assert _limit({"desktop": [600.0]}, max_minutes=0).seconds is None
    assert _limit({"desktop": [600.0]}, max_minutes=90).seconds == 90 * 60.0
    assert _limit({"desktop": [600.0]}).seconds == 1800.0


def test_this_host_is_judged_as_six_times_slower_than_a_pod():
    """A whole suite spilled to this host must not get its pod-sized limit (#3038).

    0031ffe00f5a was cancelled at 78% after 16 minutes here, where a full suite takes 31.
    """
    pods = {"desktop": [480.0], "kube": [540.0]}
    local = _limit(pods, lane="local")
    assert local.seconds == 3 * 540.0 * 6
    assert "on kube (9 min), scaled 6x for local" in local.basis

    # Quick focused runs on this host do not shrink it below what the pods have seen.
    assert _limit({**pods, "local": [90.0]}, lane="local").seconds == 3 * 540.0 * 6
    # ...and a slow local history counts for itself, at its own size.
    assert _limit({**pods, "local": [1871.0]}, lane="local").seconds == 3 * 540.0 * 6
    assert _limit({"local": [1871.0]}, lane="local").seconds == 3 * 1871.0

    # The reverse scaling keeps a pod's limit honest when only this host has history.
    assert _limit({"local": [1500.0]}, lane="kube").seconds == job_durations.FLOOR_S
    assert _limit({"local": [3600.0]}, lane="kube").seconds == 3 * 600.0


def test_the_ledger_keeps_the_last_passed_runs_once_each(tmp_path):
    ledger = DurationLedger(tmp_path / "durations.json")
    for number in range(job_durations.HISTORY + 5):
        ledger.record("repo local", f"job-{number}", float(number + 1))
    ledger.record("repo local", f"job-{job_durations.HISTORY + 4}", 99.0)
    durations = ledger.durations("repo local")
    assert len(durations) == job_durations.HISTORY
    assert durations[0] == 6.0
    assert durations[-1] == 99.0
    assert ledger.durations("repo kube") == []

    ledger.seed([("repo local", "other", 1000.0)])
    assert 1000.0 not in ledger.durations("repo local")


def test_an_unreadable_ledger_reads_as_no_history(tmp_path):
    path = tmp_path / "durations.json"
    path.write_text("{not json", encoding="utf-8")
    assert DurationLedger(path).durations("repo local") == []


def test_a_row_from_before_time_limits_still_loads():
    job = Job.from_dict(
        {"id": "a", "command": ["true"], "cwd": "/", "log_path": "/x", "submitted_at": 1.0}
    )
    assert job is not None
    assert (job.max_minutes, job.time_limit_s, job.time_limit_basis) == (None, None, "")
    again = Job.from_dict({**job.to_dict(), "max_minutes": 0, "time_limit_s": 60.0})
    assert again is not None and (again.max_minutes, again.time_limit_s) == (0, 60.0)


async def test_a_job_over_its_limit_is_stopped_and_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(job_durations, "UNKNOWN_S", 0.05)
    queue = _queue(tmp_path)
    await queue.start()
    job = queue.submit(command=["sleep", "30"], cwd=str(tmp_path))
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == FAILED
    assert final.exit_code is not None and final.exit_code != 0
    assert "over its time limit" in final.note
    assert "no passed run of" in final.note
    assert "--max-minutes" in final.note
    assert final.time_limit_s == 0.05


async def test_a_tree_that_ignores_sigterm_is_killed_after_the_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(job_durations, "UNKNOWN_S", 0.05)
    queue = _queue(tmp_path)
    queue._kill_grace_s = 0.05
    await queue.start()
    # An ignored signal stays ignored across fork and exec, so the job ignores SIGTERM
    # from its first instruction, with no start-up for the limit to race.
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        job = queue.submit(command=["sleep", "30"], cwd=str(tmp_path))
        final = await _settle(queue, job.id)
    finally:
        signal.signal(signal.SIGTERM, previous)
    await queue.stop()

    assert final.state == FAILED
    assert final.exit_code == -9


async def test_no_limit_lets_a_job_run_past_the_learned_one(tmp_path, monkeypatch):
    monkeypatch.setattr(job_durations, "UNKNOWN_S", 0.02)
    queue = _queue(tmp_path)
    await queue.start()
    job = queue.submit(command=["sleep", "0.1"], cwd=str(tmp_path), max_minutes=0)
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == DONE
    assert final.time_limit_s is None


async def test_a_passed_run_teaches_the_next_job_its_limit(tmp_path):
    queue = _queue(tmp_path)
    await queue.start()
    first = queue.submit(command=[sys.executable, "-c", "pass"], cwd=str(tmp_path))
    assert (await _settle(queue, first.id)).state == DONE
    second = queue.submit(command=[sys.executable, "-c", "pass"], cwd=str(tmp_path))
    final = await _settle(queue, second.id)
    await queue.stop()

    assert "the last 1 passed runs" in final.time_limit_basis
    assert final.time_limit_s == job_durations.FLOOR_S


async def test_a_running_job_s_chat_hears_how_it_goes(tmp_path):
    progress: list[tuple[str, str, int]] = []

    async def notify_progress(job, message, number):
        progress.append((job.id, message, number))
        return True

    queue = _queue(tmp_path)
    queue._notify_progress = notify_progress
    queue._progress_interval_s = 0.04
    await queue.start()
    script = "echo 'collected 3 items'; sleep 0.2"
    job = queue.submit(
        command=["sh", "-c", script], cwd=str(tmp_path), session_key="claude_code:a:b"
    )
    # Beside it, a job with no chat to tell runs through several intervals of its own.
    quiet = queue.submit(command=["sleep", "0.2"], cwd=str(tmp_path))
    final = await _settle(queue, job.id)
    await _settle(queue, quiet.id)
    await queue.stop()

    assert final.state == DONE
    assert len(progress) >= 2
    assert [number for _, _, number in progress] == list(range(1, len(progress) + 1))
    assert {job_id for job_id, _, _ in progress} == {job.id}
    message = progress[0][1]
    assert f"Your queued job {job.id} is still running" in message
    assert "time limit" in message
    assert f"agentdeck job cancel {job.id}" in message
    assert "collected 3 items" in progress[-1][1]


async def test_progress_messages_stop_when_the_job_ends(tmp_path):
    progress: list[int] = []

    async def notify_progress(job, message, number):
        progress.append(number)
        return True

    queue = _queue(tmp_path)
    queue._notify_progress = notify_progress
    queue._progress_interval_s = 0.05
    await queue.start()
    job = queue.submit(command=["true"], cwd=str(tmp_path), session_key="claude_code:a:b")
    await _settle(queue, job.id)
    await asyncio.sleep(0.15)
    await queue.stop()
    assert progress == []


def test_a_quiet_pytest_gets_a_hint_unless_its_suite_records_timings(tmp_path):
    plain = str(tmp_path)
    assert quiet_output_hint(plain, ["pytest", "-q"]) is not None
    assert quiet_output_hint(plain, ["uv", "run", "pytest", "-qx", "tests"]) is not None
    assert quiet_output_hint(plain, [".venv/bin/python", "-m", "pytest", "--quiet"]) is not None
    assert quiet_output_hint(plain, ["pytest", "-q", "-v"]) is None
    assert quiet_output_hint(plain, ["pytest", "-q", "-rA"]) is None
    assert quiet_output_hint(plain, ["pytest"]) is None
    assert quiet_output_hint(plain, ["make", "-q"]) is None

    agentdeck = tmp_path / "agentdeck"
    (agentdeck / "scripts").mkdir(parents=True)
    (agentdeck / "scripts" / "kube_pytest.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    assert quiet_output_hint(str(agentdeck), ["pytest", "-q"]) is None


def test_job_submit_passes_max_minutes_and_hints_at_quiet_output(tmp_path, monkeypatch, capsys):
    calls: list[dict] = []

    def submit_job(_socket, **kwargs):
        calls.append(kwargs)
        return {"id": "job-1", "log_path": str(tmp_path / "job-1.log"), "state": "queued"}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    checkout = tmp_path / "project"
    checkout.mkdir()

    cli._job(["submit", "--cwd", str(checkout), "--max-minutes", "90", "--", "pytest", "-q"])
    assert calls[-1]["max_minutes"] == 90
    assert "hint:" in capsys.readouterr().out

    cli._job(["submit", "--cwd", str(checkout), "--", "pytest", "-v"])
    assert calls[-1]["max_minutes"] is None
    assert "hint:" not in capsys.readouterr().out


def test_job_submit_refuses_a_negative_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "submit_job", lambda _socket, **kwargs: {})
    try:
        cli._job(["submit", "--cwd", str(tmp_path), "--max-minutes", "-1", "--", "true"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - the parser must refuse
        raise AssertionError("a negative --max-minutes was accepted")
