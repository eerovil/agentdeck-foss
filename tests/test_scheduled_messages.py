import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentdeck.db import Db
from agentdeck.inject import InjectionStatus, QueuedMessage
from agentdeck.models import Capability, InjectResult
from agentdeck.scheduled_messages import ScheduledMessage, ScheduledMessageService


async def test_dispatching_message_retries_with_stable_id_after_restart(tmp_path):
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    message = ScheduledMessage(
        id="scheduled-action-789",
        chat_id="chat-1",
        text="Wake up now",
        due_at=now - timedelta(seconds=1),
        created_at=now - timedelta(minutes=5),
    )
    assert store.record_scheduled_message(message) == message
    # This is the durable shape left when an earlier web process died after
    # claiming the row but before it could record the provider receipt.
    assert store.claim_scheduled_message(message.id) is True

    class Injector:
        def __init__(self):
            self.ids = []

        async def start(self, _account, _session, _provider, text, **kwargs):
            assert text == "Wake up now"
            self.ids.append(kwargs["client_action_id"])
            return InjectResult(True)

        async def wait_for_delivery(self, _session_key):
            return None

        def status(self, _session_key):
            return InjectionStatus(
                "accepted",
                items=(
                    QueuedMessage(
                        1,
                        "Wake up now",
                        client_action_id="scheduled-action-789",
                        state="accepted",
                    ),
                ),
            )

    injector = Injector()
    delivering = []
    delivered = []
    target = SimpleNamespace(
        account=object(),
        session=SimpleNamespace(key="codex:test:sid", capabilities=frozenset({Capability.INJECT})),
        provider=object(),
    )
    service = ScheduledMessageService(
        store,
        injector,
        lambda _chat_id: target,
        on_delivering=lambda key: delivering.append(key),
        on_delivered=lambda key, text: delivered.append((key, text)),
    )

    await service.dispatch_due(now)
    await asyncio.gather(*service._chat_tasks.values())

    assert injector.ids == ["scheduled-action-789"]
    assert delivering == ["codex:test:sid"]
    assert delivered == [("codex:test:sid", "Wake up now")]
    assert store.load_visible_scheduled_messages("chat-1") == ()


class _HeldInjector:
    """Delivers at once and records what it was handed."""

    def __init__(self):
        self.calls = []

    async def start(self, _account, _session, _provider, text, images=None, **kwargs):
        self.calls.append((text, images, kwargs.get("files"), kwargs["client_action_id"]))
        return InjectResult(True)

    async def wait_for_delivery(self, _session_key):
        return None

    def can_queue(self, _session_key):
        return False

    def status(self, _session_key):
        return InjectionStatus(
            "accepted",
            items=tuple(
                QueuedMessage(n, text, client_action_id=action, state="accepted")
                for n, (text, _images, _files, action) in enumerate(self.calls, 1)
            ),
        )


def _held_target(*, reachable):
    """A deck-owned chat whose runtime is down until ``reachable["up"]`` is set (#2847)."""
    from agentdeck.models import Capability

    def session():
        capabilities = (
            frozenset({Capability.INJECT}) if reachable["up"] else frozenset()
        )
        return SimpleNamespace(key="codex:test:sid", capabilities=capabilities)

    provider = SimpleNamespace(
        runtime_unreachable=lambda _account, _session: not reachable["up"]
    )
    return lambda _chat_id: SimpleNamespace(
        account=object(), session=session(), provider=provider
    )


def _held(tmp_path, now, *, created_at=None, files=()):
    from agentdeck.scheduled_messages import RUNTIME_HOLD, HeldAttachment

    return ScheduledMessage(
        id="held-1",
        chat_id="chat-1",
        text="Remove the worker limits",
        due_at=now,
        created_at=created_at or now,
        hold_reason=RUNTIME_HOLD,
        attachments=tuple(HeldAttachment(path, kind) for path, kind in files),
    )


async def _sweep(service, now):
    await service.dispatch_due(now)
    await asyncio.gather(*service._chat_tasks.values())


async def test_a_held_message_waits_for_the_runtime_then_goes_once_with_its_files(tmp_path):
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    image, note = tmp_path / "1-a.upload", tmp_path / "2-b.upload"
    image.write_bytes(b"png")
    note.write_text("hello")
    message = _held(tmp_path, now, files=((image, "image"), (note, "file")))
    assert store.record_scheduled_message(message) is not None
    reachable = {"up": False}
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable=reachable))

    await _sweep(service, now)
    assert injector.calls == []
    (waiting,) = store.load_visible_scheduled_messages("chat-1")
    assert waiting.state == "scheduled"

    reachable["up"] = True
    await _sweep(service, now)
    await _sweep(service, now)
    assert injector.calls == [("Remove the worker limits", [image], [note], "held-1")]
    assert store.load_visible_scheduled_messages("chat-1") == ()


async def test_a_held_message_is_reported_unsent_after_thirty_minutes(tmp_path):
    from agentdeck.scheduled_messages import HOLD_EXPIRED_REASON

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    note = tmp_path / "1-a.upload"
    note.write_text("hello")
    message = _held(
        tmp_path, now, created_at=now - timedelta(minutes=30), files=((note, "file"),)
    )
    store.record_scheduled_message(message)
    injector = _HeldInjector()
    service = ScheduledMessageService(
        store, injector, _held_target(reachable={"up": False})
    )

    await _sweep(service, now)

    (failed,) = store.load_visible_scheduled_messages("chat-1")
    assert (failed.state, failed.reason) == ("failed", HOLD_EXPIRED_REASON)
    assert failed.text == "Remove the worker limits"
    assert not note.exists()
    assert injector.calls == []


async def test_a_held_message_whose_files_are_gone_is_not_sent_without_them(tmp_path):
    from agentdeck.scheduled_messages import ATTACHMENTS_LOST_REASON

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    kept, lost = tmp_path / "1-a.upload", tmp_path / "2-b.upload"
    kept.write_text("still here")
    store.record_scheduled_message(
        _held(tmp_path, now, files=((kept, "file"), (lost, "file")))
    )
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable={"up": True}))

    await _sweep(service, now)

    (failed,) = store.load_visible_scheduled_messages("chat-1")
    assert (failed.state, failed.reason) == ("failed", ATTACHMENTS_LOST_REASON)
    assert not kept.exists()
    assert injector.calls == []


async def test_a_wake_on_message_waits_for_the_runtime_instead_of_failing(tmp_path):
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    store.record_scheduled_message(
        ScheduledMessage(
            id="wake-on:1",
            chat_id="chat-1",
            text="Your pull request merged",
            due_at=now,
            created_at=now - timedelta(hours=2),
        )
    )
    reachable = {"up": False}
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable=reachable))

    await _sweep(service, now)
    assert injector.calls == []
    # Not a held send, so it is neither shown in the chat nor given up after 30 minutes.
    assert store.load_visible_scheduled_messages("chat-1") == ()

    reachable["up"] = True
    await _sweep(service, now)
    assert [call[3] for call in injector.calls] == ["wake-on:1"]


async def test_a_held_message_waits_until_the_chat_can_take_it_again(tmp_path):
    """The runtime is back but the chat's controls are not projected yet (#2847)."""
    from agentdeck.models import Capability

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    store.record_scheduled_message(_held(tmp_path, now))
    projected = {"inject": False}
    provider = SimpleNamespace(runtime_unreachable=lambda _account, _session: False)

    def target(_chat_id):
        capabilities = frozenset({Capability.INJECT}) if projected["inject"] else frozenset()
        return SimpleNamespace(
            account=object(),
            session=SimpleNamespace(key="codex:test:sid", capabilities=capabilities),
            provider=provider,
        )

    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, target)

    await _sweep(service, now)
    assert injector.calls == []
    (waiting,) = store.load_visible_scheduled_messages("chat-1")
    assert waiting.state == "scheduled"

    projected["inject"] = True
    await _sweep(service, now)
    assert [call[3] for call in injector.calls] == ["held-1"]


async def test_a_row_left_mid_send_waits_for_the_runtime_before_its_retry(tmp_path):
    """A web crash during handoff must not turn the retry into a failure (#2847 review)."""
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    store.record_scheduled_message(_held(tmp_path, now))
    assert store.claim_scheduled_message("held-1") is True
    reachable = {"up": False}
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable=reachable))

    await _sweep(service, now)
    assert injector.calls == []
    (row,) = store.load_visible_scheduled_messages("chat-1")
    assert row.state == "dispatching"

    reachable["up"] = True
    await _sweep(service, now)
    assert [call[3] for call in injector.calls] == ["held-1"]


async def test_a_row_left_mid_send_whose_files_are_gone_is_not_replayed(tmp_path):
    from agentdeck.scheduled_messages import ATTACHMENTS_LOST_MID_SEND_REASON

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    kept, lost = tmp_path / "1-a.upload", tmp_path / "2-b.upload"
    kept.write_text("still here")
    store.record_scheduled_message(
        _held(tmp_path, now, files=((kept, "file"), (lost, "image")))
    )
    assert store.claim_scheduled_message("held-1") is True
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable={"up": True}))

    await _sweep(service, now)

    (failed,) = store.load_visible_scheduled_messages("chat-1")
    assert (failed.state, failed.reason) == ("failed", ATTACHMENTS_LOST_MID_SEND_REASON)
    assert not kept.exists()
    assert injector.calls == []


async def test_a_held_row_left_mid_send_is_also_given_up_after_thirty_minutes(tmp_path):
    """#2847 review: a dispatching held row is bounded too, and says it may have gone."""
    from agentdeck.scheduled_messages import HOLD_EXPIRED_MID_SEND_REASON

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    note = tmp_path / "1-a.upload"
    note.write_text("hello")
    store.record_scheduled_message(
        _held(tmp_path, now, created_at=now - timedelta(minutes=31), files=((note, "file"),))
    )
    assert store.claim_scheduled_message("held-1") is True
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable={"up": False}))

    await _sweep(service, now)

    (failed,) = store.load_visible_scheduled_messages("chat-1")
    assert (failed.state, failed.reason) == ("failed", HOLD_EXPIRED_MID_SEND_REASON)
    assert not note.exists()
    assert injector.calls == []


async def test_a_held_row_behind_a_waiting_wake_on_row_still_expires(tmp_path):
    """#2847 review: a wake-on row waiting ahead must not hide a held row's limit."""
    from agentdeck.scheduled_messages import HOLD_EXPIRED_REASON

    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    store.record_scheduled_message(
        ScheduledMessage(
            id="wake-on:3",
            chat_id="chat-1",
            text="Your pull request merged",
            due_at=now - timedelta(hours=1),
            created_at=now - timedelta(hours=1),
        )
    )
    store.record_scheduled_message(_held(tmp_path, now - timedelta(minutes=31)))
    injector = _HeldInjector()
    service = ScheduledMessageService(store, injector, _held_target(reachable={"up": False}))

    await _sweep(service, now)

    (failed,) = store.load_visible_scheduled_messages("chat-1")
    assert (failed.id, failed.state, failed.reason) == ("held-1", "failed", HOLD_EXPIRED_REASON)
    assert injector.calls == []
    # The wake-on row still waits for the runtime.
    assert [m.id for m in store.load_due_scheduled_messages(now)] == ["wake-on:3"]


async def test_a_chat_change_landing_with_a_wake_still_releases_waiting_chats():
    """#2865: a chat change and a `_wake` landing together used to keep `_blocked`, so the
    next pass skipped the waiting chat and its due rows waited for another chat change."""
    import asyncio
    from types import SimpleNamespace

    from agentdeck.events import EventBus
    from agentdeck.scheduled_messages import ScheduledMessageService

    bus = EventBus()
    store = SimpleNamespace(load_due_scheduled_messages=lambda now: [], next_scheduled_due=None)
    service = ScheduledMessageService(store, None, lambda _chat: None, bus=bus)
    service.next_due = lambda now: None
    service._blocked.add("chat-1")
    with bus.subscribe("sessions") as sessions:
        waiting = asyncio.create_task(service._wait_for_change(sessions))
        await asyncio.sleep(0)
        bus.publish("sessions", "chat-1")
        service._wake.set()
        await asyncio.wait_for(waiting, 2.0)
    assert service._blocked == set()
