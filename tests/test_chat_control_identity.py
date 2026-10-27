"""A published URL outlives the leg it named, so writes follow the chat (#1266).

Chat identity split reading from writing: a chain of provider sessions is one
conversation to read, but only one of its legs is a live process. Everything
here is the write half — a reply, an answer, a report, a nudge or a budget sent
to yesterday's account key has to reach the session the chat is on now, and a
chat must not gain a fresh lifetime allowance by moving account.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    HistoryConfig,
    InjectConfig,
    RestartNudgeConfig,
)
from agentdeck.db import Db
from agentdeck.models import (
    Capability,
    InjectResult,
    InteractionQuestion,
    PendingInteraction,
    Session,
    SessionStatus,
    TranscriptEvent,
)
from agentdeck.poller_engine.github import public_session_url
from agentdeck.providers import PROVIDERS
from agentdeck.restart_nudge import RestartNudgeRegistry
from agentdeck.state import AppState

pytestmark = pytest.mark.anyio

OLD_KEY = "claude_code:main:leg-one"
CURRENT_KEY = "claude_code:alt:leg-two"


def _chat_app(tmp_path, *, capabilities=None, cwd=None):
    """A two-leg chat: ``OLD_KEY`` is the published alias, ``CURRENT_KEY`` is live."""
    caps = capabilities or frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        # Never the shared default: registering there would nudge live chats.
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json")),
        accounts=[
            AccountConfig(
                provider="claude_code", label=label, config_dir=str(tmp_path / label)
            )
            for label in ("main", "alt")
        ],
    )
    app = create_app(config)
    state = app.state.app_state
    for key, label, session_id in (
        (OLD_KEY, "main", "leg-one"),
        (CURRENT_KEY, "alt", "leg-two"),
    ):
        state.update_session(
            Session(
                key=key,
                account_key=f"claude_code:{label}",
                session_id=session_id,
                status=SessionStatus.IDLE,
                title="Chained chat",
                cwd=cwd or tmp_path,
                capabilities=caps,
            )
        )
    app.state.budget_db.ensure_session_chats((OLD_KEY,))
    chat = app.state.budget_db.append_chat_leg(OLD_KEY, CURRENT_KEY)
    return app, chat


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- the composer ---------------------------------------------------------


async def test_a_reply_through_an_old_leg_url_is_delivered_to_the_current_leg(tmp_path):
    """The headline case: an old bookmark must not write into a dead session."""
    app, _chat = _chat_app(tmp_path)
    delivered = []

    async def start(account, session, provider, message, images=None, **kwargs):
        delivered.append((account.key, session.key, message))
        return InjectResult(True)

    app.state.injector.start = start

    async with _client(app) as client:
        accepted = await client.post(
            f"/sessions/{OLD_KEY}/inject",
            data={"message": "still there?"},
            headers={"origin": "http://test"},
        )

    assert accepted.status_code == 202
    assert delivered == [("claude_code:alt", CURRENT_KEY, "still there?")]


async def test_the_queue_status_an_old_leg_url_reports_is_the_current_legs(tmp_path):
    """The composer's own status fragment has to follow the send it describes."""
    app, _chat = _chat_app(tmp_path)
    asked = []
    real_status = app.state.injector.status
    app.state.injector.status = lambda key: (asked.append(key), real_status(key))[1]

    async with _client(app) as client:
        fragment = await client.get(f"/partials/sessions/{OLD_KEY}/inject-status")

    assert fragment.status_code == 200
    assert asked == [CURRENT_KEY]


# --- pending interactions -------------------------------------------------


def _interaction(thread_id: str) -> PendingInteraction:
    return PendingInteraction(
        id=f"int-{thread_id}",
        kind="question",
        thread_id=thread_id,
        turn_id=None,
        title="Which branch?",
        questions=(InteractionQuestion(id="q1", header="Branch", prompt="Which?"),),
    )


async def test_the_chats_interaction_is_the_current_legs_and_answering_reaches_it(
    tmp_path, monkeypatch
):
    app, _chat = _chat_app(tmp_path)
    provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(
        provider,
        "pending_interaction",
        lambda account, session: (
            _interaction(session.session_id) if session.key == CURRENT_KEY else None
        ),
    )
    answered = []

    async def answer(account, session, interaction_id, **kwargs):
        answered.append((session.key, interaction_id))
        return InjectResult(True)

    monkeypatch.setattr(provider, "answer_interaction", answer)

    async with _client(app) as client:
        # Asked through the alias, the chat reports the interaction that is
        # actually blocking it, and hands back the URL that can answer it.
        payload = (await client.get(f"/api/sessions/{OLD_KEY}/interaction")).json()
        posted = await client.post(
            f"/sessions/{OLD_KEY}/interaction",
            data={"interaction_id": "int-leg-two", "answer__q1": "main"},
            headers={"origin": "http://test"},
        )

    assert payload["interaction"]["id"] == "int-leg-two"
    assert payload["answer_url"] == f"/sessions/{CURRENT_KEY}/interaction"
    assert posted.status_code == 200
    assert answered == [(CURRENT_KEY, "int-leg-two")]


# --- session-addressed API surfaces --------------------------------------


async def test_an_attention_report_through_an_old_alias_lands_on_the_current_leg(
    tmp_path,
):
    app, _chat = _chat_app(tmp_path)
    state = app.state.app_state

    async with _client(app) as client:
        put = await client.put(
            f"/api/sessions/{OLD_KEY}/attention", json={"state": "waiting"}
        )
        read_through_alias = await client.get(f"/api/sessions/{OLD_KEY}/attention")

    assert put.json()["session_key"] == CURRENT_KEY
    assert read_through_alias.json()["state"] == "waiting"
    assert set(state.attention_reports) == {CURRENT_KEY}


async def test_a_restart_nudge_through_an_old_alias_names_the_current_leg(tmp_path):
    app, _chat = _chat_app(tmp_path)

    async with _client(app) as client:
        registered = await client.put(f"/api/sessions/{OLD_KEY}/restart-nudge")

    assert registered.json()["session_key"] == CURRENT_KEY
    assert registered.json()["nudge_on_restart"] is True
    settings = app.state.config.restart_nudge
    registry = RestartNudgeRegistry(settings.path, ttl_s=settings.ttl_s)
    assert [entry.session_key for entry in registry.entries()] == [CURRENT_KEY]


async def test_a_dead_old_leg_still_addresses_the_chat_it_belongs_to(tmp_path):
    """The alias has to work after the leg it names has left the deck."""
    app, _chat = _chat_app(tmp_path)
    app.state.app_state.sessions.pop(OLD_KEY)

    async with _client(app) as client:
        title = await client.put(
            f"/api/sessions/{OLD_KEY}/title", json={"title": "Renamed"}
        )

    assert title.status_code == 200
    assert title.json()["session_key"] == CURRENT_KEY


# --- the canonical chat URL ----------------------------------------------


async def test_the_chat_url_serves_the_transcript_a_continuation_brief_reads(
    tmp_path, monkeypatch
):
    """``<session_url>/transcript.json`` must work for the URL workers publish."""
    app, chat = _chat_app(tmp_path)

    async def read_transcript(account, session, after_seq=0):
        assert after_seq == 0
        return [
            TranscriptEvent(
                seq=1,
                role="user" if session.key == OLD_KEY else "assistant",
                text=f"Message from {account.key}",
                image_media_types=("image/png",) if session.key == OLD_KEY else (),
            )
        ]

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "read_transcript", read_transcript
    )

    async with _client(app) as client:
        through_chat = await client.get(f"/chats/{chat.chat_id}/transcript.json")

    assert through_chat.status_code == 200
    payload = through_chat.json()
    assert payload["session_key"] == CURRENT_KEY
    assert [
        (event["text"], event["seq"], event["chat_event_id"])
        for event in payload["events"]
    ] == [
        ("Message from claude_code:main", 1, "1"),
        ("Message from claude_code:alt", 1, "1.1"),
    ]
    old_event, current_event = payload["events"]
    assert old_event["image_urls"] == [
        f"http://test/sessions/{OLD_KEY}/transcript-images/1/0"
    ]
    assert current_event["image_urls"] == []


async def test_the_chat_url_keeps_readable_history_when_the_current_leg_has_none(
    tmp_path, monkeypatch
):
    app, chat = _chat_app(tmp_path)
    app.state.app_state.sessions[CURRENT_KEY].capabilities = frozenset(
        {Capability.INJECT, Capability.INTERACT}
    )

    async def read_transcript(account, session, after_seq=0):
        assert session.key == OLD_KEY
        return [TranscriptEvent(seq=1, role="user", text="Earlier decision")]

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "read_transcript", read_transcript
    )

    async with _client(app) as client:
        through_chat = await client.get(f"/chats/{chat.chat_id}/transcript.json")

    assert through_chat.status_code == 200
    assert through_chat.json()["session_key"] == CURRENT_KEY
    assert [event["text"] for event in through_chat.json()["events"]] == [
        "Earlier decision"
    ]


async def test_a_missing_middle_leg_does_not_renumber_later_chat_event_ids(
    tmp_path, monkeypatch
):
    app, chat = _chat_app(tmp_path)
    state = app.state.app_state
    third_key = "claude_code:main:leg-three"
    state.update_session(
        Session(
            key=third_key,
            account_key="claude_code:main",
            session_id="leg-three",
            status=SessionStatus.IDLE,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    app.state.budget_db.append_chat_leg(CURRENT_KEY, third_key)
    state.sessions.pop(CURRENT_KEY)

    async def read_transcript(account, session, after_seq=0):
        return [TranscriptEvent(seq=1, role="user", text=session.key)]

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "read_transcript", read_transcript
    )

    async with _client(app) as client:
        through_chat = await client.get(f"/chats/{chat.chat_id}/transcript.json")

    assert through_chat.status_code == 200
    assert through_chat.json()["session_key"] == third_key
    assert [
        (event["text"], event["chat_event_id"])
        for event in through_chat.json()["events"]
    ] == [(OLD_KEY, "1"), (third_key, "2.1")]


def test_the_poller_accepts_the_canonical_chat_url_a_worker_publishes():
    """``transcript.json`` already hands workers ``/chats/<id>`` (#1265)."""
    chat_url = "https://deck.example/chats/6b421ae740604919bc4d4b016346149c"
    leg_url = "https://deck.example/sessions/claude_code:main:leg-one"
    assert public_session_url(chat_url) == chat_url
    assert public_session_url(leg_url) == leg_url
    assert public_session_url("https://deck.example/usage") == ""
    assert public_session_url("http://127.0.0.1:8756/chats/abc") == ""


# --- chat-owned budget ----------------------------------------------------


def _budget_state(tmp_path):
    db = Db(tmp_path / "chat.db")
    db.ensure_session_chats((OLD_KEY,))
    db.append_chat_leg(OLD_KEY, CURRENT_KEY)
    state = AppState(db=db)
    return db, state


def _leg(key: str, account_key: str) -> Session:
    return Session(
        key=key,
        account_key=account_key,
        session_id=key.rsplit(":", 1)[-1],
        status=SessionStatus.IDLE,
    )












# --- chat-owned attention and questions ----------------------------------


def test_a_report_made_on_the_leg_the_chat_left_still_describes_the_chat(tmp_path):
    db, state = _budget_state(tmp_path)
    state.update_session(_leg(OLD_KEY, "claude_code:main"))
    state.update_session(_leg(CURRENT_KEY, "claude_code:alt"))
    # Written straight onto the row, as a report recorded before the chat
    # gained its second leg would have been.
    state.attention_reports[OLD_KEY] = state.db.record_session_attention_report(
        OLD_KEY, "waiting"
    )

    assert state.attention_report_for(CURRENT_KEY).state == "waiting"
    # Done on the chat settles it everywhere, so nothing resurfaces.
    assert state.clear_attention_report(CURRENT_KEY)
    assert state.attention_report_for(CURRENT_KEY) is None
    assert state.attention_reports == {}
    db.close()


def test_a_later_boss_turn_on_the_new_leg_expires_the_old_legs_report(tmp_path):
    db, state = _budget_state(tmp_path)
    state.update_session(_leg(OLD_KEY, "claude_code:main"))
    state.update_session(_leg(CURRENT_KEY, "claude_code:alt"))
    state.attention_reports[OLD_KEY] = state.db.record_session_attention_report(
        OLD_KEY, "waiting"
    )

    answered = Session(
        key=CURRENT_KEY,
        account_key="claude_code:alt",
        session_id="leg-two",
        status=SessionStatus.IDLE,
        last_role="user",
        last_activity=datetime.now(UTC) + timedelta(minutes=1),
    )
    state.update_session(answered)

    assert state.attention_report_for(CURRENT_KEY) is None
    db.close()


# --- a key the deck has not scanned yet (#2627) ----------------------------

UNSCANNED_KEY = "claude_code:main:not-scanned-yet"


async def test_a_token_bound_key_stores_its_rows_before_the_first_scan(tmp_path):
    """A worker's first-turn calls use the key by-token gave it, before any scan."""
    app, _chat = _chat_app(tmp_path)
    state = app.state.app_state
    state.bind_session_token("tok-2627", UNSCANNED_KEY)
    state.bind_session_run(UNSCANNED_KEY, "github_issue:eerovil/agentdeck#2627", "run-1")
    base = f"/api/sessions/{UNSCANNED_KEY}"

    async with _client(app) as client:
        resolved = (await client.get("/api/sessions/by-token/tok-2627")).json()
        assert resolved["session_key"] == UNSCANNED_KEY
        assert resolved["works"] == ["github_issue:eerovil/agentdeck#2627"]
        title = await client.put(f"{base}/title", json={"title": "agentdeck#2627 · Fix it"})
        issue = await client.post(f"{base}/issues", json={"issue": "eerovil/agentdeck#2627"})
        pull = await client.post(
            f"{base}/pull-requests", json={"pull_request": "eerovil/agentdeck#2700"}
        )
        # Its Run never ends; a later Work the chat took on itself may still be named (#3392).
        stranger = await client.post(
            f"{base}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#2701", "work_id": "eerovil/agentdeck#9"},
        )
        await client.post(f"{base}/issues", json={"issue": "eerovil/agentdeck#2628"})
        unnamed = await client.post(
            f"{base}/pull-requests", json={"pull_request": "eerovil/agentdeck#2701"}
        )
        later = await client.post(
            f"{base}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#2701", "work_id": "eerovil/agentdeck#2628"},
        )
        both = (await client.get("/api/sessions/by-token/tok-2627")).json()["works"]
        nudge = await client.put(f"{base}/restart-nudge")
        # In two Works now, it names the one it titles.
        work_title = await client.put(
            f"{base}/work-title", json={"title": "Fix it", "work_id": "eerovil/agentdeck#2627"}
        )
        transcript = await client.get(f"{base}/transcript-check")

    assert title.status_code == 200
    assert title.json()["session_key"] == UNSCANNED_KEY
    assert title.json()["title"] == "Fix it"
    assert issue.status_code == 200
    assert [ref.number for ref in state.issues_for(UNSCANNED_KEY)] == [2627, 2628]
    assert pull.status_code == 200
    assert [ref.number for ref in state.pull_requests_for(UNSCANNED_KEY)] == [2700, 2701]
    # Registered for the Run's Work (#3392), the one the review router then places it in.
    assert state.pull_requests_for(UNSCANNED_KEY)[0].work_id == (
        "github_issue:eerovil/agentdeck#2627"
    )
    assert stranger.status_code == 409
    assert unnamed.status_code == 409, "in two Works, the Run's is no longer a default"
    assert "several Works" in unnamed.json()["detail"]
    assert later.status_code == 200
    assert state.pull_requests_for(UNSCANNED_KEY)[1].work_id == (
        "github_issue:eerovil/agentdeck#2628"
    )
    assert both == ["github_issue:eerovil/agentdeck#2627", "github_issue:eerovil/agentdeck#2628"]
    assert nudge.json()["session_key"] == UNSCANNED_KEY
    assert nudge.json()["nudge_on_restart"] is True
    assert work_title.status_code == 200
    assert work_title.json()["agent_title"] == "Fix it"
    # Reading the transcript needs the transcript itself, so it still waits for the scan.
    assert transcript.status_code == 404
    assert transcript.json()["detail"] == "unknown session"


async def test_a_key_no_token_names_is_still_unknown(tmp_path):
    app, _chat = _chat_app(tmp_path)
    base = f"/api/sessions/{UNSCANNED_KEY}"

    async with _client(app) as client:
        title = await client.put(f"{base}/title", json={"title": "Fix it"})
        issue = await client.post(f"{base}/issues", json={"issue": "eerovil/agentdeck#2627"})

    for response in (title, issue):
        assert response.status_code == 404
        assert response.json()["detail"] == "unknown session"
    assert app.state.app_state.issues_for(UNSCANNED_KEY) == ()


async def test_a_token_bound_key_of_no_configured_account_is_still_unknown(tmp_path):
    app, _chat = _chat_app(tmp_path)
    app.state.app_state.bind_session_token("tok-other", "codex:elsewhere:thread")

    async with _client(app) as client:
        title = await client.put(
            "/api/sessions/codex:elsewhere:thread/title", json={"title": "Fix it"}
        )

    assert title.status_code == 404


def test_a_token_bound_key_is_known_from_the_store_before_this_process_adopts_it(tmp_path):
    """The runtime binds the token; the web may read the stored row before following it."""
    db = Db(tmp_path / "budgets.db")
    db.record_session_token("tok-runtime", UNSCANNED_KEY)
    state = AppState(budget_db=db)
    state.session_tokens.clear()

    assert state.session_key_has_token(UNSCANNED_KEY)
    assert not state.session_key_has_token("claude_code:main:someone-else")
