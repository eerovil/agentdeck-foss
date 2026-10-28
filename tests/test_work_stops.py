"""Recently stopped, unseen Works and folding groups on the front page (#2283).

A Work *stops* when it enters a Needs you category. The deck records when, and when the
operator last opened the Work's page; a Work that stopped after that is *unseen*. Both are
this deck's own records: nothing is written to GitHub or the board.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from agentdeck.work_overview import work_href
from agentdeck.work_stops import FORGET_AFTER, WorkStop, is_unseen, track_stops
from agentdeck.work_story import FrontSections

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
A = "github_issue:eerovil/agentdeck#1"
B = "github_issue:eerovil/agentdeck#2"
C = "github_issue:eerovil/agentdeck#3"

# --- the rule ------------------------------------------------------------------------------------


def test_a_new_category_is_stamped_now_and_the_same_one_keeps_its_stamp():
    earlier = NOW - timedelta(hours=3)
    previous = {A: WorkStop(A, "needs_review", earlier), C: WorkStop(C, "needs_answer", earlier)}

    changed = track_stops(
        previous, {A: "needs_review", B: "work_error", C: "ready_for_merge"}, now=NOW
    )

    assert A not in changed  # still where it was: its stop is when it got there
    assert changed[B] == WorkStop(B, "work_error", NOW)
    # A review that passed moves it to another category: that is a new stop.
    assert changed[C] == WorkStop(C, "ready_for_merge", NOW)


def test_a_stop_found_after_an_unwatched_gap_is_dated_inside_the_gap():
    # Nobody watched from 11:00, so the next pass ran at noon (#2865). B's chat last moved
    # at 11:20, inside the gap: that is when it stopped. C last moved before the gap, so the
    # gap's start is the earliest it can have stopped; D moved after "now" by clock skew.
    since = NOW - timedelta(hours=1)
    changed = track_stops(
        {},
        {B: "needs_answer", C: "work_error", A: "needs_review"},
        now=NOW,
        seed={
            B: NOW - timedelta(minutes=40),
            C: NOW - timedelta(hours=5),
            A: NOW + timedelta(minutes=1),
        },
        changed_since=since,
    )

    assert changed[B].stopped_at == NOW - timedelta(minutes=40)
    assert changed[C].stopped_at == since
    assert changed[A].stopped_at == NOW
    # With no activity known at all, the gap's start.
    assert track_stops({}, {B: "work_error"}, now=NOW, changed_since=since)[B].stopped_at == since


def test_leaving_needs_you_is_remembered_so_coming_back_is_a_new_stop():
    earlier = NOW - timedelta(hours=3)
    previous = {A: WorkStop(A, "needs_answer", earlier)}

    left = track_stops(previous, {A: "", B: ""}, now=NOW)
    assert left == {A: WorkStop(A, "", NOW)}  # B never stopped: nothing to remember

    back = track_stops({A: left[A]}, {A: "needs_answer"}, now=NOW + timedelta(minutes=5))
    assert back[A].stopped_at == NOW + timedelta(minutes=5)
    # A Work the pass did not build is left as it is.
    assert track_stops(previous, {}, now=NOW) == {}


# --- a reclassification is not a stop (#2332) ----------------------------------------------------


def test_a_category_that_changes_while_the_facts_do_not_keeps_its_real_date():
    """bazaar#306: a deploy re-read an unchanged Work into another category, and it looked like
    it had stopped that morning rather than on 09-17."""
    stopped = NOW - timedelta(days=10)
    previous = {A: WorkStop(A, "needs_review", NOW - timedelta(days=1), "m1")}

    changed = track_stops(previous, {A: "needs_retry"}, now=NOW, seed={A: stopped},
                          marks={A: "m1"})

    assert changed[A] == WorkStop(A, "needs_retry", stopped, "m1")
    assert not is_unseen(changed[A], NOW - timedelta(days=2))


def test_a_reclassification_with_no_activity_known_keeps_the_old_date():
    old = NOW - timedelta(days=1)
    previous = {A: WorkStop(A, "needs_review", old, "m1")}

    changed = track_stops(previous, {A: "ready_for_merge"}, now=NOW, marks={A: "m1"})

    assert changed[A].stopped_at == old


def test_a_category_change_with_changed_facts_is_a_new_stop():
    """A clean verdict or an engine park leaves no deck activity; its facts moved, so it is
    new — the signal a plain last-activity date would have lost."""
    previous = {A: WorkStop(A, "needs_review", NOW - timedelta(days=1), "m1")}

    changed = track_stops(previous, {A: "ready_for_merge"}, now=NOW,
                          seed={A: NOW - timedelta(days=3)}, marks={A: "m2"})

    assert changed[A] == WorkStop(A, "ready_for_merge", NOW, "m2")
    assert is_unseen(changed[A], NOW - timedelta(hours=1))


def test_a_row_with_no_mark_is_a_real_change_once_and_then_carries_one():
    previous = {A: WorkStop(A, "needs_review", NOW - timedelta(days=1))}

    changed = track_stops(previous, {A: "ready_for_merge"}, now=NOW, marks={A: "m1"})

    assert changed[A] == WorkStop(A, "ready_for_merge", NOW, "m1")


def test_facts_are_remembered_for_a_work_that_never_stopped_and_one_whose_facts_moved():
    earlier = NOW - timedelta(hours=2)
    previous = {B: WorkStop(B, "needs_review", earlier, "m1")}

    changed = track_stops(previous, {A: "", B: "needs_review"}, now=NOW,
                          marks={A: "a1", B: "m2"})

    assert changed == {
        A: WorkStop(A, "", NOW, "a1"),
        B: WorkStop(B, "needs_review", earlier, "m2"),  # same stop, same date, new facts
    }
    # Moved into Needs you by a reading change alone: dated by its real activity.
    again = track_stops({A: changed[A]}, {A: "needs_answer"}, now=NOW,
                        seed={A: NOW - timedelta(days=5)}, marks={A: "a1"})
    assert again[A].stopped_at == NOW - timedelta(days=5)


def test_the_stops_table_keeps_the_mark(tmp_path):
    from agentdeck.db import Db

    control = Db(tmp_path / "control.sqlite3")
    control.save_work_stops(
        [WorkStop(A, "needs_review", NOW, "m1"), WorkStop(B, "needs_answer", NOW)],
        forget_before=NOW - FORGET_AFTER,
    )

    assert control.work_stops() == {
        A: WorkStop(A, "needs_review", NOW, "m1"),
        B: WorkStop(B, "needs_answer", NOW),
    }


def test_the_first_pass_dates_what_already_waits_by_its_last_activity_never_later_than_now():
    seed = {A: NOW - timedelta(days=4), B: NOW + timedelta(hours=1), C: None}

    changed = track_stops({}, {A: "needs_answer", B: "work_error", C: "needs_review"},
                          now=NOW, seed=seed, bootstrap=True)

    assert changed[A].stopped_at == NOW - timedelta(days=4)
    assert changed[B].stopped_at == NOW
    assert changed[C].stopped_at == NOW
    # Once anything is stored, a new stop is dated now whatever its last activity says.
    later = track_stops({A: changed[A]}, {B: "work_error"}, now=NOW, seed=seed)
    assert later[B].stopped_at == NOW
    # An empty table is not the first pass: that is recorded separately.
    assert track_stops({}, {B: "work_error"}, now=NOW, seed=seed)[B].stopped_at == NOW


def test_unseen_is_a_stop_after_the_last_open_and_never_opened_counts():
    stop = WorkStop(A, "needs_review", NOW)

    assert is_unseen(stop, None)
    assert is_unseen(stop, NOW - timedelta(seconds=1))
    assert not is_unseen(stop, NOW)
    assert not is_unseen(stop, NOW + timedelta(minutes=1))
    # A naive stamp is UTC, and another zone is the same instant.
    assert not is_unseen(stop, (NOW + timedelta(minutes=1)).replace(tzinfo=None))
    assert not is_unseen(stop, NOW.astimezone(ZoneInfo("Europe/Helsinki")))
    # Not waiting on a person: nothing to see.
    assert not is_unseen(WorkStop(A, "", NOW), None)
    assert not is_unseen(None, None)


# --- the store -----------------------------------------------------------------------------------


def test_stops_and_opens_are_durable_and_an_open_never_moves_back(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    old = NOW - FORGET_AFTER - timedelta(days=1)
    db.save_work_stops(
        [WorkStop(A, "needs_answer", NOW), WorkStop(B, "", old), WorkStop(C, "", NOW)],
        forget_before=NOW - FORGET_AFTER,
    )
    stored = Db(tmp_path / "control.sqlite3").work_stops()
    # A Work that left Needs you long ago is forgotten; a recent leaver is kept.
    assert stored == {A: WorkStop(A, "needs_answer", NOW), C: WorkStop(C, "", NOW)}

    assert db.note_work_opened([A], NOW)
    assert db.note_work_opened([A], NOW - timedelta(hours=1))  # an older mark changes nothing
    assert db.works_opened() == {A: NOW}
    assert db.note_work_opened([A, B], NOW + timedelta(minutes=1))
    assert db.works_opened() == {A: NOW + timedelta(minutes=1), B: NOW + timedelta(minutes=1)}

    # The first pass is recorded once and keeps its stamp.
    assert db.work_stops_started() is None
    db.start_work_stops(NOW)
    db.start_work_stops(NOW + timedelta(hours=1))
    assert Db(tmp_path / "control.sqlite3").work_stops_started() == NOW


# --- through the deck ----------------------------------------------------------------------------


def _deck(tmp_path, monkeypatch):
    from test_work_hidden import _deck as hidden_deck

    app, _record = hidden_deck(tmp_path, monkeypatch)
    return app


def _pretend_it_left(app, work_id):
    """The Work was working again a moment ago, so it is back in Needs you now."""
    app.state.control_db.save_work_stops(
        [WorkStop(work_id, "", datetime.now(UTC) - timedelta(minutes=1))],
        forget_before=NOW - FORGET_AFTER,
    )


@pytest.mark.asyncio
async def test_turning_it_on_marks_nothing_already_waiting_as_new(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    overview = await app.state.work_overview.current()

    (row,) = overview.front.needs_you
    assert row.stopped_at is not None and not row.unseen
    assert [item.card.work_id for item in overview.front.stopped] == [WORK.work_id]
    assert overview.front.new == 0 and overview.front.summary[0][0] != "new"
    assert WORK.work_id in app.state.control_db.works_opened()


@pytest.mark.asyncio
async def test_a_first_pass_with_nothing_waiting_still_ends_the_first_pass(tmp_path, monkeypatch):
    from test_work_ui import WORK

    from agentdeck.web.work_view import _mark_stops

    app = _deck(tmp_path, monkeypatch)
    # The deck's first pass finds no Work waiting on a person.
    assert _mark_stops(app, [], datetime.now(UTC)) == []
    assert app.state.control_db.work_stops() == {}

    overview = await app.state.work_overview.current()

    # So the Work that stops next is a real stop, new until its page is opened.
    (row,) = overview.front.needs_you
    assert row.unseen and overview.front.new == 1
    assert WORK.work_id not in app.state.control_db.works_opened()


@pytest.mark.asyncio
async def test_a_work_that_stops_again_is_new_until_its_page_is_opened(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_work_ui import WORK

    from agentdeck.web.work_view import NEEDS_YOU_TOPIC, NeedsYouMenu, needs_you_menu

    app = _deck(tmp_path, monkeypatch)
    # A phone subscribed to pushes (#3348) is told once, and only after the first pass.
    phone = []
    app.state.push = SimpleNamespace(
        enabled=True, send_to_all=lambda *message: phone.append(message) or 1,
        clear_all=lambda tags: phone.append(("clear", tags)) or 1,
    )
    service = app.state.work_overview
    await service.current()
    assert app.state.control_db.load_needs_you_pushed() == set()
    _pretend_it_left(app, WORK.work_id)
    with app.state.app_state.bus.subscribe(NEEDS_YOU_TOPIC) as top_bar:
        await service.refresh()
        # The top bar's list moved, so it is pushed (#3239).
        assert top_bar.get_nowait() is not None
    await asyncio.gather(*service._push_tasks)
    (stop,) = app.state.control_db.load_needs_you_pushed()
    (told,) = service.overview.front.needs_you
    # The Work's stopped chat is where the push opens (#2512).
    assert told.card.chat_href
    assert phone == [(
        told.card.title, f"Needs retry · {told.source}", told.card.chat_href,
        f"work:{WORK.work_id}",
    )]
    await service.refresh()
    assert len(phone) == 1 and stop[0] == WORK.work_id
    tag = f"work:{WORK.work_id}"
    assert app.state.control_db.load_needs_you_showing() == {tag: {WORK.work_id}}

    front = app.state.work_overview.overview.front
    (row,) = front.needs_you
    assert row.unseen and front.stopped == (row,)
    menu = needs_you_menu(app.state.work_overview.overview)
    assert menu.rows == (row,) and menu.new == 1 and menu.more == 0
    assert front.new == 1 and front.stopped_new == 1
    assert front.summary[0] == ("new", 1, "new")
    (group,) = front.groups
    assert group.new == 1

    client = TestClient(app)
    page = client.get("/", params={"chats": "0"}).text
    # Every page's top bar lists it, linking to its stopped chat (#3263), whose opening marks
    # it seen.
    top_bar = page.split('id="needs-you-slot"', 1)[1].split("</details>", 1)[0]
    assert 'data-new="1"' in top_bar and 'aria-label="1 new Work needs you"' in top_bar
    assert f'href="{row.card.chat_href}"' in top_bar and "Needs retry" in top_bar
    # A Work with a chat links straight to it (#3263); opening the chat marks it seen too.
    chatty = replace(row, card=replace(row.card, chat_href="/chats/abc?work=w"))
    globals_ = app.state.templates.env.globals
    plain, globals_["needs_you_menu"] = globals_["needs_you_menu"], lambda seen=(): NeedsYouMenu(
        (chatty,), 1, 1, (row.card.work_id,)
    )
    item = client.get("/", params={"chats": "0"}).text.split('class="needs-you-item"', 1)[1]
    assert 'href="/chats/abc?work=w"' in item.split("</a>", 1)[0]
    globals_["needs_you_menu"] = plain
    assert f'datetime="{row.stopped_at.isoformat()}"' in top_bar
    assert "All Needs you (1)" in top_bar
    assert 'id="works-stopped"' in page and 'data-front-group="stopped"' in page
    stopped_row = page.split('data-front-section="stopped"', 1)[1].split("</article>", 1)[0]
    # The unseen mark carries the stop, which orders the front page's project tabs (#3226).
    assert f'data-work-unseen="{row.stopped_at.isoformat()}"' in stopped_row
    assert "data-front-landed>Needs retry<" in stopped_row
    assert 'class="fr-dot"' in stopped_row and "<button" not in stopped_row
    # Its age is the Work's last activity, as on the Work page, not the stop (#2397).
    assert row.card.last_activity and row.stopped_at != row.card.last_activity
    assert f'datetime="{row.card.last_activity.isoformat()}"' in stopped_row
    assert f'datetime="{row.stopped_at.isoformat()}"' not in stopped_row
    # The stop itself is the row's identity for the stop chime (#2742).
    assert f'data-work-stopped="{row.stopped_at.isoformat()}"' in stopped_row
    # The category row carries the same mark, and its header counts it even when folded.
    assert 'data-front-new="1">1 new<' in page
    assert page.count('data-front-toggle="') >= 2
    assert 'data-front-summary="new"' in page

    opened = client.get(work_href(WORK.work_id))
    # Its own page leaves it out of the top bar's list before the rebuild has run (#3289).
    assert 'class="needs-you-menu" data-new="0"' in opened.text
    await app.state.work_overview.refresh()
    await asyncio.gather(*service._push_tasks)
    # Opened, its notification is cleared on every device, once (#3365).
    assert phone[1:] == [("clear", [f"work:{WORK.work_id}"])]
    assert app.state.control_db.load_needs_you_showing() == {}

    front = app.state.work_overview.overview.front
    (row,) = front.needs_you
    assert not row.unseen and front.new == 0
    assert front.stopped == (row,)  # still where it landed, just no longer new
    page = client.get("/", params={"chats": "0"}).text
    assert "data-work-unseen" not in page
    assert 'class="needs-you-menu" data-new="0"' in page and "Nothing new needs you." in page
    # Seen, its category row still carries the stop, which orders the project tabs (#3226).
    needs_row = page.split('data-front-section="needs_you"', 1)[1].split("</article>", 1)[0]
    assert f'data-work-stopped="{row.stopped_at.isoformat()}"' in needs_row


@pytest.mark.asyncio
async def test_a_clear_waits_for_its_send_and_goes_only_where_one_arrived(tmp_path, monkeypatch):
    """#3365: a read during a slow send is cleared after it, and a send no device took is
    never cleared, since a push that closes nothing can make a phone show its own notice."""
    import threading

    from test_work_ui import WORK

    from agentdeck.web.work_view import note_work_opened

    app = _deck(tmp_path, monkeypatch)
    phone, gate, took = [], threading.Event(), [1]

    def send_to_all(*message):
        gate.wait(5)
        phone.append(message[3])
        return took[0]

    app.state.push = SimpleNamespace(
        enabled=True, send_to_all=send_to_all,
        clear_all=lambda tags: phone.append(("clear", tags)) or 1,
    )
    service = app.state.work_overview
    await service.current()
    tag = f"work:{WORK.work_id}"
    for delivered in (1, 0):
        took[0] = delivered
        gate.clear()
        _pretend_it_left(app, WORK.work_id)
        await service.refresh()
        note_work_opened(app, WORK.work_id)
        await service.refresh()  # read while its notification is still being sent
        gate.set()
        await asyncio.gather(*service._push_tasks)
    assert phone == [tag, ("clear", [tag]), tag]
    assert app.state.control_db.load_needs_you_showing() == {}


@pytest.mark.asyncio
async def test_opening_the_work_republishes_the_front_page(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    service = app.state.work_overview
    await service.current()
    _pretend_it_left(app, WORK.work_id)
    await service.refresh()
    assert service.overview.front.new == 1

    from agentdeck.web.work_view import NEEDS_YOU_TOPIC

    # #3328: the first rebuild reads the marks just before this Work is marked read, as one
    # already running when another device marks it would. The second ask must not be lost.
    control = app.state.control_db
    read_marks = control.works_opened
    builds = []
    loop = asyncio.get_running_loop()

    def marks_then_open():  # the build reads them off the loop, in a thread
        builds.append(1)
        marks = read_marks()
        if len(builds) == 1:
            control.note_work_opened([WORK.work_id], datetime.now(UTC))
            loop.call_soon_threadsafe(service.refresh_soon)  # a request, on the loop
        return marks

    monkeypatch.setattr(control, "works_opened", marks_then_open)
    bus = app.state.app_state.bus
    before = bus.revision(NEEDS_YOU_TOPIC)
    service.refresh_soon()
    service.refresh_soon()  # not started yet: the same pass reads it, no extra one
    await service._soon

    assert service.overview.front.new == 0
    assert bus.revision(NEEDS_YOU_TOPIC) != before  # every device's list is pushed again
    assert len(builds) == 2

    # With no mark in between, a pair of asks still costs one build, not one each.
    from agentdeck.web import work_view

    loads = []
    load = work_view.load_work_overview

    async def counted(*args, **kwargs):
        loads.append(1)
        return await load(*args, **kwargs)

    monkeypatch.setattr(work_view, "load_work_overview", counted)
    service.refresh_soon()
    service.refresh_soon()
    await service._soon
    assert len(loads) == 1


@pytest.mark.asyncio
async def test_marking_read_from_the_top_bar_list_clears_it_without_opening(tmp_path, monkeypatch):
    """#3246: a row, or the whole list, is marked read in place, as opening the Work would."""
    from fastapi.testclient import TestClient
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    service = app.state.work_overview
    await service.current()
    _pretend_it_left(app, WORK.work_id)
    await service.refresh()
    client = TestClient(app)
    top_bar = client.get("/", params={"chats": "0"}).text.split('id="needs-you-slot"', 1)[1]
    assert f'name="work_id" value="{WORK.work_id}"' in top_bar and "Mark all read" in top_bar

    refused = client.post(
        "/works/seen", data={"work_id": WORK.work_id}, headers={"origin": "https://elsewhere"}
    )
    assert refused.status_code == 403 and service.overview.front.new == 1

    # Not a Work id, or already read: nothing changes and the list comes back as it was.
    reply = client.post("/works/seen", data={"work_id": "nonsense"})
    assert reply.status_code == 200 and 'data-new="1"' in reply.text

    reply = client.post("/works/seen", data={"work_id": [WORK.work_id, WORK.work_id]})
    # The reply already leaves it out, before the rebuild it started has run.
    assert 'data-new="0"' in reply.text and "Nothing new needs you." in reply.text
    assert "Mark all read" not in reply.text
    await service.refresh()
    assert service.overview.front.new == 0
    assert WORK.work_id in app.state.control_db.works_opened()


def test_the_top_bar_list_is_the_newest_stops_first_and_says_how_many_more():
    """#3239: the new Needs you rows, the most recent stop first, at most eight shown."""
    from types import SimpleNamespace

    from agentdeck.web.work_view import NEEDS_YOU_MENU_LIMIT, needs_you_menu

    def row(n, unseen=True, stopped=True):
        card = SimpleNamespace(work_id=f"w{n}", title=f"Work {n}")
        when = NOW - timedelta(minutes=n) if stopped else None
        return SimpleNamespace(
            card=card, unseen=unseen, stopped_at=when, attention=None, own=None, children=()
        )

    rows = [row(n) for n in range(10)] + [row(10, unseen=False), row(11, stopped=False)]
    # #3368: a Work whose issue closed and only waits for a Finish is not news.
    closed = row(12)
    closed.attention = SimpleNamespace(category="issue_closed")
    front = FrontSections(needs_you=tuple(reversed([*rows, closed])))
    menu = needs_you_menu(SimpleNamespace(front=front))

    assert [r.card.work_id for r in menu.rows] == [f"w{n}" for n in range(NEEDS_YOU_MENU_LIMIT)]
    assert menu.new == 11 and menu.more == 3 and menu.total == 13 and "w12" not in menu.ids
    assert len(menu.ids) == 11  # "Mark all read" covers the ones past the limit too (#3246)
    # Works just marked read are left out before the overview knows (#3246).
    skipped = needs_you_menu(SimpleNamespace(front=front), seen=["w0", "w10"])
    assert skipped.new == 10 and skipped.rows[0].card.work_id == "w1" and "w0" not in skipped.ids
    assert needs_you_menu(None).new == 0 and needs_you_menu(None).rows == ()
    # A push is due only when what it shows moved, not on every overview change.
    again = needs_you_menu(SimpleNamespace(front=FrontSections(needs_you=(*rows, closed))))
    assert again.signature == menu.signature


def test_the_phone_is_pushed_each_new_stop_in_the_list_once():
    """#3348: a push per Work new in the top bar's list, with the row's words, into its chat."""
    from types import SimpleNamespace

    from agentdeck.web.work_view import (
        NEEDS_YOU_PUSH_LIMIT,
        NeedsYouPush,
        needs_you_clears,
        needs_you_pushes,
    )

    def row(n, minutes=0, chat=True):
        card = SimpleNamespace(
            work_id=f"w{n}", title=f"Work {n}", href=f"/works/w{n}",
            chat_href=f"/chats/c{n}" if chat else "", chat_key=f"k{n}" if chat else "",
        )
        answer = SimpleNamespace(label="Needs your answer", category="needs_answer")
        attention = answer if n else None
        return SimpleNamespace(
            card=card, unseen=True, stopped_at=NOW - timedelta(minutes=n + minutes),
            attention=attention, source=f"agentdeck#{n}", own=None, children=(),
        )

    def overview(*rows, degraded=()):
        return SimpleNamespace(front=FrontSections(needs_you=rows), degraded=degraded)

    first = overview(row(0, chat=False), row(1))
    # The first pass ever records the list without buzzing about what already waited.
    sends, sent = needs_you_pushes(first, None)
    assert sends == [] and {work for work, _ in sent} == {"w0", "w1"}
    assert needs_you_pushes(first, sent) == ([], sent)

    # One that stops anew is pushed once, as its row reads, opening its chat.
    second = overview(row(0, chat=False), row(1, minutes=-5))
    sends, sent = needs_you_pushes(second, sent)
    assert sends == [NeedsYouPush(
        "Work 1", "Needs your answer · agentdeck#1", "/chats/c1", "work:w1", ("w1",)
    )]
    assert needs_you_pushes(second, sent)[0] == []
    # #3365: what was sent is showing until its Work leaves the list.
    assert needs_you_clears(second, {}, sends) == ([], {"work:w1": {"w1"}})
    showing = {"work:w1": {"w1"}}
    # Read, it leaves the list and is forgotten; a Work with no chat opens its page.
    third = overview(row(0, minutes=-5, chat=False))
    sends, sent = needs_you_pushes(third, sent)
    assert sends == [
        NeedsYouPush("Work 0", "Needs you · agentdeck#0", "/works/w0", "work:w0", ("w0",))
    ]
    assert {work for work, _ in sent} == {"w0"}
    # Its notification is cleared once on every device; w0, never sent before, was not shown.
    assert needs_you_clears(third, showing, sends) == (["work:w1"], {"work:w0": {"w0"}})
    # A degraded pass may have dropped a Work, so it clears nothing.
    assert needs_you_clears(overview(degraded=("x",)), showing) == ([], showing)

    # A muted chat is remembered unsent; past the limit the rest are one summary.
    closed = row(7)
    closed.attention = SimpleNamespace(label="Issue closed — finish it?", category="issue_closed")
    many = overview(*(row(n) for n in range(1, 7)), closed)
    sends, kept = needs_you_pushes(many, sent, muted=lambda key: key == "k1")
    assert [s.title for s in sends[:NEEDS_YOU_PUSH_LIMIT]] == ["Work 2", "Work 3", "Work 4"]
    assert sends[-1] == NeedsYouPush("2 more Works need you", "", "/", "needs-you", ("w5", "w6"))
    assert len(kept) == 6
    # The summary stays while any Work it names is still new, then is cleared.
    _, showing = needs_you_clears(many, {}, sends)
    assert needs_you_clears(overview(row(6)), showing)[0] == ["work:w2", "work:w3", "work:w4"]
    assert needs_you_clears(overview(row(2)), showing)[0] == ["needs-you", "work:w3", "work:w4"]
    # A degraded pass that dropped Works forgets none of them.
    assert needs_you_pushes(overview(row(2), degraded=("x",)), kept) == ([], kept)



# --- in the browser ------------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_folded_group_stays_folded_through_a_push_and_a_reload(tmp_path, monkeypatch):
    from conftest import async_playwright
    from playwright.async_api import expect
    from test_web import _live_server
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + "/")
        await page.wait_for_function("() => window.__sseOpen === true")
        head = page.locator('#works-stuck [data-front-group="needs_retry"]')
        needs_row = page.locator(f'#works-stuck [data-work-id="{WORK.work_id}"]')
        stopped_row = page.locator(f'#works-stopped [data-work-id="{WORK.work_id}"]')
        await needs_row.wait_for()
        toggle = head.locator("[data-front-toggle]")
        assert await toggle.get_attribute("aria-expanded") == "true"
        # Measured on the heading the page holds now, until it settles: a live push that
        # lands while the page loads swaps the heading, and one measurement of the element it
        # replaced reads 0 (#2365).
        await page.wait_for_function(
            """() => {
                const head = document.querySelector(
                    '#works-stuck [data-front-group="needs_retry"]');
                return !!head && head.isConnected && head.getBoundingClientRect().height >= 36;
            }"""
        )
        # Group headers scroll with the list: stacked sticky rows floated over an open chat (#2372).
        # Read from the heading the page holds now, for the same reason as above.
        assert (
            await page.evaluate(
                """() => getComputedStyle(document.querySelector(
                    '#works-stuck [data-front-group="needs_retry"]')).position"""
            )
            == "static"
        )

        await head.locator(".fr-group-label").click()

        await needs_row.wait_for(state="hidden")
        # Retried until they hold rather than read once: a push landing now swaps the list.
        await expect(head).to_be_visible()
        await expect(stopped_row).to_be_visible()
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        assert await page.evaluate(
            "() => JSON.parse(localStorage.getItem('agentdeck-front-collapsed'))"
        ) == {"needs_retry": True}

        # A push that brings the row in again at a new revision keeps it folded.
        _pretend_it_left(app, WORK.work_id)
        await app.state.work_overview.refresh()
        await page.wait_for_selector(
            f'#works-stuck [data-work-id="{WORK.work_id}"][data-work-unseen]', state="attached"
        )
        await expect(needs_row).to_be_hidden()
        await expect(page.locator('#works-stuck [data-front-new="1"]')).to_be_visible()

        await page.reload()
        # The fold is applied by the deferred list script after the heading is parsed, so wait
        # for the toggle to say folded before asking whether the row is hidden.
        await expect(
            page.locator('#works-stuck [data-front-group="needs_retry"] [data-front-toggle]')
        ).to_have_attribute("aria-expanded", "false")
        await expect(needs_row).to_be_hidden()
        await page.locator(
            '#works-stuck [data-front-group="needs_retry"] [data-front-toggle]'
        ).click()
        await needs_row.wait_for(state="visible")
        assert await page.evaluate(
            "() => JSON.parse(localStorage.getItem('agentdeck-front-collapsed'))"
        ) == {}
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_top_bar_list_stays_open_through_a_push_and_opening_a_work_clears_it(
    tmp_path, monkeypatch
):
    """#3239 in the only place it is real: an open list survives the push that adds a Work,
    and opening that Work from the list takes it off on the Work page's own top bar. A Work
    that stops again while its page is on screen is marked read by that page (#3289)."""
    import os
    from pathlib import Path

    from conftest import async_playwright
    from playwright.async_api import expect
    from test_web import _live_server
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()  # the first pass: what waits already is seen
    shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + "/settings")
        await page.wait_for_function("() => window.__sseOpen === true")
        menu = page.locator("#needs-you-slot .needs-you-menu")
        await expect(menu).to_have_attribute("data-new", "0")
        await page.locator("#needs-you-slot summary").click()
        await expect(page.locator(".needs-you-empty")).to_be_visible()

        _pretend_it_left(app, WORK.work_id)
        await app.state.work_overview.refresh()

        await expect(menu).to_have_attribute("data-new", "1")
        assert await menu.evaluate("el => el.open")  # the push did not shut it
        item = page.locator(f'.needs-you-item[data-work-id="{WORK.work_id}"] a')
        await expect(item).to_be_visible()
        if shots:
            await page.screenshot(path=str(Path(shots) / "issue-3239-list-open.png"))
        await page.keyboard.press("Escape")
        assert not await menu.evaluate("el => el.open")
        if shots:
            await page.locator(".topbar").screenshot(
                path=str(Path(shots) / "issue-3239-icon-lit.png")
            )

        await page.locator("#needs-you-slot summary").click()
        await item.click()
        await page.wait_for_url(f"**{work_href(WORK.work_id)}")
        await expect(page.locator("#needs-you-slot .needs-you-menu")).to_have_attribute(
            "data-new", "0", timeout=10_000
        )
        await page.wait_for_function("() => window.__sseOpen === true")
        opened = app.state.control_db.works_opened()[WORK.work_id]

        _pretend_it_left(app, WORK.work_id)
        async with page.expect_response(
            lambda r: r.url.endswith("/works/seen"), timeout=10_000
        ) as seen:
            await app.state.work_overview.refresh()
        assert (await seen.value).ok
        assert app.state.control_db.works_opened()[WORK.work_id] > opened
        await expect(page.locator("#needs-you-slot .needs-you-menu")).to_have_attribute(
            "data-new", "0", timeout=10_000
        )
        if shots:
            await page.locator("#needs-you-slot summary").click()
            await page.screenshot(path=str(Path(shots) / "issue-3289-open-work-not-new.png"))
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_marking_a_row_read_keeps_the_list_open_and_empties_it(tmp_path, monkeypatch):
    """#3246 on a real page: the check mark clears the row in place, the list stays open.
    It and an open Work page close this device's notification for the Work (#3365)."""
    import os
    from pathlib import Path

    from conftest import async_playwright
    from playwright.async_api import expect
    from test_web import _live_server
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    _pretend_it_left(app, WORK.work_id)
    await app.state.work_overview.refresh()
    shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block")
        await page.add_init_script("""
            window.__closed = [];
            navigator.serviceWorker.getRegistration = async () => ({
                getNotifications: async ({ tag }) =>
                    [{ close: () => window.__closed.push(tag) }],
            });
        """)
        await page.goto(base + "/settings")
        menu = page.locator("#needs-you-slot .needs-you-menu")
        await expect(menu).to_have_attribute("data-new", "1")
        await page.locator("#needs-you-slot summary").click()
        button = page.locator(f'.needs-you-item[data-work-id="{WORK.work_id}"] .needs-you-read')
        await expect(button).to_be_visible()
        await expect(page.locator(".needs-you-read-all")).to_be_visible()
        if shots:
            await page.screenshot(path=str(Path(shots) / "issue-3246-mark-read.png"))
        await button.click()

        await expect(menu).to_have_attribute("data-new", "0")
        assert await menu.evaluate("el => el.open")
        await expect(page.locator(".needs-you-empty")).to_be_visible()
        assert page.url == base + "/settings"  # read without opening it
        if shots:
            await page.screenshot(path=str(Path(shots) / "issue-3246-after.png"))
        tag = f"work:{WORK.work_id}"
        assert await page.evaluate("() => window.__closed") == [tag]
        await page.goto(base + work_href(WORK.work_id))
        await page.wait_for_function(f"() => window.__closed.includes({tag!r})")
        await browser.close()
    assert WORK.work_id in app.state.control_db.works_opened()


@pytest.mark.asyncio
async def test_only_opening_a_new_work_rebuilds_the_front_page_at_once(tmp_path, monkeypatch):
    """#2422: the rebuild an open starts shares the event loop the Work page is served from,
    so a Work the front page does not show as new is opened without one."""
    from fastapi.testclient import TestClient
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    service = app.state.work_overview
    await service.current()
    _pretend_it_left(app, WORK.work_id)
    await service.refresh()
    assert service.overview.front.new == 1
    rebuilds = []
    monkeypatch.setattr(service, "refresh_soon", lambda: rebuilds.append(1))
    client = TestClient(app)

    assert client.get(work_href(WORK.work_id)).status_code == 200
    assert rebuilds == [1]
    await service.refresh()
    assert service.overview.front.new == 0

    assert client.get(work_href(WORK.work_id)).status_code == 200
    assert rebuilds == [1]


async def test_the_overview_remembers_when_an_unwatched_change_arrived(monkeypatch):
    """No dashboard is connected, so a change only marks the overview stale (#2865). The
    pass that catches up later is told when that first change came, to date stops by it."""
    import asyncio
    from types import SimpleNamespace

    from agentdeck.events import EventBus
    from agentdeck.web import work_view

    bus = EventBus()
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(bus=bus)))
    seen: list[datetime | None] = []

    async def fake_load(_app, *, now, changed_since=None):
        seen.append(changed_since)
        return work_view.WorkOverview()

    async def scanned(_app):
        return True

    monkeypatch.setattr(work_view, "load_work_overview", fake_load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    service = work_view.WorkOverviewService(app)
    service.start()
    try:
        await asyncio.sleep(0)
        before = datetime.now(UTC)
        bus.publish("sessions")
        bus.publish("sessions")
        await asyncio.sleep(0.05)
        assert seen == []  # nobody watching: nothing rebuilt
        await service.refresh()
        assert seen[0] is not None and before <= seen[0] <= datetime.now(UTC)
        await service.refresh()
        assert seen[1] is None  # consumed by the pass that caught up
    finally:
        await service.stop()


async def test_a_failed_rebuild_keeps_the_change_owed_and_is_retried(monkeypatch):
    """A rebuild that raises must not erase that a change is owed (#2865): no clock rebuilds
    the overview any more, so a dashboard left open retries it with a back-off, and the
    pass that succeeds is still told when the first unbuilt change came."""
    import asyncio
    from types import SimpleNamespace

    from agentdeck.events import EventBus
    from agentdeck.web import work_view

    bus = EventBus()
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(bus=bus)))
    seen: list[datetime | None] = []

    async def flaky_load(_app, *, now, changed_since=None):
        seen.append(changed_since)
        if len(seen) == 1:
            raise RuntimeError("cache unreadable")
        return work_view.WorkOverview()

    async def scanned(_app):
        return True

    monkeypatch.setattr(work_view, "load_work_overview", flaky_load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    monkeypatch.setattr(work_view, "time_deadline", lambda *args: None)
    monkeypatch.setattr(work_view.WorkOverviewService, "RETRY_MIN_S", 0.05)
    service = work_view.WorkOverviewService(app)
    stamp = datetime.now(UTC)
    service._stale_since = stamp
    with bus.subscribe(work_view.WORKS_TOPIC) as dashboard:
        service.refresh_soon()
        await asyncio.wait_for(dashboard.get(), 5.0)
    assert seen == [stamp, stamp]
    assert not service._stale and service._stale_since is None


@pytest.mark.parametrize("topic", ["works", "needs_you"])
async def test_a_dashboard_connecting_arms_the_deadline_an_unwatched_pass_skipped(
    monkeypatch, topic
):
    """#2865: with nobody watching the refresher sleeps with no deadline. A page that then
    connects before a card's clock-derived state falls due must wake it, so that state is
    rebuilt at its deadline rather than at the next unrelated change."""
    import asyncio
    from types import SimpleNamespace

    from agentdeck.events import EventBus
    from agentdeck.web import work_view

    bus = EventBus()
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(bus=bus)))
    builds: list[datetime] = []

    async def fake_load(_app, *, now, changed_since=None):
        builds.append(now)
        return work_view.WorkOverview(other=(len(builds),))

    async def scanned(_app):
        return True

    monkeypatch.setattr(work_view, "load_work_overview", fake_load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    monkeypatch.setattr(work_view, "time_deadline", lambda *args: None)
    monkeypatch.setattr(work_view.WorkOverviewService, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(work_view.WorkOverviewService, "MIN_INTERVAL_S", 0.0)
    service = work_view.WorkOverviewService(app)
    await service.refresh()  # built while nobody watched
    service._deadline = datetime.now(UTC) + timedelta(seconds=0.3)
    service.start()
    try:
        await asyncio.sleep(0.05)  # the refresher sleeps: unwatched, so no deadline
        # A page showing only the top bar's Needs you list watches too (#3239).
        with bus.subscribe(topic):
            service.connected()  # not due yet: nothing to rebuild, only a deadline to arm
            for _ in range(300):
                if len(builds) == 2:
                    break
                await asyncio.sleep(0.01)
        assert len(builds) == 2
    finally:
        await service.stop()


async def test_a_phone_listening_rebuilds_on_the_cards_clock_at_its_own_slower_rate(monkeypatch):
    """#3348: with no page open, a push subscription still has a timed stop rebuilt, so the
    phone hears of it, but no sooner than ``PHONE_CLOCK_MIN_S`` after the last build. A phone
    that subscribes while the refresher sleeps unwatched wakes it."""
    from agentdeck.events import EventBus
    from agentdeck.web import work_view

    bus = EventBus()
    phones: list[str] = []
    push = SimpleNamespace(enabled=False, has_subscriptions=lambda: bool(phones))
    app = SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(bus=bus), push=push))
    builds: list[float] = []

    async def fake_load(_app, *, now, changed_since=None):
        builds.append(asyncio.get_running_loop().time())
        return work_view.WorkOverview(other=(len(builds),))

    async def scanned(_app):
        return True

    monkeypatch.setattr(work_view, "load_work_overview", fake_load)
    monkeypatch.setattr(work_view, "sessions_scanned", scanned)
    monkeypatch.setattr(work_view, "time_deadline", lambda *args: None)
    monkeypatch.setattr(work_view.WorkOverviewService, "DEBOUNCE_S", 0.0)
    monkeypatch.setattr(work_view.WorkOverviewService, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(work_view.WorkOverviewService, "PHONE_CLOCK_MIN_S", 0.05)
    service = work_view.WorkOverviewService(app)
    await service.refresh()
    service._deadline = datetime.now(UTC) + timedelta(seconds=0.01)
    service.start()
    try:
        await asyncio.sleep(0.08)
        assert len(builds) == 1  # nobody listening: the clock is not armed
        phones.append("phone")
        service.phone_changed()  # what /push/subscribe calls
        for _ in range(50):
            if len(builds) == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await service.stop()
    assert len(builds) == 2 and builds[1] - builds[0] >= 0.05
