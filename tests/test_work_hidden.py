"""Hidden Works (#2238): Hide takes a Work out of Needs you until something new happens.

Hide is this deck's own display preference. It never writes to GitHub or the board, it
suppresses Needs you only, and it lasts until the source issue gets any new comment. Missing
data never brings a Work back by itself.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentdeck import gh_cli
from agentdeck.work_hidden import HiddenWork, issue_comments, still_hidden
from agentdeck.work_overview import work_href

# --- the rule ------------------------------------------------------------------------------------


def _hidden(comments=3) -> HiddenWork:
    return HiddenWork("github_issue:eerovil/agentdeck#2147", "2026-09-26T10:00:00+00:00", comments)


def test_any_newer_comment_ends_a_hide_and_nothing_else_about_comments_does():
    hidden = _hidden(comments=3)

    assert still_hidden(hidden, 3)
    assert not still_hidden(hidden, 4)
    # A deleted comment is not a new one, and an unknown count is not a change.
    assert still_hidden(hidden, 2)
    assert still_hidden(hidden, None)
    # Hidden before the count was known: nothing to compare it with yet.
    assert still_hidden(_hidden(comments=None), 9)


def test_a_chat_carried_on_after_the_archive_ends_it_and_one_before_does_not():
    """#2357: ``continued`` later than ``hidden_at`` brings an archived finished Work back."""
    from datetime import UTC, datetime

    hidden = _hidden()
    at = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)

    assert still_hidden(hidden, 3, None)
    assert still_hidden(hidden, 3, at.replace(hour=9))
    assert still_hidden(hidden, 3, at)
    assert not still_hidden(hidden, 3, at.replace(minute=1))
    # An unreadable hide time keeps the hide rather than guessing.
    assert still_hidden(replace(hidden, hidden_at="soon"), 3, at.replace(hour=11))


@pytest.mark.parametrize(
    ("record", "count"),
    [
        ({"comments": 4}, 4),
        ({"comments": 0}, 0),
        ({}, None),
        (None, None),
        ({"comments": True}, None),
        ({"comments": -1}, None),
        ({"comments": "4"}, None),
    ],
)
def test_the_comment_count_is_read_only_when_it_is_a_count(record, count):
    assert issue_comments(record) == count


# --- the store -----------------------------------------------------------------------------------


def test_a_hide_is_durable_idempotent_and_brought_back_by_delete(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    work_id = "github_issue:eerovil/agentdeck#2147"

    assert db.hide_work(work_id, None) and db.hide_work(work_id, None)
    db.note_hidden_comments(work_id, 5)
    db.note_hidden_comments(work_id, 7)  # never raised: a higher count ends the hide instead
    (hidden,) = Db(tmp_path / "control.sqlite3").hidden_works().values()
    assert (hidden.work_id, hidden.comments) == (work_id, 5)
    db.note_hidden_comments(work_id, 4)  # a deleted comment lowers the baseline
    assert db.hidden_works()[work_id].comments == 4
    assert db.unhide_work(work_id) and db.unhide_work(work_id)
    assert db.hidden_works() == {}


# --- through the deck ----------------------------------------------------------------------------


class _NoGh:
    """Hide and Bring back never call GitHub."""

    calls = 0

    async def __call__(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("a visibility change must not call gh")


def _deck(tmp_path, monkeypatch, *, comments=1):
    """A real Needs retry Work — its chat stopped — read through the deck's issue record."""
    from test_work_ui import WORK, _add, _app, _attention, _board, _insight

    app = _app(tmp_path, board=_board())
    _attention(app, _insight(_add(app, "claude_code:test:worker", WORK), "stalled"))
    record = {"comments": comments}
    app.state.issues.cached_issues = lambda refs: {
        (WORK.repository, WORK.number): {
            "title": "Hide me",
            "state": "open",
            "is_pr": False,
            **record,
        }
    }
    monkeypatch.setattr(gh_cli, "run_once", _NoGh())
    return app, record


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _visibility(client, action, **headers):
    from test_work_ui import WORK

    return client.post(
        work_href(WORK.work_id) + "/visibility", data={"action": action}, headers=headers
    )


def _ids(rows):
    return [row.card.work_id for row in rows]


@pytest.mark.asyncio
async def test_hide_moves_a_work_from_needs_you_to_hidden_and_writes_nothing_else(
    tmp_path, monkeypatch
):
    from test_work_ui import WORK

    app, _record = _deck(tmp_path, monkeypatch)
    client = _client(app)
    before = await app.state.work_overview.current()
    assert _ids(before.front.needs_you) == [WORK.work_id]

    response = _visibility(client, "hide")

    assert response.status_code == 200 and 'data-wd-receipt="sent"' in response.text
    after = app.state.work_overview.overview  # the press rebuilt it
    assert _ids(after.front.needs_you) == [] and _ids(after.front.hidden) == [WORK.work_id]
    (hidden,) = app.state.control_db.hidden_works().values()
    assert hidden.comments == 1
    front = client.get("/", params={"chats": "0"}).text
    assert "data-hidden-link" in front and "Hidden (1)" in front
    listed = client.get("/works", params={"view": "hidden"}).text
    assert f'data-work-id="{WORK.work_id}"' in listed and 'value="unhide"' in listed
    assert 'data-hidden-count sse-swap="works-hidden-count" hx-swap="innerHTML">1<' in listed
    # The list and its count follow the overview's stream (#3228).
    assert 'sse-connect="/events?view=works-hidden"' in listed
    page = client.get(work_href(WORK.work_id)).text
    assert "data-work-hidden" in page and 'value="hide"' not in page
    assert _NoGh.calls == 0


@pytest.mark.asyncio
async def test_bring_back_returns_it_and_a_replay_of_either_is_harmless(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _record = _deck(tmp_path, monkeypatch)
    client = _client(app)
    await app.state.work_overview.current()

    assert _visibility(client, "hide").status_code == 200
    assert _visibility(client, "hide").status_code == 200
    assert _visibility(client, "unhide").status_code == 200
    assert _visibility(client, "unhide").status_code == 200

    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]
    assert app.state.control_db.hidden_works() == {}
    assert "data-work-hidden" not in client.get(work_href(WORK.work_id)).text


@pytest.mark.asyncio
async def test_a_new_comment_brings_it_back(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, record = _deck(tmp_path, monkeypatch)
    client = _client(app)
    await app.state.work_overview.current()
    _visibility(client, "hide")

    record["comments"] = 2
    await app.state.work_overview.refresh()

    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]
    assert app.state.control_db.hidden_works() == {}  # expired, not only ignored


@pytest.mark.asyncio
async def test_missing_data_never_brings_it_back(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _record = _deck(tmp_path, monkeypatch)
    client = _client(app)
    await app.state.work_overview.current()
    _visibility(client, "hide")

    app.state.issues.cached_issues = lambda refs: {}  # the issue record is gone for now
    await app.state.work_overview.refresh()

    assert _ids(app.state.work_overview.overview.front.hidden) == [WORK.work_id]
    assert len(app.state.control_db.hidden_works()) == 1


@pytest.mark.asyncio
async def test_a_hide_taken_without_a_count_records_the_first_count_seen(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, record = _deck(tmp_path, monkeypatch)
    del record["comments"]  # a record cached before it carried the count
    client = _client(app)
    await app.state.work_overview.current()
    _visibility(client, "hide")

    record["comments"] = 4
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.hidden) == [WORK.work_id]
    assert app.state.control_db.hidden_works()[WORK.work_id].comments == 4

    record["comments"] = 5
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]


def test_hide_suppresses_needs_you_only(tmp_path):
    """A hidden Work that is Active or finished shows there: Hide is about Needs you alone."""
    from test_work_attention import _derived, _work

    from agentdeck.work_story import front_row, front_sections

    running = replace(
        _work(derived=_derived("working")), work_id="github_issue:eerovil/agentdeck#6"
    )
    parked = _work(derived=_derived("question"))
    rows = [front_row(running, current=True), front_row(parked, current=True)]
    assert [row.section for row in rows] == ["active", "needs_you"]

    shown = front_sections(rows, hidden={running.work_id})
    assert _ids(shown.active) == [running.work_id] and shown.hidden == ()
    assert _ids(shown.needs_you) == [parked.work_id]

    put_away = front_sections(rows, hidden={running.work_id, parked.work_id})
    assert _ids(put_away.needs_you) == [] and _ids(put_away.hidden) == [parked.work_id]
    assert _ids(put_away.active) == [running.work_id]


@pytest.mark.parametrize(
    ("data", "status", "text"),
    [({"action": "remove"}, 422, "not a visibility action"), ({}, 422, "not a visibility action")],
)
def test_an_unknown_visibility_action_is_refused(tmp_path, monkeypatch, data, status, text):
    from test_work_ui import WORK

    app, _record = _deck(tmp_path, monkeypatch)
    response = _client(app).post(work_href(WORK.work_id) + "/visibility", data=data)

    assert response.status_code == status and text in response.text
    assert app.state.control_db.hidden_works() == {}


def test_a_finished_work_is_archived_off_recently_finished(tmp_path, monkeypatch):
    """#2254: Archive on a finished row is the same deck-only hide."""
    from datetime import UTC, datetime, timedelta

    from test_work_ui import WORK, _closed

    app, _record = _deck(tmp_path, monkeypatch)
    _closed(app, {WORK.number: datetime.now(UTC) - timedelta(hours=1)})
    assert _client(app).get(work_href(WORK.work_id)).status_code == 200

    response = _visibility(_client(app), "hide")

    assert response.status_code == 200 and "Archived from Recently finished" in response.text
    assert WORK.work_id in app.state.control_db.hidden_works()


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "fetch-site"],
)
def test_a_cross_site_visibility_change_is_refused(tmp_path, monkeypatch, headers):
    app, _record = _deck(tmp_path, monkeypatch)

    response = _visibility(_client(app), "hide", **headers)

    assert response.status_code == 403 and app.state.control_db.hidden_works() == {}


# --- in the browser ------------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_hide_then_bring_back_on_a_phone(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_actions import _no_overflow
    from test_work_ui import WORK

    app, _record = _deck(tmp_path, monkeypatch)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 320, "height": 640}, service_workers="block"
        )
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + "/")
        row = page.locator(f'#works-stuck [data-work-id="{WORK.work_id}"]')
        await row.wait_for()
        await page.wait_for_function("() => window.__sseOpen === true")
        # Hide is on the Work page with every other action (#2254); the row only opens it.
        await page.goto(base + work_href(WORK.work_id))
        await page.wait_for_load_state("load")  # htmx has wired the form
        await page.locator('#work-status .wd-op [data-front-action="hide"]').click()
        await page.locator('#work-action-result [data-wd-receipt="sent"]').wait_for()
        await page.goto(base + "/")
        link = page.locator("#works-paused [data-hidden-link]")
        await link.wait_for()
        assert "Hidden (1)" in await link.text_content()
        await _no_overflow(page, 320)

        await link.click()
        hidden = page.locator(f'#works-hidden [data-work-id="{WORK.work_id}"]')
        await hidden.wait_for()
        await _no_overflow(page, 320)
        await page.wait_for_load_state("load")  # htmx has wired the form
        await hidden.locator('[data-front-action="unhide"]').click()
        await page.locator('#work-action-result [data-wd-receipt="sent"]').wait_for()

        await page.goto(base + "/")
        await page.locator(f'#works-stuck [data-work-id="{WORK.work_id}"]').wait_for()
        assert await page.locator("#works-paused [data-hidden-link]").count() == 0
        assert app.state.control_db.hidden_works() == {} and _NoGh.calls == 0
        await browser.close()


@pytest.mark.asyncio
async def test_a_comment_after_a_deleted_one_still_brings_it_back(tmp_path, monkeypatch):
    """3 at Hide, one deleted (2), one new (3 again): that new comment still counts."""
    from test_work_ui import WORK

    app, record = _deck(tmp_path, monkeypatch, comments=3)
    client = _client(app)
    await app.state.work_overview.current()
    _visibility(client, "hide")

    record["comments"] = 2
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.hidden) == [WORK.work_id]
    assert app.state.control_db.hidden_works()[WORK.work_id].comments == 2

    record["comments"] = 3
    await app.state.work_overview.refresh()
    assert _ids(app.state.work_overview.overview.front.needs_you) == [WORK.work_id]
    assert app.state.control_db.hidden_works() == {}
