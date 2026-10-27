import asyncio
import json
from datetime import UTC, datetime

import pytest

from agentdeck.models import Account, UsageSnapshot
from agentdeck.providers.codex.usage import CodexUsagePoller, UsageUnavailable, fetch_usage_once
from agentdeck.providers.codex.usage import parse_usage as parse_codex_usage


def _account(tmp_path) -> Account:
    return Account(key="codex:local", provider_id="codex", label="local", root=tmp_path)


def _window(percent, minutes, reset=1784543809):
    return {
        "usedPercent": percent,
        "windowDurationMins": minutes,
        "resetsAt": reset,
    }


def test_parse_usage_matches_windows_by_duration_not_position():
    snapshot = parse_codex_usage(
        "codex:local",
        {
            "rateLimits": {
                "primary": _window(17, 10080),
                "secondary": _window(42, 300),
            }
        },
    )
    assert snapshot.five_hour_pct == 42.0
    assert snapshot.seven_day_pct == 17.0
    assert snapshot.seven_day_resets_at == datetime.fromtimestamp(1784543809, tz=UTC)


def test_parse_usage_accepts_weekly_only_and_reports_the_fullest_bucket():
    snapshot = parse_codex_usage(
        "codex:local",
        {
            "rateLimits": {"primary": _window(99, 300)},
            "rateLimitsByLimitId": {
                "codex_spark": {"primary": _window(88, 10080)},
                "codex": {"primary": _window(2, 10080), "secondary": None},
            },
        },
    )
    assert snapshot.five_hour_pct is None
    assert snapshot.seven_day_pct == 88.0


def test_parse_usage_reads_every_bucket_not_just_the_codex_one():
    """The live shape (#790): several limit ids, only one of them named `codex`.
    Every bucket is parsed, and the fullest one is what the deck reports."""
    reset_soon, reset_late = 1786161876, 1786707053
    snapshot = parse_codex_usage(
        "codex:local",
        {
            "rateLimits": {"limitId": "codex", "primary": _window(100, 10080, reset_soon)},
            "rateLimitsByLimitId": {
                "codex": {"primary": _window(100, 10080, reset_soon)},
                "codex_bengalfox": {"primary": _window(0, 10080, reset_late)},
            },
        },
    )
    assert snapshot.seven_day_pct == 100.0
    assert snapshot.seven_day_resets_at == datetime.fromtimestamp(reset_soon, tz=UTC)


def test_parse_usage_reports_a_lightly_used_default_bucket_over_an_idle_one():
    """The live host: default bucket 3%, Spark untouched. Showing 0% hid real use."""
    snapshot = parse_codex_usage(
        "codex:local",
        {
            "rateLimitsByLimitId": {
                "codex": {"primary": _window(3, 10080)},
                "codex_bengalfox": {"primary": _window(0, 10080)},
            },
        },
    )
    assert snapshot.seven_day_pct == 3.0


def test_parse_usage_supports_legacy_windows_without_durations():
    snapshot = parse_codex_usage(
        "codex:local",
        {
            "rateLimits": {
                "primary": {"usedPercent": 3, "resetsAt": 1784543809},
                "secondary": {"usedPercent": 4, "resetsAt": 1784543809},
            }
        },
    )
    assert snapshot.five_hour_pct == 3.0
    assert snapshot.seven_day_pct == 4.0


def test_parse_usage_rejects_unrecognized_payload():
    with pytest.raises(UsageUnavailable):
        parse_codex_usage("codex:local", {"rateLimits": {"primary": None}})


class _FakeWriter:
    def __init__(self):
        self.messages = []
        self.closed = False

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, lines):
        self.stdin = _FakeWriter()
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(json.dumps(line).encode() + b"\n")
        self.stdout.feed_eof()
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_fetch_usage_once_speaks_app_server_protocol(tmp_path):
    process = _FakeProcess(
        [
            {"id": 1, "result": {"codexHome": str(tmp_path)}},
            {"method": "account/rateLimits/updated", "params": {}},
            {
                "id": 2,
                "result": {"rateLimits": {"primary": _window(12, 300)}},
            },
        ]
    )
    spawned = {}

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["env"] = kwargs["env"]
        return process

    snapshot = await fetch_usage_once(_account(tmp_path), process_factory=factory)

    assert snapshot.five_hour_pct == 12.0
    assert spawned["args"] == ("codex", "app-server", "--stdio")
    assert spawned["env"]["CODEX_HOME"] == str(tmp_path)
    assert [message.get("method") for message in process.stdin.messages] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert process.stdin.closed
    assert process.terminated


class _FakeState:
    def __init__(self):
        self.snapshots = []
        self.stale = []

    def set_usage(self, snapshot):
        self.snapshots.append(snapshot)

    def mark_usage_stale(self, account_key):
        self.stale.append(account_key)


async def test_poller_publishes_and_writes_shared_cache(tmp_path):
    account = _account(tmp_path)
    state = _FakeState()
    delays = []

    class Stop(Exception):
        pass

    async def fetch(_account):
        return UsageSnapshot(
            account_key=account.key,
            five_hour_pct=9,
            five_hour_resets_at=None,
            seven_day_pct=5,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )

    async def sleep(delay):
        delays.append(delay)
        if state.snapshots:
            raise Stop

    poller = CodexUsagePoller(
        account,
        state,
        interval_s=100,
        cache_dir=tmp_path / "cache",
        fetch=fetch,
        sleep=sleep,
        jitter=lambda: 0.9,
    )
    with pytest.raises(Stop):
        await poller.run()

    assert state.snapshots[-1].five_hour_pct == 9
    assert (tmp_path / "cache" / "usage-local.json").is_file()
    assert delays == [pytest.approx(0.0), pytest.approx(90.0)]


async def test_poller_marks_stale_and_backs_off(tmp_path):
    state = _FakeState()
    delays = []

    class Stop(Exception):
        pass

    async def unavailable(_account):
        raise UsageUnavailable("no usage")

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise Stop

    poller = CodexUsagePoller(
        _account(tmp_path),
        state,
        interval_s=100,
        cache_dir=tmp_path / "cache",
        fetch=unavailable,
        sleep=sleep,
        jitter=lambda: 0.9,
    )
    with pytest.raises(Stop):
        await poller.run()

    assert state.stale == ["codex:local"] * 3
    assert delays == [pytest.approx(0.0), 100, 200, 400]
