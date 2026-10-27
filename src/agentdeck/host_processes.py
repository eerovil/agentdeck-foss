"""Which processes are using this host's CPU right now, and who owns them (#2663).

Read from procfs on demand only — nothing samples in the background. CPU is the
difference between two reads ``window_s`` apart, because ``ps``'s lifetime
average hides what is busy now and shows a one-shot command at 500%. Each
process is given an owner from its systemd unit, or from the deck chat whose
worker PID it is or descends from.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

TOP_PROCESSES = 25

_PODMAN_SCOPE = re.compile(r"^libpod-([0-9a-f]{12,})\.scope$")
_POLLER_UNIT = re.compile(r"^agentdeck-poller@(.+)\.service$")
_REMOTE_CONTROL_UNIT = re.compile(r"^claude-rc-(.+)\.service$")
# ``-<runtime token>-<n>`` since #3079; scopes a runtime named before that end in ``-<n>``.
_WORKER_SCOPE = re.compile(r"^agentdeck-worker-(.+?)(?:-r[0-9a-f]{8})?-\d+\.scope$")
_APP_SCOPE = re.compile(r"^app-(?:[^-]+-)*?([^-]+)-\d+\.scope$")
_FIXED_UNITS = {
    "agentdeck.service": "Deck web",
    "agentdeck-codex.service": "Deck runtime",
}


@dataclass(frozen=True)
class ChatOwner:
    """A deck chat a process belongs to: its key, the title to show, and the start-time
    token the deck recorded for its PID (``None`` when it recorded none)."""

    key: str
    title: str
    start: str | None = None


@dataclass(frozen=True)
class _Read:
    ticks: int
    start: str
    ppid: int
    rss_bytes: int
    command: str
    unit: str


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    owner: str
    owner_href: str | None
    cpu_pct: float  # percent of one core over the window
    rss_bytes: int
    age_s: float | None
    command: str


@dataclass(frozen=True)
class OwnerRow:
    owner: str
    owner_href: str | None
    cpu_pct: float
    rss_bytes: int
    processes: int


@dataclass(frozen=True)
class ProcessSnapshot:
    window_s: float
    cores: int
    total_cpu_pct: float  # percent of one core, summed over every process
    owners: tuple[OwnerRow, ...]
    top: tuple[ProcessRow, ...]
    measured_at: float  # epoch seconds
    skipped: int  # processes that could not be read


def _unit(cgroup_text: str) -> str:
    """The innermost named cgroup: ``agentdeck.service``, ``libpod-<id>.scope``, …"""
    for line in cgroup_text.splitlines():
        if line.startswith("0::"):
            parts = [part for part in line[3:].split("/") if part]
            # A container's processes sit one level below its scope (``…/container``).
            for part in reversed(parts):
                if part.endswith((".service", ".scope")):
                    return part
            return parts[-1] if parts else ""
    return ""


def _read_one(proc_root: Path, pid: int) -> _Read | None:
    base = proc_root / str(pid)
    try:
        stat = (base / "stat").read_text()
        statm = (base / "statm").read_text().split()
        cmdline = (base / "cmdline").read_bytes()
        cgroup = (base / "cgroup").read_text()
    except (OSError, ValueError):
        return None
    name_end = stat.rfind(")")
    fields = stat[name_end + 2 :].split()
    if name_end < 0 or len(fields) < 20:
        return None
    try:
        ticks = int(fields[11]) + int(fields[12])
        ppid = int(fields[1])
        start = fields[19]
        rss_bytes = int(statm[1]) * os.sysconf("SC_PAGE_SIZE") if len(statm) > 1 else 0
    except ValueError:
        return None
    command = cmdline.replace(b"\0", b" ").decode(errors="replace").strip()
    if not command:
        command = f"[{stat[stat.find('(') + 1 : name_end]}]"
    return _Read(ticks, start, ppid, rss_bytes, command, _unit(cgroup))


def _read_all(proc_root: Path) -> tuple[dict[int, _Read], int]:
    reads: dict[int, _Read] = {}
    skipped = 0
    try:
        names = os.listdir(proc_root)
    except OSError:
        return reads, 0
    for name in names:
        if not name.isdigit():
            continue
        read = _read_one(proc_root, int(name))
        if read is None:
            skipped += 1
        else:
            reads[int(name)] = read
    return reads, skipped


def unit_owner(unit: str) -> str:
    """A plain name for a systemd unit, or the unit itself when it is not one we know."""
    if not unit:
        return "System"
    if unit in _FIXED_UNITS:
        return _FIXED_UNITS[unit]
    if match := _POLLER_UNIT.match(unit):
        return f"Poller ({match.group(1)})"
    if unit.startswith("agentdeck-nudge-"):
        return "ChatGPT nudge"
    if match := _WORKER_SCOPE.match(unit):
        return f"Agent worker ({match.group(1)})"
    if match := _PODMAN_SCOPE.match(unit):
        return f"Container {match.group(1)[:8]}"
    if match := _REMOTE_CONTROL_UNIT.match(unit):
        return f"Claude remote control ({match.group(1)})"
    if match := _APP_SCOPE.match(unit):
        return f"App ({match.group(1)})"
    return unit


def _chat_for(
    pid: int, reads: Mapping[int, _Read], chats: Mapping[int, ChatOwner]
) -> ChatOwner | None:
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        read = reads.get(pid)
        if read is None:
            return None
        chat = chats.get(pid)
        # A recorded start time that differs means the chat's process is gone and the
        # PID was reused by something else.
        if chat is not None and (chat.start is None or chat.start == read.start):
            return chat
        seen.add(pid)
        pid = read.ppid
    return None


def build_snapshot(
    first: Mapping[int, _Read],
    second: Mapping[int, _Read],
    *,
    window_s: float,
    clock_ticks: int,
    cores: int,
    chats: Mapping[int, ChatOwner],
    skipped: int = 0,
    measured_at: float = 0.0,
    uptime_s: float | None = None,
) -> ProcessSnapshot:
    rows: list[ProcessRow] = []
    for pid, now in second.items():
        before = first.get(pid)
        # A process that started inside the window, or a reused PID, used what it has.
        used = now.ticks - before.ticks if before and before.start == now.start else now.ticks
        cpu_pct = max(0.0, used / clock_ticks / window_s * 100.0) if window_s > 0 else 0.0
        chat = _chat_for(pid, second, chats)
        if chat is not None:
            owner, href = f"Chat: {chat.title}", f"/sessions/{chat.key}"
        else:
            owner, href = unit_owner(now.unit), None
        age_s = None
        if uptime_s is not None:
            try:
                age_s = max(0.0, uptime_s - int(now.start) / clock_ticks)
            except ValueError:
                age_s = None
        rows.append(ProcessRow(pid, owner, href, cpu_pct, now.rss_bytes, age_s, now.command))

    # Keyed with the link too: two chats may share a title.
    groups: dict[tuple[str, str | None], list[ProcessRow]] = {}
    for row in rows:
        groups.setdefault((row.owner, row.owner_href), []).append(row)
    owners = []
    for (owner, _href), members in groups.items():
        cpu = sum(member.cpu_pct for member in members)
        if owner.startswith("Container "):
            busiest = max(members, key=lambda member: (member.cpu_pct, member.rss_bytes))
            owner = f"{owner} ({busiest.command[:60]})"
        owners.append(
            OwnerRow(
                owner=owner,
                owner_href=members[0].owner_href,
                cpu_pct=cpu,
                rss_bytes=sum(member.rss_bytes for member in members),
                processes=len(members),
            )
        )
    owners.sort(key=lambda row: (-row.cpu_pct, -row.rss_bytes, row.owner))
    rows.sort(key=lambda row: (-row.cpu_pct, -row.rss_bytes, row.pid))
    return ProcessSnapshot(
        window_s=window_s,
        cores=cores,
        total_cpu_pct=sum(row.cpu_pct for row in rows),
        owners=tuple(owners),
        top=tuple(rows[:TOP_PROCESSES]),
        measured_at=measured_at,
        skipped=skipped,
    )


def _uptime(proc_root: Path) -> float | None:
    try:
        return float((proc_root / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def sample_processes(
    chats: Mapping[int, ChatOwner],
    *,
    proc_root: Path = Path("/proc"),
    window_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ProcessSnapshot:
    """Read every process twice, ``window_s`` apart. Blocking: call it from a thread."""
    first, _ = _read_all(proc_root)
    sleep(window_s)
    second, skipped = _read_all(proc_root)
    return build_snapshot(
        first,
        second,
        window_s=window_s,
        clock_ticks=os.sysconf("SC_CLK_TCK"),
        cores=os.cpu_count() or 1,
        chats=chats,
        skipped=skipped,
        measured_at=time.time(),
        uptime_s=_uptime(proc_root),
    )
