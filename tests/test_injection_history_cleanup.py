"""Discard only injection rows that no longer participate in reconciliation."""

import asyncio
import gc
import weakref
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck import inject as injection
from agentdeck.config import InjectConfig
from agentdeck.inject import InjectionService, QueuedMessage
from agentdeck.models import Capability, InjectResult
from agentdeck.scheduled_messages import ScheduledMessage, ScheduledMessageService

KEY = "codex:test:history"
HISTORY_SIZE = 12


def _service(**kwargs):
    config = InjectConfig(enabled=True, timeout_s=1, max_message_chars=10000)
    return InjectionService(config, **kwargs)


def _item(index, state):
    return QueuedMessage(index, f"message-{index}", state=state, reason=f"reason-{index}")


def _session(key=KEY):
    return SimpleNamespace(key=key, session_id=key, capabilities={Capability.INJECT})


@pytest.fixture
def young_heap():
    """Let `gc.collect()` walk only what the test made: the whole suite's heap took seconds."""
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.mark.parametrize("state", ["failed", "complete"])
@pytest.mark.parametrize("count", [0, 1, 12, 13, 1000])
def test_disposable_history_preserves_the_exact_ui_window(state, count):
    service = _service()
    rows = [_item(i, state) for i in range(count)]
    expected = tuple(rows[-HISTORY_SIZE:])
    service._items[KEY] = rows
    service._snapshot(KEY)
    status = service.status(KEY)
    assert status.items == expected
    assert service._items[KEY] is rows
    assert tuple(rows) == expected
    assert status.state == (state if count else "complete")
    assert status.reason == (f"reason-{count - 1}" if count and state == "failed" else None)


@pytest.mark.parametrize("state", ["queued", "running", "accepted", "unknown"])
def test_pending_and_unknown_rows_survive_count_pressure(state):
    service = _service()
    retained = [_item(i, state) for i in range(20)]
    disposable = [_item(i, "failed") for i in range(20, 120)]
    rows = [*retained, *disposable]
    service._items[KEY] = rows
    service._snapshot(KEY)
    assert rows == retained + disposable[-HISTORY_SIZE:]
    assert service.status(KEY).items == tuple(disposable[-HISTORY_SIZE:])


@pytest.mark.parametrize("state", ["complete", "failed"])
def test_evicted_status_releases_its_disposable_history(state):
    service = _service()
    for index in range(service._MAX_STATUSES + 5):
        key = f"codex:test:{index}"
        service._items[key] = [_item(index, state)]
        service._snapshot(key)
    assert len(service._status) == service._MAX_STATUSES
    assert set(service._items) == set(service._status)


@pytest.mark.parametrize("state", ["queued", "running", "accepted", "unknown"])
def test_evicted_status_keeps_pending_but_not_terminal_rows(state):
    service = _service()
    service._MAX_STATUSES = 1
    protected = _item(1, state)
    rows = [protected, _item(2, "failed"), _item(3, "complete")]
    service._items[KEY] = rows
    service._snapshot(KEY)
    service._snapshot("codex:test:other")
    assert service.status(KEY) is None
    assert service._items[KEY] is rows
    assert rows == [protected]


def test_evicted_history_can_be_used_again_without_reusing_message_ids():
    service = _service()
    service._MAX_STATUSES = 1
    first = service._begin_delivery(KEY, "first")
    service._finish_delivery(KEY, first, InjectResult(False, "refused"))
    service._snapshot("codex:test:other")
    assert KEY not in service._items
    second = service._begin_delivery(KEY, "second")
    assert second.id > first.id
    assert service._items[KEY] == [second]


def test_cleanup_releases_objects_without_deleting_uploads(tmp_path, monkeypatch, young_heap):
    import agentdeck.inject as injection

    service = _service()
    path = tmp_path / "retained-upload"
    path.write_text("cleanup belongs to the delivery path")
    first = _item(0, "failed")
    first.files = (path,)
    reference = weakref.ref(first)
    service._items[KEY] = [first, *[_item(i, "complete") for i in range(1, 20)]]
    del first

    def forbidden(_paths):
        pytest.fail("history cleanup must not change upload lifetimes")

    monkeypatch.setattr(injection, "cleanup_uploaded_files", forbidden)
    service._snapshot(KEY)
    gc.collect()
    assert reference() is None
    assert path.is_file()




def test_observed_queue_object_can_be_collected_while_correlation_survives(tmp_path, young_heap):
    service = _service()
    upload = tmp_path / "old-upload"
    upload.write_text("large queue-only payload")
    first = service._begin_delivery(
        KEY, "first" * 1000, files=[upload], client_action_id="action-first", after_seq=0
    )
    event = SimpleNamespace(
        role="user", seq=1, ts=datetime.now(UTC), queued=False,
        text=first.text, image_media_types=(), answer=None,
    )
    service.observe_transcript(KEY, [event])
    reference = weakref.ref(first)
    first_id = first.id
    del first
    for index in range(20):
        item = service._begin_delivery(KEY, str(index))
        service._finish_delivery(KEY, item, InjectResult(False, "synthetic refusal"))
    gc.collect()
    assert reference() is None
    assert upload.exists()  # Queue-history retirement never owns upload deletion.
    observed = service.observe_transcript(KEY, [event])[1]
    assert observed.id == first_id
    assert observed.client_action_id == "action-first"




def test_observed_correlations_expire_after_ui_reconnect_window():
    service = _service()
    first = service._begin_delivery(KEY, "first", after_seq=0)
    stamp = datetime.now(UTC)
    event = SimpleNamespace(
        role="user", seq=1, ts=stamp, queued=False, text="first",
        image_media_types=(), answer=None,
    )
    service.observe_transcript(KEY, [event])
    key = (KEY, 1)
    service._observed[key] = replace(
        service._observed[key],
        linked_at=stamp - timedelta(seconds=injection.OBSERVED_CORRELATION_TTL_S + 1),
    )
    service._prune_observed_correlations(now=stamp)
    assert key not in service._observed
    assert first.state == "observed"


def test_observed_correlation_volume_is_globally_bounded(monkeypatch):
    monkeypatch.setattr(injection, "MAX_OBSERVED_CORRELATIONS", 2)
    service = _service()
    for index in range(3):
        key = f"codex:test:{index}"
        item = service._begin_delivery(
            key, f"message-{index}", client_action_id=f"action-{index}", after_seq=0
        )
        event = SimpleNamespace(
            role="user", seq=1, ts=datetime.now(UTC), queued=False, text=item.text,
            image_media_types=(), answer=None,
        )
        service.observe_transcript(key, [event])
    assert list(service._observed) == [("codex:test:1", 1), ("codex:test:2", 1)]


def test_same_transcript_seq_in_different_sessions_has_independent_correlation():
    service = _service()
    for key, action in (("codex:test:one", "a-one"), ("codex:test:two", "a-two")):
        service._begin_delivery(key, "same", client_action_id=action, after_seq=0)
        event = SimpleNamespace(
            role="user", seq=1, ts=datetime.now(UTC), queued=False, text="same",
            image_media_types=(), answer=None,
        )
        service.observe_transcript(key, [event])
    assert service._observed[("codex:test:one", 1)].client_action_id == "a-one"
    assert service._observed[("codex:test:two", 1)].client_action_id == "a-two"


@pytest.mark.parametrize("refused", [False, True])
async def test_real_injection_loop_keeps_order_and_bounds_disposable_results(refused):
    service = _service()
    writes = []

    async def inject(_account, _session, text, **_kwargs):
        writes.append(text)
        return InjectResult(
            not refused, "refused" if refused else None, transcript_expected=False
        )

    provider = SimpleNamespace(inject=inject)
    session = _session()
    try:
        for index in range(60):
            result = await service.start(object(), session, provider, f"message-{index}")
            assert result.accepted
            await service.wait_for_delivery(KEY)
        assert writes == [f"message-{index}" for index in range(60)]
        assert len(service._items[KEY]) == HISTORY_SIZE
    finally:
        await service.stop()


async def test_failure_still_marks_every_queued_message_after_pruning():
    service = _service()
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def inject(_account, _session, text, **_kwargs):
        writes.append(text)
        entered.set()
        await release.wait()
        return InjectResult(False, "provider refused")

    provider = SimpleNamespace(inject=inject)
    session = _session()
    held = []
    try:
        for index in range(40):
            await service.start(object(), session, provider, f"message-{index}")
            held.append(service.status(KEY).items[-1])
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await service.wait_for_delivery(KEY)
        assert writes == ["message-0"]
        assert all(item.state == "failed" for item in held)
        assert all("previous turn failed" in item.reason for item in held[1:])
        assert len(service._items[KEY]) == HISTORY_SIZE
    finally:
        release.set()
        await service.stop()


@pytest.mark.parametrize("accepted", [False, True])
async def test_scheduler_uses_real_injector_outcome_after_history_cleanup(accepted):
    now = datetime.now(UTC)
    message = ScheduledMessage("schedule-one", "chat-1", "scheduled", now, now)
    injector = _service()
    session = _session()
    target = SimpleNamespace(account=object(), session=session, provider=None)
    writes, settled, delivered = [], [], []
    queued_more = False
    held = []
    extra_tasks = []

    def on_change(key):
        nonlocal queued_more
        status = injector.status(key)
        if not queued_more and status and status.state == "queued":
            queued_more = True
            held.append(status.items[-1])
            # The scheduler captures this item before the task appends later
            # messages. This reproduces actual start()/wait() choreography.
            extra_tasks.append(asyncio.create_task(add_messages()))

    async def add_messages():
        for index in range(30):
            await injector.start(target.account, session, target.provider, f"later-{index}")

    async def inject(_account, _session, text, **_kwargs):
        writes.append(text)
        await asyncio.sleep(0)
        return InjectResult(accepted, "provider refused" if not accepted else None,
                            transcript_expected=False)

    injector._on_change = on_change
    target.provider = SimpleNamespace(inject=inject)
    store = SimpleNamespace(
        load_due_scheduled_messages=lambda _now: (message,),
        claim_scheduled_message=lambda _id: True,
        settle_scheduled_message=lambda *args: settled.append(args),
    )
    scheduler = ScheduledMessageService(
        store, injector, lambda _chat: target,
        on_delivered=lambda *args: delivered.append(args),
    )
    try:
        await scheduler.dispatch_due(now)
        await asyncio.wait_for(asyncio.gather(*tuple(scheduler._chat_tasks.values())), 2)
        assert held and held[0] not in injector._items[KEY]
        assert settled == ([('schedule-one', 'delivered')] if accepted else
                           [('schedule-one', 'failed', 'provider refused')])
        assert bool(delivered) is accepted
        assert len(injector._items[KEY]) == HISTORY_SIZE
    finally:
        for task in extra_tasks:
            task.cancel()
        await asyncio.gather(*extra_tasks, return_exceptions=True)
        await scheduler.stop()
        await injector.stop()
