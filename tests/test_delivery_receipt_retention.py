"""Cleanup may discard settled history, never an unfinished delivery fence."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agentdeck.providers import delivery
from agentdeck.providers.delivery import DeliverResult, DeliveryReceipts, SessionDeliveryFence


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    # Replace only this module's clock, not Python's shared time module.
    monkeypatch.setattr(delivery, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def test_historical_ttl_name_matches_the_retry_horizon():
    assert delivery.DELIVERY_RECEIPT_TTL_S == delivery.DELIVERY_RETRY_HORIZON_S


def _settle(receipts, identity):
    receipts.finalize(identity, identity, DeliverResult(True, "queued", session_id="sid"))


def _pressure(receipts, clock, monkeypatch, kind):
    if kind == "age":
        clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    else:
        monkeypatch.setattr(delivery, "DELIVERY_RECEIPT_CAP", 2)
    for index in range(3):
        clock[0] += 1
        _settle(receipts, f"later-{index}")


def _stage_historically(store, delivery_id, **fields):
    """A staged receipt as a pre-#2194 phase delivery left it on disk.

    Nothing prepares a staged receipt any more, but the ones already written still have to
    survive cleanup and reload exactly as before.
    """
    store[delivery_id].update(delivery_stage="prepared_before_write", **fields)


@pytest.mark.parametrize("kind", ["age", "count"])
@pytest.mark.parametrize("stage", ["legacy", "prepared", "writing", "identified"])
def test_unfinished_receipt_survives_cleanup_and_json_reload(clock, monkeypatch, kind, stage):
    store = {}
    receipts = DeliveryReceipts(store)
    receipts.prepare("pending", "fingerprint", session_id=None)
    if stage != "legacy":
        _stage_historically(store, "pending")
    if stage in {"writing", "identified"}:
        assert receipts.begin_write("pending", "fingerprint", session_id="sid")
    if stage == "identified":
        assert receipts.identify_turn("pending", "fingerprint", "turn-1")
    original = deepcopy(store["pending"])

    _pressure(receipts, clock, monkeypatch, kind)
    assert store["pending"] == original
    reloaded = DeliveryReceipts(json.loads(json.dumps(store)))
    conflict = reloaded.lookup("pending", "different", live_session_id="sid")
    assert conflict.reason == "delivery_id_conflict"
    replay = reloaded.lookup("pending", "fingerprint", live_session_id="sid")
    if stage == "prepared":
        assert replay is None  # The same frozen request is safe to retry before I/O.
    else:
        assert replay.action == "uncertain"
        assert replay.session_id == "sid"


@pytest.mark.parametrize("kind", ["age", "count"])
@pytest.mark.parametrize("result", [
    DeliverResult(True, "uncertain", session_id="sid"),
    DeliverResult(False, "rejected", provider_write_state="unknown"),
    DeliverResult(False, "rejected", retryable_same_delivery=True),
])
def test_finalized_uncertainty_or_same_delivery_retry_is_not_settled_history(
    clock, monkeypatch, kind, result
):
    store = {}
    receipts = DeliveryReceipts(store)
    receipts.finalize("pending", "fingerprint", result)
    original = deepcopy(store["pending"])
    _pressure(receipts, clock, monkeypatch, kind)
    assert store["pending"] == original
    assert receipts.lookup("pending", "fingerprint", live_session_id="sid") == result


@pytest.mark.parametrize("cap", [2, 4096])
def test_all_unfinished_store_keeps_new_fence_and_recent_completed_history(clock, monkeypatch, cap):
    monkeypatch.setattr(delivery, "DELIVERY_RECEIPT_CAP", cap)
    store = {
        f"pending-{i}": {"fingerprint": str(i), "session_id": "sid", "at": clock[0]}
        for i in range(cap + 1)
    }
    receipts = DeliveryReceipts(store)
    receipts.prepare("new", "new", session_id="sid")
    assert len(store) == cap + 2
    assert all(f"pending-{i}" in store for i in range(cap + 1))
    _settle(receipts, "new")
    assert receipts.lookup("new", "new", live_session_id="sid").action == "queued"
    assert len(store) == cap + 2


@pytest.mark.parametrize("timestamp", [None, "not-a-timestamp"])
def test_unfinished_receipt_does_not_need_an_age_to_remain_fenced(clock, timestamp):
    receipt = {"fingerprint": "frozen", "session_id": "sid"}
    if timestamp is not None:
        receipt["at"] = timestamp
    store = {"pending": receipt}
    receipts = DeliveryReceipts(store)
    _settle(receipts, "new")
    assert store["pending"] == receipt
    assert receipts.lookup("pending", "frozen", live_session_id="sid").action == "uncertain"


@pytest.mark.parametrize("accepted", [True, False])
def test_settled_receipts_keep_existing_ttl_boundary(clock, accepted):
    store = {}
    receipts = DeliveryReceipts(store)
    result = DeliverResult(accepted, "queued" if accepted else "rejected", session_id="sid")
    receipts.finalize("old", "old", result)
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S
    receipts.prepare("tick-1", "tick-1", session_id="sid")
    assert receipts.lookup("old", "old", live_session_id="sid") == result
    clock[0] += 0.1
    receipts.prepare("tick-2", "tick-2", session_id="sid")
    assert "old" not in store


def test_delivery_volume_cannot_shorten_the_retry_horizon(clock, monkeypatch):
    # The legacy cap is intentionally not a correctness boundary any more. A
    # burst larger than it must keep every still-retryable confirmed result.
    monkeypatch.setattr(delivery, "DELIVERY_RECEIPT_CAP", 2)
    store = {}
    receipts = DeliveryReceipts(store)
    for identity in ("first", "second", "third"):
        clock[0] += 1
        _settle(receipts, identity)
    assert set(store) == {"first", "second", "third"}
    assert receipts.lookup("first", "first", live_session_id="sid").action == "queued"


def test_volume_cleanup_happens_once_the_retry_horizon_expires(clock, monkeypatch):
    monkeypatch.setattr(delivery, "DELIVERY_RECEIPT_CAP", 1)
    store = {}
    receipts = DeliveryReceipts(store)
    _settle(receipts, "old")
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    _settle(receipts, "new")
    assert set(store) == {"new"}


def test_confirmed_staged_success_can_expire(clock):
    store = {}
    receipts = DeliveryReceipts(store)
    receipts.prepare("done", "done", session_id="sid")
    _stage_historically(store, "done")
    assert receipts.begin_write("done", "done")
    receipts.finalize(
        "done", "done", DeliverResult(True, "queued", provider_write_state="started")
    )
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    receipts.prepare("new", "new", session_id="sid")
    assert "done" not in store  # The copied write-stage alone does not mean uncertainty.


@pytest.mark.parametrize("choice", ["accepted", "abandoned", "not_written"])
def test_explicit_phase_resolution_preserves_existing_retry_rules(clock, choice):
    store = {}
    receipts = DeliveryReceipts(store)
    receipts.prepare("pending", "frozen", session_id="sid")
    _stage_historically(store, "pending", phase_delivery_id="phase")
    assert receipts.begin_write("pending", "frozen")
    result = receipts.resolve("pending", choice, "checked source", session_id="sid")
    assert result.accepted
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    receipts.prepare("new", "new", session_id="sid")
    if choice == "not_written":
        assert store["pending"]["resolution_choice"] == choice
        assert receipts.lookup("pending", "frozen", live_session_id="sid") is None
        conflict = receipts.lookup("pending", "changed", live_session_id="sid")
        assert conflict.reason == "delivery_id_conflict"
    else:
        assert "pending" not in store  # An explicit terminal resolution is settled history.


def test_only_explicit_clean_failure_can_forget_unfinished_receipt(clock):
    store = {}
    receipts = DeliveryReceipts(store)
    receipts.prepare("pending", "frozen", session_id="sid")
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    receipts.prepare("new", "new", session_id="sid")
    assert not receipts.forget("pending", write_started=True)
    assert receipts.lookup("pending", "frozen", live_session_id="sid").action == "uncertain"
    assert receipts.forget("pending", write_started=False)
    assert receipts.lookup("pending", "frozen", live_session_id="sid") is None


async def _send(fence, identity, send, *, message="continue", operation="queue"):
    return await fence.deliver(
        "test-account", "sid", message, images=[], operation=operation,
        delivery_id=identity, send=send,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["age", "count"])
@pytest.mark.parametrize("operation", ["queue", "steer"])
async def test_session_fence_keeps_uncertain_write_across_cleanup_and_restart(
    tmp_path, clock, monkeypatch, kind, operation
):
    path = tmp_path / "session-deliveries.json"
    fence = SessionDeliveryFence(path)
    writes = []

    async def uncertain_send():
        # The real file must already fence the write before the provider is invoked.
        receipt = json.loads(path.read_text())["accounts"]["test-account"]["sid"]["original"]
        assert "action" not in receipt
        writes.append("original")
        raise RuntimeError("synthetic lost response after a possible write")

    result = await _send(fence, "original", uncertain_send, operation=operation)
    assert result.action == "uncertain"
    if kind == "age":
        clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    else:
        monkeypatch.setattr(delivery, "DELIVERY_RECEIPT_CAP", 2)
    fence = SessionDeliveryFence(path)

    async def success():
        writes.append("later")
        return SimpleNamespace(accepted=True, reason=None, session_id="sid")

    for index in range(3):
        clock[0] += 1
        assert (await _send(fence, f"later-{index}", success, operation=operation)).accepted
    persisted = json.loads(path.read_text())["accounts"]["test-account"]["sid"]
    assert "original" in persisted
    fence = SessionDeliveryFence(path)
    replay = await _send(fence, "original", success, operation=operation)
    assert replay.action == "uncertain"
    conflict = await _send(fence, "original", success, message="different", operation=operation)
    assert conflict.reason == "delivery_id_conflict"
    assert writes == ["original", "later", "later", "later"]


@pytest.mark.asyncio
async def test_cancelled_provider_write_remains_fenced_after_expiry_and_restart(tmp_path, clock):
    path = tmp_path / "session-deliveries.json"
    fence = SessionDeliveryFence(path)
    entered = asyncio.Event()

    async def pending_send():
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(_send(fence, "cancelled", pending_send))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    clock[0] += delivery.DELIVERY_RETRY_HORIZON_S + 1
    writes = []

    async def success():
        writes.append("sent")
        return SimpleNamespace(accepted=True, reason=None, session_id="sid")

    assert (await _send(fence, "other", success)).accepted
    replay = await _send(SessionDeliveryFence(path), "cancelled", success)
    assert replay.action == "uncertain"
    assert writes == ["sent"]


@pytest.mark.asyncio
async def test_clean_refusal_can_retry_and_confirmed_success_replays(tmp_path, clock):
    path = tmp_path / "session-deliveries.json"
    fence = SessionDeliveryFence(path)

    async def refuse():
        return SimpleNamespace(accepted=False, reason="nothing written", session_id=None)

    assert not (await _send(fence, "same-id", refuse)).accepted
    writes = []

    async def success():
        writes.append("sent")
        return SimpleNamespace(accepted=True, reason=None, session_id="sid")

    result = await _send(SessionDeliveryFence(path), "same-id", success)
    replay = await _send(SessionDeliveryFence(path), "same-id", success)
    assert result == replay
    assert result.action == "queued"
    assert writes == ["sent"]
