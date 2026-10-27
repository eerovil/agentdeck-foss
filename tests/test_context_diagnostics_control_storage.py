from types import SimpleNamespace

from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db
from agentdeck.web import routes_api


def _request(control, state):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db=control,
                control_db=control,
                app_state=state,
            )
        )
    )


async def test_context_delivery_diagnostics_read_the_store(
    tmp_path, monkeypatch
):
    control = Db(tmp_path / "control.db")
    session_key = "codex:test:source"
    chat_id = "chat-1"
    row = ContextDelivery(
        action_id="action-1",
        attempt=1,
        provider_id="codex",
        account_key="codex:test",
        worker_key="card-1",
        delivery_id="delivery-1",
        request_sha256="a" * 64,
        chat_id=chat_id,
        source_session_key=session_key,
        source_session_id="source",
        mode="enforce",
        proposed_action="none",
        executed_action="none",
        reason="below threshold",
    )
    try:
        assert control.reserve_context_delivery(row) is not None
        state = SimpleNamespace(chat_identity_key=lambda _key: chat_id)
        request = _request(control, state)
        session = SimpleNamespace(key=session_key)
        monkeypatch.setattr(
            routes_api,
            "resolve_current_leg",
            lambda _request, _session_key: (None, session, None),
        )

        payload = await routes_api.list_context_deliveries(request, session_key)

        assert payload["chat_id"] == chat_id
        assert len(payload["context_deliveries"]) == 1
        assert payload["context_deliveries"][0]["delivery_id"] == "delivery-1"
    finally:
        control.close()
