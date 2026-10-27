"""The Poller delivers straight to the persistent runtime, never through the web (#2144).

The runtime's ``ensure_delivery`` takes the engine's saved ``work_id``, ``run_id`` and
``delivery_id`` exactly as saved, and the Poller's ``requirements``; the runtime chooses the
account itself (#2146). It then delivers through the runtime's own receipt-fenced worker
delivery. These tests follow one delivery from the host adapter over a real Unix socket into
the runtime app and back, with the web unreachable throughout.
"""

from __future__ import annotations

import dis
import json
import shutil
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import CodeType
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, ValidationError

from agentdeck.config import AccountConfig, AppConfig
from agentdeck.db import Db
from agentdeck.delivery_scheduling import RUNTIME_PROVIDER_UNAVAILABLE, Placement
from agentdeck.ensure_delivery import (
    ENSURE_DELIVERY_PATH,
    EnsureDeliveryRequest,
    ensure_delivery,
)
from agentdeck.models import IssueRef
from agentdeck.poller_adapter.worker import DeliveryOutcome, WorkerDispatcher
from agentdeck.poller_engine.github import receipt_path
from agentdeck.providers.delivery import DeliverResult
from agentdeck.runtime import create_runtime_app
from agentdeck.state import AppState

pytestmark = pytest.mark.anyio

WORK = "github_issue:eerovil/agentdeck#2144"
KEY = "agentdeck#2144"
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
DEAD_WEB = "http://127.0.0.1:9"  # nothing listens here: the web is down for every test

DIRECTIVE = {
    "action": "new",
    "repo": "agentdeck",
    "number": 2144,
    "ref": KEY,
    "generation": "gen-1",
    "run_id": "gen-1",
    "work_id": WORK,
    "dispatch_id": "d1",
    "messages": [{"payload": "do the thing", "author": "eerovil"}],
}
BRIEF = "You are working eerovil/agentdeck#2144 for the AgentDeck poller.\n\nDo the thing."


# --- a curl that speaks to the real socket, and says what it was asked -------

# The adapter shells out to curl. This stands in for it with the subset the adapter uses, so the
# request really crosses a Unix socket into a real server. It logs every URL it was handed; the
# log is how a test proves the web was never asked.
_FAKE_CURL = r'''#!{python}
import http.client, json, socket, sys
args = sys.argv[1:]
log = open({log!r}, "a")
sock = data = None
write_out = ""
url = args[-1]
i = 0
while i < len(args) - 1:
    if args[i] == "--unix-socket":
        sock = args[i + 1]; i += 2; continue
    if args[i] == "--data-binary":
        data = args[i + 1]; i += 2; continue
    if args[i] == "--write-out":
        write_out = args[i + 1]; i += 2; continue
    if args[i] in ("--max-time", "-H"):
        i += 2; continue
    i += 1
log.write(json.dumps({{"url": url, "socket": sock, "data": data}}) + "\n")
log.close()
if sock is None:
    sys.exit(7)  # a TCP URL: the web, which nothing in these tests serves

class Conn(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sock)

try:
    conn = Conn("agentdeck-runtime", timeout=10)
    path = url.split("agentdeck-runtime", 1)[1]
    headers = {{"Content-Type": "application/json"}}
    conn.request("POST" if data is not None else "GET", path, body=data, headers=headers)
    response = conn.getresponse()
except (ConnectionRefusedError, FileNotFoundError):
    sys.exit(7)
body = response.read().decode()
status = str(response.status)
sys.stdout.write(body + write_out.replace("\\n", "\n").replace("%{{http_code}}", status))
sys.exit(22 if response.status >= 400 and "--fail-with-body" in args else 0)
'''


def fake_curl(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "curl.log"
    script = tmp_path / "curl"
    script.write_text(_FAKE_CURL.format(python=sys.executable, log=str(log)))
    script.chmod(0o755)
    return str(script), log


def calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def socket():
    """A socket path short enough for AF_UNIX's ~107-byte limit wherever pytest's tmp is."""
    directory = Path(tempfile.mkdtemp(prefix="ed-"))
    yield directory / "rt.sock"
    shutil.rmtree(directory, ignore_errors=True)


class SocketServer:
    """One ASGI app served on a real Unix socket in a background thread."""

    def __init__(self, app, path: Path) -> None:
        import uvicorn

        self.path = path
        self.server = uvicorn.Server(
            uvicorn.Config(app, uds=str(path), lifespan="off", log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> SocketServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "the socket server never started"
            time.sleep(0.01)
        return self

    def __exit__(self, *_exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _runtime_config(tmp_path: Path, **extra) -> AppConfig:
    return AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, "state_dir": str(tmp_path)},
            "keyed_workers": {"state_dir": str(tmp_path)},
            "restart_nudge": {"file": str(tmp_path / "restart-nudge.json")},
            "history": {"db_path": str(tmp_path / "control.db")},
            "accounts": [
                AccountConfig(
                    provider="claude_code", label="main", config_dir=str(tmp_path)
                ).model_dump()
            ],
            **extra,
        }
    )


def _host(result: DeliverResult, receipt: dict | None = None) -> MagicMock:
    """A worker host that answers ``result`` and holds ``receipt`` for the delivery."""
    host = MagicMock()
    host.deliver = AsyncMock(return_value=result)
    host.delivery_receipt = MagicMock(return_value=receipt)
    host.active_role_keys = MagicMock(return_value=set())
    host.delivery_is_finalized = MagicMock(return_value=False)
    host.role_last_result_at = MagicMock(return_value=0.0)
    host.snapshot = MagicMock(return_value={"workers": {KEY: {"session_id": "s1"}}})
    return host


def _runtime(tmp_path: Path, host: MagicMock, **extra):
    app = create_runtime_app(_runtime_config(tmp_path, **extra))
    app.state.claude_workers.hosts["main"] = host
    return app


def _dispatcher(tmp_path: Path, socket: Path, curl: str):
    return WorkerDispatcher(
        receipts_dir=tmp_path / "dispatch-acks",
        curl=curl,
        runtime_socket=socket,
        candidates=("claude_code:main",),
    )


ACCEPTED = DeliverResult(True, "spawned", session_id="s1")
MAIN_PLACED = Placement("selected", "claude_code:main")
CORRELATED = {"work_id": WORK, "run_id": "gen-1", "fingerprint": "f", "action": "spawned"}


# --- 1, 2, 9, 12: the engine's ids and the Poller's target reach the runtime unchanged ---


def test_the_direct_delivery_carries_the_engines_saved_ids_to_the_runtime(tmp_path, socket):
    host = _host(ACCEPTED, CORRELATED)
    curl, log = fake_curl(tmp_path)
    with SocketServer(_runtime(tmp_path, host), socket):
        delivery = _dispatcher(tmp_path, socket, curl).deliver(
            DIRECTIVE, message=BRIEF, now=NOW
        )

    assert delivery.outcome is DeliveryOutcome.DELIVERED
    [call] = calls(log)
    # One call, to the runtime's socket, and never the web's delivery route (#12).
    assert call["socket"] == str(socket)
    assert call["url"] == f"http://agentdeck-runtime{ENSURE_DELIVERY_PATH}"
    sent = json.loads(call["data"])
    assert (sent["work_id"], sent["run_id"], sent["delivery_id"]) == (WORK, "gen-1", "poller:d1")
    assert "fixed_target" not in sent
    assert sent["requirements"]["candidates"] == ["claude_code:main"]
    # The runtime handed the host exactly those ids; nothing on the way recomputed them.
    kwargs = host.deliver.await_args.kwargs
    assert (kwargs["correlation"].work_id, kwargs["correlation"].run_id) == (WORK, "gen-1")
    assert kwargs["delivery_id"] == "poller:d1"
    acked = receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2144, "d1")
    receipt = json.loads(acked.read_text())
    assert (receipt["work_id"], receipt["run_id"]) == (WORK, "gen-1")


def test_a_saved_correlation_that_is_not_this_cards_is_never_sent(tmp_path):
    curl, log = fake_curl(tmp_path)
    directive = {**DIRECTIVE, "work_id": "github_issue:eerovil/agentdeck#1"}

    delivery = _dispatcher(tmp_path, tmp_path / "rt.sock", curl).deliver(
        directive, message=BRIEF, now=NOW
    )

    assert delivery.reason == "correlation_mismatch"
    assert calls(log) == []


@pytest.mark.parametrize(
    "fields",
    [
        {"work_id": " " + WORK},  # a correction would be a recomputation
        {"run_id": "gen-1 "},
        {"work_id": "github_issue:EEROVIL/agentdeck#2144"},  # not the canonical spelling
        {"work_id": "github_issue:eerovil/agentdeck#1", "issue_ref": "eerovil/agentdeck#2144"},
        {"work_id": ""},
        {"phase_delivery_id": "poller:d1:phase:execute:1"},  # no phase field in the contract
        {"dispatch_generation": "gen-1"},
        # The Poller no longer chooses the account; the retired field is not accepted (#2194).
        {"fixed_target": {"provider": "claude_code", "label": "main"}},
    ],
)
def test_the_runtime_contract_refuses_rather_than_repairs_a_correlation(fields):
    body = {
        "contract": "ensure-delivery-v1",
        "delivery_id": "poller:d1",
        "work_id": WORK,
        "run_id": "gen-1",
        "requirements": {"candidates": ["claude_code:main"]},
        "worker_key": KEY,
        "message": "go",
        **fields,
    }
    with pytest.raises(ValidationError):
        EnsureDeliveryRequest.model_validate(body)


def test_a_directive_without_a_saved_run_is_not_sent_uncorrelated(tmp_path):
    curl, log = fake_curl(tmp_path)
    directive = {
        key: value for key, value in DIRECTIVE.items() if key not in {"generation", "run_id"}
    }

    delivery = _dispatcher(tmp_path, tmp_path / "rt.sock", curl).deliver(
        directive, message=BRIEF, now=NOW
    )

    assert (delivery.outcome, delivery.reason) == (DeliveryOutcome.REFUSED, "correlation_required")
    assert calls(log) == []


# --- 3: accepted, refused and uncertain read exactly as before -----------------


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (DeliverResult(True, "spawned", session_id="s1"), DeliveryOutcome.DELIVERED),
        (DeliverResult(False, "rejected", reason="at_capacity"), DeliveryOutcome.REFUSED),
        (DeliverResult(False, "rejected", reason="worker_changed"), DeliveryOutcome.REFUSED),
        # An unknown refusal reason is not proof that nothing was written.
        (DeliverResult(False, "rejected", reason="spawn crashed"), DeliveryOutcome.UNCERTAIN),
        # The runtime's own replay of an unconfirmed write is accepted-but-uncertain, which the
        # adapter has always acknowledged rather than re-sent.
        (
            DeliverResult(True, "uncertain", session_id="s1", provider_write_state="unknown"),
            DeliveryOutcome.DELIVERED,
        ),
        (
            DeliverResult(
                False,
                "rejected",
                reason="context_operation_pending",
                retryable_same_delivery=True,
            ),
            DeliveryOutcome.SAFE_RETRY,
        ),
    ],
)
def test_each_runtime_answer_reads_as_it_did_through_the_web(
    tmp_path, socket, result, outcome
):
    curl, log = fake_curl(tmp_path)
    with SocketServer(_runtime(tmp_path, _host(result, CORRELATED)), socket):
        delivery = _dispatcher(tmp_path, socket, curl).deliver(DIRECTIVE, message=BRIEF, now=NOW)

    assert delivery.outcome is outcome
    assert len(calls(log)) == 1  # never a second attempt, anywhere
    acked = receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2144, "d1").exists()
    assert acked is (outcome is DeliveryOutcome.DELIVERED)


def test_a_runtime_error_mid_delivery_is_uncertain_and_never_retried(tmp_path, socket):
    host = _host(ACCEPTED)
    host.deliver = AsyncMock(side_effect=RuntimeError("provider died mid-write"))
    curl, log = fake_curl(tmp_path)
    with SocketServer(_runtime(tmp_path, host), socket):
        delivery = _dispatcher(tmp_path, socket, curl).deliver(DIRECTIVE, message=BRIEF, now=NOW)

    assert delivery.outcome is DeliveryOutcome.UNCERTAIN
    assert len(calls(log)) == 1
    assert not (tmp_path / "dispatch-deferrals").exists() or not any(
        (tmp_path / "dispatch-deferrals").rglob("*.json")
    )


async def test_a_replay_answers_with_the_correlation_first_frozen(tmp_path):
    """The receipt decides: a replay reports, and binds, the pair it first froze (#2142)."""
    host = _host(ACCEPTED, {"work_id": WORK, "run_id": "gen-0", "action": "spawned"})
    app = _runtime(tmp_path, host)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(ENSURE_DELIVERY_PATH, json=_ensure_body())

    body = response.json()
    assert (body["work_id"], body["run_id"]) == (WORK, "gen-0")
    [binding] = app.state.budget_db.session_run_bindings()
    assert (binding.work_id, binding.run_id) == (WORK, "gen-0")


# --- 4, 7, 8, 12: the web down, the runtime healthy ---------------------------


# --- 5, 6, 7: an unreachable or old runtime refuses before any write ---------


def test_an_unreachable_runtime_refuses_cleanly_and_nothing_else_is_tried(tmp_path):
    curl, log = fake_curl(tmp_path)

    delivery = _dispatcher(tmp_path, tmp_path / "missing.sock", curl).deliver(
        DIRECTIVE, message=BRIEF, now=NOW
    )

    assert (delivery.outcome, delivery.reason) == (DeliveryOutcome.REFUSED, "runtime_unavailable")
    [call] = calls(log)  # one attempt, at the socket; the web is never tried instead
    assert call["socket"] == str(tmp_path / "missing.sock")
    assert not receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2144, "d1").exists()
    assert receipt_path(tmp_path / "dispatch-deferrals", "agentdeck", 2144, "d1").exists()


def test_a_runtime_without_ensure_delivery_is_a_clean_upgrade_refusal(tmp_path, socket):
    old_runtime = FastAPI()  # its router knows no `ensure_delivery` route

    @old_runtime.post("/workers/accounts/{provider}/{label}/deliver")
    async def legacy_deliver(provider: str, label: str) -> dict:
        raise AssertionError("an old runtime must never be sent the uncorrelated request")

    curl, log = fake_curl(tmp_path)
    with SocketServer(old_runtime, socket):
        delivery = _dispatcher(tmp_path, socket, curl).deliver(DIRECTIVE, message=BRIEF, now=NOW)

    assert (delivery.outcome, delivery.reason) == (
        DeliveryOutcome.REFUSED,
        "runtime_upgrade_required",
    )
    assert [call["url"] for call in calls(log)] == [
        f"http://agentdeck-runtime{ENSURE_DELIVERY_PATH}"
    ]
    assert receipt_path(tmp_path / "dispatch-deferrals", "agentdeck", 2144, "d1").exists()


def test_a_scheduled_delivery_sends_requirements_and_follows_the_runtimes_choice(
    tmp_path, socket
):
    """#2146: the Poller names no account; the runtime's answer says where it went."""
    host = _host(ACCEPTED, CORRELATED)
    curl, log = fake_curl(tmp_path)
    dispatcher = _dispatcher(tmp_path, socket, curl)
    dispatcher.candidates = ("claude_code:alt", "claude_code:main")
    dispatcher.usage_targets = {"claude_code:alt": 99.0}
    heard: list[str] = []
    dispatcher.note = heard.append
    directive = {**DIRECTIVE, "avoid_account": "claude_code:alt"}
    with SocketServer(_runtime(tmp_path, host), socket):
        delivery = dispatcher.deliver(directive, message=BRIEF, now=NOW)

    assert (delivery.outcome, delivery.account) == (DeliveryOutcome.DELIVERED, "claude_code:main")
    sent = json.loads(calls(log)[0]["data"])
    assert "fixed_target" not in sent
    assert sent["requirements"] == {
        "candidates": ["claude_code:alt", "claude_code:main"],
        "avoid_account": "claude_code:alt",
        "usage_targets": {"claude_code:alt": 99.0},
    }
    assert dispatcher.placed == ["claude_code:main"]
    assert heard and "runtime selected claude_code:main" in heard[0]


class _V1Request(BaseModel):
    """The ensure-delivery request as a runtime from before #2146 declared it."""

    model_config = ConfigDict(extra="forbid")
    contract: str
    delivery_id: str
    work_id: str
    run_id: str
    fixed_target: dict
    worker_key: str
    message: str
    cwd: str | None = None
    fresh: bool = False
    role: str | None = None
    allow_over_budget: bool = False
    require_idle: bool = False
    require_active: bool = False
    issue_ref: str | None = None
    continues_session_url: str | None = None
    command_artifacts: dict | None = None


def test_a_runtime_that_predates_scheduling_refuses_before_any_write(tmp_path, socket):
    """An older runtime rejects the unknown field; nothing is re-sent with a chosen account."""
    old_runtime = FastAPI()

    @old_runtime.post(ENSURE_DELIVERY_PATH)
    async def v1(body: _V1Request) -> dict:
        raise AssertionError("an old runtime must refuse the request before handling it")

    curl, log = fake_curl(tmp_path)
    dispatcher = _dispatcher(tmp_path, socket, curl)
    with SocketServer(old_runtime, socket):
        delivery = dispatcher.deliver(DIRECTIVE, message=BRIEF, now=NOW)

    assert (delivery.outcome, delivery.reason) == (
        DeliveryOutcome.REFUSED,
        "runtime_upgrade_required",
    )
    assert len(calls(log)) == 1
    assert receipt_path(tmp_path / "dispatch-deferrals", "agentdeck", 2144, "d1").exists()


# --- 10: the runtime records the same facts the web route recorded -----------


def _ensure_body(**fields) -> dict:
    return {
        "contract": "ensure-delivery-v1",
        "delivery_id": "poller:d1",
        "work_id": WORK,
        "run_id": "gen-1",
        "requirements": {"candidates": ["claude_code:main"]},
        "worker_key": KEY,
        "message": "go",
        "role": "kanban",
        "issue_ref": "eerovil/agentdeck#2144",
        **fields,
    }


async def test_an_accepted_delivery_binds_the_session_to_its_run_once(tmp_path):
    app = _runtime(tmp_path, _host(ACCEPTED, CORRELATED))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        first = await client.post(ENSURE_DELIVERY_PATH, json=_ensure_body())
        await client.post(ENSURE_DELIVERY_PATH, json=_ensure_body())  # the replay

    body = first.json()
    assert body["session_key"] == "claude_code:main:s1"
    assert body["run_participation"] is True
    db = app.state.budget_db
    # The existing #2142 fact, through its existing store method; a replay adds nothing.
    assert [
        (b.session_key, b.work_id, b.run_id, b.first_delivery_id)
        for b in db.session_run_bindings()
    ] == [("claude_code:main:s1", WORK, "gen-1", "poller:d1")]
    # The card (#2127) and the session's own token, which is how the worker finds its key.
    assert db.load_session_issues()["claude_code:main:s1"] == (
        IssueRef("eerovil/agentdeck", 2144),
    )
    token = app.state.claude_workers.hosts["main"].deliver.await_args.kwargs["session_token"]
    assert db.session_key_for_token(token) == "claude_code:main:s1"


async def test_a_refused_delivery_binds_nothing(tmp_path):
    refused = DeliverResult(False, "rejected", reason="at_capacity", session_id="s1")
    app = _runtime(tmp_path, _host(refused, None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(ENSURE_DELIVERY_PATH, json=_ensure_body())

    assert response.json()["run_participation"] is False
    assert app.state.budget_db.session_run_bindings() == []


async def test_an_unknown_target_is_refused_before_any_host_is_reached(tmp_path):
    app = _runtime(tmp_path, _host(ACCEPTED))
    body = _ensure_body(requirements={"candidates": ["claude_code:nobody"]})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(ENSURE_DELIVERY_PATH, json=body)

    assert response.json()["reason"] == RUNTIME_PROVIDER_UNAVAILABLE
    app.state.claude_workers.hosts["main"].deliver.assert_not_called()


async def test_enforced_context_maintenance_refuses_rather_than_being_skipped(tmp_path):
    app = _runtime(tmp_path, _host(ACCEPTED), context_management={"mode": "enforce"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://rt") as client:
        response = await client.post(ENSURE_DELIVERY_PATH, json=_ensure_body())

    assert response.json()["reason"] == "context_maintenance_unavailable"
    app.state.claude_workers.hosts["main"].deliver.assert_not_called()


async def test_a_continued_chat_gains_the_new_session_as_its_next_leg(tmp_path):
    db = Db(tmp_path / "chats.db", 30)
    chat = db.ensure_session_chats(("claude_code:alt:s0",))[0]

    async def schedule(_body, fields):
        # What the host does on spawn: bind the token it was handed to the new session.
        db.record_session_token(fields["session_token"], "claude_code:main:s1")
        return ACCEPTED, MAIN_PLACED

    body = EnsureDeliveryRequest.model_validate(
        _ensure_body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )
    result, key, _placement = await ensure_delivery(body, db=db, schedule=schedule)

    assert key == "claude_code:main:s1"
    assert db.chat_by_id(chat.chat_id).legs == ("claude_code:alt:s0", "claude_code:main:s1")
    db.close()


async def test_a_dead_continuation_fence_is_stepped_over(tmp_path):
    db = Db(tmp_path / "chats.db", 30)
    chat = db.ensure_session_chats(("claude_code:alt:s0",))[0]
    assert db.reserve_chat_successor("t" * 32, "claude_code:alt:s0") == "new"
    db._conn.execute(  # a start that died long ago and left its fence behind (#1620)
        "UPDATE chat_successors SET reserved_at = ?",
        ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),),
    )
    db._conn.commit()

    async def schedule(_body, fields):
        db.record_session_token(fields["session_token"], "claude_code:main:s1")
        return ACCEPTED, MAIN_PLACED

    body = EnsureDeliveryRequest.model_validate(
        _ensure_body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )
    await ensure_delivery(body, db=db, schedule=schedule)

    assert db.chat_by_id(chat.chat_id).legs[-1] == "claude_code:main:s1"
    db.close()


async def test_a_refusal_releases_the_continuation_fence(tmp_path):
    db = Db(tmp_path / "chats.db", 30)
    chat = db.ensure_session_chats(("claude_code:alt:s0",))[0]

    async def schedule(_body, _fields):
        return DeliverResult(False, "rejected", reason="at_capacity"), MAIN_PLACED

    body = EnsureDeliveryRequest.model_validate(
        _ensure_body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )
    await ensure_delivery(body, db=db, schedule=schedule)

    assert db.pending_chat_successor("claude_code:alt:s0") is None
    db.close()


async def test_landing_on_an_older_leg_releases_the_continuation_fence(tmp_path):
    """#2884 review: a delivery that lands on an older leg of the chat cannot join the chain
    after its newest leg. Its fence was left behind, and an unclaimed fence reads as a
    Continue still starting, which blocks the next Run of the card."""
    db = Db(tmp_path / "chats.db", 30)
    chat = db.ensure_session_chats(("claude_code:main:s1",))[0]
    assert db.reserve_chat_successor("t" * 32, "claude_code:main:s1") == "new"
    db.record_session_token("t" * 32, "claude_code:main:s2")

    async def schedule(_body, _fields):
        return ACCEPTED, MAIN_PLACED  # the worker resumed s1, the chat's older leg

    body = EnsureDeliveryRequest.model_validate(
        _ensure_body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )
    _result, key, _placement = await ensure_delivery(body, db=db, schedule=schedule)

    assert key == "claude_code:main:s1"
    assert db.pending_chat_successor("claude_code:main:s2") is None
    assert db.chat_by_id(chat.chat_id).legs == ("claude_code:main:s1", "claude_code:main:s2")
    db.close()


@pytest.mark.parametrize("other_chat", [False, True])
async def test_landing_on_another_existing_session(tmp_path, other_chat):
    """#3587: a resume that lands on a session standing alone links it as the next leg (a card
    split by a failed link is one chat again); a leg of another chat is refused and stands
    alone, and the fence is released."""
    db = Db(tmp_path / "chats.db", 30)
    s0, s1, s9 = "claude_code:alt:s0", "claude_code:main:s1", "claude_code:main:s9"
    chat = db.ensure_session_chats((s0,))[0]
    db.ensure_session_chats((s1,))
    if other_chat:
        assert db.reserve_chat_successor("o" * 32, s1) == "new"
        db.record_session_token("o" * 32, s9)
    other = db.chat_for_session(s1)

    async def schedule(_body, _fields):
        return ACCEPTED, MAIN_PLACED  # the worker resumed s1; nothing bound the token

    body = EnsureDeliveryRequest.model_validate(
        _ensure_body(continues_session_url=f"https://deck.example/chats/{chat.chat_id}")
    )
    _result, key, _placement = await ensure_delivery(body, db=db, schedule=schedule)

    assert key == s1
    assert db.pending_chat_successor(s0) is None
    if other_chat:
        assert db.chat_by_id(chat.chat_id).legs == (s0,)
        assert db.chat_by_id(other.chat_id).legs == (s1, s9)
    else:
        assert db.chat_by_id(chat.chat_id).legs == (s0, s1)
    db.close()


# --- the web follows what the runtime wrote ------------------------------------


def test_the_web_adopts_tokens_and_cards_the_runtime_wrote(tmp_path):
    db = Db(tmp_path / "control.db", 30)
    state = AppState(db=db, budget_db=db)
    state.follow_runtime_relations()

    # The runtime, in another process, writes through its own connection to the same file.
    runtime_db = Db(tmp_path / "control.db", 30)
    runtime_db.record_session_token("t" * 32, "claude_code:main:s1")
    runtime_db.record_session_issue("claude_code:main:s1", IssueRef("eerovil/agentdeck", 2144))

    assert state.follow_runtime_relations() is True
    assert state.session_tokens["t" * 32] == "claude_code:main:s1"
    assert state.session_issues["claude_code:main:s1"] == (IssueRef("eerovil/agentdeck", 2144),)
    assert state.follow_runtime_relations() is False  # nothing new: one stamp read, no reload
    runtime_db.close()
    db.close()


# --- 11: the engine stays out of the deck's store and the runtime -----------------


# The deck modules the engine may import (#2597): pure models with no store and no runtime.
_ENGINE_MAY_IMPORT = {"models", "work"}


_IMPORT_NAME = dis.opmap["IMPORT_NAME"]


def _imports(code: CodeType):
    """``(module, level)`` of every import in ``code`` and the code nested in it.

    Read off the compiled module, where every import at any depth is one
    ``IMPORT_NAME``: parsing the engine's source again was most of this test's time
    (#3052)."""
    if _IMPORT_NAME in code.co_code[::2]:
        instructions = list(dis.get_instructions(code))
        for index, instruction in enumerate(instructions):
            if instruction.opname == "IMPORT_NAME":
                yield instruction.argval, instructions[index - 2].argval
    for const in code.co_consts:
        if isinstance(const, CodeType):
            yield from _imports(const)


def test_the_poller_engine_never_opens_the_decks_store_or_the_runtime():
    engine = Path(__file__).resolve().parent.parent / "src" / "agentdeck" / "poller_engine"
    sqlite_users = set()
    for path in sorted(engine.rglob("*.py")):
        source = path.read_text()
        code = SourceFileLoader(path.stem, str(path)).get_code(path.stem)
        for module, level in _imports(code):
            if level > 1 and module.split(".", 1)[0] not in _ENGINE_MAY_IMPORT:
                raise AssertionError(f"{path.name} reaches outside the engine package")
            parts = module.split(".")
            if not level and parts[0] == "agentdeck" and parts[1:2] not in (
                ["poller_engine"], *([name] for name in _ENGINE_MAY_IMPORT)
            ):
                raise AssertionError(f"{path.name} reaches outside the engine package")
            if not level and module == "sqlite3":
                sqlite_users.add(path.name)
        for internal in (
            "codex-runtime.sock",
            "AGENTDECK_CODEX_SOCKET",
            "agentdeck.db",
            "session_run_bindings",
            "session_tokens",
            ENSURE_DELIVERY_PATH,
        ):
            assert internal not in source, f"{path.name} names the runtime's {internal}"
    # Its one SQLite file is its own GitHub rate-limit cache, not the deck's control store.
    assert sqlite_users == {"rate_limits.py"}


# --- review round 1: a 200 is not proof, and verification evidence skips the web --------


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "",
        "[]",
        "{}",
        '{"accepted": true}',  # no contract: not an ensure_delivery answer
        '{"contract": "ensure-delivery-v0", "accepted": true}',
        '{"contract": "ensure-delivery-v1", "accepted": "yes"}',
        '{"contract": "ensure-delivery-v1", "action": "spawned"}',
    ],
)
def test_an_unreadable_200_is_uncertain_and_never_acknowledged(tmp_path, body):
    curl = tmp_path / "curl"
    (tmp_path / "answer").write_text(f"{body}\n200")
    curl.write_text(f'#!/bin/sh\ncat {tmp_path / "answer"}\n')
    curl.chmod(0o755)

    delivery = _dispatcher(tmp_path, tmp_path / "rt.sock", str(curl)).deliver(
        DIRECTIVE, message=BRIEF, now=NOW
    )

    assert (delivery.outcome, delivery.reason) == (DeliveryOutcome.UNCERTAIN, "unreadable_answer")
    assert not receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2144, "d1").exists()
    assert not receipt_path(tmp_path / "dispatch-deferrals", "agentdeck", 2144, "d1").exists()


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        (
            '{"contract": "ensure-delivery-v1", "accepted": true,'
            ' "schedule": {"account": "claude_code:main"}}',
            DeliveryOutcome.DELIVERED,
        ),
        (
            '{"contract": "ensure-delivery-v1", "accepted": false, "reason": "at_capacity"}',
            DeliveryOutcome.REFUSED,
        ),
    ],
)
def test_a_readable_answer_to_the_contract_decides(tmp_path, body, outcome):
    curl = tmp_path / "curl"
    (tmp_path / "answer").write_text(f"{body}\n200")
    curl.write_text(f'#!/bin/sh\ncat {tmp_path / "answer"}\n')
    curl.chmod(0o755)

    delivery = _dispatcher(tmp_path, tmp_path / "rt.sock", str(curl)).deliver(
        DIRECTIVE, message=BRIEF, now=NOW
    )

    assert delivery.outcome is outcome


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://deck.example", "https://deck.example/sessions/claude_code:main:s1"),
        ("https://deck.example/", "https://deck.example/sessions/claude_code:main:s1"),
        # No public address, or only a loopback one: nothing to publish, so nothing recorded.
        ("", None),
        ("http://127.0.0.1:8756", None),
    ],
)
def test_the_receipt_names_the_chat_that_took_the_delivery(tmp_path, origin, expected):
    """#2155: the host records the Run's chat, so no worker has to register it."""
    curl = tmp_path / "curl"
    body = json.dumps({
        "contract": "ensure-delivery-v1",
        "accepted": True,
        "session_key": "claude_code:main:s1",
        "schedule": {"account": "claude_code:main"},
    })
    (tmp_path / "answer").write_text(f"{body}\n200")
    curl.write_text(f'#!/bin/sh\ncat {tmp_path / "answer"}\n')
    curl.chmod(0o755)
    dispatcher = _dispatcher(tmp_path, tmp_path / "rt.sock", str(curl))
    dispatcher.origin = lambda: origin

    delivery = dispatcher.deliver(DIRECTIVE, message=BRIEF, now=NOW)

    assert delivery.outcome is DeliveryOutcome.DELIVERED
    assert delivery.session_key == "claude_code:main:s1"
    receipt = json.loads(
        receipt_path(tmp_path / "dispatch-acks", "agentdeck", 2144, "d1").read_text()
    )
    assert receipt.get("session_url") == expected
    # The Work and Run still ride alongside it.
    assert (receipt["work_id"], receipt["run_id"]) == (WORK, "gen-1")


def _engine_adapter(**kwargs):
    from agentdeck.poller_engine.github import GitHubAdapter

    return GitHubAdapter(
        api=MagicMock(),
        host=MagicMock(),
        org="eerovil",
        issue_repos=("agentdeck",),
        code_repos=("agentdeck",),
        project_number=2,
        **kwargs,
    )


def test_verification_jobs_come_from_the_runtime_with_the_web_down(socket):
    runtime = FastAPI()

    @runtime.get("/jobs")
    async def jobs() -> dict:
        return {"jobs": [{"id": "job-1", "state": "done"}], "heavy_slots": []}

    engine = _engine_adapter(agentdeck_url=DEAD_WEB, runtime_socket=str(socket))
    with SocketServer(runtime, socket):
        assert engine.verification_jobs() == [{"id": "job-1", "state": "done"}]


def test_an_unreachable_runtime_is_no_evidence_and_not_a_web_read(tmp_path):
    engine = _engine_adapter(agentdeck_url=DEAD_WEB, runtime_socket=str(tmp_path / "gone.sock"))

    assert engine.verification_jobs() is None
