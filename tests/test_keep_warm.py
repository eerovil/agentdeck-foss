"""The keep-warm ping of a waiting Work chat (#4737, #4752): who gets one, when, and only
once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from worker_harness import settle

from agentdeck import keep_warm
from agentdeck.db import Db
from agentdeck.deck_notices import KEEP_WARM_TEXT
from agentdeck.models import CacheCall, InjectResult, Session, SessionStatus, UsageSnapshot
from agentdeck.scheduled_messages import ScheduledMessage

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
CHAT = "claude_code:main:sid-1"


def _minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


def _session(*, call_at=T0, last=T0, read=90_000, written=400, lifetime=HOUR, **over) -> Session:
    return Session(
        key=CHAT,
        account_key="claude_code:main",
        session_id="sid-1",
        status=over.pop("status", SessionStatus.LIVE),
        last_message_at=last,
        cache_call=CacheCall(call_at, lifetime, read, written),
        **over,
    )


def _card(work_id, kind, paused=False):
    return SimpleNamespace(work_id=work_id, derived=SimpleNamespace(kind=kind), paused=paused)


@pytest.mark.parametrize(
    ("cards", "expected"),
    [
        ([("o/r#1", "waiting")], ("a",)),
        ([("o/r#1", "review")], ("a",)),
        ([("o/r#1", "review_queued")], ("a",)),
        ([("o/r#1", "queued")], ("a",)),
        # Waiting on a person, in any wording the deck has for it, is no ping.
        ([("o/r#1", "needs_you")], ()),
        ([("o/r#1", "working")], ()),
        ([("o/r#1", "waiting", True)], ()),
        # A Work that is over says nothing about its chat; one that needs a person does.
        ([("o/r#1", "waiting"), ("o/r#2", "closed")], ("a",)),
        ([("o/r#1", "waiting"), ("o/r#2", "needs_you")], ()),
        ([("o/r#2", "needs_you"), ("o/r#1", "waiting")], ()),
        ([("o/r#1", "closed")], ()),
        # A Work nobody owns has no chat to ping.
        ([("o/x#9", "waiting")], ()),
        ([("o/r#1", "waiting"), ("o/q#3", "queued")], ("a", "b")),
    ],
)
def test_only_a_chat_whose_works_all_wait_on_the_deck_is_pinged(cards, expected):
    owners = {"o/r#1": "a", "o/r#2": "a", "o/q#3": "b"}
    assert keep_warm.waiting_chats([_card(*card) for card in cards], owners.get) == expected


def test_a_ping_is_due_45_to_50_minutes_after_the_last_call_for_four_hours():
    # Counted from the call's own row, not from the chat's last message.
    assert keep_warm.window(_session(call_at=T0 + _minutes(3))) == (
        T0 + _minutes(48),
        T0 + _minutes(53),
    )
    # Each ping's own call opens the next window, while that falls within four hours of
    # the last real turn: the fifth ping at 3 h 45 min is the last.
    warm_ping = dict(keep_warm_turn=True, last=T0)
    assert keep_warm.window(_session(call_at=T0 + _minutes(180), **warm_ping)) is not None
    assert keep_warm.window(_session(call_at=T0 + _minutes(194), **warm_ping)) is not None
    assert keep_warm.window(_session(call_at=T0 + _minutes(195), **warm_ping)) is None
    # A warm ping writes a little and reads the rest: the pings go on.
    assert keep_warm.window(_session(read=90_000, written=1_200, **warm_ping)) is not None
    # A ping that came too late rebuilt the cache, and ends the series ...
    assert keep_warm.window(_session(read=0, written=90_000, **warm_ping)) is None
    assert keep_warm.window(_session(read=20_000, written=70_000, **warm_ping)) is None
    # ... until the next real turn, whose own cold start is no reason to stop.
    assert keep_warm.window(_session(read=0, written=90_000)) is not None


@pytest.mark.parametrize(
    "over",
    [
        {"lifetime": timedelta(minutes=5)},
        {"lifetime": None},
        {"status": SessionStatus.REMOTE},
        {"thinking": True},
        {"question": "Which one?"},
        {"turn_failed": True},
        {"turn_cut_off": True},
        {"automatic_retry_at": T0 + HOUR},
        {"last": None},
    ],
)
def test_a_chat_that_is_working_failed_or_without_an_hour_cache_gets_no_ping(over):
    assert keep_warm.window(_session(**over)) is None
    # #4752: a stopped process is no reason. The runtime may resume it.
    assert keep_warm.window(_session(status=SessionStatus.IDLE)) == (
        T0 + _minutes(45),
        T0 + _minutes(50),
    )


def _wait(kind="time", hours=6.0, check=""):
    deadline = (T0 + timedelta(hours=hours)).timestamp()
    return SimpleNamespace(kind=kind, deadline=deadline, check_command=check)


@pytest.mark.parametrize(
    ("waits", "far"),
    [
        ([_wait()], True),
        ([_wait(), _wait(hours=30)], True),
        # A watch and a needs-you wait keep no chat waiting, so they change nothing.
        ([_wait(), _wait("watch", 1), _wait("needs_you", 1)], True),
        ([_wait(hours=5)], False),
        ([_wait(hours=4.9)], False),
        ([_wait(), _wait(hours=2)], False),
        # A check may end the wait at any run of it, and an event at any moment.
        ([_wait(check="test -e done")], False),
        ([_wait(), _wait("pull_request")], False),
        ([_wait("deck_deploy")], False),
        # No wait at all says nothing about when the chat is woken: it keeps its pings.
        ([], False),
        ([_wait("watch", 9)], False),
    ],
)
def test_a_chat_whose_only_wait_is_a_time_over_five_hours_away_is_not_pinged(waits, far):
    assert keep_warm.waits_only_on_a_far_time(waits, T0) is far


def test_the_deck_pass_skips_a_far_wait_unless_something_else_may_wake_the_chat(monkeypatch):
    """#4752: a wake that far away is cold whatever was sent, unless a review, a merge
    queue, a test run or a job may wake the chat before it."""
    from agentdeck import work_router

    owners = {1: "far", 2: "near", 3: "review", 4: "tests", 5: "job", 6: "none"}
    monkeypatch.setattr(
        work_router, "owner_of_issue", lambda state, repo, number, proj: (owners[number],)
    )
    waits = {chat: (_wait(),) for chat in owners.values()}
    waits |= {"near": (_wait(hours=2),), "none": ()}
    tended = []
    app = SimpleNamespace(
        state=SimpleNamespace(
            keep_warm=SimpleNamespace(
                tend=lambda chats, now: tended.append(list(chats)),
                # Counted from the last real turn: at 15:00 a wait at 17:30 is 2.5 hours
                # away, and still past any cache a ping of a turn at 12:00 can leave.
                last_turn=lambda chat: None if chat == "none" else T0,
            ),
            app_state=SimpleNamespace(work_projection=lambda: None),
            wake_on=SimpleNamespace(registry=SimpleNamespace(for_chat=waits.__getitem__)),
        )
    )
    cards = [_card(f"github_issue:o/r#{n}", "review" if n == 3 else "waiting") for n in owners]
    cards[3].verification = SimpleNamespace(status="running")
    cards[4].jobs = (object(),)

    waits["late"] = (_wait(hours=5.5),)
    owners[7] = "late"
    cards.append(_card("github_issue:o/r#7", "waiting"))
    keep_warm.tend_waiting_chats(app, cards, now=T0 + timedelta(hours=3))
    assert tended == [["near", "review", "tests", "job", "none"]]
    # A deck with no wait registry judges as before.
    del app.state.wake_on
    keep_warm.tend_waiting_chats(app, cards, now=T0 + timedelta(hours=3))
    assert tended[1] == ["far", "near", "review", "tests", "job", "none", "late"]


def test_an_account_at_its_limit_or_of_unknown_usage_is_not_spent_on():
    def usage(*values, stale=False):
        return UsageSnapshot(
            account_key="claude_code:main",
            five_hour_pct=values[0] if values else None,
            five_hour_resets_at=None,
            seven_day_pct=values[1] if len(values) > 1 else None,
            seven_day_resets_at=None,
            fetched_at=T0,
            stale=stale,
        )

    assert keep_warm.at_limit(None)
    assert keep_warm.at_limit(usage(40.0, 100.0))
    assert not keep_warm.at_limit(usage(40.0, 99.0))
    assert not keep_warm.at_limit(usage(40.0))
    # A reading with no meter, or an old one kept after a failed read, says nothing.
    assert keep_warm.at_limit(usage())
    assert keep_warm.at_limit(usage(40.0, 99.0, stale=True))


class _Provider:
    def __init__(self, result=None):
        self.sent: list[tuple[str, str]] = []
        self.result = result or InjectResult(True)

    async def keep_warm(self, account, session, message, *, delivery_id):
        self.sent.append((message, delivery_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _service(tmp_path, session, provider, **flags):
    db = Db(tmp_path / "deck.db")
    target = SimpleNamespace(session=session, provider=provider, account=object())
    changed: list[str] = []
    service = keep_warm.KeepWarmService(
        db,
        lambda chat_id: target if chat_id == CHAT else None,
        queued=lambda key: flags.get("queued", False),
        at_limit=lambda key: flags.get("at_limit", False),
        on_change=changed.append,
    )
    return service, db, changed


async def test_a_due_ping_is_sent_once_and_the_next_pass_is_named(tmp_path):
    provider = _Provider()
    service, db, changed = _service(tmp_path, _session(), provider)

    # Too early: nothing is sent, and the pass is told when to come back.
    assert service.tend([CHAT, "gone"], T0 + _minutes(10)) == T0 + _minutes(45)
    # Sent: the next pass comes when the answer's own call has opened the next window.
    assert service.tend([CHAT], T0 + _minutes(46)) == T0 + _minutes(48)
    assert service.tend([CHAT], T0 + _minutes(49)) == T0 + _minutes(51)  # a repeated pass
    await settle()

    key = keep_warm.message_id(CHAT, T0)
    assert provider.sent == [(KEEP_WARM_TEXT, key)]
    assert changed == [CHAT]
    assert db.load_scheduled_message(key).state == "delivered"
    # A second service, as after a web restart, finds the row and sends nothing.
    again, _db, _changed = _service(tmp_path, _session(), provider)
    again.tend([CHAT], T0 + _minutes(48))
    await settle()
    assert len(provider.sent) == 1


async def test_a_ping_past_its_window_is_not_sent_late(tmp_path):
    provider = _Provider()
    service, _db, _changed = _service(tmp_path, _session(), provider)
    assert service.tend([CHAT], T0 + _minutes(50)) is None
    await settle()
    assert provider.sent == []


@pytest.mark.parametrize("flag", ["queued", "at_limit", "stored"])
async def test_no_ping_while_a_message_is_on_its_way_or_the_account_is_spent(tmp_path, flag):
    provider = _Provider()
    service, db, _changed = _service(tmp_path, _session(), provider, **{flag: True})
    if flag == "stored":
        db.record_scheduled_message(ScheduledMessage("m1", CHAT, "a real message", T0, T0))

    service.tend([CHAT], T0 + _minutes(46))
    await settle()

    assert provider.sent == []
    assert db.load_scheduled_message(keep_warm.message_id(CHAT, T0)) is None


@pytest.mark.parametrize(
    "result", [InjectResult(False, "active_turn"), RuntimeError("socket closed")]
)
async def test_a_refused_ping_is_recorded_and_never_tried_again(tmp_path, result):
    provider = _Provider(result)
    service, db, changed = _service(tmp_path, _session(), provider)

    service.tend([CHAT], T0 + _minutes(46))
    await settle()
    service.tend([CHAT], T0 + _minutes(47))
    await settle()

    row = db.load_scheduled_message(keep_warm.message_id(CHAT, T0))
    assert row.state == "failed" and row.reason
    assert len(provider.sent) == 1 and changed == []


def test_a_pass_that_failed_a_read_or_a_deck_without_the_service_pings_nothing():
    app = SimpleNamespace(state=SimpleNamespace(keep_warm=None))
    assert keep_warm.tend_waiting_chats(app, [_card("o/r#1", "waiting")], now=T0) is None
    cards = [_card("o/r#1", "waiting")]
    app.state.keep_warm = SimpleNamespace(tend=lambda *_: pytest.fail("pinged"), owed=False)
    assert keep_warm.tend_waiting_chats(app, cards, degraded=("jobs",), now=T0) is None
    # With a window still to come, a pass that could not judge is tried again soon.
    app.state.keep_warm.owed = True
    assert keep_warm.tend_waiting_chats(app, cards, degraded=("jobs",), now=T0) == (
        T0 + keep_warm.LOOK_AGAIN
    )


def test_only_a_message_still_on_its_way_counts_as_queued():
    def status(*pending):
        return SimpleNamespace(items=tuple(SimpleNamespace(is_pending=p) for p in pending))

    assert not keep_warm.has_pending(None)
    # A queue keeps its delivered and failed rows: a chat with history is not "queued".
    assert not keep_warm.has_pending(status(False, False))
    assert keep_warm.has_pending(status(False, True))
