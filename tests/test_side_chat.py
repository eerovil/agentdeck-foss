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
from agentdeck.session_secrets import secret_notice


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
    # A Work's side chat (#4199) picks the newest reply itself, even mid-turn.
    assert claude_transcripts.newest_fork(path) == (9, "b-end")
    assert claude_transcripts.newest_fork(path, turn_finished=False) == (6, "a-end")
    working = _jsonl(
        tmp_path / "working.jsonl",
        [
            *[json.loads(line) for line in path.read_text().splitlines()],
            _prompt("third"),  # 10
            _assistant("c-mid", _text("running the tests"), stop="tool_use"),  # 11
            _assistant("c-tool", {"type": "tool_use", "name": "Bash"}, stop="tool_use"),  # 12
            _tool_result(),  # 13
        ],
    )
    assert claude_transcripts.newest_fork(working, turn_finished=False) == (9, "b-end")
    assert claude_transcripts.fork_ref(working, 9, turn_finished=False) == "b-end"
    assert claude_transcripts.newest_fork(tmp_path / "missing.jsonl") is None
    fresh = _jsonl(tmp_path / "new.jsonl", [_prompt("hi")])
    assert claude_transcripts.newest_fork(fresh) is None


def test_claude_cut_never_goes_before_a_compaction(tmp_path):
    """A resumed chat starts at its newest compaction, so Claude cannot find a cut before
    it ("No message found with message.uuid"): a Work's side chat of a long turn that
    compacted on its way failed to start (#4199)."""
    compaction = {"type": "system", "subtype": "compact_boundary", "parentUuid": None}
    summary = {
        "type": "user",
        "isCompactSummary": True,
        "message": {"role": "user", "content": "This session is being continued..."},
    }
    rows = [
        _prompt("first"),  # 1
        _assistant("a-end", _text("done")),  # 2
        _prompt("second"),  # 3
        _assistant("b-tool", {"type": "tool_use", "name": "Bash"}, stop="tool_use"),  # 4
        _tool_result(),  # 5
        compaction,  # 6
        summary,  # 7
        _assistant("b-mid", _text("still on it"), stop="tool_use"),  # 8
        _assistant("b-tool2", {"type": "tool_use", "name": "Bash"}, stop="tool_use"),  # 9
        _tool_result(),  # 10
    ]
    running = _jsonl(tmp_path / "running.jsonl", rows)
    assert claude_transcripts.fork_ref(running, 2) is None
    assert claude_transcripts.fork_ref(running, 2, turn_finished=False) is None
    # The running turn is the only one since: its words so far, not a refusal.
    assert claude_transcripts.newest_fork(running, turn_finished=False) == (8, "b-mid")
    just = _jsonl(tmp_path / "just.jsonl", rows[:7])
    assert claude_transcripts.newest_fork(just, turn_finished=False) is None
    done = _jsonl(
        tmp_path / "done.jsonl",
        [*rows, _assistant("b-end", _text("finished")), _prompt("third")],  # 11, 12
    )
    assert claude_transcripts.newest_fork(done, turn_finished=False) == (11, "b-end")
    assert claude_transcripts.fork_ref(done, 11) == "b-end"
    # With no compaction, a chat's first running turn is cut at its words so far too (#4314),
    # and before it has written any text there is nothing to cut at.
    first = _jsonl(tmp_path / "first.jsonl", [rows[0], *rows[7:10]])
    assert claude_transcripts.newest_fork(first, turn_finished=False) == (2, "b-mid")
    tools_only = _jsonl(tmp_path / "tools.jsonl", [rows[0], *rows[8:10]])
    assert claude_transcripts.newest_fork(tools_only, turn_finished=False) is None


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
    # A Work's side chat (#4199): the newest completed turn's last message, even mid-turn.
    assert codex_transcripts.newest_fork_seq(path) == 9
    assert codex_transcripts.newest_fork_seq(running) == 5
    # A Work's start (#4314): the first turn is running and has replied, but Codex cannot cut
    # a live turn, so there is no cut and the Work's side chat starts empty.
    unanswered = _jsonl(tmp_path / "e.jsonl", _codex_rows(finish_second=False)[:4])
    assert codex_transcripts.newest_fork_seq(unanswered) is None


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
    # The side chat's guide points at its own shell's token, never the source's: its
    # copied history already holds the source's key (#2576).
    tokened = host._command(None, fork_from=fork, session_token="side-token")
    guide = tokened[tokened.index("--append-system-prompt") + 1]
    assert "agentdeck session-key $AGENTDECK_SESSION_TOKEN" in guide
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
        session_token="side-token",
    )
    await settle()
    assert result.accepted
    assert result.session_id == "sid-auto-1"
    assert len(spawned) == 1
    assert spawned[0]["kwargs"]["env"]["AGENTDECK_SESSION_TOKEN"] == "side-token"
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
        # The message box's side chat (#4313) does the same.
        _box_side_chat(monkeypatch, app)
        with _pytest.raises(RuntimeError):
            await client.post(
                "/sessions/codex:test:sid/side-chat",
                data={"message": "hi"},
                headers={"origin": "http://test", "x-agentdeck-action-id": "press-0004"},
            )
    assert len(released) == 2
    assert state.reserve_chat_fork("fresh", "codex:test:sid", 7, "press-0003") == "fresh"
    assert state.side_chat_forks(["codex:test:sid"]) == []
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


# --- the message box's side chat (#4313) ---------------------------------------


def test_claude_side_chat_copy_ends_at_the_cut(tmp_path):
    path = _jsonl(
        tmp_path / "side.jsonl",
        [_prompt("first"), _assistant("a-end", _text("done")), _prompt("side q")],
    )
    assert claude_transcripts.fork_copy_end(path, "a-end") == 2
    assert claude_transcripts.fork_copy_end(path, "missing") == 0
    assert claude_transcripts.fork_copy_end(tmp_path / "gone.jsonl", "a-end") == 0


def _box_side_chat(monkeypatch, app, newest=(7, "turn-3")):
    """The codex chat's newest finished turn, and a record of sends to a live side chat."""
    from agentdeck.providers import PROVIDERS

    codex = PROVIDERS["codex"]
    turn = {"newest": newest}
    monkeypatch.setattr(codex, "newest_fork", lambda account, session: turn["newest"])
    sends = []

    async def start(account, session, provider, message, images, **kwargs):
        sends.append({"key": session.key, "message": message, **kwargs})
        return InjectResult(True)

    monkeypatch.setattr(app.state.injector, "start", start)
    return turn, sends


def _live_side(app, token):
    state = app.state.app_state
    state.update_session(
        Session(
            "codex:test:side",
            "codex:test",
            "side",
            SessionStatus.IDLE,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    state.bind_session_token(token, "codex:test:side")


async def test_box_side_chat_starts_once_then_takes_every_later_message(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    turn, sends = _box_side_chat(monkeypatch, app)
    state = app.state.app_state

    def post(client, action, text):
        return client.post(
            "/sessions/codex:test:sid/side-chat",
            data={"message": text},
            headers={"origin": "http://test", "x-agentdeck-action-id": action},
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await post(client, "", "no id")).status_code == 422
        first = await post(client, "side-0001", "what about X?")
        assert first.status_code == 202
        assert "data-side-view-body" in first.text
        for _ in range(20):
            await settle()
            if starts:
                break
        # The same press again starts nothing more.
        assert (await post(client, "side-0001", "what about X?")).status_code == 202
        await settle()
        assert len(starts) == 1
        start = starts[0]
        assert start["fork_from"] == ForkFrom("sid", "turn-3")
        # Told it is the side chat, so it does not take over the chat's work (#4556).
        brief, words = start["message"].split("\n\n", 1)
        assert words == "what about X?"
        assert "side chat of chat codex:test:sid" in brief
        assert "do not carry on its work" in brief
        assert "chat-read codex:test:sid" in brief
        [fork] = state.side_chat_forks(["codex:test:sid"])
        assert (fork.cut_ref, fork.seen_seq) == ("turn-3", 7)
        # Until the side chat is up, another message waits rather than starting a second.
        early = await post(client, "side-0002", "and Y?")
        assert early.status_code == 409
        assert "still starting" in early.json()["detail"]
        _live_side(app, start["session_token"])
        second = await post(client, "side-0003", "and Y?")
        assert second.status_code == 202
        # The main chat has not moved on, so the message goes as it is.
        assert [send["message"] for send in sends] == ["and Y?"]
        assert sends[0]["key"] == "codex:test:side"
        turn["newest"] = (11, "turn-5")
        assert (await post(client, "side-0004", "and Z?")).status_code == 202
        moved = sends[1]["message"]
        assert "moved on" in moved
        assert "chat-read codex:test:sid --after " in moved
        assert moved.endswith("\n\nand Z?")
        # Having said so once, it does not repeat it.
        assert (await post(client, "side-0005", "ok")).status_code == 202
        assert sends[2]["message"] == "ok"
        # #4562: the page's own send counts as the person writing to the side chat.
        side_chat = state.chat_identity_key("codex:test:side")
        assert state.person_message_at([side_chat]) is None
        marked = await client.post(
            "/sessions/codex:test:sid/side-chat",
            data={"message": "one more"},
            headers={
                "origin": "http://test", "x-agentdeck-action-id": "side-0006", "hx-request": "true"
            },
        )
        assert marked.status_code == 202
        assert state.person_message_at([side_chat]) is not None
        # A send that may have written answers 5xx, so the page keeps its action id for a
        # resend; one known to have written nothing is a plain refusal.
        for outcome, status in (
            (InjectResult(False, "unsure", write_uncertain=True), 503),
            (InjectResult(False, "down", runtime_unreachable=True), 503),
            (InjectResult(False, "full"), 409),
        ):

            async def answer(*args, outcome=outcome, **kwargs):
                return outcome

            monkeypatch.setattr(app.state.injector, "start", answer)
            refused = await post(client, f"side-{status}-{outcome.reason}", "hm")
            assert (refused.status_code, refused.json()["detail"]) == (status, outcome.reason)
    assert len(starts) == 1
    await app.state.injector.stop()


async def test_box_side_chat_gives_a_secret_as_a_ticket_only(tmp_path, monkeypatch):
    """#4610: the value reaches neither the side chat nor the answer, whatever happens."""
    from agentdeck.session_secrets import IssuedSecret

    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    turn, sends = _box_side_chat(monkeypatch, app)
    issued, revoked = [], []

    async def issue(_socket, **kwargs):
        issued.append(kwargs)
        return IssuedSecret(ticket="one-use", name=kwargs["name"])

    async def revoke(_socket, ticket):
        revoked.append(ticket)

    monkeypatch.setattr("agentdeck.web.routes_actions.issue_runtime_secret", issue)
    monkeypatch.setattr("agentdeck.web.routes_actions.revoke_runtime_secret", revoke)
    value = "hunter2-value"

    def post(client, action, text, **kwargs):
        return client.post(
            "/sessions/codex:test:sid/side-chat",
            data={"message": text},
            headers={"origin": "http://test", "x-agentdeck-action-id": action},
            **kwargs,
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # With no side chat yet there is nobody to give it to, and none is started.
        early = await post(client, "secret-0001", f"/secret TOKEN {value}")
        assert early.status_code == 409 and value not in early.text
        await settle()
        assert (starts, sends, issued) == ([], [], [])
        assert app.state.app_state.side_chat_forks(["codex:test:sid"]) == []
        assert (await post(client, "side-0001", "hi")).status_code == 202
        for _ in range(20):
            await settle()
            if starts:
                break
        _live_side(app, starts[0]["session_token"])
        turn["newest"] = (11, "turn-5")
        sent = await post(client, "secret-0002", f"  /secret TOKEN {value}")
        assert sent.status_code == 202 and value not in sent.text
        assert [(i["session_key"], i["action_id"], i["name"], i["value"]) for i in issued] == [
            ("codex:test:side", "secret-0002", "TOKEN", value)
        ]
        assert len(sends) == 1
        assert "moved on" in sends[0]["message"]
        assert "secret-run one-use" in sends[0]["message"]
        assert value not in sends[0]["message"]
        for text in ("/secret TOKEN", f"/secret PATH {value}"):
            bad = await post(client, "secret-0003", text)
            assert bad.status_code == 422 and value not in bad.text
        attached = await post(
            client, "secret-0004", f"/secret TOKEN {value}",
            files={"images": ("a.txt", b"notes", "text/plain")},
        )
        assert attached.status_code == 422 and "attachments" in attached.text
        assert len(sends) == 1
        # A refused send takes its ticket back; one that may have written keeps it.
        for outcome, status, gone in (
            (InjectResult(False, "unsure", write_uncertain=True), 503, []),
            (InjectResult(False, "down", runtime_unreachable=True), 503, []),
            (InjectResult(False, "full"), 409, ["one-use"]),
        ):

            async def answer(*args, outcome=outcome, **kwargs):
                return outcome

            monkeypatch.setattr(app.state.injector, "start", answer)
            refused = await post(client, f"secret-{outcome.reason}", f"/secret TOKEN {value}")
            assert refused.status_code == status and value not in refused.text
            assert revoked == gone
    await app.state.injector.stop()


async def test_box_side_chat_that_is_gone_is_replaced(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    _box_side_chat(monkeypatch, app)
    state = app.state.app_state
    state.reserve_chat_fork("old", "codex:test:sid", 3, "old-press", kind="side", cut_ref="t1")
    state.bind_session_token("old", "codex:test:vanished")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        view = await client.get("/partials/sessions/codex:test:sid/side-chat")
        assert 'data-side-state="none"' in view.text
        response = await client.post(
            "/sessions/codex:test:sid/side-chat",
            data={"message": "again"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "side-0006"},
        )
        assert response.status_code == 202
        for _ in range(20):
            await settle()
            if starts:
                break
    assert len(starts) == 1
    assert [fork.cut_ref for fork in state.side_chat_forks(["codex:test:sid"])] == ["turn-3", "t1"]
    await app.state.injector.stop()


async def test_box_side_chat_needs_a_finished_turn(tmp_path, monkeypatch):
    app = _app(tmp_path)
    starts = _forkable(monkeypatch)
    _box_side_chat(monkeypatch, app, newest=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/side-chat",
            data={"message": "hi"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "side-0007"},
        )
    assert response.status_code == 409
    assert "no finished reply" in response.json()["detail"]
    assert starts == []
    assert app.state.app_state.side_chat_forks(["codex:test:sid"]) == []
    await app.state.injector.stop()


@pytest.mark.browser
async def test_box_side_chat_takes_the_chats_place_with_its_own_draft(tmp_path, monkeypatch):
    """The box's switch (#4313): the side chat's own talk in the chat's place, a draft of
    its own, a send that never lands in the main chat, a dot, and the summary sent back."""
    from agentdeck.providers import PROVIDERS

    claude = PROVIDERS["claude_code"]
    monkeypatch.setattr(claude, "fork_refusal", lambda account, session: None)
    app = _claude_app_with_turns(tmp_path)
    copied = (tmp_path / "projects" / "-tmp" / "sid1.jsonl").read_text()
    side_file = tmp_path / "projects" / "-tmp" / "sid3.jsonl"
    side_file.write_text(copied)
    with side_file.open("a") as out:
        out.write(json.dumps(_prompt("side question")) + "\n")
        out.write(json.dumps(_assistant("s-end", _text("side answer"))) + "\n")
    state = app.state.app_state
    state.update_session(
        replace(
            state.sessions["claude_code:test:sid1"],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:sid3",
            account_key="claude_code:test",
            session_id="sid3",
            status=SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    app.state.injector.can_queue = lambda session_key: True
    state.reserve_chat_fork("tok", "claude_code:test:sid1", 7, None, kind="side", cut_ref="b-end")
    state.bind_session_token("tok", "claude_code:test:sid3")
    monkeypatch.setattr(claude, "newest_fork", lambda account, session: (7, "b-end"))
    sends = []
    gate = asyncio.Event()
    gate.set()

    async def start(account, session, provider, message, images, **kwargs):
        await gate.wait()
        sends.append((session.key, message))
        if message.startswith("refuse me"):
            return InjectResult(False, "No room for it.")
        return InjectResult(True)

    monkeypatch.setattr(app.state.injector, "start", start)
    async with _served(app) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            posts = []
            page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
            await page.goto(f"{base}/sessions/claude_code:test:sid1")
            await page.wait_for_function("() => !!window.agentdeckSidePanel")
            box = page.locator("#inject-message")
            side = page.locator("[data-side-view]")
            transcript = page.locator(".session-detail .transcript")
            assert await side.is_hidden()
            await box.fill("for the main chat")
            await page.locator('[data-side-mode="side"]').click()
            await page.wait_for_function(
                "() => document.querySelector('[data-side-view-log]')"
            )
            # The side chat's own talk only, in the chat's place.
            assert await side.is_visible()
            assert await transcript.is_hidden()
            log = await page.locator("[data-side-view-log]").inner_text()
            assert "side answer" in log and "second answer" not in log
            assert await box.input_value() == ""
            assert await box.get_attribute("placeholder") == "Ask the side chat…"
            await box.fill("for the side chat")
            await page.locator("[data-side-back]").click()
            assert await transcript.is_visible()
            assert await box.input_value() == "for the main chat"
            await page.locator('[data-side-mode="side"]').click()
            assert await box.input_value() == "for the side chat"
            # Sent to the side chat only: no main send, and no bubble in the main chat.
            await box.press("Enter")
            await page.wait_for_function(
                "() => !document.querySelector('[data-side-view]').dataset.sending"
            )
            assert sends == [("claude_code:test:sid3", "for the side chat")]
            assert not [url for url in posts if url.endswith("/inject")]
            assert "for the side chat" not in await transcript.inner_text()
            assert await box.input_value() == ""
            # Taken but still queued: the message stays shown until its line lands.
            pending = page.locator(".side-view-pending")
            assert await pending.inner_text() == "for the side chat"
            sent_id = await pending.get_attribute("data-client-action-id")
            await page.evaluate(
                """id => document.querySelector('[data-side-view-log]').dispatchEvent(
                     new CustomEvent('htmx:sseMessage', {bubbles: true, detail: {data:
                       `<div class="ev user" data-event-seq="19" data-event-id="19"
                        data-observed-action-id="${id}">
                        <div class="ev-text">sent</div></div>`}}))""",
                sent_id,
            )
            assert await pending.count() == 0
            # A secret shows no value, and one that failed comes back nowhere (#4610).
            async def refuse_secret(route):
                await route.fulfill(status=503, json={"detail": "unsure"})

            await page.route("**/side-chat", refuse_secret)
            await page.evaluate(
                """() => {
                  window.seenCards = [];
                  new MutationObserver(() => document.querySelectorAll('.side-view-pending')
                    .forEach(card => window.seenCards.push(card.textContent)))
                    .observe(document.body, {childList: true, subtree: true});
                }"""
            )
            await box.fill("/secret TOKEN hunter2")
            await box.press("Enter")
            error = page.locator("[data-side-error]")
            await error.filter(has_text="Re-enter the secret").wait_for()
            assert await box.input_value() == ""
            assert "hunter2" not in await page.evaluate("JSON.stringify({...sessionStorage})")
            assert set(await page.evaluate("window.seenCards")) == {
                "Temporary secret TOKEN submitted securely"
            }
            assert await page.evaluate(
                "!document.querySelector('[data-side-view]')._uncertainSend"
            )
            # One that got through leaves its card until the notice with its name lands.
            await page.unroute("**/side-chat", refuse_secret)
            live = await page.locator("[data-side-view-body]").evaluate("el => el.outerHTML")

            async def take_secret(route):
                await route.fulfill(status=202, content_type="text/html", body=live)

            await page.route("**/side-chat", take_secret)
            await box.fill("/secret TOKEN hunter2")
            await box.press("Enter")
            await page.wait_for_function(
                "() => !document.querySelector('[data-side-view]').dataset.sending"
            )
            assert await pending.count() == 1
            await page.evaluate(
                """notice => {
                  const line = document.createElement('div');
                  line.className = 'ev user';
                  line.dataset.eventSeq = line.dataset.eventId = '99';
                  line.innerHTML = '<div class="ev-text"></div>';
                  line.firstChild.textContent = notice;
                  document.querySelector('[data-side-view-log]').dispatchEvent(
                    new CustomEvent('htmx:sseMessage',
                      {bubbles: true, detail: {data: line.outerHTML}}));
                }""",
                secret_notice("TOKEN", "ticket"),
            )
            await pending.first.wait_for(state="detached")
            await page.unroute("**/side-chat", take_secret)
            await page.route("**/side-chat", refuse_secret)
            await box.fill("/secret TOKEN hunter2")
            await box.press("Enter")
            await error.filter(has_text="Re-enter the secret").wait_for()
            assert await error.inner_text() == "unsure. Re-enter the secret to try again."
            # A failed ordinary send is not put before a secret typed meanwhile.
            await page.evaluate(
                """() => {
                  const plain = window.fetch;
                  window.fetch = (...args) => new Promise((done, fail) => {
                    window.loseSend = () => { window.fetch = plain; fail(new Error('lost')); };
                  });
                }"""
            )
            await box.fill("plain words")
            await box.press("Enter")
            await box.fill("/secret TOKEN hunter2")
            await page.evaluate("window.loseSend()")
            await error.filter(has_text="Not sent: plain words").wait_for()
            assert await box.input_value() == "/secret TOKEN hunter2"
            await page.unroute("**/side-chat", refuse_secret)
            await box.fill("")
            # A lost answer is an uncertain write: sending the same words again reuses the
            # action id, so the deck can deliver it once.
            ids = []
            lost = []

            async def lose_first(route, request):
                ids.append(request.headers["x-agentdeck-action-id"])
                if lost:
                    await route.continue_()
                    return
                lost.append(await route.fetch())
                await route.abort()

            await page.route("**/side-chat", lose_first)
            await box.fill("again")
            await box.press("Enter")
            await page.wait_for_function(
                "() => !document.querySelector('[data-side-view]').dataset.sending"
            )
            assert await box.input_value() == "again"
            assert await page.locator("[data-side-error]").is_visible()
            await box.press("Enter")
            await page.wait_for_function(
                "() => !document.querySelector('[data-side-view]').dataset.sending"
            )
            assert len(ids) == 2 and ids[0] == ids[1] and ids[0] != sent_id
            await page.unroute("**/side-chat")
            files = 'form.inject-form input[type="file"][name="images"]'
            names = "el => [...el.files].map(f => f.name)"
            sending_done = "() => !document.querySelector('[data-side-view]').dataset.sending"

            async def attach(name):
                await page.set_input_files(
                    files, files=[{"name": name, "mimeType": "text/plain", "buffer": b"hi"}]
                )

            # Each mode keeps its own files: one picked on Main never rides a side send.
            await page.locator('[data-side-mode="main"]').click()
            await attach("main.txt")
            await page.locator('[data-side-mode="side"]').click()
            assert await page.eval_on_selector(files, names) == []
            await page.locator('[data-side-mode="main"]').click()
            assert await page.eval_on_selector(files, names) == ["main.txt"]
            await page.locator('[data-side-mode="side"]').click()
            # A refusal goes back ahead of what was typed and attached since, losing neither.
            gate.clear()
            await box.fill("refuse me")
            await attach("note.txt")
            await box.press("Enter")
            await box.fill("newer")
            await attach("newer.txt")
            gate.set()
            await page.wait_for_function(sending_done)
            assert await box.input_value() == "refuse me\n\nnewer"
            assert await page.eval_on_selector(files, names) == ["note.txt", "newer.txt"]
            # And by the same rule into the side draft when Main is shown by then.
            gate.clear()
            await box.press("Enter")
            await page.locator('[data-side-mode="main"]').click()
            gate.set()
            await page.wait_for_function(sending_done)
            assert await box.input_value() == "for the main chat"
            assert await page.eval_on_selector(files, names) == ["main.txt"]
            await page.locator('[data-side-mode="side"]').click()
            assert await box.input_value() == "refuse me\n\nnewer"
            assert await page.eval_on_selector(files, names) == ["note.txt", "newer.txt"]
            await box.fill("")
            await page.eval_on_selector(files, "el => { el.value = ''; }")
            # Two equal sends: an older line with the same words answers neither, and one
            # line answers one card, so the second stays until its own line lands.
            land = """([id, words]) => {
              const seq = String(id).split('.').pop();
              const html = `<div class="ev user" data-event-seq="${seq}" data-event-id="${id}">
                <div class="ev-text">${words}</div></div>`;
              const log = document.querySelector('[data-side-view-log]');
              log.insertAdjacentHTML('beforeend', html);
              log.dispatchEvent(new CustomEvent('htmx:sseMessage',
                {bubbles: true, detail: {data: html}}));
            }"""
            pending = page.locator(".side-view-pending")
            await box.fill("ok")
            await box.press("Enter")
            await page.wait_for_function(sending_done)
            assert await pending.count() == 1
            await page.evaluate(land, [40, "ok"])
            assert await pending.count() == 0
            await box.fill("ok")
            await box.press("Enter")
            await page.wait_for_function(sending_done)
            assert await pending.count() == 1
            await page.evaluate(land, [41, "other"])
            assert await pending.count() == 1
            await page.evaluate(land, [42, "ok"])
            assert await pending.count() == 0
            # The side chat goes on in a new leg, whose own numbers start again: its line
            # 40 is not the first leg's line 40, and it comes after line 42.
            await box.fill("ok")
            await box.press("Enter")
            await page.wait_for_function(sending_done)
            assert await pending.count() == 1
            await page.evaluate(land, ["1.40", "ok"])
            assert await pending.count() == 0
            await page.locator('[data-side-mode="main"]').click()
            await page.locator('[data-side-mode="side"]').click()
            # A side reply that lands while the chat is shown lights the dot.
            await page.locator('[data-side-mode="main"]').click()
            dot = page.locator("[data-side-dot]")
            assert await dot.is_hidden()
            await page.evaluate(
                """() => document.querySelector('[data-side-view-log]').dispatchEvent(
                     new CustomEvent('htmx:sseMessage', {bubbles: true, detail: {
                       data: '<div class="ev assistant" data-event-seq="20"></div>'}}))"""
            )
            assert await dot.is_visible()
            await page.locator('[data-side-mode="side"]').click()
            assert await dot.is_hidden()
            # The summary goes into the main chat's box, after what was waiting there.
            await page.locator("[data-side-send-back]").click()
            assert await transcript.is_visible()
            draft = await box.input_value()
            assert draft.startswith("for the main chat\n\n")
            assert "Summary from side chat “Side chat” (for information):" in draft
            assert draft.endswith("side answer")
            # The mode is kept for this tab. The reload also needs every send's replaced
            # side stream closed, or the browser has no connection left for the page.
            await page.locator('[data-side-mode="side"]').click()
            await page.reload()
            await page.wait_for_function("() => !!window.agentdeckSidePanel")
            assert await page.locator("[data-side-view]").is_visible()
            await browser.close()


# Where the browser cannot size a box to its text, the page's own fallback must (#4330).
_NO_FIELD_SIZING = """
const supports = CSS.supports.bind(CSS);
CSS.supports = (...args) => (String(args[0]).includes('field-sizing') ? false : supports(...args));
"""


@pytest.mark.browser
@pytest.mark.parametrize("field_sizing", [True, False], ids=["css", "fallback"])
async def test_the_box_is_as_tall_as_its_text_after_the_page_changes_it(
    tmp_path, monkeypatch, field_sizing
):
    """The box only grew on typing (#4330): a send empties it from code, so it stayed as
    tall as the message that left. A send, a quick phrase and a width change all refit it."""
    app = _claude_app_with_turns(tmp_path)
    state = app.state.app_state
    state.update_session(
        replace(
            state.sessions["claude_code:test:sid1"],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    app.state.injector.can_queue = lambda session_key: True

    async def start(account, session, provider, message, images, **kwargs):
        return InjectResult(True)

    monkeypatch.setattr(app.state.injector, "start", start)
    async with _served(app) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            if not field_sizing:
                await page.add_init_script(_NO_FIELD_SIZING)
            await page.goto(f"{base}/sessions/claude_code:test:sid1")
            await page.wait_for_function("() => !!window.agentdeckSidePanel")
            box = page.locator("#inject-message")

            async def height():
                return await box.evaluate("el => el.getBoundingClientRect().height")

            async def sent():
                await page.wait_for_function(
                    "() => !document.querySelector('#inject-message').value &&"
                    " !document.querySelector('form.inject-form[data-sending]')"
                )

            one_line = await height()
            await box.fill("one\ntwo\nthree")
            three_lines = await height()
            assert three_lines > one_line + 20
            # A send empties the box from code: it is one line again.
            await box.press("Enter")
            await sent()
            assert abs(await height() - one_line) < 1
            # A quick phrase fills an empty box and sends it at once.
            await page.locator("[data-quick-phrases] summary").click()
            await page.locator("[data-quick-phrase]").first.click()
            await sent()
            assert abs(await height() - one_line) < 1
            # Narrower, the same words wrap onto more lines; wider again, they do not.
            await box.fill("word " * 40)
            wide = await height()
            await page.set_viewport_size({"width": 300, "height": 844})
            await page.wait_for_function(
                "h => document.querySelector('#inject-message').getBoundingClientRect().height > h",
                arg=wide,
            )
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.wait_for_function(
                "h => Math.abs(document.querySelector('#inject-message')"
                ".getBoundingClientRect().height - h) < 1",
                arg=wide,
            )
            await browser.close()
