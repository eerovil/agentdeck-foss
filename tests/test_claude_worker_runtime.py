"""HTTP surface for deck-owned Claude workers: runtime routes + web proxy."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from agentdeck.config import AppConfig
from agentdeck.context_delivery import ContextDelivery, context_request_sha256
from agentdeck.db import Db
from agentdeck.models import (
    SNAPSHOT_ACTIVITY_CLOCK,
    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
    Account,
    Session,
    SessionStatus,
)
from agentdeck.providers.claude_code.worker import DeliverResult
from agentdeck.runtime import create_runtime_app
from agentdeck.state import AppState


def _runtime_app(**worker_cfg):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, **worker_cfg},
            "accounts": [{"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}],
        }
    )
    return create_runtime_app(cfg)


def _context_hash(provider_id, label, body):
    return context_request_sha256(
        provider_id=provider_id,
        account_key=f"{provider_id}:{label}",
        worker_key=body.key,
        message=body.message,
        cwd=body.cwd,
        fresh=body.fresh,
        image_count=len(body.images),
        model=body.model,
        effort=body.effort,
        permission_mode=body.permission_mode,
        allow_over_budget=body.allow_over_budget,
        require_idle=body.require_idle,
        require_active=body.require_active,
        expected_last_result_at=body.expected_last_result_at,
        role=body.role,
        delegated=body.delegated,
        caller_session_token=body.session_token,
        continues_session_url=body.continues_session_url,
        previous_worker_summary=body.previous_worker_summary,
        context_policy=body.context_policy,
    )


def test_per_account_overrides_merge_over_base():
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {
                "enabled": True,
                "max_workers": 4,
                "permission_mode": "",
                "accounts": {
                    "alt": {"permission_mode": "bypassPermissions", "max_workers": 3},
                },
            },
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"},
                {"provider": "claude_code", "label": "alt", "config_dir": "~/.claude2"},
            ],
        }
    )
    base = cfg.claude_workers.for_account("main")
    assert base.permission_mode == "" and base.max_workers == 4
    alt = cfg.claude_workers.for_account("alt")
    assert alt.permission_mode == "bypassPermissions" and alt.max_workers == 3
    # untouched fields inherit the base
    assert alt.state_dir == base.state_dir and alt.enabled is True


async def test_runtime_host_applies_account_overrides(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {
                "enabled": True,
                "state_dir": str(tmp_path),
                "accounts": {"alt": {"permission_mode": "bypassPermissions"}},
            },
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"},
                {"provider": "claude_code", "label": "alt", "config_dir": "~/.claude2"},
            ],
        }
    )
    app = create_runtime_app(cfg)
    assert app.state.claude_workers.host("alt").permission_mode == "bypassPermissions"
    assert app.state.claude_workers.host("main").permission_mode == "auto"


@pytest.mark.parametrize(
    "field,value",
    [("usage_ceiling_pct", -1), ("usage_ceiling_pct", 101), ("stall_after_s", -1)],
)
def test_per_account_overrides_validate_policy_ranges(field, value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"claude_workers": {"accounts": {"alt": {field: value}}}}
        )


async def test_deliver_endpoint_disabled_returns_404():
    app = create_runtime_app(AppConfig())  # claude_workers off by default
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        r = await client.post(
            "/claude/accounts/main/deliver", json={"key": "k", "message": "m", "cwd": "/tmp"}
        )
    assert r.status_code == 404


async def test_deliver_and_key_endpoints_route_to_host():
    app = _runtime_app()
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(True, "spawned", session_id="s1"))
    host.interrupt = AsyncMock(return_value=DeliverResult(True, "interrupted"))
    host.stop_worker = AsyncMock(return_value=DeliverResult(True, "stopped"))
    host.park_worker = AsyncMock(return_value=DeliverResult(True, "parked"))
    host.release_worker = AsyncMock(return_value=DeliverResult(True, "released"))
    host.forget = AsyncMock(return_value=DeliverResult(True, "forgotten"))
    app.state.claude_workers.hosts["main"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        d = await client.post(
            "/claude/accounts/main/deliver",
            json={
                "key": "owner/repo#12",
                "message": "go",
                "cwd": "/tmp",
                "delivery_id": "attempt-1",
            },
        )
        i = await client.post("/claude/accounts/main/interrupt", json={"key": "owner/repo#12"})
        s = await client.post("/claude/accounts/main/stop", json={"key": "owner/repo#12"})
        p = await client.post("/claude/accounts/main/park", json={"key": "owner/repo#12"})
        r = await client.post("/claude/accounts/main/release", json={"key": "owner/repo#12"})
        f = await client.post("/claude/accounts/main/forget", json={"key": "owner/repo#12"})

    assert d.json() == {
        "accepted": True,
        "action": "spawned",
        "reason": None,
        "session_id": "s1",
    }
    # key with '#' and '/' survives because it travels in the body, not the path
    host.deliver.assert_awaited_once_with(
        "owner/repo#12",
        "go",
        cwd="/tmp",
        fresh=False,
        images=[],
        model=None,
        effort=None,
        permission_mode=None,
        delivery_id="attempt-1",
        allow_over_budget=False,
        require_idle=False,
        require_active=False,
        context_maintenance=False,
        context_rollover=False,
        expected_session_id=None,
        delegated=True,
        role=None,
        session_token=None,
    )
    host.interrupt.assert_awaited_once_with("owner/repo#12")
    host.stop_worker.assert_awaited_once_with("owner/repo#12")
    host.park_worker.assert_awaited_once_with("owner/repo#12")
    host.release_worker.assert_awaited_once_with("owner/repo#12")
    host.forget.assert_awaited_once_with("owner/repo#12")
    assert i.json()["action"] == "interrupted"
    assert s.json()["action"] == "stopped"
    assert p.json()["action"] == "parked"
    assert r.json()["action"] == "released"
    assert f.json() == {"removed": True}


async def test_block_endpoints_route_to_the_host_by_account_key():
    # #2271: the worker's own PermissionDenied hook calls these over the runtime
    # socket with the account key and worker key from its environment.
    app = _runtime_app()
    host = MagicMock()
    host.report_block = AsyncMock(return_value=("block-1", ""))
    host.wait_block = AsyncMock(return_value="allowed")
    app.state.claude_workers.hosts["main"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        reported = await client.post(
            "/claude/blocks",
            json={
                "account_key": "claude_code:main",
                "key": "owner/repo#12",
                "session_token": "tok",
                "tool_name": "Bash",
                "tool_input": {"command": "gh api -X DELETE repos/o/r"},
                "reason": "[Irreversible Destruction]",
            },
        )
        waited = await client.get(
            "/claude/blocks/block-1",
            params={"account_key": "claude_code:main", "key": "owner/repo#12", "wait": 5},
        )
        unknown = await client.post(
            "/claude/blocks", json={"account_key": "claude_code:nobody", "key": "k"}
        )

    assert reported.json() == {"id": "block-1", "reason": ""}
    host.report_block.assert_awaited_once_with(
        "owner/repo#12",
        session_token="tok",
        tool_name="Bash",
        tool_input={"command": "gh api -X DELETE repos/o/r"},
        reason="[Irreversible Destruction]",
    )
    assert waited.json() == {"state": "allowed"}
    host.wait_block.assert_awaited_once_with("owner/repo#12", "block-1", timeout_s=5.0)
    assert unknown.status_code == 404


def test_worker_deliver_request_carries_provider_policy_fields():
    # Regression: the web-proxy request model must mirror the runtime's
    # DeliverRequest so model_dump() (what claude_deliver forwards) doesn't
    # silently drop a caller's images / model / effort / permission_mode.
    from agentdeck.web.routes_api import WorkerDeliverRequest

    dumped = WorkerDeliverRequest(
        key="owner/repo#12",
        message="go",
        cwd="/tmp",
        images=["/shot.png"],
        model="opus",
        effort="high",
        permission_mode="bypassPermissions",
        allow_over_budget=True,
        require_active=True,
        role="kanban",
    ).model_dump()
    assert dumped["images"] == ["/shot.png"]
    assert dumped["model"] == "opus"
    assert dumped["effort"] == "high"
    assert dumped["permission_mode"] == "bypassPermissions"
    assert dumped["allow_over_budget"] is True
    assert dumped["require_active"] is True
    assert dumped["role"] == "kanban"


async def test_web_proxy_forwards_to_runtime_and_surfaces_errors(tmp_path):
    from agentdeck.web.routes_api import _runtime_proxy

    # Fake runtime app the proxy client will hit over ASGI.
    runtime = _runtime_app()
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(False, "rejected", reason="over_budget"))
    runtime.state.claude_workers.hosts["main"] = host

    class Req:
        class app:
            class state:
                runtime_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime), base_url="http://agentdeck-runtime"
                )

    result = await _runtime_proxy(
        Req,
        "POST",
        "/claude/accounts/main/deliver",
        json={"key": "k", "message": "m", "cwd": "/tmp"},
    )
    assert result["reason"] == "over_budget"
    await Req.app.state.runtime_http.aclose()


async def test_web_proxy_503_when_runtime_unset():
    import pytest
    from fastapi import HTTPException

    from agentdeck.web.routes_api import _runtime_proxy

    class Req:
        class app:
            class state:
                pass

    with pytest.raises(HTTPException) as exc:
        await _runtime_proxy(Req, "GET", "/claude/accounts/main/workers")
    assert exc.value.status_code == 503


async def test_answer_endpoint_routes_to_host():
    app = _runtime_app()
    host = MagicMock()
    host.answer = AsyncMock(return_value=DeliverResult(True, "answered", session_id="s1"))
    app.state.claude_workers.hosts["main"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        r = await client.post(
            "/claude/accounts/main/answer",
            json={
                "key": "owner/repo#12",
                "interaction_id": "req-42",
                "answers": {"0": ["banana"]},
                "decision": "accept",
            },
        )
    assert r.json()["accepted"] is True and r.json()["action"] == "answered"
    host.answer.assert_awaited_once_with(
        "owner/repo#12", "req-42", answers={"0": ["banana"]}, decision="accept"
    )


async def test_a_spawned_worker_is_given_an_identity_it_can_resolve():
    """A worker spawned here has no AGENTDECK_SESSION_KEY: its session id does not exist until
    the provider returns one, and injecting a guess would hand it another session's identity
    (#235). So the deck mints a token, the worker is started with it in its instructions, and
    the token is bound to the real key the moment the spawn reports that key back.

    Without this a poller-dispatched worker can never identify itself, and everything a session
    key unlocks — registering the PR it just opened, reporting that it needs a human — is
    unavailable to it for its whole life.
    """
    from agentdeck.web.routes_api import WorkerDeliverRequest, claude_deliver

    runtime = _runtime_app()
    host = MagicMock()
    host.deliver = AsyncMock(
        return_value=DeliverResult(True, "spawned", session_id="sid-abc")
    )
    runtime.state.claude_workers.hosts["main"] = host

    bound: dict[str, str] = {}

    class Req:
        class app:
            class state:
                runtime_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime), base_url="http://agentdeck-runtime"
                )
                app_state = MagicMock(
                    bind_session_token=lambda t, k: bound.__setitem__(t, k)
                )

    result = await claude_deliver(
        Req, "main", WorkerDeliverRequest(key="issue-1", message="work it", cwd="/tmp")
    )
    await Req.app.state.runtime_http.aclose()

    assert result["session_id"] == "sid-abc"
    # The worker was started with a token...
    token = host.deliver.await_args.kwargs["session_token"]
    assert token, "the worker was spawned with no way to identify itself"
    # ...and that exact token now resolves to this session.
    assert bound == {token: "claude_code:main:sid-abc"}


async def test_provider_neutral_codex_delivery_binds_the_session_token():
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    runtime = create_runtime_app(
        AppConfig.model_validate(
            {
                "accounts": [
                    {"provider": "codex", "label": "codex", "config_dir": "~/.codex"}
                ]
            }
        )
    )
    host = MagicMock()
    host.deliver = AsyncMock(
        return_value=DeliverResult(True, "spawned", session_id="thread-abc")
    )
    runtime.state.codex_workers.hosts["codex"] = host
    bound: dict[str, str] = {}

    class Req:
        class app:
            class state:
                runtime_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime),
                    base_url="http://agentdeck-runtime",
                )
                app_state = MagicMock(
                    bind_session_token=lambda token, key: bound.__setitem__(token, key)
                )

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="work it",
            cwd="/tmp",
            allow_over_budget=True,
            require_idle=True,
            require_active=True,
            expected_last_result_at=12.0,
            role="kanban",
            command_artifacts={"mode": "enforce", "card": "issue-1"},
        ),
    )
    await Req.app.state.runtime_http.aclose()

    token = host.deliver.await_args.kwargs["session_token"]
    assert host.deliver.await_args.kwargs["allow_over_budget"] is True
    assert host.deliver.await_args.kwargs["require_idle"] is True
    assert host.deliver.await_args.kwargs["require_active"] is True
    assert host.deliver.await_args.kwargs["expected_last_result_at"] == 12.0
    assert host.deliver.await_args.kwargs["role"] == "kanban"
    assert host.deliver.await_args.kwargs["command_artifacts"] == {
        "mode": "enforce",
        "card": "issue-1",
    }
    assert result["session_id"] == "thread-abc"
    assert bound == {token: "codex:codex:thread-abc"}


async def test_observe_context_decision_is_frozen_without_runtime_maintenance(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=100_000,
    )
    state.sessions_scanned("codex:codex")
    calls = []

    async def proxy(_request, method, path, json=None):
        calls.append((method, path, json))
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [
                    SNAPSHOT_ACTIVITY_CLOCK,
                    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
                ],
            }
        return {"accepted": True, "action": "queued", "session_id": "thread-abc"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "observe"}}
                )
                accounts = [
                    Account("codex:codex", "codex", "codex", tmp_path)
                ]

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="continue",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="auto",
            role="kanban",
        ),
    )

    assert result["accepted"] is True
    assert "context_maintenance" not in calls[-1][2]
    assert "context_rollover" not in calls[-1][2]
    assert "expected_session_id" not in calls[-1][2]
    rows = db.list_chat_context_deliveries(source_key)
    assert len(rows) == 1
    assert rows[0].proposed_action == "compact"
    assert rows[0].executed_action == "none"
    assert rows[0].status == "completed"
    db.close()


async def test_off_context_mode_decides_the_boundary_and_executes_nothing(
    tmp_path, monkeypatch
):
    """Off mode records what `enforce` would have done, and does none of it.

    The ledger is the whole point (#1666): a row that only ever says "disabled"
    cannot tell the operator whether turning the mode up would change anything.
    So off mode takes the same snapshot, writes a real proposal and the occupancy
    it decided on, and still hands the runtime the ordinary unmodified payload.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-1"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        # Far over compact_at_tokens: enforce mode would maintain this worker.
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    calls = []

    async def proxy(_request, method, path, json=None):
        calls.append((method, json))
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-1",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        return {"accepted": True, "action": "queued", "session_id": "thread-1"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig()
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="continue",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="auto",
        ),
    )

    assert result["accepted"] is True
    assert [method for method, _ in calls] == ["GET", "POST"]
    payload = calls[-1][1]
    assert payload is not None
    assert payload["message"] == "continue"
    assert payload["fresh"] is False
    assert "context_policy" not in payload
    assert "dispatch_generation" not in payload
    assert "context_maintenance" not in payload
    assert "context_rollover" not in payload
    assert "expected_session_id" not in payload
    row = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d1")
    assert row is not None
    assert (row.mode, row.proposed_action, row.executed_action) == (
        "off",
        "compact",
        "none",
    )
    assert row.reason == "threshold"
    assert row.before_tokens == 100_000
    assert row.source_session_key == source_key
    assert row.status == "completed"
    db.close()


async def test_off_mode_noop_survives_turning_context_maintenance_on(
    tmp_path, monkeypatch
):
    """A delivery answered while the mode was off stays answered when it is on.

    Turning the deployment mode up must not let an already-completed delivery ID
    be decided a second time, which would give the same ID a compact or rollover
    the first attempt never performed.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-1"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        # Far over compact_at_tokens: enforce mode would maintain this worker.
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    calls = []

    async def proxy(_request, method, path, json=None):
        calls.append((method, json))
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-1",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        return {"accepted": True, "action": "queued", "session_id": "thread-1"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig()
                accounts = []

    body = WorkerDeliverRequest(
        key="issue-1",
        message="continue",
        cwd=str(tmp_path),
        delivery_id="poller:d1",
        context_policy="auto",
        role="kanban",
    )
    first = await keyed_deliver(Req, "codex", "codex", body)

    Req.app.state.config = AppConfig.model_validate(
        {"context_management": {"mode": "enforce"}}
    )
    replay = await keyed_deliver(Req, "codex", "codex", body)

    assert first["accepted"] is True
    assert replay["accepted"] is True
    assert replay["session_id"] == first["session_id"]
    # The first delivery decided once and reached the runtime once; the mode change
    # bought no second snapshot, no second delivery, and no maintenance.
    assert [method for method, _ in calls] == ["GET", "POST"]
    row = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d1")
    assert row is not None
    # The proposal is real and frozen, and `enforce` executes nothing from it later.
    assert (row.mode, row.proposed_action, row.executed_action) == (
        "off",
        "compact",
        "none",
    )
    assert row.status == "completed"
    db.close()


async def test_off_mode_noop_is_filed_under_the_chat_it_continues(
    tmp_path, monkeypatch
):
    """The no-op shows up in the continued chat's own diagnostics.

    `/sessions/{key}/context-deliveries` selects by the logical chat id, so a row
    keyed on the poller's worker key would be invisible to the chat it belongs
    to. Off mode now asks the runtime which session the worker is on, but the
    runtime does not always have one — a revival names no worker — and the leg
    being continued is provenance the deck already holds either way.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=10_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    chat = db.ensure_session_chats([source_key])[0]
    calls = []

    async def proxy(_request, method, path, json=None):
        calls.append((method, json))
        return {"accepted": True, "action": "queued", "session_id": "thread-abc"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig()
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="continue",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="auto",
            continues_session_url=f"https://deck.example/chats/{chat.chat_id}",
        ),
    )

    assert result["accepted"] is True
    # The snapshot names no worker, so nothing about the delivery changes.
    assert [method for method, _ in calls] == ["GET", "POST"]
    assert "context_maintenance" not in calls[-1][1]
    assert "context_rollover" not in calls[-1][1]

    chat_id = state.chat_identity_key(source_key)
    assert chat_id == chat.chat_id, "the source leg has a durable chat identity"
    listed = db.list_chat_context_deliveries(chat_id)
    assert [row.delivery_id for row in listed] == ["poller:d1"]
    assert listed[0].source_session_key == source_key
    assert listed[0].source_session_id == "thread-abc"
    assert listed[0].executed_action == "none"
    # No live worker means no occupancy to decide on, and the row says so rather
    # than borrowing the continued leg's number.
    assert listed[0].before_tokens is None
    db.close()


@pytest.mark.parametrize(
    "failure",
    ["snapshot_unreachable", "worker_changed"],
    ids=["snapshot_unreachable", "worker_changed"],
)
async def test_off_mode_deciding_never_refuses_a_delivery(
    tmp_path, monkeypatch, failure
):
    """Reading the runtime must not give off mode a way to fail a delivery.

    Deciding costs one snapshot, and a snapshot can be unreachable or can show a
    worker that has moved on. Under `enforce` both refuse, because a compact or a
    rollover would be aimed at the wrong session. Off mode executes nothing, so it
    freezes the no-op it always froze and lets the delivery through (#1666).
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-1"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    calls = []

    async def proxy(_request, method, path, json=None):
        calls.append((method, json))
        if method == "GET":
            if failure == "snapshot_unreachable":
                raise HTTPException(status_code=503, detail="runtime unavailable")
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-1",
                        # The worker finished a turn since the poller looked.
                        "last_result_at": 44.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        return {"accepted": True, "action": "queued", "session_id": "thread-1"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig()
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="continue",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="auto",
            expected_last_result_at=12.0,
        ),
    )

    assert result.get("reason") != "worker_changed"
    assert result["accepted"] is True
    assert [method for method, _ in calls] == ["GET", "POST"]
    row = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d1")
    assert row is not None
    assert (row.mode, row.proposed_action, row.executed_action, row.reason) == (
        "off",
        "none",
        "none",
        "disabled",
    )
    db.close()


async def test_public_off_policy_still_bypasses_the_context_ledger(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")

    async def proxy(_request, method, path, json=None):
        return {"accepted": True, "action": "queued", "session_id": "thread-1"}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="continue",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="off",
        ),
    )

    assert result["accepted"] is True
    assert db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d1") is None
    db.close()


async def test_a_capacity_refusal_does_not_poison_its_delivery_id(tmp_path, monkeypatch):
    """#1491, driven from #1488's own `cycle.log`.

    The engine reuses one delivery ID for a card that was refused for host capacity, and
    re-renders its brief and mints a fresh generation on each retry. The ledger froze the
    refused attempt's request hash, so every retry came back `delivery_id_conflict` and the
    card burned its three attempts and parked in *Blocked* with no worker for an hour.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {"workers": {}}
        posts.append(json)
        if len(posts) == 1:
            return {
                "accepted": False,
                "action": "rejected",
                "reason": "at_capacity",
                "provider_write_state": "not_started",
            }
        return {
            "accepted": True,
            "action": "spawned",
            "session_id": "thread-1",
            "provider_write_state": "started",
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    def directive(generation, others):
        return WorkerDeliverRequest(
            key="agentdeck#1488",
            message=f"Work eerovil/agentdeck#1488.\n\nOther in-flight changes: {others}",
            cwd=str(tmp_path),
            delivery_id="poller:6c4fcb63",
            context_policy="auto",
            role="kanban",
        )

    refused = await keyed_deliver(
        Req, "codex", "codex", directive("generation-1", "agentdeck#1487")
    )
    assert refused["reason"] == "at_capacity"
    parked = db.get_context_delivery("codex", "codex:codex", "agentdeck#1488", "poller:6c4fcb63")
    assert parked is not None and parked.status == "clean_failed"

    # The retry the engine actually sends: a re-rendered brief under a fresh generation.
    retried = await keyed_deliver(
        Req, "codex", "codex", directive("generation-2", "agentdeck#1487, agentdeck#1490")
    )

    assert retried["accepted"] is True
    assert retried["action"] == "spawned"
    assert len(posts) == 2, "the retry reached the runtime instead of being refused"
    row = db.get_context_delivery("codex", "codex:codex", "agentdeck#1488", "poller:6c4fcb63")
    assert row is not None
    assert row.status == "completed"
    assert row.attempt == 2, "the same ledger row, re-armed — not a second delivery"
    db.close()


async def test_the_same_request_still_replays_a_clean_refusal(tmp_path, monkeypatch):
    """#1493's frozen answer survives #1491's re-arm: only a *changed* request re-arms.

    A caller that asks the identical question twice under one delivery ID gets the same
    answer back without reaching the runtime again. It is the poller's re-rendered brief —
    a different request hash — that means a genuinely new attempt at the same card.

    The refusal here is `over_budget`, which the engine does not re-send under the same
    delivery id, so freezing it is correct. Refusals it *does* re-send are carved out by
    #1560 and covered by the two tests below.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {"workers": {}}
        posts.append(json)
        return {
            "accepted": False,
            "action": "rejected",
            "reason": "over_budget",
            "provider_write_state": "not_started",
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    def directive():
        return WorkerDeliverRequest(
            key="agentdeck#1488",
            message="Work eerovil/agentdeck#1488.",
            cwd=str(tmp_path),
            delivery_id="poller:6c4fcb63",
            context_policy="auto",
            role="kanban",
        )

    first = await keyed_deliver(Req, "codex", "codex", directive())
    again = await keyed_deliver(Req, "codex", "codex", directive())

    assert first["reason"] == "over_budget"
    assert again["reason"] == "over_budget"
    assert len(posts) == 1, "an unchanged request was decided again instead of replayed"
    row = db.get_context_delivery("codex", "codex:codex", "agentdeck#1488", "poller:6c4fcb63")
    assert row is not None and row.attempt == 1
    db.close()


async def test_an_unchanged_retry_of_a_host_state_refusal_is_decided_again(
    tmp_path, monkeypatch
):
    """#1560: `at_capacity` must never be frozen into the ledger.

    The engine re-sends a capacity refusal under the *same* lease and delivery id, because
    the runtime proved it wrote nothing -- so the request is byte-identical every time and
    #1491's re-arm, which keys on a changed request hash, never fired. The ledger replayed
    the first refusal instead, and the card was refused forever on a verdict about a host
    that had long since freed up: three cards sat `at_capacity` for over four hours against
    a pool that was half idle, never reaching the runtime again to find out.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    posts = []
    replies = [
        {
            "accepted": False,
            "action": "rejected",
            "reason": "at_capacity",
            "provider_write_state": "not_started",
        },
        {"accepted": True, "action": "spawned", "session_id": "session-1"},
    ]

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {"workers": {}}
        posts.append(json)
        return replies[min(len(posts) - 1, len(replies) - 1)]

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    def directive():
        return WorkerDeliverRequest(
            key="agentdeck#1560",
            message="Work eerovil/agentdeck#1560.",
            cwd=str(tmp_path),
            delivery_id="poller:c0ffee01",
            context_policy="auto",
            role="kanban",
        )

    first = await keyed_deliver(Req, "codex", "codex", directive())
    again = await keyed_deliver(Req, "codex", "codex", directive())

    assert first["reason"] == "at_capacity"
    assert len(posts) == 2, "the capacity refusal was replayed instead of decided again"
    assert again["accepted"] is True, "the card stayed refused after the pool freed up"
    row = db.get_context_delivery("codex", "codex:codex", "agentdeck#1560", "poller:c0ffee01")
    assert row is not None and row.attempt == 2
    db.close()


async def test_a_host_state_refusal_that_may_have_written_still_replays(
    tmp_path, monkeypatch
):
    """#2136: re-deciding a host-state refusal stops where the evidence stops.

    `worker_changed` is re-decided rather than frozen (#1560), but only for a row that
    provably did nothing. Here the rollover already created the successor chat before the
    refusal, so `_should_rearm` declines and the frozen answer is all the caller may be
    given. Refusing to replay this row as well would leave nothing to return, and the
    retry would fall through to a fresh delivery that creates a second successor -- the
    at-most-once invariant, broken by a fix for a different bug.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {"workers": {}}
        posts.append(json)
        return {
            "accepted": False,
            "action": "rejected",
            "reason": "worker_changed",
            "provider_write_state": "not_started",
            "successor_created": True,
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    def directive():
        return WorkerDeliverRequest(
            key="agentdeck#2136",
            message="Work eerovil/agentdeck#2136.",
            cwd=str(tmp_path),
            delivery_id="poller:2136beef",
            context_policy="auto",
            role="kanban",
        )

    first = await keyed_deliver(Req, "codex", "codex", directive())
    again = await keyed_deliver(Req, "codex", "codex", directive())

    assert first["reason"] == "worker_changed"
    assert again["reason"] == "worker_changed"
    assert again["accepted"] is False
    assert len(posts) == 1, "a refusal that already made a successor was delivered again"
    row = db.get_context_delivery("codex", "codex:codex", "agentdeck#2136", "poller:2136beef")
    assert row is not None and row.attempt == 1
    db.close()


def test_same_delivery_retries_are_never_frozen_by_the_ledger():
    """The engine's retry set and the ledger's re-decide set are one decision (#1560).

    `poller_engine` deliberately imports nothing from the package above it, so the two
    cannot share a constant. They can still be held in step here: every refusal the engine
    re-sends under the same delivery id must be one the ledger declines to freeze, or that
    refusal becomes permanent for the card the moment it is written.
    """
    from agentdeck.context_delivery import RE_DECIDED_REFUSALS
    from agentdeck.poller_engine.cycle import SAME_DELIVERY_RETRIES

    # `retryable_same_delivery` is the one exception, and it is deliberate: the provider
    # asked for that resume and context maintenance still stands behind the row, so
    # `_resume_context_delivery` owns it rather than deciding it again (#1493).
    assert SAME_DELIVERY_RETRIES - RE_DECIDED_REFUSALS == {"retryable_same_delivery"}
    assert RE_DECIDED_REFUSALS < SAME_DELIVERY_RETRIES


def test_the_refusal_the_engine_calls_permanent_is_one_the_ledger_never_re_decides():
    """The mirror of the above, and the other way a card gets stuck (#1886).

    The engine parks a dispatch at the first `delivery_id_conflict` because no later attempt
    can be answered differently. That is only true while the ledger declines to re-decide it:
    the day that refusal joins the re-decided set, a card that could have recovered would be
    parked instead. Held in step here for the same reason the retry set is.
    """
    from agentdeck.context_delivery import RE_DECIDED_REFUSALS
    from agentdeck.poller_engine.cycle import (
        SAME_DELIVERY_RETRIES,
        TERMINAL_SAME_DELIVERY_REFUSAL,
    )

    assert TERMINAL_SAME_DELIVERY_REFUSAL not in RE_DECIDED_REFUSALS
    assert TERMINAL_SAME_DELIVERY_REFUSAL not in SAME_DELIVERY_RETRIES


async def test_an_uncertain_delivery_still_fences_a_re_rendered_retry(tmp_path, monkeypatch):
    """At-most-once is untouched: only a provably unwritten refusal may be re-armed."""
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {"workers": {}}
        posts.append(json)
        return {
            "accepted": True,
            "action": "uncertain",
            "session_id": "thread-1",
            "provider_write_state": "unknown",
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    def directive(generation):
        return WorkerDeliverRequest(
            key="agentdeck#1488",
            message=f"Work the card under {generation}",
            cwd=str(tmp_path),
            delivery_id="poller:6c4fcb63",
            context_policy="auto",
            role="kanban",
        )

    await keyed_deliver(Req, "codex", "codex", directive("generation-1"))
    conflict = await keyed_deliver(Req, "codex", "codex", directive("generation-2"))

    assert conflict["accepted"] is False
    assert conflict["reason"] == "delivery_id_conflict"
    assert len(posts) == 1, "an unknown outcome is never sent a second time"
    db.close()


async def test_enforced_compaction_executes_once_and_replays_the_frozen_result(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    posts = []
    gets = 0

    async def proxy(_request, method, path, json=None):
        nonlocal gets
        if method == "GET":
            gets += 1
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 11.0 + gets,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        if len(posts) == 1:
            return {
                "accepted": False,
                "action": "rejected",
                "reason": "worker_changed",
                "session_id": "thread-abc",
                "provider_write_state": "not_started",
            }
        return {
            "accepted": True,
            "action": "queued",
            "reason": None,
            "session_id": "thread-abc",
            "provider_write_state": "started",
            "successor_created": False,
            "retryable_same_delivery": False,
            "context_maintenance_status": "succeeded",
            "context_maintenance_reason": None,
            "context_rollover": False,
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    body = WorkerDeliverRequest(
        key="issue-1",
        message="continue",
        cwd=str(tmp_path),
        delivery_id="poller:d1",
        context_policy="auto",
        role="kanban",
    )
    changed = await keyed_deliver(Req, "codex", "codex", body)
    first = await keyed_deliver(Req, "codex", "codex", body)
    replay = await keyed_deliver(Req, "codex", "codex", body)

    assert changed["reason"] == "worker_changed"
    assert first == replay
    assert len(posts) == 2
    assert gets == 2
    assert posts[1]["context_maintenance"] is True
    assert posts[1]["require_idle"] is True
    assert posts[1]["expected_session_id"] == "thread-abc"
    assert posts[1]["expected_last_result_at"] == 13.0
    row = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d1")
    assert row is not None and row.status == "completed"
    assert row.attempt == 2
    assert row.maintenance_result == "succeeded"
    db.close()


@pytest.mark.parametrize(
    ("observed_tokens", "current_tokens", "expected_action"),
    [(55_000, 90_000, "compact"), (80_000, 120_000, "rollover")],
)
async def test_real_post_compact_observation_controls_the_next_cycle(
    tmp_path, monkeypatch, observed_tokens, current_tokens, expected_action
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / f"cycle-{expected_action}.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=current_tokens,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    previous = db.reserve_context_delivery(
        ContextDelivery(
            action_id="first-compact",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="issue-1",
            delivery_id="poller:first",
            request_sha256="a" * 64,
            chat_id="chat-1",
            source_session_key=source_key,
            source_session_id="thread-abc",
            mode="enforce",
            proposed_action="compact",
            executed_action="compact",
            reason="threshold",
        )
    )
    assert previous is not None
    assert db.mark_completed(
        previous.action_id,
        maintenance_result="succeeded",
        provider_write_state="started",
    )
    completed = db.get_context_delivery(
        "codex", "codex:codex", "issue-1", "poller:first"
    )
    assert completed is not None and completed.maintenance_completed_at
    assert db.record_context_observation(
        "codex",
        "codex:codex",
        "issue-1",
        "thread-abc",
        observed_tokens,
        observed_at=datetime.fromisoformat(completed.maintenance_completed_at)
        + timedelta(seconds=1),
        source_version=1,
        rearm_below_tokens=60_000,
    )
    posts = []

    async def proxy(_request, method, _path, json=None):
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        return {
            "accepted": True,
            "action": "spawned" if json.get("context_rollover") else "queued",
            "session_id": "target-id" if json.get("context_rollover") else "thread-abc",
            "provider_write_state": "started",
            "successor_created": bool(json.get("context_rollover")),
            "context_maintenance_status": (
                "succeeded" if json.get("context_maintenance") else None
            ),
            "context_rollover": bool(json.get("context_rollover")),
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="next",
            cwd=str(tmp_path),
            delivery_id="poller:next",
            context_policy="auto",
        ),
    )

    assert result["accepted"] is True
    assert len(posts) == 1
    assert posts[0]["context_maintenance"] is (expected_action == "compact")
    assert posts[0]["context_rollover"] is (expected_action == "rollover")
    db.close()


async def _deliver_with_chat_model_preference(
    tmp_path, monkeypatch, *, live: bool, preference: str = "gpt-5.6-sol"
):
    """Deliver one poller nudge to a chat whose operator asked for another model."""
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / f"preference-{live}.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=1_000,
        cwd=str(tmp_path),
    )
    state.session_scan_times["codex:codex"] = datetime.now(UTC)
    db.record_chat_model_preference(state.chat_identity_key(source_key), preference)
    posts = []

    async def proxy(_request, method, _path, json=None):
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 12.0,
                        "turn_active": False,
                        "live": live,
                        "model": "gpt-5.6-codex",
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        return {
            "accepted": True,
            "action": "spawned" if json.get("context_rollover") else "queued",
            "session_id": "target-id" if json.get("context_rollover") else "thread-abc",
            "provider_write_state": "started",
            "successor_created": bool(json.get("context_rollover")),
            "context_rollover": bool(json.get("context_rollover")),
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                # Maintenance off: the code default, and what this host runs.
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "off"}}
                )
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="next",
            cwd=str(tmp_path),
            delivery_id="poller:next",
            context_policy="auto",
        ),
    )
    return db, result, posts


async def test_a_model_preference_rolls_a_live_chat_over_with_maintenance_off(
    tmp_path, monkeypatch
):
    # The live process is the whole problem: a nudge into one is a stdin write that
    # drops the model silently, so the change has to force a leg boundary (#1665).
    db, result, posts = await _deliver_with_chat_model_preference(
        tmp_path, monkeypatch, live=True
    )
    assert result["accepted"] is True
    assert len(posts) == 1
    assert posts[0]["context_rollover"] is True
    assert posts[0]["model"] == "gpt-5.6-sol"
    frozen = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:next")
    assert frozen is not None
    assert frozen.reason == "model_preference"
    # The outgoing leg's own model must not win the freeze, or the successor would
    # keep exactly the model the operator asked it to leave.
    assert frozen.effective_model == "gpt-5.6-sol"
    db.close()


async def test_a_chat_with_no_live_process_takes_the_preference_without_a_rollover(
    tmp_path, monkeypatch
):
    # Nothing would swallow it here: the next spawn or resume reads the payload, and
    # a rollover would throw away a session that could simply have been resumed.
    db, result, posts = await _deliver_with_chat_model_preference(
        tmp_path, monkeypatch, live=False
    )
    assert result["accepted"] is True
    assert posts[0].get("context_rollover") is not True
    assert posts[0]["model"] == "gpt-5.6-sol"
    db.close()


async def test_a_revival_with_no_worker_still_finds_the_chats_preference(
    tmp_path, monkeypatch
):
    """The runtime knows no worker, so only the continued leg names the chat (#1665).

    This is the revival case: nothing in the snapshot, so `source_key` is empty and
    the preference has to be read through the leg the delivery says it continues.
    Looking it up under the worker key instead asks for the chat identity of a card
    ref, which is not a leg, and silently loses the operator's choice.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "revival.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=1_000,
        cwd=str(tmp_path),
    )
    state.session_scan_times["codex:codex"] = datetime.now(UTC)
    db.record_chat_model_preference(state.chat_identity_key(source_key), "gpt-5.6-sol")
    posts = []

    async def proxy(_request, method, _path, json=None):
        if method == "GET":
            # The revival case: the runtime holds no worker for this card at all.
            return {
                "workers": {},
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        return {
            "accepted": True,
            "action": "spawned",
            "session_id": "thread-abc",
            "provider_write_state": "started",
            "successor_created": False,
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "off"}}
                )
                accounts = []

    result = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="agentdeck#1665",
            message="carry on",
            cwd=str(tmp_path),
            delivery_id="poller:revive",
            context_policy="auto",
            continues_session_url=f"https://deck.example/sessions/{source_key}",
        ),
    )

    assert result["accepted"] is True
    assert posts[0]["model"] == "gpt-5.6-sol"
    frozen = db.get_context_delivery(
        "codex", "codex:codex", "agentdeck#1665", "poller:revive"
    )
    assert frozen is not None and frozen.preferred_model == "gpt-5.6-sol"
    db.close()


@pytest.mark.parametrize("current_mode", ["off", "observe"])
@pytest.mark.parametrize("action", ["compact", "rollover"])
async def test_frozen_maintenance_retry_ignores_current_mode(
    tmp_path, monkeypatch, current_mode, action
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / f"{current_mode}-{action}.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    body = WorkerDeliverRequest(
        key="issue-1",
        message="continue",
        cwd=str(tmp_path),
        delivery_id="poller:frozen",
        context_policy="auto",
        model="gpt-5.6-sol",
        effort="high",
        role="kanban",
    )
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="frozen-action",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="issue-1",
            delivery_id="poller:frozen",
            request_sha256=_context_hash("codex", "codex", body),
            chat_id="chat-1",
            source_session_key=source_key,
            source_session_id="thread-abc",
            mode="enforce",
            proposed_action=action,
            executed_action=action,
            reason="threshold",
            expected_session_id="thread-abc",
            expected_last_result_at=42.0,
            effective_cwd="/frozen/cwd",
            effective_model="frozen-model",
            effective_effort="medium",
            effective_permission_mode="workspace-write",
            effective_role="kanban",
            effective_delegated=True,
            session_token="frozen-token",
            handoff_text="frozen handoff" if action == "rollover" else None,
        )
    )
    assert row is not None
    assert db.mark_clean_failed(
        row.action_id,
        final_accepted=False,
        final_action="rejected",
        final_reason="first_message_failed",
        provider_write_state="not_started",
        retryable_same_delivery=True,
    )
    calls = []

    async def proxy(_request, method, _path, json=None):
        calls.append((method, json))
        return {
            "accepted": False,
            "action": "rejected",
            "reason": "first_message_failed",
            "provider_write_state": "not_started",
            "successor_created": False,
            "retryable_same_delivery": True,
            "context_rollover": action == "rollover",
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": current_mode}}
                )
                accounts = []

    result = await keyed_deliver(Req, "codex", "codex", body)

    assert result["retryable_same_delivery"] is True
    assert [method for method, _payload in calls] == ["POST"]
    payload = calls[0][1]
    assert payload["session_token"] == "frozen-token"
    assert payload["expected_session_id"] == "thread-abc"
    assert payload["expected_last_result_at"] == 42.0
    assert payload["context_maintenance"] is (action == "compact")
    assert payload["context_rollover"] is (action == "rollover")
    if action == "rollover":
        assert payload["message"] == "frozen handoff\n\ncontinue"
        assert payload["cwd"] == "/frozen/cwd"
        assert payload["model"] == "frozen-model"
        assert payload["effort"] == "medium"
        assert payload["permission_mode"] == "workspace-write"
        assert payload["delegated"] is True
    db.close()


def test_frozen_compact_retry_keeps_its_source_leg(tmp_path):
    from agentdeck.web.routes_api import (
        WorkerDeliverRequest,
        _resume_context_delivery,
    )

    db = Db(tmp_path / "frozen-source.db")
    frozen = ContextDelivery(
        action_id="frozen-action",
        attempt=1,
        provider_id="codex",
        account_key="codex:codex",
        worker_key="issue-1",
        delivery_id="poller:frozen",
        request_sha256="a" * 64,
        chat_id="chat-1",
        source_session_key="codex:codex:source-a",
        source_session_id="source-a",
        mode="enforce",
        proposed_action="compact",
        executed_action="compact",
        reason="threshold",
        expected_session_id="source-a",
    )

    _payload, _row, successor_source = _resume_context_delivery(
        db,
        WorkerDeliverRequest(key="issue-1", message="continue"),
        frozen,
        token="token",
        continued_source="codex:codex:source-b",
        use_frozen_source=True,
    )

    assert successor_source == "codex:codex:source-a"
    db.close()


@pytest.mark.parametrize("current_mode", ["off", "observe", "enforce"])
async def test_changed_context_request_hash_conflicts_in_every_mode(
    tmp_path, monkeypatch, current_mode
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / f"hash-{current_mode}.db")
    body = WorkerDeliverRequest(
        key="issue-1",
        message="original",
        cwd=str(tmp_path),
        delivery_id="poller:frozen",
        context_policy="auto",
    )
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="frozen-action",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="issue-1",
            delivery_id="poller:frozen",
            request_sha256=_context_hash("codex", "codex", body),
            chat_id="chat-1",
            source_session_key="codex:codex:thread-abc",
            source_session_id="thread-abc",
            mode="enforce",
            proposed_action="compact",
            executed_action="compact",
            reason="threshold",
        )
    )
    assert row is not None
    proxy = AsyncMock()
    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate(
                    {"context_management": {"mode": current_mode}}
                )
                accounts = []

    result = await keyed_deliver(
        Req, "codex", "codex", body.model_copy(update={"message": "changed"})
    )

    assert result["reason"] == "delivery_id_conflict"
    proxy.assert_not_awaited()
    db.close()


async def test_enforced_rollover_builds_handoff_and_links_the_successor(
    tmp_path, monkeypatch, caplog
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "claude_code:main:source-id"
    source = Session(
        key=source_key,
        account_key="claude_code:main",
        session_id="source-id",
        status=SessionStatus.IDLE,
        context_tokens=130_000,
        cwd=str(tmp_path),
        title="Issue 1471",
    )
    state.sessions[source_key] = source
    state.sessions_scanned("claude_code:main")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            session_id = "target-id" if posts else "source-id"
            return {
                "workers": {
                    "issue-1": {
                        "session_id": session_id,
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        if len(posts) == 1:
            return {
                "accepted": False,
                "action": "rejected",
                "reason": "first_message_failed",
                "session_id": "target-id",
                "provider_write_state": "not_started",
                "successor_created": True,
                "retryable_same_delivery": True,
                "context_rollover": True,
            }
        return {
            "accepted": True,
            "action": "spawned",
            "session_id": "target-id",
            "provider_write_state": "started",
            "successor_created": True,
            "context_rollover": True,
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    body = WorkerDeliverRequest(
        key="issue-1",
        message="continue",
        cwd=str(tmp_path),
        delivery_id="poller:d2",
        context_policy="auto",
        role="kanban",
    )
    with caplog.at_level(logging.INFO, logger="agentdeck.web.routes_api"):
        failed = await keyed_deliver(Req, "claude_code", "main", body)
        blocked = await keyed_deliver(
            Req,
            "claude_code",
            "main",
            body.model_copy(update={"delivery_id": "poller:d3"}),
        )
        result = await keyed_deliver(Req, "claude_code", "main", body)

    assert failed["retryable_same_delivery"] is True
    assert blocked["reason"] == "context_operation_pending"
    assert result["session_id"] == "target-id"
    assert posts[0]["context_rollover"] is True
    assert posts[0]["fresh"] is True
    assert posts[0]["message"].endswith("\n\ncontinue")
    assert "Issue 1471" in posts[0]["message"]
    assert posts[1]["expected_session_id"] == "source-id"
    assert state.chat_identity_key(source_key) == state.chat_identity_key(
        "claude_code:main:target-id"
    )
    row = db.get_context_delivery(
        "claude_code", "claude_code:main", "issue-1", "poller:d2"
    )
    assert row is not None and row.target_session_key == "claude_code:main:target-id"
    # A rollover that worked used to leave no trace at all: the only two log
    # lines in this path were failures (#1667).
    rollovers = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("context rollover:")
    ]
    assert rollovers, "a successful rollover logged nothing"
    # The chat as it was named when the handoff was built — the same value the
    # handoff itself carries. A chat rolling over for the first time has no
    # durable identity yet, so that name is its source leg's key.
    assert f"chat={source_key}" in rollovers[0]
    assert f"source_leg={source_key}" in rollovers[0]
    assert "reason=threshold" in rollovers[0]
    db.close()


@pytest.mark.parametrize("retry_mode", ["off", "observe", "enforce"])
async def test_uncertain_context_delivery_reconciles_receipt_without_resend(
    tmp_path, monkeypatch, retry_mode
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    state.sessions[source_key] = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=100_000,
        cwd=str(tmp_path),
    )
    state.sessions_scanned("codex:codex")
    posts = 0

    async def proxy(_request, method, path, json=None):
        nonlocal posts
        if "delivery-receipt" in path:
            return {
                "receipt": {
                    "accepted": True,
                    "action": "queued",
                    "session_id": "thread-abc",
                    "provider_write_state": "started",
                    "context_maintenance_status": "succeeded",
                }
            }
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts += 1
        raise HTTPException(status_code=503, detail="lost runtime response")

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    body = WorkerDeliverRequest(
        key="issue-1",
        message="continue",
        cwd=str(tmp_path),
        delivery_id="poller:uncertain",
        context_policy="auto",
        role="kanban",
    )
    with pytest.raises(HTTPException):
        await keyed_deliver(Req, "codex", "codex", body)
    Req.app.state.config = AppConfig.model_validate(
        {"context_management": {"mode": retry_mode}}
    )
    reconciled = await keyed_deliver(Req, "codex", "codex", body)

    assert posts == 1
    assert reconciled["accepted"] is True
    row = db.get_context_delivery(
        "codex", "codex:codex", "issue-1", "poller:uncertain"
    )
    assert row is not None and row.status == "completed"
    db.close()


async def test_command_artifact_runtime_route_checks_session_and_card(tmp_path):
    app = _runtime_app(state_dir=str(tmp_path / "workers"))
    store = app.state.command_artifacts
    now = datetime.now(UTC)
    captured = store.capture(
        context={
            "session_key": "claude_code:main:session-1",
            "worker_key": "repo#1484",
            "access_sha256": hashlib.sha256(b"opaque-session-token").hexdigest(),
            "policy": {
                "mode": "enforce",
                "artifact_slice_max_bytes": 8,
                "artifact_slice_call_limit": 2,
            },
        },
        command="printf private",
        stdout=b"private",
        stderr=b"",
        exit_code=0,
        term_signal=None,
        timed_out=False,
        started_at=now,
        finished_at=now,
    )
    artifact_id = captured.metadata["artifact_id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        denied = await client.post(
            f"/command-artifacts/{artifact_id}/slice",
            json={
                "session_key": "claude_code:main:other",
                "card": "repo#1484",
                "session_token": "opaque-session-token",
                "stream": "stdout",
                "max_bytes": 7,
            },
        )
        allowed = await client.post(
            f"/command-artifacts/{artifact_id}/slice",
            json={
                "session_key": "claude_code:main:session-1",
                "card": "repo#1484",
                "session_token": "opaque-session-token",
                "stream": "stdout",
                "max_bytes": 7,
            },
        )
        denied_full = await client.post(
            f"/command-artifacts/{artifact_id}/full",
            json={
                "session_key": "claude_code:main:session-1",
                "card": "other#1",
                "session_token": "opaque-session-token",
            },
        )
        allowed_full = await client.post(
            f"/command-artifacts/{artifact_id}/full",
            json={
                "session_key": "claude_code:main:session-1",
                "card": "repo#1484",
                "session_token": "opaque-session-token",
            },
        )

    assert denied.status_code == 403
    assert allowed.json()["text"] == "private"
    assert denied_full.status_code == 403
    assert allowed_full.json()["stdout_base64"] == "cHJpdmF0ZQ=="


async def test_worker_snapshot_reports_uncovered_artifact_shapes(tmp_path):
    app = _runtime_app(state_dir=str(tmp_path / "workers"))
    host = MagicMock()
    host.snapshot.return_value = {
        "workers": {"repo#1484": {"session_id": "session-1"}}
    }
    app.state.claude_workers.hosts["main"] = host
    app.state.command_artifacts.observe_uncovered(
        {
            "session_key": "claude_code:main:session-1",
            "worker_key": "repo#1484",
            "policy": {"phase": "execute"},
        },
        "sleep 1 & wait",
        "background_shell",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/workers/accounts/claude_code/main/workers")

    worker = response.json()["workers"]["repo#1484"]
    assert worker["model_visible_tool_bytes"] is None
    assert worker["command_artifact_coverage"]["complete"] is False
    assert worker["command_artifact_coverage"]["uncovered"][0]["reason"] == "background_shell"


async def test_worker_snapshot_reads_the_artifact_store_once_not_once_per_worker(
    tmp_path, monkeypatch
):
    """#1553: the artifact scan is per request, and never per worker.

    A worker registry is not pruned, so asking per worker made one request cost
    `workers x artifacts` synchronous parses on the event loop -- 245,626 of them at
    the 643 workers and 382 artifacts this hit in production. The runtime stopped
    accepting connections, its socket backlog grew without ever draining, and the
    whole deck looked dead while it was merely busy.
    """
    app = _runtime_app(state_dir=str(tmp_path / "workers"))
    host = MagicMock()
    host.snapshot.return_value = {
        "workers": {f"repo#{n}": {"session_id": f"session-{n}"} for n in range(25)}
    }
    app.state.claude_workers.hosts["main"] = host

    store = app.state.command_artifacts
    scans = 0
    walk = store._metadata_paths

    def counted_walk():
        nonlocal scans
        scans += 1
        return walk()

    monkeypatch.setattr(store, "_metadata_paths", counted_walk)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/workers/accounts/claude_code/main/workers")

    assert response.status_code == 200
    assert len(response.json()["workers"]) == 25
    assert scans == 1


async def test_worker_snapshot_does_not_read_the_artifact_store_with_no_workers(
    tmp_path, monkeypatch
):
    app = _runtime_app(state_dir=str(tmp_path / "workers"))
    host = MagicMock()
    host.snapshot.return_value = {"workers": {}}
    app.state.claude_workers.hosts["main"] = host

    store = app.state.command_artifacts
    scans = 0
    walk = store._metadata_paths

    def counted_walk():
        nonlocal scans
        scans += 1
        return walk()

    monkeypatch.setattr(store, "_metadata_paths", counted_walk)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/workers/accounts/claude_code/main/workers")

    assert response.status_code == 200
    assert scans == 0


async def test_pre_write_runtime_stage_reopens_only_the_same_frozen_delivery(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api

    db = Db(tmp_path / "prepared.db")
    state = AppState(db=db)
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="prepared-action",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="agentdeck#1479",
            delivery_id="poller:dispatch",
            request_sha256="a" * 64,
            chat_id="chat",
            source_session_key="codex:codex:source",
            source_session_id="source",
            mode="enforce",
            proposed_action="none",
            executed_action="none",
            reason="below_threshold",
            runtime_fingerprint="runtime-fingerprint",
        )
    )
    assert row is not None
    assert db.mark_uncertain(row.action_id, provider_write_state="unknown")

    async def proxy(*_args, **_kwargs):
        return {
            "receipt": {
                "fingerprint": "runtime-fingerprint",
                "delivery_stage": "prepared_before_write",
            }
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                config = AppConfig()

    result = await routes_api._reconcile_uncertain_context_delivery(Req, state, row)

    assert result["reason"] == "context_delivery_retryable"
    stored = db.get_context_delivery(
        "codex", "codex:codex", "agentdeck#1479", "poller:dispatch"
    )
    assert stored is not None
    assert stored.status == "clean_failed"
    assert stored.retryable_same_delivery is True
    assert stored.provider_write_state == "not_started"
    db.close()


async def test_exact_provider_turn_receipt_finalizes_without_resend(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api

    db = Db(tmp_path / "turn.db")
    state = AppState(db=db)
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="turn-action",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="agentdeck#1479",
            delivery_id="poller:dispatch",
            request_sha256="a" * 64,
            chat_id="chat",
            source_session_key="codex:codex:source",
            source_session_id="source",
            mode="enforce",
            proposed_action="none",
            executed_action="none",
            reason="below_threshold",
            runtime_fingerprint="runtime-fingerprint",
        )
    )
    assert row is not None
    assert db.mark_uncertain(row.action_id, provider_write_state="unknown")

    async def proxy(*_args, **_kwargs):
        return {
            "receipt": {
                "fingerprint": "runtime-fingerprint",
                "delivery_stage": "provider_turn_identified",
                "provider_turn_id": "turn-7",
                "session_id": "source",
            }
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                config = AppConfig()

    result = await routes_api._reconcile_uncertain_context_delivery(Req, state, row)

    assert result["accepted"] is True
    assert result["provider_turn_id"] == "turn-7"
    stored = db.get_context_delivery(
        "codex", "codex:codex", "agentdeck#1479", "poller:dispatch"
    )
    assert stored is not None and stored.status == "completed"
    assert stored.delivery_stage == "provider_turn_identified"
    db.close()




async def test_a_new_run_uses_the_prior_noop_delivery_for_rollover(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")
    state = AppState(db=db)
    source_key = "codex:codex:thread-abc"
    source = Session(
        key=source_key,
        account_key="codex:codex",
        session_id="thread-abc",
        status=SessionStatus.IDLE,
        context_tokens=10_000,
        cwd=str(tmp_path),
    )
    state.sessions[source_key] = source
    state.sessions_scanned("codex:codex")
    posts = []

    async def proxy(_request, method, path, json=None):
        if method == "GET":
            return {
                "workers": {
                    "issue-1": {
                        "session_id": "thread-abc",
                        "last_result_at": 12.0,
                        "turn_active": False,
                    }
                },
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            }
        posts.append(json)
        return {
            "accepted": True,
            "action": "spawned" if json.get("context_rollover") else "queued",
            "session_id": "target-id" if json.get("context_rollover") else "thread-abc",
            "provider_write_state": "started",
            "successor_created": bool(json.get("context_rollover")),
            "context_rollover": bool(json.get("context_rollover")),
        }

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = state
                config = AppConfig.model_validate(
                    {"context_management": {"mode": "enforce"}}
                )
                accounts = []

    first = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="first",
            cwd=str(tmp_path),
            delivery_id="poller:d1",
            context_policy="auto",
            work_id="github_issue:eerovil/agentdeck#1",
            run_id="generation-1",
        ),
    )
    source.context_tokens = 100_000
    second = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="issue-1",
            message="second",
            cwd=str(tmp_path),
            delivery_id="poller:d2",
            context_policy="auto",
            work_id="github_issue:eerovil/agentdeck#1",
            run_id="generation-2",
        ),
    )

    assert first["session_id"] == "thread-abc"
    assert second["session_id"] == "target-id"
    assert "context_maintenance" not in posts[0]
    assert posts[1]["context_rollover"] is True
    row = db.get_context_delivery("codex", "codex:codex", "issue-1", "poller:d2")
    assert row is not None and row.reason == "new_generation"
    db.close()


async def test_a_delivery_frozen_before_2194_still_replays(tmp_path, monkeypatch):
    """Review of #2212: a completed row written before #2194 replays under its delivery id.

    Its request hash was taken with ``dispatch_generation`` and ``phase_delivery_id`` in the
    payload. The hash below is what that code produced for this exact request.
    """
    from agentdeck.web import routes_api
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "legacy.db")
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="legacy-action",
            attempt=1,
            provider_id="codex",
            account_key="codex:codex",
            worker_key="agentdeck#1488",
            delivery_id="poller:legacy",
            request_sha256="763b8ef41d058358602316d0f22183628ab6d3de01fa1c4ac7a597e3eeb48299",
            chat_id="chat",
            source_session_key="codex:codex:thread-1",
            source_session_id="thread-1",
            mode="off",
            proposed_action="none",
            executed_action="none",
            reason="disabled",
        )
    )
    assert row is not None
    assert db.mark_completed(
        row.action_id, final_accepted=True, final_action="queued", final_session_id="thread-1"
    )
    posts = []

    async def proxy(_request, method, path, json=None):
        posts.append((method, path))
        return {"workers": {}}

    monkeypatch.setattr(routes_api, "_runtime_proxy", proxy)

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    replay = await keyed_deliver(
        Req,
        "codex",
        "codex",
        WorkerDeliverRequest(
            key="agentdeck#1488",
            message="carry on",
            cwd="/work",
            delivery_id="poller:legacy",
            context_policy="auto",
            role="kanban",
        ),
    )

    assert replay.get("reason") != "delivery_id_conflict"
    assert (replay["accepted"], replay["action"]) == (True, "queued")
    assert posts == [], "a frozen answer is replayed without reaching the runtime"
    db.close()


async def test_a_spare_cli_is_asked_for_and_stopped_through_the_web_client():
    """#3420: the web keeps and stops spare CLIs through these two runtime routes."""
    from agentdeck.models import Account
    from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient

    app = _runtime_app()
    host = MagicMock()
    host.prepare_spare = AsyncMock(return_value="booted")
    host.drop_spare = AsyncMock(return_value="dropped")
    app.state.claude_workers.hosts["main"] = host
    web = ClaudeWorkerClient(
        Account("claude_code:main", "claude_code", "main", None),
        transport=httpx.ASGITransport(app=app),
    )

    booted = await web.prepare_spare(
        "new_chat", "chat-1", session_token="tok", cwd="/tmp", model=None, effort="high"
    )
    # A deck-started Work's spare names its role (#3590).
    await web.prepare_spare(
        "work", "chat-2", session_token="tok2", cwd="/tmp", model=None, effort=None, role="work"
    )

    assert (booted, await web.drop_spare("new_chat")) == ("booted", "dropped")
    assert host.prepare_spare.await_args_list == [
        call("new_chat", "chat-1", session_token="tok", cwd="/tmp", model=None, effort="high",
             role=None),
        call("work", "chat-2", session_token="tok2", cwd="/tmp", model=None, effort=None,
             role="work"),
    ]
    host.drop_spare.assert_awaited_once_with("new_chat")
