from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from public_copy import shipped

import agentdeck.__main__ as entry
from agentdeck.action_context import client_action_context
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    HistoryConfig,
    KeyedWorkersConfig,
    RestartNudgeConfig,
)
from agentdeck.db import Db
from agentdeck.local_bin import ensure_local_bin_on_path
from agentdeck.models import (
    SNAPSHOT_CONTEXT_MANAGEMENT_V1,
    Account,
    InjectResult,
    PendingInteraction,
)
from agentdeck.providers.codex.runtime_client import CodexRuntimeClient
from agentdeck.providers.delivery import DeliverResult
from agentdeck.restart_nudge import RestartNudgeRegistry
from agentdeck.runtime import (
    CodexRuntime,
    DeliverRequest,
    KanbanCapacity,
    create_runtime_app,
)


def _account(tmp_path: Path) -> Account:
    return Account("codex:local", "codex", "local", tmp_path)


class _CapacityHost:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.background: dict[str, int] = {}
        self.last_result: dict[str, float] = {}
        self.finalized: set[tuple[str, str]] = set()

    def active_role_keys(self, role: str) -> set[str]:
        assert role == "kanban"
        return set(self.active)

    def role_last_result_at(self, key: str, role: str) -> float:
        assert role == "kanban"
        return self.last_result.get(key, 0.0)

    def delivery_is_finalized(self, key: str, delivery_id: str | None) -> bool:
        return bool(delivery_id and (key, delivery_id) in self.finalized)

    def snapshot(self) -> dict:
        return {
            "workers": {
                key: {
                    "turn_active": not bool(self.background.get(key)),
                    "background_agent_count": self.background.get(key, 0),
                }
                for key in self.active
            }
        }


class _CapacityRuntime:
    def __init__(self, **hosts: _CapacityHost) -> None:
        self.hosts = hosts

    def all_hosts(self) -> dict[str, _CapacityHost]:
        return self.hosts


def _kanban_request(key: str, delivery_id: str | None = None) -> DeliverRequest:
    return DeliverRequest(
        key=key,
        message="work",
        role="kanban",
        delivery_id=delivery_id or f"d:{key}",
    )


async def test_kanban_capacity_is_global_across_providers_and_race_safe():
    claude = _CapacityHost()
    codex = _CapacityHost()
    capacity = KanbanCapacity(
        limit=2,
        workers={
            "claude_code": _CapacityRuntime(main=claude),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def admit(provider: str, label: str, host: _CapacityHost, key: str):
        async def send() -> DeliverResult:
            await asyncio.sleep(0)
            host.active.add(key)
            return DeliverResult(True, "spawned", session_id=key)

        return await capacity.deliver(provider, label, _kanban_request(key), send)

    results = await asyncio.gather(
        admit("claude_code", "main", claude, "one"),
        admit("codex", "codex", codex, "two"),
        admit("codex", "codex", codex, "three"),
    )

    assert sum(result.accepted for result in results) == 2
    assert [result.reason for result in results].count("at_capacity") == 1


async def test_default_kanban_capacity_limits_all_poller_projects_to_four():
    host = _CapacityHost()
    limit = AppConfig().keyed_workers.max_kanban_workers
    capacity = KanbanCapacity(
        limit=limit,
        workers={"codex": _CapacityRuntime(codex=host)},
    )

    async def admit(key: str) -> DeliverResult:
        async def send() -> DeliverResult:
            host.active.add(key)
            return DeliverResult(True, "spawned", session_id=key)

        return await capacity.deliver("codex", "codex", _kanban_request(key), send)

    results = [
        await admit(f"kanban:{project}#{number}")
        for number, project in enumerate(
            ("agentdeck", "trailhead", "bazaar", "agentdeck", "trailhead"), start=1
        )
    ]

    assert limit == 4
    assert sum(result.accepted for result in results) == 4
    assert results[-1].reason == "at_capacity"


async def test_kanban_capacity_allows_steering_and_excludes_interactive_roles():
    codex = _CapacityHost()
    codex.active.add("existing")
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )
    sent: list[str] = []

    async def send() -> DeliverResult:
        sent.append("sent")
        return DeliverResult(True, "steered", session_id="existing")

    steered = await capacity.deliver(
        "codex", "codex", _kanban_request("existing"), send
    )
    interactive = DeliverRequest(key="chat", message="hello", delegated=False)
    ordinary = await capacity.deliver("codex", "codex", interactive, send)

    assert steered.accepted and ordinary.accepted
    assert sent == ["sent", "sent"]


async def test_uncertain_kanban_delivery_holds_capacity_until_a_terminal_result():
    codex = _CapacityHost()
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def uncertain() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="maybe")

    async def spawned() -> DeliverResult:
        return DeliverResult(True, "spawned", session_id="next")

    first = await capacity.deliver("codex", "codex", _kanban_request("maybe"), uncertain)
    blocked = await capacity.deliver("codex", "codex", _kanban_request("next"), spawned)
    codex.last_result["maybe"] = time.time() + 1
    admitted = await capacity.deliver("codex", "codex", _kanban_request("next"), spawned)

    assert first.accepted
    assert not blocked.accepted and blocked.reason == "at_capacity"
    assert admitted.accepted


async def test_capacity_refusal_logs_each_holder_and_why(caplog):
    claude = _CapacityHost()
    codex = _CapacityHost()
    claude.active.add("repo#1")
    claude.background["repo#1"] = 2
    capacity = KanbanCapacity(
        limit=2,
        workers={
            "claude_code": _CapacityRuntime(main=claude),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def uncertain() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="maybe")

    await capacity.deliver("codex", "codex", _kanban_request("repo#2"), uncertain)

    async def should_not_send() -> DeliverResult:
        raise AssertionError("capacity refusal must happen before the provider write")

    with caplog.at_level("WARNING", logger="agentdeck.runtime"):
        refused = await capacity.deliver(
            "codex", "codex", _kanban_request("repo#3"), should_not_send
        )

    assert not refused.accepted and refused.reason == "at_capacity"
    assert "claude_code:main:repo#1(background_agents=2)" in caplog.text
    assert "codex:codex:repo#2(reservation=" in caplog.text


async def test_rejected_retry_does_not_erase_an_existing_uncertain_reservation():
    codex = _CapacityHost()
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def uncertain() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="maybe")

    async def conflict() -> DeliverResult:
        return DeliverResult(False, "rejected", reason="delivery_id_conflict")

    async def spawned() -> DeliverResult:
        return DeliverResult(True, "spawned", session_id="next")

    await capacity.deliver("codex", "codex", _kanban_request("maybe"), uncertain)
    rejected = await capacity.deliver(
        "codex", "codex", _kanban_request("maybe"), conflict
    )
    blocked = await capacity.deliver(
        "codex", "codex", _kanban_request("next"), spawned
    )

    assert not rejected.accepted
    assert not blocked.accepted and blocked.reason == "at_capacity"


async def test_finalized_delivery_replay_neither_needs_nor_leaks_a_slot():
    codex = _CapacityHost()
    codex.finalized.add(("done", "d:done"))
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def cached() -> DeliverResult:
        return DeliverResult(True, "spawned", session_id="done")

    async def spawned() -> DeliverResult:
        codex.active.add("next")
        return DeliverResult(True, "spawned", session_id="next")

    replay = await capacity.deliver(
        "codex", "codex", _kanban_request("done"), cached
    )
    admitted = await capacity.deliver(
        "codex", "codex", _kanban_request("next"), spawned
    )

    assert replay.accepted and admitted.accepted


async def test_finalized_replay_preserves_a_different_uncertain_reservation():
    codex = _CapacityHost()
    codex.finalized.add(("same", "old"))
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def accepted() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="same")

    async def spawned() -> DeliverResult:
        return DeliverResult(True, "spawned", session_id="next")

    await capacity.deliver(
        "codex", "codex", _kanban_request("same", "current"), accepted
    )
    replay = await capacity.deliver(
        "codex", "codex", _kanban_request("same", "old"), accepted
    )
    blocked = await capacity.deliver(
        "codex", "codex", _kanban_request("next"), spawned
    )

    assert replay.accepted
    assert not blocked.accepted and blocked.reason == "at_capacity"


async def test_worker_release_returns_an_uncertain_reservation():
    codex = _CapacityHost()
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def uncertain() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="maybe")

    async def released() -> DeliverResult:
        return DeliverResult(True, "released", session_id="maybe")

    async def spawned() -> DeliverResult:
        codex.active.add("next")
        return DeliverResult(True, "spawned", session_id="next")

    await capacity.deliver("codex", "codex", _kanban_request("maybe"), uncertain)
    await capacity.release("codex", "codex", "maybe", released)
    admitted = await capacity.deliver(
        "codex", "codex", _kanban_request("next"), spawned
    )

    assert admitted.accepted


async def test_confirmed_inactive_stop_returns_an_uncertain_reservation():
    codex = _CapacityHost()
    capacity = KanbanCapacity(
        limit=1,
        workers={
            "claude_code": _CapacityRuntime(),
            "codex": _CapacityRuntime(codex=codex),
        },
    )

    async def uncertain() -> DeliverResult:
        return DeliverResult(True, "uncertain", session_id="maybe")

    async def not_live() -> DeliverResult:
        return DeliverResult(False, "rejected", reason="not_live")

    async def spawned() -> DeliverResult:
        codex.active.add("next")
        return DeliverResult(True, "spawned", session_id="next")

    await capacity.deliver("codex", "codex", _kanban_request("maybe"), uncertain)
    await capacity.release(
        "codex", "codex", "maybe", not_live, inactive=lambda: True
    )
    admitted = await capacity.deliver(
        "codex", "codex", _kanban_request("next"), spawned
    )

    assert admitted.accepted


async def test_runtime_client_reconnects_to_shared_backend_state(tmp_path):
    state = {
        "thread-1": {
            "active_turn": "turn-1",
            "status": "active",
            "interaction": {
                "id": "question-1",
                "kind": "question",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "title": "Codex needs your answer",
                "message": None,
                "questions": [
                    {
                        "id": "database",
                        "header": "Database",
                        "prompt": "Which database?",
                        "options": [
                            {"label": "SQLite", "description": "Local", "value": None}
                        ],
                        "allow_other": False,
                        "secret": False,
                    }
                ],
                "command": None,
                "cwd": None,
                "url": None,
                "decisions": [],
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": state})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    first = CodexRuntimeClient(_account(tmp_path), transport=transport)
    await first.start()
    assert first.owns("thread-1")
    assert first.active_turn("thread-1") == "turn-1"
    assert first.interaction("thread-1").questions[0].prompt == "Which database?"
    await first.stop()

    # A fresh web-side client sees the state that stayed in the runtime.
    second = CodexRuntimeClient(_account(tmp_path), transport=transport)
    await second.start()
    assert second.active_turn("thread-1") == "turn-1"
    assert second.interaction("thread-1").id == "question-1"
    await second.stop()


# --- not re-decoding a state snapshot already ingested (#1362) ---

async def test_an_unchanged_runtime_state_snapshot_is_not_decoded_again(tmp_path):
    # The provider re-reads this once a second per account and it is
    # byte-identical whenever no thread moved. Decoding it to derive an empty
    # changed-set is the waste, so the bytes are compared before the parse.
    body = json.dumps({"threads": {"thread-1": {"status": "running"}}})
    served = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )

    client = CodexRuntimeClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    assert await client.refresh() == {"thread-1"}
    assert await client.refresh() == set()
    assert served == 2
    assert client.thread_status("thread-1") == "running"

    # A body that actually moved is still ingested and still reported.
    body = json.dumps({"threads": {"thread-1": {"status": "idle"}}})
    assert await client.refresh() == {"thread-1"}
    assert client.thread_status("thread-1") == "idle"


async def test_cancelled_runtime_request_closes_web_side_socket(tmp_path):
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/queue":
            started.set()
            await asyncio.Future()
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.queue_turn("thread-1", "Queued work"))
    await started.wait()

    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert client._http.is_closed


async def test_runtime_activity_reports_codex_and_claude_active_turns():
    app = create_runtime_app(AppConfig())
    codex = MagicMock()
    codex.owned_threads.return_value = {"busy", "idle"}
    codex.active_turn.side_effect = lambda thread_id: "turn-1" if thread_id == "busy" else None
    codex.interaction.return_value = None
    app.state.runtime.clients["local"] = codex
    claude = MagicMock()
    claude.snapshot.return_value = {
        "workers": {
            "issue-1": {
                "session_id": "busy",
                "turn_active": True,
                "effective_activity": True,
            },
            "issue-2": {
                "session_id": "background",
                "turn_active": False,
                "effective_activity": True,
            },
            "issue-3": {
                "session_id": "idle",
                "turn_active": False,
                "effective_activity": False,
            },
        }
    }
    # One of the two is held and outlives a restart (#3248); the Codex turn has no
    # persistent backend, so it would be cut.
    claude.turns_a_restart_would_cut.return_value = 1
    app.state.claude_workers.hosts["main"] = claude

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/activity")

    assert response.json() == {
        "active": True,
        "cut_by_restart": 2,
        "active_turns": {"codex": 1, "claude": 2},
        "active_sessions": [
            "claude_code:main:background",
            "claude_code:main:busy",
            "codex:local:busy",
        ],
        "live_worker_processes": 0,
        "unfinished_jobs": 0,
    }


async def test_runtime_activity_excludes_a_codex_turn_parked_on_interaction(tmp_path):
    app = create_runtime_app(
        AppConfig(
            restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json"))
        )
    )
    codex = MagicMock()
    codex.owned_threads.return_value = {"parked"}
    codex.active_turn.return_value = "turn-1"
    codex.interaction.return_value = PendingInteraction(
        id="interaction-1",
        kind="mcp_form",
        thread_id="parked",
        turn_id="turn-1",
        title="Input requested by an app",
    )
    app.state.runtime.clients["local"] = codex

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/activity")

    assert response.json() == {
        "active": False,
        "cut_by_restart": 0,
        "active_turns": {"codex": 0, "claude": 0},
        "active_sessions": [],
        "live_worker_processes": 0,
        "unfinished_jobs": 0,
    }


async def test_runtime_reports_an_active_codex_thread_without_registering_it(tmp_path):
    """Nothing is registered ahead of a restart any more (#1547) — /activity only reports."""
    config = AppConfig(
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json"))
    )
    app = create_runtime_app(config)
    codex = MagicMock()
    codex.owned_threads.return_value = {"busy"}
    codex.active_turn.return_value = "turn-1"
    codex.interaction.return_value = None
    app.state.runtime.clients["local"] = codex

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/activity")

    assert response.json()["active_sessions"] == ["codex:local:busy"]
    registry = RestartNudgeRegistry(config.restart_nudge.path, ttl_s=config.restart_nudge.ttl_s)
    assert registry.entries() == []


async def test_runtime_refuses_to_skip_an_active_claude_worker_without_a_session_id(
    tmp_path,
):
    config = AppConfig(
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json"))
    )
    app = create_runtime_app(config)
    claude = MagicMock()
    claude.snapshot.return_value = {
        "workers": {"issue-1509": {"effective_activity": True, "session_id": None}}
    }
    app.state.claude_workers.hosts["main"] = claude

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://runtime",
    ) as client:
        response = await client.get("/activity")

    assert response.status_code == 500
    registry = RestartNudgeRegistry(config.restart_nudge.path, ttl_s=config.restart_nudge.ttl_s)
    assert registry.entries() == []


async def test_provider_neutral_worker_route_dispatches_to_codex(tmp_path):
    app = create_runtime_app(
        AppConfig(
            accounts=[AccountConfig(provider="codex", label="codex", config_dir=str(tmp_path))]
        )
    )
    host = MagicMock()
    from agentdeck.providers.claude_code.delivery import DeliverResult

    host.deliver = AsyncMock(return_value=DeliverResult(True, "spawned", session_id="thread-1"))
    app.state.codex_workers.hosts["codex"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/workers/accounts/codex/codex/deliver",
            json={
                "key": "owner/repo#12",
                "message": "go",
                "cwd": "/work",
                "delivery_id": "poller:d1",
                "delegated": True,
                "require_idle": True,
                "context_maintenance": True,
                "expected_session_id": "thread-1",
            },
        )

    assert response.status_code == 200
    assert response.json()["session_id"] == "thread-1"
    host.deliver.assert_awaited_once()
    assert host.deliver.await_args.kwargs["context_maintenance"] is True
    assert host.deliver.await_args.kwargs["expected_session_id"] == "thread-1"


async def test_idle_only_codex_queue_resolves_the_keyed_worker_and_fences_activity(
    tmp_path,
):
    app = create_runtime_app(
        AppConfig(
            accounts=[AccountConfig(provider="codex", label="codex", config_dir=str(tmp_path))]
        )
    )
    host = MagicMock()
    host.find_key_by_session.return_value = "owner/repo#12"
    host.worker_role.return_value = "kanban"
    host.active_role_keys.return_value = set()
    host.snapshot.return_value = {
        "workers": {"owner/repo#12": {"last_result_at": 12.0}}
    }
    host.deliver = AsyncMock(
        return_value=DeliverResult(True, "revived", session_id="thread-1")
    )
    app.state.codex_workers.hosts["codex"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        version = await client.get(
            "/workers/accounts/codex/codex/session-version",
            params={"session_id": "thread-1"},
        )
        response = await client.post(
            "/workers/accounts/codex/codex/revive-session",
            json={
                "session_id": "thread-1",
                "message": "continue",
                "client_action_id": "routine-revival-12",
                "expected_last_result_at": 12.0,
            },
        )

    assert version.json() == {"last_result_at": 12.0}
    assert response.json()["accepted"] is True
    host.deliver.assert_awaited_once()
    assert host.deliver.await_args.args[:2] == ("owner/repo#12", "continue")
    assert host.deliver.await_args.kwargs["require_idle"] is True
    assert host.deliver.await_args.kwargs["expected_last_result_at"] == 12.0
    assert host.deliver.await_args.kwargs["delivery_id"] == "routine-revival-12"






async def test_idle_only_codex_client_fails_closed_against_an_old_runtime(tmp_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/revive-session"):
            return httpx.Response(404)
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    result = await client.queue_turn(
        "thread-1", "continue", require_idle=True, expected_last_result_at=12.0
    )

    assert not result.accepted
    assert paths == ["/workers/accounts/codex/local/revive-session"]


async def test_client_reopens_after_cancellation_close(tmp_path):
    # A cancelled turn closes the shared client (see the test above). A still-running
    # web process must reopen it on the next call; otherwise refresh() dies silently
    # and the cached runtime state freezes, pinning finished sessions as "working".
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/queue":
            started.set()
            await asyncio.Future()
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.queue_turn("thread-1", "Queued work"))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert client._http.is_closed

    # refresh() must transparently rebuild the closed client and resync state.
    await client.refresh()
    assert not client._http.is_closed
    assert client.owns("thread-1")
    await client.stop()


async def test_runtime_client_negotiates_keyed_context_capability(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/local/state"
        return httpx.Response(
            200,
            json={
                "threads": {"thread-1": {}},
                "workers": {"issue-1": {"session_id": "thread-1"}},
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            },
        )

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    await client.refresh()

    assert client.supports(SNAPSHOT_CONTEXT_MANAGEMENT_V1)
    assert client.worker_key("thread-1") == "issue-1"
    await client.stop()


async def test_runtime_client_sends_compact_action(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/compact":
            requests.append(request)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    result = await client.compact("thread-1")

    assert result.accepted
    assert json.loads(requests[0].content) == {"thread_id": "thread-1"}
    await client.stop()


async def test_runtime_client_forwards_browser_action_id(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/queue":
            requests.append(request)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    with client_action_context("browser-action-123"):
        result = await client.queue_turn("thread-1", "Queued work", effort="high")

    assert result.accepted
    assert json.loads(requests[0].content) == {
        "thread_id": "thread-1",
        "message": "Queued work",
        "images": [],
        "effort": "high",
        "client_action_id": "browser-action-123",
    }
    await client.stop()


async def test_runtime_queue_forwards_effort_to_app_server(tmp_path):
    app = create_runtime_app(AppConfig())
    runtime_client = MagicMock()
    runtime_client.account = _account(tmp_path)
    runtime_client.queue_turn = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/queue",
            json={"thread_id": "thread-1", "message": "Next", "effort": "ultra"},
        )

    assert response.status_code == 200
    runtime_client.queue_turn.assert_awaited_once_with(
        "thread-1", "Next", images=[], effort="ultra"
    )


async def test_runtime_queue_replays_action_receipt_and_refuses_conflict(tmp_path):
    app = create_runtime_app(
        AppConfig(keyed_workers={"state_dir": str(tmp_path / "workers")})
    )
    runtime_client = MagicMock()
    runtime_client.account = _account(tmp_path)
    runtime_client.queue_turn = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        payload = {
            "thread_id": "thread-1",
            "message": "Next",
            "effort": "ultra",
            "client_action_id": "browser-action-123",
        }
        first = await client.post("/accounts/local/queue", json=payload)
        replay = await client.post("/accounts/local/queue", json=payload)
        conflict = await client.post(
            "/accounts/local/queue",
            json={**payload, "message": "Different"},
        )

    assert first.json() == replay.json() == {
        "accepted": True,
        "action": "queued",
        "reason": None,
        "session_id": "thread-1",
    }
    assert conflict.json() == {
        "accepted": False,
        "action": "rejected",
        "reason": "delivery_id_conflict",
        "session_id": None,
    }
    runtime_client.queue_turn.assert_awaited_once_with(
        "thread-1", "Next", images=[], effort="ultra"
    )




async def test_runtime_session_delivery_replays_uncertain_without_resending(tmp_path):
    app = create_runtime_app(
        AppConfig(keyed_workers={"state_dir": str(tmp_path / "workers")})
    )
    runtime_client = MagicMock()
    runtime_client.account = _account(tmp_path)
    runtime_client.queue_turn = AsyncMock(side_effect=RuntimeError("transport vanished"))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        payload = {
            "thread_id": "thread-1",
            "message": "Next",
            "client_action_id": "browser-action-123",
        }
        first = await client.post("/accounts/local/queue", json=payload)
        replay = await client.post("/accounts/local/queue", json=payload)

    assert first.json() == replay.json() == {
        "accepted": True,
        "action": "uncertain",
        "reason": None,
        "session_id": "thread-1",
    }
    runtime_client.queue_turn.assert_awaited_once()


async def test_runtime_session_delivery_replays_uncertain_after_restart(tmp_path):
    config = AppConfig(keyed_workers={"state_dir": str(tmp_path / "workers")})
    first_app = create_runtime_app(config)
    first_runtime_client = MagicMock()
    first_runtime_client.account = _account(tmp_path)
    first_runtime_client.queue_turn = AsyncMock(
        side_effect=RuntimeError("transport vanished")
    )
    first_app.state.runtime.clients["local"] = first_runtime_client
    payload = {
        "thread_id": "thread-1",
        "message": "Next",
        "client_action_id": "browser-action-123",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app), base_url="http://runtime"
    ) as client:
        first = await client.post("/accounts/local/queue", json=payload)

    second_app = create_runtime_app(config)
    second_runtime_client = MagicMock()
    second_runtime_client.account = _account(tmp_path)
    second_runtime_client.queue_turn = AsyncMock(return_value=InjectResult(True))
    second_app.state.runtime.clients["local"] = second_runtime_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second_app), base_url="http://runtime"
    ) as client:
        replay = await client.post("/accounts/local/queue", json=payload)

    assert first.json() == replay.json()
    first_runtime_client.queue_turn.assert_awaited_once()
    second_runtime_client.queue_turn.assert_not_awaited()


async def test_runtime_steer_uses_the_session_delivery_receipt_fence(tmp_path):
    app = create_runtime_app(
        AppConfig(keyed_workers={"state_dir": str(tmp_path / "workers")})
    )
    runtime_client = MagicMock()
    runtime_client.account = _account(tmp_path)
    runtime_client.steer = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        payload = {
            "thread_id": "thread-1",
            "message": "Correct course",
            "client_action_id": "browser-action-123",
        }
        first = await client.post("/accounts/local/steer", json=payload)
        replay = await client.post("/accounts/local/steer", json=payload)

    assert first.json() == replay.json() == {
        "accepted": True,
        "action": "steered",
        "reason": None,
        "session_id": "thread-1",
    }
    runtime_client.steer.assert_awaited_once_with(
        "thread-1", "Correct course", images=[]
    )


async def test_runtime_exposes_compact_endpoint():
    app = create_runtime_app(AppConfig())
    runtime_client = MagicMock()
    runtime_client.compact = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/compact", json={"thread_id": "thread-1"}
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": None, "session_id": None}
    runtime_client.compact.assert_awaited_once_with("thread-1")


async def test_runtime_persists_permission_prompt_override(tmp_path):
    app = create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "control.db")),
            accounts=[
                AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
            ],
        )
    )
    runtime_client = MagicMock()
    runtime_client.current_approval_policy = AsyncMock(return_value="never")
    runtime_client.set_permission_prompts = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/permission-prompts",
            json={"thread_id": "thread-1", "enabled": True},
        )
        disabled = await client.post(
            "/accounts/local/permission-prompts",
            json={"thread_id": "thread-1", "enabled": False},
        )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert disabled.json()["accepted"] is True
    assert runtime_client.set_permission_prompts.await_args_list == [
        call("thread-1", True, previous_policy="never"),
        call("thread-1", False, previous_policy="never"),
    ]
    runtime_client.current_approval_policy.assert_awaited_once_with("thread-1")
    assert not app.state.runtime._budget_db.permission_prompts_allowed(
        "codex:local:thread-1"
    )
    assert (
        app.state.runtime._budget_db.permission_prompts_previous_policy(
            "codex:local:thread-1"
        )
        is None
    )


async def test_runtime_reverts_permission_override_when_persistence_fails(
    tmp_path, monkeypatch
):
    app = create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "control.db")),
            accounts=[
                AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
            ],
        )
    )
    runtime_client = MagicMock()
    runtime_client.current_approval_policy = AsyncMock(return_value="never")
    runtime_client.set_permission_prompts = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client
    monkeypatch.setattr(
        app.state.runtime._budget_db,
        "set_permission_prompts",
        lambda *_args, **_kwargs: False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/permission-prompts",
            json={"thread_id": "thread-1", "enabled": True},
        )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": False,
        "reason": "could not save permission prompt setting",
        "session_id": None,
    }
    assert runtime_client.set_permission_prompts.await_args_list == [
        call("thread-1", True, previous_policy="never"),
        call("thread-1", False, previous_policy="never"),
    ]


async def test_runtime_persistence_failure_restores_actual_prior_routing(
    tmp_path, monkeypatch
):
    app = create_runtime_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "control.db")),
            accounts=[
                AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
            ],
        )
    )
    runtime_client = MagicMock()
    runtime_client.set_permission_prompts = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client
    monkeypatch.setattr(
        app.state.runtime._budget_db,
        "set_permission_prompts",
        lambda *_args, **_kwargs: False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/permission-prompts",
            json={"thread_id": "thread-1", "enabled": False},
        )

    assert response.json()["accepted"] is False
    assert runtime_client.set_permission_prompts.await_args_list == [
        call("thread-1", False, previous_policy=None),
        call("thread-1", False, previous_policy=None),
    ]


def test_runtime_snapshot_serializes_owned_turn_and_interaction(tmp_path):
    class FakeClient:
        def owned_threads(self):
            return frozenset({"thread-1"})

        def active_turn(self, thread_id):
            assert thread_id == "thread-1"
            return "turn-1"

        def thread_status(self, thread_id):
            return "active"

        def interaction(self, thread_id):
            return PendingInteraction(
                id="approval-1",
                kind="command_approval",
                thread_id=thread_id,
                turn_id="turn-1",
                title="Approve command?",
                command="uv run pytest -q",
                decisions=("accept", "decline"),
            )

    runtime = CodexRuntime(
        AppConfig(
            accounts=[
                AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
            ]
        ),
        Db(tmp_path / "agentdeck.db"),
    )
    runtime.clients = {"local": FakeClient()}

    snapshot = runtime.snapshot("local")

    assert snapshot["threads"]["thread-1"] == {
        "active_turn": "turn-1",
        "status": "active",
        "interaction": asdict(FakeClient().interaction("thread-1")),
        "permission_prompts": False,
        "permission_prompts_available": True,
    }


async def test_runtime_keeps_upload_copy_until_accepted_turn_finishes(tmp_path):
    runtime = CodexRuntime(AppConfig(), Db(tmp_path / "agentdeck.db"))
    release = asyncio.Event()

    class FakeClient:
        async def wait_for_thread(self, thread_id):
            assert thread_id == "thread-1"
            await release.wait()

    upload_root = tmp_path / "preserved-upload"
    upload_root.mkdir()
    (upload_root / "image.png").write_bytes(b"image")

    runtime.defer_upload_cleanup(FakeClient(), "thread-1", upload_root)
    await asyncio.sleep(0)
    assert upload_root.exists()

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not upload_root.exists():
            break

    assert not upload_root.exists()
    await asyncio.sleep(0)  # let the task's discard callback run
    assert not runtime._cleanup_tasks


def test_systemd_services_keep_web_and_codex_in_separate_control_groups():
    root = Path(__file__).parents[1]
    web = (root / "systemd" / "agentdeck.service").read_text()
    web_socket = (root / "systemd" / "agentdeck.socket").read_text()
    runtime = (root / "systemd" / "agentdeck-codex.service").read_text()

    assert "Wants=agentdeck-codex.service" in web
    assert "agentdeck codex-runtime" not in web
    assert ".venv/bin/python -m agentdeck.web_supervisor" in web
    assert "Sockets=agentdeck.socket" in web
    assert "ExecReload=/bin/kill -HUP $MAINPID" in web
    assert "ListenStream=127.0.0.1:8756" in web_socket
    assert "PartOf=agentdeck.service" not in web_socket
    assert ".venv/bin/agentdeck codex-runtime" in runtime
    assert "uv run" not in web + runtime
    assert "PartOf=agentdeck.service" not in runtime


def test_a_runtime_that_fails_to_start_exits_non_zero(tmp_path, monkeypatch):
    # #3151: uvicorn's Server.run returns quietly after a failed startup, so the
    # runtime exited 0 and Restart=on-failure never brought it back.
    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "no codex"})

    monkeypatch.setattr(entry, "initialize_error_reporting", lambda _name: None)
    monkeypatch.setattr(entry, "_install_redaction_filter", lambda: None)
    monkeypatch.setattr("agentdeck.github_accounts.check_at_start", lambda _name: None)
    monkeypatch.setattr(entry, "runtime_socket_path", lambda: tmp_path / "rt.sock")
    monkeypatch.setattr(entry.os, "umask", lambda _mask: 0o022)
    monkeypatch.setattr(entry, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr("agentdeck.runtime.create_runtime_app", lambda _config: app)

    with pytest.raises(SystemExit) as exited:
        entry._codex_runtime([])
    assert exited.value.code == 3


def test_services_put_local_bin_on_a_bare_boot_path(tmp_path, monkeypatch):
    # #3151: the user manager boots without ~/.local/bin, where gh and codex live.
    # It goes first, and nothing the host already put on PATH is dropped.
    monkeypatch.setenv("HOME", str(tmp_path))
    local_bin = str(tmp_path / ".local" / "bin")
    environ = {"PATH": "/opt/gcloud/bin:/usr/bin"}
    ensure_local_bin_on_path(environ)
    assert environ["PATH"] == f"{local_bin}:/opt/gcloud/bin:/usr/bin"
    ensure_local_bin_on_path(environ)
    assert environ["PATH"].count(local_bin) == 1

    bare: dict[str, str] = {}
    ensure_local_bin_on_path(bare)
    assert bare["PATH"].startswith(f"{local_bin}:")
    assert "/usr/bin" in bare["PATH"].split(":")


@shipped("systemd/agentdeck-staging.service")
def test_staging_web_has_its_own_stable_listener_and_supervisor():
    root = Path(__file__).parents[1]
    web = (root / "systemd" / "agentdeck-staging.service").read_text()
    web_socket = (root / "systemd" / "agentdeck-staging.socket").read_text()

    assert "Sockets=agentdeck-staging.socket" in web
    assert ".venv/bin/python -m agentdeck.web_supervisor" in web
    assert "agentdeck-staging-web/worker.pid" in web
    assert "ListenStream=127.0.0.1:8757" in web_socket
    assert "PartOf=agentdeck-staging.service" not in web_socket
    setup = (root / "scripts" / "staging-setup.sh").read_text()
    checkout = (root / "scripts" / "staging-checkout.sh").read_text()
    assert "agentdeck-staging.socket" in setup
    assert "agentdeck-staging.socket" in checkout


def test_web_service_stays_responsive_under_worker_resource_pressure():
    root = Path(__file__).parents[1]
    web = (root / "systemd" / "agentdeck.service").read_text()
    runtime = (root / "systemd" / "agentdeck-codex.service").read_text()

    assert "CPUWeight=1000" in web
    assert "IOWeight=1000" in web
    assert "MemoryLow=2G" in web
    assert "CPUWeight=1000" not in runtime
    assert "IOWeight=1000" not in runtime
    assert "MemoryLow=2G" not in runtime


def test_unknown_account_404_names_the_labels_the_runtime_knows(tmp_path):
    # #788: renaming a label and restarting only one service split the
    # namespace, and the bare "unknown Codex account" 404 read as an outage.
    runtime = CodexRuntime(AppConfig(), Db(tmp_path / "agentdeck.db"))
    runtime.clients = {"cursor-a": MagicMock(), "cursor-b": MagicMock()}
    try:
        runtime.client("main")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
        assert "'main'" in exc.detail
        assert "['cursor-a', 'cursor-b']" in exc.detail
        assert "restart both" in exc.detail
    else:
        raise AssertionError("expected a 404")


async def test_runtime_defaults_to_stdio_codex_backend(tmp_path, monkeypatch):
    # #992: the persistent `--listen`/proxy backend hangs on `initialize` and
    # crash-loops the runtime, so it must be opt-in. By default the runtime spins
    # up each CodexAppServer on the working `--stdio` path (persistent off).
    config = AppConfig(
        accounts=[
            AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
        ]
    )
    assert config.keyed_workers.persistent_backend is False

    runtime = CodexRuntime(config, Db(tmp_path / "agentdeck.db"))
    assert runtime._persistent_backend is False

    captured: dict = {}

    class FakeClient:
        def __init__(self, _account, **kwargs):
            captured["kwargs"] = kwargs

        async def start(self):
            pass

    monkeypatch.setattr("agentdeck.runtime.CodexAppServer", FakeClient)
    await runtime.start()
    assert captured["kwargs"]["persistent_backend"] is False


def test_runtime_honors_opt_in_persistent_backend(tmp_path):
    # The knob still exists for when the proxy path is fixed: setting it in config
    # threads through to the runtime.
    config = AppConfig(
        accounts=[
            AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
        ],
        keyed_workers=KeyedWorkersConfig(persistent_backend=True),
    )
    runtime = CodexRuntime(config, Db(tmp_path / "agentdeck.db"))
    assert runtime._persistent_backend is True


async def test_runtime_activity_counts_a_live_worker_process_no_turn_is_running_on():
    """The idle web watchdog must not stop a runtime holding a live worker process."""
    app = create_runtime_app(AppConfig())
    host = MagicMock()
    host.snapshot.return_value = {
        "workers": {
            "parked": {"live": True, "turn_active": False, "effective_activity": False},
            "gone": {"live": False, "turn_active": False, "effective_activity": False},
        }
    }
    app.state.claude_workers.all_hosts = lambda: {"main": host}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        payload = (await client.get("/activity")).json()

    assert payload["active"] is False
    assert payload["live_worker_processes"] == 1


async def test_runtime_unloads_idle_threads_past_a_failing_account(tmp_path, monkeypatch):
    runtime = CodexRuntime(AppConfig(), Db(tmp_path / "agentdeck.db"))
    swept = []

    class FakeClient:
        def __init__(self, key, fails):
            self.account = Account(key, "codex", key, tmp_path)
            self.fails = fails

        async def unload_idle(self):
            swept.append(self.account.key)
            if self.fails:
                raise RuntimeError("boom")
            return []

    sleeps = 0

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    runtime.clients = {
        "bad": FakeClient("codex:bad", True),
        "ok": FakeClient("codex:ok", False),
    }
    monkeypatch.setattr("agentdeck.runtime.asyncio.sleep", sleep)

    try:
        await runtime._unload_idle_forever()
    except asyncio.CancelledError:
        pass

    assert swept == ["codex:bad", "codex:ok"]
