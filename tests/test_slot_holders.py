"""Naming whatever is holding a heavy slot (#1391).

The queue panel could say "0 running, 4 waiting" and offer no way at all to find
out why. A heavy slot is held by anything on the host — a queued job, an API
lease, a direct ``pytest`` through the conftest backstop — and only the first of
those was ever visible. These cover reading a holder back out of the kernel, and
the classification that keeps the runtime's own two lock owners from being
reported as a mystery process.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentdeck.host_slots import (
    SlotHolder,
    _flock_holders,
    _mount_devices,
    _superblock_device,
    describe_heavy_slots,
    slot_holders,
    slot_paths,
    try_take_slot,
)

MOUNTINFO = "\n".join(
    [
        "24 30 0:22 / / rw,relatime shared:1 - overlay overlay rw",
        "30 24 8:2 / /boot rw,relatime shared:2 - ext4 /dev/sda2 rw",
        "31 24 0:35 /var /var rw,relatime shared:3 - btrfs /dev/sda3 rw,subvol=/var",
        "32 24 0:82 / /run/user/1000 rw,relatime shared:4 - tmpfs tmpfs rw",
        "33 24 0:91 / /mnt/with\\040space rw,relatime shared:5 - tmpfs tmpfs rw",
    ]
)


@pytest.fixture
def slots(tmp_path):
    return slot_paths("heavy", 2, directory=tmp_path)


def test_reads_a_real_lock_back_out_of_the_kernel(slots):
    fd = try_take_slot(slots)
    assert fd is not None
    try:
        found = slot_holders(slots)
        held = found[slots[0]]
        assert held is not None
        assert held.pid == os.getpid()
        # This test process is a pytest, so its own command line is the proof
        # that a holder's identity survives the round trip through /proc.
        assert held.command
        assert held.cwd == os.getcwd()
        # The second slot was never taken, so it has no holder to name.
        assert found[slots[1]] is None
    finally:
        os.close(fd)


def test_an_untaken_slot_file_is_free(slots):
    assert slot_holders(slots) == {slots[0]: None, slots[1]: None}


def test_a_blocked_waiter_is_not_reported_as_the_holder():
    """The pid parked *on* a lock must never be named as the pid holding it."""
    locks = "\n".join(
        [
            "1: FLOCK  ADVISORY  WRITE 4242 00:23:99 0 EOF",
            "1: -> FLOCK  ADVISORY  WRITE 5353 00:23:99 0 EOF",
        ]
    )
    assert _flock_holders(locks) == {(0, 0x23, 99): 4242}


def test_unparseable_lock_lines_are_skipped_not_fatal():
    locks = "\n".join(
        [
            "nonsense",
            "2: POSIX  ADVISORY  READ 7 00:23:11 0 EOF",
            "3: FLOCK  ADVISORY  WRITE notapid 00:23:12 0 EOF",
            "4: FLOCK  ADVISORY  WRITE 88 00:23:13 0 EOF",
            "5: FLOCK  ADVISORY  WRITE 89 nodevice 0 EOF",
        ]
    )
    assert _flock_holders(locks) == {(0, 0x23, 13): 88}


def test_the_device_is_read_as_hex_like_the_kernel_prints_it():
    """``/proc/locks`` prints major:minor in hex and the inode in decimal."""
    locks = "6: FLOCK  ADVISORY  WRITE 77 00:2d:64286934 0 EOF"
    assert _flock_holders(locks) == {(0, 45, 64286934): 77}


def test_the_mount_table_is_parsed_into_mount_points_and_devices():
    """``mountinfo`` fields up to the ``-`` separator, with escapes undone.

    Only the first five fields are safe to index: optional propagation tags sit
    between them and the separator. The device is printed in decimal here,
    unlike in ``/proc/locks``.
    """
    assert _mount_devices(MOUNTINFO) == [
        ("/", (0, 22)),
        ("/boot", (8, 2)),
        ("/var", (0, 35)),
        ("/run/user/1000", (0, 82)),
        ("/mnt/with space", (0, 91)),
    ]


def test_the_device_is_the_longest_mount_point_the_path_lives_under(tmp_path):
    """A prefix has to be a whole path component.

    The mount points are built under ``tmp_path`` rather than written out as
    plausible real paths, because the lookup resolves symlinks first and this
    host symlinks several top-level directories into ``/var``.
    """
    root = Path(os.path.realpath(tmp_path))
    mounts = [
        ("/", (0, 22)),
        (str(root / "boot"), (8, 2)),
        (str(root / "var"), (0, 35)),
        (str(root / "with space"), (0, 91)),
    ]
    assert _superblock_device(root / "var/tmp/slots/slot-0", mounts) == (0, 35)
    assert _superblock_device(root / "var", mounts) == (0, 35)
    assert _superblock_device(root / "boot/grub/grub.cfg", mounts) == (8, 2)
    # Not the "boot" mount: only a whole component counts as a prefix.
    assert _superblock_device(root / "bootstrap/thing", mounts) == (0, 22)
    # Nothing else matches, so the root mount does.
    assert _superblock_device(root / "etc/hostname", mounts) == (0, 22)
    # A mount point the kernel had to octal-escape is matched unescaped.
    assert _superblock_device(root / "with space/slot-0", mounts) == (0, 91)


def test_a_mount_stacked_on_an_occupied_mount_point_shadows_the_one_beneath(tmp_path):
    """Two mounts can share a mount point; the later entry is the visible one."""
    root = Path(os.path.realpath(tmp_path))
    mounts = [("/", (0, 22)), (str(root / "srv"), (0, 50)), (str(root / "srv"), (0, 51))]
    assert _superblock_device(root / "srv/slot-0", mounts) == (0, 51)


def test_an_unreadable_or_unhelpful_mount_table_yields_no_device():
    assert _mount_devices("") == []
    assert _superblock_device(Path("/var/tmp/slot-0"), []) is None
    # Short lines and unparseable devices are skipped rather than fatal.
    assert _mount_devices("nonsense\n24 30 notadevice / / rw - overlay overlay rw") == []


def test_a_slot_whose_stat_device_is_not_its_superblock_device_is_still_found(slots, monkeypatch):
    """The btrfs case that made every held slot read as free (#1443).

    On btrfs a subvolume's files carry their own anonymous ``st_dev``, while
    ``/proc/locks`` prints the filesystem superblock's device. Keying the lock
    by ``st_dev`` therefore never matched, and ``slot_holders`` reported a slot
    somebody was really holding as free.
    """
    slots[0].touch()
    stat = slots[0].stat()
    # A superblock device deliberately unequal to this file's st_dev, the way a
    # btrfs subvolume's is.
    superblock = (0, os.minor(stat.st_dev) + 1)
    mount_point = os.path.realpath(slots[0].parent)
    monkeypatch.setattr(
        "agentdeck.host_slots._mount_devices",
        lambda: [("/", (0, 22)), (mount_point, superblock)],
    )
    printed = f"{superblock[0]:02x}:{superblock[1]:02x}:{stat.st_ino}"
    locks = f"1: FLOCK  ADVISORY  WRITE 7171 {printed} 0 EOF"
    monkeypatch.setattr("agentdeck.host_slots._flock_holders", lambda: _flock_holders(locks))
    monkeypatch.setattr(
        "agentdeck.host_slots._describe_process",
        lambda pid: SlotHolder(pid=pid, command="held", cwd="", started_at=0.0),
    )
    held = slot_holders(slots)[slots[0]]
    assert held is not None
    assert held.pid == 7171


def test_a_real_lock_is_found_even_with_no_mount_table_to_consult(slots, monkeypatch):
    """Without ``mountinfo`` the device half of the key falls back to ``st_dev``.

    That is wrong on btrfs, which is the bug, but losing the device entirely
    would reintroduce the cross-filesystem inode collision of #1397. A useful
    guess beats no key at all.
    """
    monkeypatch.setattr("agentdeck.host_slots._mount_devices", lambda: [])
    stat_key = None
    fd = try_take_slot(slots)
    assert fd is not None
    try:
        stat = slots[0].stat()
        stat_key = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
        monkeypatch.setattr(
            "agentdeck.host_slots._flock_holders",
            lambda: {stat_key: os.getpid()},
        )
        held = slot_holders(slots)[slots[0]]
        assert held is not None
        assert held.pid == os.getpid()
    finally:
        os.close(fd)


def test_an_inode_number_reused_on_another_filesystem_is_not_the_holder(slots, monkeypatch):
    """Inode numbers are only unique within one filesystem.

    A lock held on some unrelated mount can carry the same inode number as a
    slot file, and matching on the number alone would name that stranger as the
    process holding the slot.
    """
    slots[0].touch()
    stat = slots[0].stat()
    # The slot's *superblock* device, not its ``st_dev``: on btrfs those differ
    # and only the former is what ``/proc/locks`` prints (#1443).
    major, minor = _superblock_device(slots[0], _mount_devices())
    inode = stat.st_ino
    # Same inode number, one on this slot's device and one on a made-up other
    # device that cannot collide with it.
    locks = "\n".join(
        [
            f"1: FLOCK  ADVISORY  WRITE 5151 {major + 1:02x}:{minor:02x}:{inode} 0 EOF",
            f"2: FLOCK  ADVISORY  WRITE 6262 {major:02x}:{minor:02x}:{inode} 0 EOF",
        ]
    )
    monkeypatch.setattr("agentdeck.host_slots._flock_holders", lambda: _flock_holders(locks))
    monkeypatch.setattr(
        "agentdeck.host_slots._describe_process",
        lambda pid: SlotHolder(pid=pid, command="held", cwd="", started_at=0.0),
    )
    found = slot_holders(slots)
    held = found[slots[0]]
    assert held is not None
    assert held.pid == 6262
    assert found[slots[1]] is None


def test_the_runtimes_own_holders_are_named_rather_than_guessed(slots):
    """A job and a lease are both locked *by the runtime*, and must say so.

    Reading /proc alone would attribute each of them to one pid running
    ``agentdeck``, which is true and tells the reader nothing.
    """
    first = try_take_slot(slots)
    second = try_take_slot(slots)
    assert first is not None and second is not None
    try:
        described = describe_heavy_slots(
            slots,
            jobs={os.fstat(first).st_ino: "abc123"},
            leases={os.fstat(second).st_ino: ("web test", 1_800_000_000.0)},
        )
        assert [entry["kind"] for entry in described] == ["job", "lease"]
        assert described[0]["job"] == "abc123"
        assert described[1]["label"] == "web test"
        assert described[1]["since"] == 1_800_000_000.0
    finally:
        os.close(first)
        os.close(second)


def test_anything_else_holding_a_slot_is_described_as_a_process(slots):
    """The case the panel could not explain: a suite nobody queued."""
    fd = try_take_slot(slots)
    assert fd is not None
    try:
        described = describe_heavy_slots(slots, jobs={}, leases={})
        assert described[0]["kind"] == "process"
        assert described[0]["pid"] == os.getpid()
        assert described[0]["command"]
        assert described[0]["cwd"] == os.getcwd()
        assert described[0]["since"] > 0
        assert described[1]["kind"] == "free"
    finally:
        os.close(fd)


def test_a_free_pool_describes_every_slot_as_free(slots):
    described = describe_heavy_slots(slots)
    assert [entry["kind"] for entry in described] == ["free", "free"]
    assert [entry["slot"] for entry in described] == [0, 1]
