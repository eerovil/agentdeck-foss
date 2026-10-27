"""The loop that keeps the shared issue/pull cache warm (#1115)."""

from __future__ import annotations

import asyncio

import pytest

from agentdeck.issue_refresh import IssueRefreshService

# This file is the refresher's own test: let it actually refresh (see conftest).
pytestmark = pytest.mark.issue_refresh


class _Resolver:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.called = asyncio.Event()

    async def resolve(self, *, force: bool = False):
        self.calls += 1
        self.called.set()
        if self.fail:
            raise RuntimeError("GitHub is unreachable")
        return object()


@pytest.mark.asyncio
async def test_it_refreshes_before_its_first_sleep():
    """A web restart is when the operator is most likely to be looking at the
    front page, so an interval-first loop would show them the old cache."""
    resolver = _Resolver()
    service = IssueRefreshService(resolver, interval_s=3600.0)
    service.start()
    try:
        await asyncio.wait_for(resolver.called.wait(), timeout=2.0)
    finally:
        await service.stop()
    assert resolver.calls == 1


@pytest.mark.asyncio
async def test_a_failed_refresh_does_not_kill_the_loop():
    """GitHub being unreachable costs freshness, not the refresher."""
    resolver = _Resolver(fail=True)
    service = IssueRefreshService(resolver, interval_s=0.01)
    service.start()
    try:
        await asyncio.sleep(0.1)
        assert resolver.calls > 1
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_a_zero_interval_turns_it_off():
    """The operator switching it off is not a busy loop to guess a default for."""
    resolver = _Resolver()
    service = IssueRefreshService(resolver, interval_s=0)
    assert service.enabled is False
    service.start()
    await asyncio.sleep(0.05)
    await service.stop()
    assert resolver.calls == 0


@pytest.mark.asyncio
async def test_stopping_before_it_started_is_harmless():
    service = IssueRefreshService(_Resolver(), interval_s=60.0)
    await service.stop()
