"""New-Claude-chat provider wiring: worker client + start_session/inject/interrupt."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agentdeck.config import AppConfig
from agentdeck.models import (
    Account,
    Capability,
    Session,
    SessionStatus,
)
from agentdeck.providers.claude_code import worker_client
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.claude_code.worker import DeliverResult
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
from agentdeck.providers.transcript_reader import TranscriptMeta
from agentdeck.runtime import create_runtime_app
from agentdeck.state import AppState


def _account() -> Account:
    return Account("claude_code:main", "claude_code", "main", Path("/tmp/cfg"))


def _runtime_app(**worker_cfg):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, **worker_cfg},
            "accounts": [{"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}],
        }
    )
    return create_runtime_app(cfg)


def _client_against(app) -> ClaudeWorkerClient:
    return ClaudeWorkerClient(
        _account(), transport=httpx.ASGITransport(app=app)
    )


def test_new_session_defaults_use_claude_settings_and_worker_override(tmp_path):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    provider = ClaudeCodeProvider()
    (tmp_path / "settings.json").write_text(
        json.dumps({"model": "opus", "effortLevel": "medium"})
    )

    assert provider.new_session_defaults(account) == ("opus", "medium")
    assert provider.new_session_defaults(account, model_override="sonnet") == (
        "sonnet",
        "medium",
    )

    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "effortLevel": "medium",
                "env": {
                    "ANTHROPIC_MODEL": "sonnet",
                    "CLAUDE_CODE_EFFORT_LEVEL": "auto",
                },
            }
        )
    )
    assert provider.new_session_defaults(account) == ("sonnet", "medium")

    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"CLAUDE_CODE_EFFORT_LEVEL": "auto"},
            }
        )
    )
    assert provider.new_session_defaults(account) == ("opus", "medium")
    assert provider.new_session_defaults(account, model_override="fable") == (
        "fable",
        "medium",
    )
    assert next(
        choice.label for choice in provider.selectable_models if choice.value == "fable"
    ) == "Fable 5.1"


async def test_probe_false_when_workers_disabled():
    app = create_runtime_app(AppConfig())  # workers off → runtime returns 404
    client = _client_against(app)
    assert await client.probe() is False
    await client.stop()


async def test_client_maps_sessions_and_delivers():
    from unittest.mock import AsyncMock, MagicMock

    app = _runtime_app()
    host = MagicMock()
    host.snapshot = MagicMock(
        return_value={
            "workers": {
                "chat-abc": {
                    "session_id": "sid-1",
                    "session_token": "token-1",
                    "live": True,
                    "turn_active": True,
                    "stalled": False,
                }
            },
            "live_count": 1,
        }
    )
    host.deliver = AsyncMock(return_value=DeliverResult(True, "steered", session_id="sid-1"))
    app.state.claude_workers.hosts["main"] = host

    bound = {}
    client = ClaudeWorkerClient(
        _account(),
        on_session_token=lambda token, session_id: bound.__setitem__(token, session_id),
        transport=httpx.ASGITransport(app=app),
    )
    assert await client.probe() is True
    assert bound == {"token-1": "sid-1"}
    assert client.owns("sid-1") and client._key_for("sid-1") == "chat-abc"
    assert client.turn_active("sid-1") is True

    result = await client.deliver("chat-abc", "hi")
    assert result.accepted
    host.deliver.assert_awaited_with(
        "chat-abc",
        "hi",
        cwd=None,
        fresh=False,
        images=[],
        model=None,
        effort=None,
        permission_mode=None,
        delivery_id=None,
        allow_over_budget=False,
        require_idle=False,
        require_active=False,
        context_maintenance=False,
        context_rollover=False,
        expected_session_id=None,
        delegated=False,
        role=None,
        session_token=None,
    )
    await client.stop()


async def test_client_waits_for_worker_turn_completion():
    from unittest.mock import MagicMock

    app = _runtime_app()
    active = True

    def snapshot():
        return {
            "workers": {
                "chat-abc": {
                    "session_id": "sid-1",
                    "live": True,
                    "turn_active": active,
                    "stalled": False,
                }
            }
        }

    host = MagicMock()
    host.snapshot = snapshot
    app.state.claude_workers.hosts["main"] = host
    client = _client_against(app)
    assert await client.probe()

    async def finish():
        nonlocal active
        await asyncio.sleep(0.01)
        active = False

    finishing = asyncio.create_task(finish())
    result = await client.wait_for_turn("sid-1", timeout_s=1)
    await finishing

    assert result.accepted and result.session_id == "sid-1"
    await client.stop()


class _FlakyTransport(httpx.AsyncBaseTransport):
    """Wraps a transport and drops the connection on chosen request numbers."""

    def __init__(self, inner: httpx.AsyncBaseTransport, fail_until: int) -> None:
        self._inner = inner
        self._fail_until = fail_until
        self.requests = 0

    async def handle_async_request(self, request):
        self.requests += 1
        if self.requests <= self._fail_until:
            raise httpx.ReadError("Server disconnected without sending a response.")
        return await self._inner.handle_async_request(request)


def _flaky_client(app, fail_until: int) -> tuple[ClaudeWorkerClient, _FlakyTransport]:
    transport = _FlakyTransport(httpx.ASGITransport(app=app), fail_until)
    return ClaudeWorkerClient(_account(), transport=transport), transport


def _worker_snapshot(active_flag):
    def snapshot():
        return {
            "workers": {
                "chat-abc": {
                    "session_id": "sid-1",
                    "live": True,
                    "turn_active": active_flag(),
                    "stalled": False,
                }
            }
        }

    return snapshot


async def test_transient_runtime_drop_does_not_fail_a_running_turn(monkeypatch):
    """A dropped poll is not evidence the delegated turn failed.

    A long cross-account delegation polls the runtime thousands of times, so one
    `Server disconnected` is near-certain; it used to abort the wait and report
    healthy, still-running work as a failed delegation.
    """
    from unittest.mock import MagicMock

    app = _runtime_app()
    active = True

    host = MagicMock()
    host.snapshot = _worker_snapshot(lambda: active)
    app.state.claude_workers.hosts["main"] = host

    # Fail the two polls after probe()'s successful one, then recover.
    client, transport = _flaky_client(app, fail_until=0)
    assert await client.probe()
    transport._fail_until = transport.requests + 2
    client._RETRY_INTERVAL_S = 0.01
    monkeypatch.setattr(worker_client, "TURN_POLL_S", 0.01)

    async def finish():
        await asyncio.sleep(0.05)
        nonlocal active
        active = False

    finishing = asyncio.create_task(finish())
    result = await client.wait_for_turn("sid-1", timeout_s=5)
    await finishing

    assert result.accepted and result.session_id == "sid-1"
    await client.stop()


async def test_persistently_unreachable_runtime_still_fails_the_wait():
    from unittest.mock import MagicMock

    app = _runtime_app()
    host = MagicMock()
    host.snapshot = _worker_snapshot(lambda: True)
    app.state.claude_workers.hosts["main"] = host

    client, transport = _flaky_client(app, fail_until=0)
    assert await client.probe()
    transport._fail_until = 10**9  # runtime never comes back
    client.RUNTIME_UNREACHABLE_GRACE_S = 0.05
    client._RETRY_INTERVAL_S = 0.01

    result = await client.wait_for_turn("sid-1", timeout_s=5)

    assert not result.accepted
    assert "runtime unavailable" in (result.reason or "")
    await client.stop()


class _FakeWorkerClient:
    def __init__(self):
        self.calls = []
        self.delivery_ids = []
        self.delegated = []
        self.allow_over_budget = []
        self.conflict_reason = None
        self._owned = {"sid-1": "chat-1"}
        self.available = True
        self._live = True
        self._active = True
        self._effective = True
        self._pending = None

    def owns(self, sid):
        return sid in self._owned

    def _key_for(self, sid):
        return self._owned.get(sid)

    def worker_key(self, sid):
        return self._owned.get(sid)

    def bypass_permissions_available(self, sid):
        return sid in self._owned and getattr(self, "bypass_available", False)

    def bypass_permissions_on(self, sid):
        return sid in self._owned and getattr(self, "bypass_on", False)

    async def set_bypass_permissions(self, sid, enabled):
        from agentdeck.models import InjectResult

        self.calls.append(("bypass", sid, enabled))
        self.bypass_on = enabled
        return InjectResult(True, "updated")

    def supports(self, _capability):
        return False

    def turn_active(self, sid):
        return self._active and sid in self._owned

    def effective_activity(self, sid):
        return self._effective and sid in self._owned

    def live(self, sid):
        return self._live and sid in self._owned

    def exit_reason(self, sid):
        return ""

    def role(self, sid):
        return None

    def turn_cut_off(self, sid):
        return False

    def turn_cut_off_at(self, sid):
        return 0.0

    def turn_started_at(self, sid):
        return getattr(self, "_turn_started_at", None) if self.turn_active(sid) else None

    def revival_due(self, sid):
        return False

    def owned_session_ids(self):
        return set(self._owned)

    def cwd_for(self, sid):
        return None

    def pending_interaction(self, sid):
        return self._pending if sid in self._owned else None

    def turn_held(self, sid):
        return self.pending_interaction(sid) is not None

    async def deliver(
        self,
        key,
        message,
        *,
        cwd=None,
        fresh=False,
        images=None,
        model=None,
        effort=None,
        permission_mode=None,
        delivery_id=None,
        allow_over_budget=False,
        delegated=False,
        session_token=None,
    ):
        from agentdeck.models import InjectResult

        self.calls.append(
            (
                "deliver",
                key,
                message,
                cwd,
                fresh,
                images or [],
                model,
                effort,
                permission_mode,
            )
        )
        self.delivery_ids.append(delivery_id)
        self.allow_over_budget.append(allow_over_budget)
        self.delegated.append(delegated)
        if self.conflict_reason is not None:
            return InjectResult(False, self.conflict_reason)
        return InjectResult(True, session_id="sid-new")

    async def send(
        self,
        session_id,
        message,
        *,
        images=None,
        delivery_id=None,
        allow_over_budget=False,
    ):
        from agentdeck.models import InjectResult

        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self.deliver(
            key,
            message,
            images=images,
            delivery_id=delivery_id,
            allow_over_budget=allow_over_budget,
        )

    async def wait_for_turn(self, session_id, *, timeout_s):
        from agentdeck.models import InjectResult

        self.calls.append(("wait", session_id, timeout_s))
        return InjectResult(True, session_id=session_id)

    async def interrupt(self, session_id):
        from agentdeck.models import InjectResult

        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        self.calls.append(("interrupt", key))
        return InjectResult(True)

    async def answer(self, session_id, interaction_id, *, answers, decision):
        from agentdeck.models import InjectResult

        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        self.calls.append(("answer", key, interaction_id, answers, decision))
        return InjectResult(True)


async def test_provider_supports_new_session():
    assert ClaudeCodeProvider.supports_new_session is True
    provider = ClaudeCodeProvider()
    assert [choice.value for choice in provider.selectable_efforts] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert provider.can_start_session(_account()) is False
    provider._workers[_account().key] = _FakeWorkerClient()
    assert provider.can_start_session(_account()) is True


async def test_start_session_spawns_fresh_worker():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    result = await provider.start_session(
        _account(), Path("/tmp"), "hello", timeout_s=1, delegated=True
    )
    assert result.accepted
    kind, key, message, cwd, fresh, images, model, effort, permission_mode = fake.calls[0]
    assert (kind, message, cwd, fresh) == ("deliver", "hello", "/tmp", True)
    # A dashboard-created chat (no sandbox) inherits the account's configured
    # worker permission_mode — permission_mode is None here so the runtime
    # applies it, rather than forcing "default" (which would stall on the first
    # approval-gated tool since no permission-prompt tool is wired).
    assert images == [] and model is None and effort is None and permission_mode is None
    assert key.startswith("chat-")  # generated key
    assert fake.delegated == [True]


async def test_start_session_forwards_images_model_and_read_only_mode(tmp_path):
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")

    result = await provider.start_session(
        _account(),
        tmp_path,
        "inspect",
        timeout_s=1,
        images=[image],
        sandbox="read-only",
        model="haiku",
        effort="high",
    )

    assert result.accepted
    assert fake.calls[0][5:] == ([str(image)], "haiku", "high", "plan")


async def test_start_session_forwards_manual_budget_override():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake

    result = await provider.start_session(
        _account(), Path("/tmp"), "hello", timeout_s=1, allow_over_budget=True
    )

    assert result.accepted
    assert fake.allow_over_budget == [True]


async def test_start_session_errors_when_workers_absent():
    provider = ClaudeCodeProvider()
    result = await provider.start_session(_account(), Path("/tmp"), "hello", timeout_s=1)
    assert not result.accepted and "not enabled" in result.reason


async def test_inject_and_interrupt_resolve_session_to_key():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.LIVE,
    )
    r1 = await provider.inject(_account(), session, "reply", timeout_s=1)
    r2 = await provider.interrupt(_account(), session)
    assert r1.accepted and r2.accepted
    assert ("deliver", "chat-1", "reply", None, False, [], None, None, None) in fake.calls
    assert ("interrupt", "chat-1") in fake.calls


async def test_boss_followup_forwards_account_budget_override():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake

    result = await provider.inject(
        _account(),
        _live_session(),
        "reply",
        timeout_s=1,
        allow_over_budget=True,
    )

    assert result.accepted
    assert fake.allow_over_budget == [True]


async def test_inject_rejects_unowned_session():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:other",
        account_key="claude_code:main",
        session_id="other",
        status=SessionStatus.IDLE,
    )
    result = await provider.inject(_account(), session, "reply", timeout_s=1)
    assert not result.accepted and "not a deck-owned worker" in result.reason


async def test_owned_worker_liveness_stays_visible_without_cli_registry():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.IDLE,
    )

    changed = provider.sweep_liveness(_account(), [session], AppState())

    assert changed == [session]
    assert session.status is SessionStatus.LIVE
    assert session.thinking is True
    assert session.show_when_idle is True

    fake._active = False
    fake._effective = False
    changed = provider.sweep_liveness(_account(), [session], AppState())
    assert changed == [session]
    assert session.status is SessionStatus.LIVE
    assert session.thinking is False
    assert session.show_when_idle is True


def test_only_a_live_owned_worker_stalls_or_works(monkeypatch):
    """#2956: a dead worker's transcript can keep an open turn, and the stall it read held
    the Work's release as "still working" with nothing running."""
    from datetime import timedelta

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    old = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(
        provider, "_turn_state", lambda *_a, **_k: ("Working", True, old, False)
    )
    session = _live_session()

    provider.sweep_liveness(_account(), [session], AppState())
    assert (session.status, session.stalled, session.thinking) == (
        SessionStatus.LIVE, True, False,
    )

    fake._live = False
    provider.sweep_liveness(_account(), [session], AppState())
    assert (session.status, session.stalled, session.thinking) == (
        SessionStatus.IDLE, False, False,
    )


def test_an_owned_turn_that_ended_on_a_background_command_never_stalls(monkeypatch):
    """#3016: the runtime said the turn was over and nothing stalled, while two renders ran
    in the background; the web's own stall read showed the Work "no progress since"."""
    from datetime import timedelta

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake._active = False
    fake._effective = False
    provider._workers["claude_code:main"] = fake
    old = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(provider, "_background_work_active", lambda _path: True)
    monkeypatch.setattr(
        provider, "_turn_state", lambda *_a, **_k: (None, True, old, False)
    )
    session = _live_session()

    provider.sweep_liveness(_account(), [session], AppState())

    assert session.status is SessionStatus.LIVE
    assert session.lifecycle_active is True
    assert session.stalled is False
    # #3123: quiet past the stall window the transcript gives no label at all, and an
    # unlabelled thinking chat read as Working on its Work. The runtime says no turn is open.
    assert session.activity == "Background task"


def test_an_owned_turn_waiting_on_a_writing_helper_agent_never_stalls(tmp_path, monkeypatch):
    """#3067: a chat waiting on its helper agents writes nothing to its own transcript, so
    the Work read "no progress since" while the helpers worked and the runtime saw no stall.
    #3110: a chat woken after a long idle has written nothing yet either, and its stall
    clock starts at the turn the runtime just opened, not at the turn before."""
    import os
    from datetime import timedelta

    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    project = tmp_path / "projects" / "-repo"
    project.mkdir(parents=True)
    transcript = project / "sid-1.jsonl"
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": hour_ago.isoformat().replace("+00:00", "Z"),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Agent", "input": {}}
                    ],
                },
            }
        )
        + "\n"
    )
    os.utime(transcript, (hour_ago.timestamp(), hour_ago.timestamp()))

    def sweep(turn_started_at: datetime | None = None) -> Session:
        provider = ClaudeCodeProvider()
        worker = _FakeWorkerClient()
        worker._turn_started_at = turn_started_at
        provider._workers["claude_code:main"] = worker
        session = _live_session()
        provider.sweep_liveness(account, [session], AppState())
        return session

    quiet = sweep()
    assert quiet.stalled is True
    just_woken = datetime.now(UTC) - timedelta(seconds=1)
    woken = sweep(turn_started_at=just_woken)
    assert (woken.stalled, woken.last_progress) == (False, just_woken)

    helpers = project / "sid-1" / "subagents"
    helpers.mkdir(parents=True)
    (helpers / "agent-a1.jsonl").write_text("{}\n")
    busy = sweep()
    assert busy.stalled is False
    assert busy.thinking is True
    assert busy.last_progress is not None and busy.last_progress > hour_ago


def test_owned_worker_liveness_can_clear_a_stale_message_timestamp(
    tmp_path, monkeypatch
):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript = tmp_path / "sid-1.jsonl"
    transcript.write_text("{}\n")
    provider = ClaudeCodeProvider()
    provider._workers[account.key] = _FakeWorkerClient()
    session = _live_session()
    session.last_message_at = datetime(2026, 8, 13, 9, tzinfo=UTC)
    monkeypatch.setattr(provider, "_transcript_path", lambda *_a, **_k: transcript)
    monkeypatch.setattr(provider, "_cached_meta", lambda *_: TranscriptMeta())

    provider.sweep_liveness(account, [session], AppState())

    assert session.last_message_at is None


async def test_an_unrouted_question_stops_an_owned_worker_reading_as_working(tmp_path):
    """Issue #1878: without a permission prompt tool the ask never reaches the
    runtime as a control request, so the turn stays open and only the transcript
    shows it. Keying on the routed interaction alone left the chat as Working
    with no label — and, through the pill rule, with no waiting pill either."""
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "sid-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(tmp_path),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Close PR 1773 or finish it?",
                                        "header": "PR 1773",
                                        "options": [],
                                    }
                                ]
                            },
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake._active = True
    fake._effective = True
    provider._workers[account.key] = fake

    (session,) = await provider.scan_sessions(account)

    assert session.question == "Close PR 1773 or finish it?"
    assert session.thinking is False
    assert session.stalled is False
    assert session.display_state == "idle"
    assert session.activity is None


async def test_background_agent_keeps_owned_session_working_without_turn_controls(
    tmp_path,
):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "sid-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(tmp_path),
                "message": {"role": "assistant", "content": "Reviewers launched"},
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake._active = False
    fake._effective = True
    provider._workers[account.key] = fake

    (session,) = await provider.scan_sessions(account)

    assert session.lifecycle_active is True
    assert session.thinking is True
    assert session.activity == "Background agent"
    assert session.capabilities == frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.REVIVE}
    )


async def test_quiet_background_agent_does_not_make_slot_holder_look_idle(tmp_path):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "sid-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(tmp_path),
                "message": {"role": "assistant", "content": "Reviewers launched"},
            }
        )
        + "\n"
    )
    old = datetime.now(UTC).timestamp() - 3_600
    os.utime(transcript, (old, old))
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake._active = False
    fake._effective = True
    provider._workers[account.key] = fake

    (session,) = await provider.scan_sessions(account)

    assert session.lifecycle_active is True
    assert session.stalled is False
    assert session.thinking is True
    assert session.display_state == "thinking"
    assert session.activity == "Background agent"


async def test_background_shell_task_keeps_owned_session_working_without_turn_controls(
    tmp_path,
):
    """Issue #683: the runtime never saw this one launch — the transcript did.

    A backgrounded shell command is announced only on the CLI's own tool_result,
    so an owned worker whose outer turn has ended still has outstanding work. It
    keeps the chat working (no false handoff) without making the finished turn
    steerable or interruptible again.
    """
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "sid-1.jsonl").write_text(
        "".join(
            json.dumps(line) + "\n"
            for line in (
                {
                    "type": "user",
                    "sessionId": "sid-1",
                    "cwd": str(tmp_path),
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": (
                                    "Command running in background with ID: bwait1."
                                ),
                            }
                        ],
                    },
                    "toolUseResult": {"stdout": "", "backgroundTaskId": "bwait1"},
                },
                {
                    "type": "assistant",
                    "sessionId": "sid-1",
                    "cwd": str(tmp_path),
                    "message": {
                        "role": "assistant",
                        "content": "Waiting for the gate to open.",
                        "stop_reason": "end_turn",
                    },
                },
            )
        )
    )
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake._active = False
    fake._effective = False
    provider._workers[account.key] = fake

    (session,) = await provider.scan_sessions(account)

    assert session.lifecycle_active is True
    assert session.thinking is True
    # Named distinctly from a model-driven turn: this chat is blocked on an
    # uncollected background command, not on the model (#683, #791).
    assert session.activity == "Background task"
    assert session.capabilities == frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.REVIVE}
    )

    # #3123: a command the chat stopped itself never notifies; the TaskStop result ends it.
    with (transcript_dir / "sid-1.jsonl").open("a") as out:
        out.write(json.dumps({
            "type": "user",
            "sessionId": "sid-1",
            "cwd": str(tmp_path),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_02",
                 "content": '{"message":"Successfully stopped task: bwait1 (sleep 1)"}'}
            ]},
            "toolUseResult": {"message": "Successfully stopped task: bwait1 (sleep 1)",
                              "task_id": "bwait1", "task_type": "local_bash"},
        }) + "\n")
    provider = ClaudeCodeProvider()
    provider._workers[account.key] = fake
    (session,) = await provider.scan_sessions(account)
    assert (session.lifecycle_active, session.thinking) == (False, False)


async def test_owned_worker_projection_matches_scan_and_sweep(tmp_path):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "sid-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(tmp_path),
                "message": {"role": "assistant", "content": "Working on it"},
            }
        )
        + "\n"
    )

    for available in (True, False):
        provider = ClaudeCodeProvider()
        fake = _FakeWorkerClient()
        fake.available = available
        provider._workers[account.key] = fake

        (session,) = await provider.scan_sessions(account)
        projected_fields = (
            session.status,
            session.thinking,
            session.activity,
            session.show_when_idle,
            session.capabilities,
        )

        session.status = SessionStatus.IDLE
        session.thinking = False
        session.activity = None
        session.show_when_idle = False
        session.capabilities = frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.REVIVE,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        )
        provider.sweep_liveness(account, [session], AppState())
        sweep_fields = (
            session.status,
            session.thinking,
            session.activity,
            session.show_when_idle,
            session.capabilities,
        )

        assert projected_fields == sweep_fields


async def test_owned_worker_projection_is_withdrawn_when_ownership_disappears():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.LIVE,
        thinking=True,
        activity="Working",
        show_when_idle=True,
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.REVIVE,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )

    fake._owned.clear()
    changed = provider.sweep_liveness(_account(), [session], AppState())

    assert changed == [session]
    assert session.status is SessionStatus.IDLE
    assert session.thinking is False
    assert session.activity is None
    assert session.show_when_idle is False
    assert session.capabilities == frozenset({Capability.TRANSCRIPT})


async def test_provider_wait_and_result_complete_claude_delegation(tmp_path):
    account = Account("claude_code:main", "claude_code", "main", tmp_path / "cfg")
    transcript_dir = account.root / "projects" / "-tmp-project"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "sid-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Delegation complete."}],
                },
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers[account.key] = fake

    waited = await provider.wait_for_session(account, "sid-1", timeout_s=12)
    result = await provider.session_result(account, "sid-1")

    assert waited.accepted
    assert ("wait", "sid-1", 12) in fake.calls
    assert result == "Delegation complete."


async def test_retried_new_chat_reuses_one_key_and_delivery(tmp_path):
    # A dashboard retry carries the same client-action id, so both start_session
    # calls must map to the SAME worker key + delivery id — the deliver layer then
    # dedups instead of spawning a second worker.
    from agentdeck.action_context import client_action_context

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    with client_action_context("act-42"):
        await provider.start_session(_account(), tmp_path, "hi", timeout_s=1)
        await provider.start_session(_account(), tmp_path, "hi", timeout_s=1)
    keys = [c[1] for c in fake.calls]
    assert keys[0] == keys[1] and keys[0].startswith("chat-")
    assert fake.delivery_ids == ["act-42", "act-42"]


async def test_new_chat_without_action_id_uses_a_fresh_key(tmp_path):
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    await provider.start_session(_account(), tmp_path, "hi", timeout_s=1)
    await provider.start_session(_account(), tmp_path, "hi", timeout_s=1)
    keys = [c[1] for c in fake.calls]
    assert keys[0] != keys[1]  # random per call — no action id to key on
    assert fake.delivery_ids == [None, None]


async def test_new_chat_digest_mismatch_maps_to_client_action_conflict(tmp_path):
    from agentdeck.action_context import client_action_context

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake.conflict_reason = "delivery_id_conflict"  # same id, different payload
    provider._workers["claude_code:main"] = fake
    with client_action_context("act-9"):
        result = await provider.start_session(_account(), tmp_path, "hi", timeout_s=1)
    assert not result.accepted and result.reason == "client_action_conflict"


def _live_session():
    return Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.LIVE,
    )


async def test_retried_send_forwards_stable_delivery_id(tmp_path):
    # A retried dashboard send (same client-action id) must reach the deliver layer
    # with the same delivery id so it dedups instead of writing the message twice.
    from agentdeck.action_context import client_action_context

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    with client_action_context("send-7"):
        await provider.inject(_account(), _live_session(), "reply", timeout_s=1)
        await provider.inject(_account(), _live_session(), "reply", timeout_s=1)
    assert fake.delivery_ids == ["send-7", "send-7"]


async def test_send_digest_mismatch_maps_to_client_action_conflict(tmp_path):
    from agentdeck.action_context import client_action_context

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    fake.conflict_reason = "delivery_id_conflict"  # same id, different payload
    provider._workers["claude_code:main"] = fake
    with client_action_context("send-8"):
        result = await provider.inject(_account(), _live_session(), "reply", timeout_s=1)
    assert not result.accepted and result.reason == "client_action_conflict"


async def test_send_without_action_id_passes_no_delivery_id(tmp_path):
    # Interactive callers with no action id keep the old behavior (no dedup).
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    await provider.inject(_account(), _live_session(), "reply", timeout_s=1)
    assert fake.delivery_ids == [None]


async def test_control_actions_suppressed_when_runtime_unreachable():
    # The _owned map is a last-known snapshot. While the runtime is unreachable,
    # inject/steer/interrupt must be withheld (they'd target a worker we can't talk
    # to); the session stays visible and they come back on reconnect.
    from agentdeck.models import Capability

    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.LIVE,
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )
    fake._pending = {
        "id": "permission-1",
        "kind": "command_approval",
        "thread_id": "sid-1",
        "title": "Approve command?",
    }
    assert provider.pending_interaction(_account(), session) is None

    provider.sweep_liveness(_account(), [session], AppState())
    assert Capability.INTERACT in session.capabilities
    assert provider.pending_interaction(_account(), session) is not None

    fake.available = False  # runtime unreachable
    provider.sweep_liveness(_account(), [session], AppState())
    assert Capability.INJECT not in session.capabilities
    assert Capability.STEER not in session.capabilities
    assert Capability.INTERRUPT not in session.capabilities
    assert Capability.INTERACT not in session.capabilities
    assert Capability.TRANSCRIPT in session.capabilities  # still readable
    assert session.show_when_idle is True  # visible, just uncontrollable
    assert provider.pending_interaction(_account(), session) is None

    fake.available = True  # reconnect re-grants control
    provider.sweep_liveness(_account(), [session], AppState())
    assert Capability.INJECT in session.capabilities
    assert Capability.INTERACT in session.capabilities
    assert provider.pending_interaction(_account(), session) is not None


def test_runtime_availability_change_reprojects_cached_capabilities():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    account = _account()
    provider._workers[account.key] = fake
    state = AppState()
    session = Session(
        key=f"{account.key}:sid-1",
        account_key=account.key,
        session_id="sid-1",
        status=SessionStatus.LIVE,
        thinking=True,
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )
    state.sessions[session.key] = session

    with state.bus.subscribe("sessions") as events:
        fake.available = False
        provider._runtime_changed(account, state)

        assert session.capabilities == frozenset({Capability.TRANSCRIPT})
        assert events.get_nowait()[0] == "sessions"

        provider._runtime_changed(account, state)
        assert events.get_nowait() is None

        fake.available = True
        provider._runtime_changed(account, state)

        assert session.capabilities == frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.REVIVE,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        )
        assert events.get_nowait()[0] == "sessions"


async def test_client_write_methods_reject_an_unowned_session():
    # The client resolves session_id -> key internally; an unowned session gets
    # the standard miss result with no runtime round-trip (parity with Codex).
    app = _runtime_app()
    client = _client_against(app)
    miss = "this session is not a deck-owned worker"
    assert (await client.send("ghost", "hi")).reason == miss
    assert (await client.interrupt("ghost")).reason == miss
    assert (await client.answer("ghost", "iid", answers={}, decision=None)).reason == miss
    await client.stop()


async def _owned_session(tmp_path, fake):
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / "sid-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(tmp_path),
                "message": {"role": "assistant", "content": "Working on it"},
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    provider._workers[account.key] = fake
    (session,) = await provider.scan_sessions(account)
    return provider, account, session


async def test_a_claude_chat_never_offers_the_permission_switch(tmp_path):
    # #2271 removed the switch for Claude: every classifier block reaches the
    # boss anyway, so the checkbox would change nothing. Codex keeps its own.
    fake = _FakeWorkerClient()
    provider, account, session = await _owned_session(tmp_path, fake)

    assert Capability.PERMISSION_PROMPTS not in session.capabilities
    assert provider.permission_prompts_allowed(account, session) is False
    assert not (await provider.set_permission_prompts(account, session, True)).accepted


async def test_a_claude_chat_resumes_elsewhere_only_when_the_runtime_can(tmp_path):
    # #2524: an older runtime would wake the CLI in a deleted directory and never
    # answer, so the deck lets such a send through only on the runtime's word.
    from agentdeck.models import SNAPSHOT_RELOCATE_V1

    fake = _FakeWorkerClient()
    _provider, _account, session = await _owned_session(tmp_path, fake)
    assert Capability.RESUME_ELSEWHERE not in session.capabilities

    fake.supports = lambda capability: capability == SNAPSHOT_RELOCATE_V1
    _provider, _account, session = await _owned_session(tmp_path, fake)
    assert Capability.RESUME_ELSEWHERE in session.capabilities


async def test_a_moved_claude_chat_reads_its_directory_from_the_runtime(tmp_path):
    # #2524: the transcript keeps the deleted worktree it started in; the runtime's
    # record says where the chat runs now.
    repository = tmp_path / "project-repo"
    repository.mkdir()
    fake = _FakeWorkerClient()
    fake.cwd_for = lambda sid: str(repository)
    gone = tmp_path / "gone-worktree"
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    transcript_dir = tmp_path / "projects" / "project"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "sid-1.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "cwd": str(gone),
                "message": {"role": "assistant", "content": "Working on it"},
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    provider._workers[account.key] = fake
    (session,) = await provider.scan_sessions(account)
    assert session.cwd == repository

    # While the recorded directory exists, it stays the truth.
    gone.mkdir()
    provider = ClaudeCodeProvider()
    provider._workers[account.key] = fake
    (session,) = await provider.scan_sessions(account)
    assert session.cwd == gone


async def test_a_claude_chat_offers_bypass_only_when_the_runtime_does(tmp_path):
    # #2285: the runtime says whether this chat's own mode leaves anything to
    # bypass; an older runtime that says nothing offers no switch.
    fake = _FakeWorkerClient()
    provider, account, session = await _owned_session(tmp_path, fake)
    assert Capability.BYPASS_PERMISSIONS not in session.capabilities

    fake.bypass_available = True
    provider, account, session = await _owned_session(tmp_path, fake)
    assert Capability.BYPASS_PERMISSIONS in session.capabilities
    assert provider.bypass_permissions_on(account, session) is False
    assert (await provider.set_bypass_permissions(account, session, True)).accepted
    assert fake.calls[-1] == ("bypass", "sid-1", True)
    assert provider.bypass_permissions_on(account, session) is True


async def test_a_spawning_worker_is_kept_but_a_dead_one_with_no_transcript_is_not(
    tmp_path,
):
    """Issue #1139: a worker record outlives the chat it spawned.

    Keeping an owned id in the scan universe is what makes a freshly spawned
    worker appear before its first transcript write. Once the process is gone
    and the transcript with it, the same rule kept rebuilding a row out of
    nothing — an eight-character hex title, no activity, nothing to open, and a
    `revive` that cannot resume a file that no longer exists.
    """
    account = Account("claude_code:main", "claude_code", "main", tmp_path)
    (tmp_path / "projects" / "project").mkdir(parents=True)
    provider = ClaudeCodeProvider()
    spawning = _FakeWorkerClient()
    provider._workers[account.key] = spawning

    (session,) = await provider.scan_sessions(account)
    assert session.session_id == "sid-1"
    assert Capability.TRANSCRIPT not in session.capabilities

    dead = _FakeWorkerClient()
    dead._live = False
    provider._workers[account.key] = dead

    assert await provider.scan_sessions(account) == []


async def _probed_client(handler) -> ClaudeWorkerClient:
    client = ClaudeWorkerClient(_account(), transport=httpx.MockTransport(handler))
    await client.probe()
    return client


async def test_a_runtime_that_cannot_be_reached_is_still_offered_in_the_new_chat_form():
    """Offered while the runtime restarts, never started (#2975)."""

    def down(request):
        raise httpx.ConnectError("no socket", request=request)

    provider = ClaudeCodeProvider()
    assert provider.offers_new_session(_account()) is False
    provider._workers[_account().key] = await _probed_client(down)
    assert provider.can_start_session(_account()) is False
    assert provider.offers_new_session(_account()) is True


async def test_a_runtime_that_serves_no_claude_workers_is_not_offered():
    """Workers switched off answer 404: never offered, so Start never says to retry (#2975)."""

    def disabled(request):
        return httpx.Response(404, json={"detail": "claude workers are disabled"})

    provider = ClaudeCodeProvider()
    client = await _probed_client(disabled)
    provider._workers[_account().key] = client
    assert provider.offers_new_session(_account()) is False
    # Nor after a refresh, which reads the same refusal through raise_for_status.
    await client.refresh()
    assert client.unreachable is False
    assert provider.offers_new_session(_account()) is False
