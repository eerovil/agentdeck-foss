"""The host-wide heavy-command queue (#1287)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import conftest as harness
import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import jobs as jobs_module
from agentdeck.config import AppConfig, HistoryConfig, JobsConfig
from agentdeck.host_slots import (
    DEFAULT_KUBE_SLOTS,
    DEFAULT_MAC_SLOTS,
    HEAVY_SLOT_ENV,
    HEAVY_SLOT_HELD_ENV,
    KUBE_SLOT_ENV,
    MAC_SLOT_ENV,
    SLOT_DIR_ENV,
    MemoryGate,
    default_heavy_slots,
    desktop_slot_paths,
    heavy_slot_count,
    heavy_slot_is_held,
    hold_slot,
    kube_slot_count,
    kube_slot_paths,
    local_suite_workers,
    mac_slot_count,
    mac_slot_paths,
    slot_paths,
    try_take_slot,
)
from agentdeck.jobs import (
    CANCELLED,
    DESKTOP,
    DONE,
    FAILED,
    FINISHED_RETENTION_S,
    INTERRUPTED,
    KUBE,
    KUBE_NO_ROOM_EXIT,
    KUBE_PROFILE_ENV,
    LOCAL,
    MAC,
    NOTIFY_GIVE_UP_S,
    NOTIFY_HOST_BUSY_GIVE_UP_S,
    QUEUED,
    QUEUED_TTL_S,
    RUNNING,
    SPILL_PAUSE_S,
    UNREACHABLE_SKIP_S,
    VERIFICATION,
    Job,
    JobError,
    JobQueue,
    NotifyOutcome,
    _Running,
    default_lane,
    job_message,
    lanes_down,
    moved_note,
    spill_back_note,
    spilled_note,
)
from agentdeck.models import InjectResult
from agentdeck.poller_engine import baseline, flaky
from agentdeck.providers.delivery import DeliverResult, SessionDeliveryFence
from agentdeck.runtime import create_runtime_app
from agentdeck.slot_leases import SlotLeases
from agentdeck.verification_baselines import DEFAULT_FILENAME as BASELINE_FILENAME
from agentdeck.verification_baselines import BaselineLedger

SLOW = "import time; time.sleep(30)"


def _queue(
    tmp_path: Path,
    *,
    slots: int = 2,
    kube_slots: int = 2,
    mac_slots: int = 2,
    desktop_slots: int = 1,
    notify=None,
    give_up_s: float = NOTIFY_GIVE_UP_S,
    host_busy_give_up_s: float = NOTIFY_HOST_BUSY_GIVE_UP_S,
    clock=time.time,
    survive_restart: bool = False,
    memory_gate=None,
) -> JobQueue:
    """A queue on its own slot files, so it never contends with the run itself."""
    pool = tmp_path / "slots"
    pool.mkdir(exist_ok=True)
    kube_pool = tmp_path / "kube-slots"
    kube_pool.mkdir(exist_ok=True)
    mac_pool = tmp_path / "mac-slots"
    mac_pool.mkdir(exist_ok=True)
    desktop_pool = tmp_path / "desktop-slots"
    desktop_pool.mkdir(exist_ok=True)
    return JobQueue(
        state_dir=tmp_path / "jobs",
        notify=notify,
        slot_paths=[pool / f"slot-{index}" for index in range(slots)],
        kube_slot_paths=[kube_pool / f"slot-{index}" for index in range(kube_slots)],
        mac_slot_paths=[mac_pool / f"slot-{index}" for index in range(mac_slots)],
        desktop_slot_paths=[desktop_pool / f"slot-{index}" for index in range(desktop_slots)],
        poll_s=0.02,
        notify_retry_s=0.02,
        notify_give_up_s=give_up_s,
        notify_host_busy_give_up_s=host_busy_give_up_s,
        clock=clock,
        survive_restart=survive_restart,
        memory_gate=memory_gate,
    )


class _Clock:
    """A clock the test moves by hand, so hours pass in milliseconds."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


async def _wait_for(predicate, timeout: float = 20.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _settle(queue: JobQueue, job_id: str) -> Job:
    def finished() -> Job | None:
        job = queue.get(job_id)
        return job if job is not None and job.is_final else None

    return await _wait_for(finished, what=f"job {job_id} to finish")


async def test_a_finished_job_tells_the_chat_that_submitted_it(tmp_path):
    told: list[tuple[Job, str]] = []

    async def notify(job: Job, message: str) -> bool:
        told.append((job, message))
        return True

    queue = _queue(tmp_path, notify=notify)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('hello from the job')"],
        cwd=str(tmp_path),
        session_key="claude_code:main:sid",
    )
    await _wait_for(lambda: told, what="the completion message")
    await queue.stop()

    final = queue.get(job.id)
    assert final.state == DONE
    assert final.exit_code == 0
    assert "hello from the job" in Path(final.log_path).read_text()
    assert final.result_class == "success"
    assert final.result_id and final.output_sha256
    assert final.output_bytes == len(b"hello from the job\n")
    assert final.diagnostic_excerpt == "hello from the job"
    # The message carries the outcome and enough output to act on without
    # opening the log file.
    _, message = told[0]
    assert job.id in message
    assert "hello from the job" in message
    assert final.log_path in message


async def test_a_rejected_completion_stays_pending_until_a_later_retry(tmp_path):
    attempts = 0

    async def notify(job: Job, message: str) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 1

    queue = _queue(tmp_path, notify=notify)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('done')"],
        cwd=str(tmp_path),
        session_key="claude_code:main:sid",
    )
    await _wait_for(
        lambda: queue.get(job.id).notified,
        what="the completion retry to succeed",
    )
    delivered_attempts = attempts
    await asyncio.sleep(0.1)
    await queue.stop()

    assert delivered_attempts == 2
    assert attempts == delivered_attempts


async def test_a_completion_that_can_never_be_delivered_stops_retrying(tmp_path):
    """A refusal does not say whether it is permanent (#1299).

    `no worker record for this session` never becomes deliverable, but it looks
    exactly like `that worker is briefly unavailable` — both are `accepted=False`.
    Retrying forever would be free were it not for `_prune` deliberately keeping
    un-notified rows, which together mean one released worker leaves a row and a
    log line per minute for as long as the runtime runs.
    """
    attempts = 0

    async def never(job: Job, message: str) -> bool:
        nonlocal attempts
        attempts += 1
        return False

    # Give up immediately: the job is already older than a zero-second deadline.
    queue = _queue(tmp_path, notify=never, give_up_s=0.0)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('done')"],
        cwd=str(tmp_path),
        session_key="claude_code:main:released-worker",
    )
    await _wait_for(lambda: queue.get(job.id).notified, what="the queue to give up")
    settled = attempts
    await asyncio.sleep(0.2)
    await queue.stop()

    # Done trying, and it says why on the row rather than only in a log line.
    assert attempts == settled == 1
    assert "not delivered" in queue.get(job.id).note


def test_a_completion_the_queue_gave_up_on_is_pruned_like_any_other(tmp_path):
    """Giving up has to restore ordinary retention, or the leak is only slower."""
    old = time.time() - 2 * FINISHED_RETENTION_S
    _write_jobs(
        tmp_path,
        [
            _row(id="gave-up", state=DONE, finished_at=old, notified=True,
                 note="its chat was never reachable, so this completion was not delivered"),
            _row(id="still-trying", state=DONE, finished_at=old, notified=False),
        ],
    )
    queue = _queue(tmp_path)

    queue.load()

    assert queue.get("gave-up") is None
    assert queue.get("still-trying") is not None


async def test_a_transient_refusal_is_still_retried_to_success(tmp_path):
    """The deadline must not break the case the retry exists for.

    A worker evicted between the job finishing and the message being written is
    back on the next tick, and that completion should still arrive.
    """
    attempts = 0

    async def unavailable_once(job: Job, message: str) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts > 2

    queue = _queue(tmp_path, notify=unavailable_once, give_up_s=3600.0)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('done')"],
        cwd=str(tmp_path),
        session_key="claude_code:main:evicted",
    )
    await _wait_for(lambda: queue.get(job.id).notified, what="the retry to succeed")
    await queue.stop()

    assert attempts == 3
    assert "not delivered" not in queue.get(job.id).note


async def test_a_full_worker_pool_does_not_spend_the_give_up_deadline(tmp_path):
    """A busy host is not an unreachable chat (#1610).

    Waking an idle chat means starting a worker for it, and starting one needs a
    free slot, so a full pool refuses `at_capacity` however reachable the chat
    is. Counting that against the deadline throws away a finished suite's result
    and leaves the chat waiting for a message that will never come — which is
    exactly what a `pytest -q` run did while every slot was taken.
    """
    attempts = 0

    async def at_capacity_then_room(job: Job, message: str) -> NotifyOutcome:
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            return NotifyOutcome(True)
        return NotifyOutcome(False, "at_capacity")

    # A deadline of zero: any refusal about the chat itself gives up at once.
    queue = _queue(tmp_path, notify=at_capacity_then_room, give_up_s=0.0)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('done')"],
        cwd=str(tmp_path),
        session_key="claude_code:alt:waiting-behind-a-full-pool",
    )
    await _wait_for(
        lambda: queue.get(job.id).notified, what="the pool to free a slot"
    )
    await queue.stop()

    assert attempts == 3
    assert "not delivered" not in queue.get(job.id).note


def test_a_long_busy_spell_does_not_spend_the_chat_deadline(tmp_path):
    """The two deadlines must not share one clock.

    Both were once compared against the job's own age, so five hours of
    `at_capacity` left the *first* genuinely chat-scoped refusal already past
    `NOTIFY_GIVE_UP_S`: the queue gave up on the spot, having never once waited
    for the chat. That hands the busy host the very budget the split exists to
    protect.

    Driven a refusal at a time rather than through the dispatch loop, because
    the point is five hours of wall clock and what the queue makes of them.
    """
    clock = _Clock()
    _write_jobs(
        tmp_path,
        [_row(id="waited-out", state=DONE, finished_at=clock.now, notified=False)],
    )
    queue = _queue(tmp_path, give_up_s=3600.0, clock=clock)
    queue.load()

    for _ in range(5):  # five hours of a full worker pool
        queue._defer_or_give_up("waited-out", "at_capacity", host_busy=True)
        clock.now += 3600.0
    assert not queue.get("waited-out").notified

    # The first refusal that is actually about the chat must still defer: the
    # chat has not had its hour yet, however old the job is by now.
    queue._defer_or_give_up("waited-out", "no worker record", host_busy=False)
    assert not queue.get("waited-out").notified

    # ...and it gives up once that hour — its own, not the job's — has passed.
    clock.now += 3601.0
    queue._defer_or_give_up("waited-out", "no worker record", host_busy=False)
    assert queue.get("waited-out").notified
    assert "its chat was never reachable" in queue.get("waited-out").note


def test_pausing_the_chat_stopwatch_banks_the_time_it_was_running(tmp_path):
    """A pause keeps the time already on the clock; it does not reset it.

    Clearing the running timestamp without first adding the interval it had been
    running threw away real chat-refusal time — half an hour of being told the
    chat was gone, wiped by the next `at_capacity`. A host that got busy every so
    often would hand such a chat an unspendable budget, which is the same leak
    from the other side.
    """
    clock = _Clock()
    _write_jobs(
        tmp_path,
        [_row(id="half-spent", state=DONE, finished_at=clock.now, notified=False)],
    )
    queue = _queue(tmp_path, give_up_s=3600.0, clock=clock)
    queue.load()

    queue._defer_or_give_up("half-spent", "no worker record", host_busy=False)
    clock.now += 1800.0  # half an hour of being refused about the chat...
    queue._defer_or_give_up("half-spent", "at_capacity", host_busy=True)  # ...paused
    assert queue.get("half-spent").chat_refused_for == pytest.approx(1800.0, abs=1.0)

    # That half hour is still on the clock, so only the other half is owed.
    clock.now += 7200.0  # a long busy spell, which counts for nothing
    queue._defer_or_give_up("half-spent", "no worker record", host_busy=False)
    assert not queue.get("half-spent").notified
    clock.now += 1800.0
    queue._defer_or_give_up("half-spent", "no worker record", host_busy=False)
    assert queue.get("half-spent").notified
    assert "its chat was never reachable" in queue.get("half-spent").note


def test_a_busy_spell_in_the_middle_does_not_spend_the_chat_deadline(tmp_path):
    """Pausing matters as much as starting late.

    A fixed start still let the host run the chat's clock: one chat-scoped
    refusal opened the budget, the pool then said `at_capacity` for an hour, and
    the next chat-scoped refusal was already past the deadline — on the strength
    of an hour that carried not one word about whether the chat was reachable.
    """
    clock = _Clock()
    _write_jobs(
        tmp_path,
        [_row(id="one-strike", state=DONE, finished_at=clock.now, notified=False)],
    )
    queue = _queue(tmp_path, give_up_s=3600.0, clock=clock)
    queue.load()

    # Attempts a minute apart, as the retry loop actually makes them.
    queue._defer_or_give_up("one-strike", "no worker record", host_busy=False)
    for _ in range(120):  # two hours in which the host said nothing about the chat
        clock.now += 60.0
        queue._defer_or_give_up("one-strike", "at_capacity", host_busy=True)
    clock.now += 60.0
    queue._defer_or_give_up("one-strike", "no worker record", host_busy=False)
    assert not queue.get("one-strike").notified
    # Only the one minute the clock was actually running is on it.
    assert queue.get("one-strike").chat_refused_for == pytest.approx(60.0, abs=1.0)

    # The rest of the hour still has to be spent being refused about the chat.
    clock.now += 3600.0
    queue._defer_or_give_up("one-strike", "no worker record", host_busy=False)
    assert queue.get("one-strike").notified
    assert "its chat was never reachable" in queue.get("one-strike").note


def test_the_chat_deadline_is_not_reset_by_a_restart(tmp_path):
    """A restart must not hand an unreachable chat a fresh hour.

    Held in memory, the deadline died with the process while the row itself did
    not, so every restart started the budget again — and this host redeploys
    through the day. A chat that was gone for good would keep its row and its
    log line per minute for as long as the deck kept restarting, which is #1299's
    leak coming back through the door #1610 opened.

    The row below is written without the field at all, which is also what an
    older `jobs.json` looks like.
    """
    clock = _Clock()
    _write_jobs(
        tmp_path,
        [_row(id="gone-for-good", state=DONE, finished_at=clock.now, notified=False)],
    )
    queue = _queue(tmp_path, give_up_s=3600.0, clock=clock)
    queue.load()
    queue._defer_or_give_up("gone-for-good", "no worker record", host_busy=False)
    assert not queue.get("gone-for-good").notified

    # Half an hour in, the runtime restarts and a fresh queue reads the file.
    clock.now += 1800.0
    revived = _queue(tmp_path, give_up_s=3600.0, clock=clock)
    revived.load()
    revived._defer_or_give_up("gone-for-good", "no worker record", host_busy=False)
    assert not revived.get("gone-for-good").notified

    # The deadline is the original one, so it falls due half an hour later —
    # not a whole hour after the restart.
    clock.now += 1801.0
    revived._defer_or_give_up("gone-for-good", "no worker record", host_busy=False)
    assert revived.get("gone-for-good").notified
    assert "its chat was never reachable" in revived.get("gone-for-good").note


async def test_a_pool_that_never_frees_a_slot_still_stops_eventually(tmp_path):
    """The longer ceiling is a backstop, not a licence to retry forever.

    Without one, a refusal that happens to be spelled `at_capacity` would leave
    a row that `_prune` keeps and the dispatch loop retries for the life of the
    runtime — the leak #1299 closed.
    """
    attempts = 0

    async def always_at_capacity(job: Job, message: str) -> NotifyOutcome:
        nonlocal attempts
        attempts += 1
        return NotifyOutcome(False, "at_capacity")

    queue = _queue(
        tmp_path,
        notify=always_at_capacity,
        give_up_s=3600.0,
        host_busy_give_up_s=0.0,
    )
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "print('done')"],
        cwd=str(tmp_path),
        session_key="claude_code:alt:never-any-room",
    )
    await _wait_for(lambda: queue.get(job.id).notified, what="the queue to give up")
    settled = attempts
    await asyncio.sleep(0.2)
    await queue.stop()

    assert attempts == settled == 1
    # And it says which of the two happened, so nobody reads a busy deck as a
    # chat that no longer exists.
    assert "the host never had room to wake its chat" in queue.get(job.id).note


async def test_a_failing_command_is_failed_and_keeps_its_exit_code(tmp_path):
    queue = _queue(tmp_path)
    await queue.start()
    job = queue.submit(
        command=[sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        cwd=str(tmp_path),
    )
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == FAILED
    assert final.exit_code == 3
    # stderr is merged into the one log, so a failure is readable from it alone.
    assert "boom" in Path(final.log_path).read_text()


async def test_a_job_never_acts_as_the_chat_that_submitted_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "claude_code:main:sid")
    monkeypatch.setenv("AGENTDECK_URL", "http://127.0.0.1:8756")
    queue = _queue(tmp_path)
    await queue.start()
    job = queue.submit(
        command=[
            sys.executable,
            "-c",
            "import os; print(os.environ.get('AGENTDECK_SESSION_KEY'), "
            "os.environ.get('AGENTDECK_URL'), os.environ.get('AGENTDECK_HEAVY_SLOT_HELD'), "
            "os.environ.get('AGENTDECK_QUEUED_JOB'))",
        ],
        cwd=str(tmp_path),
        session_key="claude_code:main:sid",
    )
    final = await _settle(queue, job.id)
    await queue.stop()

    # No inherited identity, and the marker that stops the suite inside this job
    # from queueing behind the slot the job already holds. The queued-job marker
    # keeps xdist's workers for a suite the queue started (#2666).
    assert Path(final.log_path).read_text().strip() == "None None 1 1"


# ---- the cap ---------------------------------------------------------------


async def test_only_as_many_jobs_run_at_once_as_there_are_slots(tmp_path):
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    first = queue.submit(command=[sys.executable, "-c", SLOW], cwd=str(tmp_path))
    second = queue.submit(command=[sys.executable, "-c", "print('second')"], cwd=str(tmp_path))
    await _wait_for(lambda: queue.get(first.id).state == RUNNING, what="the first job to start")
    await asyncio.sleep(0.2)

    assert queue.get(second.id).state == QUEUED

    await queue.cancel(first.id)
    final = await _settle(queue, second.id)
    await queue.stop()
    assert final.state == DONE


async def test_a_slot_held_outside_the_queue_still_holds_the_queue_back(tmp_path):
    """The cap counts suites the queue never started.

    This is the whole reason the slot is an flock and not a counter: a direct
    `pytest` in another worktree takes one through the conftest backstop, and the
    queue has to see it.
    """
    queue = _queue(tmp_path, slots=1)
    outsider = try_take_slot(queue._slots())
    assert outsider is not None
    await queue.start()
    job = queue.submit(command=[sys.executable, "-c", "print('mine')"], cwd=str(tmp_path))
    await asyncio.sleep(0.3)

    assert queue.get(job.id).state == QUEUED

    os.close(outsider)
    final = await _settle(queue, job.id)
    await queue.stop()
    assert final.state == DONE


# ---- the workspace's own interpreter (#1635) --------------------------------


def _checkout(tmp_path: Path, name: str, *, venv: bool = True) -> Path:
    """A checkout, with or without the virtualenv a local pytest job needs."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    if venv:
        interpreter = root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_text("#!/bin/sh\nexec /usr/bin/env python3 \"$@\"\n", encoding="utf-8")
        interpreter.chmod(0o755)
    return root


def test_a_local_pytest_job_runs_the_workspace_interpreter(tmp_path):
    """The bug: a bare `pytest` came off the runtime's PATH, not the workspace's.

    Against a worktree the runtime does not hold, that is a one-second import
    error on the conftest — an exit code the poller reads as an infrastructure
    fault and parks the card on, having learned nothing about the branch.
    """
    root = _checkout(tmp_path, "checkout")
    queue = _queue(tmp_path)

    job = queue.submit(command=["pytest", "-q"], cwd=str(root), lane=LOCAL)

    # The stored command still reads as the suite somebody asked for.
    assert job.command == ["pytest", "-q"]
    assert jobs_module._argv(job) == [
        str(root / ".venv" / "bin" / "python"), "-m", "pytest", "-q",
    ]


def test_a_local_pytest_job_takes_the_venv_of_its_own_checkout_only(tmp_path):
    """A worktree without one does not borrow the checkout it sits inside.

    `.worktrees/issue-1635` lives inside `~/agentdeck`, which has a virtualenv of
    its own. Running through that one imports the *other* checkout's `agentdeck`
    package, so the suite passes on code nobody wrote — worse than the import
    error it papers over.
    """
    outer = _checkout(tmp_path, "outer")
    inner = _checkout(tmp_path / "outer" / ".worktrees", "issue-1", venv=False)
    queue = _queue(tmp_path)

    with pytest.raises(JobError, match="no virtualenv"):
        queue.submit(command=["pytest", "-q"], cwd=str(inner), lane=LOCAL)

    assert jobs_module.workspace_python(str(outer)) == outer / ".venv" / "bin" / "python"
    assert jobs_module.workspace_python(str(inner)) is None


def test_only_a_bare_local_pytest_is_rewritten(tmp_path):
    """Everything else is left exactly as submitted.

    `uv run` resolves the project's environment itself, an explicit path is a
    submitter who already chose one, and a command that is not pytest at all is
    none of this rule's business — including in a workspace with no virtualenv.
    """
    root = _checkout(tmp_path, "bare", venv=False)
    queue = _queue(tmp_path)

    for command in (
        ["uv", "run", "pytest", "-q"],
        [".venv/bin/pytest", "-q"],
        ["make", "build"],
    ):
        job = queue.submit(command=command, cwd=str(root), lane=LOCAL)
        assert jobs_module._argv(job) == command


# ---- the remote lane (#1368) -----------------------------------------------


def _fake_worktree(tmp_path: Path) -> Path:
    """A directory that looks enough like a worktree to submit a kube job from.

    The runner stands in for ``scripts/kube_pytest.sh``: the queue's side of the
    remote lane is which pool it counts against and what it execs, and neither
    needs a cluster to test.
    """
    worktree = tmp_path / "worktree"
    (worktree / "scripts").mkdir(parents=True, exist_ok=True)
    runner = worktree / "scripts" / "kube_pytest.sh"
    runner.write_text('#!/bin/bash\necho "ran remotely: $*"\n', encoding="utf-8")
    runner.chmod(0o755)
    return worktree


def _fake_trailhead(tmp_path: Path) -> Path:
    """A directory that looks enough like the trailhead superproject.

    Recognised by the runner it ships, exactly as the real one is, with the two
    nested checkouts a job can be submitted from.
    """
    trailhead = tmp_path / "trailhead"
    (trailhead / "helm" / "app" / "jobs" / "test").mkdir(parents=True, exist_ok=True)
    runner = trailhead / "helm" / "app" / "jobs" / "test" / "run.sh"
    runner.write_text('#!/bin/bash\necho "trailhead: $*"\n', encoding="utf-8")
    runner.chmod(0o755)
    (trailhead / "web").mkdir(exist_ok=True)
    (trailhead / "abacus" / "local_apps").mkdir(parents=True, exist_ok=True)
    return trailhead


async def test_a_remote_job_runs_while_the_local_slot_is_busy(tmp_path):
    """The point of the lane: the cluster does not wait for this host.

    One heavy slot, already taken by a long local job. A `pytest` submitted to
    the kube lane behind it still runs, because it costs this host a kubectl
    rather than four pytest workers.
    """
    worktree = _fake_worktree(tmp_path)
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    local = queue.submit(command=[sys.executable, "-c", SLOW], cwd=str(tmp_path))
    await _wait_for(lambda: queue.get(local.id).state == RUNNING, what="the local job to start")
    remote = queue.submit(
        command=["pytest", "-q", "tests/test_web.py"], cwd=str(worktree), lane=KUBE
    )

    final = await _settle(queue, remote.id)
    assert queue.get(local.id).state == RUNNING

    await queue.cancel(local.id)
    await queue.stop()

    assert final.state == DONE
    # The runner is handed the pytest arguments, not the word `pytest`.
    assert Path(final.log_path).read_text().strip() == "ran remotely: -q tests/test_web.py"


async def test_remote_jobs_queue_against_their_own_pool(tmp_path):
    worktree = _fake_worktree(tmp_path)
    slow_runner = worktree / "scripts" / "kube_pytest.sh"
    slow_runner.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
    queue = _queue(tmp_path, kube_slots=1)
    await queue.start()
    first = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE)
    second = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE)
    await _wait_for(lambda: queue.get(first.id).state == RUNNING, what="the first pod to start")
    await asyncio.sleep(0.2)

    assert queue.get(second.id).state == QUEUED

    await queue.cancel(first.id)
    await queue.cancel(second.id)
    await queue.stop()


def test_the_remote_lane_refuses_what_it_cannot_honour(tmp_path):
    """A refusal the submitter can read, rather than a surprise ten minutes on.

    The pod is a pytest runner. A build or a shell pipeline sent to it has to be
    turned down at submission, and so does a directory with no runner in it.
    """
    worktree = _fake_worktree(tmp_path)
    queue = _queue(tmp_path)

    with pytest.raises(JobError, match="pytest only"):
        queue.submit(command=["make", "build"], cwd=str(worktree), lane=KUBE)
    with pytest.raises(JobError, match="agentdeck checkout and the projects"):
        queue.submit(command=["pytest", "-q"], cwd=str(tmp_path), lane=KUBE)
    with pytest.raises(JobError, match="unknown lane"):
        queue.submit(command=["pytest"], cwd=str(worktree), lane="mars")

    # The forms an agent actually types for a suite are all accepted.
    for command in (["pytest", "-q"], ["uv", "run", "pytest", "-q"], [".venv/bin/pytest", "-q"]):
        assert queue.submit(command=command, cwd=str(worktree), lane=KUBE).lane == KUBE


def test_a_submission_that_names_no_lane_goes_to_a_pod_when_one_can_run_it(tmp_path, monkeypatch):
    """The host is the fallback, not the default.

    A suite the remote lane can honour belongs off this host: the pod costs one
    `kubectl` where a local run costs four pytest workers and the single heavy
    slot. Everything the pod cannot run comes back here quietly, because naming
    no lane is a preference rather than an instruction — unlike an explicit
    `--where kube`, which still refuses out loud (the test above).
    """
    worktree = _fake_worktree(tmp_path)
    trailhead = _fake_trailhead(tmp_path)

    # The agentdeck checkout goes to desktop first (#2297); trailhead's suites
    # cannot run there, so they keep the dev cluster.
    assert default_lane(str(worktree), ["pytest", "-q"]) == DESKTOP
    assert default_lane(str(worktree / "src"), ["uv", "run", "pytest"]) == DESKTOP
    assert default_lane(str(trailhead / "web"), ["pytest"]) == KUBE

    # A build, a checkout the lane does not serve, and an trailhead root too
    # ambiguous to name a suite: all of them run here rather than being refused.
    assert default_lane(str(worktree), ["make", "build"]) == LOCAL
    assert default_lane(str(tmp_path), ["pytest", "-q"]) == LOCAL
    assert default_lane(str(trailhead), ["pytest", "-q"]) == LOCAL

    # A host with ten threads or more runs the agentdeck checkout's suites itself first
    # (#3614); trailhead's still take the dev cluster.
    monkeypatch.setenv(jobs_module.LOCAL_FIRST_MIN_CORES_ENV, "10")
    monkeypatch.setattr(jobs_module, "usable_cores", lambda: 12)
    assert default_lane(str(worktree), ["uv", "run", "pytest"]) == LOCAL
    assert default_lane(str(trailhead / "web"), ["pytest"]) == KUBE
    # ... unless the owner marked this host down, which sends them to the pods again.
    assert default_lane(str(worktree), ["uv", "run", "pytest"], down=frozenset({LOCAL})) == (
        DESKTOP
    )
    monkeypatch.setattr(jobs_module, "usable_cores", lambda: 8)
    assert default_lane(str(worktree), ["uv", "run", "pytest"]) == DESKTOP


def test_a_lane_marked_down_is_skipped_for_the_next_one(tmp_path, monkeypatch):
    """#2826 skipped a lane marked down; #2835 sends an agentdeck suite whose
    desktop is away to the dev cluster next, because this host is slow at a whole
    suite. It runs here only when the dev cluster is marked down too."""
    worktree = _fake_worktree(tmp_path)
    trailhead = _fake_trailhead(tmp_path)

    assert default_lane(str(worktree), ["pytest", "-q"], down=frozenset({DESKTOP})) == KUBE
    assert default_lane(str(worktree), ["pytest", "-q"], down=frozenset({KUBE})) == DESKTOP
    both = frozenset({DESKTOP, KUBE})
    assert default_lane(str(worktree), ["pytest", "-q"], down=both) == LOCAL
    assert default_lane(str(trailhead / "web"), ["pytest"], down=frozenset({DESKTOP})) == KUBE
    assert default_lane(str(trailhead / "web"), ["pytest"], down=frozenset({KUBE})) == LOCAL
    # Read from the file when the caller does not say.
    marked = tmp_path / "job-lanes-down"
    marked.write_text("desktop\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES_DOWN", str(marked))
    assert default_lane(str(worktree), ["pytest", "-q"]) == KUBE
    marked.write_text("desktop\nkube\n", encoding="utf-8")
    assert default_lane(str(worktree), ["pytest", "-q"]) == LOCAL


def test_the_lanes_down_file_lapses_on_its_last_day(tmp_path):
    marked = tmp_path / "job-lanes-down"
    marked.write_text(
        "# away for a few days\n"
        "desktop 2026-10-15  # back at the weekend\n"
        "MAC\n"
        "local\n"
        "mars 2099-01-01\n"
        "kube someday\n",
        encoding="utf-8",
    )

    # This host may be marked down too since #3614; an unknown lane is still ignored.
    assert lanes_down(marked, today="2026-10-15") == {DESKTOP, MAC, LOCAL, KUBE}
    assert lanes_down(marked, today="2026-10-16") == {MAC, LOCAL, KUBE}
    assert lanes_down(tmp_path / "absent", today="2026-10-15") == frozenset()
    # The conftest points the default at a file that is not there.
    assert lanes_down() == frozenset()
    # A lane's older name marks it down too (#3623).
    marked.write_text("old-desktop\n", encoding="utf-8")
    assert lanes_down(marked) == {DESKTOP}


def test_a_host_without_lane_settings_runs_everything_here(tmp_path, monkeypatch):
    """No remote lane is set up, so each counts as down and naming one is refused (#3623)."""
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setenv(jobs_module.LANES_FILE_ENV, str(tmp_path / "absent.toml"))

    assert lanes_down(tmp_path / "absent") == {KUBE, MAC, DESKTOP}
    assert default_lane(str(worktree), ["pytest", "-q"]) == LOCAL
    with pytest.raises(JobError, match="the kube lane is not set up on this host"):
        _queue(tmp_path).submit(command=["pytest"], cwd=str(worktree), lane=KUBE)


def test_the_desktop_pool_keeps_its_older_name_while_the_lane_has_one(tmp_path, monkeypatch):
    """A job started before the rename holds a lock in the pool under its old name, so the
    pool keeps that name and a second job cannot start beside it (#3623)."""
    monkeypatch.setenv(SLOT_DIR_ENV, str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    queue = JobQueue(state_dir=tmp_path / "jobs")
    assert queue._slots(DESKTOP) == desktop_slot_paths(name="old-desktop")
    assert queue._slots(DESKTOP)[0].parent.name.startswith("agentdeck-old-desktop-slots")

    plain = tmp_path / "job-lanes.toml"
    plain.write_text("[lanes.desktop]\n", encoding="utf-8")
    monkeypatch.setenv(jobs_module.LANES_FILE_ENV, str(plain))
    assert queue._slots(DESKTOP) == desktop_slot_paths(name="desktop")


def test_a_job_from_deeper_in_the_checkout_still_finds_its_runner(tmp_path):
    """Suites are submitted from wherever the agent happens to be standing."""
    worktree = _fake_worktree(tmp_path)
    inside = worktree / "src" / "agentdeck"
    inside.mkdir(parents=True)

    assert _queue(tmp_path).submit(
        command=["pytest", "-q"], cwd=str(inside), lane=KUBE
    ).lane == KUBE


async def test_an_trailhead_job_runs_trailhead_s_own_runner(tmp_path):
    """Trailhead already knows how to run its suites on that cluster (#1383).

    Its runner carries the image, the sidecar services and the settings module
    each suite needs, so the lane calls it rather than growing a second one — and
    which suite this is comes from the nested checkout the job was submitted in,
    the same answer `web test --kube` gets from the shell you typed it in.
    """
    trailhead = _fake_trailhead(tmp_path)
    queue = _queue(tmp_path)
    await queue.start()

    store = queue.submit(command=["pytest", "-q", "app"], cwd=str(trailhead / "web"), lane=KUBE)
    settled = await _settle(queue, store.id)
    abacus = queue.submit(
        command=["pytest"], cwd=str(trailhead / "abacus" / "local_apps"), lane=KUBE
    )
    abacus_settled = await _settle(queue, abacus.id)
    await queue.stop()

    assert settled.state == DONE
    assert Path(settled.log_path).read_text().strip() == "trailhead: store -q app"
    assert Path(abacus_settled.log_path).read_text().strip() == "trailhead: abacus"


def test_an_trailhead_job_from_the_root_says_which_suite_it_needs(tmp_path):
    """The root holds several suites, and guessing one would run the wrong tests."""
    trailhead = _fake_trailhead(tmp_path)

    with pytest.raises(JobError, match="submit this from the one you mean"):
        _queue(tmp_path).submit(command=["pytest"], cwd=str(trailhead), lane=KUBE)


# ---- the Mac's own cluster (#1597) -----------------------------------------


async def test_a_mac_job_asks_the_runner_for_the_mac_cluster(tmp_path):
    """One runner, two clusters, and the lane is what says which.

    The script defaults to trailhead's cluster, so a mac job that failed to name
    its profile would run on the dev cluster while counting against the mac
    pool — right answer, wrong machine, and nothing in the log to show it.
    """
    worktree = _fake_worktree(tmp_path)
    runner = worktree / "scripts" / "kube_pytest.sh"
    runner.write_text(
        f'#!/bin/bash\necho "profile=${{{KUBE_PROFILE_ENV}:-unset}} args=$*"\n', encoding="utf-8"
    )
    runner.chmod(0o755)
    queue = _queue(tmp_path)
    await queue.start()

    on_mac = await _settle(
        queue, queue.submit(command=["pytest", "-q"], cwd=str(worktree), lane=MAC).id
    )
    on_trailhead = await _settle(
        queue, queue.submit(command=["pytest", "-q"], cwd=str(worktree), lane=KUBE).id
    )
    await queue.stop()

    assert Path(on_mac.log_path).read_text().strip() == "profile=mac args=-q"
    assert Path(on_trailhead.log_path).read_text().strip() == "profile=kube args=-q"
    assert "Mac's Kubernetes" in job_message(on_mac)


def test_the_mac_lane_runs_the_agentdeck_checkout_only(tmp_path):
    """Trailhead's runner is wired to trailhead's own cluster.

    Sent to the mac lane it would not run on the Mac at all: it would run on the
    dev cluster while holding a mac slot, so the refusal has to be explicit.
    """
    trailhead = _fake_trailhead(tmp_path)
    queue = _queue(tmp_path)

    with pytest.raises(JobError, match="mac lane does not run the trailhead checkout"):
        queue.submit(command=["pytest"], cwd=str(trailhead / "web"), lane=MAC)

    # ...and the agentdeck checkout is accepted on both remote lanes.
    worktree = _fake_worktree(tmp_path)
    assert queue.submit(command=["pytest"], cwd=str(worktree), lane=MAC).lane == MAC


async def test_the_two_clusters_do_not_share_a_queue(tmp_path):
    """A full dev cluster must not leave the Mac idle, which is why the pools
    are separate rather than one remote pool of six."""
    worktree = _fake_worktree(tmp_path)
    (worktree / "scripts" / "kube_pytest.sh").write_text(
        "#!/bin/bash\nsleep 30\n", encoding="utf-8"
    )
    queue = _queue(tmp_path, kube_slots=1)
    await queue.start()
    busy = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE)
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="the kube pod to start")
    behind = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE)
    elsewhere = queue.submit(command=["pytest"], cwd=str(worktree), lane=MAC)

    await _wait_for(
        lambda: queue.get(elsewhere.id).state == RUNNING, what="the mac pod to start"
    )
    assert queue.get(behind.id).state == QUEUED

    for job in (busy, behind, elsewhere):
        await queue.cancel(job.id)
    await queue.stop()


# ---- a full dev cluster (#2170) --------------------------------------------


async def test_a_full_dev_cluster_never_sends_a_job_to_the_mac(tmp_path):
    """#2170 moved a chosen kube job to the Mac when the dev cluster was full.
    The Mac is now used only when named (#2297), so the job fails with the
    "nothing ran" exit instead of running there."""
    worktree = _fake_worktree(tmp_path)
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = kube ]; then\n'
        f'  echo "no room"; exit {KUBE_NO_ROOM_EXIT}\n'
        "fi\n",
    )
    queue = _queue(tmp_path)
    await queue.start()

    final = await _settle(
        queue,
        queue.submit(
            command=["pytest", "-q"], cwd=str(worktree), lane=KUBE, lane_defaulted=True
        ).id,
    )
    await queue.stop()

    assert (final.state, final.lane, final.exit_code) == (FAILED, KUBE, KUBE_NO_ROOM_EXIT)
    assert Path(final.log_path).read_text().splitlines() == ["no room"]


async def test_a_full_cluster_fails_what_the_mac_cannot_take(tmp_path):
    """Trailhead's suites and a job already on the Mac have nowhere else to go, so
    they fail as before rather than bouncing between lanes — and an explicit
    `--where kube` is obeyed, refusal and all, rather than rerouted."""
    worktree = _fake_worktree(tmp_path)
    runner = worktree / "scripts" / "kube_pytest.sh"
    runner.write_text(f"#!/bin/bash\nexit {KUBE_NO_ROOM_EXIT}\n", encoding="utf-8")
    runner.chmod(0o755)
    trailhead = _fake_trailhead(tmp_path)
    trailhead_runner = trailhead / "helm" / "app" / "jobs" / "test" / "run.sh"
    trailhead_runner.write_text(f"#!/bin/bash\nexit {KUBE_NO_ROOM_EXIT}\n", encoding="utf-8")
    queue = _queue(tmp_path)
    await queue.start()

    on_mac = await _settle(
        queue, queue.submit(command=["pytest"], cwd=str(worktree), lane=MAC).id
    )
    store = await _settle(
        queue,
        queue.submit(
            command=["pytest"], cwd=str(trailhead / "web"), lane=KUBE, lane_defaulted=True
        ).id,
    )
    explicit = await _settle(
        queue, queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE).id
    )
    await queue.stop()

    assert (on_mac.state, on_mac.lane, on_mac.note) == (FAILED, MAC, "")
    assert (store.state, store.lane, store.note) == (FAILED, KUBE, "")
    assert (explicit.state, explicit.lane, explicit.note) == (FAILED, KUBE, "")
    assert explicit.exit_code == KUBE_NO_ROOM_EXIT


async def test_a_full_dev_cluster_sends_a_suite_desktop_left_back_to_this_host(
    tmp_path, monkeypatch
):
    """#2835: with desktop marked down an agentdeck suite takes the dev cluster
    first, and a dev cluster without room sends it here rather than failing it.
    Trailhead's suites, and a job already moved off desktop, still fail."""
    worktree = _fake_worktree(tmp_path)
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = kube ]; then\n'
        f'  echo "no room"; exit {KUBE_NO_ROOM_EXIT}\n'
        "fi\n",
    )
    fake_pytest = tmp_path / "bin" / "pytest"
    fake_pytest.parent.mkdir()
    fake_pytest.write_text('#!/bin/bash\necho "ran here: $*"\n', encoding="utf-8")
    fake_pytest.chmod(0o755)
    marked = tmp_path / "job-lanes-down"
    marked.write_text("desktop\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES_DOWN", str(marked))
    # One suite's share is every core, so the local run adds no `-n` on any host (#3641).
    monkeypatch.setattr(jobs_module, "local_suite_workers", jobs_module.usable_cores)
    queue = _queue(tmp_path)
    await queue.start()

    chosen = await _settle(
        queue,
        queue.submit(
            command=[str(fake_pytest), "-q"], cwd=str(worktree), lane=KUBE, lane_defaulted=True
        ).id,
    )
    named = await _settle(
        queue, queue.submit(command=[str(fake_pytest), "-q"], cwd=str(worktree), lane=KUBE).id
    )
    await queue.stop()

    assert (chosen.state, chosen.lane, chosen.moved_from_lane) == (DONE, LOCAL, KUBE)
    assert chosen.note == moved_note(KUBE, LOCAL)
    assert Path(chosen.log_path).read_text().splitlines() == ["no room", "ran here: -q"]
    # An explicit `--where kube` is obeyed, refusal and all.
    assert (named.state, named.lane, named.exit_code) == (FAILED, KUBE, KUBE_NO_ROOM_EXIT)


async def test_an_unreachable_cluster_is_skipped_for_a_while_and_the_retry_goes_elsewhere(
    tmp_path,
):
    """#3584: a "nothing ran" (69) whose log says the cluster could not be reached marks the
    lane: the queue's one retry and a later suite that names no lane go elsewhere until the
    mark lapses, a named lane is kept with a warning, and any other 69 marks nothing."""
    worktree = _fake_worktree(tmp_path)
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = desktop ]; then\n'
        '  echo "dial tcp 10.0.0.5:6443: i/o timeout"; exit 69\n'
        "fi\n",
    )
    now = [time.time()]
    queue = _queue(tmp_path, clock=lambda: now[0])
    await queue.start()
    first = await _settle(
        queue,
        queue.submit(
            command=["pytest", "-q"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True,
            purpose=VERIFICATION, run_id="gen-1", work_id="github_issue:eerovil/agentdeck#1",
        ).id,
    )
    retry = await _wait_for(
        lambda: next((job for job in queue.list() if job.follows == first.id), None),
        what="the retry to be submitted",
    )
    retry = await _settle(queue, retry.id)
    # The CLI chose desktop before the queue heard; a lane named with --where is kept.
    chosen = queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True)
    named = queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP)
    await _settle(queue, chosen.id)
    await _settle(queue, named.id)
    await queue.stop()

    assert (first.state, first.exit_code) == (FAILED, 69)
    assert first.note.startswith("the desktop cluster was unreachable; nothing ran.")
    assert (retry.state, retry.lane, retry.follow_kind) == (DONE, KUBE, flaky.RESUBMIT)
    said = queue.get(first.id).note
    assert f"moved the retry to kube as job {retry.id}. Do not resubmit." in said
    assert (chosen.lane, named.lane) == (KUBE, DESKTOP)
    assert "unreachable" in chosen.note and "this job named desktop" in named.note
    # Kept beside jobs.json, so a restarted runtime still skips it, until it lapses.
    assert _queue(tmp_path, clock=lambda: now[0])._skipped_lanes() == {DESKTOP}
    now[0] += UNREACHABLE_SKIP_S
    assert queue._skipped_lanes() == frozenset()
    # A dropped stream that names no unreachable cluster is retried where it was.
    lost = tmp_path / "lost.log"
    lost.write_text("error: lost connection to pod\n", encoding="utf-8")
    other = Job(id="o", command=["pytest"], cwd=str(worktree), log_path=str(lost),
                submitted_at=0.0, lane=KUBE)
    assert queue._mark_if_unreachable(other, 69) is None
    # This host has no cluster to be unreachable.
    here = replace(other, lane=LOCAL, log_path=first.log_path)
    assert queue._mark_if_unreachable(here, 69) is None
    assert queue._skipped_lanes() == frozenset()


# ---- desktop first (#2297) -------------------------------------------------


def _profile_runner(worktree: Path, body: str = "") -> None:
    runner = worktree / "scripts" / "kube_pytest.sh"
    runner.write_text(
        f"#!/bin/bash\n{body}echo \"ran on ${KUBE_PROFILE_ENV}: $*\"\n", encoding="utf-8"
    )
    runner.chmod(0o755)


async def test_an_desktop_job_asks_the_runner_for_desktop(tmp_path):
    worktree = _fake_worktree(tmp_path)
    _profile_runner(worktree)
    queue = _queue(tmp_path)
    await queue.start()

    final = await _settle(
        queue, queue.submit(command=["pytest", "-q"], cwd=str(worktree), lane=DESKTOP).id
    )
    await queue.stop()

    assert (final.state, final.lane) == (DONE, DESKTOP)
    assert Path(final.log_path).read_text().strip() == "ran on desktop: -q"
    assert "Ran in a pod on the desktop, not on this host." in job_message(final)


def test_the_desktop_lane_runs_the_agentdeck_checkout_only(tmp_path):
    trailhead = _fake_trailhead(tmp_path)
    queue = _queue(tmp_path)

    with pytest.raises(JobError, match="desktop lane does not run the trailhead checkout"):
        queue.submit(command=["pytest"], cwd=str(trailhead / "web"), lane=DESKTOP)


async def test_desktop_without_room_or_switched_off_moves_the_job_to_the_dev_cluster(tmp_path):
    """The runner exits 75 for a full node and for one that does not answer."""
    worktree = _fake_worktree(tmp_path)
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = desktop ]; then echo "off"; exit {KUBE_NO_ROOM_EXIT}; fi\n',
    )
    queue = _queue(tmp_path)
    await queue.start()

    chosen = await _settle(
        queue,
        queue.submit(
            command=["pytest", "-q"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
        ).id,
    )
    named = await _settle(
        queue, queue.submit(command=["pytest", "-q"], cwd=str(worktree), lane=DESKTOP).id
    )
    await queue.stop()

    assert (chosen.state, chosen.lane, chosen.moved_from_lane) == (DONE, KUBE, DESKTOP)
    assert chosen.note == moved_note(DESKTOP, KUBE)
    assert Path(chosen.log_path).read_text().splitlines() == ["off", "ran on kube: -q"]
    assert moved_note(DESKTOP, KUBE) in job_message(chosen)
    # An explicit `--where desktop` is obeyed, refusal and all.
    assert (named.state, named.lane, named.exit_code) == (FAILED, DESKTOP, KUBE_NO_ROOM_EXIT)


async def test_a_busy_desktop_sends_the_next_chosen_job_to_the_dev_cluster(tmp_path):
    """desktop has one slot; the second suite runs on the dev cluster now rather
    than waiting a whole suite for it. A job that named desktop waits its turn,
    and nothing lands on the Mac even with its slots free."""
    worktree = _fake_worktree(tmp_path)
    _profile_runner(worktree, f'[ "${KUBE_PROFILE_ENV}" = desktop ] && sleep 30\n')
    queue = _queue(tmp_path, desktop_slots=1)
    await queue.start()
    busy = queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True)
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="desktop to start")
    named = queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP)
    chosen = queue.submit(
        command=["pytest"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
    )

    spilled = await _settle(queue, chosen.id)
    assert queue.get(named.id).state == QUEUED
    assert queue.get(busy.id).lane == DESKTOP

    for job in (busy, named):
        await queue.cancel(job.id)
    await queue.stop()

    assert (spilled.state, spilled.lane, spilled.moved_from_lane) == (DONE, KUBE, DESKTOP)
    assert spilled.note == spilled_note(DESKTOP, KUBE)
    assert Path(spilled.log_path).read_text().strip() == "ran on kube:"


async def test_a_spilled_job_the_dev_cluster_has_no_room_for_waits_for_desktop(tmp_path):
    """#2303: a job spilled off a busy desktop that the dev cluster cannot place
    goes back to wait for desktop instead of failing, and the queue stops
    spilling there for a while, so the next job waits for desktop as well."""
    worktree = _fake_worktree(tmp_path)
    hold = tmp_path / "hold"
    hold.touch()
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = kube ]; then echo "no room"; '
        f'exit {KUBE_NO_ROOM_EXIT}; fi\n'
        f'if [ "$1" = busy ]; then while [ -e {hold} ]; do sleep 0.05; done; fi\n',
    )
    now = [1000.0]
    queue = _queue(tmp_path, desktop_slots=1, clock=lambda: now[0])
    await queue.start()
    busy = queue.submit(
        command=["pytest", "busy"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="desktop to start")
    spilled = queue.submit(
        command=["pytest", "one"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
    )
    await _wait_for(
        lambda: queue.get(spilled.id).note == spill_back_note(DESKTOP),
        what="the spilled job to come back",
    )
    behind = queue.submit(
        command=["pytest", "two"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
    )
    await asyncio.sleep(0.3)
    assert (queue.get(spilled.id).state, queue.get(spilled.id).lane) == (QUEUED, DESKTOP)
    assert (queue.get(behind.id).state, queue.get(behind.id).lane) == (QUEUED, DESKTOP)

    hold.unlink()
    first = await _settle(queue, spilled.id)
    second = await _settle(queue, behind.id)
    await queue.stop()

    assert (first.state, first.lane, first.moved_from_lane) == (DONE, DESKTOP, KUBE)
    assert Path(first.log_path).read_text().splitlines() == ["no room", "ran on desktop: one"]
    assert spill_back_note(DESKTOP) in job_message(first)
    assert (second.state, second.lane, second.note) == (DONE, DESKTOP, "")


async def test_spilling_resumes_once_the_pause_after_no_room_is_over(tmp_path):
    worktree = _fake_worktree(tmp_path)
    full = tmp_path / "full"
    full.touch()
    hold = tmp_path / "hold"
    hold.touch()
    _profile_runner(
        worktree,
        f'if [ "${KUBE_PROFILE_ENV}" = kube ] && [ -e {full} ]; '
        f'then exit {KUBE_NO_ROOM_EXIT}; fi\n'
        f'if [ "$1" = busy ]; then while [ -e {hold} ]; do sleep 0.05; done; fi\n',
    )
    now = [1000.0]
    queue = _queue(tmp_path, desktop_slots=1, clock=lambda: now[0])
    await queue.start()
    busy = queue.submit(
        command=["pytest", "busy"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="desktop to start")
    first = queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP, lane_defaulted=True)
    await _wait_for(
        lambda: queue.get(first.id).note == spill_back_note(DESKTOP),
        what="the spilled job to come back",
    )
    full.unlink()
    now[0] += SPILL_PAUSE_S + 1
    queue._wake.set()

    spilled_again = await _settle(queue, first.id)
    hold.unlink()
    await _settle(queue, busy.id)
    await queue.stop()

    assert (spilled_again.state, spilled_again.lane) == (DONE, KUBE)
    assert spilled_again.note == spilled_note(DESKTOP, KUBE)


async def test_a_suite_this_host_took_spills_to_the_dev_cluster_while_its_slot_is_taken(
    tmp_path, monkeypatch
):
    """#2826: with desktop marked down this host has its one slot; the second
    suite takes the dev cluster rather than waiting. One that also tests another
    checkout, or that named this host, waits; and nothing spills to a lane that
    is itself marked down."""
    worktree = _fake_worktree(tmp_path)
    other = _fake_worktree(tmp_path / "other")
    _profile_runner(worktree)
    hold = tmp_path / "hold"
    hold.touch()
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    busy = queue.submit(
        command=["bash", "-c", f"while [ -e {hold} ]; do sleep 0.05; done"],
        cwd=str(worktree),
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="the local slot to fill")
    named = queue.submit(command=["uv", "run", "pytest"], cwd=str(worktree))
    both = queue.submit(
        command=["uv", "run", "pytest"],
        cwd=str(worktree),
        lane_defaulted=True,
        purpose="verification",
        run_id="run-1",
        workspaces=[str(other)],
    )
    chosen = queue.submit(command=["uv", "run", "pytest"], cwd=str(worktree), lane_defaulted=True)

    spilled = await _settle(queue, chosen.id)
    assert (queue.get(named.id).state, queue.get(both.id).state) == (QUEUED, QUEUED)

    marked = tmp_path / "job-lanes-down"
    marked.write_text("kube\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES_DOWN", str(marked))
    held = queue.submit(command=["uv", "run", "pytest"], cwd=str(worktree), lane_defaulted=True)
    await asyncio.sleep(0.3)
    assert (queue.get(held.id).state, queue.get(held.id).lane) == (QUEUED, LOCAL)

    for job in (named, both, held, busy):
        await queue.cancel(job.id)
    hold.unlink()
    await queue.stop()

    assert (spilled.state, spilled.lane, spilled.moved_from_lane) == (DONE, KUBE, LOCAL)
    assert spilled.note == spilled_note(LOCAL, KUBE)
    assert Path(spilled.log_path).read_text().strip() == "ran on kube:"


async def test_a_big_host_spills_to_desktop_then_the_dev_cluster_and_waits_for_memory(
    tmp_path, monkeypatch
):
    """#3614: a host that runs suites first sends the next one to desktop while its own
    slot is taken, then to the dev cluster, and one desktop has no room for comes back to
    wait here. Short of memory, this host counts as full though a slot is free."""
    monkeypatch.setenv(jobs_module.LOCAL_FIRST_MIN_CORES_ENV, "10")
    monkeypatch.setattr(jobs_module, "usable_cores", lambda: 12)
    worktree = _fake_worktree(tmp_path)
    hold = tmp_path / "hold"
    hold.touch()
    _profile_runner(
        worktree,
        f'if [ "$1" = full ]; then echo "no room"; exit {KUBE_NO_ROOM_EXIT}; fi\n'
        f'while [ -e {hold} ]; do sleep 0.05; done\n',
    )
    memory = [True]
    gate = MemoryGate(available=lambda: (100 if memory[0] else 0) * 1024**3, floor=lambda: 1)
    queue = _queue(tmp_path, slots=2, memory_gate=gate)
    await queue.start()
    busy = queue.submit(
        command=["bash", "-c", f"while [ -e {hold} ]; do sleep 0.05; done"], cwd=str(worktree)
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="one local slot to fill")
    memory[0] = False

    def suite(*args: str) -> Job:
        return queue.submit(
            command=["uv", "run", "pytest", *args], cwd=str(worktree), lane_defaulted=True
        )

    to_desktop = suite("one")
    await _wait_for(lambda: queue.get(to_desktop.id).state == RUNNING, what="the first spill")
    to_kube = suite("two")
    await _wait_for(lambda: queue.get(to_kube.id).state == RUNNING, what="the second spill")
    held = queue.submit(command=["uv", "run", "pytest"], cwd=str(worktree))
    await _wait_for(lambda: queue.memory_waiting == 1, what="the memory check")
    assert queue.get(held.id).state == QUEUED, "a free slot but no memory"

    assert (queue.get(to_desktop.id).lane, queue.get(to_desktop.id).note) == (
        DESKTOP, spilled_note(LOCAL, DESKTOP)
    )
    assert (queue.get(to_kube.id).lane, queue.get(to_kube.id).note) == (
        KUBE, spilled_note(LOCAL, KUBE)
    )
    hold.unlink()
    await _settle(queue, to_desktop.id)
    await _settle(queue, to_kube.id)
    await queue.cancel(held.id)
    await _settle(queue, busy.id)
    hold.touch()

    # desktop answering "no room" sends the spilled suite back here to wait.
    marked = tmp_path / "job-lanes-down"
    marked.write_text("kube\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES_DOWN", str(marked))
    bounced = suite("full")
    await _wait_for(
        lambda: queue.get(bounced.id).note == spill_back_note(LOCAL),
        what="the spilled job to come back",
    )
    await asyncio.sleep(0.1)
    assert (queue.get(bounced.id).lane, queue.get(bounced.id).state) == (LOCAL, QUEUED)
    await queue.cancel(bounced.id)
    await _wait_for(lambda: queue.memory_waiting == 0, what="nothing left waiting here")

    # This host marked down: a suite placed here goes to a pod though a local slot is free,
    # and a job no pod can run still runs here.
    memory[0] = True
    marked.write_text("local\n", encoding="utf-8")
    hold.unlink()
    away = await _settle(queue, suite("three").id)
    build = await _settle(queue, queue.submit(command=["true"], cwd=str(worktree)).id)
    await queue.stop()
    # The dev cluster, as desktop's "no room" above paused spilling there.
    assert (away.lane, away.state, away.moved_from_lane) == (KUBE, DONE, LOCAL)
    assert (build.lane, build.state) == (LOCAL, DONE)


def test_a_local_agentdeck_suite_splits_the_cores_unless_it_says_otherwise(
    tmp_path, monkeypatch
):
    """#3614: two suites at once each get their share of the cores, not one worker per core."""
    worktree = _fake_worktree(tmp_path)
    monkeypatch.setattr(jobs_module, "local_suite_workers", lambda: 6)
    monkeypatch.setattr(jobs_module, "usable_cores", lambda: 12)
    queue = _queue(tmp_path)

    def argv(*command: str) -> list[str]:
        return jobs_module._argv(queue.submit(command=list(command), cwd=str(worktree)))

    assert argv("uv", "run", "pytest", "-q") == ["uv", "run", "pytest", "-q", "-n", "6"]
    for given in (["-n", "2"], ["-n2"], ["--numprocesses=3"], ["-p", "no:xdist"]):
        assert argv("uv", "run", "pytest", *given)[-len(given):] == given
        assert "6" not in argv("uv", "run", "pytest", *given)
    assert argv("make", "build") == ["make", "build"]
    # One slot's share is every core, which the project's own `-n auto` already is.
    monkeypatch.setattr(jobs_module, "usable_cores", lambda: 6)
    assert argv("uv", "run", "pytest", "-q") == ["uv", "run", "pytest", "-q"]


def test_a_spill_is_known_by_its_state_not_its_note_and_memory_is_reserved_per_start(tmp_path):
    """#3614 review: a run appends to the note (a quarantine line), so the spill is kept as
    state, and survives a reload; and two starts a moment apart cannot both spend the same
    free memory, nor can a lease spend what a suite just took."""
    worktree = _fake_worktree(tmp_path)
    queue = _queue(tmp_path)
    job = queue.submit(command=["uv", "run", "pytest"], cwd=str(worktree), lane_defaulted=True)
    moved = queue._move_lane(job.id, DESKTOP, spilled_note(LOCAL, DESKTOP), spilled=True)
    quarantined = replace(moved, note=f"{moved.note}; Left out 1 quarantined test")
    assert jobs_module._spilled_from(quarantined) == LOCAL
    assert Job.from_dict(quarantined.to_dict()).spilled is True
    assert jobs_module._spilled_from(replace(quarantined, spilled=False)) is None

    gib = 1024**3
    now = [0.0]
    gate = MemoryGate(available=lambda: 7 * gib, floor=lambda: 6 * gib, clock=lambda: now[0])
    assert gate.ok()
    gate.admit()
    assert not gate.ok(), "the first start's share is still counted as free"
    leases = SlotLeases(slot_paths=slot_paths("lease", 1, tmp_path), memory_gate=gate)
    assert asyncio.run(leases.acquire(label="scan")) is None
    assert leases.short_of_memory is True
    now[0] += 121
    assert gate.ok(), "its own use shows in MemAvailable by now"


async def test_a_follow_up_of_a_queue_placed_verification_can_leave_this_host(
    tmp_path, monkeypatch
):
    """#3043: a verification the queue placed here by fallback has its failing tests rerun
    on a pod while this host's one slot is held, not queued behind it. One whose submitter
    named this host, or that also tested another checkout, keeps its follow-ups here."""
    worktree = _fake_worktree(tmp_path)
    other = _fake_worktree(tmp_path / "other")
    _profile_runner(worktree)
    monkeypatch.setattr(jobs_module, "_pinned_checkout", lambda cwd, sha, prefix: str(worktree))
    marked = tmp_path / "job-lanes-down"
    marked.write_text("desktop\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES_DOWN", str(marked))
    hold = tmp_path / "hold"
    hold.touch()
    queue = _queue(tmp_path, slots=1)

    def verification(**placed) -> Job:
        return queue.submit(
            command=["uv", "run", "pytest"],
            cwd=str(worktree),
            lane=LOCAL,
            purpose="verification",
            work_id="github_issue:eerovil/agentdeck#3043",
            **placed,
        )

    chosen = verification(lane_defaulted=True)
    named = verification()
    both = verification(lane_defaulted=True, workspaces=[str(other)])
    for job in (chosen, named, both):
        await queue.cancel(job.id)  # finished before the queue starts, as if they had run
    await queue.start()
    busy = queue.submit(
        command=["bash", "-c", f"while [ -e {hold} ]; do sleep 0.05; done"],
        cwd=str(worktree),
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="the local slot to fill")

    def follow_of(job: Job) -> Job:
        [follow] = [row for row in queue.list() if row.follows == job.id]
        return follow

    for job in (chosen, named, both):
        await queue._submit_follow_up(
            queue.get(job.id), flaky.FollowUp(flaky.RERUN, job.id, ("tests/test_a.py::t",))
        )
    rerun = await _settle(queue, follow_of(chosen).id)
    await asyncio.sleep(0.3)
    kept = [queue.get(follow_of(job).id) for job in (named, both)]
    assert [(job.state, job.lane) for job in kept] == [(QUEUED, LOCAL), (QUEUED, LOCAL)]

    for job in (*kept, busy):
        await queue.cancel(job.id)
    hold.unlink()
    await queue.stop()

    assert (rerun.state, rerun.lane, rerun.lane_defaulted) == (DONE, KUBE, True)
    assert Path(rerun.log_path).read_text().strip() == "ran on kube: -q tests/test_a.py::t"


async def test_a_rerun_or_base_check_starts_ahead_of_a_suite_queued_before_it(tmp_path):
    """#3040: a merge-queue head's two-minute base check waited half an hour behind a whole
    suite for this host's one slot. A same-commit rerun or merge-base check goes first; a
    resubmitted verification is a whole suite again and keeps its place. A starred Work's
    ordinary job comes right after them (#3116), found by its chat's Work, and then a
    verification ahead of a plain job queued before it (#3611): a merge waits on the one."""
    hold = tmp_path / "hold"
    hold.touch()
    order = tmp_path / "order"
    queue = _queue(tmp_path, slots=1)
    queue._starred = lambda: {"GitHub_Issue:eerovil/AgentDeck#7"}  # case as spelled anywhere
    await queue.start()
    busy = queue.submit(
        command=["bash", "-c", f"while [ -e {hold} ]; do sleep 0.05; done"], cwd=str(tmp_path)
    )
    await _wait_for(lambda: queue.get(busy.id).state == RUNNING, what="the slot to fill")

    def noting(name: str) -> list[str]:
        return ["bash", "-c", f"echo {name} >> {order}"]

    jobs = [
        queue.submit(command=noting("suite"), cwd=str(tmp_path)),
        queue.submit(
            command=noting("resubmit"), cwd=str(tmp_path), follows="v", follow_kind=flaky.RESUBMIT
        ),
        queue.submit(
            command=noting("base"), cwd=str(tmp_path), follows="v", follow_kind=flaky.BASE
        ),
        queue.submit(
            command=noting("rerun"), cwd=str(tmp_path), follows="w", follow_kind=flaky.RERUN
        ),
        queue.submit(
            command=noting("starred"),
            cwd=str(tmp_path),
            work_id="github_issue:eerovil/agentdeck#7",
        ),
        queue.submit(
            command=noting("verify"),
            cwd=str(tmp_path),
            purpose=VERIFICATION,
            work_id="github_issue:eerovil/agentdeck#8",
        ),
    ]
    assert queue.get(jobs[4].id).work_id == "", "an ordinary job is still no Work's evidence"
    assert [job.id for job in queue.waiting()] == [jobs[i].id for i in (2, 3, 4, 5, 0, 1)]
    hold.unlink()
    for job in jobs:
        await _settle(queue, job.id)
    await queue.stop()

    assert order.read_text().split() == ["base", "rerun", "starred", "verify", "suite", "resubmit"]


def test_whether_the_lane_was_chosen_survives_a_restart(tmp_path):
    """A queued job outlives the runtime; so must its permission to move."""
    worktree = _fake_worktree(tmp_path)
    queue = _queue(tmp_path)
    chosen = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE, lane_defaulted=True)
    named = queue.submit(command=["pytest"], cwd=str(worktree), lane=KUBE)

    assert Job.from_dict(chosen.to_dict()).lane_defaulted is True
    assert Job.from_dict(named.to_dict()).lane_defaulted is False
    # A row written before #2170 reads as a lane the submitter named.
    old = {k: v for k, v in chosen.to_dict().items() if k != "lane_defaulted"}
    assert Job.from_dict(old).lane_defaulted is False


def test_a_row_written_before_the_lane_existed_still_loads(tmp_path):
    row = Job(
        id="old",
        command=["pytest"],
        cwd=str(tmp_path),
        log_path=str(tmp_path / "old.log"),
        submitted_at=time.time(),
    ).to_dict()
    row.pop("lane")

    assert Job.from_dict(row).lane == LOCAL


# ---- the base-branch baseline (#1737) ---------------------------------------


def _baseline_repo(path: Path) -> str:
    """A repository with an origin and the two files the environment identity is read from."""
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "master")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    git("remote", "add", "origin", "git@github.com:owner/repo.git")
    (path / "uv.lock").write_text("lock\n")
    (path / "pyproject.toml").write_text("[project]\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/master", head)
    return head


def _summary_command(*failures: str) -> list[str]:
    """A command whose output is shaped the way pytest's really is.

    An all-pass run prints **no** summary section — pytest emits one only when it has
    something to put in it — so this must not print a header when there is nothing to report,
    or it would hide the very case a green baseline depends on.
    """
    if failures:
        lines = "\\n".join(f"FAILED {name} - AssertionError" for name in failures)
        printed = f"=== short test summary info ===\\n{lines}\\n{len(failures)} failed in 1.20s"
        status = 1
    else:
        printed = "........ [100%]\\n12 passed in 1.20s"
        status = 0
    return [sys.executable, "-c", f"import sys;print('{printed}');sys.exit({status})"]


async def test_a_finished_run_records_what_it_failed(tmp_path):
    repo = tmp_path / "repo"
    _baseline_repo(repo)
    queue = _queue(tmp_path, slots=1)
    await queue.start()

    job = queue.submit(command=_summary_command("tests/test_a.py::test_one"), cwd=str(repo))
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.failures == ("tests/test_a.py::test_one",)
    assert final.failures_complete is True
    assert final.environment.startswith(f"{LOCAL}:")


async def test_a_clean_run_becomes_the_baseline_a_later_branch_run_finds(tmp_path):
    repo = tmp_path / "repo"
    base = _baseline_repo(repo)
    queue = _queue(tmp_path, slots=1)
    await queue.start()

    job = queue.submit(command=_summary_command("tests/test_a.py::test_one"), cwd=str(repo))
    final = await _settle(queue, job.id)
    await queue.stop()

    # On master the merge base is HEAD itself, so this job's own lookup key is the key its
    # result was stored under -- which is exactly how a branch off it will find it.
    ledger = BaselineLedger(tmp_path / "jobs" / BASELINE_FILENAME)
    stored = ledger.lookup(baseline.lookup_key(final.to_dict()))
    assert stored is not None
    assert stored["head"] == base
    assert stored["failures"] == ["tests/test_a.py::test_one"]


async def test_an_all_pass_run_becomes_a_green_baseline(tmp_path):
    """The case a real pytest run exercises and a printed header would have hidden: an
    all-pass suite emits no summary section at all, and must still be recorded as a base
    result with nothing failing -- otherwise the feature has no green baseline to compare
    against, which is the ordinary case."""
    repo = tmp_path / "repo"
    _baseline_repo(repo)
    queue = _queue(tmp_path, slots=1)
    await queue.start()

    job = queue.submit(command=_summary_command(), cwd=str(repo))
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == DONE
    assert "short test summary info" not in Path(final.log_path).read_text()
    assert final.failures_complete is True
    stored = BaselineLedger(tmp_path / "jobs" / BASELINE_FILENAME).lookup(
        baseline.lookup_key(final.to_dict())
    )
    assert stored is not None
    assert stored["failures"] == []


async def test_a_run_over_an_edited_tree_is_not_kept_as_a_baseline(tmp_path):
    repo = tmp_path / "repo"
    _baseline_repo(repo)
    (repo / "uv.lock").write_text("edited\n")
    queue = _queue(tmp_path, slots=1)
    await queue.start()

    job = queue.submit(command=_summary_command(), cwd=str(repo))
    await _settle(queue, job.id)
    await queue.stop()

    assert not (tmp_path / "jobs" / BASELINE_FILENAME).exists()


async def test_a_baseline_that_cannot_be_written_never_costs_the_job_its_result(tmp_path):
    repo = tmp_path / "repo"
    _baseline_repo(repo)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    ledger = BaselineLedger(blocker / "baselines.json")
    queue = _queue(tmp_path, slots=1)
    queue._baselines = ledger
    await queue.start()

    job = queue.submit(command=_summary_command(), cwd=str(repo))
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == DONE
    assert final.result_class == "success"


# ---- cancelling ------------------------------------------------------------


async def test_cancelling_a_running_job_stops_it_and_frees_the_slot(tmp_path):
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    job = queue.submit(command=[sys.executable, "-c", SLOW], cwd=str(tmp_path))
    await _wait_for(lambda: queue.get(job.id).state == RUNNING, what="the job to start")

    await queue.cancel(job.id)
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == CANCELLED
    assert try_take_slot(queue._slots()) is not None


async def test_cancelling_while_the_process_spawns_still_stops_it(tmp_path, monkeypatch):
    """A job reads as running before its process exists (#2616): a cancel in
    that window found nothing to stop, and the job ran to its end."""
    entered = asyncio.Event()
    release = asyncio.Event()
    original = jobs_module.asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(jobs_module.asyncio, "create_subprocess_exec", slow_spawn)
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    job = queue.submit(command=[sys.executable, "-c", SLOW], cwd=str(tmp_path))
    await asyncio.wait_for(entered.wait(), 5)
    assert queue.get(job.id).state == RUNNING

    await queue.cancel(job.id)
    release.set()
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == CANCELLED
    assert final.note == "cancelled while running"
    assert try_take_slot(queue._slots()) is not None


async def test_cancelling_a_queued_job_never_runs_it(tmp_path):
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    blocker = queue.submit(command=[sys.executable, "-c", SLOW], cwd=str(tmp_path))
    waiting = queue.submit(command=[sys.executable, "-c", "print('never')"], cwd=str(tmp_path))
    await _wait_for(lambda: queue.get(blocker.id).state == RUNNING, what="the blocker to start")

    cancelled = await queue.cancel(waiting.id)
    await queue.cancel(blocker.id)
    await queue.stop()

    assert cancelled.state == CANCELLED
    assert not Path(waiting.log_path).exists()


async def test_a_rolled_back_chat_hears_nothing_of_the_jobs_it_forgot(tmp_path):
    """A rollback (#3474) cancels the chat's jobs from the removed part, unannounced."""
    clock = _Clock()
    queue = _queue(tmp_path, slots=1, clock=lambda: clock.now)
    kept = queue.submit(command=["true"], cwd=str(tmp_path), session_key="acct:s")
    clock.now += 100
    since = clock.now
    forgotten = queue.submit(command=["true"], cwd=str(tmp_path), session_key="acct:s")
    other = queue.submit(command=["true"], cwd=str(tmp_path), session_key="acct:other")

    silenced = await queue.silence(["acct:s"], since)

    assert [job.id for job in silenced] == [forgotten.id]
    assert queue.get(forgotten.id).state == CANCELLED
    assert queue.get(forgotten.id).notified
    assert queue.get(kept.id).state == QUEUED
    assert queue.get(other.id).state == QUEUED


async def test_cancelling_during_workspace_capture_never_starts_the_command(
    tmp_path, monkeypatch
):
    entered = threading.Event()
    release = threading.Event()
    original = jobs_module.collect_workspace_snapshot

    def slow_snapshot(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs_module, "collect_workspace_snapshot", slow_snapshot)
    marker = tmp_path / "command-ran"
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    job = queue.submit(
        command=[
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
        ],
        cwd=str(tmp_path),
    )
    assert await asyncio.to_thread(entered.wait, 5)

    await queue.cancel(job.id)
    release.set()
    final = await _settle(queue, job.id)
    await queue.stop()

    assert final.state == CANCELLED
    assert final.note == "cancelled before command start"
    assert not marker.exists()


# ---- submission refusals ---------------------------------------------------


def test_a_submission_needs_a_command_and_a_real_directory(tmp_path):
    queue = _queue(tmp_path)

    with pytest.raises(JobError):
        queue.submit(command=[], cwd=str(tmp_path))
    with pytest.raises(JobError):
        queue.submit(command=["true"], cwd=str(tmp_path / "does-not-exist"))


async def test_a_command_that_cannot_start_fails_rather_than_wedging_the_queue(tmp_path):
    queue = _queue(tmp_path, slots=1)
    await queue.start()
    broken = queue.submit(command=[str(tmp_path / "not-a-program")], cwd=str(tmp_path))
    final = await _settle(queue, broken.id)

    assert final.state == FAILED
    assert "could not start" in final.note

    # The slot went back, so the next job still runs.
    good = queue.submit(command=[sys.executable, "-c", "print('fine')"], cwd=str(tmp_path))
    assert (await _settle(queue, good.id)).state == DONE
    await queue.stop()


# ---- surviving a runtime restart -------------------------------------------


def _write_jobs(tmp_path: Path, rows: list[dict]) -> None:
    directory = tmp_path / "jobs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jobs.json").write_text(json.dumps(rows))


def _row(**overrides) -> dict:
    row = {
        "id": "job-1",
        "command": ["pytest", "-q"],
        "cwd": "/tmp",
        "log_path": "/tmp/job-1.log",
        "submitted_at": time.time(),
        "state": RUNNING,
        "session_key": "claude_code:main:sid",
        "label": "",
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "note": "",
        "notified": False,
    }
    row.update(overrides)
    return row


async def test_a_job_the_restart_killed_comes_back_interrupted_and_is_announced(tmp_path):
    """A restart replaces the runtime, so a row that says `running` is a lie.

    Left alone it is worse than a lie: the chat that submitted it waits forever
    for a message about a process that no longer exists.
    """
    _write_jobs(tmp_path, [_row()])
    told: list[str] = []

    async def notify(job: Job, message: str) -> bool:
        told.append(message)
        return True

    queue = _queue(tmp_path, notify=notify)
    await queue.start()
    await queue.stop()

    job = queue.get("job-1")
    assert job.state == INTERRUPTED
    assert "restarted" in job.note
    assert told and "Resubmit" in told[0]
    assert "was cut off by a runtime restart after 0 min" in told[0]


async def test_a_cut_off_job_says_how_long_it_ran_up_to_its_last_output(tmp_path):
    """#2625: "did not run" sent workers to rerun a suite that had died at 86%. The time the
    runtime spent coming back ran nothing, so the log's last write is where it stopped."""
    log = tmp_path / "job-1.log"
    log.write_text("....F....\n")
    started = time.time() - 3600
    os.utime(log, (started + 20 * 60, started + 20 * 60))
    _write_jobs(tmp_path, [_row(log_path=str(log), started_at=started)])
    told: list[str] = []

    async def notify(job: Job, message: str) -> bool:
        told.append(message)
        return True

    queue = _queue(tmp_path, notify=notify)
    await queue.start()
    await queue.stop()

    [message] = told
    assert message.startswith(
        "Your queued job job-1 was cut off by a runtime restart after 20 min."
    )
    assert "did not run" not in message
    assert "Ran for" not in message, "its finish time is the new runtime's start"


async def test_a_restart_takes_a_running_job_back_instead_of_cutting_it_off(
    tmp_path, monkeypatch
):
    """#3248: the job runs outside the runtime, so the next runtime watches it to its end.

    No systemd here: the scope prefix is dropped, which leaves the wrapper that records the
    exit code and holds the slot -- the part a restart actually depends on.
    """

    async def scopes(_memory_max: str) -> bool:
        return True

    monkeypatch.setattr(jobs_module.worker_limits, "available", scopes)
    monkeypatch.setattr(jobs_module, "_scope_prefix", lambda _job_id: [])
    release = tmp_path / "release"
    told: list[str] = []

    async def notify(job: Job, message: str) -> bool:
        told.append(message)
        return True

    first = _queue(tmp_path, slots=1, notify=notify, survive_restart=True)
    await first.start()
    job = first.submit(
        command=[
            sys.executable,
            "-c",
            f"import os, time\nwhile not os.path.exists({str(release)!r}): time.sleep(0.01)\n"
            # The slot's descriptor is the wrapper's alone (a runtime's is usually above 9).
            "fds = []\n"
            "for fd in os.listdir('/proc/self/fd'):\n"
            "    try: fds.append(os.readlink(f'/proc/self/fd/{fd}'))\n"
            "    except OSError: pass\n"
            "print('slot fds:', [path for path in fds if 'slot-' in path], flush=True)\n"
            "raise SystemExit(3)",
        ],
        cwd=str(tmp_path),
        session_key="claude_code:main:sid",
    )
    running = await _wait_for(lambda: (row := first.get(job.id)) and row.pid and row)
    assert first.running_jobs_a_restart_would_cut() == 0, "it outlives the runtime"
    await first.stop()
    assert first.get(job.id).state == RUNNING
    os.kill(running.pid, 0)  # still running with no runtime watching it
    assert jobs_module.try_take_slot(first.heavy_slots()) is None, "the job keeps its slot"

    second = _queue(tmp_path, slots=1, notify=notify, survive_restart=True)
    await second.start()
    assert second.get(job.id).state == RUNNING
    release.touch()
    final = await _settle(second, job.id)
    await second.stop()

    assert (final.state, final.exit_code) == (FAILED, 3)
    assert "slot fds: []" in Path(final.log_path).read_text()
    assert told and "cut off" not in told[0]
    assert not os.path.exists(running.exit_path)


async def test_a_job_that_finished_while_no_runtime_ran_keeps_its_exit_code(tmp_path):
    exit_path = tmp_path / "job-1.log.exit"
    exit_path.write_text("0\n")
    _write_jobs(tmp_path, [_row(pid=2**22 + 1, pid_start=1, exit_path=str(exit_path))])

    # A runtime that cannot save the outcome keeps the exit file, the one record of it.
    unsaved = _queue(tmp_path, survive_restart=True)
    unsaved._save = lambda: False
    await unsaved.start()
    await _wait_for(lambda: "job-1" not in unsaved._running, what="the unsaved finish")
    await unsaved.stop()
    assert exit_path.exists()

    queue = _queue(tmp_path, survive_restart=True)
    await queue.start()
    final = await _settle(queue, "job-1")
    await queue.stop()

    assert (final.state, final.exit_code) == (DONE, 0)
    assert not exit_path.exists()


def test_a_job_that_never_started_still_did_not_run():
    job = Job.from_dict(_row(state=INTERRUPTED, started_at=None, finished_at=time.time()))

    assert job_message(job).startswith("Your queued job job-1 did not run.")


async def test_an_unavailable_completion_retries_after_restart_exactly_once(tmp_path):
    _write_jobs(tmp_path, [_row(state=DONE, finished_at=time.time(), exit_code=0)])

    unavailable = _runtime_app(tmp_path)
    await unavailable.state.jobs.start()
    await unavailable.state.jobs.stop()

    assert unavailable.state.jobs.get("job-1").notified is False
    unavailable.state.budget_db.close()

    retry = _runtime_app(tmp_path)
    retry.state.restart_nudges.deliver_to_session = AsyncMock(
        return_value=DeliverResult(True, "queued", session_id="sid")
    )
    await retry.state.jobs.start()
    await retry.state.jobs.stop()

    assert retry.state.jobs.get("job-1").notified is True
    retry.state.restart_nudges.deliver_to_session.assert_awaited_once()
    assert retry.state.restart_nudges.deliver_to_session.await_args.kwargs[
        "delivery_id"
    ] == "job-finished:job-1"
    retry.state.budget_db.close()

    already_delivered = _runtime_app(tmp_path)
    already_delivered.state.restart_nudges.deliver_to_session = AsyncMock()
    await already_delivered.state.jobs.start()
    await already_delivered.state.jobs.stop()

    already_delivered.state.restart_nudges.deliver_to_session.assert_not_awaited()
    already_delivered.state.budget_db.close()


async def test_a_crash_after_fenced_delivery_replays_without_a_second_write(tmp_path):
    _write_jobs(tmp_path, [_row(state=DONE, finished_at=time.time(), exit_code=0)])
    writes: list[str] = []
    fence_path = tmp_path / "session-deliveries.json"

    def notifier(fence: SessionDeliveryFence):
        async def notify(job: Job, message: str) -> bool:
            async def send() -> InjectResult:
                writes.append(message)
                return InjectResult(True, session_id="sid")

            result = await fence.deliver(
                "main",
                "sid",
                message,
                images=[],
                operation="queue",
                delivery_id=f"job-finished:{job.id}",
                send=send,
            )
            return result.accepted

        return notify

    first_fence = SessionDeliveryFence(
        fence_path, budget_exhausted=lambda _label, _session_id: False
    )
    first = _queue(tmp_path, notify=notifier(first_fence))
    pending = first.load()
    first._save = MagicMock()  # crash before notified=true reaches disk
    await first._announce(pending[0])

    retry_fence = SessionDeliveryFence(
        fence_path, budget_exhausted=lambda _label, _session_id: False
    )
    retry = _queue(tmp_path, notify=notifier(retry_fence))
    await retry.start()
    await retry.stop()

    assert retry.get("job-1").notified is True
    assert len(writes) == 1


async def test_a_job_that_waited_all_morning_is_dropped_rather_than_run(tmp_path):
    stale = _row(id="stale", state=QUEUED, submitted_at=time.time() - QUEUED_TTL_S - 60)
    fresh = _row(id="fresh", state=QUEUED, submitted_at=time.time(), command=["true"])
    _write_jobs(tmp_path, [stale, fresh])

    queue = _queue(tmp_path)
    interrupted = queue.load()

    assert [job.id for job in interrupted] == ["stale"]
    assert queue.get("fresh").state == QUEUED


def test_a_row_from_another_version_is_dropped_not_fatal(tmp_path):
    _write_jobs(tmp_path, [{"id": "bad"}, _row(id="good", state=DONE)])
    queue = _queue(tmp_path)
    queue.load()

    assert queue.get("bad") is None
    assert queue.get("good") is not None


def test_an_unnotified_completion_is_not_pruned_before_delivery(tmp_path):
    old = time.time() - 2 * FINISHED_RETENTION_S
    _write_jobs(
        tmp_path,
        [
            _row(id="pending", state=DONE, finished_at=old, notified=False),
            _row(id="delivered", state=DONE, finished_at=old, notified=True),
        ],
    )
    queue = _queue(tmp_path)

    pending = queue.load()

    assert [job.id for job in pending] == ["pending"]
    assert queue.get("pending") is not None
    assert queue.get("delivered") is None


def test_a_finished_job_with_no_chat_ages_out(tmp_path):
    """Nothing ever marks a chat-less row notified, so notified cannot be what prunes it (#2162)."""
    old = time.time() - 2 * FINISHED_RETENTION_S
    _write_jobs(
        tmp_path,
        [
            _row(id="old-chatless", state=DONE, finished_at=old, session_key=None),
            _row(id="new-chatless", state=DONE, finished_at=time.time(), session_key=None),
        ],
    )
    queue = _queue(tmp_path)

    queue.load()

    assert queue.get("old-chatless") is None
    assert queue.get("new-chatless") is not None


def test_the_cap_trims_chatless_rows_before_a_fresh_poller_result(tmp_path, monkeypatch):
    """#2138's passed suite was the row the cap trimmed, three minutes after it finished.

    Chat-less rows had piled up past the cap unprunable, so the just-announced rows were the
    only ones it could take. The engine reads a poller-bound result on a later wake, so the cap
    must leave it for ordinary retention.
    """
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 3)
    now = time.time()
    _write_jobs(
        tmp_path,
        [
            _row(id=f"chatless-{index}", state=DONE, submitted_at=now - 3600 + index,
                 finished_at=now - 3600 + index, session_key=None)
            for index in range(4)
        ]
        + [
            _row(id="poller", state=DONE, submitted_at=now - 7200, finished_at=now - 60,
                 notified=True, purpose="verification", run_id="gen-2138"),
        ],
    )
    queue = _queue(tmp_path)

    queue.load()

    assert queue.get("poller") is not None
    assert [job.id for job in queue.list()] == ["poller", "chatless-2", "chatless-3"]


def test_the_cap_trims_a_runs_ordinary_job_like_any_other(tmp_path, monkeypatch):
    """#2622: every job a Run's chat queues names the Run, but only a verification row is a
    result the engine still has to read. An ordinary one, once announced, is trimmed as before."""
    monkeypatch.setattr(jobs_module, "MAX_JOBS", 2)
    now = time.time()
    _write_jobs(
        tmp_path,
        [
            _row(id="ordinary", state=DONE, submitted_at=now - 7200, finished_at=now - 60,
                 notified=True, run_id="gen-1"),
            _row(id="verification", state=DONE, submitted_at=now - 7100, finished_at=now - 60,
                 notified=True, purpose="verification", run_id="gen-1"),
            _row(id="newest", state=DONE, submitted_at=now - 60, finished_at=now - 30,
                 notified=True),
        ],
    )
    queue = _queue(tmp_path)

    queue.load()

    assert queue.get("ordinary") is None
    assert {job.id for job in queue.list()} == {"verification", "newest"}


def test_an_unreadable_state_file_starts_empty(tmp_path):
    directory = tmp_path / "jobs"
    directory.mkdir(parents=True)
    (directory / "jobs.json").write_text("{not json")
    queue = _queue(tmp_path)

    assert queue.load() == []
    assert queue.list() == []


# ---- the message -----------------------------------------------------------


def test_the_message_says_what_happened_without_needing_the_log(tmp_path):
    log = tmp_path / "out.log"
    log.write_text("collected 12 items\n1 failed, 11 passed\n")
    job = Job(
        id="abc123",
        command=["pytest", "-q"],
        cwd=str(tmp_path),
        log_path=str(log),
        submitted_at=100.0,
        state=FAILED,
        started_at=100.0,
        finished_at=142.0,
        exit_code=1,
    )
    message = job_message(job)

    assert "abc123" in message
    assert "failed" in message
    assert "Exit code: 1" in message
    assert "42s" in message
    assert "1 failed, 11 passed" in message


# ---- the runtime's own routes ----------------------------------------------


def _runtime_app(tmp_path):
    return create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "runtime-jobs.db")),
            jobs=JobsConfig(state_dir=str(tmp_path / "jobs")),
            accounts=[],
        )
    )


async def test_the_runtime_accepts_lists_and_refuses_unknown_jobs(tmp_path):
    app = _runtime_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        submitted = await client.post(
            "/jobs",
            json={"command": ["true"], "cwd": str(tmp_path), "session_key": None, "label": "smoke"},
        )
        listed = await client.get("/jobs")
        missing = await client.get("/jobs/nope")
        refused = await client.post(
            "/jobs", json={"command": [], "cwd": str(tmp_path), "session_key": None, "label": ""}
        )

    assert submitted.status_code == 200
    assert submitted.json()["state"] == QUEUED
    assert [job["id"] for job in listed.json()["jobs"]] == [submitted.json()["id"]]
    assert missing.status_code == 404
    assert refused.status_code == 422
    app.state.budget_db.close()


async def test_the_topbar_s_read_gets_only_running_and_queued_rows(tmp_path, monkeypatch):
    """#2864: the pill read the whole retained queue every two seconds; `active=1` skips
    finished rows, and the plain read every other caller makes is unchanged."""
    from dataclasses import replace as replace_job

    app = _runtime_app(tmp_path)
    queue = app.state.jobs
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        live = await client.post(
            "/jobs",
            json={"command": ["true"], "cwd": str(tmp_path), "session_key": None, "label": "a"},
        )
        jobs = queue.list()
        finished = replace_job(jobs[0], id="finished-job", state="done")
        monkeypatch.setattr(queue, "list", lambda: [*jobs, finished])
        active = await client.get("/jobs", params={"active": "1"})
        everything = await client.get("/jobs")

    assert [job["id"] for job in active.json()["jobs"]] == [live.json()["id"]]
    assert "heavy_slots" in active.json()
    assert active.json()["memory_waiting"] == 0
    assert [job["id"] for job in everything.json()["jobs"]] == [live.json()["id"], "finished-job"]
    app.state.budget_db.close()


async def test_the_runtime_publishes_who_holds_each_heavy_slot(tmp_path, monkeypatch):
    """The queue read the panel already makes also carries the slot holders,
    so naming them costs no second socket round trip (#1391).

    The pool is redirected under ``tmp_path``: taking the host's real heavy slot
    here would both fight the live queue and fail outright under the conftest
    backstop, which already holds it for this very process.
    """
    monkeypatch.setenv(SLOT_DIR_ENV, str(tmp_path))
    app = _runtime_app(tmp_path)
    slots = app.state.jobs.heavy_slots()
    held = try_take_slot(slots)
    assert held is not None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://runtime"
        ) as client:
            listed = await client.get("/jobs")
    finally:
        os.close(held)

    described = listed.json()["heavy_slots"]
    # This test process took the slot, and nothing in the runtime holds it, so
    # it is exactly the "somebody else" case the panel could not explain.
    assert described[0]["kind"] == "process"
    assert described[0]["pid"] == os.getpid()
    app.state.budget_db.close()


async def test_a_slot_the_queue_holds_is_reported_as_its_job_not_as_the_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(SLOT_DIR_ENV, str(tmp_path))
    app = _runtime_app(tmp_path)
    queue = app.state.jobs
    fd = try_take_slot(queue.heavy_slots())
    assert fd is not None
    try:
        # Stand in for a running job: the queue holds this lock in the runtime's
        # own process, which is what makes /proc alone insufficient.
        queue._running["job1"] = _Running(
            task=asyncio.get_running_loop().create_future(),
            slot_inode=os.fstat(fd).st_ino,
        )
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://runtime"
        ) as client:
            listed = await client.get("/jobs")
    finally:
        queue._running.pop("job1", None)
        os.close(fd)

    described = listed.json()["heavy_slots"]
    assert described[0]["kind"] == "job"
    assert described[0]["job"] == "job1"
    app.state.budget_db.close()


async def test_the_runtime_lists_one_works_rows_when_asked(tmp_path):
    """``/jobs?work_id=`` is what one Work's page reads (#2837): its own jobs, case-folded,
    and every unfinished one, never another Work's finished history or the slot holders."""
    app = _runtime_app(tmp_path)
    work = "github_issue:eerovil/agentdeck#7"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        ids = {}
        for name, work_id in (("mine", work.upper()), ("other", "github_issue:eerovil/agentdeck#8"),
                              ("waiting", "")):
            submitted = await client.post("/jobs", json={
                "command": ["true"], "cwd": str(tmp_path), "session_key": None,
                "purpose": "verification" if work_id else "", "work_id": work_id,
                "run_id": "gen-1" if work_id else "",
            })
            assert submitted.status_code == 200, submitted.text
            ids[name] = submitted.json()["id"]
        for name in ("mine", "other"):
            assert (await client.post(f"/jobs/{ids[name]}/cancel")).status_code == 200
        everything = (await client.get("/jobs")).json()
        scoped = (await client.get("/jobs", params={"work_id": work})).json()

    assert {job["id"] for job in everything["jobs"]} == set(ids.values())
    assert "heavy_slots" in everything
    assert {job["id"] for job in scoped["jobs"]} == {ids["mine"], ids["waiting"]}
    assert "heavy_slots" not in scoped
    app.state.budget_db.close()


def test_rows_for_work_keeps_what_one_works_page_reads():
    from agentdeck.jobs import rows_for_work

    work = "github_issue:eerovil/agentdeck#7"
    rows = [
        {"id": "mine", "state": "done", "work_id": " GITHUB_ISSUE:eerovil/agentdeck#7 "},
        {"id": "follow-up", "state": "failed", "work_id": work, "follows": "mine"},
        {"id": "other", "state": "done", "work_id": "github_issue:eerovil/agentdeck#70"},
        {"id": "other-repo", "state": "done", "work_id": "github_issue:someone/agentdeck#7"},
        {"id": "plain", "state": "cancelled"},
        {"id": "run-only", "state": "interrupted", "run_id": "gen-1"},
        {"id": "queued", "state": "queued", "work_id": "github_issue:eerovil/agentdeck#8"},
        {"id": "running", "state": "running"},
    ]
    kept = [row["id"] for row in rows_for_work(rows, work)]
    assert kept == ["mine", "follow-up", "run-only", "queued", "running"]
    assert rows_for_work(rows, "") == [rows[5], rows[6], rows[7]]


def test_work_rows_give_each_work_what_rows_for_work_gives_it():
    """#2841: the front page slices the queue per Work from one walk of it."""
    from agentdeck.jobs import WorkRows, rows_for_work

    rows = [
        {"id": "mine", "state": "done", "work_id": " GITHUB_ISSUE:eerovil/agentdeck#7 "},
        {"id": "queued", "state": "queued", "work_id": "github_issue:eerovil/agentdeck#8"},
        {"id": "follow-up", "state": "failed", "work_id": "github_issue:eerovil/agentdeck#7"},
        {"id": "other", "state": "done", "work_id": "github_issue:eerovil/agentdeck#70"},
        {"id": "blank", "state": "done", "work_id": "  "},
        {"id": "plain", "state": "cancelled"},
        {"id": "run-only", "state": "interrupted", "run_id": "gen-1"},
        {"id": "running", "state": "running", "work_id": "github_issue:eerovil/agentdeck#7"},
    ]
    sliced = WorkRows(rows)
    for work_id in (
        "github_issue:eerovil/agentdeck#7",
        "GitHub_Issue:eerovil/agentdeck#7 ",
        "github_issue:eerovil/agentdeck#70",
        "github_issue:eerovil/agentdeck#8",
        "github_issue:nobody/else#1",
        "",
        "  ",
    ):
        assert sliced.for_work(work_id) == rows_for_work(rows, work_id), work_id


async def test_a_disabled_queue_answers_404_rather_than_silently_accepting(tmp_path):
    app = create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "runtime-off.db")),
            jobs=JobsConfig(enabled=False, state_dir=str(tmp_path / "jobs")),
            accounts=[],
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        response = await client.post(
            "/jobs", json={"command": ["true"], "cwd": str(tmp_path), "session_key": None}
        )

    assert response.status_code == 404
    app.state.budget_db.close()


# ---- the slot pool itself --------------------------------------------------


def test_the_heavy_slots_follow_the_cores_and_split_them():
    # Two oversubscribed the four-core host badly enough to be worth pinning
    # the narrowed value rather than only comparing against itself (#1304); the
    # low-priority slice lets twelve threads run two at once (#3614).
    assert [default_heavy_slots(cores) for cores in (4, 8, 12, 16)] == [1, 1, 2, 3]
    assert local_suite_workers(cores=12, slots=2) == 6
    assert local_suite_workers(cores=8, slots=1) == 8
    gib = 1024**3
    assert jobs_module.jobs_slice_properties(39 * gib) == [
        "IOWeight=20", f"MemoryHigh={27 * gib}", f"MemoryMax={31 * gib}"
    ]
    # A small host keeps half and three quarters rather than nothing.
    assert jobs_module.jobs_slice_properties(8 * gib)[1:] == [
        f"MemoryHigh={4 * gib}", f"MemoryMax={6 * gib}"
    ]
    assert f"--slice={jobs_module.JOBS_SLICE}" in jobs_module._scope_prefix("abc")


def test_the_heavy_slot_count_defaults_and_takes_an_override(monkeypatch):
    DEFAULT_HEAVY_SLOTS = default_heavy_slots()
    monkeypatch.delenv(HEAVY_SLOT_ENV, raising=False)
    assert heavy_slot_count() == DEFAULT_HEAVY_SLOTS

    monkeypatch.setenv(HEAVY_SLOT_ENV, "5")
    assert heavy_slot_count() == 5

    # A misspelled override falls back rather than refusing to start: this is
    # read from a systemd service and from a pytest hook alike.
    monkeypatch.setenv(HEAVY_SLOT_ENV, "not-a-number")
    assert heavy_slot_count() == DEFAULT_HEAVY_SLOTS
    monkeypatch.setenv(HEAVY_SLOT_ENV, "0")
    assert heavy_slot_count() == DEFAULT_HEAVY_SLOTS


def test_the_kube_pool_is_wider_than_the_local_one_and_separate_from_it(monkeypatch):
    """Remote work is capped by what the cluster can take, not by this host."""
    monkeypatch.delenv(KUBE_SLOT_ENV, raising=False)
    assert kube_slot_count() == DEFAULT_KUBE_SLOTS
    assert DEFAULT_KUBE_SLOTS > default_heavy_slots(8)

    monkeypatch.setenv(KUBE_SLOT_ENV, "2")
    monkeypatch.setenv(HEAVY_SLOT_ENV, "7")
    assert kube_slot_count() == 2
    assert heavy_slot_count() == 7

    monkeypatch.setenv(KUBE_SLOT_ENV, "nonsense")
    assert kube_slot_count() == DEFAULT_KUBE_SLOTS


def test_the_mac_pool_is_sized_for_one_node_and_counted_apart(monkeypatch):
    """Its own pool, because it is its own cluster (#1597).

    Narrower than the dev cluster's: eight cores and 24 GB on one node against
    a pod asking for three cores and 6 GB each.
    """
    monkeypatch.delenv(MAC_SLOT_ENV, raising=False)
    monkeypatch.delenv(KUBE_SLOT_ENV, raising=False)
    assert mac_slot_count() == DEFAULT_MAC_SLOTS
    assert DEFAULT_MAC_SLOTS < DEFAULT_KUBE_SLOTS

    monkeypatch.setenv(MAC_SLOT_ENV, "1")
    assert mac_slot_count() == 1
    assert kube_slot_count() == DEFAULT_KUBE_SLOTS

    monkeypatch.setenv(MAC_SLOT_ENV, "nonsense")
    assert mac_slot_count() == DEFAULT_MAC_SLOTS


def test_the_two_remote_pools_are_different_files(tmp_path, monkeypatch):
    """Sharing a slot file would make one cluster's queue block the other."""
    monkeypatch.setenv(SLOT_DIR_ENV, str(tmp_path))
    assert set(mac_slot_paths()).isdisjoint(kube_slot_paths())


def test_a_pool_hands_out_exactly_its_slots(tmp_path):
    paths = slot_paths("test", 2, tmp_path)
    with hold_slot(paths, timeout=0.1) as first, hold_slot(paths, timeout=0.1) as second:
        assert first and second
        with hold_slot(paths, timeout=0.1) as third:
            # Timing out yields False and runs uncapped rather than hanging.
            assert third is False
    assert try_take_slot(paths) is not None


def test_the_held_marker_is_what_stops_a_nested_run_from_queueing(monkeypatch):
    monkeypatch.delenv(HEAVY_SLOT_HELD_ENV, raising=False)
    assert heavy_slot_is_held() is False

    monkeypatch.setenv(HEAVY_SLOT_HELD_ENV, "1")
    assert heavy_slot_is_held() is True


# ---- the pytest backstop ---------------------------------------------------


class _FakeConfig:
    """Stands in for a pytest Config; `workerinput` marks an xdist worker."""

    def __init__(self, *, worker: bool = False) -> None:
        if worker:
            self.workerinput = {}


def test_the_backstop_skips_xdist_workers_and_anything_already_holding_a_slot(monkeypatch):
    """Only the branches that take no slot are exercised on purpose.

    This test runs *inside* a run that already holds the real slot, and calling
    the acquiring path would overwrite the module global that releases it.
    """
    before = harness._heavy_slot_fd

    monkeypatch.delenv(HEAVY_SLOT_HELD_ENV, raising=False)
    harness._hold_heavy_slot(_FakeConfig(worker=True))
    assert harness._heavy_slot_fd is before

    monkeypatch.setenv(HEAVY_SLOT_HELD_ENV, "1")
    harness._hold_heavy_slot(_FakeConfig())
    assert harness._heavy_slot_fd is before


# ---- per-test timings beside the log (#2297) --------------------------------


async def test_a_job_tells_its_suite_where_to_record_test_timings(tmp_path):
    worktree = _fake_worktree(tmp_path)
    (worktree / "scripts" / "kube_pytest.sh").write_text(
        '#!/bin/bash\necho "$AGENTDECK_TEST_TIMINGS"\n', encoding="utf-8"
    )
    queue = _queue(tmp_path)
    await queue.start()

    final = await _settle(
        queue, queue.submit(command=["pytest"], cwd=str(worktree), lane=DESKTOP).id
    )
    await queue.stop()

    assert Path(final.log_path).read_text().strip() == str(
        Path(final.log_path).with_name(f"{final.id}.tests.jsonl")
    )
