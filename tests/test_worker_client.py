"""ClaudeWorkerClient: the runtime-socket behaviors it inherits from
RuntimeSocketClient — cancel-close + reopen, and client_action_id forwarding."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agentdeck.action_context import client_action_context
from agentdeck.models import SNAPSHOT_CONTEXT_MANAGEMENT_V1, Account
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient


def _account(tmp_path: Path) -> Account:
    return Account("claude_code:main", "claude_code", "main", tmp_path)


async def test_worker_client_reopens_after_cancellation_close(tmp_path):
    # A web deploy cancels an in-flight deliver; the base closes the socket so
    # uvicorn can shut down. A still-running web process must reopen it on the
    # next read, or the owned map freezes and finished sessions pin as "working".
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            started.set()
            await asyncio.Future()  # hang until cancelled
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.deliver("key-1", "hello"))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert client._http.is_closed

    # refresh() transparently rebuilds the closed client rather than dying.
    await client.refresh()
    assert not client._http.is_closed
    assert client.available


async def test_worker_client_negotiates_context_capability_and_key(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/workers")
        return httpx.Response(
            200,
            json={
                "workers": {"issue-1": {"session_id": "sid-1"}},
                "snapshot_capabilities": [SNAPSHOT_CONTEXT_MANAGEMENT_V1],
            },
        )

    client = ClaudeWorkerClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    await client.refresh()

    assert client.supports(SNAPSHOT_CONTEXT_MANAGEMENT_V1)
    assert client.worker_key("sid-1") == "issue-1"


async def test_worker_client_forwards_client_action_id(tmp_path):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True, "session_id": "s-1"})
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    with client_action_context("act-42"):
        result = await client.deliver("key-1", "hello", effort="high")

    assert result.accepted
    assert result.session_id == "s-1"
    assert seen["payload"]["client_action_id"] == "act-42"
    assert seen["payload"]["effort"] == "high"


async def test_worker_client_preserves_effective_background_activity(tmp_path):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/workers")
        return httpx.Response(
            200,
            json={
                "workers": {
                    "key-1": {
                        "session_id": "sid-1",
                        "live": True,
                        "turn_active": False,
                        "effective_activity": True,
                        # A question the turn has moved past is still open (#3451).
                        "open_questions": {"id": "questions-1", "kind": "question"},
                    },
                    # A rolling web/runtime restart may briefly pair this client
                    # with an older snapshot that has no effective field.
                    "key-2": {
                        "session_id": "sid-2",
                        "live": True,
                        "turn_active": True,
                        "pending_interaction": {"id": "req-2", "kind": "question"},
                    },
                    # A permission prompt the CLI is stuck on comes before the questions.
                    "key-3": {
                        "session_id": "sid-3",
                        "live": True,
                        "turn_active": True,
                        "pending_interaction": {"id": "g-3", "kind": "permission"},
                        "open_questions": {"id": "questions-3", "kind": "question"},
                    },
                }
            },
        )

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.refresh()

    assert client.turn_active("sid-1") is False
    assert client.effective_activity("sid-1") is True
    assert client.effective_activity("sid-2") is True
    assert [client.pending_interaction(sid)["id"] for sid in ("sid-1", "sid-2", "sid-3")] == [
        "questions-1", "req-2", "g-3"
    ]
    assert [client.turn_held(sid) for sid in ("sid-1", "sid-2", "sid-3")] == [False, True, True]


async def test_worker_client_reads_the_provider_quota_reset_boundary(tmp_path):
    reset = datetime(2026, 10, 12, 17, 50, tzinfo=UTC)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/workers")
        return httpx.Response(
            200,
            json={
                "workers": {
                    "key-1": {
                        "session_id": "sid-1",
                        "last_result_limit_reset_at": reset.timestamp(),
                        "live": True,
                        "role": "work",
                    }
                }
            },
        )

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.refresh()

    assert client.automatic_retry_at("sid-1") == reset
    # The role reaches the normalized session, where the usage move reads it (#3362).
    from agentdeck.models import Session, SessionStatus
    from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
    from agentdeck.state import AppState

    provider = ClaudeCodeProvider()
    provider._workers["claude_code:main"] = client
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.IDLE,
    )
    provider.sweep_liveness(_account(tmp_path), [session], AppState())
    assert session.owned_role == "work"


async def test_worker_client_omits_client_action_id_without_a_context(tmp_path):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.deliver("key-1", "hello")
    assert "client_action_id" not in seen["payload"]


async def test_an_unchanged_worker_snapshot_is_not_decoded_again(tmp_path):
    # The watcher re-reads this snapshot every second forever, and it is
    # byte-identical whenever no worker moved. Decoding it only to rebuild the
    # same owned map is the waste; the bytes have to be compared first.
    body = json.dumps({"workers": {"issue-1": {"session_id": "sid-1", "live": True}}})
    served = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    decoded: list[object] = []
    ingest = client._ingest
    client._ingest = lambda data: (decoded.append(data), ingest(data))[1]

    assert await client.refresh() is True  # first read has to ingest
    assert await client.refresh() is False
    assert await client.refresh() is False

    assert served == 3, "the snapshot is still fetched every time"
    assert len(decoded) == 1, "but only decoded when its bytes moved"
    assert client.owns("sid-1")


async def test_a_moved_worker_snapshot_is_decoded_and_reported(tmp_path):
    bodies = [
        json.dumps({"workers": {"issue-1": {"session_id": "sid-1", "live": True}}}),
        json.dumps({"workers": {"issue-1": {"session_id": "sid-1", "live": True}}}),
        json.dumps({"workers": {"issue-1": {"session_id": "sid-1", "live": False}}}),
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=bodies.pop(0) if bodies else "{}",
            headers={"content-type": "application/json"},
        )

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    assert await client.refresh() is True
    assert await client.refresh() is False
    assert await client.refresh() is True, "a body that moved must still be ingested"
    assert client._owned["sid-1"]["live"] is False


async def test_a_reconnecting_runtime_re_ingests_even_if_it_answers_the_same(tmp_path):
    # A runtime restart is exactly when the web process's derived state may need
    # rebuilding, so an unreachable stretch must not leave a body cached as
    # "already ingested" and skip the decode on the way back.
    body = json.dumps({"workers": {"issue-1": {"session_id": "sid-1"}}})
    fail = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("runtime restarting")
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    decoded: list[object] = []
    ingest = client._ingest
    client._ingest = lambda data: (decoded.append(data), ingest(data))[1]

    await client.refresh()
    fail = True
    assert await client.refresh() is False
    assert client.available is False
    fail = False
    assert await client.refresh() is True
    assert len(decoded) == 2


async def test_delivery_spent_asks_with_the_worker_key_not_the_session_id(tmp_path):
    """#2003. The receipt is filed under the runtime's worker key.

    Both hosts read it with ``_records.get(key)``, and those records are keyed by
    an opaque worker key. Asking with a session id is not an error — the endpoint
    answers 200 with no receipt — so the fence would go unseen exactly in the
    window this query exists for: a web process cancelled mid-send while the
    runtime carried the same send through and armed the fence.
    """
    asked: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workers"):
            # The worker key is deliberately nothing like the session id.
            return httpx.Response(
                200, json={"workers": {"issue-2003-worker": {"session_id": "sid-1"}}}
            )
        assert request.url.path.endswith("/delivery-receipt")
        asked.append(dict(request.url.params))
        if request.url.params.get("key") != "issue-2003-worker":
            # What the runtime really does for an unknown key: 200, no receipt.
            return httpx.Response(200, json={"receipt": None, "worker": None})
        return httpx.Response(
            200,
            json={
                "receipt": {"delivery_id": "mac-runner:run-1", "action": "steered"},
                "worker": {"session_id": "sid-1"},
            },
        )

    client = ClaudeWorkerClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    await client.refresh()
    assert client.worker_key("sid-1") == "issue-2003-worker"

    spent = await client.delivery_spent("claude_code", "sid-1", "mac-runner:run-1")

    assert spent is True
    assert asked == [{"key": "issue-2003-worker", "delivery_id": "mac-runner:run-1"}]


async def test_delivery_spent_is_false_when_the_runtime_holds_no_such_worker(tmp_path):
    """No worker, no question to ask — and "not spent" is the safe answer.

    Guessing spent would suppress the working stamp for a chat that really was
    given work, and nothing later puts that right.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError("must not ask about a worker the runtime does not hold")

    client = ClaudeWorkerClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    await client.refresh()

    assert await client.delivery_spent("claude_code", "sid-1", "mac-runner:run-1") is False


async def test_worker_client_reads_and_sets_bypass_permissions(tmp_path):
    posted: dict[str, object] = {}
    snapshot = {
        "workers": {
            "issue-1": {
                "session_id": "sid-1",
                "bypass_permissions": True,
                "bypass_permissions_available": True,
            },
            # An older runtime says nothing: not offered, and never "already on".
            "issue-2": {"session_id": "sid-2"},
        }
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bypass-permissions"):
            posted.update(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json=snapshot)
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.refresh()

    assert client.bypass_permissions_on("sid-1") is True
    assert client.bypass_permissions_available("sid-1") is True
    assert client.bypass_permissions_on("sid-2") is False
    assert client.bypass_permissions_available("sid-2") is False
    result = await client.set_bypass_permissions("sid-1", False)

    assert result.accepted
    # The runtime is addressed by worker key, not by the session id.
    assert posted["key"] == "issue-1"
    assert posted["enabled"] is False
    missing = await client.set_bypass_permissions("ghost", True)
    assert not missing.accepted


async def test_a_dead_worker_owing_an_answer_reads_as_cut_off(tmp_path):
    """#3008: a runtime restart killed a chat with its question open. Its record still owes
    an answer, which is a death the revival answers, not an ending the deck may judge."""
    workers = {
        # Killed mid-turn: an open turn, no process.
        "cut": {"session_id": "sid-cut", "live": False, "turn_open_at": 100.0,
                "last_delivery_at": 100.0},
        # Still running its turn: a live worker is never cut off.
        "live": {"session_id": "sid-live", "live": True, "turn_open_at": 100.0},
        # The turn ended with a result: nothing owed.
        "done": {"session_id": "sid-done", "live": False, "turn_open_at": 0.0,
                 "last_delivery_at": 100.0},
        # Stopped on purpose after the turn opened: a person's ending, not a death.
        "stopped": {"session_id": "sid-stopped", "live": False, "turn_open_at": 100.0,
                    "last_delivery_at": 100.0, "turn_closed_at": 150.0},
        # A message after that stop reopened the chat's claim.
        "reopened": {"session_id": "sid-reopened", "live": False, "turn_open_at": 200.0,
                     "last_delivery_at": 100.0, "turn_closed_at": 150.0},
        # An older runtime publishes no marker at all.
        "old": {"session_id": "sid-old", "live": False},
    }

    revival: dict | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        body: dict = {"workers": workers}
        if revival is not None:
            body["revival"] = revival
        return httpx.Response(200, json=body)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.refresh()
    # A runtime that does not say promises no revival.
    assert not client.revival_due("sid-cut")
    assert client.turn_cut_off("sid-cut")
    assert client.turn_cut_off("sid-reopened")
    # Revival's staleness window runs from the later of the two stamps.
    assert client.turn_cut_off_at("sid-cut") == 100.0
    assert client.turn_cut_off_at("sid-reopened") == 200.0
    assert client.turn_cut_off_at("sid-done") == 0.0
    for session_id in ("sid-live", "sid-done", "sid-stopped", "sid-old", "sid-unknown"):
        assert not client.turn_cut_off(session_id), session_id

    # Before the boot's scan has run, every cut-off chat is still owed its revival.
    revival = {"scanned": False, "due": []}
    await client.refresh()
    assert client.revival_due("sid-cut") and client.revival_due("sid-reopened")
    assert not client.revival_due("sid-live") and not client.revival_due("sid-done")
    # After it, only one a full account turned away and the runtime keeps retrying. A worker
    # killed while the runtime stayed up (its memory cap, a CLI crash) is owed nothing.
    revival = {"scanned": True, "due": ["claude_code:main:sid-cut"]}
    await client.refresh()
    assert client.revival_due("sid-cut")
    assert not client.revival_due("sid-reopened")
