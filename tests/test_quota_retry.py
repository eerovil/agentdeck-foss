import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from agentdeck.db import Db
from agentdeck.inject import InjectionStatus, QueuedMessage
from agentdeck.models import Capability, InjectResult, Session, SessionStatus
from agentdeck.quota_retry import QuotaRetryService, message_for
from agentdeck.scheduled_messages import ScheduledMessageService


def _session(reset: datetime, *, key: str = "claude_code:main:source") -> Session:
    return Session(
        key=key,
        account_key="claude_code:main",
        session_id=key.rpartition(":")[2],
        status=SessionStatus.LIVE,
        turn_failed=True,
        automatic_retry_at=reset,
        chat_id="chat-1",
        capabilities=frozenset({Capability.INJECT}),
    )


def test_quota_retry_is_one_durable_message_for_the_provider_boundary(tmp_path):
    now = datetime(2026, 10, 12, 16, 10, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    session = _session(reset)
    store = Db(tmp_path / "agentdeck.db")
    scheduled = ScheduledMessageService(store, None, lambda _chat: None)
    service = QuotaRetryService(
        lambda: (session,), lambda key: key, scheduled.schedule, scheduled.exists
    )

    assert service.reconcile(now=now) == 1
    assert service.reconcile(now=now + timedelta(minutes=1)) == 0

    (message,) = store.load_visible_scheduled_messages("chat-1")
    assert message.due_at == reset
    assert message.quota_retry == (session.key, reset.timestamp())
    assert "continue from the latest user message" in message.text.lower()
    assert message.id in scheduled.pending_ids()
    assert scheduled.cancel(message.id, message.chat_id)
    assert message.id not in scheduled.pending_ids()


def test_quota_retry_is_only_scheduled_for_the_current_chat_leg(tmp_path):
    session = _session(datetime(2026, 10, 12, 17, 10, tzinfo=UTC))
    store = Db(tmp_path / "agentdeck.db")
    scheduled = ScheduledMessageService(store, None, lambda _chat: None)
    service = QuotaRetryService(
        lambda: (session,),
        lambda _key: "claude_code:main:newer-leg",
        scheduled.schedule,
        scheduled.exists,
    )

    assert service.reconcile() == 0
    assert store.load_visible_scheduled_messages("chat-1") == ()


def test_only_scheduled_or_dispatching_quota_rows_are_pending(tmp_path):
    session = _session(datetime(2026, 10, 12, 17, 10, tzinfo=UTC))
    store = Db(tmp_path / "agentdeck.db")
    scheduled = ScheduledMessageService(store, None, lambda _chat: None)
    retry = message_for(session)
    assert retry is not None
    assert scheduled.schedule(retry) is not None
    assert retry.id in scheduled.pending_ids()

    assert store.claim_scheduled_message(retry.id)
    assert retry.id in scheduled.pending_ids()
    store.settle_scheduled_message(retry.id, "failed", "refused")

    assert retry.id not in scheduled.pending_ids()


class _Injector:
    def __init__(self) -> None:
        self.calls = []

    async def start(self, _account, session, _provider, text, **kwargs):
        self.calls.append((session.key, text, kwargs["client_action_id"]))
        return InjectResult(True)

    async def wait_for_delivery(self, _session_key):
        return None

    def status(self, _session_key):
        identity = self.calls[-1][2]
        return InjectionStatus(
            "accepted",
            items=(QueuedMessage(1, "retry", client_action_id=identity, state="accepted"),),
        )

    def can_queue(self, _session_key):
        return False


async def _dispatch(store, target):
    injector = _Injector()
    service = ScheduledMessageService(
        store,
        injector,
        lambda _chat: SimpleNamespace(account=object(), session=target, provider=object()),
    )
    await service.dispatch_due(datetime.now(UTC) + timedelta(days=2))
    await asyncio.gather(*service._chat_tasks.values())
    return injector


async def test_due_quota_retry_continues_the_same_still_failed_chat(tmp_path):
    reset = datetime.now(UTC) - timedelta(seconds=1)
    session = _session(reset)
    store = Db(tmp_path / "agentdeck.db")
    assert store.record_scheduled_message(message_for(session)) is not None

    injector = await _dispatch(store, session)

    assert len(injector.calls) == 1
    assert store.load_visible_scheduled_messages("chat-1") == ()


async def test_due_quota_retry_is_cancelled_after_the_chat_moves_on(tmp_path):
    reset = datetime.now(UTC) - timedelta(seconds=1)
    source = _session(reset)
    successor = _session(reset, key="claude_code:alt:successor")
    store = Db(tmp_path / "agentdeck.db")
    assert store.record_scheduled_message(message_for(source)) is not None

    injector = await _dispatch(store, successor)

    assert injector.calls == []
    assert store.load_visible_scheduled_messages("chat-1") == ()


# --- a Work's chat moves to another listed account (#3362) -------------------


def _account(key: str):
    from pathlib import Path

    from agentdeck.models import Account

    provider, _, label = key.partition(":")
    return Account(key=key, provider_id=provider, label=label, root=Path("/nonexistent"))


def _moves(tmp_path, session, accounts, *, repository="owner/repo", started=None):
    from agentdeck.usage_move import UsageMoveService

    started = [] if started is None else started
    store = Db(tmp_path / "agentdeck.db")

    async def start(move):
        # As ``start_moved_leg`` does at the provider boundary.
        store.mark_continued_chat_dispatched(move.token)
        started.append(move)
        return "started"

    service = UsageMoveService(
        lambda: (session,),
        lambda key: key,
        lambda _session: repository,
        lambda _repository: [_account(key) for key in accounts],
        lambda: tmp_path / "usage",
        store,
        start,
        lambda key: f"http://deck/sessions/{key}/transcript.json",
    )
    return service, started


def _work_session(tmp_path, reset, **changes):
    from dataclasses import replace

    return replace(
        _session(reset, key="claude_code:alt:source"),
        account_key="claude_code:alt",
        owned_role="work",
        cwd=tmp_path,
        **changes,
    )


async def test_a_work_chat_the_limit_stopped_moves_once_to_the_next_listed_account(tmp_path):
    now = datetime.now(UTC)
    session = _work_session(tmp_path, now + timedelta(hours=2))
    accounts = ["claude_code:alt", "claude_code:main"]
    service, started = _moves(tmp_path, session, accounts)

    assert await service.reconcile(now=now) == 1
    (move,) = started
    assert move.account.key == "claude_code:main"
    assert "The previous account (alt) reached its usage limit" in move.message
    assert "http://deck/sessions/claude_code:alt:source/transcript.json" in move.message
    # The same stop never starts a second chat, not even after a restart.
    assert await service.reconcile(now=now) == 0
    again, started_again = _moves(tmp_path, session, accounts)
    assert await again.reconcile(now=now) == 0
    assert started_again == []
    # A restart between the reservation and the start resumes it: nothing was started.
    from dataclasses import replace

    from agentdeck.usage_move import move_token

    later = replace(session, automatic_retry_at=now + timedelta(hours=3))
    Db(tmp_path / "agentdeck.db").reserve_continued_chat_start(move_token(later), later.key)
    resumed, started_resumed = _moves(tmp_path, later, accounts)
    assert await resumed.reconcile(now=now) == 1
    assert len(started_resumed) == 1


async def test_a_stopped_chat_waits_when_it_cannot_usefully_move(tmp_path, monkeypatch):
    from dataclasses import replace

    from agentdeck.poller_engine import github

    now = datetime.now(UTC)
    far = _work_session(tmp_path, now + timedelta(hours=2))
    both = ["claude_code:alt", "claude_code:main"]
    for session, accounts in (
        (_work_session(tmp_path, now + timedelta(minutes=10)), both),  # resets soon
        (replace(far, owned_role=None), both),  # not a Work's agent
        (far, ["claude_code:alt"]),  # its project lists no other account
    ):
        service, started = _moves(tmp_path, session, accounts)
        assert await service.reconcile(now=now) == 0
        assert started == []
    service, started = _moves(tmp_path, far, both, repository=None)
    assert await service.reconcile(now=now) == 0
    monkeypatch.setattr(
        github, "account_refusal_detail", lambda _cache, _account, **_: ("5h at 100%", None)
    )
    service, started = _moves(tmp_path, far, both)
    assert await service.reconcile(now=now) == 0
    assert started == []


async def test_a_moved_leg_starts_as_the_same_chat_with_the_work_role(tmp_path):
    from agentdeck.usage_move import RETRY, STARTED, Move, start_moved_leg

    session = _work_session(tmp_path, datetime.now(UTC) + timedelta(hours=2))
    calls = []
    token = "a" * 32

    class _State:
        successor = "new"

        def chat_newest_leg_key(self, key):
            return key

        def reserve_chat_successor(self, token, source, newest_run=None):
            calls.append(("reserve", token, source))
            return self.successor

        def release_chat_successor(self, token):
            calls.append(("release", token))

        def pending_chat_successor(self, _source):
            return None  # the conflict's own start is not known here

    class _Starter:
        accept = True
        answer = None

        async def start_new(self, account, provider, cwd, message, **kwargs):
            calls.append(("start", account.key, cwd, kwargs))
            return InjectResult(self.accept)

        async def new_start_answer(self, _account_key):
            return self.answer

    class _Db:
        marks = True

        def load_account_new_chat_defaults(self, _key):
            return {}

        def planned_resets(self):
            return {}

        def mark_continued_chat_dispatched(self, token):
            calls.append(("dispatched", token))
            return self.marks

        def release_continued_chat_start(self, token):
            calls.append(("released", token))

    state, starter, store = _State(), _Starter(), _Db()
    app = SimpleNamespace(state=SimpleNamespace(app_state=state, injector=starter, db=store))
    move = Move(session, _account("claude_code:main"), token, "carry on")

    assert await start_moved_leg(app, move) == STARTED
    reserve, dispatched, start = calls
    assert reserve == ("reserve", token, session.key)
    assert dispatched == ("dispatched", token)
    assert start[1:3] == ("claude_code:main", tmp_path)
    kwargs = start[3]
    assert kwargs["role"] == "work"
    assert kwargs["session_token"] == token
    assert kwargs["source_session_key"] == session.key
    assert kwargs["model"] is None
    # Every proof that nothing started frees both fences and asks for another try:
    # a refusal up front (another start running), and a clean refusal from the spawn.
    for refusal in ("now", "later"):
        calls.clear()
        starter.accept = refusal != "now"
        starter.answer = InjectResult(False, "at_capacity") if refusal == "later" else None
        assert await start_moved_leg(app, move) == RETRY
        assert calls[-2:] == [("release", token), ("released", token)]
    # An unknown answer may have launched, so it counts as a start.
    starter.accept, starter.answer = True, None
    assert await start_moved_leg(app, move) == STARTED
    # A fence that did not land starts nothing; a busy successor fence starts nothing.
    calls.clear()
    store.marks = False
    assert await start_moved_leg(app, move) == RETRY
    assert not any(call[0] == "start" for call in calls)
    calls.clear()
    store.marks, state.successor = True, "conflict"
    assert await start_moved_leg(app, move) == RETRY
    assert [call[0] for call in calls] == ["reserve"]


async def test_a_move_that_started_nothing_is_tried_again_after_a_pause(tmp_path):
    from agentdeck.usage_move import RETRY, RETRY_AFTER, STARTED

    now = datetime.now(UTC)
    session = _work_session(tmp_path, now + timedelta(hours=2))
    service, started = _moves(tmp_path, session, ["claude_code:alt", "claude_code:main"])
    outcomes = [RETRY, STARTED]
    start = service._start

    async def flaky(move):
        await start(move)
        outcome = outcomes.pop(0)
        if outcome == RETRY:  # as ``start_moved_leg`` frees its fence when nothing started
            service._db.release_continued_chat_start(move.token)
        return outcome

    service._start = flaky
    service._clock = lambda: now
    assert await service.reconcile(now=now) == 0
    assert await service.reconcile(now=now + RETRY_AFTER / 2) == 0
    assert await service.reconcile(now=now + RETRY_AFTER) == 1
    assert len(started) == 2


async def test_a_retry_is_made_at_its_time_with_no_chat_changing(tmp_path, monkeypatch):
    from agentdeck import usage_move
    from agentdeck.events import EventBus

    monkeypatch.setattr(usage_move, "RETRY_AFTER", timedelta(milliseconds=20))
    session = _work_session(tmp_path, datetime.now(UTC) + timedelta(hours=2))
    service, started = _moves(tmp_path, session, ["claude_code:alt", "claude_code:main"])
    outcomes = [usage_move.RETRY, usage_move.STARTED]
    start, moved = service._start, asyncio.Event()

    async def flaky(move):
        await start(move)
        outcome = outcomes.pop(0)
        if outcome == usage_move.RETRY:
            # A refusal slower than the retry pause: its deadline counts from here.
            await asyncio.sleep(0.05)
            service._db.release_continued_chat_start(move.token)
        else:
            moved.set()
        return outcome

    service._start = flaky
    service._bus = EventBus()  # nothing is ever published on it
    service.start()
    try:
        await asyncio.wait_for(moved.wait(), timeout=2)
    finally:
        await service.stop()
    assert len(started) == 2
