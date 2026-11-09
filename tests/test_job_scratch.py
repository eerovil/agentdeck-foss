"""The folder that keeps test runs' temp files off the deck's disk (#4143)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentdeck import job_scratch
from agentdeck.job_scratch import (
    JOB_SCRATCH_ENV,
    point_chat_temp_files,
    scratch_problem,
)

# This host's real table: /var and /var/home are two subvolumes of one disk.
MOUNTS = """\
69 77 0:34 /var /var rw,relatime shared:164 - btrfs /dev/nvme0n1p3 rw,subvol=/var
149 69 0:34 /home /var/home rw,relatime shared:217 - btrfs /dev/nvme0n1p3 rw,subvol=/home
264 69 8:1 / /var/mnt/ssd rw,relatime shared:229 - ext4 /dev/sda1 rw,seclabel
"""


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    folder = tmp_path / "scratch"
    folder.mkdir()
    monkeypatch.setenv(JOB_SCRATCH_ENV, str(folder))
    monkeypatch.setattr(job_scratch, "_disk_of", lambda path: path)
    monkeypatch.setattr(job_scratch, "JOB_SCRATCH_MIN_FREE_BYTES", 0)
    return folder


def test_disks_come_from_the_mount_table_so_subvolumes_count_as_one(tmp_path, monkeypatch):
    table = tmp_path / "mountinfo"
    table.write_text(MOUNTS + "broken line\n")
    monkeypatch.setattr(job_scratch, "_MOUNTS", table)
    monkeypatch.setattr(job_scratch.os.path, "realpath", lambda path: path)

    assert job_scratch._disk_of("/var/mnt/ssd/tmp") == "/dev/sda1"
    assert job_scratch._disk_of("/var/home/someone") == "/dev/nvme0n1p3"
    # The SSD unmounted: the same path now sits on the deck's disk, and this says so.
    table.write_text(MOUNTS.replace("/var/mnt/ssd", "/var/mnt/sdz"))
    assert job_scratch._disk_of("/var/mnt/ssd/tmp") == "/dev/nvme0n1p3"
    assert job_scratch._disk_of("/var/mnt/ssdx") == "/dev/nvme0n1p3"


def test_each_way_the_folder_can_fail(scratch, tmp_path, monkeypatch):
    home = tmp_path / "home"
    assert scratch_problem(home) is None

    monkeypatch.setattr(job_scratch, "JOB_SCRATCH_MIN_FREE_BYTES", 10**18)
    assert "short of room" in scratch_problem(home)
    monkeypatch.setattr(job_scratch, "JOB_SCRATCH_MIN_FREE_BYTES", 0)

    monkeypatch.setattr(job_scratch.os, "access", lambda *_: False)
    assert "not writable" in scratch_problem(home)
    monkeypatch.undo()

    monkeypatch.setenv(JOB_SCRATCH_ENV, str(tmp_path / "gone"))
    assert "not there" in scratch_problem(home)
    assert not (tmp_path / "gone").exists()

    monkeypatch.setenv(JOB_SCRATCH_ENV, str(tmp_path))
    monkeypatch.setattr(job_scratch, "_disk_of", lambda path: "/dev/same")
    assert "own disk" in scratch_problem(home)

    monkeypatch.setenv(JOB_SCRATCH_ENV, "  ")
    assert scratch_problem(home) is None


def test_a_chat_gets_the_folder_and_never_the_setting(scratch, monkeypatch):
    env = {"TMPDIR": "/var/tmp", JOB_SCRATCH_ENV: str(scratch)}
    point_chat_temp_files(env)
    assert env == {"TMPDIR": str(scratch)}

    monkeypatch.setattr(job_scratch, "_disk_of", lambda path: "/dev/same")
    env = {"TMPDIR": "/var/tmp", JOB_SCRATCH_ENV: str(scratch)}
    point_chat_temp_files(env)
    assert env == {"TMPDIR": "/var/tmp"}

    monkeypatch.delenv(JOB_SCRATCH_ENV)
    env = {"TMPDIR": "/var/tmp"}
    point_chat_temp_files(env)
    assert env == {"TMPDIR": "/var/tmp"}


def test_every_place_that_starts_a_chat_points_its_temp_files():
    """Each sets up a chat's environment by hand, so a new one is easy to miss."""
    root = Path(job_scratch.__file__).parent / "providers"
    counts = {
        "claude_code/worker.py": 1,
        "codex/appserver.py": 1,
        "codex/inject.py": 2,
    }
    for name, count in counts.items():
        text = (root / name).read_text(encoding="utf-8")
        assert text.count("point_chat_temp_files(env)") == count, name
        assert text.count("gh_shim.worker_path(env)") == count, name
