"""A Work is finished by a person (#2510): its issue may close, the Work waits for Finish.

The issue closes as it always did — a merge, the Poller's ``resolved``, a person on GitHub —
but the deck keeps the Work in Needs you, under *Issue closed — finish it?*, until someone
presses Finish. Finish is this deck's own record: it never calls GitHub. Closes before the
deck began asking count as signed off, so nothing old comes back.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck import gh_cli
from agentdeck.work_attention import FINISH, ISSUE_CLOSED
from agentdeck.work_facts import FINISH_SLACK, awaiting_finish
from agentdeck.work_overview import work_href

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# --- the rule ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("closed", "signed", "cutoff", "waits"),
    [
        (None, None, T0, False),  # still open
        (T0 + timedelta(hours=1), None, None, False),  # no cutoff on record: as before
        (T0 - timedelta(hours=1), None, T0, False),  # closed before the deck asked
        (T0, None, T0, False),  # the cutoff itself is before
        (T0 + timedelta(hours=1), None, T0, True),
        (T0 + timedelta(hours=1), T0 + timedelta(hours=2), T0, False),
        # Close issue records the Finish on this host's clock, a moment off GitHub's.
        (T0 + timedelta(hours=1), T0 + timedelta(hours=1) - FINISH_SLACK, T0, False),
        # A Finish before a later close (reopened, then closed again) does not count for it.
        (T0 + timedelta(hours=3), T0 + timedelta(hours=2), T0, True),
    ],
)
def test_a_close_after_the_cutoff_waits_until_a_finish_after_it(closed, signed, cutoff, waits):
    assert awaiting_finish(closed, signed, cutoff) is waits


def test_sign_offs_are_durable_and_a_stale_close_never_moves_one_back(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    work_id = "github_issue:eerovil/agentdeck#2510"
    later = T0 + timedelta(hours=1)

    db.note_work_closed(work_id, later)
    db.note_work_closed(work_id, T0)  # an older close read by a stale pass
    assert Db(tmp_path / "control.sqlite3").work_signoffs() == {work_id: (later, None)}
    assert db.finish_work(work_id, later + timedelta(minutes=5))
    assert db.work_signoffs()[work_id] == (later, later + timedelta(minutes=5))
    db.note_work_closed(work_id, later + timedelta(hours=1))  # reopened and closed again
    assert db.work_signoffs()[work_id][0] == later + timedelta(hours=1)


# --- through the deck ----------------------------------------------------------------------------


class _NoGh:
    """Finish never calls GitHub."""

    calls = 0

    async def __call__(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("Finish must not call gh")


def _deck(tmp_path, monkeypatch, *, closed_at: datetime, cutoff: datetime, active=None):
    """A Work whose issue closed at ``closed_at``, with its
    worker's chat last active at ``active`` (ten minutes ago by default)."""
    from test_work_ui import WORK, _add, _app

    from agentdeck.web.work_view import SIGNOFF_CUTOFF

    app = _app(tmp_path)
    _add(app, "claude_code:main:worker-2510", WORK,
         last_activity=active or datetime.now(UTC) - timedelta(minutes=10))
    # The deck recorded its cutoff when it started; move it to where this case needs it.
    assert app.state.control_db._write(
        "UPDATE work_backfills SET summary = ? WHERE name = ?",
        (json.dumps({"cutoff": cutoff.isoformat()}), SIGNOFF_CUTOFF),
    )
    record = {
        "title": "Check everything is fine",
        "state": "closed",
        "closedAt": closed_at.isoformat(),
        "is_pr": False,
        "comments": 1,
    }
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): record}
    monkeypatch.setattr(gh_cli, "run_once", _NoGh())
    return app, record


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _visibility(client, action):
    from test_work_ui import WORK

    return client.post(work_href(WORK.work_id) + "/visibility", data={"action": action})


def _ids(rows):
    return [row.card.work_id for row in rows]


def _releases(monkeypatch) -> list[str]:
    """The Works whose worktrees a Finish gave back (#3274)."""
    from agentdeck.web import routes_works

    released: list[str] = []

    def release(app, work_id):
        released.append(work_id)
        return asyncio.sleep(0)

    monkeypatch.setattr(routes_works, "release_work_worktrees", release)
    return released


NOW = datetime.now(UTC)


@pytest.mark.asyncio
async def test_a_closed_work_waits_in_needs_you_until_a_person_finishes_it(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    client = _client(app)
    # #3675: a wayfinder ticket's answer is already on its map, so its close needs no Finish.
    record["labels"] = ["agent", "Wayfinder:Research"]
    ticket = await app.state.work_overview.current()
    assert _ids(ticket.front.needs_you) == [] and _ids(ticket.front.finished) == [WORK.work_id]
    assert app.state.control_db.work_signoffs() == {}
    record["labels"] = ["agent", "wayfinder:map"]  # the map itself still waits, as any Work
    await app.state.work_overview.refresh()
    before = app.state.work_overview.overview

    (row,) = before.front.needs_you
    assert row.card.work_id == WORK.work_id and _ids(before.front.finished) == []
    assert row.attention.category == ISSUE_CLOSED and FINISH in row.attention.actions
    front = client.get("/", params={"chats": "0"}).text
    assert "Issue closed — finish it?" in front and 'value="finish"' in front
    page = client.get(work_href(WORK.work_id)).text
    assert 'value="finish"' in page and "This Work is finished" not in page

    # #3187: with a pull request still open, the page reads it first and keeps Finish beside
    # the headline; Finish asks once, naming it, and a press without the answer finishes nothing.
    import re

    from agentdeck.web import routes_works
    from agentdeck.work_facts import OutputFact

    opened = OutputFact(
        "https://github.com/o/store/pull/3593", "o/Store", 3593, "registered", state="open"
    )
    real = routes_works.load_work_detail

    async def with_open_pr(*args, **kwargs):
        detail = await real(*args, **kwargs)
        return replace(detail, card=replace(detail.card, outputs=(opened,)))

    monkeypatch.setattr(routes_works, "load_work_detail", with_open_pr)
    page = client.get(work_href(WORK.work_id)).text
    form = re.search(r"data-work-finish>(<form.*?</form>)", page, re.S).group(1)
    assert 'hx-confirm="Store#3593 is still open. Finish anyway?"' in form
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', form))
    assert fields == {"action": "finish", "confirmed": "o/Store#3593"}
    asked = _visibility(client, "finish")
    assert asked.status_code == 409 and "Store#3593 is still open. Finish anyway?" in asked.text
    assert app.state.control_db.work_signoffs().get(WORK.work_id, (None, None))[1] is None

    response = client.post(work_href(WORK.work_id) + "/visibility", data=fields)

    assert response.status_code == 200 and 'data-wd-receipt="sent"' in response.text
    after = app.state.work_overview.overview  # the press rebuilt it
    assert _ids(after.front.needs_you) == [] and _ids(after.front.finished) == [WORK.work_id]
    assert app.state.control_db.work_signoffs()[WORK.work_id][1] is not None
    assert _NoGh.calls == 0

    # #3770: a closed build issue filed under the map is no ticket, yet finishes with the map.
    from test_work_ui import OTHER, _add

    _add(app, "claude_code:main:worker-3770", OTHER)
    child = {
        **record,
        "labels": ["agent"],
        "parent": f"https://api.github.com/repos/{WORK.repository}/issues/{WORK.number}",
    }
    app.state.issues.cached_issues = lambda refs: {
        (WORK.repository, WORK.number): record,
        (OTHER.repository, OTHER.number): child,
    }
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.needs_you) == []
    # A map the deck builds no Work for is read from the issue cache, on the child's page too.
    from test_work_ui import UNSTARTED

    child["parent"] = child["parent"].replace(str(WORK.number), str(UNSTARTED.number))
    old_map = {**record, "closedAt": (NOW - timedelta(days=2)).isoformat()}  # before the cutoff
    app.state.issues.cached_issues = lambda refs: {
        (WORK.repository, WORK.number): record,
        (OTHER.repository, OTHER.number): child,
        (UNSTARTED.repository, UNSTARTED.number): old_map,
    }
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.needs_you) == []
    assert 'value="finish"' not in client.get(work_href(OTHER.work_id)).text
    # A pull request of the child's own still waits, open or not read yet, under any map.
    from agentdeck.web.work_view import _finishes_by_itself
    from agentdeck.work_facts import WorkFacts

    url = "https://github.com/eerovil/agentdeck/pull/9"
    built = WorkFacts(ref=OTHER, record=child, outputs=(OutputFact(url, "", 9, "issue"),))
    maps = {UNSTARTED.work_id.casefold()}
    assert not _finishes_by_itself(built, maps)
    merged = replace(built, outputs=(OutputFact(url, "", 9, "issue", state="merged"),))
    assert _finishes_by_itself(merged, maps)
    old_map["labels"] = ["agent"]  # a parent that is no map finishes nothing
    assert 'value="finish"' in client.get(work_href(OTHER.work_id)).text
    child["parent"] = None  # under no map, the same closed issue waits for its own Finish
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.needs_you) == [OTHER.work_id]


@pytest.mark.asyncio
async def test_a_closed_work_left_unproven_is_finished_from_its_row(tmp_path, monkeypatch):
    """A not_live check with no wait left has only a person's Finish to move it on (#4126)."""
    from test_work_ui import WORK

    app, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    client = _client(app)
    app.state.work_router = SimpleNamespace(switched=lambda repository: True)  # deck-run
    note = "no new case came up since the deploy"
    app.state.control_db.set_work_live_check(WORK.work_id, "not_live", note, "s")
    await app.state.work_overview.refresh()

    (row,) = app.state.work_overview.overview.front.needs_you
    assert row.attention.category == "needs_answer" and FINISH in row.attention.actions
    front = client.get("/", params={"chats": "0"}).text
    assert f"not live yet: {note}" in front and 'value="finish"' in front
    assert "or finish the Work" in row.status.you

    assert _visibility(client, "finish").status_code == 200
    after = app.state.work_overview.overview
    assert _ids(after.front.needs_you) == [] and _ids(after.front.finished) == [WORK.work_id]


@pytest.mark.asyncio
async def test_a_work_closed_before_the_cutoff_is_finished_as_before(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(hours=2), cutoff=NOW - timedelta(hours=1)
    )
    overview = await app.state.work_overview.current()

    assert _ids(overview.front.needs_you) == [] and _ids(overview.front.finished) == [WORK.work_id]
    response = _visibility(_client(app), "finish")
    assert response.status_code == 409 and app.state.control_db.work_signoffs() == {}


@pytest.mark.asyncio
async def test_a_reopened_work_closed_again_waits_for_a_new_finish(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(hours=3), cutoff=NOW - timedelta(days=1)
    )
    await app.state.work_overview.current()
    assert _visibility(_client(app), "finish").status_code == 200

    record["closedAt"] = (NOW + timedelta(hours=1)).isoformat()  # a later close of the issue
    await app.state.work_overview.refresh()

    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]


@pytest.mark.asyncio
async def test_a_waiting_work_stays_however_long_it_is_quiet(tmp_path, monkeypatch):
    """Once seen waiting, a Work stays a candidate after its card is forgotten (#2510)."""
    from test_work_ui import WORK, _board

    app, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(days=30), cutoff=NOW - timedelta(days=60),
        active=NOW - timedelta(days=30),
    )
    await app.state.work_overview.current()
    assert WORK.work_id in app.state.control_db.work_signoffs()

    app.state.issues.read_board = lambda: _board()  # the engine released the card
    await app.state.work_overview.refresh()

    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]


@pytest.mark.asyncio
async def test_finish_clears_a_hide_so_the_work_lands_in_recently_finished(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    client = _client(app)
    released = _releases(monkeypatch)
    await app.state.work_overview.current()
    hidden = _visibility(client, "hide")
    assert hidden.status_code == 200 and "Hidden from Needs you" in hidden.text
    assert _ids(app.state.work_overview.overview.front.hidden) == [WORK.work_id]

    assert _visibility(client, "finish").status_code == 200

    after = app.state.work_overview.overview
    assert _ids(after.front.finished) == [WORK.work_id]
    assert app.state.control_db.hidden_works() == {}
    assert released == [WORK.work_id], "a hide gives nothing back; the Finish does"


def test_closing_the_issue_from_the_deck_is_the_sign_off(tmp_path, monkeypatch):
    from test_work_actions import WORK, _closable_app, _Gh, _post
    from test_work_actions import _client as client_for

    released = _releases(monkeypatch)
    app, _gh, _ = _closable_app(tmp_path, monkeypatch)
    assert _post(client_for(app), "close").status_code == 200
    closed, finished = app.state.control_db.work_signoffs()[WORK.work_id]
    assert finished is not None and finished >= closed
    assert released == [WORK.work_id]

    refused, _gh, _ = _closable_app(
        tmp_path / "refused", monkeypatch,
        gh=_Gh(gh_cli.GitHubRefusal("github: no GitHub authority configured for owner x")),
    )
    assert _post(client_for(refused), "close").status_code == 409
    assert refused.state.control_db.work_signoffs() == {}
    assert released == [WORK.work_id], "a refused close gives nothing back"


def _closed_works(tmp_path, monkeypatch, count):
    """``count`` Works whose issues closed and wait for Finish, all in the same repository."""
    from test_work_ui import WORK, _add

    from agentdeck.work import WorkRef

    app, record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    works = [WORK, *(WorkRef(WORK.repository, 4700 + i) for i in range(1, count))]
    for work in works[1:]:
        _add(app, f"claude_code:main:worker-{work.number}", work,
             last_activity=NOW - timedelta(minutes=10))
    records = {(work.repository, work.number): dict(record) for work in works}
    app.state.issues.cached_issues = lambda refs: records
    return app, records, works


@pytest.mark.asyncio
async def test_finish_all_finishes_the_named_works_that_only_wait_for_it(tmp_path, monkeypatch):
    """#4623: each named Work is checked again; the rest are skipped and said so."""
    import re

    from agentdeck import work_story

    released = _releases(monkeypatch)
    app, records, works = _closed_works(tmp_path, monkeypatch, 6)
    one, two, reopened, opened, unrecorded, unnamed = works
    real = work_story.FrontRow.finish.fget
    monkeypatch.setattr(work_story.FrontRow, "finish", property(
        lambda row: replace(real(row), confirm="o/store#9 is still open. Finish anyway?")
        if row.actor.work_id == opened.work_id else real(row)
    ))
    await app.state.work_overview.refresh()
    client = _client(app)
    front = client.get("/").text
    head = re.search(r'data-front-group="issue_closed".*?</h3>', front, re.S).group(0)
    button = re.search(r"<button[^>]*data-finish-all[^>]*>", head).group(0)
    assert " hidden" not in button
    assert set(re.findall(r'data-finish-id="([^"]+)"', front)) == {
        work.work_id for work in works if work is not opened
    }

    db = app.state.control_db
    real_finish = db.finish_work
    monkeypatch.setattr(
        db, "finish_work", lambda wid, at: wid != unrecorded.work_id and real_finish(wid, at)
    )
    records[(reopened.repository, reopened.number)].update(state="open", closedAt="")
    response = client.post("/works/finish-all", data={
        "work": [w.work_id for w in (one, two, one, reopened, opened, unrecorded)]
    })

    assert response.status_code == 200, response.text
    assert "Finished 2. Skipped 3: " in response.text
    assert f"#{opened.number} has an open pull request" in response.text
    assert f"#{reopened.number} no longer waits for Finish" in response.text
    assert f"#{unrecorded.number} could not be recorded" in response.text
    named = re.search(r"data-finished-works='([^']*)'", response.text).group(1)
    assert json.loads(named) == [one.work_id, two.work_id]
    signed = {wid for wid, (_, at) in db.work_signoffs().items() if at is not None}
    assert signed == {one.work_id, two.work_id}
    assert released == [one.work_id, two.work_id]
    left = _ids(app.state.work_overview.overview.front.needs_you)  # the press rebuilt it
    assert unnamed.work_id in left and one.work_id not in left
    assert _NoGh.calls == 0

    again = client.post("/works/finish-all", data={"work": [one.work_id]})
    assert again.status_code == 409 and "data-finished-works" not in again.text


@pytest.mark.asyncio
async def test_finish_all_leaves_a_starred_work_and_hides_at_one_row(tmp_path, monkeypatch):
    import re

    _releases(monkeypatch)
    app, _records, (one, starred) = _closed_works(tmp_path, monkeypatch, 2)
    db = app.state.control_db
    db.star_work(starred.work_id)
    await app.state.work_overview.refresh()
    client = _client(app)
    front = client.get("/").text

    # Listed under Starred and in Issue closed (#4745), where Finish all still leaves it.
    assert front.count(f'data-work-id="{starred.work_id}"') == 2
    assert re.findall(r'data-finish-id="([^"]+)"', front) == [one.work_id]
    assert " hidden" in re.search(r"<button[^>]*data-finish-all[^>]*>", front).group(0)

    response = client.post("/works/finish-all", data={"work": [one.work_id, starred.work_id]})
    assert response.status_code == 200, response.text
    assert f"#{starred.number} no longer waits for Finish" in response.text
    assert {wid for wid, (_, at) in db.work_signoffs().items() if at} == {one.work_id}


def test_finish_all_refuses_what_it_cannot_check(tmp_path, monkeypatch):
    app, _records, (work, _other) = _closed_works(tmp_path, monkeypatch, 2)
    client = _client(app)

    def press(*ids, **kwargs):
        return client.post("/works/finish-all", data={"work": list(ids)}, **kwargs).status_code

    assert press() == 409
    assert press("nonsense") == 422
    assert press(*(f"github_issue:o/r#{n}" for n in range(1, 202))) == 409
    assert press(work.work_id, headers={"Origin": "https://evil.example"}) == 403

    async def broken():
        raise RuntimeError("no overview")

    monkeypatch.setattr(app.state.work_overview, "refresh", broken)
    assert press(work.work_id) == 409
    assert all(at is None for _, at in app.state.control_db.work_signoffs().values())


def test_finish_all_takes_only_issue_closed_rows_and_the_leading_child():
    from agentdeck.work_story import FrontRow

    def row(category, *, waits=True, lead=None, outputs=(), work="github_issue:o/r#1"):
        card = SimpleNamespace(work_id=work, awaiting_finish=waits, outputs=outputs)
        return FrontRow(card, None, "needs_you", "", attention=SimpleNamespace(category=category),
                        lead=lead)

    child = row(ISSUE_CLOSED, work="github_issue:o/r#2")
    assert row(ISSUE_CLOSED).finish_all_id == "github_issue:o/r#1"
    assert row(ISSUE_CLOSED, lead=child).finish_all_id == "github_issue:o/r#2"
    assert row(ISSUE_CLOSED, waits=False).finish_all_id == ""
    assert row("needs_answer").finish_all_id == ""  # live_failed and not_live rows
    pull = SimpleNamespace(is_open=True, label="o/r#9")
    assert row(ISSUE_CLOSED, outputs=(pull,)).finish_all_id == ""


@pytest.mark.browser
@pytest.mark.asyncio
async def test_finish_all_asks_once_and_takes_only_what_is_shown(tmp_path, monkeypatch):
    """#4623: the count asked, a declined ask, the fold, and a skipped row coming back."""
    from urllib.parse import parse_qs

    from conftest import async_playwright
    from test_web import _live_server

    _releases(monkeypatch)
    app, records, works = _closed_works(tmp_path, monkeypatch, 3)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 640},
                                      service_workers="block")
        await page.goto(base + "/")
        head = page.locator('#works-cleanup [data-front-group="issue_closed"]')
        button = head.locator("[data-finish-all]")
        await button.wait_for()
        await page.wait_for_load_state("load")
        # Asked of the page, not of one node: the first push redraws the header.
        await page.wait_for_function("""() => {
          const box = document.querySelector('#works-cleanup [data-front-group="issue_closed"]')
            ?.getBoundingClientRect();
          return box && box.height > 0 && box.height <= 37;
        }""")
        rows = page.locator("#works-cleanup article[data-finish-id]")
        assert await rows.count() == 3

        await head.locator("[data-front-toggle]").click()
        assert await button.is_hidden()  # folded away
        await head.locator("[data-front-toggle]").click()
        assert await button.is_visible()

        # Finish all takes only what a search shows, and a search looks inside the fold (#4624).
        numbers = [work.number for work in works]
        await head.locator("[data-front-toggle]").click()
        await page.locator("[data-find-open]").click()
        await page.locator("#front-find-input").fill(f"#{numbers[0]}")
        assert await button.is_hidden()
        finishable = "() => AgentDeckFront.finishable().map(row => row.dataset.workId)"
        assert await page.evaluate(finishable) == [works[0].work_id]
        await page.locator("#front-find-input").fill("#")
        assert await button.is_visible() and len(await page.evaluate(finishable)) == 3
        await page.keyboard.press("Escape")
        assert await button.is_hidden()  # folded away again
        await head.locator("[data-front-toggle]").click()

        # A row on a project the tabs hide is left out of the press.
        await rows.nth(2).evaluate("row => row.setAttribute('data-work-project', 'elsewhere')")
        await page.evaluate("""() => {
          localStorage.setItem('agentdeck-front-hidden-projects', '{"elsewhere": true}');
          AgentDeckFront.apply();
        }""")
        left = await rows.nth(2).get_attribute("data-work-id")

        asked, sent = [], []
        answer = {"accept": False}

        async def on_dialog(dialog):
            asked.append(dialog.message)
            await (dialog.accept() if answer["accept"] else dialog.dismiss())

        page.on("dialog", on_dialog)
        held = asyncio.Event()

        async def hold(route):
            sent.append(parse_qs(route.request.post_data)["work"])
            if answer.get("fail") == "refused":
                return await route.fulfill(status=409, content_type="text/html", body="<p>No</p>")
            if answer.get("fail") == "lost":
                return await route.abort()
            await held.wait()
            await route.continue_()

        await page.route("**/works/finish-all", hold)
        await button.click()
        assert asked == ["Finish 2 Works?"] and sent == []
        assert await rows.nth(0).is_visible()  # nothing folded either

        answer["accept"] = True
        for fail in ("refused", "lost"):  # every row comes back, and a new press is taken
            answer["fail"] = fail
            async with page.expect_request("**/works/finish-all"):
                await button.click()
            await page.wait_for_function(
                "() => !document.querySelector('#front-finish-all')._finishAllBusy"
            )
            assert await page.locator("[data-work-finishing]").count() == 0, fail
            assert await rows.nth(0).is_visible() and await rows.nth(1).is_visible()
            assert await button.is_visible()
        assert [len(ids) for ids in sent] == [2, 2]
        answer["fail"] = ""
        sent.clear()
        skipped = await rows.nth(0).get_attribute("data-work-id")
        done = await rows.nth(1).get_attribute("data-work-id")
        ref = next(w for w in works if w.work_id == skipped)
        records[(ref.repository, ref.number)].update(state="open", closedAt="")
        await button.click()
        await page.locator(f'#works-cleanup [data-work-id="{done}"]').wait_for(state="hidden")
        assert await button.is_hidden()
        held.set()
        await page.locator("#work-action-result [data-finished-works]").wait_for()
        assert len(sent) == 1 and set(sent[0]) == {skipped, done} and left not in sent[0]
        await page.locator(f'[data-work-id="{done}"][data-front-section="finished"]').wait_for()
        assert await page.locator(f'[data-work-id="{skipped}"][data-work-finishing]').count() == 0
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_finish_takes_the_row_away_on_the_press_and_a_refusal_puts_it_back(
    tmp_path, monkeypatch
):
    """#3287: the row leaves Needs you as Finish is pressed, before the deck answers."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_ui import WORK

    app, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    _releases(monkeypatch)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 640},
                                      service_workers="block")
        await page.clock.install()  # time flows; the hold's deadline is jumped to below
        await page.goto(base + "/")
        row = page.locator(f'#works-cleanup [data-work-id="{WORK.work_id}"]')
        count = page.locator('#works-cleanup [data-front-group="issue_closed"] .fr-group-count')
        await row.wait_for()
        await page.wait_for_load_state("load")  # htmx has wired the form
        held = asyncio.Event()
        answer = {"status": 409}

        async def hold(route):
            await held.wait()
            if answer["status"] == 409:
                await route.fulfill(status=409, body="<p>refused</p>")
            else:
                await route.continue_()

        await page.route("**/visibility", hold)
        await row.locator('[data-front-action="finish"]').click()
        await row.wait_for(state="hidden")  # still unanswered
        assert await count.is_hidden()
        held.set()
        await row.wait_for(state="visible")  # refused: nothing finished
        assert await count.text_content() == "1"

        held.clear()
        answer["status"] = 200
        stale = await page.evaluate("() => document.getElementById('works-cleanup').innerHTML")
        rev = await row.get_attribute("data-work-rev")
        await row.locator('[data-front-action="finish"]').click()
        await row.wait_for(state="hidden")
        # #3299: a push from before the rebuild still lists the Work, at a new revision, and
        # brings a fresh row and header count; neither puts the Work back on the page.
        await page.evaluate(
            """([html, id]) => {
              const push = (list, markup) => htmx.swap(
                document.getElementById(list), markup,
                {swapStyle: 'keyed-works'}, {contextElement: document.getElementById(list)});
              push('works-cleanup', html.replace(/data-work-rev="[^"]*"/, 'data-work-rev="newer"'));
              // Out of order: Recently finished shows it before Needs you's last stale push.
              push('works-finished', `<article class="fr" data-work-id="${id}"
                data-work-rev="done" data-front-section="finished"></article>`);
              push('works-cleanup',
                html.replace(/data-work-rev="[^"]*"/, 'data-work-rev="newest"'));
            }""",
            [stale, WORK.work_id],
        )
        assert await row.count() == 1  # the pushed row, not the pressed one
        assert await row.get_attribute("data-work-rev") == "newest"
        assert await row.is_hidden() and await count.is_hidden()
        # With no push at all, the hold still ends at its deadline and the deck's list stands.
        # The row gets back the revision the stream sent, so the stream's next placeholder for
        # it keeps it rather than reading it as lost.
        await row.evaluate("(el, rev) => el.setAttribute('data-work-rev', rev)", rev)
        await page.clock.fast_forward("02:01")
        # Read at once: a push the jump sets off would end the hold too, but takes a round trip.
        assert await row.is_visible() and await count.text_content() == "1"
        held.set()
        await row.wait_for(state="detached")  # the rebuild's push leaves it out
        await page.locator(
            f'#works-finished [data-work-id="{WORK.work_id}"]:not([data-work-rev="done"])'
        ).wait_for()
        await browser.close()
