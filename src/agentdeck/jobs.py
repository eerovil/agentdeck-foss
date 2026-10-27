"""A host-wide queue for heavy commands, owned by the persistent runtime (#1287).

The problem is not that agents think too hard, it is that four worktrees run
``pytest -n auto`` at the same moment on a four-core host and everything —
including the deck's own web process — starves. Load average 28 with 4 cores was
the observation that prompted this.

So an agent submits its test command instead of running it:

    agentdeck job submit --cwd <dir> -- pytest -q

and gets a job id straight back. At most ``host_slots.heavy_slot_count()`` jobs
run at once; the rest wait. When one finishes, the runtime delivers a message
into the chat that submitted it, so the agent can do other work — or end its
turn — rather than sitting inside a ten-minute Bash call.

Why the runtime owns it: it already outlives web deploys (which happen every two
minutes) and already knows how to write into a chat. A runtime restart used to
kill running jobs; since #3248 a job runs in a scope of its own and the next
runtime takes it back. Where that is not possible, ``load()`` still turns a job
left ``running`` into ``interrupted`` and says so, instead of leaving a row that
claims to be running a process that no longer exists.

The slot is taken with ``flock`` rather than counted in memory here, because the
cap has to include suites this queue did not start — the conftest backstop takes
one for a direct ``pytest``.

Since #1368 a job also names a *lane*. The default lane runs the command on this
host behind the one heavy slot, as above. The ``kube`` lane instead hands the
suite to ``scripts/kube_pytest.sh``, which runs it in a throwaway pod on a
Kubernetes cluster and streams the local working copy in — so it costs this
host one ``kubectl`` rather than four pytest workers, and several can run at
once. ``mac`` is the same runner pointed at the Mac's own Kubernetes (#1597).
Each lane counts against its own pool, which is the entire point: a remote job
that waited for the local slot would save nobody any time, and a job waiting on
one cluster's queue must not leave the other cluster idle.

Since #3614 the kernel, not the slot count, keeps the deck responsive: every
local job runs in ``JOBS_SLICE``, under the desktop's low-priority
``background.slice``, so the deck, the chats and the desktop get the CPU first
and a job uses what is idle. That lets the slot count follow the cores
(``host_slots.default_heavy_slots``), with free memory as the other gate, and on
a host with ``LOCAL_FIRST_MIN_CORES`` the agentdeck checkout's suites run here
before any pod.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import tomllib
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from . import job_durations, pid_watch, worker_limits
from .context_delivery import RE_DECIDED_REFUSALS
from .flaky_quarantine import Quarantine
from .host_slots import (
    HEAVY_SLOT_HELD_ENV,
    QUEUED_JOB_ENV,
    MemoryGate,
    heavy_slot_paths,
    local_suite_workers,
    try_take_slot,
    usable_cores,
)
from .host_slots import desktop_slot_paths as default_desktop_slot_paths
from .host_slots import kube_slot_paths as default_kube_slot_paths
from .host_slots import mac_slot_paths as default_mac_slot_paths
from .poller_engine import baseline, flaky
from .poller_engine.baseline import extract_failures
from .poller_engine.workspace import (
    WorkspaceSnapshot,
    collect_workspace_snapshot,
    repository_snapshots,
)
from .run_evidence import DEFAULT_DIRNAME as EVIDENCE_DIRNAME
from .run_evidence import RunEvidenceLedger
from .test_timings import TIMINGS_ENV, read_timings, summarize, timings_path_for_log
from .verification_baselines import DEFAULT_FILENAME as BASELINE_FILENAME
from .verification_baselines import BaselineLedger

log = logging.getLogger(__name__)

# How a verification leaves its quarantined tests out (#3055): a standalone pytest plugin
# matching whole test ids, told which ones through the environment. A local run loads it from
# here; a pod runner that knows the plugin-file variable copies it into the pod.
QUARANTINE_PLUGIN = "agentdeck_quarantine"
QUARANTINE_PLUGIN_DIR = Path(__file__).with_name("pytest_plugins")
QUARANTINE_ENV = "AGENTDECK_QUARANTINED_TESTS"
QUARANTINE_PLUGIN_FILE_ENV = "AGENTDECK_QUARANTINE_PLUGIN"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

# The one job purpose there is (#2154): a Run's explicit proof of its tree.
VERIFICATION = "verification"
PURPOSES = ("", VERIFICATION)

FINAL_STATES = frozenset({DONE, FAILED, CANCELLED, INTERRUPTED})


def rows_for_work(rows: Iterable[dict], work_id: str) -> list[dict]:
    """The queue rows one Work's page can use (#2837): a fraction of the whole queue.

    Every unfinished job, because a Work's chats' running jobs are matched by chat and not by
    Work (#2573); every job naming this Work, compared case-folded, which carries its
    verifications and their follow-ups; and a job naming a Run but no Work, which verification
    still credits to that Run. A finished job of another Work, or of none, is never read there.
    """
    wanted = work_id.strip().lower()
    return [
        row
        for row in rows
        if str(row.get("state") or "") not in FINAL_STATES
        or (wanted and str(row.get("work_id") or "").strip().lower() == wanted)
        or (not row.get("work_id") and row.get("run_id"))
    ]


class WorkRows:
    """``rows_for_work`` for many Works from one walk of the queue (#2841).

    The front page builds a card for every Work it shows, and scanning the whole queue again
    for each one grew with the deck's job history. ``for_work`` gives the same rows in the
    same order as ``rows_for_work`` would.
    """

    def __init__(self, rows: Iterable[dict]) -> None:
        self._rows: list[dict] = []
        self._shared: list[int] = []  # every Work's: unfinished, or a Run's without a Work
        self._by_work: dict[str, list[int]] = {}
        for row in rows:
            index = len(self._rows)
            self._rows.append(row)
            if str(row.get("state") or "") not in FINAL_STATES or (
                not row.get("work_id") and row.get("run_id")
            ):
                self._shared.append(index)
                continue
            named = str(row.get("work_id") or "").strip().lower()
            if named:
                self._by_work.setdefault(named, []).append(index)

    def for_work(self, work_id: str) -> list[dict]:
        own = self._by_work.get(work_id.strip().lower(), ()) if work_id.strip() else ()
        if not own:
            return [self._rows[index] for index in self._shared]
        return [self._rows[index] for index in sorted((*self._shared, *own))]

# Where a job runs. ``local`` is this host behind the heavy slot; ``kube``, ``mac`` and
# ``desktop`` are throwaway pods on a Kubernetes cluster each (#1368, #1597, #2297). Which
# of the remote lanes this host has, which cluster profile each asks ``scripts/kube_pytest.sh``
# for and what a person calls it all come from ``job-lanes.toml`` (``lane_settings``); a
# remote lane the file does not list counts as down, so a host with no file runs everything
# here (#3623). A lane's table may list ``aliases``, older names it is still known by.
LOCAL = "local"
KUBE = "kube"
MAC = "mac"
DESKTOP = "desktop"
REMOTE_LANES = (KUBE, MAC, DESKTOP)
LANES = (LOCAL, KUBE, MAC, DESKTOP)
KUBE_PROFILE_ENV = "AGENTDECK_KUBE_PROFILE"

# This host's remote lanes and the other projects that may use them (#3623)::
#
#     [lanes.kube]
#     profile = "dev"                    # the kube_pytest.sh profile; the lane name if absent
#     label = "the dev cluster"          # what messages call it; "the kube lane" if absent
#     aliases = ["cluster"]              # older names `--where` and the down file still take
#
#     [projects.shop]                    # a checkout recognised by the runner it ships
#     runner = "helm/app/jobs/test/run.sh"
#     lanes = ["kube"]                   # where it may run; kube if absent
#     suites = { api = "api" }           # nested checkout -> the suite its runner takes
LANES_FILE_ENV = "AGENTDECK_JOB_LANES"
DEFAULT_LANES_FILE = Path("~/.config/agentdeck/job-lanes.toml")


@dataclass(frozen=True)
class LaneSettings:
    lanes: dict[str, dict[str, str]] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Older name -> lane.
    aliases: dict[str, str] = field(default_factory=dict)


_lane_settings_cache: tuple[object, LaneSettings] | None = None


def lanes_file() -> Path:
    return Path(os.environ.get(LANES_FILE_ENV) or DEFAULT_LANES_FILE).expanduser()


def lane_settings() -> LaneSettings:
    """``job-lanes.toml``, re-read when it changes. Missing or unreadable reads as empty."""
    global _lane_settings_cache
    path = lanes_file()
    try:
        stat = path.stat()
        key: object = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), None)
    if _lane_settings_cache is not None and _lane_settings_cache[0] == key:
        return _lane_settings_cache[1]
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raw = {}
    lanes: dict[str, dict[str, str]] = {}
    aliases: dict[str, str] = {}
    for name, table in (raw.get("lanes") or {}).items():
        lane = str(name).strip().lower()
        if lane in REMOTE_LANES and isinstance(table, dict):
            lanes[lane] = {
                key: str(value) for key, value in table.items() if isinstance(value, str)
            }
            for alias in table.get("aliases") or ():
                if isinstance(alias, str) and alias.strip().lower() not in LANES:
                    aliases[alias.strip().lower()] = lane
    projects: dict[str, dict[str, Any]] = {}
    for name, table in (raw.get("projects") or {}).items():
        if not isinstance(table, dict) or not isinstance(table.get("runner"), str):
            continue
        suites = table.get("suites") if isinstance(table.get("suites"), dict) else {}
        listed = table.get("lanes") if isinstance(table.get("lanes"), list) else [KUBE]
        projects[str(name)] = {
            "runner": table["runner"],
            "lanes": tuple(
                aliases.get(str(lane).strip().lower(), str(lane).strip().lower()) for lane in listed
            ),
            "suites": {str(key): str(value) for key, value in suites.items()},
        }
    settings = LaneSettings(lanes=lanes, projects=projects, aliases=aliases)
    _lane_settings_cache = (key, settings)
    return settings


def canonical_lane(name: str) -> str:
    """A lane's name as the queue keeps it: lowercased, an old name read as the new one."""
    lowered = str(name or "").strip().lower()
    if lowered in LANES or not lowered:
        return lowered
    return lane_settings().aliases.get(lowered, lowered)


def slot_pool_name(lane: str) -> str:
    """The file name of ``lane``'s slot pool: its first older name while it has one.

    A slot is a lock on a file named after its pool, and a job running across a deploy
    keeps the lock it took. Were the pool to follow a renamed lane, that job would hold
    a slot of a pool nobody checks any more and a second job would start beside it
    (#3623). So the pool keeps the old name for as long as the lane lists it.
    """
    older = [name for name, to in lane_settings().aliases.items() if to == lane]
    return older[0] if older else lane


def configured_lanes() -> frozenset[str]:
    return frozenset(lane_settings().lanes)


def lane_label(lane: str) -> str:
    """What a message calls ``lane``."""
    if lane == LOCAL:
        return "this host"
    return lane_settings().lanes.get(lane, {}).get("label") or f"the {lane} lane"


def kube_profile(lane: str) -> str | None:
    """The cluster profile a remote lane asks ``scripts/kube_pytest.sh`` for."""
    if lane not in REMOTE_LANES:
        return None
    return lane_settings().lanes.get(lane, {}).get("profile") or lane


# What ``scripts/kube_pytest.sh`` exits with when the cluster never found room
# for the pod: nothing ran, so it is not a test result (#2170). A desktop can
# also simply be switched off; the script answers both with this exit, because
# in both nothing ran (#2297). A job the queue placed on the desktop that hears
# it moves to the kube lane rather than failing and leaving its chat to resubmit.
KUBE_NO_ROOM_EXIT = 75


def moved_note(home: str, lane: str) -> str:
    """Why a job the queue placed on ``home`` moved to ``lane`` when ``home`` had no room."""
    if home == DESKTOP:
        return (
            f"{lane_label(home)} could not take the pod (no room, or the machine is off), "
            f"so this moved to {lane_label(lane)}"
        )
    return f"{lane_label(home)} had no room, so this ran on {lane_label(lane)} instead"


def spilled_note(home: str, lane: str) -> str:
    """Why a job started on ``lane`` while every slot of ``home`` was taken."""
    return f"{lane_label(home)}'s slot was taken, so this ran on {lane_label(lane)} instead"


def spill_back_note(home: str) -> str:
    """Why a spilled job went back to wait for ``home``: the kube lane had no room."""
    return f"{lane_label(KUBE)} had no room, so this waited for {lane_label(home)} instead"


# Where a job the queue placed goes when its cluster answers "no room": once,
# and only to a lane that can run it. Never to the Mac (#2297): the mac lane
# runs only what names it with `--where mac`, so a full kube cluster fails the
# job as before #2170 rather than sending it there.
#
# An agentdeck suite the queue sent to the kube lane because the desktop is marked
# down (#2835) comes back to this host instead, since its first choice is away.
NO_ROOM_FALLBACK = {DESKTOP: KUBE, KUBE: LOCAL}
# Where a job the queue placed starts instead when every slot of its own lane is
# taken and one of these has room, tried in order. The desktop has one slot, so a
# second agentdeck suite runs on the kube lane now rather than waiting a whole
# suite for it (#2297).
#
# A suite this host took because its own remote lane is marked down (#2826)
# spills the same way: this host has one heavy slot, so a second suite takes the
# kube lane rather than waiting a whole suite for it. On a host big enough to
# run suites first (#3614, ``local_first``) this host spills to the desktop before
# the kube lane instead (``LOCAL_FIRST_SPILL``).
SPILL_LANES: dict[str, tuple[str, ...]] = {DESKTOP: (KUBE,), LOCAL: (KUBE,)}
LOCAL_FIRST_SPILL = (DESKTOP, KUBE)
# From this many usable cores the agentdeck checkout's suites run on this host first
# (#3614). Below it a whole local suite is slower than a pod, so the order stays a pod
# first; the new 12-thread CPU passes it and the old 8-thread one does not. Read through
# the environment so a test, or an owner wanting the old order back, can set it.
LOCAL_FIRST_MIN_CORES_ENV = "AGENTDECK_LOCAL_FIRST_MIN_CORES"
DEFAULT_LOCAL_FIRST_MIN_CORES = 10
# The slice every local job's scope runs in (#3614): a child of the desktop's own
# ``background.slice`` (CPU weight 30 against 100 for apps and 500 for the session), so
# the kernel serves the deck, the chats and the desktop first however many jobs run.
JOBS_SLICE = "background-agentdeck.slice"
JOBS_SLICE_IO_WEIGHT = 20
# The slice's memory: slowed past total minus 12 GB, killed past total minus 8 GB, so a
# runaway suite fails instead of pushing the deck into swap (#1546 lost the host to one).
JOBS_SLICE_HIGH_HEADROOM = 12 * 1024**3
JOBS_SLICE_MAX_HEADROOM = 8 * 1024**3
# A spilled job the kube lane had no room for goes back to wait for its own
# lane rather than failing: nothing ran, and waiting was the other choice all
# along (#2303). A shared cluster can shrink to one node on nights and weekends
# (#2295), so after it says "no room" the queue stops spilling there for a while
# instead of spending another pod wait on a cluster it just heard is full.
SPILL_PAUSE_S = 600.0
# What ``scripts/kube_pytest.sh`` exits with when the connection to the cluster dropped
# before pytest started, or the pod was never created (#2475): nothing ran.
SETUP_LOST_EXIT = 69
# When that log says the cluster itself could not be reached, the queue stops sending work
# there for a while (#3584): a retry on the same lane lands in the same outage, and so does
# every resubmission by hand. Kept beside jobs.json, so a runtime restart remembers it.
UNREACHABLE_SKIP_S = 900.0
UNREACHABLE_FILENAME = "lanes-unreachable.json"
UNREACHABLE_TAIL_BYTES = 64 * 1024
_UNREACHABLE_LOG = re.compile(
    r"dial tcp|i/o timeout|dial timeout|connection refused|TLS handshake timeout"
    r"|Unable to connect to the server",
    re.IGNORECASE,
)

# Lanes the owner has marked down, one per line, each optionally followed
# by the last day it is down (`desktop 2026-10-15`); `#` starts a comment (#2826).
# A desktop can be away for days: while it is marked down, a suite that would
# default there runs on this host instead of first spending a pod wait finding it
# off. Read on every submission and spill, so marking a lane down or back up needs
# no restart; a dated line lapses on its own. `local` may be marked down too
# (#3614): a host that runs suites first then sends them to the pods again, while
# a job no pod can run still runs here. A remote lane ``job-lanes.toml`` does not
# list is down as well (#3623).
LANES_DOWN_ENV = "AGENTDECK_JOB_LANES_DOWN"
DEFAULT_LANES_DOWN_FILE = Path("~/.config/agentdeck/job-lanes-down")


def lanes_down(path: Path | None = None, today: str | None = None) -> frozenset[str]:
    """The lanes marked down today, and every remote lane this host has not set up."""
    unset = frozenset(REMOTE_LANES) - configured_lanes()
    if path is None:
        path = Path(os.environ.get(LANES_DOWN_ENV) or DEFAULT_LANES_DOWN_FILE).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return unset
    today = today or time.strftime("%Y-%m-%d")
    down = set(unset)
    for line in text.splitlines():
        words = line.split("#", 1)[0].split()
        if not words or canonical_lane(words[0]) not in LANES:
            continue
        # An ISO date compares as text; anything else in that place is not a date
        # and leaves the lane down until the line is removed.
        last_day = words[1] if len(words) > 1 else ""
        if len(last_day) == 10 and last_day[4] == "-" and last_day < today:
            continue
        down.add(canonical_lane(words[0]))
    return frozenset(down)


# Who may send work to a remote lane, and what runs it there. The agentdeck checkout
# ships ``scripts/kube_pytest.sh``; any other project is listed in ``job-lanes.toml``
# with the runner it ships (#1383, #3623). Each is recognised by that runner, resolved
# per job rather than from this file, so a worktree tests its own copy of the script.
#
# Another project's own runner is reused rather than reimplemented — it already carries
# the image, the sidecar services and the settings module each suite needs, and a second
# runner beside it would drift from it.
AGENTDECK = "agentdeck"
AGENTDECK_RUNNER = "scripts/kube_pytest.sh"


def kube_runners() -> dict[str, str]:
    """Runner path -> project, the agentdeck checkout's first."""
    runners = {AGENTDECK_RUNNER: AGENTDECK}
    for name, project in lane_settings().projects.items():
        runners.setdefault(project["runner"], name)
    return runners


# A queued job that has waited this long no longer describes work anyone is
# waiting for; the chat that submitted it has usually moved on. Dropped on boot
# rather than started, so a restart does not suddenly run a morning's backlog.
QUEUED_TTL_S = 6 * 3600.0
# Finished rows are kept so `agentdeck job status` still answers after the fact.
FINISHED_RETENTION_S = 24 * 3600.0
MAX_JOBS = 200
MAX_QUEUED = 50
# How much of the output goes into the message delivered to the chat. Enough to
# see a failing assertion, not enough to bury the turn.
NOTIFY_TAIL_CHARS = 3000
# How often a running job's chat hears how it is going (#3038), and how much of the log that
# message carries: less than the completion's, because it arrives every few minutes.
PROGRESS_INTERVAL_S = 300.0
PROGRESS_TAIL_CHARS = 2000
# How long a job stopped for its time limit gets to exit on SIGTERM before SIGKILL: long
# enough for kube_pytest.sh to delete its pod, short enough that a wedged tree does not hold
# its slot past the limit for long.
KILL_GRACE_S = 30.0
# How much of the end of a log is read to find the failure summary (#1737). Far more than the
# message tail, because six thousand tests can name a lot of failures — and still bounded,
# because a run whose summary does not fit simply reports an incomplete list and never compares.
FAILURE_SUMMARY_CHARS = 64_000
# A missing worker can be transient during recovery, but retrying a permanently
# removed account every dispatch tick would spend a day producing log noise.
NOTIFY_RETRY_S = 60.0
# ...and a refusal that never recovers has to stop somewhere, or the row is
# retried forever and can never be pruned. Generous enough to cover a runtime
# restart plus an evicted worker being revived.
NOTIFY_GIVE_UP_S = 3600.0
# The deadline above only judges refusals about the *chat*. A refusal that
# describes the host at the moment of asking -- every worker slot taken, above
# all -- says nothing about whether the chat can be reached, and waking an idle
# chat needs a free slot: a busy deck can refuse `at_capacity` for hours on end
# (#1560 watched four). Spending the deadline on that throws away a finished
# suite's result and leaves the chat waiting for a message that will now never
# come (#1610). So those refusals get their own, far longer ceiling, which
# exists only so a row cannot be retried for the life of the runtime -- the leak
# #1299 closed.
NOTIFY_HOST_BUSY_GIVE_UP_S = 24 * 3600.0
# Environment the child must not inherit: it would make a test suite act as the
# chat that submitted it, and several tests assert these are absent from a spawn.
_STRIPPED_ENV = ("AGENTDECK_SESSION_KEY", "AGENTDECK_URL")


class JobError(ValueError):
    """A submission that cannot be accepted."""


@dataclass(frozen=True)
class Job:
    id: str
    command: list[str]
    cwd: str
    log_path: str
    submitted_at: float
    state: str = QUEUED
    lane: str = LOCAL
    # Whether the queue picked the lane because the submission named none. Only
    # then may a full cluster move the job elsewhere (#2170): an explicit
    # `--where kube` is obeyed, refusal and all.
    lane_defaulted: bool = False
    # The lane it was submitted to, once a full cluster has moved it off that
    # one; empty while it is still where it was sent. `lane` is where it runs
    # now, this is what the submitter asked for, and a replayed submission is
    # matched against the latter.
    moved_from_lane: str = ""
    # Whether that move was a spill: started on another lane only because every slot of its
    # own was taken. Kept as state rather than read off ``note``, which a run appends to
    # (#3614).
    spilled: bool = False
    session_key: str | None = None
    label: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    note: str = ""
    notified: bool = False
    # A stopwatch for how long this completion has been refused for reasons
    # about the *chat* rather than about the host (#1610): seconds already on the
    # clock, and when it was last running -- `None` while it is paused, which is
    # what a host-state refusal does to it. Durable, because the deadline they
    # measure has to survive the runtime restart it exists to outlast.
    chat_refused_for: float = 0.0
    chat_refused_at: float | None = None
    # What the job is for, and which Work/Run it is evidence for (#2154). Only ``verification``
    # means anything: a Run's explicit proof that its tree passes, which the poller credits to
    # the exact commit the job tested. Empty for every ordinary job.
    purpose: str = ""
    work_id: str = ""
    run_id: str = ""
    # Other checkouts a verification job tests besides ``cwd`` (#2148): a Work can span more
    # than one repository, and the evidence has to name every one the command relied on.
    workspaces: list[str] = field(default_factory=list)
    tested_workspace: dict[str, Any] | None = None
    # The same workspace observed again after a successful verification command exited
    # (#2154). ``tested_workspace`` is read before the command starts, so on its own it cannot
    # tell a tree that held still from one edited or re-committed while the suite ran; only a
    # matching second reading proves the command saw the tree the first one names.
    verified_workspace: dict[str, Any] | None = None
    # A verification job's evidence keyed by repository (#2148): ``cwd`` and every entry of
    # ``workspaces``, before the command and again after it succeeded. The singular fields
    # above are still written, for the one checkout the job ran in, because the baseline
    # ledger reads them and a row from before this reads only them.
    tested_workspaces: dict[str, dict[str, Any]] | None = None
    verified_workspaces: dict[str, dict[str, Any]] | None = None
    result_id: str = ""
    result_class: str = ""
    output_bytes: int = 0
    output_sha256: str = ""
    diagnostic_excerpt: str = ""
    # What this run actually failed, and what it ran against (#1737). The failure list is
    # extracted here because this is where the log is; whether it means a regression is decided
    # by the poller engine, which never touches a file.
    environment: str = ""
    failures: tuple[str, ...] = ()
    failures_complete: bool = False
    # A job the queue submitted by itself for a verification (#2382): ``follows`` is that
    # verification's id and ``follow_kind`` says what for -- a rerun of its failing tests on the
    # same commit, a run of them on its merge base, or a resubmission of a run that ended with
    # no test result. Empty for every job someone submitted.
    follows: str = ""
    follow_kind: str = ""
    # The test ids a rerun or merge-base follow-up ran: the queue chose them, so it records
    # them rather than anyone inferring them from the command later.
    follow_tests: tuple[str, ...] = ()
    # Which of a failed verification's failing tests already exist on its merge base (#2552).
    # A test the branch added is its own, so the poller never forgives it; None means nobody
    # could tell, which forgives nothing either.
    base_present: tuple[str, ...] | None = None
    # The running time the submitter asked for with ``--max-minutes`` (#3038): None lets the
    # queue learn one from the project's past runs, 0 means no limit. The limit actually
    # applied, and why, is fixed when the job starts.
    max_minutes: int | None = None
    time_limit_s: float | None = None
    time_limit_basis: str = ""
    # The quarantined tests this verification left out (#3055), fixed when it starts.
    quarantined: tuple[str, ...] = ()
    # The Work of the chat that submitted it, kept for every job (``work_id`` is kept only
    # where it means something to the poller): it orders a starred Work's jobs first (#3116).
    owner_work_id: str = ""
    # The running process, kept so the next runtime can take the job back instead of calling
    # it cut off (#3248): its pid, the kernel's start time for that pid (so a recycled number
    # is never mistaken for it), the file its wrapper writes the exit code to, and the slot
    # file's inode. ``exit_path`` is empty for a job started where it could not outlive the
    # runtime, and such a job is still interrupted by a restart.
    pid: int | None = None
    pid_start: int | None = None
    exit_path: str = ""
    slot_inode: int | None = None

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES

    @property
    def display(self) -> str:
        return self.label or " ".join(self.command)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Job | None:
        """Rebuild one row, or None if it is not a job this version understands.

        A durable file outlives the code that wrote it; a row that lost a field
        is dropped rather than allowed to crash the runtime's boot.
        """
        try:
            command = [str(part) for part in raw["command"]]
            return cls(
                id=str(raw["id"]),
                command=command,
                cwd=str(raw["cwd"]),
                log_path=str(raw["log_path"]),
                submitted_at=float(raw["submitted_at"]),
                state=str(raw.get("state", QUEUED)),
                # A row written before #1368 has no lane, and it ran here.
                lane=canonical_lane(str(raw.get("lane", LOCAL))) or LOCAL,
                # Absent before #2170, which reads as a lane the submitter chose.
                lane_defaulted=bool(raw.get("lane_defaulted", False)),
                moved_from_lane=canonical_lane(str(raw.get("moved_from_lane", ""))),
                spilled=bool(raw.get("spilled", False)),
                session_key=raw.get("session_key") or None,
                label=str(raw.get("label", "")),
                started_at=_optional_float(raw.get("started_at")),
                finished_at=_optional_float(raw.get("finished_at")),
                exit_code=_optional_int(raw.get("exit_code")),
                note=str(raw.get("note", "")),
                notified=bool(raw.get("notified", False)),
                # Absent from any row written before #1610: that job simply
                # starts its chat budget at its next chat-scoped refusal.
                chat_refused_for=float(raw.get("chat_refused_for", 0.0) or 0.0),
                chat_refused_at=_optional_float(raw.get("chat_refused_at")),
                purpose=str(raw.get("purpose", "")),
                work_id=str(raw.get("work_id", "")),
                run_id=str(raw.get("run_id", "")),
                workspaces=[str(path) for path in raw.get("workspaces") or ()],
                tested_workspace=(
                    dict(raw["tested_workspace"])
                    if isinstance(raw.get("tested_workspace"), dict)
                    else None
                ),
                verified_workspace=(
                    dict(raw["verified_workspace"])
                    if isinstance(raw.get("verified_workspace"), dict)
                    else None
                ),
                tested_workspaces=_keyed_records(raw.get("tested_workspaces")),
                verified_workspaces=_keyed_records(raw.get("verified_workspaces")),
                result_id=str(raw.get("result_id", "")),
                result_class=str(raw.get("result_class", "")),
                output_bytes=int(raw.get("output_bytes", 0) or 0),
                output_sha256=str(raw.get("output_sha256", "")),
                diagnostic_excerpt=str(raw.get("diagnostic_excerpt", "")),
                # Absent from any row written before #1737, which reads as a run whose
                # failures were never extracted -- so it simply never compares.
                environment=str(raw.get("environment", "")),
                failures=tuple(str(item) for item in raw.get("failures") or ()),
                failures_complete=bool(raw.get("failures_complete", False)),
                follows=str(raw.get("follows", "")),
                follow_kind=str(raw.get("follow_kind", "")),
                # Absent before #3116: such a job simply never counts as starred.
                owner_work_id=str(raw.get("owner_work_id", "")),
                follow_tests=tuple(str(item) for item in raw.get("follow_tests") or ()),
                base_present=(
                    tuple(str(item) for item in raw["base_present"])
                    if isinstance(raw.get("base_present"), list)
                    else None
                ),
                # Absent before #3038: the queue learns a limit, as for any job that named none.
                max_minutes=_optional_int(raw.get("max_minutes")),
                time_limit_s=_optional_float(raw.get("time_limit_s")),
                time_limit_basis=str(raw.get("time_limit_basis", "")),
                # Absent before #3055: nothing was left out.
                quarantined=tuple(str(item) for item in raw.get("quarantined") or ()),
                # Absent before #3248: a running row without them is cut off, as it was then.
                pid=_optional_int(raw.get("pid")),
                pid_start=_optional_int(raw.get("pid_start")),
                exit_path=str(raw.get("exit_path", "")),
                slot_inode=_optional_int(raw.get("slot_inode")),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class NotifyOutcome:
    """Whether a completion reached its chat, and if not, what refused it.

    The reason is what separates "that chat is gone" from "the host had no room
    to wake it just now", which the queue has to tell apart before it decides to
    stop trying. A notifier may still answer with a bare bool; that reads as a
    refusal about the chat, which is the older and stricter of the two.
    """

    delivered: bool
    reason: str = ""


Notifier = Callable[[Job, str], Awaitable[bool | NotifyOutcome]]
# A running job's progress message, numbered from 1 so each has its own delivery id (#3038).
ProgressNotifier = Callable[[Job, str, int], Awaitable[bool | NotifyOutcome]]


def _notify_outcome(value: bool | NotifyOutcome) -> NotifyOutcome:
    return value if isinstance(value, NotifyOutcome) else NotifyOutcome(bool(value))


@dataclass
class _Running:
    task: asyncio.Task
    process: asyncio.subprocess.Process | None = None
    cancelled: bool = field(default=False)
    # The inode of the slot file this job locked, so the queue can say which of
    # the host's slots are its own (#1391). The lock lives in this process, so
    # /proc alone cannot tell a queued job apart from anything else the runtime
    # holds.
    slot_inode: int | None = None


def _prunable(job: Job) -> bool:
    """A finished row with nobody left to tell.

    ``notified`` is what keeps a pending completion retry alive (#1299), but a job submitted
    without a chat has no retry to keep: nothing ever marks it notified. Counting only notified
    rows let those pile up past ``MAX_JOBS`` for good, and from then on the cap trimmed the only
    rows it was allowed to — each announced result, minutes after it finished (#2162).
    """
    return job.is_final and (job.notified or not job.session_key)


class JobQueue:
    """Durable FIFO of heavy commands, capped by the host-wide heavy slots."""

    def __init__(
        self,
        *,
        state_dir: Path,
        notify: Notifier | None = None,
        slot_paths: list[Path] | None = None,
        kube_slot_paths: list[Path] | None = None,
        mac_slot_paths: list[Path] | None = None,
        desktop_slot_paths: list[Path] | None = None,
        baselines: BaselineLedger | None = None,
        evidence: RunEvidenceLedger | None = None,
        quarantine: Quarantine | None = None,
        poll_s: float = 1.0,
        notify_retry_s: float = NOTIFY_RETRY_S,
        notify_give_up_s: float = NOTIFY_GIVE_UP_S,
        notify_host_busy_give_up_s: float = NOTIFY_HOST_BUSY_GIVE_UP_S,
        notify_progress: ProgressNotifier | None = None,
        progress_interval_s: float = PROGRESS_INTERVAL_S,
        kill_grace_s: float = KILL_GRACE_S,
        clock: Callable[[], float] = time.time,
        starred: Callable[[], Collection[str]] | None = None,
        survive_restart: bool = False,
        memory_gate: MemoryGate | None = None,
        configure_slice: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._dir = Path(state_dir)
        # Whether this host has the memory for one more local job (#3614), shared with the
        # slot leases, and how many local jobs the last sweep held back for lack of it, which
        # the job panel says.
        self._memory = memory_gate or MemoryGate()
        self.memory_waiting = 0
        # Sets up the slice local jobs run in (``configure_jobs_slice``); the runtime passes
        # it, so a test's queue never changes the host's systemd settings.
        self._configure_slice = configure_slice
        # Whether running jobs outlive this queue's process (#3248): the runtime sets it, so a
        # restart no longer cuts off a suite. Off by default, so a test's queue never leaves a
        # process behind.
        self._survive_restart = survive_restart
        self._detaching = False
        self._resumable: list[str] = []
        # The starred Works' ids (#3116), read once per sweep; their jobs start first.
        self._starred = starred
        self._path = self._dir / "jobs.json"
        self._log_dir = self._dir / "logs"
        self._notify = notify
        self._slot_paths = slot_paths
        self._kube_slot_paths = kube_slot_paths
        self._mac_slot_paths = mac_slot_paths
        self._desktop_slot_paths = desktop_slot_paths
        self._baselines = baselines or BaselineLedger(self._dir / BASELINE_FILENAME)
        self._evidence = evidence or RunEvidenceLedger(self._dir / EVIDENCE_DIRNAME)
        # Which tests a verification leaves out until their fix issue closes (#3055); None
        # leaves nothing out.
        self._quarantine = quarantine
        self._durations = job_durations.DurationLedger(self._dir / job_durations.DEFAULT_FILENAME)
        self._notify_progress = notify_progress
        self._progress_interval_s = progress_interval_s
        self._kill_grace_s = kill_grace_s
        # Progress deliveries in flight, held so they are not collected half-way.
        self._progress_tasks: set[asyncio.Task] = set()
        self._poll_s = poll_s
        self._notify_retry_s = notify_retry_s
        self._notify_give_up_s = notify_give_up_s
        self._notify_host_busy_give_up_s = notify_host_busy_give_up_s
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._boot_interrupted: list[str] = []
        self._running: dict[str, _Running] = {}
        self._announcing: set[str] = set()
        self._notify_after: dict[str, float] = {}
        self._spill_paused_until: dict[str, float] = {}
        # Remote lane -> when the queue may send work there again (#3584).
        self._unreachable_path = self._dir / UNREACHABLE_FILENAME
        self._unreachable_until = _read_unreachable(self._unreachable_path)
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # Told after every save, which is every change to a row: the runtime's change
        # feed passes it on so the web reads the queue only when it moved (#2865).
        self.on_change: Callable[[], None] = lambda: None

    # ---- durable state -------------------------------------------------

    def _slots(self, lane: str = LOCAL) -> list[Path]:
        if lane == KUBE:
            return (
                default_kube_slot_paths()
                if self._kube_slot_paths is None
                else self._kube_slot_paths
            )
        if lane == MAC:
            return (
                default_mac_slot_paths()
                if self._mac_slot_paths is None
                else self._mac_slot_paths
            )
        if lane == DESKTOP:
            return (
                default_desktop_slot_paths(name=slot_pool_name(DESKTOP))
                if self._desktop_slot_paths is None
                else self._desktop_slot_paths
            )
        return heavy_slot_paths() if self._slot_paths is None else self._slot_paths

    def running_jobs_a_restart_would_cut(self) -> int:
        """Running jobs that die with the runtime: started where they could not outlive it."""
        return sum(
            1
            for job_id in self._running
            if (job := self._jobs.get(job_id)) is not None and not job.exit_path
        )

    def heavy_slots(self) -> list[Path]:
        """The local slot files this queue competes for."""
        return self._slots(LOCAL)

    def held_slot_inodes(self) -> dict[int, str]:
        """Slot inode -> job id, for every slot this queue is currently holding.

        A kube job's inode is in here too and simply matches no heavy slot,
        which is the correct answer for a lane that takes a different pool.
        """
        return {
            entry.slot_inode: job_id
            for job_id, entry in self._running.items()
            if entry.slot_inode is not None
        }

    def load(self) -> list[Job]:
        """Read durable state and return every completion still needing delivery.

        That includes jobs interrupted by the previous runtime and final rows
        whose earlier notification attempt was unavailable. The delivery id is
        stable, so replaying one of these rows cannot write the message twice.
        """
        self._jobs = {}
        self._boot_interrupted = []
        self._resumable = []
        raw = self._read()
        for item in raw:
            job = Job.from_dict(item) if isinstance(item, dict) else None
            if job is not None:
                self._jobs[job.id] = job
        now = self._clock()
        for job_id, job in list(self._jobs.items()):
            if job.state == RUNNING and self._survive_restart and _still_running(job):
                # The previous runtime left it running on purpose; take it back (#3248).
                self._resumable.append(job_id)
            elif job.state == RUNNING:
                # Nothing is running it: this process just started.
                self._boot_interrupted.append(job_id)
                self._jobs[job_id] = replace(
                    job,
                    state=INTERRUPTED,
                    finished_at=now,
                    note="the runtime restarted while this job was running",
                )
            elif job.state == QUEUED and now - job.submitted_at > QUEUED_TTL_S:
                self._jobs[job_id] = replace(
                    job,
                    state=INTERRUPTED,
                    finished_at=now,
                    note="waited too long for a slot and was dropped",
                )
        self._prune(now)
        self._save()
        # The first runtime with time limits learns from the passed runs it still holds, so it
        # does not give every project the unknown-project hour until twenty more have passed.
        self._durations.seed(
            (_duration_key(job), job.id, job.finished_at - job.started_at)
            for job in self._ordered()
            if _counts_as_duration(job)
        )
        return [
            job
            for job in self._ordered()
            if job.is_final and job.session_key and not job.notified
        ]

    def _read(self) -> list[Any]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            log.exception("jobs: could not read %s", self._path)
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            log.warning("jobs: %s is not valid JSON; starting empty", self._path)
            return []
        return payload if isinstance(payload, list) else []

    def _save(self) -> bool:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = [job.to_dict() for job in self._ordered()]
        temporary = self._path.with_suffix(".json.tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(temporary, self._path)
            self.on_change()
            return True
        except OSError:
            log.exception("jobs: could not write %s", self._path)
            return False

    def _prune(self, now: float) -> None:
        def age(job: Job) -> float:
            return now - (job.finished_at or job.submitted_at)

        stale = [
            job.id
            for job in self._jobs.values()
            if _prunable(job) and age(job) > FINISHED_RETENTION_S
        ]
        for job_id in stale:
            self._forget(job_id)
        surplus = len(self._jobs) - MAX_JOBS
        if surplus > 0:
            # A poller-bound verification row is the one result somebody else still has to read:
            # the engine consumes it on a later wake, not when the chat is told. Trimming it to
            # make room stranded #2138 on a passed suite the queue had already forgotten (#2162),
            # so the cap only reaches it once ordinary retention would have anyway. An ordinary
            # job of a Run is never evidence, so the cap trims it like any other (#2622).
            oldest_final = [
                job
                for job in self._ordered()
                if _prunable(job) and not (_is_verification(job) and job.run_id)
            ][:surplus]
            for job in oldest_final:
                self._forget(job.id)

    def _forget(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        self._notify_after.pop(job_id, None)
        if job is None:
            return
        for path in (Path(job.log_path), timings_path_for_log(job.log_path)):
            with contextlib.suppress(OSError):
                path.unlink()

    def _ordered(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda job: job.submitted_at)

    def waiting(self) -> list[Job]:
        """The queued jobs in the order they start: a failed verification's reruns first
        (``_jumps_the_queue``), then a starred Work's jobs (#3116), then verifications
        (#3611), each by submission.

        A verification is what a merge waits on; a plain job (a render, an errand) never is.
        One chat's thirty renders held three Works' zero-second checks, and so their merges,
        for two and a half hours on this host's one slot."""
        queued = [job for job in self._ordered() if job.state == QUEUED]
        starred: frozenset[str] = frozenset()
        if len(queued) > 1 and self._starred is not None:
            try:
                starred = frozenset(work.casefold() for work in self._starred())
            except Exception:  # noqa: BLE001 -- a star only reorders; unreadable is none
                log.debug("jobs: starred Works unreadable", exc_info=True)

        def place(job: Job) -> tuple[bool, bool, bool]:
            work = (job.owner_work_id or job.work_id).casefold()
            return (
                not _jumps_the_queue(job),
                not (work and work in starred),
                job.purpose != VERIFICATION,
            )

        return sorted(queued, key=place)

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        interrupted = await asyncio.to_thread(self.load)
        for job_id in list(self._boot_interrupted):
            # A verification the restart cut off is resubmitted once (#2382).
            await self._follow_up(job_id)
        for job in interrupted:
            await self._announce(self._jobs.get(job.id, job))
        for job_id in self._resumable:
            job = self._jobs[job_id]
            log.info("jobs: took back %s (pid %s) from the previous runtime", job_id, job.pid)
            self._running[job_id] = _Running(
                task=asyncio.create_task(self._resume(job), name=f"job-{job_id}"),
                slot_inode=job.slot_inode,
            )
        self._resumable = []
        if self._configure_slice is not None:
            await self._configure_slice()
        self._loop_task = asyncio.create_task(self._dispatch_loop(), name="job-queue")

    async def stop(self) -> None:
        self._detaching = self._survive_restart
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        for task in list(self._progress_tasks):
            task.cancel()
        for entry in list(self._running.values()):
            entry.task.cancel()
        if self._running:
            await asyncio.gather(
                *(entry.task for entry in self._running.values()), return_exceptions=True
            )

    # ---- public API ----------------------------------------------------

    def submit(
        self,
        *,
        command: Iterable[str],
        cwd: str,
        lane: str = LOCAL,
        session_key: str | None = None,
        label: str = "",
        lane_defaulted: bool = False,
        purpose: str = "",
        work_id: str = "",
        run_id: str = "",
        workspaces: Iterable[str] = (),
        follows: str = "",
        follow_kind: str = "",
        follow_tests: Iterable[str] = (),
        max_minutes: int | None = None,
    ) -> Job:
        argv = [str(part) for part in command]
        if not argv:
            raise JobError("a command is required")
        if max_minutes is not None and max_minutes < 0:
            raise JobError("max_minutes must be 0 (no limit) or more")
        if purpose not in PURPOSES:
            raise JobError(f"unknown job purpose {purpose!r}")
        if purpose == VERIFICATION and not run_id and not work_id:
            # Evidence for no Run and no Work is evidence for nothing; refused while the
            # submitter can still act on it rather than queued as a result nothing will read.
            # A Work the deck releases has no Run, so its Work alone is enough (#2895).
            raise JobError("a verification job must name the Run or the Work it verifies")
        lane = canonical_lane(lane)
        if lane not in LANES:
            raise JobError(f"unknown lane {lane!r}; expected one of {', '.join(LANES)}")
        if lane in REMOTE_LANES and lane not in configured_lanes():
            raise JobError(
                f"the {lane} lane is not set up on this host; list it under [lanes.{lane}] "
                f"in {lanes_file()}, or submit without --where to run here"
            )
        directory = Path(cwd).expanduser()
        if not directory.is_dir():
            raise JobError(f"working directory does not exist: {cwd}")
        note = ""
        if lane in self._unreachable_lanes():
            # The submitter chose this lane before the queue heard the cluster was
            # unreachable (#3584). One the queue chose goes elsewhere; one named is kept.
            until = _clock_time(self._unreachable_until[lane])
            if lane_defaulted:
                moved = default_lane(str(directory), argv, self._skipped_lanes())
                note = (
                    f"the {lane} cluster was unreachable, so this went to {moved}; "
                    f"the queue skips {lane} until {until}"
                )
                lane = moved
            else:
                note = (
                    f"the {lane} cluster was unreachable a moment ago and the queue skips it "
                    f"until {until}; this job named {lane}, so it waits for it there"
                )
        extra = _extra_workspaces(workspaces, directory, purpose=purpose, lane=lane)
        if lane in REMOTE_LANES:
            # Refuse at submission rather than at dispatch: the submitter is
            # still at its terminal now, and in ten minutes it is not.
            _kube_argv(str(directory), argv, lane)
        else:
            # Same reason, for the same kind of fault: a workspace with no
            # interpreter is a refusal the submitter can act on, where the run it
            # would otherwise get back is an exit code that says nothing (#1635).
            _local_argv(str(directory), argv)
        queued = sum(1 for job in self._jobs.values() if job.state == QUEUED)
        if queued >= MAX_QUEUED:
            raise JobError(f"the queue already holds {queued} waiting jobs")
        job_id = uuid.uuid4().hex[:12]
        self._log_dir.mkdir(parents=True, exist_ok=True)
        job = Job(
            id=job_id,
            command=argv,
            cwd=str(directory),
            log_path=str(self._log_dir / f"{job_id}.log"),
            submitted_at=self._clock(),
            lane=lane,
            lane_defaulted=lane_defaulted,
            session_key=session_key or None,
            label=label,
            purpose=purpose,
            # An ordinary job keeps its Run too: it is never evidence (only ``purpose`` makes
            # it that), but the poller holds a card while its worker waits on it (#2622).
            work_id=work_id if purpose or follows or run_id else "",
            owner_work_id=work_id,
            run_id=run_id,
            workspaces=extra,
            follows=follows,
            follow_kind=follow_kind,
            follow_tests=tuple(str(test) for test in follow_tests),
            max_minutes=max_minutes,
            note=note,
        )
        self._jobs[job_id] = job
        if not self._save():
            self._jobs.pop(job_id, None)
            raise JobError("the job could not be persisted")
        self._wake.set()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def evidence(self, *, work_id: str = "") -> list[dict]:
        """Every finished verification job's evidence, kept past the queue's retention."""
        return self._evidence.rows(work_id=work_id)

    def list(self) -> list[Job]:
        return self._ordered()

    async def cancel(self, job_id: str) -> Job | None:
        """Cancel a queued job, or terminate a running one. None if unknown."""
        job = self._jobs.get(job_id)
        if job is None or job.is_final:
            return job
        entry = self._running.get(job_id)
        if entry is None:
            self._jobs[job_id] = replace(
                job, state=CANCELLED, finished_at=self._clock(), note="cancelled while queued"
            )
            self._save()
            return self._jobs[job_id]
        entry.cancelled = True
        process = entry.process
        if process is not None and process.returncode is None:
            _terminate(process)
        return self._jobs.get(job_id)

    async def silence(self, session_keys: Iterable[str], since: float) -> list[Job]:
        """Cancel, without a word to the chat, its jobs submitted at or after ``since``.

        A person rolled the chat back to before that moment (#3474), so the agent no
        longer remembers asking for them: a running or queued one is cancelled, a
        finished one not yet announced is never announced, and the chat hears of none.
        """
        keys = {key for key in session_keys if key}
        silenced: list[Job] = []
        for job in list(self._ordered()):
            if job.session_key not in keys or job.submitted_at < since or job.notified:
                continue
            # Marked told first: a running job's own finish keeps the mark.
            self._jobs[job.id] = replace(job, notified=True)
            self._notify_after.pop(job.id, None)
            silenced.append(self._jobs[job.id])
        if silenced:
            self._save()
        for job in silenced:
            if not job.is_final:
                await self.cancel(job.id)
        return [self._jobs.get(job.id, job) for job in silenced]

    # ---- dispatch ------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                self._start_what_fits()
                await self._announce_pending()
            except Exception:  # noqa: BLE001 -- the queue must outlive one bad job
                log.exception("jobs: dispatch failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            self._wake.clear()

    def _start_what_fits(self) -> None:
        """Start queued jobs while their lane has a free slot.

        Polled rather than blocking on the lock: the slots are shared with
        pytest processes this queue never sees, so "can I start now" is a
        question about the whole host and the only honest way to ask it without
        tying up a thread is to try, and try again a second later.

        A lane with no free slot is skipped, not returned from: the queue is one
        FIFO across both lanes, and a local job waiting for the single heavy slot
        must not hold up the remote jobs behind it — that would leave the cluster
        idle for exactly the reason the remote lane exists.

        A same-commit rerun or merge-base check of a failed verification goes first
        (``_jumps_the_queue``), then a starred Work's jobs, then verifications ahead of plain
        jobs (``waiting``); otherwise it is first come, first served.

        A job whose own lane is full may start on one of its spill lanes instead,
        tried in order (``_spill_order``); it is asked again every sweep, so it
        takes whichever frees up first.
        """
        waiting = self.waiting()
        full: set[str] = set()
        short = False
        skipped = self._skipped_lanes()
        for job in waiting:
            if job.id in self._running:
                continue
            fd = None
            # This host marked down (#3614): a suite the queue placed here goes to a pod and
            # waits for one rather than taking a local slot. A job no pod can run still runs
            # here.
            to_a_pod = job.lane == LOCAL and LOCAL in skipped and bool(_spill_lanes(job, skipped))
            if not to_a_pod and job.lane == LOCAL and LOCAL not in full and not self._memory.ok():
                # This host counts as full while it is short of memory, however many slots
                # are free (#3614).
                full.add(LOCAL)
                short = True
            if not to_a_pod and job.lane not in full:
                fd = try_take_slot(self._slots(job.lane))
                if fd is None:
                    full.add(job.lane)
                elif job.lane == LOCAL:
                    self._memory.admit()
            if fd is None:
                home = job.lane
                for spill in _spill_lanes(job, skipped):
                    if spill in full or self._spill_paused(spill):
                        continue
                    fd = try_take_slot(self._slots(spill))
                    if fd is None:
                        full.add(spill)
                        continue
                    job = self._move_lane(
                        job.id, spill, spilled_note(home, spill), spilled=True
                    )
                    break
            if fd is None:
                if len(full) >= len(LANES):
                    break
                continue
            try:
                slot_inode = os.fstat(fd).st_ino
            except OSError:  # pragma: no cover - the fd was just opened
                slot_inode = None
            self._running[job.id] = _Running(
                task=asyncio.create_task(self._run(job, fd), name=f"job-{job.id}"),
                slot_inode=slot_inode,
            )
        held = (
            sum(1 for job in self.waiting() if job.lane == LOCAL and job.id not in self._running)
            if short
            else 0
        )
        if held != self.memory_waiting:
            self.memory_waiting = held
            self.on_change()  # the panel says why, and it reads the queue only on a change

    def _unreachable_lanes(self) -> frozenset[str]:
        """The remote lanes found unreachable less than ``UNREACHABLE_SKIP_S`` ago (#3584)."""
        now = self._clock()
        return frozenset(lane for lane, until in self._unreachable_until.items() if now < until)

    def _skipped_lanes(self) -> frozenset[str]:
        """The lanes nothing the queue places goes to now: marked down by the owner (#2826),
        or found unreachable a moment ago (#3584). Every placement the queue makes asks this."""
        return lanes_down() | self._unreachable_lanes()

    def _mark_if_unreachable(self, job: Job, code: int | None) -> float | None:
        """Skip ``job``'s lane for a while if it ended because its cluster was unreachable.

        Returns when the lane may be used again, or None when this was not that.
        """
        if code != SETUP_LOST_EXIT or job.lane not in REMOTE_LANES:
            return None
        if not _UNREACHABLE_LOG.search(_log_tail(job.log_path, UNREACHABLE_TAIL_BYTES)):
            return None
        until = self._clock() + UNREACHABLE_SKIP_S
        self._unreachable_until[job.lane] = until
        log.warning("jobs: the %s lane was unreachable; skipping it until %s", job.lane, until)
        payload = json.dumps(self._unreachable_until, indent=1)
        temporary = self._unreachable_path.with_suffix(".json.tmp")
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self._unreachable_path)
        except OSError:
            log.exception("jobs: could not write %s", self._unreachable_path)
        return until

    def _spill_paused(self, lane: str) -> bool:
        return self._clock() < self._spill_paused_until.get(lane, 0.0)

    async def _run(self, job: Job, slot_fd: int) -> None:
        entry = self._running[job.id]
        started = self._clock()
        tested, tested_all = await asyncio.to_thread(_snapshots, job)
        job = replace(
            job,
            state=RUNNING,
            started_at=started,
            tested_workspace=tested.as_record(),
            tested_workspaces=tested_all,
            environment=environment_identity(job.lane, tested.root or job.cwd),
        )
        limit = await asyncio.to_thread(self._time_limit, job)
        job = replace(job, time_limit_s=limit.seconds, time_limit_basis=limit.basis)
        quarantined = self._quarantined(job, tested.repository)
        if quarantined:
            count = len(quarantined)
            said = (
                f"Left out {count} quarantined test{'' if count == 1 else 's'} until "
                f"{'its' if count == 1 else 'their'} fix issue closes (#3055): "
                + ", ".join(quarantined)
            )
            job = replace(
                job,
                quarantined=quarantined,
                note="\n\n".join(part for part in (job.note, said) if part),
            )
        self._jobs[job.id] = job
        self._save()
        if entry.cancelled:
            persisted = self._finish(
                job.id,
                state=CANCELLED,
                exit_code=None,
                note="cancelled before command start",
            )
            os.close(slot_fd)
            self._running.pop(job.id, None)
            self._wake.set()
            if persisted:
                await self._announce_by_id(job.id)
            return
        handle = None
        try:
            argv = _argv(job)
            if job.quarantined and job.lane not in REMOTE_LANES:
                argv += ["-p", QUARANTINE_PLUGIN]
            # A job moved off a full cluster keeps the attempt that found it full
            # above its second run, so the log still says why it ran where it did.
            mode = "ab" if job.moved_from_lane else "wb"
            handle = open(job.log_path, mode)  # noqa: SIM115 -- closed in finally
            # A job that may outlive this runtime (#3248) runs in a scope of its own, outside
            # the runtime's cgroup that a restart tears down, and keeps its slot locked
            # itself. Its wrapper leaves the exit code in a file, because the runtime that
            # finds it finished may not be its parent.
            exit_path = ""
            prefix: list[str] = []
            pass_fds: tuple[int, ...] = ()
            if self._survive_restart and await worker_limits.available(
                worker_limits.DEFAULT_MEMORY_MAX
            ):
                exit_path = f"{job.log_path}{EXIT_SUFFIX}"
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(exit_path)
                prefix = _scope_prefix(job.id)
                argv = _recording_exit(argv, exit_path)
                pass_fds = (slot_fd,)
            process = await asyncio.create_subprocess_exec(
                *prefix,
                *argv,
                cwd=job.cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                env=_child_env(job),
                # Its own process group, so cancelling reaches the whole test
                # tree (pytest workers, a browser) rather than just the shell.
                start_new_session=True,
                pass_fds=pass_fds,
            )
        # ValueError covers JobError too: the remote lane's runner was there at
        # submission and is not now, because the worktree went away underneath a
        # queued job. That fails the job with the reason rather than hanging.
        except (OSError, ValueError) as exc:
            persisted = self._finish(
                job.id, state=FAILED, exit_code=None, note=f"could not start: {exc}"
            )
            os.close(slot_fd)
            if handle is not None:
                handle.close()
            self._running.pop(job.id, None)
            if persisted:
                await self._announce_by_id(job.id)
            return
        if exit_path:
            job = replace(
                job,
                pid=process.pid,
                pid_start=_process_start(process.pid),
                exit_path=exit_path,
                slot_inode=entry.slot_inode,
            )
            self._jobs[job.id] = job
            self._save()

        def release() -> None:
            handle.close()
            os.close(slot_fd)

        await self._see_through(job, entry, process, release)

    async def _resume(self, job: Job) -> None:
        """Watch a job the previous runtime started, to its end (#3248)."""
        entry = self._running[job.id]
        process = _AdoptedProcess(job.pid or 0, job.exit_path)
        already = max(0.0, self._clock() - (job.started_at or self._clock()))
        await self._see_through(job, entry, process, lambda: None, already_ran=already)

    async def _see_through(
        self,
        job: Job,
        entry: _Running,
        process: Any,
        release: Callable[[], None],
        *,
        already_ran: float = 0.0,
    ) -> None:
        """Wait for a started job's process, then record and announce how it ended."""
        entry.process = process
        # A cancel that landed while the process was being spawned found no
        # process to stop; stop it now rather than let it run to the end.
        if entry.cancelled:
            _terminate(process)
        try:
            code, ran_for = await self._watch(job, entry, process, already_ran=already_ran)
        except asyncio.CancelledError:
            # A runtime going down leaves a job that can outlive it running, for the next
            # one to take back; anything else would die with the runtime anyway.
            if not (self._detaching and job.exit_path):
                _terminate(process)
            raise
        finally:
            release()
            self._running.pop(job.id, None)
        if code == KUBE_NO_ROOM_EXIT and job.lane in _SPILL_TARGETS:
            self._spill_paused_until[job.lane] = self._clock() + SPILL_PAUSE_S
        unreachable_until = self._mark_if_unreachable(job, code)
        if entry.cancelled:
            persisted = self._finish(
                job.id, state=CANCELLED, exit_code=code, note="cancelled while running"
            )
        elif ran_for is not None:
            stopped = (
                f"stopped after {ran_for / 60:.0f} min, over its time limit of "
                f"{(job.time_limit_s or 0) / 60:.0f} min ({job.time_limit_basis}); resubmit "
                "with --max-minutes to allow longer"
            )
            persisted = self._finish(
                job.id,
                state=FAILED,
                exit_code=code,
                note=f"{job.note}; {stopped}" if job.note else stopped,
            )
        elif code is None:
            # Only a job taken back after a restart can end without an exit code: its wrapper
            # was killed along with it, so nothing says how the command itself ended.
            persisted = self._finish(
                job.id,
                state=INTERRUPTED,
                exit_code=None,
                note="its process ended without an exit code after the runtime restarted",
            )
        elif code == KUBE_NO_ROOM_EXIT and (home := _spilled_from(job)):
            # Before the no-room fallback: a job this host spilled to the desktop goes back
            # to wait for this host, not on to a third lane it was never meant for (#3614).
            self._move_lane(job.id, home, spill_back_note(home))
            _drop_exit(job)
            self._wake.set()
            return
        elif code == KUBE_NO_ROOM_EXIT and (
            fallback := _no_room_fallback(job, self._skipped_lanes())
        ):
            self._move_lane(job.id, fallback, moved_note(job.lane, fallback))
            _drop_exit(job)
            self._wake.set()
            return
        else:
            # A failed verification is read again too (#2382): its follow-ups can later show
            # its failures were not the commit's doing, and then it must prove its tree held
            # still exactly as a green one does. So must each follow-up (#2495): a rerun whose
            # checkout was edited while its pod copied it tested the fix, not the commit.
            if _is_verification(job) or job.follows:
                after, after_all = await asyncio.to_thread(_snapshots, job)
                self._jobs[job.id] = replace(
                    self._jobs[job.id],
                    verified_workspace=after.as_record(),
                    verified_workspaces=after_all,
                )
            if _is_verification(job) and code != 0:
                present = await asyncio.to_thread(_failures_present_on_base, self._jobs[job.id])
                self._jobs[job.id] = replace(self._jobs[job.id], base_present=present)
            # A queued row carries no note of its own unless it was moved here
            # from a full cluster, which the finished run should still say.
            note = job.note
            if unreachable_until is not None:
                said = (
                    f"the {job.lane} cluster was unreachable; nothing ran. The queue skips "
                    f"{job.lane} until {_clock_time(unreachable_until)}."
                )
                note = "\n\n".join(part for part in (note, said) if part)
            persisted = self._finish(
                job.id, state=DONE if code == 0 else FAILED, exit_code=code, note=note
            )
        # Only once the outcome is on disk: until then the exit file is the one record of how
        # the command ended, and the next runtime finishes the row from it.
        if persisted:
            _drop_exit(job)
        self._wake.set()
        if persisted:
            await self._follow_up(job.id)
            await self._announce_by_id(job.id)

    def _time_limit(self, job: Job) -> job_durations.TimeLimit:
        repository = _duration_repository(job)
        by_lane = {
            lane: self._durations.durations(job_durations.project_key(repository, lane))
            for lane in LANES
        }
        return job_durations.limit_for(
            by_lane,
            repository=repository,
            lane=job.lane,
            max_minutes=job.max_minutes,
        )

    async def _watch(
        self, job: Job, entry: _Running, process: Any, *, already_ran: float = 0.0
    ) -> tuple[int | None, float | None]:
        """Wait for the job's process, telling its chat how it goes and enforcing its limit.

        Returns the exit code, and how long it had run when the limit stopped it (None when
        it ended on its own or was cancelled). Timed on the event loop's monotonic clock: a
        limit is about real time passing, which the queue's injectable clock is not.
        ``already_ran`` is the time a job taken back after a restart ran before it.
        """
        loop = asyncio.get_running_loop()
        begun = loop.time() - already_ran
        deadline = None if job.time_limit_s is None else begun + job.time_limit_s
        nudging = (
            self._notify_progress is not None
            and bool(job.session_key)
            and self._progress_interval_s > 0
        )
        # A job taken back mid-run carries on from the nudges it already had, rather than
        # sending every one it missed at once.
        nudges = int(already_ran // self._progress_interval_s) if nudging else 0
        next_nudge = begun + self._progress_interval_s * (nudges + 1) if nudging else None
        waiting = asyncio.ensure_future(process.wait())
        try:
            while True:
                wakes = [at for at in (deadline, next_nudge) if at is not None]
                timeout = max(0.0, min(wakes) - loop.time()) if wakes else None
                done, _ = await asyncio.wait({waiting}, timeout=timeout)
                if done:
                    return waiting.result(), None
                now = loop.time()
                if entry.cancelled:
                    # The cancel already signalled the tree; the limit no longer applies.
                    deadline = None
                if deadline is not None and now >= deadline:
                    _terminate(process)
                    try:
                        code = await asyncio.wait_for(
                            asyncio.shield(waiting), timeout=self._kill_grace_s
                        )
                    except TimeoutError:
                        _kill(process)
                        code = await waiting
                    return code, now - begun
                if next_nudge is not None and now >= next_nudge:
                    next_nudge += self._progress_interval_s
                    nudges += 1
                    self._send_progress(job.id, now - begun, nudges)
        finally:
            if not waiting.done():
                waiting.cancel()

    def _send_progress(self, job_id: str, ran_for: float, number: int) -> None:
        """Deliver one progress message without holding up the watch that sent it.

        Best effort and never retried: the next one is a few minutes away, and an old progress
        message delivered late says nothing the next would not.
        """
        job = self._jobs.get(job_id)
        if job is None or self._notify_progress is None:
            return
        notify = self._notify_progress
        message = progress_message(job, ran_for)

        async def deliver() -> None:
            try:
                await notify(job, message, number)
            except Exception:  # noqa: BLE001 -- a refusing chat must not break the queue
                log.exception("jobs: could not tell %s how job %s goes", job.session_key, job_id)

        task = asyncio.create_task(deliver(), name=f"job-progress-{job_id}-{number}")
        self._progress_tasks.add(task)
        task.add_done_callback(self._progress_tasks.discard)

    def _finish(self, job_id: str, *, state: str, exit_code: int | None, note: str) -> bool:
        job = self._jobs[job_id]
        classification = _result_class(job, state=state, exit_code=exit_code)
        output_bytes, output_sha256, excerpt = _log_identity(job.log_path)
        failures, failures_complete = extract_failures(
            read_tail(job.log_path, limit=FAILURE_SUMMARY_CHARS), succeeded=state == DONE
        )
        result_id = hashlib.sha256(
            json.dumps(
                {
                    "job_id": job.id,
                    "state": state,
                    "exit_code": exit_code,
                    "workspace": job.tested_workspace,
                    "output_bytes": output_bytes,
                    "output_sha256": output_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        finished = replace(
            job,
            state=state,
            exit_code=exit_code,
            finished_at=self._clock(),
            note=note,
            result_id=result_id,
            result_class=classification,
            output_bytes=output_bytes,
            output_sha256=output_sha256,
            diagnostic_excerpt=excerpt,
            failures=failures,
            failures_complete=failures_complete,
        )
        # A verification result is evidence only once its evidence is durable (#2148): the
        # queue forgets this row a day from now, and a result that was announced and credited
        # must not be left with nothing that says what it tested. So the evidence is written
        # first, and failing to write it is a failed completion like failing to save the row.
        # The other order's crash window is the honest one: a line whose row never landed still
        # describes a command that really ran, and nothing credits from the line.
        # A follow-up is kept beside the verification it is for (#2382): whether that one
        # counts is worked out from both, after this queue has forgotten the rows.
        if (_is_verification(finished) or finished.follows) and not self._evidence.record(
            finished.to_dict()
        ):
            return False
        self._jobs[job_id] = finished
        self._prune(self._clock())
        if self._save():
            self._record_baseline(finished)
            if _counts_as_duration(finished):
                self._durations.record(
                    _duration_key(finished),
                    finished.id,
                    (finished.finished_at or 0.0) - (finished.started_at or 0.0),
                )
            self._say_forgiven(finished)
            return True
        # Never announce a result the runtime could not make durable. Keeping the running row
        # causes boot recovery to classify it as interrupted instead of inventing success.
        self._jobs[job_id] = job
        return False

    def _move_lane(self, job_id: str, lane: str, note: str, *, spilled: bool = False) -> Job:
        """Queue a job again on ``lane``: its cluster had no room for it, or
        every slot of its own lane was taken and ``lane`` had one free.

        The same row rather than a new submission: the chat, the poller's
        submission key and the job id it was told about all stay valid, and the
        chat hears once, when the run that did happen finishes. It keeps its
        place at the front of the queue because the order is by submission time.
        """
        job = self._jobs[job_id]
        self._jobs[job_id] = replace(
            job,
            state=QUEUED,
            lane=lane,
            moved_from_lane=job.lane,
            spilled=spilled,
            started_at=None,
            note=note,
            pid=None,
            pid_start=None,
            exit_path="",
            slot_inode=None,
        )
        self._save()
        return self._jobs[job_id]

    def _record_baseline(self, job: Job) -> None:
        """Keep this result if it is reusable as a base-branch baseline (#1737).

        Deliberately after the queue's own save and never able to fail it: the baseline is a
        cache, and a card whose verification finished must not be held up because a cache
        write did not land.
        """
        entry = baseline.baseline_entry(job.to_dict())
        if entry is not None:
            self._baselines.record(entry)

    def _quarantined(self, job: Job, repository: str) -> tuple[str, ...]:
        """The quarantined tests this job leaves out: only a verification's ``pytest`` does.

        A rerun of failing tests is not a verification, and a resubmitted one is.
        """
        if self._quarantine is None or not _is_verification(job) or not _runs_pytest(job):
            return ()
        if not _leaves_out_quarantined(job):
            return ()
        return self._quarantine.tests(repository, keep_for_issue=_own_issue(job, repository))

    def _say_forgiven(self, finished: Job) -> None:
        """Tell the chat when this follow-up completed the evidence its verification needed.

        Nothing is written onto the verification: whether it counts is worked out from it and
        its follow-ups wherever it is read (``flaky.forgiveness``, #2382). This only puts the
        news in the completion message of the follow-up that brought the last piece.
        """
        if finished.follow_kind not in {flaky.RERUN, flaky.BASE}:
            return
        verification = self._jobs.get(finished.follows)
        if verification is None:
            return
        forgiven = flaky.forgiveness(
            verification.to_dict(), [job.to_dict() for job in self._jobs.values()]
        )
        if not forgiven:
            # Once it is forgiven no further follow-up is planned, so the first one to find it
            # forgiven is the one that completed it, and the only one that says so.
            return
        head = str((verification.tested_workspace or {}).get("head") or "")[:12]
        said = (
            f"Verification job {verification.id} on {head} now counts as green: none of its "
            "failures was that commit's doing.\n" + flaky.describe(forgiven)
        )
        if self._quarantine is not None and _leaves_out_quarantined(verification):
            added = self._quarantine.add(
                str((verification.tested_workspace or {}).get("repository") or ""),
                [item for item in forgiven if item.get("kind") == flaky.FLAKY],
                verification=verification.id,
            )
            if added:
                said += (
                    "\nQuarantined until a fix issue for each closes, so later verifications "
                    "leave them out: " + ", ".join(added)
                )
        current = self._jobs[finished.id]
        self._jobs[finished.id] = replace(
            current, note="\n\n".join(part for part in (current.note, said) if part)
        )
        self._save()

    async def _follow_up(self, job_id: str) -> None:
        """Submit whatever evidence this finished job calls for, without anyone asking (#2382).

        A verification that failed on a few tests gets them rerun on the same commit, then
        whatever still fails run once on its merge base; one that ended with no test result
        is resubmitted once. ``poller_engine.flaky.follow_up`` decides and bounds it; this only
        carries it out, and a follow-up that cannot be submitted is logged and dropped rather
        than allowed to fail the job it follows. Each follow-up tells the same chat when it
        finishes, and the job it follows says it was scheduled.
        """
        finished = self._jobs.get(job_id)
        if finished is None:
            return
        pinned = finished.cwd if finished.follow_kind in {flaky.RERUN, flaky.BASE} else ""
        try:
            plan = flaky.follow_up(
                finished.to_dict(), [job.to_dict() for job in self._jobs.values()]
            )
            if plan is not None:
                await self._submit_follow_up(finished, plan)
            else:
                self._say_not_followed(finished)
        except Exception:
            log.exception("jobs: could not follow up job %s", job_id)
        if pinned and not any(
            job.cwd == pinned and not job.is_final for job in self._jobs.values()
        ):
            await asyncio.to_thread(_remove_pinned_checkout, pinned)

    def _say_not_followed(self, finished: Job) -> None:
        """Say why a verification's failures got no same-commit rerun, when the tree is why."""
        reason = flaky.unfollowable_tree(finished.to_dict())
        if reason is None:
            return
        said = {
            flaky.DIRTY: "The verified tree had uncommitted changes, so its failures cannot be "
            "rerun on the same commit.",
            flaky.MOVED: "The tree changed while the verification ran, so its failures cannot "
            "be rerun on the same commit.",
        }[reason]
        current = self._jobs[finished.id]
        self._jobs[finished.id] = replace(
            current, note="\n\n".join(part for part in (current.note, said) if part)
        )
        self._save()

    async def _submit_follow_up(self, finished: Job, plan: flaky.FollowUp) -> None:
        verification = self._jobs[plan.verification_id]
        if plan.kind == flaky.RESUBMIT:
            follow = self.submit(
                command=verification.command,
                cwd=verification.cwd,
                **_follow_up_lane(
                    verification,
                    verification.cwd,
                    verification.command,
                    self._skipped_lanes(),
                ),
                session_key=verification.session_key,
                label=verification.label,
                purpose=verification.purpose,
                work_id=verification.work_id,
                run_id=verification.run_id,
                workspaces=verification.workspaces,
                follows=verification.id,
                follow_kind=flaky.RESUBMIT,
                # The submitter's limit, or none: the one retry must not be judged by a
                # learned limit the first run was never under (#3038).
                max_minutes=verification.max_minutes,
            )
            said = (
                f"It ended with no test result, so the queue resubmitted it once as job "
                f"{follow.id}."
            )
            lost = finished.lane
            if finished.exit_code == SETUP_LOST_EXIT and lost in self._unreachable_lanes():
                # The one retry must not walk back into the outage, and the chat must not
                # spend another by hand (#3584).
                said = (
                    f"The queue moved the retry to {follow.lane} as job {follow.id}. "
                    "Do not resubmit."
                    if follow.lane != lost
                    else f"The retry, job {follow.id}, stays on {lost} because the "
                    f"submission named that lane, though {lost} was just unreachable. "
                    "Do not resubmit."
                )
        else:
            tested = verification.tested_workspace or {}
            if plan.kind == flaky.BASE:
                base = str(tested.get("base_head") or "")
                cwd = await asyncio.to_thread(
                    _pinned_checkout, verification.cwd, base, BASE_CHECKOUT_PREFIX
                )
                prefix = _checkout_pytest()
                where = f"its merge base {base[:12]}"
            else:
                head = str(tested.get("head") or "")
                # The card's worktree may hold later commits or edits by now (#2625), so the
                # rerun gets a checkout of the commit that was verified, run like the base's.
                cwd = await asyncio.to_thread(
                    _pinned_checkout, verification.cwd, head, RERUN_CHECKOUT_PREFIX
                )
                prefix = _checkout_pytest()
                attempt = 1 + sum(
                    1
                    for job in self._jobs.values()
                    if job.follows == verification.id and job.follow_kind == flaky.RERUN
                )
                where = f"the same commit {head[:12]} (attempt {attempt} of {flaky.MAX_RERUNS})"
            command = [*prefix, "-q", *plan.tests]
            follow = self.submit(
                command=command,
                cwd=cwd,
                **_follow_up_lane(verification, cwd, command, self._skipped_lanes()),
                session_key=verification.session_key,
                label=f"{plan.kind} of verification {verification.id}",
                work_id=verification.work_id,
                follows=verification.id,
                follow_kind=plan.kind,
                follow_tests=plan.tests,
            )
            count = len(plan.tests)
            said = (
                f"The queue is running its {count} failing test{'' if count == 1 else 's'} "
                f"on {where} as job {follow.id}. If a same-commit run passes them, or they "
                "also fail on the merge base, this verification counts as green."
            )
        current = self._jobs[finished.id]
        self._jobs[finished.id] = replace(
            current, note="\n\n".join(part for part in (current.note, said) if part)
        )
        self._save()

    async def _announce_by_id(self, job_id: str) -> None:
        # Retention can have pruned the row between finishing and announcing.
        job = self._jobs.get(job_id)
        if job is not None:
            await self._announce(job)

    async def _announce_pending(self) -> None:
        now = self._clock()
        pending = [
            job
            for job in self._ordered()
            if job.is_final
            and job.session_key
            and not job.notified
            and self._notify_after.get(job.id, 0.0) <= now
        ]
        for job in pending:
            await self._announce(job)

    async def _announce(self, job: Job) -> None:
        """Tell the submitting chat, exactly once per job."""
        current = self._jobs.get(job.id)
        if (
            self._notify is None
            or not job.session_key
            or current is None
            or current.notified
            or job.id in self._announcing
        ):
            return
        self._announcing.add(job.id)
        try:
            outcome = _notify_outcome(await self._notify(current, job_message(current)))
            if not outcome.delivered:
                self._defer_or_give_up(
                    job.id,
                    outcome.reason or "the chat did not accept it",
                    host_busy=outcome.reason in RE_DECIDED_REFUSALS,
                )
                return
            current = self._jobs.get(job.id)
            if current is not None and not current.notified:
                self._jobs[job.id] = replace(current, notified=True)
                self._save()
            self._notify_after.pop(job.id, None)
        except Exception:  # noqa: BLE001 -- a refusing chat must not break the queue
            log.exception("jobs: could not tell %s about job %s", job.session_key, job.id)
            self._defer_or_give_up(job.id, "the delivery raised", host_busy=False)
        finally:
            self._announcing.discard(job.id)

    def _defer_or_give_up(self, job_id: str, why: str, *, host_busy: bool) -> None:
        """Retry a refused completion for a while, then stop for good.

        A refusal does not say whether it is transient. "That worker is
        temporarily unavailable" recovers on the next tick; "no worker record
        for this session" and "malformed session key" never do, and the caller
        cannot tell them apart — ``accepted`` is False for both.

        Without a deadline the permanent kind is the expensive one: the row is
        retried every minute forever, and because ``_prune`` deliberately keeps
        un-notified rows so a pending retry survives, it can never age out
        either. One released worker would leave a row and a log line per minute
        for as long as the deck runs.

        So retry generously — ``NOTIFY_GIVE_UP_S`` covers a runtime restart and
        an evicted worker being revived — and then mark it notified with the
        reason. Notified here means "done trying", not "the chat read it": the
        job's own state and log still say what happened, and the row prunes.

        ``host_busy`` names the one refusal that deadline must not judge
        (#1610). A chat with no live worker is woken by starting one, and
        starting one needs a free slot, so a full pool refuses ``at_capacity``
        however reachable the chat is. An hour of that is an ordinary busy
        afternoon; spending the deadline on it throws away a finished suite's
        result and leaves the chat waiting for a message that will now never
        come. Those refusals answer to ``NOTIFY_HOST_BUSY_GIVE_UP_S`` instead,
        which is a backstop against an immortal row rather than a verdict on
        whether the chat still exists.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        now = self._clock()
        age = now - (job.finished_at or job.submitted_at)
        if host_busy:
            # Measured from the job, because this one is only a backstop against a
            # row outliving the runtime.
            spent = age >= self._notify_host_busy_give_up_s
            # ...and it pauses the chat's stopwatch, so the hours a full pool can
            # last are not later counted as time spent failing to reach the chat.
            # Pausing banks the interval the clock was running rather than
            # dropping it: the chat really was being refused for that stretch,
            # and forgetting it would hand the chat a fresh budget every time the
            # host got busy.
            if job.chat_refused_at is not None:
                self._jobs[job_id] = replace(
                    job,
                    chat_refused_for=job.chat_refused_for + (now - job.chat_refused_at),
                    chat_refused_at=None,
                )
                self._save()
        else:
            # The chat's budget counts only the time between consecutive refusals
            # that were actually about the chat. Anything simpler hands the busy
            # host the very budget this split exists to protect: measured from
            # the job, five hours of `at_capacity` leave the first chat-scoped
            # refusal already past the deadline; measured from a fixed start, an
            # hour of `at_capacity` in the middle does the same. Neither hour
            # carries one word about whether the chat is reachable.
            #
            # The stopwatch lives on the row rather than in memory, because a
            # restart reloads the row: held in memory it would hand every restart
            # a fresh full deadline, and on a host that redeploys through the day
            # a chat that is gone for good would never age out. That is #1299's
            # leak, and it must not come back through this door.
            elapsed = job.chat_refused_for
            if job.chat_refused_at is not None:
                elapsed += now - job.chat_refused_at
            self._jobs[job_id] = replace(
                job, chat_refused_for=elapsed, chat_refused_at=now
            )
            self._save()
            spent = elapsed >= self._notify_give_up_s
        if not spent:
            log.info("jobs: completion delivery for %s is still pending", job_id)
            self._notify_after[job_id] = now + self._notify_retry_s
            return
        log.warning(
            "jobs: gave up telling %s about job %s after %.0fs (%s)",
            job.session_key,
            job_id,
            age,
            why,
        )
        lost = (
            "the host never had room to wake its chat"
            if host_busy
            else "its chat was never reachable"
        )
        self._jobs[job_id] = replace(
            job,
            notified=True,
            note=(f"{job.note}; " if job.note else "")
            + f"{lost}, so this completion was not delivered ({why})",
        )
        self._notify_after.pop(job_id, None)
        self._save()


def _extra_workspaces(
    workspaces: Iterable[str], directory: Path, *, purpose: str, lane: str
) -> list[str]:
    """The other checkouts a verification job names, resolved, or a refusal (#2148)."""
    paths: list[str] = []
    for raw in workspaces:
        path = Path(str(raw)).expanduser()
        if not path.is_dir():
            raise JobError(f"workspace does not exist: {raw}")
        resolved = str(path.resolve())
        if resolved != str(directory.resolve()) and resolved not in paths:
            paths.append(resolved)
    if not paths:
        return []
    if purpose != VERIFICATION:
        # What a job tested is evidence only for verification; anywhere else the extra
        # reading is cost with no reader.
        raise JobError("only a verification job names other workspaces it tests")
    if lane in REMOTE_LANES:
        # A pod receives the checkout it runs in and nothing else, so it cannot have tested
        # another repository -- and evidence must not claim it did.
        raise JobError(
            f"the {lane} lane runs the job's own checkout only; a job that also tests "
            "another workspace has to run here (--where local)"
        )
    return paths


def _snapshots(job: Job) -> tuple[WorkspaceSnapshot, dict[str, dict[str, Any]] | None]:
    """Read the job's own checkout, and for verification every other one it names."""
    primary = collect_workspace_snapshot(job.cwd, command=job.command)
    if not _is_verification(job):
        return primary, None
    others = [collect_workspace_snapshot(path, command=job.command) for path in job.workspaces]
    keyed = repository_snapshots((primary, *others))
    return primary, {key: snapshot.as_record() for key, snapshot in keyed.items()}


def _keyed_records(raw: object) -> dict[str, dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def _runs_pytest(job: Job) -> bool:
    parts = list(job.command)
    if parts[:2] == ["uv", "run"]:
        parts = parts[2:]
    head = parts[0] if parts else ""
    if head == "pytest" or head.endswith("/pytest"):
        return True
    return len(parts) >= 3 and parts[1:3] == ["-m", "pytest"]


_WORK_ISSUE = re.compile(r"github_issue:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")


def _own_issue(job: Job, repository: str) -> int | None:
    """The issue in ``repository`` the Work this job verifies is about, if any (#3584).

    A quarantined test whose fix issue that is runs in this Work's verification: leaving
    it out would verify the fix green without the test it fixes. Another repository's
    issue of the same number is not this one.
    """
    for work in (job.owner_work_id, job.work_id):
        match = _WORK_ISSUE.fullmatch((work or "").strip())
        if match is not None:
            if match.group(1).casefold() == str(repository or "").casefold():
                return int(match.group(2))
            return None
    return None


def _leaves_out_quarantined(job: Job) -> bool:
    """Whether a run of ``job`` on its lane can leave quarantined tests out (#3055).

    A local run always can. A pod runs the checkout's own runner, and only one that knows
    ``QUARANTINE_PLUGIN_FILE_ENV`` copies the plugin in; any other (an older branch, another
    project) runs every test, so nothing is quarantined for it rather than claimed left out.
    """
    if job.lane not in REMOTE_LANES:
        return True
    found = kube_project(job.cwd)
    if found is None:
        return False
    try:
        return QUARANTINE_PLUGIN_FILE_ENV in found[1].read_text(encoding="utf-8")
    except OSError:
        return False


def _is_verification(job: Job) -> bool:
    """Whether this job's result may become release evidence (#2154)."""
    return job.purpose == VERIFICATION


def _result_class(job: Job, *, state: str, exit_code: int | None) -> str:
    """Separate a command that ran and failed from runner/queue failures."""
    if state == DONE:
        return "success"
    if state != FAILED or exit_code is None:
        return "infrastructure"
    return "failure"


def _log_tail(path: str, limit: int) -> str:
    """The last ``limit`` bytes of a log, as text; empty when it cannot be read."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_unreachable(path: Path) -> dict[str, float]:
    """Remote lane -> until when it is skipped; nothing when absent or unreadable (#3584)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(lane): float(until)
        for lane, until in payload.items()
        if lane in REMOTE_LANES
        and isinstance(until, int | float)
        and not isinstance(until, bool)
    }


def _clock_time(at: float) -> str:
    """``at`` as this host's local HH:MM."""
    return time.strftime("%H:%M", time.localtime(at))


def _log_identity(path: str) -> tuple[int, str, str]:
    """Hash a full log incrementally while retaining only its bounded tail."""
    digest = hashlib.sha256()
    total = 0
    tail = bytearray()
    try:
        with Path(path).open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                total += len(chunk)
                digest.update(chunk)
                tail.extend(chunk)
                if len(tail) > NOTIFY_TAIL_CHARS * 4:
                    del tail[: len(tail) - NOTIFY_TAIL_CHARS * 4]
    except OSError:
        return 0, "", ""
    text = bytes(tail).decode("utf-8", errors="replace").rstrip()
    if len(text) > NOTIFY_TAIL_CHARS:
        text = "…" + text[-NOTIFY_TAIL_CHARS:]
    return total, digest.hexdigest(), text


def _argv(job: Job) -> list[str]:
    """What to actually exec for ``job``.

    The stored command stays what the submitter asked for, so listings and the
    completion message read as the suite someone wanted rather than as the
    plumbing that carried it.
    """
    if job.lane not in REMOTE_LANES:
        argv = _local_argv(job.cwd, job.command)
        workers = local_suite_workers()
        # Split the cores between the suites that may run at once (#3614), rather than each
        # taking one worker per core through the project's `-n auto`. With one slot that
        # share is every core, which `-n auto` already gives.
        if (
            workers < usable_cores()
            and _agentdeck_suite(job.cwd, job.command)
            and not _asks_for_workers(job.command)
        ):
            argv += ["-n", str(workers)]
        return argv
    return _kube_argv(job.cwd, job.command, job.lane)


def _agentdeck_suite(cwd: str, command: list[str]) -> bool:
    """Whether ``command`` is a pytest run of the agentdeck checkout: what the desktop takes."""
    try:
        _kube_argv(cwd, command, DESKTOP)
    except JobError:
        return False
    return True


def _asks_for_workers(command: list[str]) -> bool:
    """Whether ``command`` already sets xdist's worker count, or turns xdist off."""
    for index, arg in enumerate(command):
        if arg == "--numprocesses" or arg.startswith("--numprocesses="):
            return True
        if arg.startswith("-n") and not arg.startswith("--"):
            return True
        if arg == "-pno:xdist" or (arg == "-p" and command[index + 1 : index + 2] == ["no:xdist"]):
            return True
    return False


def _checkout_pytest() -> list[str]:
    """How a pinned checkout runs pytest: the card's own interpreter would test the card's
    worktree, so `uv run` here. A pod drops the `uv run` and uses the checkout's own runner,
    so the one command serves whichever lane the follow-up ends up on (#3043)."""
    return ["uv", "run", "pytest"]


def _follow_up_lane(
    verification: Job, cwd: str, command: list[str], down: frozenset[str] | None = None
) -> dict[str, Any]:
    """Where a verification's follow-up job runs, and whether the queue may move it (#3043).

    A lane the submitter named is kept. A lane the queue chose is chosen again, as for a
    fresh submission — a pod first, even when the verification only reached this host by
    fallback — and the follow-up may spill and fall back exactly as the verification could,
    rather than waiting for this host's one slot while the dev cluster has room. One that
    also tested another checkout stays here and is not moved, because a pod receives only
    its own (#2148) and a rerun does not carry the other checkouts that would hold it here.
    """
    if verification.workspaces or not verification.lane_defaulted:
        return {"lane": verification.lane, "lane_defaulted": False}
    return {"lane": default_lane(cwd, command, down), "lane_defaulted": True}


# Where the queue checks out a commit to run failing tests on (#2382): beside the cards' own
# worktrees, one per commit, removed once no job runs in it. A merge base gets one, and so does
# the verified head a same-commit rerun tests (#2625): the card's own worktree has usually moved
# on by the time the rerun starts, and a pod copies the working copy as it is.
BASE_CHECKOUT_PREFIX = "verify-base-"
RERUN_CHECKOUT_PREFIX = "verify-rerun-"
PINNED_CHECKOUT_PREFIXES = (BASE_CHECKOUT_PREFIX, RERUN_CHECKOUT_PREFIX)


def _git(*args: str, cwd: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=120, check=True
    )
    return done.stdout.strip()


def _failures_present_on_base(job: Job) -> tuple[str, ...] | None:
    """Which of this run's failing tests its merge base already has, or None if unknown.

    Read from the base commit's own files: a test whose file or ``def`` is not there is one the
    branch added (#2552). A new parametrized case of an existing test still reads as present;
    telling those apart would need a collection run on the base, a whole extra lane.
    """
    import re
    import subprocess

    failures, _complete = extract_failures(read_tail(job.log_path, limit=FAILURE_SUMMARY_CHARS))
    base = str((job.tested_workspace or {}).get("base_head") or "")
    if not failures or not base:
        return None

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", job.cwd, *args], capture_output=True, text=True, timeout=60
        )

    try:
        if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
            return None
        sources: dict[str, str | None] = {}
        present = []
        for test in failures:
            path, _, rest = test.partition("::")
            if path not in sources:
                shown = git("show", f"{base}:./{path}")
                sources[path] = shown.stdout if shown.returncode == 0 else None
            source = sources[path]
            if source is None:
                continue
            # Parameters first: an id like ``test_x[a::b]`` carries ``::`` inside them.
            name = rest.split("[", 1)[0].rsplit("::", 1)[-1]
            if not name or re.search(
                rf"^\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(", source, re.MULTILINE
            ):
                present.append(test)
    except (OSError, subprocess.SubprocessError):
        return None
    return tuple(present)


def _pinned_checkout(cwd: str, sha: str, prefix: str) -> str:
    """A clean detached checkout of ``sha`` in the repository ``cwd`` belongs to."""
    if not sha:
        raise JobError("no commit to check out")
    if prefix not in PINNED_CHECKOUT_PREFIXES:
        raise JobError(f"not a pinned checkout prefix: {prefix}")
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd))
    path = common.parent / ".worktrees" / f"{prefix}{sha[:12]}"
    if path.is_dir():
        with contextlib.suppress(Exception):
            if _git("rev-parse", "HEAD", cwd=str(path)) == sha and not _git(
                "status", "--porcelain", cwd=str(path)
            ):
                return str(path)
        _remove_pinned_checkout(str(path))
    _git("worktree", "add", "--detach", str(path), sha, cwd=cwd)
    return str(path)


def _remove_pinned_checkout(path: str) -> None:
    """Remove a checkout ``_pinned_checkout`` made, and never anything else."""
    if not Path(path).name.startswith(PINNED_CHECKOUT_PREFIXES):
        return
    with contextlib.suppress(Exception):
        common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        _git("worktree", "remove", "--force", path, cwd=str(Path(common).parent))


def workspace_python(cwd: str) -> Path | None:
    """The interpreter a job in ``cwd`` should run, or None when it has none (#1635).

    The search stops at the enclosing checkout — the first directory holding a
    ``.git`` entry, which a worktree has as much as a clone does. That boundary is
    the whole point rather than tidiness: ``.worktrees/issue-1635`` sits inside
    ``~/agentdeck``, so a walk that kept going would find the live checkout's
    virtualenv, and a suite run through it imports ``~/agentdeck``'s ``agentdeck``
    package. That reports a green run of code nobody wrote, which is worse than
    the import error it would be papering over.
    """
    directory = Path(cwd).expanduser().resolve()
    for candidate in (directory, *directory.parents):
        interpreter = candidate / ".venv" / "bin" / "python"
        if interpreter.is_file() and os.access(interpreter, os.X_OK):
            return interpreter
        if (candidate / ".git").exists():
            return None
    return None


def _local_argv(cwd: str, command: list[str]) -> list[str]:
    """``command`` as this host should exec it, with the workspace's own Python.

    A bare ``pytest`` is resolved off ``PATH``, and the ``PATH`` a job inherits is
    the runtime's rather than the workspace's, so a submission that names one runs
    whatever interpreter the runtime happens to have. Against a worktree that is
    not installed into it, that is a one-second ``ModuleNotFoundError`` on the
    conftest import — an exit code the poller reads as an infrastructure fault and
    parks the card on, having learned nothing about the code (#1635).

    So a bare ``pytest`` becomes the workspace's ``python -m pytest``, and a
    workspace with no interpreter is refused rather than run. Everything else is
    left exactly as submitted: ``uv run`` resolves the project's environment
    itself, an explicit path is a submitter who already chose one, and a command
    that is not pytest at all is none of this function's business.
    """
    parts = list(command)
    if parts[:2] == ["uv", "run"]:
        return parts
    head = parts[0] if parts else ""
    if head != "pytest":
        return parts
    interpreter = workspace_python(cwd)
    if interpreter is None:
        raise JobError(
            f"{cwd} has no virtualenv to run pytest with, so this would run the "
            "runtime's interpreter against a checkout it does not hold and fail "
            "on the conftest import. Create one first (`uv sync`), or submit the "
            "interpreter you mean."
        )
    return [str(interpreter), "-m", "pytest", *parts[1:]]


def kube_project(cwd: str) -> tuple[Path, Path, str] | None:
    """``(checkout root, runner, project)`` for ``cwd``, or None if the lane is
    not for it.

    Only the agentdeck checkout and the projects ``job-lanes.toml`` lists may send
    work to a cluster, and each is recognised by the runner it ships rather than by a
    path written down here — which is what makes a worktree or a subdirectory
    work without listing them (#1383).
    """
    directory = Path(cwd).resolve()
    for candidate in (directory, *directory.parents):
        for runner, project in kube_runners().items():
            if (candidate / runner).is_file():
                return candidate, candidate / runner, project
    return None


def quiet_output_hint(cwd: str, command: list[str]) -> str | None:
    """A line for the submitter when a pytest job's log will say little about its progress.

    The progress message a running job's chat gets every few minutes carries the end of its
    log (#3038). Under ``-q`` that is a row of dots, so the hint suggests per-test output. Not
    for the agentdeck checkout: its suite records which tests are running beside the log, and
    the progress message lists them.
    """
    if not any("pytest" in Path(part).name for part in command):
        return None
    quiet = any(
        part == "--quiet" or (part.startswith("-q") and part[1:].isalpha()) for part in command
    )
    verbose = any(
        part in ("--verbose", "-rA", "-ra") or (part.startswith("-v") and part[1:].isalpha())
        for part in command
    )
    if not quiet or verbose:
        return None
    found = kube_project(cwd)
    if found is not None and found[2] == AGENTDECK:
        return None
    return (
        "hint: this job's chat gets a progress message every few minutes with the end of its "
        "log, and under -q that is only dots. Add -v (or -rA) to see which tests ran."
    )


def kube_image_tag(root: Path) -> str:
    """The runner-image tag ``root``'s dependencies ask for.

    The same hash ``kube_image_tag`` in ``scripts/lib/kube_image.sh`` computes,
    written twice because the warning below is issued by Python and the image is
    built and run by shell. ``tests/test_kube_image_ledger.py`` runs both against
    this checkout and fails if they ever disagree.
    """
    digest = hashlib.sha256()
    for name in ("uv.lock", "pyproject.toml"):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()[:12]


def environment_identity(lane: str, root: str) -> str:
    """What a result is only comparable with another result of (#1737).

    The lane, because a pod and this host are different machines, and the dependency set the
    lane installs from — the same digest the runner image is tagged with, so a lockfile change
    invalidates a baseline by simply not matching it.

    Empty when the dependencies cannot be read. An environment nobody can name is one no
    comparison may be made in, and the engine treats the empty answer as exactly that.
    """
    if not lane or not root:
        return ""
    try:
        return f"{lane}:{kube_image_tag(Path(root))}"
    except OSError:
        return ""


def kube_image_ledger_path() -> Path:
    """Where this host records the runner-image tags it knows are baked."""
    override = os.environ.get("AGENTDECK_KUBE_IMAGE_LEDGER")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(data_home).expanduser() / "agentdeck" / "kube-images"


def kube_image_notice(cwd: str) -> str | None:
    """What to tell a submitter about the image this kube job will get (#1590).

    A lockfile change asks for an image nobody has baked, and the lane handles
    that by falling back to the public base image and installing the
    dependencies in the pod — correct, and about forty seconds slower. That used
    to be visible only as one line thirty seconds into a log nobody reads, so a
    dependency bump quietly made every remote suite slower until someone noticed.

    Harbor answers only inside the cluster, so this cannot ask the registry; it
    reads the host's record of baked tags instead. Being a cache, it can be
    wrong in both directions, so this stays a warning that names what to do and
    never refuses the job — and the run itself corrects the record.
    """
    if os.environ.get("AGENTDECK_KUBE_IMAGE"):
        return None  # An explicit image; the tag this would check is not used.
    found = kube_project(cwd)
    if found is None or found[2] != AGENTDECK:
        return None
    root = found[0]
    try:
        tag = kube_image_tag(root)
    except OSError:  # An unreadable checkout is the runner's problem to report.
        return None
    try:
        recorded = kube_image_ledger_path().read_text().split()
    except OSError:  # No record yet — nothing has been baked on this host.
        recorded = []
    if tag in recorded:
        return None
    return (
        f"No runner image is baked for this checkout's dependencies (tag {tag}), "
        "so the pod will start on the public Playwright image and spend about 40 "
        "seconds installing them first. The run is correct either way, and it "
        "starts `scripts/kube_image.sh` in the background so the next runs skip that."
    )


def _kube_argv(cwd: str, command: list[str], lane: str = KUBE) -> list[str]:
    """The project's own runner plus the pytest arguments in ``command``.

    A remote lane runs pytest and nothing else. That is not a limitation being
    apologised for: the pod is a test runner, and a command it cannot honour —
    a build, a shell pipeline — has to be refused where the submitter can read
    it, not silently turned into a pytest run in a pod.
    """
    found = kube_project(cwd)
    if found is None:
        raise JobError(
            f"the {lane} lane runs the agentdeck checkout and the projects in "
            f"{lanes_file()} only, and {cwd} is inside none of them"
        )
    root, runner, project = found
    settings = lane_settings().projects.get(project)
    if settings is not None and lane not in settings["lanes"]:
        # Another project's runner points at that project's own cluster and carries the
        # sidecars its suites need, so aiming it at another lane would not run there —
        # it would quietly run on its own cluster under the wrong lane's slot.
        raise JobError(
            f"the {lane} lane does not run the {project} checkout ({cwd}); "
            f"it runs on {', '.join(settings['lanes'])}"
        )
    parts = list(command)
    if parts[:2] == ["uv", "run"]:
        parts = parts[2:]
    head = parts[0] if parts else ""
    if head != "pytest" and not head.endswith("/pytest"):
        raise JobError("the kube lane runs pytest only; submit `pytest ...`")
    if settings is not None and settings["suites"]:
        # A runner that serves several nested checkouts takes the suite first, and
        # which suite this is comes from where the job was submitted — the same
        # answer that project's own test command gets from the shell it was typed in.
        return [str(runner), _project_suite(project, settings["suites"], cwd, root), *parts[1:]]
    return [str(runner), *parts[1:]]


def _jumps_the_queue(job: Job) -> bool:
    """Whether ``job`` starts ahead of the jobs submitted before it (#3040).

    A rerun of a verification's failing tests, or the same tests on its merge base, is a few
    tests, and whether that verification counts -- often the merge-queue head's -- waits on
    it. Behind a whole suite on this host's single slot it waited half an hour for a two
    minute answer. A resubmitted verification is a whole suite again and keeps its place.
    """
    return job.follow_kind in (flaky.RERUN, flaky.BASE)


def _no_room_fallback(job: Job, down: frozenset[str] | None = None) -> str | None:
    """The lane a job whose cluster found no room can run on instead, if any.

    Only when the queue chose the lane — an explicit `--where kube` is obeyed —
    only once — a job already moved is on the dev cluster, which falls back to
    nothing, so a full dev cluster fails it — and only for what the target lane
    takes.
    """
    return _fallback(job, NO_ROOM_FALLBACK.get(job.lane), down)


def local_first(cores: int | None = None) -> bool:
    """Whether this host is big enough to run the agentdeck checkout's suites first (#3614)."""
    raw = os.environ.get(LOCAL_FIRST_MIN_CORES_ENV, "").strip()
    try:
        threshold = int(raw) if raw else DEFAULT_LOCAL_FIRST_MIN_CORES
    except ValueError:
        threshold = DEFAULT_LOCAL_FIRST_MIN_CORES
    return (usable_cores() if cores is None else cores) >= threshold


def _spill_order(home: str) -> tuple[str, ...]:
    if home == LOCAL and local_first():
        return LOCAL_FIRST_SPILL
    return SPILL_LANES.get(home, ())


# Every lane something may spill to, whichever order is in force.
_SPILL_TARGETS = frozenset(
    {*LOCAL_FIRST_SPILL, *(lane for order in SPILL_LANES.values() for lane in order)}
)


def _spill_lanes(job: Job, down: frozenset[str] | None = None) -> list[str]:
    """The lanes a queued job may start on while every slot of its own is taken, in order."""
    found = (_fallback(job, target, down) for target in _spill_order(job.lane))
    return [lane for lane in found if lane is not None]


def _spilled_from(job: Job) -> str | None:
    """The lane a job was spilled off while its slots were taken, if it was."""
    home = job.moved_from_lane
    if not home:
        return None
    # A row from before ``spilled`` existed says so only in its note.
    if job.spilled or job.note == spilled_note(home, job.lane):
        return home
    return None


def _fallback(job: Job, target: str | None, down: frozenset[str] | None = None) -> str | None:
    """``target`` if ``job`` may move there; ``down`` is the lanes to skip, ``lanes_down``
    when not given (``_skipped_lanes``)."""
    down = lanes_down() if down is None else down
    if target is None or not job.lane_defaulted:
        return None
    # A job that also tests another checkout runs here because a pod receives only
    # its own (#2148), and a lane marked down is not one to move anything to.
    if job.lane == LOCAL and job.workspaces:
        return None
    if target in down:
        return None
    if target == LOCAL:
        # Only an agentdeck suite that took the kube lane in the desktop's place, and
        # only once: another project's suites cannot run here, and a job already moved
        # off the desktop fails on a full kube lane as it always has.
        if job.moved_from_lane or DESKTOP not in down:
            return None
        target_check = DESKTOP
    else:
        target_check = target
    try:
        _kube_argv(job.cwd, job.command, target_check)
    except JobError:
        return None
    return target


def default_lane(
    cwd: str, command: list[str], down: frozenset[str] | None = None
) -> str:
    """Which lane a submission that named none should take.

    For the agentdeck checkout that is the desktop (#2297): a machine of our own,
    idle most of the time, where a shared cluster's CI nodes are busy with other
    builds. The queue moves such a job on to the kube lane when the desktop is busy,
    full or switched off, and never to the Mac. Another project's suites cannot run
    on the desktop, so they still take the kube lane.

    A remote pod first, whenever a remote lane can honour the command:
    it costs this host one ``kubectl`` where a local suite costs it four pytest
    workers and the single heavy slot, and three can run at once. Coming back to
    the host is the exception — a cluster that is down or backed up — and the
    submitter says so with ``--where local`` rather than getting it by default.

    Anything the remote lane cannot honour (a build, a checkout it does not
    serve) falls back to this host rather than being refused: naming no lane is
    a preference, not an instruction, and the explicit ``--where kube`` still
    refuses out loud so a mistyped submission is not silently run here.

    A lane the owner marked down (``lanes_down``) is skipped (#2826). An agentdeck
    suite whose desktop is marked down takes the kube lane next, because this
    host is slow at a whole suite and has one slot (#2835); it comes back here
    only when the dev cluster is marked down too, or says it has no room.

    On a host with ``LOCAL_FIRST_MIN_CORES`` (#3614) all of that turns round for
    the agentdeck checkout's suites: they run here first, in the low-priority
    ``JOBS_SLICE``, and spill to the desktop and then the kube lane when this host
    is full (``LOCAL_FIRST_SPILL``). Other projects' keep the kube lane.

    A remote lane ``job-lanes.toml`` does not list is down (``lanes_down``), so on a
    host with no remote lanes everything runs here.
    """
    down = lanes_down() if down is None else down
    if LOCAL not in down and local_first() and _agentdeck_suite(cwd, command):
        return LOCAL
    for lane in (DESKTOP, KUBE):
        if lane in down:
            continue
        try:
            _kube_argv(cwd, command, lane)
        except JobError:
            continue
        return lane
    return LOCAL


def _project_suite(project: str, suites: dict[str, str], cwd: str, root: Path) -> str:
    """The suite a several-suite runner takes, from which nested checkout ``cwd`` sits in."""
    try:
        relative = Path(cwd).resolve().relative_to(root)
    except ValueError:  # pragma: no cover - kube_project found root above cwd
        relative = Path()
    first = relative.parts[0] if relative.parts else ""
    suite = suites.get(first)
    if suite is None:
        raise JobError(
            f"the {project} checkout holds several suites, so submit this from the "
            f"one you mean ({', '.join(sorted(set(suites)))}), not {cwd}"
        )
    return suite


def _child_env(job: Job) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV}
    # The job already holds a heavy slot; anything inside it must not wait for a
    # second one (the conftest backstop reads this).
    env[HEAVY_SLOT_HELD_ENV] = "1"
    env[QUEUED_JOB_ENV] = "1"
    env["AGENTDECK_JOB_ID"] = job.id
    # Where a pytest run records when each test started and stopped, read back
    # by `agentdeck job tests` (#2297). Anything that is not this suite ignores it.
    env[TIMINGS_ENV] = str(timings_path_for_log(job.log_path))
    if job.quarantined:
        env[QUARANTINE_ENV] = json.dumps(list(job.quarantined))
        if job.lane in REMOTE_LANES:
            env[QUARANTINE_PLUGIN_FILE_ENV] = str(QUARANTINE_PLUGIN_DIR / f"{QUARANTINE_PLUGIN}.py")
        else:
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(QUARANTINE_PLUGIN_DIR), env.get("PYTHONPATH", "")) if part
            )
    profile = kube_profile(job.lane)
    if profile is not None:
        # Set rather than defaulted to, so a stray value in the queue's own
        # environment cannot send a mac job to the dev cluster or the reverse.
        env[KUBE_PROFILE_ENV] = profile
    return env


EXIT_SUFFIX = ".exit"


class _AdoptedProcess:
    """A job's process that a previous runtime started, watched by its pid (#3248).

    Not this process's child, so it cannot be waited on: its exit is learned from a pidfd,
    and its exit code from the file its wrapper writes. ``wait`` returns None when that file
    is missing, which is a wrapper killed along with its command.
    """

    def __init__(self, pid: int, exit_path: str) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._exit_path = exit_path

    async def wait(self) -> int | None:
        # Finished while no runtime watched: its pid may already belong to someone else.
        self.returncode = _read_exit(self._exit_path)
        if self.returncode is not None:
            return self.returncode
        gone = asyncio.Event()
        exits = pid_watch.ExitWatches(lambda _pid: gone.set())
        exits.watch([self.pid])
        try:
            if self.pid in exits.unwatched:
                while _alive(self.pid):
                    await asyncio.sleep(1.0)
            else:
                await gone.wait()
        finally:
            exits.close()
        self.returncode = _read_exit(self._exit_path)
        return self.returncode


def _scope_prefix(job_id: str) -> list[str]:
    """Run the job in a transient scope of its own, outside the runtime's cgroup, in the
    low-priority ``JOBS_SLICE`` (#3614)."""
    unit = f"agentdeck-job-{job_id}-{uuid.uuid4().hex[:8]}.scope"
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--unit={unit}", f"--slice={JOBS_SLICE}", "--",
    ]


def jobs_slice_properties(total_bytes: int) -> list[str]:
    """``JOBS_SLICE``'s settings for a host with ``total_bytes`` of memory (#3614).

    A host too small to spare the headroom keeps half and three quarters of its memory
    instead, so the limits never fall to nothing.
    """
    high = max(total_bytes // 2, total_bytes - JOBS_SLICE_HIGH_HEADROOM)
    ceiling = max(total_bytes * 3 // 4, total_bytes - JOBS_SLICE_MAX_HEADROOM)
    return [f"IOWeight={JOBS_SLICE_IO_WEIGHT}", f"MemoryHigh={high}", f"MemoryMax={ceiling}"]


def _memory_total_bytes(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    try:
        text = meminfo.read_text(encoding="ascii")
    except OSError:
        return None
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


async def configure_jobs_slice() -> bool:
    """Set ``JOBS_SLICE``'s IO weight and memory limits for this boot (#3614).

    ``--runtime`` because they are computed from this host's memory, and a slice set
    again at every runtime start follows a RAM change with nothing to edit. Best effort: a
    host without a usable ``systemctl --user`` runs its jobs as before.
    """
    total = _memory_total_bytes()
    if total is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "set-property", "--runtime", JOBS_SLICE,
            *jobs_slice_properties(total),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await process.communicate()
    except OSError as exc:
        log.warning("jobs: could not configure %s: %s", JOBS_SLICE, exc)
        return False
    if process.returncode != 0:
        log.warning("jobs: could not configure %s: %s", JOBS_SLICE, err.decode(errors="replace"))
        return False
    return True


def _recording_exit(argv: list[str], exit_path: str) -> list[str]:
    """Wrap ``argv`` so its exit code lands in ``exit_path`` and the wrapper holds the slot.

    The wrapper keeps the slot's locked descriptor (passed to it alone), so the slot stays
    taken for exactly as long as the job runs, whichever runtime is watching. The command
    is started with every other descriptor closed, so nothing it leaves behind can hold
    the slot after the job has finished. Python rather than ``sh``: a shell redirection
    cannot portably name a descriptor above 9, and the runtime's usually is.
    """
    return [sys.executable, "-I", "-c", _EXIT_WRAPPER, exit_path, *argv]


# A command killed by a signal reports 128 + the signal, as a shell would.
_EXIT_WRAPPER = (
    "import os, subprocess, sys\n"
    "code = subprocess.call(sys.argv[2:])\n"
    "code = 128 - code if code < 0 else code\n"
    "tmp = sys.argv[1] + '.tmp'\n"
    "with open(tmp, 'w') as handle:\n"
    "    handle.write(f'{code}\\n')\n"
    "os.replace(tmp, sys.argv[1])\n"
    "sys.exit(code)\n"
)


def _drop_exit(job: Job) -> None:
    if job.exit_path:
        with contextlib.suppress(OSError):
            os.unlink(job.exit_path)


def _read_exit(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _process_start(pid: int) -> int | None:
    """The kernel's start time for ``pid`` (clock ticks since boot), or None if unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command name is in parentheses and may hold spaces; the fields after it do not.
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _still_running(job: Job) -> bool:
    """Whether a running row's process can be taken back: alive, or finished with a code."""
    if not job.exit_path or not job.pid:
        return False
    if _read_exit(job.exit_path) is not None:
        return True
    return job.pid_start is not None and _process_start(job.pid) == job.pid_start


def _terminate(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def _duration_repository(job: Job) -> str:
    """The project a job's running time is learned for, before its lane is added."""
    workspace = job.tested_workspace or {}
    return str(workspace.get("repository") or workspace.get("root") or job.cwd)


def _duration_key(job: Job) -> str:
    return job_durations.project_key(_duration_repository(job), job.lane)


def _counts_as_duration(job: Job) -> bool:
    """A passed run of a whole command someone asked for, which says how long the project takes.

    A follow-up reruns a handful of tests the queue picked, so it says nothing about the suite.
    """
    return (
        job.state == DONE
        and not job.follows
        and job.started_at is not None
        and job.finished_at is not None
        and job.finished_at > job.started_at
    )


def _lane_line(job: Job) -> str | None:
    if job.lane in REMOTE_LANES:
        return f"Ran in a pod on {lane_label(job.lane)}, not on this host."
    return None


def progress_message(job: Job, ran_for: float) -> str:
    """What a running job's chat is told every few minutes (#3038)."""
    if job.time_limit_s is None:
        limit = "no time limit"
    else:
        limit = f"time limit {job.time_limit_s / 60:.0f} min ({job.time_limit_basis})"
    lines = [
        f"Your queued job {job.id} is still running: {ran_for / 60:.0f} min so far, {limit}.",
        "Nothing to do if it looks fine; the result arrives when it ends. If it looks stuck, "
        f"cancel it with `~/agentdeck/.venv/bin/agentdeck job cancel {job.id}`.",
        f"Command: {' '.join(job.command)}",
        f"Working directory: {job.cwd}",
    ]
    lane = _lane_line(job)
    if lane:
        lines.append(lane.replace("Ran in", "Running in"))
    timings = read_timings(timings_path_for_log(job.log_path))
    if timings:
        lines.append("")
        lines.append(summarize(timings, now=time.time(), slowest=5))
    lines.append(f"Full output: {job.log_path}")
    tail = read_tail(job.log_path, limit=PROGRESS_TAIL_CHARS)
    lines.append("")
    if tail:
        lines.append("Last output:")
        lines.append(tail)
    else:
        lines.append("No output yet.")
    return "\n".join(lines)


def read_tail(path: str | Path, limit: int = NOTIFY_TAIL_CHARS) -> str:
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max(1, limit) * 4))
            data = handle.read(max(1, limit) * 4)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").rstrip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _cut_off_minutes(job: Job) -> int:
    """How long a job the restart killed had run (#2625): up to its log's last write, because
    the row is only marked when the new runtime starts, and the time in between ran nothing."""
    started = job.started_at or 0.0
    ended = job.finished_at or started
    with contextlib.suppress(OSError):
        ended = min(ended, Path(job.log_path).stat().st_mtime)
    return max(0, round((ended - started) / 60))


def job_message(job: Job) -> str:
    """What the submitting chat is told when its job ends."""
    outcome = {
        DONE: "finished successfully",
        FAILED: "failed",
        CANCELLED: "was cancelled",
        INTERRUPTED: "did not run",
    }.get(job.state, job.state)
    cut_off = job.state == INTERRUPTED and bool(job.started_at)
    if cut_off:
        outcome = f"was cut off by a runtime restart after {_cut_off_minutes(job)} min"
    lines = [f"Your queued job {job.id} {outcome}."]
    if job.exit_code is not None:
        lines.append(f"Exit code: {job.exit_code}")
    if job.started_at and job.finished_at and not cut_off:
        lines.append(f"Ran for {job.finished_at - job.started_at:.0f}s")
    lines.append(f"Command: {' '.join(job.command)}")
    lines.append(f"Working directory: {job.cwd}")
    lane = _lane_line(job)
    if lane:
        lines.append(lane)
    if job.note:
        lines.append(f"Note: {job.note}")
    lines.append(f"Full output: {job.log_path}")
    tail = read_tail(job.log_path)
    if tail:
        lines.append("")
        lines.append("Last output:")
        lines.append(tail)
    if job.state == INTERRUPTED:
        lines.append("")
        lines.append("Resubmit it with `agentdeck job submit` if you still need it.")
    return "\n".join(lines)
