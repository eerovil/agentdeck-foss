"""Scheduled sends keep their own outcome when the bounded UI history moves."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck.models import Capability
from agentdeck.scheduled_messages import ScheduledMessage, ScheduledMessageService

pytestmark = pytest.mark.asyncio


class _Store:
    def __init__(self, *messages):
        self.rows = {message.id: message for message in messages}

    def load_due_scheduled_messages(self, now):
        return tuple(
            row for row in self.rows.values()
            if row.state in ("scheduled", "dispatching") and row.due_at <= now
        )

    def claim_scheduled_message(self, identity):
        row = self.rows[identity]
        if row.state not in ("scheduled", "dispatching"):
            return False
        self.rows[identity] = replace(row, state="dispatching")
        return True

    def settle_scheduled_message(self, identity, state, reason=None):
        self.rows[identity] = replace(self.rows[identity], state=state, reason=reason)


class _Injector:
    """Mutable queue items, with the same twelve-item presentation window."""

    def __init__(self, outcome="failed", *, churn=0, previous=None, missing=None):
        self.outcome = outcome
        self.churn = churn
        self.previous = previous
        self.missing = missing
        self.items = []
        self.ids = []
        self.waits = 0
        self.current = None

    async def start(self, _account, _session, _provider, _text, **kwargs):
        identity = kwargs["client_action_id"]
        self.ids.append(identity)
        if self.previous:
            self.items.append(SimpleNamespace(
                client_action_id=identity, state=self.previous, reason="older attempt"
            ))
        self.current = SimpleNamespace(client_action_id=identity, state="queued", reason=None)
        self.items.append(self.current)
        return SimpleNamespace(accepted=True, reason=None)

    async def wait_for_delivery(self, _key):
        self.waits += 1
        # Let the scheduler's caller run while this send is still outstanding.
        await asyncio.sleep(0)
        self.current.state = self.outcome
        self.current.reason = "provider refused" if self.outcome == "failed" else None
        for index in range(self.churn):
            self.items.append(SimpleNamespace(
                client_action_id=f"later-{index}", state="complete", reason=None
            ))

    def status(self, _key):
        if self.missing == "status":
            return None
        if self.missing == "item":
            return SimpleNamespace(items=())
        return SimpleNamespace(items=tuple(self.items[-12:]))


def _setup(injector):
    now = datetime.now(UTC)
    message = ScheduledMessage(
        "scheduled-outcome", "chat-1", "Continue", now - timedelta(seconds=1), now
    )
    store = _Store(message)
    delivered = []
    target = SimpleNamespace(
        account=object(), session=SimpleNamespace(
            key="codex:test:session-1", capabilities=frozenset({Capability.INJECT})
        ), provider=object()
    )
    service = ScheduledMessageService(
        store, injector, lambda _chat_id: target,
        on_delivered=lambda key, text: delivered.append((key, text)),
    )
    return service, store, message, delivered


async def _dispatch(service):
    await service.dispatch_due()
    await asyncio.wait_for(asyncio.gather(*tuple(service._chat_tasks.values())), timeout=2.0)


@pytest.mark.parametrize("outcome", ["failed", "accepted", "observed", "complete"])
@pytest.mark.parametrize("churn", [0, 12, 30])
async def test_own_outcome_survives_ui_history_churn(outcome, churn):
    injector = _Injector(outcome, churn=churn)
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        if churn >= 12:
            assert all(item.client_action_id != message.id for item in injector.status("").items)
        assert store.rows[message.id].state == ("failed" if outcome == "failed" else "delivered")
        expected_reason = "provider refused" if outcome == "failed" else None
        assert store.rows[message.id].reason == expected_reason
        assert delivered == ([] if outcome == "failed" else [("codex:test:session-1", "Continue")])
        await _dispatch(service)
        assert injector.ids == [message.id]
    finally:
        await service.stop()


@pytest.mark.parametrize("previous,outcome", [("accepted", "failed"), ("failed", "complete")])
async def test_replay_uses_newest_item_with_same_action_id(previous, outcome):
    injector = _Injector(outcome, previous=previous)
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        assert store.rows[message.id].state == ("failed" if outcome == "failed" else "delivered")
        assert bool(delivered) == (outcome != "failed")
    finally:
        await service.stop()


@pytest.mark.parametrize("missing", ["status", "item"])
async def test_missing_evidence_stays_dispatching_and_recovers_with_same_id(missing):
    injector = _Injector("complete", missing=missing)
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        assert store.rows[message.id].state == "dispatching"
        assert delivered == []
        assert injector.waits == 1
        # Recovery reuses the original action ID, never a fresh provider write ID.
        injector.missing = None
        await _dispatch(service)
        assert injector.ids == [message.id, message.id]
        assert store.rows[message.id].state == "delivered"
        assert delivered == [("codex:test:session-1", "Continue")]
    finally:
        await service.stop()


@pytest.mark.parametrize("outcome", ["queued", "running", "unknown"])
async def test_unsettled_item_is_not_delivery_evidence(outcome):
    injector = _Injector(outcome)
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        assert store.rows[message.id].state == "dispatching"
        assert delivered == []
    finally:
        await service.stop()


@pytest.mark.parametrize("outcome", ["failed", "complete"])
async def test_retained_item_survives_entire_status_eviction(outcome):
    class EvictingInjector(_Injector):
        async def wait_for_delivery(self, key):
            await super().wait_for_delivery(key)
            self.missing = "status"

    injector = EvictingInjector(outcome)
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        assert injector.status("") is None
        assert store.rows[message.id].state == ("failed" if outcome == "failed" else "delivered")
        assert bool(delivered) == (outcome != "failed")
    finally:
        await service.stop()


async def test_admission_refusal_does_not_wait_or_report_delivery():
    class RefusingInjector(_Injector):
        async def start(self, *_args, **_kwargs):
            return SimpleNamespace(accepted=False, reason="injection disabled")

        def status(self, _key):
            pytest.fail("a refused admission has no delivery item")

    injector = RefusingInjector()
    service, store, message, delivered = _setup(injector)
    try:
        await _dispatch(service)
        assert store.rows[message.id].state == "failed"
        assert store.rows[message.id].reason == "injection disabled"
        assert injector.waits == 0
        assert delivered == []
    finally:
        await service.stop()


async def test_cancellation_preserves_dispatching_row_for_same_id_recovery():
    waiting = asyncio.Event()
    release = asyncio.Event()

    class BlockingInjector(_Injector):
        async def wait_for_delivery(self, key):
            waiting.set()
            await release.wait()
            await super().wait_for_delivery(key)

    injector = BlockingInjector("complete")
    service, store, message, delivered = _setup(injector)
    try:
        await service.dispatch_due()
        await asyncio.wait_for(waiting.wait(), timeout=2.0)
        await service.stop()
        assert store.rows[message.id].state == "dispatching"
        assert delivered == []
        release.set()
        await _dispatch(service)
        assert injector.ids == [message.id, message.id]
        assert store.rows[message.id].state == "delivered"
        assert delivered == [("codex:test:session-1", "Continue")]
    finally:
        await service.stop()
