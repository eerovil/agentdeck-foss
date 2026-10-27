"""Side chats: a new chat cut from another chat's history after one finished turn (#2485)."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from pathlib import Path

import pytest
import uvicorn
from conftest import async_playwright
from httpx import ASGITransport, AsyncClient
from worker_harness import claude_host, settle

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.db import Db
from agentdeck.models import (
    Account,
    Capability,
    ForkFrom,
    InjectResult,
    Session,
    SessionStatus,
)
from agentdeck.providers.claude_code import transcripts as claude_transcripts
from agentdeck.providers.codex import transcripts as codex_transcripts
from agentdeck.providers.codex.appserver import AppServerError, CodexAppServer
from agentdeck.providers.delivery import DeliveryReceipts


def _jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _assistant(uuid: str, *blocks: dict, stop: str = "end_turn") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": list(blocks), "stop_reason": stop},
    }


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _prompt(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result() -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }


# --- where a cut can go ------------------------------------------------------


def test_claude_cut_only_after_a_reply_that_ends_its_turn(tmp_path):
    path = _jsonl(
        tmp_path / "s.jsonl",
        [
            _prompt("first"),  # 1
            _assistant("a-mid", _text("looking"), stop="tool_use"),  # 2
            _assistant("a-tool", {"type": "tool_use", "name": "Bash"}, stop="tool_use"),  # 3
            _tool_result(),  # 4: a tool result is not a new prompt
            {"type": "last-prompt"},  # 5
            _assistant("a-end", _text("done")),  # 6
            {"type": "queue-operation", "operation": "enqueue", "content": "x"},  # 7
            _prompt("second"),  # 8
            _assistant("b-end", _text("again")),  # 9
        ],
    )
    assert claude_transcripts.fork_ref(path, 2) is None  # more of the turn follows
    assert claude_transcripts.fork_ref(path, 3) is None  # not a reply with text
    assert claude_transcripts.fork_ref(path, 4) is None  # not an assistant line
    assert claude_transcripts.fork_ref(path, 6) == "a-end"
    assert claude_transcripts.fork_ref(path, 9) == "b-end"
    # The newest turn may still be running; only the live session can say.
    assert claude_transcripts.fork_ref(path, 9, turn_finished=False) is None
    assert claude_transcripts.fork_ref(path, 6, turn_finished=False) == "a-end"
    assert claude_transcripts.fork_ref(path, 0) is None
    assert claude_transcripts.fork_ref(path, 99) is None
    assert claude_transcripts.fork_ref(tmp_path / "missing.jsonl", 6) is None


def test_claude_cut_refuses_an_api_error_reply(tmp_path):
    error = _assistant("err", _text("API Error: 529"))
    error["isApiErrorMessage"] = True
    path = _jsonl(tmp_path / "s.jsonl", [_prompt("hi"), error])
    assert claude_transcripts.fork_ref(path, 2) is None


def _codex_rows(*, finish_second: bool) -> list[dict]:
    def started(turn: str) -> dict:
        return {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}}

    def message(role: str, text: str) -> dict:
        return {
            "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"text": text}]},
        }

    def complete(turn: str) -> dict:
        return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}

    rows = [
        {"type": "session_meta", "payload": {"id": "thread"}},  # 1
        started("t1"),  # 2
        message("user", "hi"),  # 3
        message("assistant", "thinking aloud"),  # 4
        message("assistant", "answer one"),  # 5
        complete("t1"),  # 6
        started("t2"),  # 7
        message("user", "more"),  # 8
        message("assistant", "answer two"),  # 9
    ]
    if finish_second:
        rows.append(complete("t2"))  # 10
    return rows


def test_codex_cut_only_after_a_completed_turns_last_message(tmp_path):
    path = _jsonl(tmp_path / "r.jsonl", _codex_rows(finish_second=True))
    assert codex_transcripts.fork_turn(path, 4) is None  # another message follows
    assert codex_transcripts.fork_turn(path, 5) == "t1"
    assert codex_transcripts.fork_turn(path, 3) is None  # the person's own message
    assert codex_transcripts.fork_turn(path, 9) == "t2"
    running = _jsonl(tmp_path / "running.jsonl", _codex_rows(finish_second=False))
    assert codex_transcripts.fork_turn(running, 9) is None  # Codex cannot fork a live turn
    assert codex_transcripts.fork_turn(running, 5) == "t1"


# --- the Claude worker ---------------------------------------------------------


def test_claude_fork_command_copies_the_source_up_to_the_cut(tmp_path):
    host, _spawned = claude_host(tmp_path)
    fork = ForkFrom("source-sid", "line-uuid")
    cmd = host._command(None, fork_from=fork)
    at = cmd.index("--resume")
    assert cmd[at : at + 5] == [
        "--resume",
        "source-sid",
        "--fork-session",
        "--resume-session-at",
        "line-uuid",
    ]
    # The side chat is told its own token, never the source's: its copied history
    # already holds the source's key (#2576).
    tokened = host._command(None, fork_from=fork, session_token="side-token")
    guide = tokened[tokened.index("--append-system-prompt") + 1]
    assert "agentdeck session-key side-token" in guide
    # A revive of the side chat later resumes the side chat itself, never the source.
    revived = host._command("side-sid", fork_from=fork)
    assert revived[-2:] == ["--resume", "side-sid"]
    assert "--fork-session" not in revived


def test_a_side_chat_is_a_different_start_from_a_plain_one():
    def fingerprint(fork):
        return DeliveryReceipts.fingerprint(
            "hi",
            cwd="/w",
            fresh=True,
            images=[],
            model=None,
            effort=None,
            permission_mode="plan",
            fork=fork,
        )

    assert fingerprint(None) != fingerprint("s@a")
    assert fingerprint("s@a") != fingerprint("s@b")


async def test_claude_fork_spawns_once_with_the_cut_and_reports_the_new_session(tmp_path):
    host, spawned = claude_host(tmp_path)
    result = await host.deliver(
        "chat-1",
        "what about X?",
        cwd=str(tmp_path),
        fresh=True,
        permission_mode="plan",
        delegated=False,
        fork_from=ForkFrom("source-sid", "line-uuid"),
    )
    await settle()
    assert result.accepted
    assert result.session_id == "sid-auto-1"
    assert len(spawned) == 1
    args = list(spawned[0]["args"])
    assert args[args.index("--resume") + 1] == "source-sid"
    assert "--fork-session" in args
    assert args[args.index("--resume-session-at") + 1] == "line-uuid"
    assert args[args.index("--permission-mode") + 1] == "plan"
    await host.stop()


async def test_claude_fork_never_falls_back_to_an_empty_chat(tmp_path):
    host, spawned = claude_host(tmp_path)

    async def refuse(*args, **kwargs):
        spawned.append({"args": args})
        raise OSError("claude refused the cut")

    host._process_factory = refuse
    result = await host.deliver(
        "chat-1",
        "what about X?",
        cwd=str(tmp_path),
        fresh=True,
        delegated=False,
        fork_from=ForkFrom("source-sid", "line-uuid"),
    )
    assert not result.accepted
    # One attempt, and it was the fork: no second spawn without the history.
    assert len(spawned) == 1
    await host.stop()


async def test_claude_fork_is_only_a_fresh_start(tmp_path):
    host, spawned = claude_host(tmp_path)
    result = await host.deliver(
        "chat-1",
        "hi",
        cwd=str(tmp_path),
        delegated=False,
        fork_from=ForkFrom("source-sid", "line-uuid"),
    )
    assert not result.accepted
    assert result.reason == "fork_requires_fresh"
    assert spawned == []


# --- the Codex app-server ------------------------------------------------------


def _codex_server(tmp_path: Path) -> CodexAppServer:
    return CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path), persistent_backend=False
    )


async def test_codex_fork_copies_the_thread_through_the_chosen_turn(tmp_path):
    from unittest.mock import AsyncMock

    server = _codex_server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "thread/fork":
            return {"thread": {"id": "side-thread", "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn-9", "status": "inProgress"}}
        return {}

    server._request = request
    result = await server.start_thread(
        tmp_path,
        "what about X?",
        sandbox="read-only",
        fork_from=ForkFrom("source-thread", "turn-3"),
        session_token="side-token",
    )
    assert result.accepted
    assert result.session_id == "side-thread"
    method, params = calls[0]
    assert method == "thread/fork"
    # The fork carries the side chat's own token, not the source's (#2576).
    assert "agentdeck session-key side-token" in params["developerInstructions"]
    assert params["threadId"] == "source-thread"
    assert params["lastTurnId"] == "turn-3"
    assert params["sandbox"] == "read-only"
    assert params["threadSource"] == "agentdeck"
    assert server.owns("side-thread")
    assert calls[1][0] == "turn/start"


async def test_a_refused_codex_fork_says_why_and_starts_nothing(tmp_path):
    from unittest.mock import AsyncMock

    server = _codex_server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append(method)
        error = AppServerError("{'code': -32600, 'message': 'turn turn-3 is in progress'}")
        error.rpc_error = {"code": -32600, "message": "turn turn-3 is in progress"}
        raise error

    server._request = request
    result = await server.start_thread(
        tmp_path, "hi", fork_from=ForkFrom("source-thread", "turn-3")
    )
    assert not result.accepted
    # Codex's own sentence, not the JSON-RPC object it came in.
    assert result.reason == "Codex could not make the side chat: turn turn-3 is in progress"
    assert calls == ["thread/fork"]


# --- where the link is kept --------------------------------------------------------


def test_fork_link_is_found_once_the_start_names_its_session(tmp_path):
    db = Db(tmp_path / "h.db")
    assert db.reserve_chat_fork("tok-1", "acct:source", 12, "press-1") == "tok-1"
    # The same press again is the same side chat, never a second one.
    assert db.reserve_chat_fork("tok-2", "acct:source", 12, "press-1") == "tok-1"
    assert db.chat_fork_source("acct:side") is None
    db.record_session_token("tok-1", "acct:side")
    assert db.chat_fork_source("acct:side") == ("acct:source", 12)
    # A plain chat started under a token of its own is not a side chat.
    db.record_session_token("tok-3", "acct:plain")
    assert db.chat_fork_source("acct:plain") is None
    # Releasing never drops a side chat that started; only a start that did not.
    db.release_chat_fork("tok-1")
    assert db.chat_fork_source("acct:side") == ("acct:source", 12)
    assert db.reserve_chat_fork("tok-4", "acct:source", 3, "press-2") == "tok-4"
    db.release_chat_fork("tok-4")
    assert db.reserve_chat_fork("tok-5", "acct:source", 3, "press-2") == "tok-5"


# --- the route -----------------------------------------------------------------


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            "codex:test:sid",
            "codex:test",
            "sid",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


def _forkable(monkeypatch, *, ref="turn-3", refusal=None):
    from agentdeck.providers import PROVIDERS

    codex = PROVIDERS["codex"]
    monkeypatch.setattr(codex, "fork_refusal", lambda account, session: refusal)
    monkeypatch.setattr(codex, "fork_ref", lambda account, session, seq: ref)
    monkeypatch.setattr(codex, "fork_settings", lambda account, session: ("gpt-x", "high"))
    starts = []

    async def start_session(account, cwd, message, **kwargs):
        starts.append({"cwd": cwd, "message": message, **kwargs})
        return InjectResult(True, session_id="side")

    monkeypatch.setattr(codex, "start_session", start_session)
    return starts


def _targets(monkeypatch):
    """One Poller repository new work can be filed in (#3171)."""
    from agentdeck import new_work
    from agentdeck.web import routes_works

    target = new_work.Target(
        repository="eerovil/agentdeck", project="agentdeck", ownership_label="agent"
    )
    monkeypatch.setattr(routes_works, "_new_work_targets", lambda request: (target,))


async def test_side_chat_starts_from_the_chosen_reply_with_full_permissions(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "what about X?"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "press-0001"},
        )
        assert response.status_code == 202
        assert 'id="fork-session-result"' in response.text
        for _ in range(20):
            await settle()
            if starts:
                break
        again = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "what about X?"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "press-0001"},
        )
        assert again.status_code == 202
        await settle()
    assert len(starts) == 1
    start = starts[0]
    assert start["fork_from"] == ForkFrom("sid", "turn-3")
    # Same permissions as a normal new chat, not read-only (#2750).
    assert "sandbox" not in start
    assert start["model"] == "gpt-x"
    assert start["effort"] == "high"
    assert start["cwd"] == tmp_path
    token = start["session_token"]
    app.state.app_state.bind_session_token(token, "codex:test:side")
    assert app.state.app_state.chat_fork_source("codex:test:side") == ("codex:test:sid", 7)
    await app.state.injector.stop()


async def test_new_work_from_a_reply_briefs_a_side_chat_to_file_it(tmp_path, monkeypatch):
    """#3171: the side chat files the work in the chosen repository and asks nothing."""
    app = _app(tmp_path)
    # Room for the brief; a person's words that leave it none are refused up front.
    app.state.config.inject.max_message_chars = 1500
    starts = _forkable(monkeypatch)
    _targets(monkeypatch)
    headers = {"origin": "http://test"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        elsewhere = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "add X", "repository": "someone/else"},
            headers=headers,
        )
        assert elsewhere.status_code == 422
        assert "Choose a repository" in elsewhere.text
        too_long = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "x" * 1400, "repository": "eerovil/agentdeck"},
            headers=headers,
        )
        assert too_long.status_code == 422
        assert "too long" in too_long.text
        assert starts == []
        response = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "add X", "repository": "EEROVIL/agentdeck"},
            headers=headers,
        )
        assert response.status_code == 202
        for _ in range(20):
            await settle()
            if starts:
                break
    assert len(starts) == 1
    assert starts[0]["fork_from"] == ForkFrom("sid", "turn-3")
    brief = starts[0]["message"]
    assert "The work, in my words:\nadd X" in brief
    assert "`gh issue create --repo eerovil/agentdeck --label agent`" in brief
    assert "Ask me nothing" in brief
    await app.state.injector.stop()


async def test_side_chat_refusals_say_why(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch, ref=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mid_turn = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "hi"},
            headers={"origin": "http://test"},
        )
        assert mid_turn.status_code == 409
        assert "does not end a finished turn" in mid_turn.text
        no_seq = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"message": "hi"},
            headers={"origin": "http://test"},
        )
        assert no_seq.status_code == 422
        _forkable(monkeypatch, refusal="This kind of chat cannot be branched into a side chat.")
        unsupported = await client.post(
            "/sessions/codex:test:sid/fork",
            data={"seq": "7", "message": "hi"},
            headers={"origin": "http://test"},
        )
        assert unsupported.status_code == 409
        assert "cannot be branched" in unsupported.text
    assert starts == []
    await app.state.injector.stop()


async def test_side_chat_page_links_back_and_offers_the_way_home(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _forkable(monkeypatch)
    state = app.state.app_state
    state.update_session(
        Session(
            "codex:test:side",
            "codex:test",
            "side",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    state.reserve_chat_fork("tok", "codex:test:sid", 7, None)
    state.bind_session_token("tok", "codex:test:side")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unmanaged = await client.get("/sessions/codex:test:sid")
        _targets(monkeypatch)
        side = await client.get("/sessions/codex:test:side")
        source = await client.get("/sessions/codex:test:sid")
    # With nowhere to file new work, there is no modal and so no reply button.
    assert "data-fork-form" not in unmanaged.text
    assert "Side chat of" in side.text
    assert "data-side-chat-origin" in side.text
    assert 'data-target-url="/sessions/codex:test:sid/composer-target"' in side.text
    assert "Summarise" in side.text
    # The original says nothing new: the link runs one way.
    assert "data-side-chat-origin" not in source.text
    # Both pages can branch, so both carry the new-work modal.
    assert "data-fork-dialog" in source.text
    assert 'hx-post="/sessions/codex:test:sid/fork"' in source.text
    assert '<option value="eerovil/agentdeck"' in source.text
    await app.state.injector.stop()


async def test_send_back_targets_the_originals_newest_leg_at_send_time(tmp_path, monkeypatch):
    app = _app(tmp_path)
    state = app.state.app_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        before = await client.get("/sessions/codex:test:sid/composer-target")
        assert before.json()["draft_key"] == (
            "agentdeck:message-draft:/sessions/codex:test:sid/inject"
        )
        # The original moves on to a new leg while the side chat stays open.
        state.update_session(
            Session(
                "codex:test:sid2",
                "codex:test",
                "sid2",
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        )
        state.budget_db.append_chat_leg("codex:test:sid", "codex:test:sid2")
        after = await client.get("/sessions/codex:test:sid/composer-target")
    assert after.json()["draft_key"] == (
        "agentdeck:message-draft:/sessions/codex:test:sid2/inject"
    )
    assert after.json()["url"] == state.chat_path_for_session("codex:test:sid2")
    await app.state.injector.stop()


async def test_a_failed_side_chat_start_leaves_the_press_free_to_retry(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    outcomes = [InjectResult(False, "this provider cannot start sessions"), None]
    real_start_new = app.state.injector.start_new

    async def start_new(*args, **kwargs):
        outcome = outcomes.pop(0)
        if outcome is not None:
            return outcome
        return await real_start_new(*args, **kwargs)

    app.state.injector.start_new = start_new
    headers = {"origin": "http://test", "x-agentdeck-action-id": "press-0002"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            "/sessions/codex:test:sid/fork", data={"seq": "7", "message": "hi"}, headers=headers
        )
        assert refused.status_code == 422
        # The resend of that same press really starts, rather than being shown the
        # start that never happened.
        retried = await client.post(
            "/sessions/codex:test:sid/fork", data={"seq": "7", "message": "hi"}, headers=headers
        )
        assert retried.status_code == 202
        for _ in range(20):
            await settle()
            if starts:
                break
    assert len(starts) == 1
    await app.state.injector.stop()


async def test_a_side_chat_start_that_raises_releases_its_reservation(tmp_path, monkeypatch):
    import pytest as _pytest

    app = _app(tmp_path)
    _forkable(monkeypatch)
    released = []
    state = app.state.app_state
    real_release = state.release_chat_fork
    monkeypatch.setattr(
        state, "release_chat_fork", lambda token: (released.append(token), real_release(token))
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("injector down")

    app.state.injector.start_new = boom
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=True), base_url="http://test"
    ) as client:
        with _pytest.raises(RuntimeError):
            await client.post(
                "/sessions/codex:test:sid/fork",
                data={"seq": "7", "message": "hi"},
                headers={"origin": "http://test", "x-agentdeck-action-id": "press-0003"},
            )
    assert len(released) == 1
    assert state.reserve_chat_fork("fresh", "codex:test:sid", 7, "press-0003") == "fresh"
    await app.state.injector.stop()


# --- the page ------------------------------------------------------------------


def _claude_app_with_turns(tmp_path):
    proj = tmp_path / "projects" / "-tmp"
    proj.mkdir(parents=True)
    _jsonl(
        proj / "sid1.jsonl",
        [
            _prompt("first question"),  # 1
            _assistant("a-mid", _text("let me look"), stop="tool_use"),  # 2
            _assistant(
                "a-tool", {"type": "tool_use", "name": "Bash", "input": {}}, stop="tool_use"
            ),
            _tool_result(),  # 4
            _assistant("a-end", _text("the answer")),  # 5
            _prompt("second question"),  # 6
            _assistant("b-end", _text("second answer")),  # 7
        ],
    )
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "h.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.IDLE,
            cwd=tmp_path,
            title="Original chat",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    return app


@asynccontextmanager
async def _served(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        # Bounded, so a failing assertion is reported instead of hanging on the stream.
        server.should_exit = True
        done, _ = await asyncio.wait({task}, timeout=5)
        if not done:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        sock.close()


@pytest.mark.browser
async def test_new_work_button_opens_the_modal_only_on_turn_ends(tmp_path, monkeypatch):
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "fork_refusal", lambda account, session: None
    )
    # The page offers the cut; the server then refuses it, so nothing ever starts.
    monkeypatch.setattr(PROVIDERS["claude_code"], "fork_ref", lambda account, session, seq: None)
    app = _claude_app_with_turns(tmp_path)
    _targets(monkeypatch)
    app.state.injector.can_queue = lambda session_key: True  # the chat has its own composer
    async with _served(app) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.goto(f"{base}/sessions/claude_code:test:sid1")
            await page.wait_for_function("() => !!window.agentdeckSideChat")
            composer = page.locator(".inject-form")
            assert await composer.is_visible()
            visible = await page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-fork-toggle]'))
                     .filter((b) => !b.hidden).map((b) => b.dataset.forkSeq)"""
            )
            # A reply mid-turn has more of the same turn after it; it is not a cut.
            assert visible == ["5", "7"]
            head = page.locator('[data-event-seq="5"] .ev-head')
            height_before = await head.evaluate("(h) => h.getBoundingClientRect().height")
            form = page.locator("[data-fork-form]")
            assert await form.is_hidden()
            await page.locator('[data-fork-toggle][data-fork-seq="5"]').click()
            assert await form.is_visible()
            assert await page.locator("[data-fork-dialog]").evaluate("(d) => d.open")
            assert await form.get_attribute("hx-post") == "/sessions/claude_code:test:sid1/fork"
            assert await page.locator("[data-fork-seq-input]").input_value() == "5"
            assert await page.evaluate("() => document.activeElement.id") == "fork-message"
            assert await page.locator("#fork-repository").input_value() == "eerovil/agentdeck"
            # A modal: the chat's own composer stays where it is.
            assert await composer.evaluate(
                "(f) => !f.hidden && f.getBoundingClientRect().height > 0"
            )
            # The button sits in the reply's existing head row, which keeps its height.
            assert await head.evaluate("(h) => h.getBoundingClientRect().height") == height_before
            # ↩ belongs to side chats only.
            assert await page.locator("[data-send-back]:visible").count() == 0
            await page.locator("[data-fork-cancel]").click()
            assert await form.is_hidden()
            # A refusal is said inside the modal, with the words kept.
            await page.locator('[data-fork-toggle][data-fork-seq="7"]').click()
            await page.fill("#fork-message", "add X")
            await page.locator('[data-fork-form] button[type="submit"]').click()
            error = page.locator("[data-fork-error]")
            await error.wait_for()
            assert "reply" in await error.inner_text()
            assert await page.locator("#fork-message").input_value() == "add X"
            assert await form.is_visible()
            await browser.close()


@pytest.mark.browser
async def test_send_to_original_waits_in_the_original_chats_message_box(tmp_path):
    app = _claude_app_with_turns(tmp_path)
    _jsonl(
        tmp_path / "projects" / "-tmp" / "sid2.jsonl",
        [_prompt("what about X?"), _assistant("c-end", _text("X is fine, ship it"))],
    )
    state = app.state.app_state
    state.update_session(
        replace(
            state.sessions["claude_code:test:sid1"],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:sid2",
            account_key="claude_code:test",
            session_id="sid2",
            status=SessionStatus.IDLE,
            cwd=tmp_path,
            title="About X",
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    # The original takes messages from the deck, as a deck-owned chat does; the
    # transcript scan alone would not grant that to a chat no worker holds.
    app.state.injector.can_queue = lambda session_key: True
    state.reserve_chat_fork("tok", "claude_code:test:sid1", 5, None)
    state.bind_session_token("tok", "claude_code:test:sid2")
    async with _served(app) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.goto(f"{base}/sessions/claude_code:test:sid2")
            await page.wait_for_function("() => !!window.agentdeckSideChat")
            assert await page.locator("[data-side-chat-origin]").is_visible()
            assert await page.locator("[data-send-back]:visible").count() == 1
            # Text already waiting in the original's composer is the person's own.
            await page.evaluate(
                "() => sessionStorage.setItem("
                "'agentdeck:message-draft:/sessions/claude_code:test:sid1/inject',"
                " 'my own half-written note')"
            )
            await page.locator("[data-send-back-latest]").click()
            # The original opens at its own chat address, whatever that is.
            await page.wait_for_url(f"{base}/chats/*")
            await page.wait_for_function(
                "() => (document.querySelector('#inject-message') || {}).value"
            )
            assert (
                await page.locator("[data-session-key]").first.get_attribute("data-session-key")
                == "claude_code:test:sid1"
            )
            draft = await page.locator("#inject-message").input_value()
            assert draft.startswith("my own half-written note\n\n")
            assert "Summary from side chat “About X” (for information):" in draft
            assert "X is fine, ship it" in draft
            await browser.close()


@pytest.mark.browser
async def test_side_chat_controls_work_in_a_chat_the_desktop_front_page_opens(
    tmp_path, monkeypatch
):
    """The desktop front page opens a chat in its column without loading a page, and
    scripts inside the inserted chat never run (#2493)."""
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "fork_refusal", lambda account, session: None
    )
    app = _claude_app_with_turns(tmp_path)
    _targets(monkeypatch)
    _jsonl(
        tmp_path / "projects" / "-tmp" / "sid2.jsonl",
        [_prompt("what about X?"), _assistant("c-end", _text("X is fine, ship it"))],
    )
    state = app.state.app_state
    state.update_session(
        replace(
            state.sessions["claude_code:test:sid1"],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:sid2",
            account_key="claude_code:test",
            session_id="sid2",
            status=SessionStatus.IDLE,
            cwd=tmp_path,
            title="About X",
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            thinking=True,
        )
    )
    # Working, so the Works' "Other activity" lists both chats outside every Work.
    state.update_session(replace(state.sessions["claude_code:test:sid1"], thinking=True))
    app.state.injector.can_queue = lambda session_key: True
    state.reserve_chat_fork("tok", "claude_code:test:sid1", 5, None)
    state.bind_session_token("tok", "claude_code:test:sid2")
    visible_forks = """() => Array.from(document.querySelectorAll(
        '.session-detail [data-fork-toggle]')).filter((b) => !b.hidden)
        .map((b) => b.dataset.forkSeq)"""
    async with _served(app) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(
                viewport={"width": 1600, "height": 900}
            )
            await page.goto(f"{base}/")
            await page.evaluate("() => { window.__sameDocument = true; }")
            await page.locator('#work-other a[data-session-key="claude_code:test:sid1"]').click()
            await page.wait_for_selector(
                '.session-detail[data-session-key="claude_code:test:sid1"] [data-fork-form]',
                state="attached",
            )
            await page.wait_for_function(visible_forks + ".length > 0")
            # A reply mid-turn has more of the same turn after it; it is not a cut.
            assert await page.evaluate(visible_forks) == ["5", "7"]
            await page.locator('[data-fork-toggle][data-fork-seq="7"]').click()
            assert await page.locator("[data-fork-form]").is_visible()
            await page.locator("[data-fork-cancel]").click()

            # A side chat opened the same way offers its conclusion back.
            await page.locator('#work-other a[data-session-key="claude_code:test:sid2"]').click()
            origin = page.locator(
                '.session-detail[data-session-key="claude_code:test:sid2"] '
                "[data-side-chat-origin]"
            )
            await origin.wait_for()
            assert "Side chat of" in await origin.inner_text()
            await page.wait_for_function(
                "() => document.querySelectorAll('[data-send-back]:not([hidden])').length === 1"
            )
            # Every step above happened in the page the front page first loaded.
            assert await page.evaluate("() => window.__sameDocument") is True
            await page.locator("[data-send-back-latest]").click()
            await page.wait_for_url(f"{base}/chats/*")
            await page.wait_for_function(
                "() => (document.querySelector('#inject-message') || {}).value"
            )
            draft = await page.locator("#inject-message").input_value()
            assert "Summary from side chat “About X” (for information):" in draft
            assert "X is fine, ship it" in draft
            await browser.close()
