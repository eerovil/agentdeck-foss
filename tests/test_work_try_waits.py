"""Deployed work unblocks the issues it blocks (#4384): the set the overview pass keeps, and
the note the web sends the router once per live-check record."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck.db import Db
from agentdeck.services_link import ServicesUnavailable, ServicesUncertain
from agentdeck.work_overview import _reopened
from agentdeck.work_try_waits import TryWaits, deployed_set

A = "github_issue:acme/x#1"
B = "github_issue:acme/x#2"
AT = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)


def card(work_id, kind="try_wait", reopened=False):
    return SimpleNamespace(work_id=work_id, derived=SimpleNamespace(kind=kind), reopened=reopened)


class Router:
    enabled = True

    def __init__(self, db, answers=None):
        self.db = db
        self.sent = []
        self.answers = answers or {}

    async def note_deployed(self, repository, number):
        self.sent.append(f"{repository}#{number}")
        answer = self.answers.get(number)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        # As the services process does: read the saved set.
        return {"in_set": f"github_issue:{repository}#{number}" in self.db.load_try_waits()}


@pytest.fixture
def db(tmp_path):
    db = Db(tmp_path / "control.db")
    yield db
    db.close()


def deck(db, router=None):
    app = SimpleNamespace(state=SimpleNamespace(control_db=db, work_router=router))
    lock = asyncio.Lock()
    return TryWaits(app, lock), lock


def checked(db, work_id, at):
    db.set_work_live_check(work_id, "needs_person", "try it", "s1")
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE work_metadata SET live_check_at = ? WHERE work_id = ?",
            (at.isoformat(), work_id),
        )


async def keep(waits, lock, cards, *, degraded=False):
    async with lock:
        await waits.keep(cards, degraded=degraded)
    if waits._task is not None:
        await waits._task


def test_the_set_is_waiting_for_a_try_and_never_reopened():
    cards = [card(A), card(B, reopened=True), card("github_issue:acme/x#3", kind="in_review")]
    assert deployed_set(cards) == {A}
    # #4585: a reopen that started a round is read from that round's own check.
    in_round = card(B, reopened=True)
    in_round.round_started_at = datetime(2026, 11, 20, tzinfo=UTC)
    assert deployed_set([card(A), in_round]) == {A, B}
    assert _reopened({"state": "open", "state_reason": "reopened"})
    assert _reopened({"state": "OPEN", "state_reason": "REOPENED"})
    assert not _reopened({"state": "open", "state_reason": None})
    assert not _reopened({"state": "closed", "state_reason": "completed"})
    assert not _reopened(None)


async def test_the_set_is_kept_only_under_the_overview_lock(db):
    waits, _lock = deck(db)
    with pytest.raises(RuntimeError, match="lock"):
        await waits.keep([card(A)], degraded=False)
    assert db.load_try_waits() == frozenset()


async def test_a_record_is_sent_once_and_again_when_checked_again(db):
    router = Router(db)
    waits, lock = deck(db, router)
    checked(db, A, AT)
    await keep(waits, lock, [card(A)])
    assert db.load_try_waits() == {A}
    assert router.sent == ["acme/x#1"]
    assert db.load_try_waits_noted() == {f"{A}@{AT.isoformat()}"}
    await keep(waits, lock, [card(A)])
    assert router.sent == ["acme/x#1"]
    # A fresh deck reads the noted keys from their row and sends nothing more either.
    fresh, fresh_lock = deck(db, router)
    await keep(fresh, fresh_lock, [card(A)])
    assert router.sent == ["acme/x#1"]
    # The agent records the check again: a new record, sent again.
    later = AT + timedelta(hours=1)
    checked(db, A, later)
    await keep(waits, lock, [card(A)])
    assert router.sent == ["acme/x#1", "acme/x#1"]
    assert db.load_try_waits_noted() == {f"{A}@{later.isoformat()}"}


async def test_a_note_and_a_set_change_in_one_pass_leave_both_rows_right(db):
    router = Router(db)
    waits, lock = deck(db, router)
    checked(db, A, AT)
    checked(db, B, AT)
    await keep(waits, lock, [card(A)])
    # One pass: B comes in, A goes out (its issue closed, or a pull request opened).
    await keep(waits, lock, [card(B)])
    assert db.load_try_waits() == {B}
    assert db.load_try_waits_noted() == {f"{B}@{AT.isoformat()}"}
    assert router.sent == ["acme/x#1", "acme/x#2"]


@pytest.mark.parametrize(
    "answer",
    [ServicesUnavailable("down"), ServicesUncertain("lost"), {"in_set": False}],
)
async def test_an_unsent_or_unseen_note_stays_unnoted(db, answer):
    router = Router(db, {1: answer})
    waits, lock = deck(db, router)
    checked(db, A, AT)
    await keep(waits, lock, [card(A)])
    assert db.load_try_waits() == {A}
    assert db.load_try_waits_noted() == set()
    # The next pass sends it again.
    router.answers = {}
    await keep(waits, lock, [card(A)])
    assert router.sent == ["acme/x#1", "acme/x#1"]
    assert db.load_try_waits_noted() == {f"{A}@{AT.isoformat()}"}


async def test_a_degraded_pass_adds_no_work_and_drops_no_note(db):
    router = Router(db)
    waits, lock = deck(db, router)
    checked(db, A, AT)
    checked(db, B, AT)
    await keep(waits, lock, [card(A)])
    await keep(waits, lock, [card(B)], degraded=True)
    # B is not added, A, missing from a pass whose reads failed, is taken out; nothing is sent
    # and A's note stays.
    assert db.load_try_waits() == frozenset()
    assert router.sent == ["acme/x#1"]
    assert db.load_try_waits_noted() == {f"{A}@{AT.isoformat()}"}


async def test_a_failed_set_write_hands_nothing_over(db, monkeypatch):
    router = Router(db)
    waits, lock = deck(db, router)
    checked(db, A, AT)
    monkeypatch.setattr(db, "save_try_waits", lambda works: False)
    await keep(waits, lock, [card(A)])
    assert router.sent == [] and db.load_try_waits_noted() == set()
    monkeypatch.undo()
    await keep(waits, lock, [card(A)])
    assert router.sent == ["acme/x#1"] and db.load_try_waits() == {A}


async def test_a_disabled_router_is_never_asked(db):
    router = Router(db)
    router.enabled = False
    waits, lock = deck(db, router)
    checked(db, A, AT)
    await keep(waits, lock, [card(A)])
    assert db.load_try_waits() == {A}
    assert router.sent == [] and db.load_try_waits_noted() == set()


async def test_two_passes_save_in_the_order_they_hold_the_lock(db):
    """The older pass saves first, whatever order the builds finish in: the newer set stays."""
    waits, lock = deck(db)
    first_in = asyncio.Event()

    async def older():
        async with lock:
            first_in.set()
            await asyncio.sleep(0)  # its build is slow; the newer pass waits on the lock
            await waits.keep([card(A)], degraded=False)

    async def newer():
        await first_in.wait()
        async with lock:
            await waits.keep([card(B)], degraded=False)

    await asyncio.gather(newer(), older())
    if waits._task is not None:
        await waits._task
    assert db.load_try_waits() == {B}


async def test_an_unreadable_row_reads_as_no_deployed_work(db):
    assert db.save_try_waits([A])
    with db._lock, db._conn:
        db._conn.execute("UPDATE work_try_waits SET payload = 'nope' WHERE name = 'set'")
    assert db.load_try_waits() == frozenset()
    assert db.load_try_waits_noted() == set()


async def test_an_unreadable_row_is_rewritten_by_the_next_complete_pass(db, monkeypatch):
    """#4384 review: an unreadable row is not taken for an empty set, so a complete empty
    pass writes it rather than leaving the old row's Works deployed; a degraded pass, with
    nothing known to take out of, writes nothing."""
    assert db.save_try_waits([A])
    waits, lock = deck(db)
    monkeypatch.setattr(db, "read_try_waits", lambda: None)
    await keep(waits, lock, [], degraded=True)
    assert db.load_try_waits() == {A} and not waits.swept
    await keep(waits, lock, [])
    assert db.load_try_waits() == frozenset() and waits.swept


async def test_a_quiet_start_owes_one_complete_pass_with_nobody_watching(db, monkeypatch):
    """#4384 review: after a restart with no viewer and no event, the overview still makes
    one complete pass, so Works already waiting for a try unblock what they block. A
    degraded pass leaves the pass owed, tried again after a wait."""
    from agentdeck.events import EventBus
    from agentdeck.web import work_view

    assert db.save_try_waits([A])
    bus = EventBus()
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(bus=bus), control_db=db))
    passes = []

    async def load(_app, *, now, changed_since=None):
        passes.append(now)
        return work_view.WorkOverview(degraded=("gh",) if len(passes) == 1 else ())

    async def scanned(_app):
        return True

    monkeypatch.setattr(work_view, "load_work_overview", load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    monkeypatch.setattr(work_view, "time_deadline", lambda *args: None)
    monkeypatch.setattr(work_view.WorkOverviewService, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(work_view.WorkOverviewService, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(work_view.WorkOverviewService, "RETRY_MIN_S", 0.01)
    service = work_view.WorkOverviewService(app)
    service.start()
    try:
        for _ in range(200):
            if service.try_waits.swept:
                break
            await asyncio.sleep(0.005)
        assert service.try_waits.swept
        assert len(passes) == 2
        assert db.load_try_waits() == frozenset()
        await asyncio.sleep(0.05)
        assert len(passes) == 2  # swept: nothing more is owed with nobody watching
    finally:
        await service.stop()
