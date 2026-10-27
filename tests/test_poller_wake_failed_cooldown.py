import pytest

from agentdeck.poller_wake import PollerWakeService


async def test_failed_unit_start_does_not_consume_the_wake_cooldown(tmp_path):
    calls = 0

    def start_unit(_instance: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient systemd failure")

    service = PollerWakeService(
        tmp_path / "webhooks",
        tmp_path / "poller",
        start_unit=start_unit,
        clock=lambda: 10.0,
    )

    with pytest.raises(RuntimeError, match="transient systemd failure"):
        await service.wake("agentdeck")

    # The webhook batch is still uncommitted when the start raises, so its replay
    # must be allowed to retry immediately rather than being swallowed by the
    # cooldown reservation from the failed attempt.
    assert await service.wake("agentdeck") is True
    assert calls == 2
