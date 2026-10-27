"""Every change of a Work's front-page status is logged, with the facts behind it (#2751).

The overview pass reclassifies every Work every few seconds; the log keeps one row each time
a Work's section, Needs you category, status kind or tone changes, so a wrong or flapping
"Needs you" can be traced afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentdeck.work_status_log import GONE, HIDDEN, StatusPoint, changes, flaps, point

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
A = "github_issue:eerovil/agentdeck#1"
B = "github_issue:eerovil/agentdeck#2"


def _row(
    work_id=A,
    *,
    section="needs_you",
    category="needs_answer",
    kind="chat_question",
    tone="attention",
    headline="Needs answer",
    **card,
):
    attention = (
        SimpleNamespace(category=category, label=headline) if section == "needs_you" else None
    )
    chat = SimpleNamespace(
        session_key="claude_code:main:s1",
        state="waiting",
        asks=True,
        question="Which one?",
        interaction="",
        recovering=False,
    )
    fields = {
        "work_id": work_id,
        "attention": (chat,),
        "working_count": 0,
        "run_state": "ended",
        **card,
    }
    return SimpleNamespace(
        card=SimpleNamespace(**fields),
        section=section,
        attention=attention,
        status=SimpleNamespace(kind=kind, tone=tone, headline=headline, you="Answer it"),
    )


def _active(work_id=A):
    return _row(
        work_id,
        section="active",
        category="",
        kind="",
        tone="active",
        headline="Working",
        working_count=1,
    )


# --- the rule ------------------------------------------------------------------------------------


def test_a_first_sighting_and_each_change_is_a_row_and_nothing_else_is():
    first = changes({}, [_row()], now=NOW, pid=7, revision="abc")
    (row,) = first
    assert (row.section, row.category, row.kind, row.tone) == (
        "needs_you",
        "needs_answer",
        "chat_question",
        "attention",
    )
    assert row.prev_section == "" and row.pid == 7 and row.revision == "abc"
    assert row.why["chats"][0]["session_key"] == "claude_code:main:s1"
    assert row.why["chats"][0]["asks"] is True and "working_count" not in row.why

    last = {A: row}
    # Same status again, even with another headline: nothing to log.
    assert changes(last, [_row(headline="Needs answer now")], now=NOW) == []
    # A section, a category, a kind or a tone change is a row each.
    for moved in (
        _active(),
        _row(category="needs_review"),
        _row(kind="chat_stopped"),
        _row(tone="problem"),
    ):
        (change,) = changes(last, [moved], now=NOW + timedelta(seconds=10))
        assert change.prev_section == "needs_you" and change.prev_category == "needs_answer"
        assert change.key != row.key


def test_a_work_the_pass_no_longer_builds_is_logged_gone_once():
    (row,) = changes({}, [_row()], now=NOW)
    (gone,) = changes({A: row}, [], now=NOW + timedelta(minutes=1), degraded=("cached_pulls",))
    assert gone.section == GONE and gone.prev_section == "needs_you"
    assert gone.degraded == ("cached_pulls",)
    assert changes({A: gone}, [], now=NOW + timedelta(minutes=2)) == []
    # Coming back is a row again.
    (back,) = changes({A: gone}, [_row()], now=NOW + timedelta(minutes=3))
    assert back.prev_section == GONE and back.section == "needs_you"


def test_a_hidden_needs_you_work_is_logged_as_hidden():
    assert point(_row(), at=NOW, hidden=True).section == HIDDEN
    assert point(_active(), at=NOW, hidden=True).section == "active"
    (row,) = changes({}, [_row()], now=NOW, hidden=frozenset({A}))
    assert row.section == HIDDEN


# --- flaps ---------------------------------------------------------------------------------------


def _at(seconds, section, category="", work_id=A, kind=""):
    return StatusPoint(work_id, NOW + timedelta(seconds=seconds), section, category, kind)


def test_a_short_trip_into_needs_you_and_back_is_a_flap_and_a_long_one_is_not():
    rows = [
        _at(0, "active"),
        _at(10, "needs_you", "needs_answer"),
        _at(40, "active"),
        _at(1000, "needs_you", "needs_answer"),
        _at(2000, "active"),
    ]
    (flap,) = flaps(rows, within=timedelta(minutes=5))
    assert flap["to"] == {"section": "needs_you", "category": "needs_answer"}
    assert flap["from"] == {"section": "active", "category": ""}
    assert flap["seconds"] == 30.0


def test_out_and_back_and_a_category_bounce_are_flaps_and_kind_changes_do_not_split():
    rows = [
        _at(0, "needs_you", "needs_answer"),
        _at(5, "active"),
        _at(8, "active", kind="waiting_job"),  # same span
        _at(20, "needs_you", "needs_answer"),
        _at(25, "needs_you", "needs_retry"),
        _at(30, "needs_you", "needs_answer"),
        _at(0, "active", work_id=B),
        _at(10, "needs_you", "needs_review", work_id=B),  # never came back: no flap
    ]
    found = flaps(rows, within=timedelta(seconds=60))
    assert [(f["work_id"], f["kind"], f["to"]["section"], f["to"]["category"]) for f in found] == [
        (A, "into_needs_you", "needs_you", "needs_answer"),
        (A, "out_of_needs_you", "active", ""),
        (A, "bounce", "needs_you", "needs_retry"),
    ]
    assert [f["seconds"] for f in found] == [5.0, 15.0, 5.0]
    assert found[1]["from"] == {"section": "needs_you", "category": "needs_answer"}


def test_a_needs_you_visit_is_one_trip_whatever_its_category_does_inside():
    rows = [
        _at(0, "active"),
        _at(5, "needs_you", "needs_answer"),
        _at(10, "needs_you", "needs_retry"),
        _at(15, "active"),
    ]
    (flap,) = flaps(rows, within=timedelta(seconds=60))
    assert flap["kind"] == "into_needs_you" and flap["seconds"] == 10.0
    assert flap["from"] == {"section": "active", "category": ""}
    assert flap["then"] == {"section": "active", "category": ""}


def test_a_needs_you_visit_that_leaves_somewhere_else_is_still_a_trip():
    rows = [
        _at(0, "active"),
        _at(5, "needs_you", "needs_answer"),
        _at(20, "finished"),
    ]
    (flap,) = flaps(rows, within=timedelta(seconds=60))
    assert flap["kind"] == "into_needs_you" and flap["seconds"] == 15.0
    assert flap["then"] == {"section": "finished", "category": ""}
    # Gone from the overview counts as leaving too.
    gone = [_at(0, "needs_you", "needs_answer"), _at(30, GONE)]
    (flap,) = flaps(gone, within=timedelta(seconds=60))
    assert flap["then"]["section"] == GONE
    # A trip between two other sections and back is a bounce.
    (flap,) = flaps(
        [_at(0, "active"), _at(5, "finished"), _at(9, "active")], within=timedelta(seconds=60)
    )
    assert flap["kind"] == "bounce" and flap["to"]["section"] == "finished"


# --- the store -----------------------------------------------------------------------------------


def test_rows_are_durable_newest_per_work_is_readable_and_old_ones_are_pruned(tmp_path):
    from agentdeck.db import Db

    path = tmp_path / "control.sqlite3"
    db = Db(path)
    now = datetime.now(UTC)
    old = StatusPoint(A, now - timedelta(days=31), "active")
    first = StatusPoint(
        A,
        now - timedelta(minutes=2),
        "needs_you",
        "needs_answer",
        why={"working_count": 0, "chats": [{"state": "waiting"}]},
        degraded=("runtime/jobs",),
        pid=3,
        revision="r1",
    )
    second = StatusPoint(A, now - timedelta(minutes=1), "active", prev_section="needs_you")
    other = StatusPoint(B, now, "finished")
    db.append_work_status_log([old, first, second, other])

    reopened = Db(path)  # opening drops rows past the 30 days
    assert reopened.work_status_log(work_id=A) == [first, second]
    assert reopened.work_status_log(since=now - timedelta(seconds=90)) == [second, other]
    assert reopened.work_status_log(limit=1) == [other]
    assert reopened.latest_work_status_points() == {A: second, B: other}


@pytest.mark.asyncio
async def test_no_overview_is_built_before_every_accounts_first_scan(monkeypatch):
    """#2760: each web restart built an overview while the accounts were still being scanned,
    dropping every finished Work only an unscanned chat names (1,225 logged gone). Every build
    goes through ``refresh``, so the background loop, a Work opened and an action all wait."""
    from agentdeck.web import work_view
    from agentdeck.web.work_view import WorkOverview, WorkOverviewService

    scanned = False

    async def wait_initial_scans(timeout: float) -> bool:
        return scanned

    built: list[bool] = []

    async def load(app, **kwargs):
        built.append(scanned)
        return WorkOverview(archived=len(built) - 1)

    bus = SimpleNamespace(publish=lambda topic: None)
    state = SimpleNamespace(
        collector=SimpleNamespace(wait_initial_scans=wait_initial_scans),
        app_state=SimpleNamespace(bus=bus),
    )
    service = WorkOverviewService(SimpleNamespace(state=state))
    monkeypatch.setattr(work_view, "load_work_overview", load)

    assert await service.refresh() is False
    assert await service.current() == WorkOverview() and built == []
    scanned = True
    await service.current()
    assert built == [True]

    # #3108: a page load waits briefly for a stale overview's rebuild, rather than painting
    # the old cards and swapping in the new ones a moment later.
    service._stale = True
    assert (await service.current(wait=2.0)).archived == 1 and built == [True, True]


# --- through the deck ----------------------------------------------------------------------------


def _deck(tmp_path, monkeypatch):
    from test_work_hidden import _deck as hidden_deck

    app, _card, _record = hidden_deck(tmp_path, monkeypatch)
    return app


@pytest.mark.asyncio
async def test_the_overview_logs_a_change_once_and_a_restart_logs_nothing_new(
    tmp_path, monkeypatch
):
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    await app.state.work_overview.current()
    (row,) = app.state.control_db.work_status_log(work_id=WORK.work_id)
    assert row.section == "needs_you" and row.category
    assert row.pid is not None and row.why

    await app.state.work_overview.refresh()
    assert len(app.state.control_db.work_status_log(work_id=WORK.work_id)) == 1

    # A new web process starts from the log itself, so it does not log the Work again.
    app.state.work_status_points = None
    app.state.work_overview.overview = None
    await app.state.work_overview.refresh()
    assert len(app.state.control_db.work_status_log(work_id=WORK.work_id)) == 1


@pytest.mark.asyncio
async def test_a_read_that_failed_during_the_pass_is_named_on_the_row(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)

    def broken(*_args):
        raise OSError("store gone")

    monkeypatch.setattr(app.state.control_db, "paused_works", broken)
    await app.state.work_overview.current()
    (row,) = app.state.control_db.work_status_log(work_id=WORK.work_id)
    assert "deck_paused" in row.degraded


@pytest.mark.asyncio
async def test_an_unreadable_issue_cache_fails_the_pass_instead_of_reopening_works(
    tmp_path, monkeypatch
):
    """Without the issue records every closed or paused Work reads as open and stopped: the
    pass logs nothing and keeps the last overview rather than show that (#3553)."""
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    kept = await app.state.work_overview.current()
    before = app.state.control_db.work_status_log(work_id=WORK.work_id)

    def broken(refs):
        raise OSError("database is locked")

    app.state.issues.cached_issues = broken
    with pytest.raises(OSError):
        await app.state.work_overview.refresh()
    assert app.state.work_overview.overview is kept
    assert app.state.control_db.work_status_log(work_id=WORK.work_id) == before


def test_the_routes_read_the_log_and_its_flaps(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    work = WORK.work_id
    app.state.control_db.append_work_status_log(
        [
            StatusPoint(work, now - timedelta(seconds=60), "active"),
            StatusPoint(
                work,
                now - timedelta(seconds=50),
                "needs_you",
                "needs_answer",
                prev_section="active",
            ),
            StatusPoint(work, now - timedelta(seconds=40), "active", prev_section="needs_you"),
            StatusPoint(A, now - timedelta(seconds=30), "finished"),
        ]
    )
    client = TestClient(app)

    body = client.get(
        "/api/works/status-log", params={"work": f"{WORK.repository}#{WORK.number}"}
    ).json()
    assert body["work_id"] == work
    assert [row["section"] for row in body["rows"]] == ["active", "needs_you", "active"]
    every = client.get("/api/works/status-log").json()["rows"]
    assert len(every) >= 4
    since = (now - timedelta(seconds=35)).isoformat()
    assert [
        row["work_id"]
        for row in client.get("/api/works/status-log", params={"since": since}).json()["rows"]
    ] == [A]
    helsinki = (now - timedelta(seconds=35)).astimezone(timezone(timedelta(hours=3))).isoformat()
    assert [
        row["work_id"]
        for row in client.get("/api/works/status-log", params={"since": helsinki}).json()["rows"]
    ] == [A]
    assert client.get("/api/works/status-log", params={"since": "yesterday"}).status_code == 400
    assert client.get("/api/works/status-log", params={"work": "nonsense"}).status_code == 400

    flapped = client.get("/api/works/status-log/flaps", params={"work": work}).json()
    assert flapped["count"] == 1
    assert flapped["flaps"][0]["kind"] == "into_needs_you"
    assert flapped["flaps"][0]["to"]["section"] == "needs_you"
    assert flapped["flaps"][0]["seconds"] == 10.0
    assert (
        client.get("/api/works/status-log/flaps", params={"work": work, "within": 5}).json()[
            "count"
        ]
        == 0
    )
