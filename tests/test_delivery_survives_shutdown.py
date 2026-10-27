"""A frontend deploy must not turn a worker delivery into an unknowable outcome (#1889).

On 2026-09-11 a deploy cancelled card agentdeck#1840's delivery between its ledger fence and
the runtime's answer. The row could only be settled `uncertain`, at-most-once made that
permanent, and the instruction was lost. These tests hold both ends of the fix: a delivery that
has not begun is refused with a reason the poller retries, and a delivery already under way
outlives the caller that was cancelled.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agentdeck import shutdown
from agentdeck.config import AppConfig
from agentdeck.db import Db
from agentdeck.state import AppState


@pytest.fixture(autouse=True)
def _clean_shutdown_state():
    shutdown.reset()
    yield
    shutdown.reset()


async def test_a_stopping_web_process_refuses_a_delivery_instead_of_risking_it(tmp_path):
    """The refusal happens before a row exists, so nothing about it is unknowable."""
    from agentdeck.web.routes_api import WorkerDeliverRequest, keyed_deliver

    db = Db(tmp_path / "context.db")

    class Req:
        class app:
            class state:
                app_state = AppState(db=db)
                config = AppConfig.model_validate({})
                accounts = []

    body = WorkerDeliverRequest(
        key="agentdeck#1889",
        message="Work the card",
        cwd=str(tmp_path),
        delivery_id="poller:2b6a413d",
        context_policy="auto",
        role="kanban",
    )

    shutdown.signal_shutdown()
    result = await keyed_deliver(Req, "codex", "codex", body)

    assert result["accepted"] is False
    assert result["reason"] == "host_shutting_down"
    assert result["provider_write_state"] == "not_started"
    assert db.get_context_delivery("codex", "codex:codex", body.key, body.delivery_id) is None, (
        "a refusal that never started must not leave a fence behind it"
    )
    db.close()


def test_the_shutdown_refusal_is_one_the_poller_retries():
    """Refusing is only better than an uncertain row if the card comes back (#1560)."""
    from agentdeck.context_delivery import RE_DECIDED_REFUSALS
    from agentdeck.poller_engine.cycle import SAME_DELIVERY_RETRIES

    assert "host_shutting_down" in SAME_DELIVERY_RETRIES
    assert "host_shutting_down" in RE_DECIDED_REFUSALS


async def test_a_cancelled_caller_does_not_cancel_the_write():
    """The two seconds cut the caller. The delivery keeps going and settles its row."""
    settled = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def delivery():
        started.set()
        await release.wait()
        settled.append("row settled")
        return {"accepted": True}

    caller = asyncio.ensure_future(shutdown.protect("poller:2b6a413d", delivery()))
    await started.wait()

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert settled == [], "the write is still in flight, not abandoned"
    release.set()
    await shutdown.drain(timeout=5.0)
    assert settled == ["row settled"]


async def test_a_cut_delivery_says_so_with_its_id(caplog):
    """The 2026-09-11 loss was only ever traced by matching timestamps to a deploy by hand."""
    hangs = asyncio.Event()

    async def never_answers():
        await hangs.wait()

    caller = asyncio.ensure_future(shutdown.protect("poller:2b6a413d", never_answers()))
    await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="agentdeck.shutdown"):
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await shutdown.drain(timeout=0.05)

    messages = [record.getMessage() for record in caplog.records]
    assert any("poller:2b6a413d" in message for message in messages)
    assert any("did not finish before shutdown" in message for message in messages)
    assert any("agentdeck resolve-context-delivery" in message for message in messages), (
        "an operator meeting this line needs the supported way out named in it"
    )


async def test_a_delivery_that_answers_in_time_leaves_the_drain_nothing_to_do(caplog):
    """The ordinary case: nothing waits, nothing is logged, the deploy is not held up."""

    async def delivery():
        return {"accepted": True, "action": "delivered"}

    result = await shutdown.protect("poller:2b6a413d", delivery())
    assert result["accepted"] is True

    with caplog.at_level(logging.INFO, logger="agentdeck.shutdown"):
        await shutdown.drain(timeout=5.0)
    assert caplog.records == []


def test_the_signal_is_what_arms_the_refusal():
    """By the time a lifespan unwinds, the connections a delivery rides are already cut."""
    import signal

    import uvicorn

    from agentdeck.__main__ import _WebServer

    async def app(scope, receive, send):  # pragma: no cover - never served here
        raise AssertionError("this app is only ever configured, never run")

    server = _WebServer(uvicorn.Config(app))
    assert shutdown.is_shutting_down() is False
    assert shutdown.phase() == "running"

    server.handle_exit(signal.SIGTERM, None)

    assert shutdown.is_shutting_down() is True
    assert shutdown.phase() == "stopping"
    assert server.should_exit is True, "uvicorn's own stop is still armed"


async def test_the_coordinator_drains_before_it_closes_the_runtime_channel():
    """FastAPI hands over a closer; it no longer has to know the temporal protocol (#1911)."""
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []

    async def delivery():
        started.set()
        await release.wait()
        events.append("delivery-settled")
        return {"accepted": True}

    async def close_runtime():
        events.append("runtime-closed")

    caller = asyncio.create_task(shutdown.protect("poller:2b6a413d", delivery()))
    await started.wait()

    # A real uvicorn stop is terminal for this process.
    shutdown.signal_shutdown()
    stopping = asyncio.create_task(
        shutdown.drain_then_close(close_runtime, timeout=5.0)
    )
    await asyncio.sleep(0)

    assert shutdown.is_shutting_down() is True
    assert shutdown.phase() == "draining"
    assert events == [], "the channel must stay open while a protected write is using it"

    release.set()
    await stopping
    assert await caller == {"accepted": True}
    assert events == ["delivery-settled", "runtime-closed"]
    assert shutdown.phase() == "closed"


async def test_lifespan_teardown_without_a_signal_rearms_a_recreated_app():
    """Explicit app teardown still orders safely without poisoning the next in-process app."""
    closed = False

    async def close_runtime():
        nonlocal closed
        closed = True

    assert shutdown.phase() == "running"
    await shutdown.drain_then_close(close_runtime, timeout=0.01)

    assert closed is True
    assert shutdown.is_shutting_down() is False
    assert shutdown.phase() == "running"

    result = await shutdown.protect(
        "poller:after-recreate",
        asyncio.sleep(0, result={"accepted": True}),
    )
    assert result == {"accepted": True}
