from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.providers import account_health


def test_hard_quota_circuit_survives_a_fresh_reader(tmp_path):
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    assert account_health.trip_hard_quota(
        tmp_path,
        "claude_code:main",
        reason="weekly quota",
        evidence="weekly limit",
        now=now,
    )

    circuit = account_health.open_circuit(
        tmp_path, "claude_code:main", now=now + timedelta(minutes=5)
    )

    assert circuit is not None
    assert circuit.reason == "weekly quota"


def test_known_reset_reopens_the_account_deterministically(tmp_path):
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    account_health.trip_hard_quota(
        tmp_path,
        "claude_code:main",
        reason="weekly quota",
        clears_at=reset,
        now=now,
    )

    assert (
        account_health.open_circuit(
            tmp_path, "claude_code:main", now=reset - timedelta(seconds=1)
        )
        is not None
    )
    assert account_health.open_circuit(tmp_path, "claude_code:main", now=reset) is None


def test_stale_structured_reset_does_not_immediately_reopen_hard_circuit(tmp_path):
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    assert account_health.trip_hard_quota(
        tmp_path,
        "claude_code:main",
        reason="weekly quota",
        clears_at=now - timedelta(minutes=1),
        now=now,
    )

    circuit = account_health.open_circuit(
        tmp_path, "claude_code:main", now=now + timedelta(minutes=1)
    )

    assert circuit is not None
    assert circuit.clears_at is None


def test_new_healthy_usage_probe_reopens_unknown_reset_circuit(tmp_path):
    now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
    account_health.trip_hard_quota(
        tmp_path, "claude_code:main", reason="weekly quota", now=now
    )

    assert not account_health.reconcile_healthy_usage(
        tmp_path,
        "claude_code:main",
        fetched_at=now - timedelta(seconds=1),
        percentages=(10.0, 20.0),
    )
    assert not account_health.reconcile_healthy_usage(
        tmp_path,
        "claude_code:main",
        fetched_at=now + timedelta(seconds=1),
        percentages=(10.0, None),
    )
    assert not account_health.reconcile_healthy_usage(
        tmp_path,
        "claude_code:main",
        fetched_at=now + timedelta(seconds=2),
        percentages=(10.0, 100.0),
    )
    assert account_health.reconcile_healthy_usage(
        tmp_path,
        "claude_code:main",
        fetched_at=now + timedelta(seconds=3),
        percentages=(10.0, 20.0),
    )
    assert account_health.open_circuit(tmp_path, "claude_code:main") is None


def test_structured_reset_parser_ignores_unstructured_prose():
    reset = account_health.structured_reset_at(
        {"error": {"reset_at": "2026-09-10T12:00:00Z"}}
    )
    assert reset == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    assert account_health.structured_reset_at({"message": "resets tomorrow at noon"}) is None


def test_exact_provider_quota_line_supplies_its_next_named_zone_reset():
    now = datetime(2026, 10, 12, 16, 9, tzinfo=UTC)  # 19:09 in Helsinki

    reset = account_health.stated_reset_at(
        "You've hit your session limit · resets 8:50pm (Europe/Helsinki)", now=now
    )

    assert reset == datetime(2026, 10, 12, 17, 50, tzinfo=UTC)
    assert account_health.stated_reset_at("resets tomorrow at noon", now=now) is None


@pytest.mark.parametrize(
    ("text", "now", "expected"),
    [
        (
            "resets 12am (Europe/Helsinki)",
            datetime(2026, 10, 12, 21, 0, tzinfo=UTC),
            datetime(2026, 10, 13, 21, 0, tzinfo=UTC),
        ),
        (
            "resets 12pm (Europe/Helsinki)",
            datetime(2026, 10, 12, 10, 0, tzinfo=UTC),
            datetime(2026, 10, 13, 9, 0, tzinfo=UTC),
        ),
    ],
)
def test_provider_quota_reset_handles_twelve_hour_boundaries(text, now, expected):
    assert account_health.stated_reset_at(text, now=now) == expected


@pytest.mark.parametrize(
    "text",
    [
        "resets 0am (Europe/Helsinki)",
        "resets 13pm (Europe/Helsinki)",
        "resets 8:60pm (Europe/Helsinki)",
        "resets 8:50pm (not/a-zone)",
        "resets 8:50pm",
    ],
)
def test_provider_quota_reset_rejects_malformed_boundaries(text):
    assert account_health.stated_reset_at(text) is None
