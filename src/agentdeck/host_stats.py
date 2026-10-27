"""Lightweight Linux host CPU and memory sampling from procfs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class HostStats:
    cpu_pct: float | None
    memory_pct: float
    memory_used_bytes: int
    memory_total_bytes: int
    sampled_at: datetime


class HostStatsSampler:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self._previous_cpu: tuple[int, int] | None = None

    def _cpu_counters(self) -> tuple[int, int]:
        line = (self.proc_root / "stat").read_text().splitlines()[0]
        fields = line.split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            raise ValueError("invalid /proc/stat cpu row")
        values = [int(value) for value in fields[1:]]
        # guest/guest_nice are already included in user/nice, so only the first
        # eight Linux CPU fields belong in the aggregate total.
        total = sum(values[:8])
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    def _memory(self) -> tuple[int, int]:
        values = {}
        for line in (self.proc_root / "meminfo").read_text().splitlines():
            key, separator, raw = line.partition(":")
            if separator:
                values[key] = int(raw.split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable")
        if available is None:
            available = sum(values.get(key, 0) for key in ("MemFree", "Buffers", "Cached"))
        used = min(total, max(0, total - available))
        return used, total

    def sample(self) -> HostStats:
        total, idle = self._cpu_counters()
        cpu_pct = None
        if self._previous_cpu is not None:
            previous_total, previous_idle = self._previous_cpu
            total_delta = total - previous_total
            idle_delta = idle - previous_idle
            if total_delta > 0:
                cpu_pct = min(100.0, max(0.0, 100.0 * (total_delta - idle_delta) / total_delta))
        self._previous_cpu = (total, idle)

        memory_used, memory_total = self._memory()
        memory_pct = 100.0 * memory_used / memory_total if memory_total else 0.0
        return HostStats(
            cpu_pct=cpu_pct,
            memory_pct=memory_pct,
            memory_used_bytes=memory_used,
            memory_total_bytes=memory_total,
            sampled_at=datetime.now(UTC),
        )
