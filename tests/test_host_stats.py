from pathlib import Path

import pytest

from agentdeck.host_stats import HostStatsSampler


def _write_proc(root: Path, *, stat: str, meminfo: str) -> None:
    (root / "stat").write_text(stat)
    (root / "meminfo").write_text(meminfo)


def test_host_stats_uses_cpu_delta_and_available_memory(tmp_path):
    _write_proc(
        tmp_path,
        stat="cpu  100 0 100 700 100 0 0 0\n",
        meminfo="MemTotal: 1000 kB\nMemAvailable: 400 kB\n",
    )
    sampler = HostStatsSampler(tmp_path)
    first = sampler.sample()

    assert first.cpu_pct is None
    assert first.memory_used_bytes == 600 * 1024
    assert first.memory_total_bytes == 1000 * 1024
    assert first.memory_pct == pytest.approx(60.0)

    _write_proc(
        tmp_path,
        stat="cpu  200 0 150 750 100 0 0 0\n",
        meminfo="MemTotal: 1000 kB\nMemAvailable: 250 kB\n",
    )
    second = sampler.sample()

    assert second.cpu_pct == pytest.approx(75.0)
    assert second.memory_pct == pytest.approx(75.0)


def test_host_stats_falls_back_when_mem_available_is_missing(tmp_path):
    _write_proc(
        tmp_path,
        stat="cpu  1 0 1 8\n",
        meminfo=(
            "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 100 kB\nCached: 200 kB\n"
        ),
    )

    sample = HostStatsSampler(tmp_path).sample()

    assert sample.memory_pct == pytest.approx(60.0)
