import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from agentdeck.models import Account, UsageSnapshot
from agentdeck.providers import usage as shared_usage
from agentdeck.providers.claude_code import usage
from agentdeck.providers.claude_code.usage import (
    USAGE_URL,
    ClaudeUsagePoller,
    UsageAuthError,
    UsageRateLimited,
    UsageUnavailable,
    fetch_usage_once,
)

OK_BODY = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-07-03T13:00:00Z"},
    "seven_day": {"utilization": 5.0, "resets_at": "2026-07-06T20:59:59Z"},
}


def _account(cfg_path) -> Account:
    return Account(key="claude_code:main", provider_id="claude_code", label="main", root=cfg_path)


@pytest.fixture
def account(tmp_path, write_credentials):
    write_credentials(tmp_path, "tok-1")
    return _account(tmp_path)


async def test_fetch_usage_once_happy(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        async with httpx.AsyncClient() as client:
            snap = await fetch_usage_once(account, client)
    assert snap.five_hour_pct == 12.0
    assert snap.seven_day_pct == 5.0
    assert snap.five_hour_resets_at is not None
    assert snap.seven_day_resets_at.year == 2026


async def test_fetch_usage_once_401(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageAuthError):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_429(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(429))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageRateLimited):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_5xx_is_rate_limited(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageRateLimited):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_malformed(account):
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, text="not json"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageUnavailable):
                await fetch_usage_once(account, client)


async def test_fetch_usage_once_no_token(tmp_path):
    acc = _account(tmp_path)  # no .credentials.json written
    with respx.mock:
        route = respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        async with httpx.AsyncClient() as client:
            with pytest.raises(UsageUnavailable):
                await fetch_usage_once(acc, client)
    assert not route.called  # short-circuits before any HTTP


async def test_token_read_fresh_each_call(tmp_path, write_credentials):
    """The bearer must be re-read from disk every request (tokens rotate)."""
    write_credentials(tmp_path, "tok-A")
    acc = _account(tmp_path)
    seen = []
    with respx.mock:

        def _capture(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json=OK_BODY)

        respx.get(USAGE_URL).mock(side_effect=_capture)
        async with httpx.AsyncClient() as client:
            await fetch_usage_once(acc, client)
            write_credentials(tmp_path, "tok-B")  # rotate on disk
            await fetch_usage_once(acc, client)
    assert seen == ["Bearer tok-A", "Bearer tok-B"]


async def test_poller_backoff_schedule(account):
    """On persistent 429 the delay doubles from the base interval."""
    delays: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) > 4:
            raise Stop

    poller = ClaudeUsagePoller(
        account,
        _FakeState(),
        interval_s=100.0,
        sleep=fake_sleep,
        jitter=lambda: 0.9,  # phase offset → 0
    )
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(Stop):
            await poller.run()
    # delays[0] = phase offset (0); then exponential backoff
    assert delays[0] == pytest.approx(0.0)
    assert delays[1] == pytest.approx(100.0)
    assert delays[2] == pytest.approx(200.0)
    assert delays[3] == pytest.approx(400.0)


async def test_poller_publishes_on_success(account):
    state = _FakeState()

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        if state.snapshots:
            raise Stop

    poller = ClaudeUsagePoller(
        account, state, interval_s=100.0, sleep=fake_sleep, jitter=lambda: 0.9
    )
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        with pytest.raises(Stop):
            await poller.run()
    assert state.snapshots[-1].five_hour_pct == 12.0


class _FakeState:
    def __init__(self):
        self.snapshots = []
        self.stale = []
        self.rejected = []

    def set_usage(self, snap):
        self.snapshots.append(snap)

    def mark_usage_stale(self, key):
        self.stale.append(key)

    def mark_usage_credential_rejected(self, key, reason):
        self.rejected.append((key, reason))


async def test_a_refused_credential_is_reported_as_refused_not_as_stale(account):
    """#758. Only an auth failure withdraws the account's percentage; the loop has
    to tell the two apart without knowing which provider raised it. The Claude
    poller retries a 401 once first (the token may have rotated), so a persistent
    401 reports one rejection per cycle, not one per request."""
    state = _FakeState()
    delays: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) == 3:
            raise Stop

    poller = ClaudeUsagePoller(
        account,
        state,
        interval_s=100.0,
        cache_dir=account.root / "cache",
        sleep=fake_sleep,
        jitter=lambda: 0.9,
    )
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(Stop):
            await poller.run()

    assert state.stale == []
    assert [key for key, _ in state.rejected] == ["claude_code:main"] * 2
    assert "401" in state.rejected[0][1]


async def test_a_refused_credential_unpublishes_the_snapshot_a_restart_would_warm(
    account, tmp_path
):
    """#758. ``warm_usage_state`` applies no max age by design, so leaving the
    published file behind would put the withdrawn percentage back on the header at
    the next web restart — the exact number the poller just refused to stand behind."""
    cache = tmp_path / "cache"
    state = _FakeState()
    poller = ClaudeUsagePoller(account, state, interval_s=100.0, cache_dir=cache)
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(200, json=OK_BODY))
        async with httpx.AsyncClient() as client:
            await poller._publish_once(client)
    assert shared_usage.read_shared_cache(account, cache) is not None

    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient() as client:
            await poller._on_error(UsageAuthError("401"), client)

    assert shared_usage.read_shared_cache(account, cache) is None
    assert not list(cache.glob("usage-*.json"))


async def test_clearing_one_account_leaves_another_providers_alias_alone(account, tmp_path):
    """The label-only alias is a shared filename: two providers can hold one label,
    and the surviving account's compatibility read must not be collateral damage."""
    cache = tmp_path / "cache"
    twin = Account(key="codex:main", provider_id="codex", label="main", root=account.root)
    snapshot = _snapshot(account.key)
    shared_usage.write_shared_cache(_snapshot(twin.key), twin, cache)
    shared_usage.write_shared_cache(snapshot, account, cache)  # claims the alias

    shared_usage.clear_shared_cache(twin, cache)

    assert shared_usage.read_shared_cache(twin, cache) is None
    assert shared_usage.read_shared_cache(account, cache) is not None


def _snapshot(account_key: str) -> UsageSnapshot:
    return UsageSnapshot(
        account_key=account_key,
        five_hour_pct=12.0,
        five_hour_resets_at=None,
        seven_day_pct=5.0,
        seven_day_resets_at=None,
        fetched_at=datetime.now(UTC),
    )


async def test_a_transport_failure_still_only_ages_the_last_read(account):
    """The complement: a 5xx says nothing about the credential, so the previous
    number stays on screen and is merely marked stale."""
    state = _FakeState()
    delays: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) == 2:
            raise Stop

    poller = ClaudeUsagePoller(
        account,
        state,
        interval_s=100.0,
        cache_dir=account.root / "cache",
        sleep=fake_sleep,
        jitter=lambda: 0.9,
    )
    with respx.mock:
        respx.get(USAGE_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(Stop):
            await poller.run()

    assert state.rejected == []
    assert state.stale == ["claude_code:main"]


async def test_poller_survives_unexpected_exception(account):
    """A non-HTTP bug in the read must not crash the long-lived loop: the base
    catches it, marks the account stale, and backs off like any other failure."""
    state = _FakeState()
    delays: list[float] = []

    class Stop(Exception):
        pass

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        if len(delays) == 3:
            raise Stop

    class _Boom(ClaudeUsagePoller):
        async def _fetch(self, client):
            raise ValueError("unexpected")

    poller = _Boom(
        account,
        state,
        interval_s=100.0,
        cache_dir=account.root / "cache",
        sleep=fake_sleep,
        jitter=lambda: 0.9,
    )
    with pytest.raises(Stop):
        await poller.run()
    assert state.stale == ["claude_code:main"] * 2
    assert delays == [pytest.approx(0.0), pytest.approx(100.0), pytest.approx(200.0)]


async def test_write_shared_cache(account, tmp_path):
    snap = usage.parse_usage(account.key, OK_BODY)
    cache = tmp_path / "cache"
    shared_usage.write_shared_cache(snap, account, cache)
    written = (cache / "usage-main.json").read_text()
    assert '"five_hour_pct": 12.0' in written
    assert (cache / "usage-claude_code-main.json").read_text() == written


def test_read_shared_cache_round_trips_write(account, tmp_path):
    snap = usage.parse_usage(account.key, OK_BODY)
    cache = tmp_path / "cache"
    shared_usage.write_shared_cache(snap, account, cache)
    loaded = shared_usage.read_shared_cache(account, cache)
    assert loaded is not None
    assert loaded.account_key == account.key
    assert loaded.five_hour_pct == 12.0
    assert loaded.seven_day_pct == 5.0
    assert loaded.five_hour_resets_at == snap.five_hour_resets_at
    assert loaded.fetched_at == snap.fetched_at


def test_read_shared_cache_does_not_cross_providers_with_the_same_label(account, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "usage-main.json").write_text(
        json.dumps(
            {
                "account": "codex:main",
                "fetched_at": datetime.now(UTC).isoformat(),
                "five_hour_pct": 99.0,
                "seven_day_pct": 99.0,
            }
        )
    )

    assert shared_usage.read_shared_cache(account, cache) is None


def test_read_shared_cache_missing_returns_none(account, tmp_path):
    assert shared_usage.read_shared_cache(account, tmp_path / "empty") is None


class _WarmDb:
    def __init__(self, snap=None):
        self._snap = snap

    def latest_usage(self, account_key):
        return self._snap


class _WarmState:
    def __init__(self, db=None):
        self.db = db
        self.usage = {}

    def warm_usage(self, snapshot):
        self.usage.setdefault(snapshot.account_key, snapshot)


def test_warm_usage_state_prefers_cache_over_db(account, tmp_path):
    cache = tmp_path / "cache"
    shared_usage.write_shared_cache(usage.parse_usage(account.key, OK_BODY), account, cache)
    db_snap = usage.parse_usage(account.key, {"five_hour": {"utilization": 99.0}})
    state = _WarmState(db=_WarmDb(db_snap))
    shared_usage.warm_usage_state(state, [account], cache)
    # Cache wins; the DB fallback (99%) is not consulted.
    assert state.usage[account.key].five_hour_pct == 12.0


def test_warm_usage_state_falls_back_to_db(account, tmp_path):
    db_snap = usage.parse_usage(account.key, {"seven_day": {"utilization": 42.0}})
    state = _WarmState(db=_WarmDb(db_snap))
    shared_usage.warm_usage_state(state, [account], tmp_path / "empty")
    assert state.usage[account.key].seven_day_pct == 42.0


def test_warm_usage_state_no_source_leaves_gap(account, tmp_path):
    state = _WarmState(db=_WarmDb(None))
    shared_usage.warm_usage_state(state, [account], tmp_path / "empty")
    assert account.key not in state.usage
