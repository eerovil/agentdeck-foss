"""Host-wide slot pools: how many heavy things may run on this machine at once.

A slot is an exclusively ``flock``ed file. Locks rather than anything in-process
because the things being counted are separate processes with no shared memory —
several pytest runs in several worktrees, an npm test in another repo, a job the
persistent runtime started — and because the kernel drops a ``flock`` when its
holder dies, so a killed run cannot strand a slot.

Two pools exist and they are deliberately separate:

``browser``
    How many Playwright tests run at once, half the cores (#760). Owned by
    ``tests/conftest.py``.

``heavy``
    How many whole test suites or builds run at once: one for every five cores,
    so one on the four-core host this was written for (#1287, narrowed to one in
    #1304) and two on twelve threads (#3614). Held by a queued job for its whole
    life, and by a direct ``pytest`` through the conftest backstop.

``kube``
    How many suites run at once *off* this host, in dev-cluster pods (#1368).
    Separate from ``heavy`` because it counts something else: the local cost of
    a remote job is one ``kubectl`` streaming logs, so capping it at the local
    figure would throw away the whole point of running elsewhere. What it
    protects is the cluster — the CI nodes it lands on also carry Drone builds.

``mac``
    The same thing for the Mac's own Kubernetes (#1597). A pool of its own
    rather than a share of ``kube``, because the two count different clusters:
    the Mac's one node cannot fit what a CI node group fits, and a job waiting
    on the other cluster's queue would leave this one idle.

Merging them would deadlock: a heavy job holding a heavy slot runs a suite whose
browser tests then need slots from the same exhausted pool, and jobs raised
above one slot would wait on each other forever.

The directory is resolved without reading ``TMPDIR``. Host-wide has to mean the
same files for a systemd service and for an agent's shell, and those two do not
agree on ``TMPDIR``.
"""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SLOT_DIR_ENV = "AGENTDECK_SLOT_DIR"
HEAVY_SLOT_ENV = "AGENTDECK_HEAVY_SLOTS"
HEAVY_SLOT_HELD_ENV = "AGENTDECK_HEAVY_SLOT_HELD"
# Set on everything the job queue runs, here or in a pod (#2666). Unlike the
# held-slot marker it is not also a manual escape hatch, so the conftest can tell
# a queued suite, which may use every core it was given, from a direct run here.
QUEUED_JOB_ENV = "AGENTDECK_QUEUED_JOB"
KUBE_SLOT_ENV = "AGENTDECK_KUBE_SLOTS"
MAC_SLOT_ENV = "AGENTDECK_MAC_SLOTS"
DESKTOP_SLOT_ENV = "AGENTDECK_DESKTOP_SLOTS"

# A heavy slot per this many usable cores (#3614). A queued job runs at low priority in
# its own slice (``jobs.JOBS_SLICE``), so a second suite can no longer freeze the deck;
# it only shares the cores with the first, and below ten threads that is a loss.
CORES_PER_HEAVY_SLOT = 5
DEFAULT_KUBE_SLOTS = 3
DEFAULT_MAC_SLOTS = 2
DEFAULT_DESKTOP_SLOTS = 1
SLOT_TIMEOUT_S = 900.0
_POLL_S = 0.05


# How much memory this host must have free before it starts another heavy job (#3614): a
# whole suite takes about what a pod asks for, 6 GB. Read from the environment so a test
# can pin it rather than depend on how busy the host running it is.
LOCAL_JOB_MIN_MEMORY_ENV = "AGENTDECK_LOCAL_JOB_MIN_MEMORY_MB"
DEFAULT_LOCAL_JOB_MIN_MEMORY_MB = 6144


def usable_cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - not Linux
        return os.cpu_count() or 2


def default_heavy_slots(cores: int | None = None) -> int:
    """One heavy slot per ``CORES_PER_HEAVY_SLOT`` usable cores, and never none."""
    return max(1, (usable_cores() if cores is None else cores) // CORES_PER_HEAVY_SLOT)


def local_suite_workers(cores: int | None = None, slots: int | None = None) -> int:
    """How many xdist workers one queued local suite gets: the cores split between the
    suites that may run at once, so two suites do not each start one per core (#3614)."""
    cores = usable_cores() if cores is None else cores
    slots = heavy_slot_count() if slots is None else slots
    return max(1, cores // max(1, slots))


def memory_available_bytes(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """The kernel's ``MemAvailable``, or None where it cannot be read."""
    try:
        text = meminfo.read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            try:
                return int(parts[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def local_job_min_memory_bytes() -> int:
    raw = os.environ.get(LOCAL_JOB_MIN_MEMORY_ENV, "").strip()
    try:
        megabytes = int(raw) if raw else DEFAULT_LOCAL_JOB_MIN_MEMORY_MB
    except ValueError:
        megabytes = DEFAULT_LOCAL_JOB_MIN_MEMORY_MB
    return max(0, megabytes) * 1024 * 1024


# How long a start keeps its share of memory reserved (#3614). A suite takes most of a
# minute to import, collect and start its workers, and until then ``MemAvailable`` still
# counts the memory it is about to use, so two starts a second apart would both pass.
# An assumption the owner accepted on review rather than tracked use: a job that takes
# longer than this to reach its memory can still overlap a second start. The jobs slice's
# MemoryHigh/MemoryMax (``jobs.jobs_slice_properties``) keeps that from reaching the deck;
# the cost is a suite slowed or killed, not a frozen host.
MEMORY_RESERVE_S = 120.0


class MemoryGate:
    """Whether this host has the memory to start one more heavy job (#3614).

    Low priority keeps a busy job from taking the deck's CPU, but not its memory, so a job
    waits while less than ``local_job_min_memory_bytes`` is free. Each start reserves that
    much for ``MEMORY_RESERVE_S``, until its real use shows in ``MemAvailable``; one gate
    is shared by the job queue and the slot leases, so neither can spend what the other just
    took. A host whose ``/proc/meminfo`` cannot be read is not held back: the slot count
    still bounds it.
    """

    def __init__(
        self,
        *,
        available: Callable[[], int | None] = memory_available_bytes,
        floor: Callable[[], int] = local_job_min_memory_bytes,
        clock: Callable[[], float] = time.monotonic,
        reserve_s: float = MEMORY_RESERVE_S,
    ) -> None:
        self._available = available
        self._floor = floor
        self._clock = clock
        self._reserve_s = reserve_s
        self._reserved: list[tuple[float, int]] = []

    def reserved_bytes(self) -> int:
        now = self._clock()
        self._reserved = [(until, size) for until, size in self._reserved if until > now]
        return sum(size for _until, size in self._reserved)

    def ok(self) -> bool:
        """Whether one more start fits now, counting the starts still reserved."""
        floor = self._floor()
        if floor == 0:
            return True
        available = self._available()
        return available is None or available - self.reserved_bytes() >= floor

    def admit(self) -> None:
        """Record a start: its share stays reserved until its own use is visible."""
        floor = self._floor()
        if floor:
            self._reserved.append((self._clock() + self._reserve_s, floor))


def slot_root() -> Path:
    """The directory both the runtime and a bare shell will pick.

    ``$XDG_RUNTIME_DIR`` is the natural home but the variable is often unset in
    a service; the canonical ``/run/user/<uid>`` path is the same directory
    without the disagreement. ``TMPDIR`` is never consulted for the same reason.
    """
    override = os.environ.get(SLOT_DIR_ENV, "").strip()
    if override:
        return Path(override)
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    if runtime_dir.is_dir():
        return runtime_dir
    if sys.platform == "darwin":
        # A launchd job and a Terminal shell get different TMPDIRs on a Mac (#3623).
        return Path.home() / ".cache" / "agentdeck"
    return Path(tempfile.gettempdir())


def slot_directory(name: str) -> Path:
    """The pool directory for ``name``, created if missing.

    The uid is in the name because the fallback root is world-writable, and the
    files are never cleaned: they are lock targets, empty and reused.
    """
    directory = slot_root() / f"agentdeck-{name}-slots-{os.getuid()}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def slot_paths(name: str, count: int, directory: Path | None = None) -> list[Path]:
    directory = slot_directory(name) if directory is None else directory
    directory.mkdir(mode=0o700, exist_ok=True)
    return [directory / f"slot-{index}" for index in range(count)]


def try_take_slot(paths: list[Path]) -> int | None:
    """One sweep for a free slot. An fd holding the lock, or None right now."""
    for path in paths:
        # O_NOFOLLOW: the fallback slot directory can sit on a predictable /tmp
        # path, so refuse to follow a symlink planted where a slot file should be.
        candidate = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(candidate)
            continue
        return candidate
    return None


def take_a_slot(paths: list[Path], deadline: float) -> int | None:
    """An fd holding an exclusive flock on one of ``paths``, or None by ``deadline``."""
    while True:
        fd = try_take_slot(paths)
        if fd is not None:
            return fd
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_S)


@contextmanager
def hold_slot(paths: list[Path], timeout: float = SLOT_TIMEOUT_S) -> Iterator[bool]:
    """Hold one of ``paths`` for the block. Yields whether a slot was won.

    On timeout it yields False and the caller runs uncapped: an oversubscribed
    run is slow, but a run that hangs forever on a lost slot is worse.
    """
    deadline = time.monotonic() + timeout
    fd = take_a_slot(paths, deadline)
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        # flock is released by the close; closing is the whole teardown.
        os.close(fd)


@dataclass(frozen=True)
class SlotHolder:
    """The process holding one slot, as far as ``/proc`` can say.

    ``started_at`` is when the *process* started, not when it took the lock.
    For the two things that hold a heavy slot for their whole life — a queued
    job's child and a direct ``pytest`` through the conftest backstop — those
    are the same moment to within a second, and reading a real lock-acquired
    time would mean every holder cooperating by writing one down.
    """

    pid: int
    command: str
    cwd: str
    started_at: float


def _unescape_mount_field(field: str) -> str:
    """A ``mountinfo`` field with the kernel's octal escapes put back.

    Space, tab, newline and backslash are printed as ``\\040``, ``\\011``,
    ``\\012`` and ``\\134``, because the file is space separated. A mount point
    under a directory with a space in its name is otherwise unmatchable.
    """
    if "\\" not in field:
        return field
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 3 < len(field):
            digits = field[index + 1 : index + 4]
            if len(digits) == 3 and all(digit in "01234567" for digit in digits):
                out.append(chr(int(digits, 8)))
                index += 4
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _mount_devices(text: str | None = None) -> list[tuple[str, tuple[int, int]]]:
    """(mount point, (major, minor)) for every mount, in mount-table order.

    The device here is the *superblock's*, which is the number ``/proc/locks``
    prints — and on btrfs it is not the number ``stat`` reports. See
    ``_superblock_device`` for why that distinction is the whole point.

    ``text`` is the file's contents, for tests: the host's real mount table is
    whatever the host happens to have mounted.
    """
    mounts: list[tuple[str, tuple[int, int]]] = []
    if text is None:
        try:
            text = Path("/proc/self/mountinfo").read_text()
        except OSError:  # pragma: no cover - not Linux, or /proc not mounted
            return mounts
    for line in text.splitlines():
        # "36 35 98:0 / /var rw,... - btrfs /dev/sda1 rw,subvol=/var". Fields
        # up to the separating "-" are fixed; optional tags sit before it, so
        # only the first five are safe to index.
        parts = line.split()
        if len(parts) < 5:
            continue
        device = parts[2].split(":")
        if len(device) != 2:
            continue
        try:
            major, minor = int(device[0]), int(device[1])
        except ValueError:
            continue
        mounts.append((_unescape_mount_field(parts[4]), (major, minor)))
    return mounts


def _superblock_device(
    path: Path, mounts: list[tuple[str, tuple[int, int]]]
) -> tuple[int, int] | None:
    """The device ``/proc/locks`` will print for a lock on ``path``.

    ``stat().st_dev`` is the obvious answer and it is wrong on btrfs. Every
    subvolume gets its own *anonymous* device number, so a file under one stats
    as (say) ``0:57`` while its filesystem's superblock — the number the kernel
    prints in ``/proc/locks`` — is ``0:35``. Keying a lock by ``st_dev`` there
    never matches anything, and every held slot reads as free (#1443).

    ``/proc/self/mountinfo`` carries the superblock's ``major:minor`` per mount,
    so the answer is the mount the file lives under: the longest mount point
    that is a path-component prefix of it. Later entries win a tie, because a
    mount stacked on an occupied mount point shadows the one beneath it.

    Returns None when the mount table says nothing about this path, leaving the
    caller to fall back to ``st_dev`` rather than lose the device half of the
    key entirely.
    """
    resolved = os.path.realpath(path)
    best: tuple[int, int] | None = None
    best_length = -1
    for point, device in mounts:
        if resolved == point or resolved.startswith(point.rstrip("/") + "/"):
            if len(point) >= best_length:
                best, best_length = device, len(point)
    return best


def _flock_holders(text: str | None = None) -> dict[tuple[int, int, int], int]:
    """(major, minor, inode) -> pid, for every write ``flock`` *held* here.

    The key carries the filesystem device because an inode number is only
    unique within one filesystem: some unrelated lock on another mount can
    perfectly well reuse a slot file's inode number, and keying on the number
    alone would report that stranger as the holder of the slot.

    ``/proc/locks`` lists blocked waiters as well as holders, as
    ``N: -> FLOCK ...``. Skipping them is the other subtlety here: a waiter is
    precisely what must never be reported as the holder, or a queue blocked on
    a slot gets named as the thing blocking it.

    ``text`` is the file's contents, for tests that need a waiter present —
    producing a real blocked waiter takes a second process parked on a lock.
    """
    holders: dict[tuple[int, int, int], int] = {}
    if text is None:
        try:
            text = Path("/proc/locks").read_text()
        except OSError:  # pragma: no cover - not Linux, or /proc not mounted
            return holders
    for line in text.splitlines():
        parts = line.split()
        # "26: FLOCK ADVISORY WRITE <pid> <maj>:<min>:<inode> 0 EOF". A waiter
        # has "->" at parts[1], so requiring FLOCK there drops it.
        if len(parts) < 6 or parts[1] != "FLOCK":
            continue
        device = parts[5].split(":")
        if len(device) != 3:
            continue
        try:
            pid = int(parts[4])
            # The kernel prints the device as two hex bytes and the inode as
            # decimal, so "00:2d:99" is minor 45 and not minor 23.
            key = (int(device[0], 16), int(device[1], 16), int(device[2]))
        except ValueError:
            continue
        holders.setdefault(key, pid)
    return holders


def _describe_process(pid: int) -> SlotHolder | None:
    """What ``pid`` is, or None if it exited while we were looking at it."""
    proc = Path(f"/proc/{pid}")
    try:
        started_at = proc.stat().st_ctime
        raw = (proc / "cmdline").read_bytes()
    except OSError:
        return None
    command = " ".join(part for part in raw.decode(errors="replace").split("\0") if part)
    if not command:
        # A kernel thread, or a process that has already been reaped far enough
        # to have an empty cmdline. Its name is still better than nothing.
        try:
            command = (proc / "comm").read_text().strip()
        except OSError:
            command = ""
    try:
        cwd = os.readlink(proc / "cwd")
    except OSError:
        # Another user's process, or one exiting. Not knowing where it runs
        # does not make the rest of the answer useless.
        cwd = ""
    return SlotHolder(pid=pid, command=command, cwd=cwd, started_at=started_at)


# How many more times ``slot_holders`` reads ``/proc/locks`` for a slot that is
# locked but was missing from the read (#3838).
_LOCKS_REREADS = 5


def _is_locked(path: Path) -> bool:
    """Whether some open file holds a ``flock`` on ``path`` right now.

    A non-blocking shared probe, dropped at once: it fails exactly when a holder
    has the slot's exclusive lock. A taker racing the probe skips this slot for
    that instant, which its own sweep already tolerates.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def slot_holders(paths: list[Path]) -> dict[Path, SlotHolder | None]:
    """Who holds each of ``paths`` right now; None where the slot is free.

    ``/proc/locks`` is not one snapshot: the kernel hands it out a page per
    ``read``, and a lock released between two reads shifts every later line, so
    one can be skipped. On a host with thousands of locks that is about one read
    in a hundred, and a held slot read as free (#3838). A slot missing from the
    read is therefore probed, and only a slot that really is locked costs more
    reads to find its holder.
    """
    mounts = _mount_devices()
    keys: dict[Path, tuple[int, int, int] | None] = {}
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            # No file yet means nobody has ever taken this slot: free.
            keys[path] = None
            continue
        device = _superblock_device(path, mounts) or (
            os.major(stat.st_dev),
            os.minor(stat.st_dev),
        )
        keys[path] = (*device, stat.st_ino)
    flocks = _flock_holders()
    for _ in range(_LOCKS_REREADS):
        missed = [
            key
            for path, key in keys.items()
            if key is not None and key not in flocks and _is_locked(path)
        ]
        if not missed:
            break
        for key, pid in _flock_holders().items():
            flocks.setdefault(key, pid)
    found: dict[Path, SlotHolder | None] = {}
    for path, key in keys.items():
        pid = None if key is None else flocks.get(key)
        found[path] = None if pid is None else _describe_process(pid)
    return found


def describe_heavy_slots(
    paths: list[Path],
    *,
    jobs: dict[int, str] | None = None,
    leases: dict[int, tuple[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """What is holding each heavy slot, for the runtime to publish (#1391).

    ``jobs`` maps a slot's *inode* to the job id holding it, and ``leases`` to
    the holding lease's ``(label, acquired-at)``. Both are passed in rather
    than looked up, because only the runtime knows them and this module must
    not import back into it. A lease carries its own acquired-at because the
    process holding its lock is the runtime, whose start time says nothing
    about when this lease began.

    They matter because the runtime holds those locks in its own process: a
    ``/proc`` reading alone would attribute every queued job and every API lease
    to one pid called ``agentdeck``, which is true and useless. Anything left
    over is genuinely somebody else — the conftest backstop, or a suite someone
    ran by hand — and that is the case the queue panel could not explain.
    """
    jobs = jobs or {}
    leases = leases or {}
    described: list[dict[str, Any]] = []
    for index, (path, holder) in enumerate(slot_holders(paths).items()):
        try:
            inode: int | None = path.stat().st_ino
        except OSError:
            inode = None
        entry: dict[str, Any] = {"slot": index}
        if holder is None:
            entry["kind"] = "free"
        elif inode is not None and inode in jobs:
            entry["kind"] = "job"
            entry["job"] = jobs[inode]
        elif inode is not None and inode in leases:
            label, since = leases[inode]
            entry["kind"] = "lease"
            entry["label"] = label
            entry["since"] = since
        else:
            entry["kind"] = "process"
            entry["pid"] = holder.pid
            entry["command"] = holder.command
            entry["cwd"] = holder.cwd
            entry["since"] = holder.started_at
        described.append(entry)
    return described


def heavy_slot_count() -> int:
    """How many whole suites/builds may run at once on this host.

    One on the four-core host this was written for. #1287 set it to two on the
    theory that oversubscribing by 2x was survivable; measuring it in #1304 said
    otherwise — one ``pytest -n auto`` is already four workers plus browsers, so
    a second suite only takes CPU away from the first, and the deck itself is
    not free either. Serialising costs the second job wall-clock it was largely
    losing to contention anyway.

    Since #3614 the default follows the core count (``default_heavy_slots``): a
    queued job runs at low priority, so the deck no longer pays for a second
    suite, and twelve threads split between two suites lose little to switching.
    """
    return _configured_count(HEAVY_SLOT_ENV, default_heavy_slots())


def kube_slot_count() -> int:
    """How many suites may run in dev-cluster pods at once.

    Three by default: enough that a few worktrees stop queueing behind each
    other, small enough that the pods (3 cores and 6 GB each) do not crowd Drone
    off the two CI nodes they share. Raise it only alongside the CI node-group
    arithmetic of the cluster they run on.
    """
    return _configured_count(KUBE_SLOT_ENV, DEFAULT_KUBE_SLOTS)


def mac_slot_count() -> int:
    """How many suites may run in pods on the Mac's Kubernetes at once (#1597).

    Two, because that is what the node holds: eight cores and 24 GB against a
    pod asking for three cores and 6 GB, with the rest left for the machine
    itself. Lower than ``kube`` for the plain reason that it is one node rather
    than a CI node group.
    """
    return _configured_count(MAC_SLOT_ENV, DEFAULT_MAC_SLOTS)


def desktop_slot_count() -> int:
    """How many suites may run in pods on the desktop's Kubernetes at once (#2297).

    One, because that is what the node has left: eight cores and 16 GB, of which
    its own workloads already ask for about three cores and 6 GB, against a pod
    asking for 2.5 cores and 6 GB. A second pod would sit Pending until the
    no-room timeout; with one slot a second job goes straight to the kube lane
    instead.
    """
    return _configured_count(DESKTOP_SLOT_ENV, DEFAULT_DESKTOP_SLOTS)


def _configured_count(variable: str, default: int) -> int:
    """A positive integer from ``variable``, or ``default``.

    An unparseable or non-positive override is ignored rather than raising: this
    is read from a systemd service and from a pytest hook, and a queue that
    refuses to start because a stray variable is misspelled is worse than one
    that runs at the default.
    """
    raw = os.environ.get(variable, "").strip()
    if raw:
        try:
            requested = int(raw)
        except ValueError:
            return default
        if requested >= 1:
            return requested
    return default


def heavy_slot_paths(count: int | None = None, directory: Path | None = None) -> list[Path]:
    return slot_paths("heavy", heavy_slot_count() if count is None else count, directory)


def kube_slot_paths(count: int | None = None, directory: Path | None = None) -> list[Path]:
    return slot_paths("kube", kube_slot_count() if count is None else count, directory)


def mac_slot_paths(count: int | None = None, directory: Path | None = None) -> list[Path]:
    return slot_paths("mac", mac_slot_count() if count is None else count, directory)


def desktop_slot_paths(
    count: int | None = None, directory: Path | None = None, name: str = "desktop"
) -> list[Path]:
    """``name`` is the pool's file name: ``jobs`` passes the lane's older name while it has
    one, so a job started before the rename still holds a slot of the same pool (#3623)."""
    return slot_paths(name, desktop_slot_count() if count is None else count, directory)


def heavy_slot_is_held() -> bool:
    """Whether an ancestor of this process already holds a heavy slot.

    Set in the environment of anything the job queue starts, so a suite running
    *as* a queued job does not queue behind itself for a second slot.
    """
    return os.environ.get(HEAVY_SLOT_HELD_ENV, "").strip() not in ("", "0")
