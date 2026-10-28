"""A Work's own title and resolution (#2328): stored with the Work, shown on its page."""

from __future__ import annotations

import dataclasses

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import gh_cli
from agentdeck.work_overview import work_href, work_title
from agentdeck.work_resolution import MAX_TITLE, WorkResolution, normalize_title

WORK_ID = "github_issue:eerovil/agentdeck#2147"


def _resolution(**fields) -> WorkResolution:
    parts = {
        "problem": "The Work page could not say why a change was made.",
        "solution": "A Work-owned record the verify worker writes.",
        "rationale": "Rebuilding it from chats was rejected: chats age out.",
        "correctness": "Focused tests pass; the selected suite is running.",
    }
    parts.update(fields)
    return WorkResolution(WORK_ID, **parts)


# --- the store -----------------------------------------------------------------------------------


def test_a_custom_title_survives_a_reopen_and_clearing_it_restores_the_fallback(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    assert db.work_titles() == {}
    assert db.set_work_title(WORK_ID, "My own name")
    assert Db(tmp_path / "control.sqlite3").work_titles() == {WORK_ID: "My own name"}
    assert db.set_work_title(WORK_ID, "")
    assert Db(tmp_path / "control.sqlite3").work_titles() == {}


def test_a_resolution_write_replaces_the_one_record_and_keeps_the_title(tmp_path):
    from agentdeck.db import Db

    db = Db(tmp_path / "control.sqlite3")
    assert db.work_resolution(WORK_ID) is None
    db.set_work_title(WORK_ID, "Named")
    first = db.set_work_resolution(_resolution(run_id="run-1", session_key="claude_code:a:s1"))
    assert first is not None and first.run_id == "run-1" and first.updated_at is not None

    again = db.set_work_resolution(
        _resolution(correctness="The selected suite passed.", run_id="run-2")
    )

    reopened = Db(tmp_path / "control.sqlite3")
    stored = reopened.work_resolution(WORK_ID)
    assert stored == again
    assert stored.correctness == "The selected suite passed." and stored.run_id == "run-2"
    assert stored.session_key == ""  # the whole record is replaced, provenance included
    assert reopened.work_titles() == {WORK_ID: "Named"}
    rows = reopened._conn.execute("SELECT COUNT(*) FROM work_metadata").fetchone()[0]
    assert rows == 1
    # A title written after a resolution leaves the resolution alone.
    reopened.set_work_title(WORK_ID, "")
    assert reopened.work_resolution(WORK_ID) == stored


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("  Two   words \n", "Two words"),
        ("", ""),
        (None, ""),
        ("x" * (MAX_TITLE + 5), "x" * MAX_TITLE),
    ],
)
def test_a_title_is_one_trimmed_bounded_line(typed, stored):
    assert normalize_title(typed) == stored


def test_the_custom_title_wins_but_keeps_the_issue_state():
    from test_work_ui import WORK

    record = {"title": "From GitHub", "state": "OPEN", "is_pr": False}
    assert work_title(WORK, record=record) == ("From GitHub", "open")
    assert work_title(WORK, record=record, custom="Mine") == ("Mine", "open")
    assert work_title(WORK, record=None, custom="") == ("", "")


# --- through the deck ----------------------------------------------------------------------------


class _NoGh:
    """Renaming a Work never touches GitHub, so the issue keeps its title."""

    calls = 0

    async def __call__(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("renaming a Work must not call gh")


def _deck(tmp_path, monkeypatch):
    from test_work_ui import WORK, _add, _app

    app = _app(tmp_path)
    _add(app, "claude_code:test:worker", WORK, thinking=True)
    app.state.issues.cached_issues = lambda refs: {
        (WORK.repository, WORK.number): {"title": "From GitHub", "state": "open", "is_pr": False}
    }
    monkeypatch.setattr(gh_cli, "run_once", _NoGh())
    return app


def _rename(client, **data):
    return client.post(work_href(WORK_ID) + "/title", data=data)


def _front_titles(overview) -> set[str]:
    front = overview.front
    return {row.card.title for row in (*front.needs_you, *front.active, *front.finished)}


@pytest.mark.asyncio
async def test_renaming_shows_on_the_work_page_and_the_front_page_and_clearing_restores(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    page = client.get(work_href(WORK_ID)).text
    assert "From GitHub" in page and "data-work-rename-open" in page and "data-work-rename" in page

    response = _rename(client, title="  My   Work ", action="save")

    assert response.status_code == 200 and 'data-wd-receipt="sent"' in response.text
    assert app.state.control_db.work_titles() == {WORK_ID: "My Work"}
    page = client.get(work_href(WORK_ID)).text
    assert '<h1 class="wd-title">My Work<button' in page and 'value="My Work"' in page
    assert "<title>eerovil/agentdeck#2147 · My Work" in page
    assert "My Work" in _front_titles(app.state.work_overview.overview)  # the press rebuilt it

    assert _rename(client, title="ignored", action="clear").status_code == 200
    assert app.state.control_db.work_titles() == {}
    assert '<h1 class="wd-title">From GitHub<button' in client.get(work_href(WORK_ID)).text
    assert "From GitHub" in _front_titles(app.state.work_overview.overview)
    assert _NoGh.calls == 0


def test_an_empty_name_clears_it_too(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    _rename(client, title="Mine")
    assert _rename(client, title="   ").status_code == 200
    assert app.state.control_db.work_titles() == {}


@pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}],
    ids=["origin", "fetch-site"],
)
def test_a_cross_site_rename_is_refused(tmp_path, monkeypatch, headers):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    response = TestClient(app).post(work_href(WORK_ID) + "/title", data={"title": "x"},
                                    headers=headers)
    assert response.status_code == 403 and app.state.control_db.work_titles() == {}


def test_the_work_page_shows_the_resolution_only_once_one_is_written(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    client = TestClient(app)
    assert "data-work-resolution" not in client.get(work_href(WORK_ID)).text

    app.state.control_db.set_work_resolution(_resolution(rationale="Line one.\nLine two."))

    page = client.get(work_href(WORK_ID)).text
    assert "data-work-resolution" in page
    for heading in ("Problem", "Solution", "Why this solution", "Why we believe it is correct"):
        assert f'<h4 class="wd-title-xs">{heading}</h4>' in page
    assert '<div class="wd-resolution-text" data-wd-resolution="rationale">' in page
    assert "<p>Line one.\nLine two.</p>" in page
    assert page.index('id="work-sec-result"') < page.index('id="work-sec-resolution"')


def test_the_work_page_breaks_a_part_into_paragraphs_and_bullets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _deck(tmp_path, monkeypatch)
    app.state.control_db.set_work_resolution(_resolution(
        solution="Store-box shelves hold whole boxes.\n\n"
        "- Scanning a box shelves it\n- <b>Bold</b>",
    ))

    page = TestClient(app).get(work_href(WORK_ID)).text

    solution = page[page.index('data-wd-resolution="solution"'):]
    solution = solution[:solution.index("</div>")]
    assert "<p>Store-box shelves hold whole boxes.</p>" in solution
    assert "<ul><li>Scanning a box shelves it</li><li>&lt;b&gt;Bold&lt;/b&gt;</li></ul>" in solution
    assert "<details" not in solution


@pytest.mark.parametrize(
    ("text", "blocks"),
    [
        ("One paragraph.", (("p", "One paragraph."),)),
        ("Line one.\nLine two.", (("p", "Line one.\nLine two."),)),
        ("Gist.\r\n\r\nDetail.", (("p", "Gist."), ("p", "Detail."))),
        ("Gist.\n  \n\n\nDetail.", (("p", "Gist."), ("p", "Detail."))),
        (
            "Gist.\n\n- one\n* two\n  wrapped\nAfter.",
            (("p", "Gist."), ("ul", ("one", "two wrapped")), ("p", "After.")),
        ),
        ("Intro:\n- a\n- b", (("p", "Intro:"), ("ul", ("a", "b")))),
        ("-not a bullet\n*nor this", (("p", "-not a bullet\n*nor this"),)),
        ("Order.save calls x. e.g. y", (("p", "Order.save calls x. e.g. y"),)),
        ("", ()),
        ("- \n- kept", (("p", "-"), ("ul", ("kept",)))),
    ],
)
def test_resolution_blocks_reads_only_paragraphs_and_bullets(text, blocks):
    from agentdeck.work_resolution import resolution_blocks

    assert resolution_blocks(text) == blocks


# --- the verify worker's write -------------------------------------------------------------------


def _history_deck(tmp_path):
    """A deck with one real chat, ``KEY``, whose account the API resolves."""
    from test_web import _app_with_state

    return _app_with_state(tmp_path, with_transcript=True)


KEY = "claude_code:test:sid1"


_BODY = {
    "problem": "P",
    "solution": "S",
    "rationale": "R: the alternative was rejected because of X.",
    "correctness": "C: the selected suite is running.",
}


@pytest.mark.asyncio
async def test_the_worker_writes_its_own_works_resolution_and_a_retry_replaces_it(tmp_path):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    key = KEY
    state.bind_session_run(key, WORK_ID, "run-1")
    url = f"/api/sessions/{key}/work-resolution"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get(url)).json() == {"work_id": WORK_ID, "resolution": None}
        first = await client.put(url, json=_BODY)
        assert first.status_code == 200
        body = first.json()["resolution"]
        assert body["run_id"] == "run-1" and body["session_key"] == key
        assert body["rationale"] == _BODY["rationale"]

        again = await client.put(url, json={**_BODY, "correctness": "The selected suite passed."})
        assert again.status_code == 200
        read = (await client.get(url)).json()["resolution"]

    assert read["correctness"] == "The selected suite passed."
    stored = app.state.control_db.work_resolution(WORK_ID)
    assert stored is not None and stored.correctness == "The selected suite passed."
    count = app.state.control_db._conn.execute("SELECT COUNT(*) FROM work_metadata").fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_the_worker_records_its_live_check_apart_from_the_resolution(tmp_path):
    """#3141: the live check is its own record, so a later resolution write keeps it."""
    app = _history_deck(tmp_path)
    app.state.app_state.bind_session_run(KEY, WORK_ID, "run-1")
    url = f"/api/sessions/{KEY}/live-check"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.get(url)).json() == {"work_id": WORK_ID, "live_check": None}
        bad = await client.put(url, json={"result": "fine"})
        assert bad.status_code == 422 and "result must be one of" in bad.json()["detail"]
        put = await client.put(url, json={"result": " Verified ", "note": "the  card\nshows"})
        assert put.status_code == 200
        assert put.json()["live_check"]["result"] == "verified"
        await client.put(f"/api/sessions/{KEY}/work-resolution", json=_BODY)
        read = (await client.get(url)).json()["live_check"]
    assert (read["result"], read["note"]) == ("verified", "the card shows")
    assert read["session_key"] == KEY
    check = app.state.control_db.work_live_check(WORK_ID)
    assert check.line == "checked live: the card shows"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        put = await client.put(url, json={"result": "not_live", "note": "VPS connector"})
    assert put.status_code == 200, "#3220: merged but not running yet"
    assert app.state.control_db.work_live_check(WORK_ID).line == "not live yet: VPS connector"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({**_BODY, "rationale": "   "}, "rationale must not be empty"),
        ({**_BODY, "problem": "x" * 9000}, "problem is longer than"),
    ],
)
async def test_an_empty_or_runaway_part_is_refused(tmp_path, body, detail):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    key = KEY
    state.bind_session_run(key, WORK_ID, "run-1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.put(f"/api/sessions/{key}/work-resolution", json=body)
    assert response.status_code == 422 and detail in response.json()["detail"]
    assert app.state.control_db.work_resolution(WORK_ID) is None


@pytest.mark.asyncio
async def test_a_chat_in_no_run_cannot_write_a_resolution(tmp_path):
    app = _history_deck(tmp_path)
    key = KEY
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.put(f"/api/sessions/{key}/work-resolution", json=_BODY)
    assert response.status_code == 409 and "not part of any Work" in response.json()["detail"]


OTHER_WORK = "github_issue:eerovil/agentdeck#99"


async def _put_as(app, key, *, work_id=None):
    body = {**_BODY, **({"work_id": work_id} if work_id is not None else {})}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.put(f"/api/sessions/{key}/work-resolution", json=body)


async def _register(app, key, issue):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/sessions/{key}/issues", json={"issue": issue})
    assert response.status_code == 200, response.text


def _issue_of(work_id):
    return work_id.split(":", 1)[1]


@pytest.mark.asyncio
async def test_a_rescue_chat_that_registered_the_issue_writes_the_resolution(tmp_path):
    """#2552: a person's rescue chat has no Run, but registering the issue is its own act."""
    app = _history_deck(tmp_path)
    await _register(app, KEY, _issue_of(WORK_ID))

    response = await _put_as(app, KEY)

    assert response.status_code == 200, response.text
    assert response.json()["resolution"]["run_id"] is None
    stored = app.state.control_db.work_resolution(WORK_ID)
    assert stored is not None and stored.session_key == KEY


@pytest.mark.asyncio
async def test_a_side_chat_writes_its_works_resolution(tmp_path):
    app = _history_deck(tmp_path)
    app.state.app_state.db.bind_session_work(KEY, WORK_ID, "side_chat")

    assert (await _put_as(app, KEY)).status_code == 200


@pytest.mark.asyncio
async def test_a_chat_in_two_works_must_name_one_of_its_own(tmp_path):
    app = _history_deck(tmp_path)
    await _register(app, KEY, _issue_of(WORK_ID))
    await _register(app, KEY, _issue_of(OTHER_WORK))

    unnamed = await _put_as(app, KEY)
    assert unnamed.status_code == 409 and OTHER_WORK in unnamed.json()["detail"]
    stranger = await _put_as(app, KEY, work_id="github_issue:eerovil/agentdeck#12345")
    assert stranger.status_code == 409
    # Named in the body, in any case spelling of the issue.
    named = await _put_as(app, KEY, work_id=OTHER_WORK.replace("eerovil", "EeroVil"))
    assert named.status_code == 200 and named.json()["work_id"] == OTHER_WORK
    assert app.state.control_db.work_resolution(WORK_ID) is None
    # The read has no body, so it names the Work in the query.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        read = await client.get(
            f"/api/sessions/{KEY}/work-resolution?work_id={OTHER_WORK.replace('#', '%23')}"
        )
    assert read.status_code == 200 and read.json()["resolution"]["problem"] == "P"


@pytest.mark.asyncio
async def test_a_run_bound_chat_cannot_name_another_work(tmp_path):
    app = _history_deck(tmp_path)
    app.state.app_state.bind_session_run(KEY, WORK_ID, "run-1")

    response = await _put_as(app, KEY, work_id=OTHER_WORK)

    assert response.status_code == 409
    assert app.state.control_db.work_resolution(OTHER_WORK) is None


@pytest.mark.asyncio
async def test_a_chat_that_only_mentions_an_issue_cannot_write_its_resolution(tmp_path):
    app = _history_deck(tmp_path)
    state = app.state.app_state
    url = "https://github.com/" + _issue_of(WORK_ID).replace("#", "/issues/")
    state.sessions[KEY] = dataclasses.replace(state.sessions[KEY], issue_url=url)

    # The deck does count the mention as membership; it is just not the chat's own act.
    chat = state.chat_identity_key(KEY)
    assert WORK_ID in {ref.work_id for ref in state.work_projection().works_for_chat(chat)}

    response = await _put_as(app, KEY, work_id=WORK_ID)

    assert response.status_code == 409


# --- in the browser ------------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_rename_a_work_on_a_phone_and_back(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_actions import _no_overflow

    app = _deck(tmp_path, monkeypatch)
    app.state.control_db.set_work_resolution(_resolution())
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 640},
                                      service_workers="block")
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        await page.goto(base + work_href(WORK_ID))
        await page.wait_for_load_state("load")  # htmx has wired the form
        # The new name comes back by the page's stream, so it must be connected first.
        title = page.locator("#work-sec-status h1.wd-title")
        form = page.locator("#work-rename")
        head = await title.bounding_box()
        assert await form.is_hidden()
        await page.locator("#work-sec-resolution [data-work-resolution]").wait_for()
        await _no_overflow(page, 320)

        await page.locator("[data-work-rename-open]").click()
        box = form.locator('input[name="title"]')
        await box.wait_for()
        await box.fill("Renamed on a phone")
        await form.locator('button[value="save"]').click()
        await page.locator('#work-rename-result [data-wd-receipt="sent"]').wait_for()
        assert await form.is_hidden()
        # The new name rides back with the receipt (#2864: the page has no stream), not a reload.
        await page.wait_for_function(
            "() => document.querySelector('#work-sec-status h1.wd-title')"
            ".textContent.includes('Renamed on a phone')"
        )
        # Measured until it settles: the swap that brings the name in can still be laying out.
        await page.wait_for_function(
            """(height) => {
                const title = document.querySelector('#work-sec-status h1.wd-title');
                return !!title && title.getBoundingClientRect().height === height;
            }""",
            arg=head["height"],
        )
        await _no_overflow(page, 320)

        await page.goto(base + "/")
        row = page.locator(f'[data-work-id="{WORK_ID}"]').first
        await row.wait_for()
        assert "Renamed on a phone" in await row.text_content()

        await page.goto(base + work_href(WORK_ID))
        await page.wait_for_load_state("load")
        await page.locator("[data-work-rename-open]").click()
        await form.locator('button[value="clear"]').click()
        await page.wait_for_function(
            "() => document.querySelector('#work-sec-status h1.wd-title')"
            ".textContent.includes('From GitHub')"
        )
        assert app.state.control_db.work_titles() == {} and _NoGh.calls == 0
        await browser.close()
