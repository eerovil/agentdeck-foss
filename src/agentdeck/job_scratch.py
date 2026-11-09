"""The folder that keeps test runs' temp files off the deck's disk (#4143).

Tests are full of small synced writes. On the deck's btrfs disk they queue with its state
writes and stretch its commits to seconds, so ``AGENTDECK_JOB_SCRATCH`` names a folder on
another disk. A queued job waits, or moves to a remote lane, while that folder cannot be
used (``JobQueue._start_what_fits``). A chat cannot wait, so it starts with the runtime's
own ``TMPDIR`` then, and its runs there are the small focused ones.

Keep the path short: a Unix socket path has room for 107 characters, and
``/var/mnt/ssd/agentdeck-scratch`` was already too long for the suite's own sockets.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

JOB_SCRATCH_ENV = "AGENTDECK_JOB_SCRATCH"
# That disk may hold other things too (here podman's storage), so jobs leave it this much.
JOB_SCRATCH_MIN_FREE_BYTES = 10 * 10**9
_MOUNTS = Path("/proc/self/mountinfo")


def scratch_root() -> str:
    """The configured folder, empty when this host sets none."""
    return os.environ.get(JOB_SCRATCH_ENV, "").strip()


def _disk_of(path: str) -> str:
    """What ``path`` is stored on: the device of its mount, or its ``st_dev`` off Linux.

    Not ``st_dev`` alone: two btrfs subvolumes of one disk report different ones.
    """
    real, mount_point, device = os.path.realpath(path), "", ""
    try:
        lines = _MOUNTS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return str(os.stat(path).st_dev)
    for line in lines:
        before, _, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        mount = fields[4]
        inside = real == mount or real.startswith(mount.rstrip("/") + "/")
        if inside and len(mount) >= len(mount_point):  # A later mount hides an earlier one.
            mount_point, device = mount, tail[1]
    return device or str(os.stat(path).st_dev)


def scratch_problem(deck_path: Path) -> str | None:
    """Why the folder cannot take temp files now, or None when it can or none is set.

    It is never created here: with the disk unmounted, that would put it on the disk
    beneath the mount point, which may well be the deck's own.
    """
    root = scratch_root()
    if not root:
        return None
    try:
        if not os.path.isdir(root):
            return f"{root} is not there; is its disk mounted?"
        if _disk_of(root) == _disk_of(str(deck_path)):
            return f"{root} is on the deck's own disk; is its disk mounted?"
        if not os.access(root, os.W_OK | os.X_OK):
            return f"{root} is not writable"
        if shutil.disk_usage(root).free < JOB_SCRATCH_MIN_FREE_BYTES:
            return f"{root} is short of room"
    except OSError as exc:
        return f"{root} is unusable: {exc}"
    return None


def point_chat_temp_files(env: dict[str, str]) -> None:
    """Send a chat's own commands, direct test runs included, to the folder.

    The variable itself stays behind: a test run inside the chat must not read this
    host's setting, or its throwaway queues would judge their own temp folders by it.
    """
    root = scratch_root()
    env.pop(JOB_SCRATCH_ENV, None)
    if not root:
        return
    problem = scratch_problem(Path.home())
    if problem is None:
        env["TMPDIR"] = root
    else:
        log.warning("a chat keeps its TMPDIR: %s", problem)
