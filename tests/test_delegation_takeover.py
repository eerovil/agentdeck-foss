"""A delegation outlives the web process that started it (#2582).

A web deploy replaces the process holding a delegation's wait while the child goes on
in the persistent runtime. These pin that the status is kept in the database, that a
later process takes the wait over once the first has exited, and that nothing is ever
started twice.
"""

import asyncio
import time

from httpx import ASGITransport, AsyncClient

from agentdeck import inject
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.db import Db
from agentdeck.inject import InjectionService
from agentdeck.models import InjectResult


def _account(tmp_path):
    config = AppConfig(
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))]
    )
    return config.build_accounts()[0]


class _Provider:
    """Starts a child named ``thread-1``; its turn ends when ``release`` is set."""

    def __init__(self, result="The child's answer."):
        self.release = asyncio.Event()
        self.result = result
        self.starts = 0
        self.waits: list[tuple[str, float]] = []

    def can_start_session(self, account):
        return True

    async def start_session(self, account, cwd, message, **kwargs):
        self.starts += 1
        return InjectResult(True, session_id="thread-1")

    async def wait_for_session(self, account, session_id, *, timeout_s):
        self.waits.append((session_id, timeout_s))
        await self.release.wait()
        return InjectResult(True)

    async def session_result(self, account, session_id):
        return self.result


def _service(db, **kwargs):
    return InjectionService(InjectConfig(enabled=True), delegation_store=db, **kwargs)


async def _settle(predicate, rounds=50):
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


async def _started(service, db, tmp_path, provider, account):
    result, delegation_id = await service.start_delegation(
        account, provider, tmp_path, "do the thing"
    )
    assert result.accepted and delegation_id is not None
    await _settle(lambda: (db.load_delegation(delegation_id) or {}).get("state") == "running")
    return delegation_id


async def test_every_state_is_saved_and_read_back_by_another_service(tmp_path):
    db = Db(tmp_path / "control.db")
    account = _account(tmp_path)
    provider = _Provider()
    service = _service(db)

    delegation_id = await _started(service, db, tmp_path, provider, account)
    record = db.load_delegation(delegation_id)
    assert record["session_key"] == "codex:test:thread-1"
    assert record["session_token"]
    assert record["owner_pid"] == inject.os.getpid()

    provider.release.set()
    await _settle(lambda: db.load_delegation(delegation_id)["state"] == "complete")

    other = _service(db)
    status = other.delegation_status(delegation_id)
    assert status is not None
    assert status.state == "complete"
    assert status.final_message == "The child's answer."
    await service.stop()


async def test_a_web_shutdown_leaves_the_saved_delegation_unfinished(tmp_path):
    db = Db(tmp_path / "control.db")
    account = _account(tmp_path)
    service = _service(db)

    delegation_id = await _started(service, db, tmp_path, _Provider(), account)
    await service.stop()

    assert db.load_delegation(delegation_id)["state"] == "running"


async def test_a_later_process_takes_over_the_wait_once_the_owner_exited(
    tmp_path, monkeypatch
):
    db = Db(tmp_path / "control.db")
    account = _account(tmp_path)
    first = _Provider()
    service = _service(db)
    delegation_id = await _started(service, db, tmp_path, first, account)
    await service.stop()
    # The old web process's id, and whether it still runs.
    assert db.claim_delegation(delegation_id, inject.os.getpid(), 4242)
    alive = {4242}
    monkeypatch.setattr(inject, "_process_alive", lambda pid: pid in alive)

    second = _Provider()
    successor = _service(db)
    successor.take_over_delegations([account], {"codex": second})
    assert second.waits == []
    assert db.load_delegation(delegation_id)["owner_pid"] == 4242

    alive.clear()
    successor.take_over_delegations([account], {"codex": second})
    await _settle(lambda: second.waits)
    assert second.waits[0][0] == "thread-1"
    assert db.load_delegation(delegation_id)["owner_pid"] == inject.os.getpid()

    second.release.set()
    await _settle(lambda: successor.delegation_status(delegation_id).state == "complete")
    assert successor.delegation_status(delegation_id).final_message == "The child's answer."
    assert first.starts == 1
    assert second.starts == 0
    await successor.stop()


async def test_a_takeover_gets_what_is_left_of_the_budget_but_never_less_than_the_floor(
    tmp_path, monkeypatch
):
    db = Db(tmp_path / "control.db")
    account = _account(tmp_path)
    monkeypatch.setattr(inject, "_process_alive", lambda pid: False)
    budget = InjectConfig().delegation_timeout_s
    now = time.time()
    for delegation_id, age in (("fresh", 60.0), ("old", budget + 600.0)):
        db.save_delegation(
            {
                "id": delegation_id,
                "state": "running",
                "account_key": account.key,
                "session_key": f"{account.key}:{delegation_id}-thread",
                "owner_pid": 4242,
                "created_at": now - age,
            }
        )
    provider = _Provider()
    successor = _service(db)

    successor.take_over_delegations([account], {"codex": provider})
    await _settle(lambda: len(provider.waits) == 2)

    waits = dict(provider.waits)
    assert budget - 120 < waits["fresh-thread"] <= budget - 60
    assert waits["old-thread"] == inject.DELEGATION_RESUME_FLOOR_S
    await successor.stop()


async def test_a_start_cut_off_before_its_chat_was_named_is_never_started_again(
    tmp_path, monkeypatch
):
    db = Db(tmp_path / "control.db")
    account = _account(tmp_path)
    monkeypatch.setattr(inject, "_process_alive", lambda pid: False)
    for delegation_id in ("unbound", "bound"):
        db.save_delegation(
            {
                "id": delegation_id,
                "state": "starting",
                "account_key": account.key,
                "session_token": f"{delegation_id}-token",
                "owner_pid": 4242,
                "created_at": time.time(),
            }
        )
    tokens = {"bound-token": f"{account.key}:bound-thread"}
    provider = _Provider()
    successor = _service(db, session_key_for_token=tokens.get)

    successor.take_over_delegations([account], {"codex": provider})
    await _settle(lambda: provider.waits)

    unbound = successor.delegation_status("unbound")
    assert unbound.state == "failed"
    assert "look for it on the deck before relaunching" in unbound.reason
    bound = successor.delegation_status("bound")
    assert bound.state == "running"
    assert bound.session_key == f"{account.key}:bound-thread"
    assert provider.waits[0][0] == "bound-thread"
    assert provider.starts == 0
    await successor.stop()


async def test_a_delegation_on_an_account_that_is_gone_fails_instead_of_hanging(
    tmp_path, monkeypatch
):
    db = Db(tmp_path / "control.db")
    monkeypatch.setattr(inject, "_process_alive", lambda pid: False)
    db.save_delegation(
        {
            "id": "orphan",
            "state": "running",
            "account_key": "codex:removed",
            "session_key": "codex:removed:thread",
            "owner_pid": 4242,
            "created_at": time.time(),
        }
    )
    successor = _service(db)

    successor.take_over_delegations([_account(tmp_path)], {"codex": _Provider()})

    status = successor.delegation_status("orphan")
    assert status.state == "failed"
    assert status.reason == "account no longer configured"


async def test_only_finished_delegations_past_retention_are_pruned(tmp_path):
    db = Db(tmp_path / "control.db")
    for delegation_id, state in (("done", "complete"), ("waiting", "running")):
        db.save_delegation(
            {
                "id": delegation_id,
                "state": state,
                "account_key": "codex:test",
                "created_at": time.time(),
            }
        )

    db.prune_delegations(time.time() - 60)
    assert db.load_delegation("done") is not None

    db.prune_delegations(time.time() + 60)
    assert db.load_delegation("done") is None
    assert db.load_delegation("waiting") is not None


async def test_the_status_route_answers_from_a_saved_delegation(tmp_path):
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "history.db")),
            inject=InjectConfig(enabled=True),
            accounts=[
                AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))
            ],
        )
    )
    app.state.injector._delegation_store.save_delegation(
        {
            "id": "saved",
            "state": "complete",
            "account_key": "codex:test",
            "session_key": "codex:test:thread",
            "final_message": "Kept across the restart.",
            "owner_pid": 4242,
            "created_at": time.time(),
        }
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        saved = await client.get("/api/delegations/saved")
        unknown = await client.get("/api/delegations/never-started")

    assert saved.status_code == 200
    assert saved.json()["state"] == "complete"
    assert saved.json()["final_message"] == "Kept across the restart."
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "unknown delegation"}


def test_a_reused_process_id_does_not_count_as_the_owner(monkeypatch):
    monkeypatch.setattr(inject.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(inject.Path, "read_bytes", lambda self: b"sleep\x00100\x00")
    assert inject._process_alive(4242) is False
    monkeypatch.setattr(
        inject.Path, "read_bytes", lambda self: b"python\x00-m\x00agentdeck\x00"
    )
    assert inject._process_alive(4242) is True
    assert inject._process_alive(None) is False

