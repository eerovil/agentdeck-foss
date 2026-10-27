from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.config import AppConfig, HistoryConfig
from agentdeck.runtime import create_runtime_app
from agentdeck.session_secrets import (
    SecretCommandError,
    SessionSecretStore,
    parse_secret_command,
    secret_notice,
)


@pytest.mark.parametrize("name", ["LD_PRELOAD", "PYTHONPATH", "NODE_OPTIONS", "PATH"])
def test_secret_command_rejects_process_control_environment_names(name):
    with pytest.raises(SecretCommandError, match="controls program execution"):
        parse_secret_command(f"/secret {name} value-never-echoed")


def test_store_is_retry_stable_single_use_and_expires_without_exposing_values():
    now = 100.0
    store = SessionSecretStore(ttl_s=10.0, clock=lambda: now)

    first = store.register(
        session_key="codex:test:sid",
        action_id="action-123",
        name="SERVICE_TOKEN",
        value="value-never-in-repr",
    )
    retry = store.register(
        session_key="codex:test:sid",
        action_id="action-123",
        name="SERVICE_TOKEN",
        value="value-never-in-repr",
    )
    assert retry.ticket == first.ticket
    assert "value-never-in-repr" not in repr(first)

    claimed = store.claim(first.ticket)
    assert claimed is not None
    assert claimed.value == "value-never-in-repr"
    assert "value-never-in-repr" not in repr(claimed)
    assert store.claim(first.ticket) is None

    second = store.register(
        session_key="codex:test:sid",
        action_id="action-456",
        name="SERVICE_TOKEN",
        value="expires",
    )
    now = 111.0
    assert store.claim(second.ticket) is None


def test_store_rejects_changed_retry_without_echoing_either_value():
    store = SessionSecretStore()
    store.register(
        session_key="codex:test:sid",
        action_id="action-123",
        name="SERVICE_TOKEN",
        value="first-value",
    )

    with pytest.raises(ValueError) as raised:
        store.register(
            session_key="codex:test:sid",
            action_id="action-123",
            name="SERVICE_TOKEN",
            value="second-value",
        )
    assert "first-value" not in str(raised.value)
    assert "second-value" not in str(raised.value)


def test_store_caps_pending_values_and_can_discard_a_rejected_delivery():
    store = SessionSecretStore(max_pending=1)
    issued = store.register(
        session_key="codex:test:sid",
        action_id="action-123",
        name="SERVICE_TOKEN",
        value="first-value",
    )
    with pytest.raises(ValueError, match="too many temporary secrets"):
        store.register(
            session_key="codex:test:sid",
            action_id="action-456",
            name="SECOND_TOKEN",
            value="second-value",
        )

    assert store.discard(issued.ticket)
    assert store.claim(issued.ticket) is None
    assert store.register(
        session_key="codex:test:sid",
        action_id="action-456",
        name="SECOND_TOKEN",
        value="second-value",
    )


def test_provider_notice_contains_only_name_and_single_use_ticket():
    notice = secret_notice("SERVICE_TOKEN", "opaque-ticket")

    assert "SERVICE_TOKEN" in notice
    assert "opaque-ticket" in notice
    assert "one command" in notice
    # #1171: the store pops the ticket on claim, so the agent has to be told the
    # run is its only one rather than discovering it from a failed second run.
    assert "exactly one run" in notice
    assert "running it again fails" in notice


async def test_runtime_issues_and_atomically_claims_one_secret(tmp_path):
    app = create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "runtime-secrets.db")),
            accounts=[],
        )
    )
    value = "runtime-value"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://runtime") as client:
        issued = await client.post(
            "/session-secrets",
            json={
                "session_key": "codex:test:sid",
                "client_action_id": "action-123",
                "name": "SERVICE_TOKEN",
                "value": value,
            },
        )
        assert issued.status_code == 200
        assert value not in issued.text
        ticket = issued.json()["ticket"]

        claimed = await client.post("/session-secrets/claim", json={"ticket": ticket})
        replay = await client.post("/session-secrets/claim", json={"ticket": ticket})

    assert claimed.json() == {"name": "SERVICE_TOKEN", "value": value}
    assert replay.status_code == 404
    assert value not in replay.text
    app.state.budget_db.close()
