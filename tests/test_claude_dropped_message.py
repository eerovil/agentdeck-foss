"""Refusing a message Claude Code would swallow, and the /api catch-all 404.

Claude Code (reproduced on 2.1.220) drops a prompt containing the bare word
"Stuck?" from the request it builds, while still writing it to its own
transcript. Everything downstream then agrees a question was asked and the model
answers one it never saw. The deck cannot patch their CLI, so it refuses the send
and says why.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.models import Capability, InjectResult, Session, SessionStatus
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.codex.provider import CodexProvider

pytestmark = pytest.mark.anyio


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        accounts=[
            AccountConfig(
                provider="claude_code", label="test", config_dir=str(tmp_path / "claude")
            ),
            AccountConfig(
                provider="codex", label="test", config_dir=str(tmp_path / "codex")
            ),
        ],
    )
    app = create_app(config)
    for key, account_key, session_id in (
        ("claude_code:test:sid", "claude_code:test", "sid"),
        ("codex:test:sid", "codex:test", "sid"),
    ):
        app.state.app_state.update_session(
            Session(
                key,
                account_key,
                session_id,
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        )
    return app


def test_only_the_shape_that_actually_disappears_is_refused():
    """Narrow on purpose: each survivor below was sent to a real worker and arrived."""
    refuse = ClaudeCodeProvider.user_message_refusal
    assert refuse("Stuck? https://deck.example/sessions/abc") is not None
    assert refuse("X Stuck? repeat my message") is not None
    assert refuse("Stuck?") is not None
    for survivor in (
        "stuck? repeat my message",  # lower case arrives intact
        "Stuck repeat my message",  # no question mark
        "Stuck ? repeat my message",  # space before the question mark
        "aStuck? repeat my message",  # not its own word
        "Broken? repeat my message",  # nothing generic about a one-word question
        "Are you stuck?",
    ):
        assert refuse(survivor) is None, survivor


def test_a_provider_without_the_defect_delivers_everything():
    assert CodexProvider.user_message_refusal("Stuck? anything") is None


async def test_inject_refuses_the_swallowed_message_and_delivers_the_reworded_one(
    tmp_path,
):
    app = _app(tmp_path)
    sent = []

    async def start(account, session, provider, message, images=None, **kwargs):
        sent.append(message)
        return InjectResult(True)

    app.state.injector.start = start

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/sessions/claude_code:test:sid/inject",
            data={"message": "Stuck? have a look"},
            headers={"origin": "http://test"},
        )
        reworded = await client.post(
            "/sessions/claude_code:test:sid/inject",
            data={"message": "stuck? have a look"},
            headers={"origin": "http://test"},
        )
        # The defect is Claude Code's, so another provider must not inherit it.
        elsewhere = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "Stuck? have a look"},
            headers={"origin": "http://test"},
        )

    assert refused.status_code == 422
    assert "Stuck?" in refused.json()["detail"]
    assert reworded.status_code == 202
    assert elsewhere.status_code == 202
    # The refused one never reached the queue: no half-sent message to reconcile.
    assert sent == ["stuck? have a look", "Stuck? have a look"]


async def test_new_chat_refusal_says_why_instead_of_starting_a_deaf_session(tmp_path):
    app = _app(tmp_path)
    starts = []

    async def start_new(account, provider, cwd, message, images=None, **kwargs):
        starts.append(message)
        return InjectResult(True)

    app.state.injector.start_new = start_new

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            "/sessions/new",
            data={
                "account_key": "claude_code:test",
                "cwd": str(tmp_path),
                "message": "Stuck? https://deck.example/sessions/abc",
            },
            headers={"origin": "http://test"},
        )

    assert refused.status_code == 422
    # A bare 422 body leaves the person with a number; they need the reason.
    assert "Stuck?" in refused.text
    assert starts == []


async def test_an_unmatched_api_path_is_not_reported_as_a_missing_file(tmp_path):
    """The file catch-all claims every unmatched path, including imagined API routes."""
    app = _app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        missing_route = await client.get("/api/sessions/claude_code:test:sid")
        missing_file = await client.get("/no/such/file/anywhere")

    assert missing_route.status_code == 404
    assert missing_route.json()["detail"] == "no such API route"
    assert missing_file.status_code == 404
    assert missing_file.json()["detail"] == "local file not found"
