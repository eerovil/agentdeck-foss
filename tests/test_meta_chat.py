"""The deck chat (#3403): one always-on chat that starts a fresh leg after an idle hour."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient

from agentdeck import meta_chat
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig, MetaChatConfig
from agentdeck.inject import StartedChat
from agentdeck.models import Capability, InjectResult, Session, SessionStatus
from agentdeck.web.render import display_chat_message

KEY = "claude_code:main:sid1"


def _app(tmp_path, *, idle_minutes: float, deck_chat: bool = True):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=4000),
        meta_chat=MetaChatConfig(cwd=str(tmp_path)),
        accounts=[
            AccountConfig(provider="codex", label="other", config_dir=str(tmp_path / "c")),
            AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path)),
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            KEY,
            "claude_code:main",
            "sid1",
            SessionStatus.IDLE,
            cwd=tmp_path,
            last_activity=datetime.now(UTC) - timedelta(minutes=idle_minutes),
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    if deck_chat:
        app.state.db.record_deck_chat_start("first-token", meta_chat.FIRST)
        app.state.app_state.budget_db.record_session_token("first-token", KEY)
    starts, sends = [], []
    injector = app.state.injector

    async def start_new(account, provider, cwd, message, images=None, **kwargs):
        starts.append({"account": account.key, "cwd": cwd, "message": message, **kwargs})
        injector._record_start(
            StartedChat(
                token=kwargs["session_token"],
                account_key=account.key,
                cwd=str(cwd),
                message=message,
                origin=kwargs["origin"],
                source_session_key=kwargs.get("source_session_key"),
            )
        )
        return InjectResult(True)

    async def start(_account, _session, _provider, message, images, **_kwargs):
        sends.append(message)
        return InjectResult(True)

    app.state.injector.start_new = start_new
    app.state.injector.start = start
    return app, starts, sends


def _client(app):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"origin": "http://test"}
    )


def test_a_fresh_leg_only_after_the_whole_quiet_window():
    now = datetime.now(UTC)
    session = Session(KEY, "claude_code:main", "sid1", SessionStatus.IDLE)
    for minutes, expected in ((59, False), (61, True)):
        quiet = replace(session, last_activity=now - timedelta(minutes=minutes))
        assert meta_chat.needs_fresh_leg(quiet, busy=False, now=now, minutes=60) is expected
    stale = replace(session, last_activity=now - timedelta(hours=3))
    # A working turn, a queued message or no recorded activity keeps the message here.
    assert not meta_chat.needs_fresh_leg(
        replace(stale, thinking=True), busy=False, now=now, minutes=60
    )
    assert not meta_chat.needs_fresh_leg(stale, busy=True, now=now, minutes=60)
    assert not meta_chat.needs_fresh_leg(session, busy=False, now=now, minutes=60)
    assert meta_chat.needs_fresh_leg(
        replace(session, last_activity=now - timedelta(minutes=11)), busy=False, now=now, minutes=10
    )


def test_the_chat_view_hides_the_brief_behind_a_mark():
    from agentdeck.voice_mode import VOICE_LINE

    first = meta_chat.with_brief("What is running?")
    # #3481: answers follow the question API's answer_with, a box never by chat message.
    brief = meta_chat.brief()
    assert "/question and follow each question's answer_with" in brief
    assert "chat message only where it says message" in brief
    assert "Never answer a question box by chat message" in brief
    # #3773: "do you need me?" is one call, answered by the 🔊 paragraph alone.
    assert "make one call, GET $AGENTDECK_URL/api/works/needs-you" in brief
    assert "write only the 🔊 paragraph, with nothing after it" in brief
    # #3816: the owner can send the deck chat's page to another chat by asking.
    assert "to $AGENTDECK_URL/api/view/open-chat; your page moves there" in brief
    # #3814: the owner can switch every open page's project tabs by asking.
    assert "to $AGENTDECK_URL/api/view/project-filter; every open page switches" in brief
    later = meta_chat.with_brief("And now?", "https://deck/sessions/x/transcript")
    assert display_chat_message(first) == "🎙️ What is running?"
    assert display_chat_message(later) == "🎙️ And now?"
    assert display_chat_message(f"{later}\n\n{VOICE_LINE}") == "🎙️ And now? 🎧"
    # Only a leading brief is the deck's own.
    assert display_chat_message(f"quote: {first}") == f"quote: {first}"


async def test_an_idle_deck_chat_starts_a_fresh_leg_of_the_same_chat(tmp_path):
    app, starts, sends = _app(tmp_path, idle_minutes=61)
    async with _client(app) as client:
        response = await client.post(
            f"/sessions/{KEY}/inject", data={"message": "What is running?"}
        )

    assert response.status_code == 202, response.text
    assert "Fresh start" in response.text and 'id="inject-result"' in response.text
    assert sends == []
    [start] = starts
    assert start["account"] == "claude_code:main" and start["cwd"] == tmp_path
    assert start["origin"] == "deck_chat" and start["source_session_key"] == KEY
    assert start["message"].startswith("(Deck chat: ")
    assert start["message"].endswith("\n\nWhat is running?")
    assert f"/sessions/{KEY}/transcript" in start["message"]
    resets = [
        token for token, kind, _ in app.state.db.deck_chat_starts() if kind == meta_chat.RESET
    ]
    assert resets == [start["session_token"]]
    # The new leg's seam reads as a fresh start once its token resolves.
    app.state.app_state.budget_db.record_session_token(resets[0], "claude_code:main:sid2")
    assert app.state.app_state.chat_rollover_reasons(KEY)["claude_code:main:sid2"] == (
        meta_chat.IDLE_RESET
    )


async def test_a_recent_deck_chat_or_any_other_chat_keeps_the_message_where_it_is(tmp_path):
    recent, recent_starts, recent_sends = _app(tmp_path / "a", idle_minutes=5)
    other, other_starts, other_sends = _app(tmp_path / "b", idle_minutes=600, deck_chat=False)
    for app in (recent, other):
        async with _client(app) as client:
            response = await client.post(f"/sessions/{KEY}/inject", data={"message": "hi"})
        assert response.status_code == 202, response.text
    assert recent_starts == other_starts == []
    assert recent_sends == other_sends == ["hi"]


async def test_meta_starts_the_deck_chat_once_then_redirects_to_it(tmp_path):
    app, starts, _sends = _app(tmp_path, idle_minutes=5, deck_chat=False)
    async with _client(app) as client:
        page = await client.get("/meta")
        assert page.status_code == 200 and 'hx-post="/meta/new"' in page.text
        started = await client.post("/meta/new", data={"message": "Hello deck"})
        assert started.status_code == 202, started.text
        [start] = starts
        # The first Claude account, in the configured folder, with the first-leg brief.
        assert start["account"] == "claude_code:main" and start["cwd"] == tmp_path
        assert start["message"] == meta_chat.with_brief("Hello deck")
        assert "Earlier conversation" not in start["message"]
        assert (await client.post("/meta/new", data={"message": "again"})).status_code == 409

        state = app.state.app_state
        state.budget_db.record_session_token(start["session_token"], KEY)
        # The start's "Open it" link lands on the chat itself, which names it, over the
        # chat's own self-chosen title.
        state.set_generated_title(KEY, "Agent's own name", "sig", source="agent")
        assert (await client.get(f"/sessions/{KEY}")).status_code == 200
        assert state.generated_title_for(KEY).title == meta_chat.TITLE
        # A name the owner typed stands.
        state.set_generated_title(KEY, "My helper", "sig", source="user")
        redirect = await client.get("/meta", follow_redirects=False)
    assert redirect.status_code == 303
    assert redirect.headers["location"].endswith(state.chat_path_for_session(KEY) or KEY)
    assert state.generated_title_for(KEY).title == "My helper"


def test_the_deck_chat_runs_only_on_a_claude_account():
    from types import SimpleNamespace as Account

    codex = Account(label="work", provider_id="codex")
    first, named = (Account(label=label, provider_id="claude_code") for label in ("a", "b"))
    accounts = [codex, first, named]
    assert meta_chat.resolve_account(accounts, None) is first
    assert meta_chat.resolve_account(accounts, "b") is named
    # A label naming another provider's account is refused, not used.
    assert meta_chat.resolve_account(accounts, "work") is None


async def test_a_first_start_lost_to_a_restart_still_blocks_a_second_one(tmp_path):
    """A web restart drops the start's record, not the start: while the runtime may still
    be spawning it, /meta/new refuses; a start lost for longer frees the way."""
    app, starts, _sends = _app(tmp_path, idle_minutes=5, deck_chat=False)
    db = app.state.db
    db.record_deck_chat_start("lost-now", meta_chat.FIRST)
    async with _client(app) as client:
        refused = await client.post("/meta/new", data={"message": "Hello deck"})
        assert refused.status_code == 409 and starts == []
        # A secret never rides a first message, whatever the fence says.
        secret = await client.post("/meta/new", data={"message": "/secret API_TOKEN abc123"})
        assert secret.status_code == 422 and "abc123" not in secret.text
        long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        db._write("UPDATE deck_chat_starts SET created_at = ?", (long_ago,))
        accepted = await client.post("/meta/new", data={"message": "Hello deck"})
    assert accepted.status_code == 202, accepted.text
    assert len(starts) == 1


async def test_the_deck_chat_moves_only_its_own_page_to_another_chat(tmp_path):
    """#3816: the deck chat names a chat; only a stream showing the deck chat carries the
    jump, once, as it is made and never in a prime."""
    import asyncio

    from agentdeck.chat_jump import JUMP_TOPIC
    from agentdeck.web.routes_sse import _session_stream

    app, _starts, _sends = _app(tmp_path, idle_minutes=5)
    state = app.state.app_state
    other = replace(state.sessions[KEY], key="claude_code:main:sid2", session_id="sid2")
    state.update_session(other)
    state.set_generated_title(other.key, "The login bug", "sig", source="agent")

    class Page:
        query_params: dict = {}

        def __init__(self):
            self.app = app

        async def is_disconnected(self):
            return False

    async def frames_until_jump(stream):
        while "event: chat_jump" not in (frame := await asyncio.wait_for(anext(stream), 5)):
            assert "data-chat-jump" not in frame
        return frame

    deck, elsewhere = _session_stream(Page(), KEY), _session_stream(Page(), other.key)
    try:
        # Each stream subscribes at its first frame; only the deck chat's hears jumps.
        await asyncio.wait_for(anext(deck), 5)
        await asyncio.wait_for(anext(elsewhere), 5)
        assert state.bus.subscribers(JUMP_TOPIC) == 1
        async with _client(app) as client:
            chats = (await client.get("/api/view/chats")).json()["chats"]
            title = state.sessions[other.key].display_title
            assert "The login bug" in title
            assert [(c["key"], c["title"]) for c in chats] == [(other.key, title)]
            for body, status in (({"chat": "nope"}, 404), ({"chat": KEY}, 409), ({}, 400)):
                assert (await client.post("/api/view/open-chat", json=body)).status_code == status
            assert app.state.chat_jumps.latest is None
            answer = await client.post("/api/view/open-chat", json={"chat": other.key})
        path = state.chat_path_for_session(other.key)
        assert answer.json() == {"opened": path, "title": title, "pages": 1}
        frame = await frames_until_jump(deck)
    finally:
        await deck.aclose()
        await elsewhere.aclose()
    assert f"&#34;path&#34;: &#34;{path}&#34;" in frame
    assert frame.count("event: chat_jump") == 1


def test_a_jump_finds_a_work_by_any_spelling_of_its_reference():
    from types import SimpleNamespace

    from agentdeck.chat_jump import work_matches

    cards = [
        SimpleNamespace(work_id="github_issue:eerovil/agentdeck#3816"),
        SimpleNamespace(work_id="github_issue:eerovil/bazaar#3816"),
        SimpleNamespace(work_id="github_issue:eerovil/agentdeck#38"),
    ]
    one = [cards[0]]
    for ref in ("eerovil/AgentDeck#3816", "agentdeck#3816", cards[0].work_id, " agentdeck#3816 "):
        assert work_matches(cards, ref) == one, ref
    assert work_matches(cards, "#3816") == work_matches(cards, "3816") == cards[:2]
    assert work_matches(cards, "other#3816") == work_matches(cards, "agentdeck") == []
