"""Scheduler-loop regressions with synthetic storage and injection boundaries."""

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck.events import EventBus
from agentdeck.models import Capability
from agentdeck.scheduled_messages import ScheduledMessage, ScheduledMessageService

pytestmark = pytest.mark.asyncio


class _Store:
    def __init__(self, *messages):
        self.rows = {message.id: message for message in messages}

    def record_scheduled_message(self, message):
        self.rows[message.id] = message
        return message

    def load_due_scheduled_messages(self, now):
        return tuple(
            row for row in self.rows.values()
            if row.state in ("scheduled", "dispatching") and row.due_at <= now
        )

    def claim_scheduled_message(self, message_id):
        row = self.rows[message_id]
        if row.state not in ("scheduled", "dispatching"):
            return False
        self.rows[message_id] = replace(row, state="dispatching")
        return True

    def settle_scheduled_message(self, message_id, state, reason=None):
        self.rows[message_id] = replace(self.rows[message_id], state=state, reason=reason)

    def load_scheduled_message(self, message_id):
        return self.rows.get(message_id)

    def cancel_scheduled_message(self, message_id, chat_id):
        row = self.rows.get(message_id)
        if row is None or row.chat_id != chat_id or row.state != "scheduled":
            return False
        self.rows[message_id] = replace(row, state="cancelled")
        return True


class _Injector:
    def __init__(self):
        self.ids = []
        self.items = []

    async def start(self, _account, _session, _provider, _text, **kwargs):
        action_id = kwargs["client_action_id"]
        self.ids.append(action_id)
        self.items.append(SimpleNamespace(client_action_id=action_id, state="accepted"))
        return SimpleNamespace(accepted=True, reason=None)

    async def wait_for_delivery(self, _session_key):
        pass

    def status(self, _session_key):
        return SimpleNamespace(items=tuple(self.items))


def _message(identity="scheduled-retry-1"):
    now = datetime.now(UTC)
    return ScheduledMessage(identity, "chat-1", "Continue", now - timedelta(seconds=1), now)


def _target():
    return SimpleNamespace(
        account=object(), session=SimpleNamespace(
            key="codex:test:session-1", capabilities=frozenset({Capability.INJECT})
        ), provider=object()
    )


@pytest.mark.parametrize("resolver_raises", [False, True])
async def test_unavailable_target_does_not_hot_loop(resolver_raises, caplog):
    # Both a missing target and an exception leave the same row due. A completed
    # attempt must not wake the scheduler into an immediate repetition.
    caplog.set_level(logging.CRITICAL, logger="agentdeck.scheduled_messages")
    message = _message()
    store = _Store(message)
    injector = _Injector()
    first_attempt = asyncio.Event()
    attempts = 0

    def resolve(_chat_id):
        nonlocal attempts
        attempts += 1
        first_attempt.set()
        if resolver_raises:
            raise RuntimeError("synthetic discovery failure")
        return None

    service = ScheduledMessageService(store, injector, resolve)
    service.start()
    try:
        await asyncio.wait_for(first_attempt.wait(), timeout=2.0)
        # Much shorter than the existing one-second tick. Allow one extra
        # attempt for a heavily delayed test runner, but never a busy loop.
        await asyncio.sleep(0.05)
    finally:
        await service.stop()
    assert 1 <= attempts <= 2, f"{attempts} attempts without a target or external wake"
    assert store.rows[message.id].state == "scheduled"
    assert injector.ids == []


async def test_a_waiting_row_is_tried_again_on_a_chat_change_not_a_tick():
    """#2865: no target yet is waiting on discovery, and discovery is a chat change."""
    message = _message()
    store = _Store(message)
    injector = _Injector()
    bus = EventBus()
    attempts = 0
    attempted = asyncio.Event()
    delivered = asyncio.Event()
    target = None

    def resolve(_chat_id):
        nonlocal attempts
        attempts += 1
        attempted.set()
        return target

    service = ScheduledMessageService(
        store, injector, resolve, on_delivered=lambda _key, _text: delivered.set(), bus=bus
    )
    service.start()
    try:
        await asyncio.wait_for(attempted.wait(), timeout=2.0)
        target = _target()
        # Nothing retries on its own: no deadline is armed for the waiting row, so only a
        # wake can bring the loop back, and a short wait sees none.
        assert service.next_due(datetime.now(UTC)) is None
        await asyncio.sleep(0.05)
        assert attempts == 1 and not delivered.is_set()
        bus.publish("sessions")
        await asyncio.wait_for(delivered.wait(), timeout=3.0)
    finally:
        await service.stop()
    assert store.rows[message.id].state == "delivered"
    assert injector.ids == [message.id]


async def test_the_loop_sleeps_until_the_next_row_is_due():
    """#2865: a row due later is a deadline, not something a tick finds."""
    now = datetime.now(UTC)
    later = ScheduledMessage("later", "chat-1", "Continue", now + timedelta(seconds=0.4), now)
    store = _Store(later)
    store.next_scheduled_message_due = lambda moment: min(
        (
            row.due_at
            for row in store.rows.values()
            if row.state == "scheduled" and row.due_at > moment
        ),
        default=None,
    )
    delivered = asyncio.Event()
    service = ScheduledMessageService(
        store,
        _Injector(),
        lambda _chat_id: _target(),
        on_delivered=lambda _key, _text: delivered.set(),
    )
    assert service.next_due(now) == later.due_at
    service.start()
    try:
        await asyncio.wait_for(delivered.wait(), timeout=3.0)
    finally:
        await service.stop()
    assert store.rows["later"].state == "delivered"


async def test_schedule_and_cancel_still_signal_immediate_wake():
    store = _Store()
    service = ScheduledMessageService(store, _Injector(), lambda _chat_id: None)
    message = _message()
    assert service.schedule(message) == message
    assert service._wake.is_set()
    service._wake.clear()
    assert service.cancel(message.id, message.chat_id)
    assert service._wake.is_set()
    assert store.rows[message.id].state == "cancelled"


async def test_due_batch_keeps_order_and_stable_delivery_ids():
    messages = (_message("scheduled-first"), _message("scheduled-second"))
    store = _Store(*messages)
    injector = _Injector()
    service = ScheduledMessageService(store, injector, lambda _chat_id: _target())
    try:
        await service.dispatch_due()
        await asyncio.gather(*tuple(service._chat_tasks.values()))
    finally:
        await service.stop()
    assert injector.ids == [message.id for message in messages]
    assert all(store.rows[message.id].state == "delivered" for message in messages)
