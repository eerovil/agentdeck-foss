"""An agent's message to the chat running another Work (#3908)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentdeck.models import IssueRef
from agentdeck.web import deps, routes_works
from agentdeck.work import WorkChatFacts, build_work_projection


class Messages:
    """The scheduled-message store's two calls, as ``work_message`` makes them."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def exists(self, key):
        return key in self.rows

    def schedule(self, message):
        if message.id in self.rows:
            return None
        self.rows[message.id] = message
        return message


class State:
    def __init__(self) -> None:
        # chat-99 runs acme/games#99 and filed #102; chat-102 runs #102.
        self.projection = build_work_projection(
            [
                WorkChatFacts("chat-99", registered=(("leg-99", IssueRef("acme/games", 99)),)),
                WorkChatFacts("chat-102", registered=(("leg-102", IssueRef("acme/games", 102)),)),
            ]
        )
        self.sessions = {
            "leg-99": SimpleNamespace(provider_id="claude_code", last_activity=None),
            "chat-102": SimpleNamespace(provider_id="claude_code", last_activity=None),
        }

    def work_projection(self):
        return self.projection

    def chat_by_id(self, chat_id):
        return None

    def current_session_for_chat(self, chat):
        return None

    def chat_identity_key(self, leg):
        return {"leg-99": "chat-99", "leg-102": "chat-102"}.get(leg, leg)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(deps, "resolve_chat_key", lambda request, key: (key, None))
    app = FastAPI()
    app.include_router(routes_works.router)
    app.state.app_state = State()
    app.state.scheduled_messages = Messages()
    with TestClient(app) as test_client:
        yield test_client


def test_a_message_reaches_the_chat_running_the_other_work_once(client):
    text = "Ignore the ball game: make Hiihto 3D instead."
    url = "/api/works/acme/games/102/message"
    answer = client.post(url, json={"text": text, "from": "leg-99", "id": "hiihto-1"})
    assert answer.status_code == 202, answer.text
    assert answer.json()["chat"] == "chat-102"
    assert answer.json()["already_sent"] is False
    rows = client.app.state.scheduled_messages.rows
    [row] = rows.values()
    assert row.chat_id == "chat-102"
    assert text in row.text
    # The receiver knows who wrote it, that it is not the owner, and how to answer.
    assert "from the chat of acme/games#99, another agent, not the owner" in row.text
    assert "/api/works/acme/games/99/message" in row.text
    # Retried under its id (a curl that timed out): stored once, and said so.
    again = client.post(url, json={"text": text, "from": "leg-99", "id": "hiihto-1"})
    assert again.status_code == 202
    assert again.json()["already_sent"] is True
    assert len(rows) == 1
    # A, then B, then A again (#3909 review): each new instruction is sent, the same words
    # too; with no id every call is a new message. The path's case does not matter.
    other = client.post(
        "/api/works/ACME/Games/102/message", json={"text": "Keep 2D.", "from": "leg-99"}
    )
    assert other.status_code == 202
    assert other.json()["work_id"] == answer.json()["work_id"]
    back = client.post(url, json={"text": text, "from": "leg-99", "id": "hiihto-2"})
    assert back.json()["already_sent"] is False
    plain = client.post(url, json={"text": text, "from": "leg-99"})
    assert plain.json()["already_sent"] is False
    assert len(rows) == 4
    assert [row.text.count(text) for row in rows.values()] == [1, 0, 1, 1]


@pytest.mark.parametrize(
    ("path", "body", "status", "why"),
    [
        ("acme/games/102", {"text": "x", "from": "leg-102"}, 409, "this chat's own"),
        ("acme/games/103", {"text": "x", "from": "leg-99"}, 409, "no chat on this deck"),
        ("acme/games/102", {"text": "  ", "from": "leg-99"}, 422, "empty"),
        ("acme/games/102", {"text": "x" * 4001, "from": "leg-99"}, 422, "over 4000"),
        # Claude Code drops a prompt holding this word; the sender is told, not the receiver.
        ("acme/games/102", {"text": "Stuck? Try 3D.", "from": "leg-99"}, 422, "would not"),
        ("not-a-work", {"text": "x", "from": "leg-99"}, 404, "not a Work id"),
    ],
)
def test_a_message_that_cannot_reach_a_running_work_is_refused(client, path, body, status, why):
    answer = client.post(f"/api/works/{path}/message", json=body)
    assert answer.status_code == status
    assert why in answer.json()["detail"]
    assert client.app.state.scheduled_messages.rows == {}


async def test_a_message_is_delivered_to_the_other_chat_and_leaves_its_owner_question(tmp_path):
    """Through the real store and dispatcher: Work B's chat gets the text, and since another
    agent is not the owner, B's open question to the owner and its report stay (#3909 review).
    """
    import asyncio
    from datetime import UTC, datetime

    from agentdeck.db import Db
    from agentdeck.inject import InjectionStatus, QueuedMessage
    from agentdeck.models import Capability, InjectResult
    from agentdeck.scheduled_messages import ScheduledMessage, ScheduledMessageService
    from agentdeck.work import WorkRef
    from agentdeck.work_message import on_delivered, send

    class Injector:
        def __init__(self):
            self.calls = []

        async def start(self, _account, session, _provider, text, **kwargs):
            self.calls.append((session.key, text, kwargs["client_action_id"]))
            return InjectResult(True)

        async def wait_for_delivery(self, _session_key):
            return None

        def can_queue(self, _session_key):
            return False

        def status(self, _session_key):
            return InjectionStatus(
                "accepted",
                items=tuple(
                    QueuedMessage(n, text, client_action_id=action, state="accepted")
                    for n, (_key, text, action) in enumerate(self.calls, 1)
                ),
            )

    state = State()
    boss_turns = []
    state.note_boss_turn = lambda key, text: boss_turns.append(key)
    assistant = SimpleNamespace(handle_boss_turn=lambda key: boss_turns.append(f"retire {key}"))
    injector = Injector()
    session = SimpleNamespace(key="claude_code:b:102", capabilities=frozenset({Capability.INJECT}))
    service = ScheduledMessageService(
        Db(tmp_path / "agentdeck.db"),
        injector,
        lambda chat_id: (
            SimpleNamespace(account=object(), session=session, provider=object())
            if chat_id == "chat-102"
            else None
        ),
        on_delivered=on_delivered(state, assistant),
    )
    sent = send(state, service, WorkRef("acme/games", 102), "leg-99", "Make Hiihto 3D.")
    now = datetime.now(UTC)
    # And a person's message to the same chat, which is a boss turn.
    service.schedule(ScheduledMessage("owner-1", "chat-102", "Go on.", now, now))
    await service.dispatch_due(now)
    await asyncio.gather(*service._chat_tasks.values())

    delivered = {action: (key, text) for key, text, action in injector.calls}
    key, text = delivered[sent.message_id]
    assert key == "claude_code:b:102"
    assert "Make Hiihto 3D." in text and "another agent, not the owner" in text
    assert boss_turns == ["claude_code:b:102", "retire claude_code:b:102"], (
        "only the person's message retires the chat's question"
    )
