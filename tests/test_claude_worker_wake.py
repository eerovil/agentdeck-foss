"""Voice mode starts a chat's Claude process ahead of its phrase (#4360)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import HTTPException
from worker_harness import FakeProcess
from worker_harness import claude_host as _host
from worker_harness import settle as _settle

from agentdeck import voice_helper
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.models import Account, Capability, Session, SessionStatus
from agentdeck.providers import PROVIDERS
from agentdeck.providers.claude_code import locked
from agentdeck.providers.claude_code.worker import (
    BYPASS_PERMISSIONS_MODE,
    IDLE_RECLAIM_GRACE_S,
    SESSION_TOKEN_ENV,
    VOICE_HOLD_S,
    ClaudeWorkerHost,
)
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
from agentdeck.runtime import create_runtime_app
from agentdeck.web import routes_speech


async def _resting(host, spawned, key, tmp_path, **kwargs):
    """A chat that ran a turn and whose process then went, as after a reclaim."""
    kwargs.setdefault("permission_mode", "default")
    await host.deliver(key, "go", cwd=str(tmp_path), fresh=True, delegated=False, **kwargs)
    await _settle()
    process = spawned[-1]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    process.terminate()
    await _settle()
    assert key not in host._live
    return host._records[key]


async def _idle(host, spawned, key, tmp_path):
    """A chat whose process is live, idle, and out of its delivery grace."""
    await host.deliver(key, "go", cwd=str(tmp_path), fresh=True, delegated=False)
    await _settle()
    spawned[-1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    host._records[key].last_delivery_at -= IDLE_RECLAIM_GRACE_S + 1


def _arg(args, flag):
    return args[args.index(flag) + 1]


async def test_a_wake_resumes_a_resting_chat_with_its_own_settings_and_writes_nothing(tmp_path):
    host, spawned = _host(tmp_path, permission_mode="bypassPermissions")
    rec = await _resting(
        host, spawned, "chat-v", tmp_path, permission_mode="plan", effort="high", model="opus"
    )
    before = (rec.last_delivery_at, rec.last_result_at, rec.session_id)

    assert await host.wake("chat-v") == "spawned"
    args = spawned[1]["args"]
    assert _arg(args, "--resume") == before[2]
    assert _arg(args, "--permission-mode") == "plan"
    assert _arg(args, "--effort") == "high"
    assert _arg(args, "--model") == "opus"
    # Nothing goes into the process, and the record does not say a message came: a
    # woken chat that is never spoken to owes nobody a revival.
    assert spawned[1]["process"].stdin.lines == []
    assert (rec.last_delivery_at, rec.last_result_at, rec.session_id) == before
    woken_at = host._live["chat-v"].woken_at
    assert woken_at > 0

    # Later wakes of the live process change nothing, and the phrase steers into it.
    assert await host.wake("chat-v") == "live"
    assert host._live["chat-v"].woken_at == woken_at
    sent = await host.deliver("chat-v", "hello", cwd=str(tmp_path), delegated=False)
    assert sent.accepted and sent.action != "spawned"
    assert len(spawned) == 2
    await host.stop()


async def test_a_bypass_chat_wakes_in_bypass_and_its_mode_is_kept(tmp_path):
    host, spawned = _host(tmp_path)
    rec = await _resting(host, spawned, "chat-b", tmp_path, permission_mode="acceptEdits")
    rec.bypass_permissions = True

    assert await host.wake("chat-b") == "spawned"
    assert _arg(spawned[1]["args"], "--permission-mode") == BYPASS_PERMISSIONS_MODE
    assert rec.permission_mode == "acceptEdits"
    await host.stop()


async def test_a_revive_naming_a_model_spawns_with_it_and_records_it(tmp_path):
    host, spawned = _host(tmp_path)
    rec = await _resting(host, spawned, "chat-m", tmp_path, model="sonnet", effort="low")

    await host.deliver("chat-m", "again", cwd=str(tmp_path), model="opus", delegated=False)
    await _settle()
    args = spawned[1]["args"]
    assert "--resume" in args
    assert (_arg(args, "--model"), _arg(args, "--effort")) == ("opus", "low")
    assert rec.model == "opus"
    await host.stop()


async def test_a_wake_answers_what_it_did_and_starts_nothing_it_should_not(tmp_path):
    host, spawned = _host(tmp_path)
    assert await host.wake("nobody") == "no_record"

    cases = {
        "rewound": lambda rec: setattr(rec, "resume_at", "uuid-1"),
        "rolling": lambda rec: setattr(rec, "pending_context_operation", "rollover"),
        "rolling-id": lambda rec: setattr(rec, "pending_context_delivery_id", "d-1"),
        "cut-off": lambda rec: setattr(rec, "turn_open_at", 1.0),
        "no-mode": lambda rec: setattr(rec, "permission_mode", None),
        "no-token": lambda rec: setattr(rec, "session_token", None),
        "enforced": lambda rec: setattr(rec, "command_artifacts", {"mode": "enforce"}),
    }
    for key, change in cases.items():
        change(await _resting(host, spawned, key, tmp_path))
    count = len(spawned)

    answers = {key: await host.wake(key) for key in cases}
    assert answers == dict.fromkeys(cases, "not_plain")
    assert len(spawned) == count
    assert not host._live
    # An older record with no token is left to the send, which gives it one (#4360).
    sent = await host.deliver("no-token", "hi", cwd=str(tmp_path), delegated=False)
    token = host._records["no-token"].session_token
    assert sent.accepted and token
    assert spawned[-1]["kwargs"]["env"][SESSION_TOKEN_ENV] == token
    await host.stop()


async def test_a_kiosk_locked_chat_wakes_within_its_own_limits_checked_again(tmp_path):
    folder = tmp_path / "kitchen"
    folder.mkdir()
    (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"keep": {"command": "true"}}}))
    (folder / "CLAUDE.md").write_text("Guide")
    host, spawned = _host(tmp_path, permission_mode="bypassPermissions")
    # Started as the kiosk starts it: locked, a token, a model, and no permission mode.
    await host.deliver(
        "kitchen",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role=locked.LOCKED_ROLE,
        model="haiku",
        delegated=False,
    )
    await _settle()
    spawned[-1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    spawned[-1]["process"].terminate()
    await _settle()
    rec = host._records["kitchen"]
    assert rec.permission_mode is None
    rec.bypass_permissions = True  # never set for a locked chat; must not loosen it anyway
    before = (rec.last_delivery_at, rec.last_result_at, rec.session_id)

    assert await host.wake("kitchen") == "spawned"
    args = [str(item) for item in spawned[1]["args"]]
    args = args[args.index("claude") :]
    assert _arg(args, "--resume") == before[2]
    assert _arg(args, "--permission-mode") == "dontAsk"
    assert _arg(args, "--tools") == "WebSearch"
    assert _arg(args, "--model") == "haiku"
    assert "--strict-mcp-config" in args
    assert not any("bypass" in item or "dangerously" in item for item in args)
    assert SESSION_TOKEN_ENV not in spawned[1]["kwargs"]["env"]
    assert spawned[1]["process"].stdin.lines == []
    assert (rec.last_delivery_at, rec.last_result_at, rec.session_id) == before

    # The phrase steers into the woken process.
    sent = await host.deliver("kitchen", "what's for dinner", cwd=str(folder), delegated=False)
    assert sent.accepted and sent.action != "spawned"
    assert len(spawned) == 2
    await host.stop()

    # A limit gone since the last spawn refuses the wake, as it refuses the send.
    host, spawned = _host(tmp_path / "again")
    (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"keep": {"command": "true"}}}))
    (folder / "CLAUDE.md").write_text("Guide")
    await host.deliver(
        "kitchen",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role=locked.LOCKED_ROLE,
        delegated=False,
    )
    await _settle()
    spawned[-1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    spawned[-1]["process"].terminate()
    await _settle()
    (folder / ".mcp.json").unlink()
    assert await host.wake("kitchen") == "spawn_failed"
    assert len(spawned) == 1 and "kitchen" not in host._live
    await host.stop()


async def test_a_turn_cut_off_by_a_restart_is_left_to_its_revival(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver(
        "chat-r", "go", cwd=str(tmp_path), fresh=True, delegated=False, permission_mode="default"
    )
    await _settle()
    spawned[0]["process"].terminate()  # dies mid-turn, as a restart cuts it
    await _settle()
    await _idle(host, spawned, "other", tmp_path)
    host._records["other"].last_delivery_at += IDLE_RECLAIM_GRACE_S + 1  # in its grace
    refused = await host.deliver(
        "chat-r", "you were cut off", cwd=str(tmp_path), delegated=False, automated=True
    )
    assert not refused.accepted and refused.reason == "at_capacity"

    assert await host.wake("chat-r") == "not_plain"
    host._records["other"].last_delivery_at -= IDLE_RECLAIM_GRACE_S + 1
    revived = await host.deliver(
        "chat-r", "you were cut off", cwd=str(tmp_path), delegated=False, automated=True
    )
    assert revived.accepted and revived.action == "revived"
    await host.stop()


async def test_a_wake_at_the_cap_reclaims_no_busy_chat(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await _resting(host, spawned, "chat-c", tmp_path)
    await host.deliver("busy", "go", cwd=str(tmp_path), fresh=True, delegated=False)
    await _settle()

    assert await host.wake("chat-c") == "at_capacity"
    assert "busy" in host._live and "chat-c" not in host._live
    await host.stop()


async def test_a_woken_process_keeps_a_delivery_grace_from_its_start_only(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await _resting(host, spawned, "chat-w", tmp_path)
    host._records["chat-w"].last_delivery_at -= IDLE_RECLAIM_GRACE_S + 1
    assert await host.wake("chat-w") == "spawned"

    # An automated delivery at the cap cannot take it while the phrase may still come,
    refused = await host.deliver(
        "job", "go", cwd=str(tmp_path), fresh=True, delegated=False, automated=True
    )
    assert not refused.accepted and refused.reason == "at_capacity"
    # and more wakes while it is live do not stretch that grace.
    assert await host.wake("chat-w") == "live"
    host._live["chat-w"].woken_at -= IDLE_RECLAIM_GRACE_S + 1
    taken = await host.deliver(
        "job", "go", cwd=str(tmp_path), fresh=True, delegated=False, automated=True
    )
    assert taken.accepted and "chat-w" not in host._live
    await host.stop()


async def test_a_held_chat_is_reclaimed_last_but_never_refuses_a_slot(tmp_path):
    host, spawned = _host(tmp_path, max_workers=2)
    await _idle(host, spawned, "held", tmp_path)
    await _idle(host, spawned, "plain", tmp_path)
    host._records["held"].last_delivery_at -= 100  # quieter: first to go without a hold
    assert await host.wake("held") == "live"
    assert host._voice_until["held"] > 0

    await host.deliver("third", "go", cwd=str(tmp_path), fresh=True, delegated=False)
    assert "held" in host._live and "plain" not in host._live
    host._records["third"].last_delivery_at -= IDLE_RECLAIM_GRACE_S + 1
    spawned[-1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    # Only held and busy chats left: the hold orders, it does not refuse.
    await host.deliver("busy", "go", cwd=str(tmp_path), fresh=True, delegated=False)
    await _settle()
    fourth = await host.deliver("fourth", "go", cwd=str(tmp_path), fresh=True, delegated=False)
    assert fourth.accepted and "held" not in host._live
    await host.stop()


async def test_an_old_hold_runs_out_by_itself(tmp_path):
    host, spawned = _host(tmp_path)
    await _resting(host, spawned, "old", tmp_path)
    await host.wake("nobody")
    host._voice_until["old"] = 1.0
    await _resting(host, spawned, "new", tmp_path)
    await host.wake("new")
    assert set(host._voice_until) == {"new"}
    assert host._voice_until["new"] > VOICE_HOLD_S
    await host.stop()


async def test_a_wake_and_a_send_racing_on_one_chat_start_one_process(tmp_path):
    host, spawned = _host(tmp_path)
    await _resting(host, spawned, "chat-x", tmp_path)
    factory = host._process_factory
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return await factory(*args, **kwargs)

    host._process_factory = slow
    wake = asyncio.create_task(host.wake("chat-x"))
    await _settle()
    send = asyncio.create_task(host.deliver("chat-x", "hi", cwd=str(tmp_path), delegated=False))
    await _settle()
    gate.set()

    assert await wake == "spawned"
    sent = await send
    assert sent.accepted and sent.action != "spawned"
    assert len(spawned) == 2 and len(host._live) == 1
    assert spawned[1]["process"].stdin.lines[-1]["message"]["content"][0]["text"] == "hi"
    await host.stop()


async def test_a_failed_wake_leaves_nothing_and_the_send_spawns(tmp_path):
    host, spawned = _host(tmp_path)
    await _resting(host, spawned, "chat-f", tmp_path)
    factory = host._process_factory

    async def broken(*args, **kwargs):
        raise OSError("no such file")

    host._process_factory = broken
    assert await host.wake("chat-f") == "spawn_failed"
    assert "chat-f" not in host._live

    host._process_factory = factory
    sent = await host.deliver("chat-f", "hi", cwd=str(tmp_path), delegated=False)
    assert sent.accepted and sent.action == "revived"
    await host.stop()


async def test_voice_warm_wakes_the_chat_in_the_background_once_at_a_time(monkeypatch):
    account = Account("claude_code:main", "claude_code", "main", None)
    session = SimpleNamespace(key="claude_code:main:s1", session_id="s1")
    gate = asyncio.Event()

    async def wake(acc, session_id):
        await gate.wait()
        return "spawned"

    provider = SimpleNamespace(wake_worker=AsyncMock(side_effect=wake))
    monkeypatch.setattr(
        routes_speech, "resolve_current_leg", lambda request, key: (account, session, provider)
    )
    routes_speech._wake_chat(None, session.key)
    routes_speech._wake_chat(None, session.key)  # a second warm while the first asks
    await _settle()
    gate.set()
    await _settle()
    provider.wake_worker.assert_awaited_once_with(account, "s1")
    assert session.key not in routes_speech._waking

    # A provider that cannot wake, and a chat that is no leg, are skipped.
    monkeypatch.setattr(
        routes_speech,
        "resolve_current_leg",
        lambda request, key: (account, session, SimpleNamespace()),
    )
    routes_speech._wake_chat(None, session.key)

    def missing(request, key):
        raise HTTPException(404)

    monkeypatch.setattr(routes_speech, "resolve_current_leg", missing)
    routes_speech._wake_chat(None, session.key)
    # Nor does a deck that cannot resolve the chat at all make the warm fail.
    monkeypatch.setattr(routes_speech, "resolve_current_leg", lambda request, key: 1 / 0)
    routes_speech._wake_chat(None, session.key)
    assert not routes_speech._waking


async def test_a_kiosk_locked_chat_warmed_over_http_steers_its_phrase_into_one_locked_process(
    tmp_path, monkeypatch
):
    """The whole way (#4360 review): ``POST /api/voice/warm`` on the deck, its own
    session resolution, the Claude provider, the runtime client and route, and the
    worker host, for a chat the kiosk made locked; then the phrase sent the same way."""
    folder = tmp_path / "kitchen"
    folder.mkdir()
    (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"keep": {"command": "true"}}}))
    (folder / "CLAUDE.md").write_text("Guide")
    account = Account("claude_code:main", "claude_code", "main", tmp_path / "cfg")
    spawned = []

    async def factory(*args, **kwargs):
        process = FakeProcess()
        spawned.append({"args": args, "kwargs": kwargs, "process": process})
        asyncio.get_running_loop().call_soon(
            process.stdout.emit, {"type": "system", "subtype": "init", "session_id": "sid-k"}
        )
        return process

    host = ClaudeWorkerHost(account, state_dir=tmp_path / "state", process_factory=factory)
    runtime = create_runtime_app(
        AppConfig.model_validate(
            {
                "claude_workers": {"enabled": True},
                "accounts": [
                    {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}
                ],
            }
        )
    )
    runtime.state.claude_workers.hosts["main"] = host
    client = ClaudeWorkerClient(account, transport=httpx.ASGITransport(app=runtime))

    # Made as the kiosk makes it: locked, a token and a model, no permission mode.
    made = await client.deliver(
        "kitchen",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role=locked.LOCKED_ROLE,
        model="haiku",
    )
    assert made.accepted, made
    await _settle()
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    spawned[0]["process"].terminate()
    await _settle()
    await client.refresh()
    rec = host._records["kitchen"]
    before = (rec.last_delivery_at, rec.last_result_at, rec.permission_mode)

    deck = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "history.db")),
            accounts=[
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path / "cfg")
                )
            ],
        )
    )
    session = Session(
        "claude_code:main:sid-k",
        account.key,
        "sid-k",
        SessionStatus.IDLE,
        cwd=folder,
        capabilities=frozenset({Capability.INJECT}),
    )
    deck.state.app_state.update_session(session)
    provider = PROVIDERS["claude_code"]
    monkeypatch.setitem(provider._workers, account.key, client)
    monkeypatch.setattr(voice_helper, "warm", AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=deck), base_url="http://deck"
    ) as web:
        warmed = await web.post("/api/voice/warm", json={"session_key": session.key})
        assert warmed.status_code == 204
        await asyncio.gather(*routes_speech._wakes)

    assert len(spawned) == 2
    args = [str(item) for item in spawned[1]["args"]]
    args = args[args.index("claude") :]
    assert _arg(args, "--resume") == "sid-k"
    assert _arg(args, "--permission-mode") == "dontAsk"
    assert _arg(args, "--tools") == "WebSearch"
    assert not any("bypass" in item or "dangerously" in item for item in args)
    assert spawned[1]["process"].stdin.lines == []
    assert (rec.last_delivery_at, rec.last_result_at, rec.permission_mode) == before

    sent = await provider.inject(account, session, "what's for dinner", timeout_s=5)
    await _settle()
    assert sent.accepted, sent
    assert len(spawned) == 2
    assert "what's for dinner" in json.dumps(spawned[1]["process"].stdin.lines)
    await host.stop()
