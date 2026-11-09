import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentdeck.db import Db
from agentdeck.inject import InjectionStatus, QueuedMessage
from agentdeck.models import (
    Capability,
    InjectResult,
    InteractionQuestion,
    PendingInteraction,
)
from agentdeck.scheduled_messages import (
    HOLD_EXPIRED_REASON,
    QUESTION_GONE_MID_SEND_REASON,
    QUESTION_GONE_REASON,
    RUNTIME_HOLD,
    HeldAnswer,
    ScheduledMessage,
    ScheduledMessageService,
)


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


def _asked(interaction_id="tok-1", *, question_ids=("q1",), dismissible=False):
    return PendingInteraction(
        id=interaction_id,
        kind="question",
        thread_id="sid",
        turn_id=None,
        title="Pick",
        questions=tuple(
            InteractionQuestion(id=item, header="", prompt=f"{item}?") for item in question_ids
        ),
        dismissible=dismissible,
    )


class _Box:
    """A chat's question box as the delivery reads it (#4106)."""

    def __init__(self, live, *, up=False, result=None):
        self.live = live
        self.up = up
        self.result = result or InjectResult(True)
        self.answered = []

    def runtime_unreachable(self, _account, _session):
        return not self.up

    def interactions_unknown(self, _account):
        return False

    def live_interaction(self, _account, _session):
        return self.live

    async def answer_live_interaction(self, _account, _session, live, *, answers, decision):
        self.answered.append((live.id, answers, decision))
        return self.result


def _answer_row(now, asked=None, *, answers=None, decision=None, created_at=None):
    asked = asked or _asked()
    return ScheduledMessage(
        id="answer-1",
        chat_id="chat-1",
        text="q1?: yes",
        due_at=now,
        created_at=created_at or now,
        hold_reason=RUNTIME_HOLD,
        interaction_answer=HeldAnswer(
            "codex:test:sid",
            asked.id,
            asked.kind,
            asked.fingerprint(),
            {"q1": ["yes"]} if answers is None else answers,
            decision,
        ),
    )


def _answer_service(tmp_path, box, injector=None):
    store = Db(tmp_path / "answers.db")
    # A chat whose answer waits cannot take a new turn: only the answer's resolver finds it.
    target = SimpleNamespace(
        account=object(),
        session=SimpleNamespace(key="codex:test:sid", capabilities=frozenset()),
        provider=box,
    )
    service = ScheduledMessageService(
        store,
        injector or _HeldInjector(),
        lambda _chat_id: None,
        resolve_answer_target=lambda _chat_id: target,
    )
    return store, service


def _row(store, message_id="answer-1"):
    return store.load_scheduled_message(message_id)


async def test_a_held_answer_goes_to_the_box_once_the_runtime_is_back(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked())
    injector = _HeldInjector()
    store, service = _answer_service(tmp_path, box, injector)
    service.schedule(_answer_row(now))

    await _sweep(service, now)
    assert box.answered == []
    assert _row(store).state == "scheduled"

    box.up = True
    await _sweep(service, now)

    assert box.answered == [("tok-1", {"q1": ["yes"]}, None)]
    assert _row(store).state == "delivered"
    assert injector.calls == []  # never an ordinary chat message
    store.close()


async def test_a_web_process_that_has_not_reached_the_runtime_keeps_an_answer_waiting(tmp_path):
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    box.interactions_unknown = lambda _account: True
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)

    assert service._question_deadlines == {}  # the wait for the question has not begun
    assert _row(store).state == "scheduled"
    store.close()


async def test_a_prompt_replayed_after_the_return_is_answered_under_its_new_id(tmp_path):
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)
    assert _row(store).state == "scheduled"
    assert service.next_due(now) == service._question_deadlines["answer-1"]

    box.live = _asked("tok-2")
    await _sweep(service, now)

    assert [answer[0] for answer in box.answered] == ["tok-2"]
    assert _row(store).state == "delivered"
    assert service._question_deadlines == {}
    store.close()


async def test_a_question_that_never_returns_is_reported_after_a_minute(tmp_path):
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)
    service._question_deadlines["answer-1"] = now - timedelta(seconds=1)
    await _sweep(service, now)

    row = _row(store)
    assert (row.state, row.reason) == ("failed", QUESTION_GONE_REASON)
    assert box.answered == []
    store.close()


async def test_a_different_question_in_the_box_does_not_take_the_answer(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked("tok-2", question_ids=("other",)), up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)
    service._question_deadlines["answer-1"] = now - timedelta(seconds=1)
    await _sweep(service, now)

    assert box.answered == []
    assert _row(store).state == "failed"
    store.close()


async def test_a_held_decision_stays_with_the_leg_that_asked(tmp_path):
    # The chat moved to leg B, which asks the very same thing: A's "accept" is not B's.
    now = datetime.now(UTC)
    legs = {
        "leg-a": _Box(None, up=True),
        "leg-b": _Box(_asked(), up=True),
    }
    store = Db(tmp_path / "answers.db")
    service = ScheduledMessageService(
        store,
        _HeldInjector(),
        lambda _chat_id: None,
        resolve_answer_target=lambda key: SimpleNamespace(
            account=object(),
            session=SimpleNamespace(key=key, capabilities=frozenset()),
            provider=legs[key],
        ),
    )
    row = _answer_row(now)
    pinned = replace(row.interaction_answer, session_key="leg-a")
    service.schedule(replace(row, interaction_answer=pinned))

    await _sweep(service, now)
    assert legs["leg-b"].answered == []
    assert _row(store).state == "scheduled"

    legs["leg-a"].live = _asked()
    await _sweep(service, now)
    assert len(legs["leg-a"].answered) == 1
    assert legs["leg-b"].answered == []
    store.close()


async def test_the_runtime_leaving_again_restarts_the_wait_for_the_question(tmp_path):
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))
    await _sweep(service, now)
    assert "answer-1" in service._question_deadlines

    box.up = False
    await _sweep(service, now)

    assert service._question_deadlines == {}
    store.close()


async def test_a_held_answer_is_given_up_after_thirty_minutes(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked())
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now, created_at=now - timedelta(minutes=31)))

    await _sweep(service, now)

    row = _row(store)
    assert (row.state, row.reason) == ("failed", HOLD_EXPIRED_REASON)
    assert box.answered == []
    store.close()


async def test_a_cancelled_answer_is_not_delivered(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked())
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    assert service.cancel("answer-1", "chat-1") is True
    box.up = True
    await _sweep(service, now)

    assert box.answered == []
    store.close()


async def test_a_refused_answer_shows_the_runtimes_reason(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked(), up=True, result=InjectResult(False, "interaction_not_pending"))
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)

    row = _row(store)
    assert (row.state, row.reason) == ("failed", "interaction_not_pending")
    store.close()


async def test_an_answer_left_mid_send_is_retried_only_while_its_question_is_open(tmp_path):
    now = datetime.now(UTC)
    box = _Box(_asked(), up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))
    store.claim_scheduled_message("answer-1")

    await _sweep(service, now)
    assert len(box.answered) == 1
    assert _row(store).state == "delivered"
    store.close()


async def test_an_answer_left_mid_send_with_its_question_gone_says_it_cannot_tell(tmp_path):
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))
    store.claim_scheduled_message("answer-1")

    await _sweep(service, now)
    service._question_deadlines["answer-1"] = now - timedelta(seconds=1)
    await _sweep(service, now)

    row = _row(store)
    assert (row.state, row.reason) == ("failed", QUESTION_GONE_MID_SEND_REASON)
    assert box.answered == []
    store.close()


async def test_an_answer_whose_first_send_lost_its_reply_never_claims_it_was_not_sent(tmp_path):
    # The runtime took the answer and the reply was lost: the question closes, which is
    # the answer working, and the row must not tell the person to give it again.
    now = datetime.now(UTC)
    box = _Box(None, up=True)
    store, service = _answer_service(tmp_path, box)
    row = _answer_row(now)
    tried = replace(row.interaction_answer, attempted=True)
    service.schedule(replace(row, interaction_answer=tried))

    await _sweep(service, now)
    service._question_deadlines["answer-1"] = now - timedelta(seconds=1)
    await _sweep(service, now)

    settled = _row(store)
    assert settled.interaction_answer.attempted
    assert (settled.state, settled.reason) == ("failed", QUESTION_GONE_MID_SEND_REASON)
    assert settled.may_have_reached_the_agent
    assert box.answered == []
    store.close()


async def test_a_tried_answer_out_of_time_says_it_may_have_been_sent(tmp_path):
    from agentdeck.scheduled_messages import HOLD_EXPIRED_MID_SEND_REASON

    now = datetime.now(UTC)
    box = _Box(_asked(), up=False)
    store, service = _answer_service(tmp_path, box)
    row = _answer_row(now, created_at=now - timedelta(minutes=31))
    tried = replace(row.interaction_answer, attempted=True)
    service.schedule(replace(row, interaction_answer=tried))

    await _sweep(service, now)

    settled = _row(store)
    assert (settled.state, settled.reason) == ("failed", HOLD_EXPIRED_MID_SEND_REASON)
    assert settled.may_have_reached_the_agent
    store.close()


async def test_the_runtime_leaving_mid_delivery_leaves_the_row_for_the_question_to_settle(
    tmp_path,
):
    now = datetime.now(UTC)
    lost = InjectResult(False, "runtime unavailable", runtime_unreachable=True)
    box = _Box(_asked(), up=True, result=lost)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now))

    await _sweep(service, now)
    assert _row(store).state == "dispatching"

    box.result = InjectResult(True)
    await _sweep(service, now)
    assert _row(store).state == "delivered"
    store.close()


async def test_an_open_question_answer_goes_on_when_one_of_its_questions_closed(tmp_path):
    now = datetime.now(UTC)
    answered = _asked("questions-old", question_ids=("q1", "q2"), dismissible=True)
    live = _asked("questions-new", question_ids=("q2", "q3"), dismissible=True)
    box = _Box(live, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(
        _answer_row(now, answered, answers={"q1": ["yes"], "q2": ["no"]})
    )

    await _sweep(service, now)

    assert box.answered == [("questions-new", {"q2": ["no"]}, None)]
    store.close()


async def test_a_held_dismissal_needs_its_own_question_open(tmp_path):
    now = datetime.now(UTC)
    answered = _asked("questions-old", question_ids=("q1", "q2"), dismissible=True)
    live = _asked("questions-new", question_ids=("q2",), dismissible=True)
    box = _Box(live, up=True)
    store, service = _answer_service(tmp_path, box)
    service.schedule(_answer_row(now, answered, answers={}, decision="dismiss:q1"))

    await _sweep(service, now)

    assert box.answered == []
    store.close()


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


async def test_a_deck_message_gains_the_voice_line_as_it_goes(tmp_path):
    """The line is asked for when the row goes, and the row keeps its own text (#3875)."""
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    for id_ in ("wake-on:1", "wake-on:2"):
        store.record_scheduled_message(
            ScheduledMessage(id=id_, chat_id="chat-1", text="Merged", due_at=now, created_at=now)
        )
    injector = _HeldInjector()
    asked = []

    def voice_line(message, target):
        asked.append((message.id, target.session.key))
        return "(Voice mode: speak.)" if message.id == "wake-on:1" else ""

    service = ScheduledMessageService(
        store, injector, _held_target(reachable={"up": True}), voice_line=voice_line
    )
    await _sweep(service, now)
    assert [call[0] for call in injector.calls] == ["Merged\n\n(Voice mode: speak.)", "Merged"]
    assert asked == [("wake-on:1", "codex:test:sid"), ("wake-on:2", "codex:test:sid")]


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
