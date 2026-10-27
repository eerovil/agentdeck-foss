"""Roll a chat back to before one of the person's messages (#3474)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from worker_harness import claude_host, settle

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.models import Capability, InjectResult, Session, SessionStatus, TranscriptEvent
from agentdeck.providers import PROVIDERS
from agentdeck.providers.claude_code import transcripts as claude_transcripts
from agentdeck.transcript_rollbacks import HIDDEN
from agentdeck.wake_on import WakeOnRegistry, WakeRequest


def _line(kind: str, uuid: str, parent: str | None, content) -> dict:
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "message": {"role": kind, "content": content},
    }


def _transcript(path: Path) -> Path:
    rows = [
        _line("user", "u1", None, "remember APPLE"),
        _line("assistant", "r1", "u1", [{"type": "text", "text": "OK"}]),
        {"type": "attachment", "uuid": "a1", "parentUuid": "r1"},
        {"type": "queue-operation"},
        _line("user", "u2", "a1", "remember BANANA"),
        _line("assistant", "r2", "u2", [{"type": "tool_use", "id": "t", "name": "Bash"}]),
        _line("user", "u3", "r2", [{"type": "tool_result", "content": "ok"}]),
        _line("assistant", "r3", "u3", [{"type": "text", "text": "OK"}]),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def test_a_rollback_resumes_at_the_message_the_prompt_hangs_off(tmp_path):
    path = _transcript(tmp_path / "chat.jsonl")
    # Past the attachment and the queue line, to the reply before the prompt.
    assert claude_transcripts.rewind_ref(path, 5) == "r1"
    # Not the first prompt (nothing before it), a tool result, a reply, or no line.
    assert claude_transcripts.rewind_ref(path, 1) is None
    assert claude_transcripts.rewind_ref(path, 7) is None
    assert claude_transcripts.rewind_ref(path, 2) is None
    assert claude_transcripts.rewind_ref(path, 99) is None
    # The button is hidden on that first prompt.
    assert claude_transcripts.first_prompt_seq(path) == 1


async def test_the_next_turn_resumes_the_same_session_at_the_cut_once(tmp_path):
    host, spawned = claude_host(tmp_path)
    await host.deliver("chat-1", "hi", cwd=str(tmp_path), fresh=True, delegated=False)
    await settle()
    # Mid-turn the person stops first; nothing changes.
    refused = await host.rewind_worker("chat-1", "r1")
    assert (refused.accepted, refused.reason) == (False, "worker_busy")
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await settle()

    rewound = await host.rewind_worker("chat-1", "r1")
    assert rewound.accepted
    assert spawned[0]["process"].returncode is not None
    await host.deliver("chat-1", "try again", delegated=False)
    await settle()
    args = list(spawned[1]["args"])
    assert args[args.index("--resume") + 1 : args.index("--resume") + 4] == [
        "sid-auto-1",
        "--resume-session-at",
        "r1",
    ]
    assert host._records["chat-1"].resume_at is None
    # Questions asked after the cut close with it; one asked before stays open.
    spawned[1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await settle()
    record = host._records["chat-1"]
    record.open_questions = [
        {"id": "old.0", "request_id": "old", "asked_at": 100.0},
        {"id": "new.0", "request_id": "new", "asked_at": 300.0},
    ]
    assert (await host.rewind_worker("chat-1", "r1", since=200.0)).accepted
    assert [item["id"] for item in record.open_questions] == ["old.0"]
    # A later revive follows the branch the CLI wrote, with a plain resume.
    assert "--resume-session-at" not in host._command("sid-auto-1")
    # A cut is only ever used on the session it was made in.
    record = host._records["chat-1"]
    record.resume_at, record.resume_at_session = "r1", "another-sid"
    _env, _unit, _prefix, command = await host._spawn_plan("chat-1", record, resume_id="sid-auto-1")
    assert "--resume-session-at" not in command
    await host.stop()


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            "claude_code:test:sid",
            "claude_code:test",
            "sid",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


def _rewindable(monkeypatch, *, sent_at: float):
    claude = PROVIDERS["claude_code"]
    rewinds = []

    async def read_transcript(account, session, after_seq=0):
        sent = datetime.fromtimestamp(sent_at, UTC)
        return [TranscriptEvent(5, "user", "remember BANANA", ts=sent)]

    async def rewind(account, session, at, *, chat_keys=(), since=None):
        rewinds.append((session.key, at, tuple(chat_keys), since))
        await asyncio.sleep(0.01)  # a press arriving meanwhile must wait for this one
        rewinds.append("done")
        return InjectResult(True)

    async def cursor(account, session):
        return (0, 8)

    monkeypatch.setattr(claude, "rewind_refusal", lambda account, session: None)
    monkeypatch.setattr(claude, "rewind_ref", lambda account, session, seq: "r1")
    monkeypatch.setattr(claude, "read_transcript", read_transcript)
    monkeypatch.setattr(claude, "rewind", rewind)
    monkeypatch.setattr(claude, "transcript_cursor", cursor)
    return rewinds


def _wait(chat_id: str, wait_id: str, registered_at: float) -> WakeRequest:
    return WakeRequest(
        wait_id,
        "claude_code:test:sid",
        chat_id,
        "time",
        "",
        registered_at=registered_at,
        note=wait_id,
    )


async def test_rollback_hides_the_rest_and_cancels_what_the_agent_forgot(tmp_path, monkeypatch):
    app = _app(tmp_path)
    sent_at = float(int(time.time()) - 60)  # whole seconds survive the datetime round trip
    rewinds = _rewindable(monkeypatch, sent_at=sent_at)
    registry = WakeOnRegistry(tmp_path / "wake-on.json")
    chat_id = app.state.app_state.chat_identity_key("claude_code:test:sid")
    registry.add(_wait(chat_id, "before", sent_at - 1))
    registry.add(_wait(chat_id, "after", sent_at + 1))
    app.state.wake_on = SimpleNamespace(registry=registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/claude_code:test:sid/rollback",
            data={"seq": "5"},
            headers={"origin": "http://test"},
        )
    assert response.status_code == 200
    assert response.json() == {"text": "remember BANANA", "cancelled_waits": 1}
    # The runtime cancels the jobs from the removed part in the same call that rolls back.
    assert rewinds == [("claude_code:test:sid", "r1", ("claude_code:test:sid",), sent_at), "done"]
    assert [item.id for item in registry.all()] == ["before"]
    # Lines 5..8 leave the chat, now and after a restart.
    assert [HIDDEN.hides("claude_code:test:sid", seq) for seq in (4, 5, 8, 9)] == [
        False,
        True,
        True,
        False,
    ]
    assert app.state.app_state.budget_db.load_transcript_rollbacks() == [
        ("claude_code:test:sid", 5, 8)
    ]
    await app.state.injector.stop()


async def test_rollback_refusals_say_why(tmp_path, monkeypatch):
    app = _app(tmp_path)
    _rewindable(monkeypatch, sent_at=time.time())
    claude = PROVIDERS["claude_code"]
    state = app.state.app_state
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:

        async def post(seq="5"):
            return await client.post(
                "/sessions/claude_code:test:sid/rollback",
                data={"seq": seq},
                headers={"origin": "http://test"},
            )

        working = state.sessions["claude_code:test:sid"]
        state.update_session(Session(**{**vars(working), "thinking": True}))
        busy = await post()
        assert (busy.status_code, busy.json()["detail"]) == (
            409,
            "The agent is working. Stop it first, then roll back.",
        )
        state.update_session(working)

        async def unreachable(account, session, at, **kwargs):
            return InjectResult(False, "Claude worker runtime unavailable")

        monkeypatch.setattr(claude, "rewind", unreachable)
        down = await post()
        assert down.status_code == 409
        assert "unavailable" in down.json()["detail"]
        monkeypatch.setattr(claude, "rewind", lambda *args, **kwargs: None)

        async def deck_sent(account, session, after_seq=0):
            return [TranscriptEvent(5, "user", "AgentDeck event: the reviewer posted")]

        monkeypatch.setattr(claude, "read_transcript", deck_sent)
        deck = await post()
        assert deck.status_code == 409
        assert "Only your own messages" in deck.json()["detail"]
        monkeypatch.setattr(claude, "rewind_ref", lambda account, session, seq: None)
        first = await post("1")
        assert first.status_code == 409
        assert "not the chat's first one" in first.json()["detail"]
    assert not HIDDEN.hides_any("claude_code:test:sid")
    await app.state.injector.stop()


async def test_a_claude_read_leaves_rolled_back_lines_out(tmp_path, monkeypatch):
    claude = PROVIDERS["claude_code"]
    path = _transcript(tmp_path / "chat.jsonl")
    monkeypatch.setattr(claude, "_transcript_path", lambda account, session: path)
    # The shared on-disk render cache is not what this is about, and warming it is slow.
    monkeypatch.setattr(
        claude,
        "_read_transcript_file",
        lambda path, after: [
            event for event in claude_transcripts.read_events(path).events if event.seq > after
        ],
    )
    session = Session("claude_code:test:sid", "claude_code:test", "sid", SessionStatus.IDLE)
    account = SimpleNamespace(key="claude_code:test", root=tmp_path)
    HIDDEN.replace_all([("claude_code:test:sid", 5, 8)])
    try:
        shown = [event.seq for event in await claude.read_transcript(account, session)]
        detail = await claude.load_transcript(account, session)
        last = await claude.last_event(account, session)
    finally:
        HIDDEN.replace_all([])
    assert shown and max(shown) < 5
    assert [event.seq for event in detail.events] == shown
    assert last is not None and last.seq == max(shown)


async def test_two_rollbacks_pressed_at_once_run_one_after_the_other(tmp_path, monkeypatch):
    """Overlapping rollbacks would each hide their part while the runtime kept one cut."""
    app = _app(tmp_path)
    rewinds = _rewindable(monkeypatch, sent_at=time.time())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        both = await asyncio.gather(
            *(
                client.post(
                    "/sessions/claude_code:test:sid/rollback",
                    data={"seq": "5"},
                    headers={"origin": "http://test"},
                )
                for _ in range(2)
            )
        )
    assert [response.status_code for response in both] == [200, 200]
    # The second rollback began only once the first had finished.
    assert [entry == "done" for entry in rewinds] == [False, True, False, True]
    await app.state.injector.stop()
