"""#2603: `/chats` is rendered on the server at page load and filtered by its address.

The chat list stopped being the front page (#2219, #2256), so it no longer streams, no
longer filters in the browser and no longer rides beside every chat as a sidebar. These
tests hold the page to that: the cards are in the first response, every filter is a
query parameter the server applies, Load older is a link that keeps the filters, no
stream sends a list, and a chat page carries none.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import pytest

from agentdeck.models import Session, SessionStatus
from agentdeck.web.render import SESSION_HISTORY_PAGE_SIZE, ChatListQuery, _chat_matches

SID1 = "claude_code:test:sid1"


def _add(app, session_id: str, **fields) -> Session:
    fields.setdefault("last_activity", datetime.now(UTC))
    session = Session(
        key=f"claude_code:test:{session_id}",
        account_key="claude_code:test",
        session_id=session_id,
        status=SessionStatus.LIVE,
        **fields,
    )
    app.state.app_state.update_session(session)
    return session


def _card_keys(html: str) -> set[str]:
    body = html.split('<div id="sessions"', 1)[1]
    return {
        chunk.split('"', 1)[0]
        for chunk in body.split('data-session-key="')[1:]
    }


def _query(**params) -> ChatListQuery:
    class Params(dict):
        def getlist(self, name):
            value = self.get(name)
            return list(value) if isinstance(value, (list, tuple)) else ([value] if value else [])

    return ChatListQuery.from_params(Params(params))


# ---- the query itself -------------------------------------------------------------------


def test_the_query_reads_every_filter_and_ignores_what_it_does_not_know():
    query = _query(
        q="  Fix Deploy ",
        done="1",
        closed="0",
        account=["claude_code:a", "claude_code:a", ""],
        project=["agentdeck"],
        source=["kanban", "robot"],
        history_limit="80",
    )
    assert query.text == "Fix Deploy"
    assert query.hide_closed
    assert not hasattr(query, "show_done")  # nothing is marked done any more (#2599)
    assert query.accounts == ("claude_code:a",)  # repeats and blanks dropped
    assert query.projects == ("agentdeck",)
    assert query.sources == ("kanban",)  # an unknown source is not a filter
    assert query.history_limit == 80
    assert query.panel_count == 3
    assert query.narrowing

    default = _query(done="yes", closed="1", history_limit="ten")
    assert not default.hide_closed
    assert default.history_limit == SESSION_HISTORY_PAGE_SIZE
    assert not default.narrowing
    assert default.url() == "/chats"
    assert _query(history_limit="3").history_limit == SESSION_HISTORY_PAGE_SIZE


def test_the_query_round_trips_through_its_own_address():
    query = _query(q="a&b c", closed="0", project=["x", "y"], source=["you"])
    url = query.url(history_limit=80)
    parsed = urlparse(url)
    assert parsed.path == "/chats"
    pairs = parse_qsl(parsed.query)
    assert ("q", "a&b c") in pairs and ("history_limit", "80") in pairs
    assert [value for name, value in pairs if name == "project"] == ["x", "y"]
    again = ChatListQuery.from_params(
        SimpleNamespace(
            get=lambda name, default=None: dict(pairs).get(name, default),
            getlist=lambda name: [value for key, value in pairs if key == name],
        )
    )
    assert again == replace(query, history_limit=80)


def test_matching_follows_the_rules_the_browser_used_to_apply():
    session = Session(
        key="claude_code:test:x",
        account_key="claude_code:test",
        session_id="x",
        status=SessionStatus.IDLE,
        title="Deploy the CANARY",
        worker_type="kanban",
    )
    child = replace(session, key="claude_code:test:child", title="Sub-agent ÄÖ Review")
    done = {session.key: SimpleNamespace(state="done")}
    closed = {session.key: SimpleNamespace(is_open=False)}

    def matches(query, **kwargs):
        context = {"children": (), "badge": "main", "deckhand_status": {}, "work_item_pill": {}}
        context.update(kwargs)
        return _chat_matches(session, query, **context)

    assert matches(_query(q="canary"))  # case folded
    assert matches(_query(q="äö review"), children=(child,))  # a sub-agent row's title
    assert matches(_query(q="main"))  # the account badge
    assert not matches(_query(q="canary deploy"))  # one substring, not separate words
    assert matches(_query(source=["kanban"]))
    assert not matches(_query(source=["you"]))
    assert not matches(_query(account=["claude_code:other"]))
    # Nothing is marked done any more (#2599), so no Deckhand state hides a row.
    assert matches(_query(), deckhand_status=done)
    # Hide closed reads the work item's own open state; a row with none is never hidden.
    assert matches(_query(), work_item_pill=closed)
    assert not matches(_query(closed="0"), work_item_pill=closed)
    assert matches(_query(closed="0"))


# ---- the page --------------------------------------------------------------------------


async def test_chats_renders_its_cards_at_load_with_no_list_stream(tmp_path):
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        page = await client.get("/chats")
        old_link = await client.get("/chats?chats=1")
        front = await client.get("/?chats=1", follow_redirects=False)

    assert page.status_code == 200
    assert SID1 in _card_keys(page.text)
    assert "Hello World Session" in page.text
    # The page's stream is the topbar's alone, and nothing on it swaps a list.
    assert 'sse-connect="/events"' in page.text
    assert 'sse-swap="sessions"' not in page.text
    assert "chat_list.js" not in page.text and "session_filters.js" not in page.text
    # An old `?chats=1` address is just the page, and `/?chats=1` still lands here.
    assert SID1 in _card_keys(old_link.text)
    assert front.status_code == 307 and front.headers["location"] == "/chats"


async def test_every_filter_is_applied_by_the_server(tmp_path):
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path)
    mine = _add(app, "mine", title="Tidy the docs")
    worker = _add(app, "worker", title="Kanban chore", worker_type="kanban")
    # A chat with an issue would belong to that issue's Work, so the project is its cwd.
    elsewhere = _add(app, "elsewhere", title="Other project", cwd=Path("/srv/bazaar"))
    assert elsewhere.project_name == "bazaar"

    async with _client(app) as client:
        text = await client.get("/chats", params={"q": "TIDY"})
        source = await client.get("/chats", params={"source": "kanban"})
        project = await client.get("/chats", params={"project": "bazaar"})
        nothing = await client.get("/chats", params={"q": "no chat says this"})

    assert _card_keys(text.text) == {mine.key}
    assert 'value="TIDY"' in text.text  # the box shows what filtered the list
    assert _card_keys(source.text) == {worker.key}
    assert 'name="source" value="kanban" checked' in source.text
    assert _card_keys(project.text) == {elsewhere.key}
    # The project choices come from the whole list, so the others stay offered.
    assert 'name="project" value="bazaar" checked' in project.text
    assert "no matching chats" in nothing.text
    assert 'href="/chats?q=">Clear</a>' in nothing.text


async def test_load_older_is_a_link_that_keeps_the_filters(tmp_path):
    from test_web import _add_large_session_history, _app_with_state, _client

    app = _app_with_state(tmp_path)
    _add_large_session_history(app)
    async with _client(app) as client:
        first = await client.get("/chats", params={"source": "you"})
        link = first.text.split('class="session-history-more" href="', 1)[1].split('"', 1)[0]
        link = link.replace("&amp;", "&")
        more = await client.get(link)

    pairs = parse_qsl(urlparse(link).query)
    limit = dict(pairs)["history_limit"]
    assert ("source", "you") in pairs and int(limit) > SESSION_HISTORY_PAGE_SIZE
    assert len(_card_keys(more.text)) > len(_card_keys(first.text))
    assert f'data-history-limit="{limit}"' in more.text
    assert 'value="you" checked' in more.text


async def test_a_chat_page_carries_no_chat_list(tmp_path):
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        page = await client.get(f"/sessions/{SID1}")
        in_page = await client.get(
            f"/sessions/{SID1}", headers={"X-AgentDeck-Session-Navigation": "detail"}
        )

    assert page.status_code == 200
    sidebar = page.text.split('<aside class="session-sidebar"', 1)[1].split("</aside>", 1)[0]
    assert "data-list-empty" in sidebar and 'data-list-path="/"' in sidebar
    assert "a.session" not in sidebar and 'id="sessions"' not in page.text
    assert 'id="new-chat' not in sidebar
    assert "chats=1" not in page.text
    assert '<aside class="session-sidebar"' not in in_page.text


# ---- in a browser ----------------------------------------------------------------------


@pytest.mark.browser
async def test_typing_a_filter_and_pressing_enter_loads_the_filtered_page(tmp_path):
    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path)
    tidy = _add(app, "tidy", title="Tidy the docs")
    worker = _add(app, "worker", title="Kanban chore", worker_type="kanban")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(base + "/chats")
        await page.locator(f'#sessions a.session[data-session-key="{SID1}"]').wait_for()

        await page.locator("#q").fill("tidy")
        await page.locator("#q").press("Enter")
        await page.wait_for_url("**/chats?q=tidy")
        assert await page.locator("#q").input_value() == "tidy"
        keys = await page.locator("#sessions a.session").evaluate_all(
            "cards => cards.map(card => card.dataset.sessionKey)"
        )
        assert keys == [tidy.key]
        # The server-side deep search was removed (#2864); the filter is the search.
        assert await page.locator("#deep-search").count() == 0

        # A ticked filter applies at once, with the text kept.
        await page.locator("#more-filters").click()
        await page.locator('label:has(input[name="source"][value="kanban"])').click()
        await page.wait_for_function("location.search.indexOf('source=kanban') !== -1")
        await page.locator("#q").wait_for()
        assert "q=tidy" in page.url
        assert await page.locator("#sessions a.session").count() == 0
        assert "no matching chats" in await page.locator("#sessions").inner_text()

        # A bare /chats reopens with this device's last filters (#3367) ...
        await page.goto(base + "/chats")
        await page.wait_for_url("**/chats?q=tidy&source=kanban")
        assert await page.locator("#q").input_value() == "tidy"

        # ... until Clear, which shows the whole list and forgets them.
        await page.locator("a.filter-clear").click()
        await page.wait_for_url(base + "/chats?q=")
        await page.locator(f'#sessions a.session[data-session-key="{worker.key}"]').wait_for()
        await page.goto(base + "/chats")
        await page.locator(f'#sessions a.session[data-session-key="{worker.key}"]').wait_for()
        assert page.url == base + "/chats"

        # "Guess for me" switched off: filters are neither recorded nor reapplied.
        app.state.db.record_guessing_enabled(False)
        await page.goto(base + "/chats?q=tidy")
        await page.wait_for_function("document.readyState === 'complete'")
        await page.goto(base + "/chats")
        await page.wait_for_function("document.readyState === 'complete'")
        assert page.url == base + "/chats"
        assert await page.evaluate("localStorage.getItem('agentdeck:chats:filters')") == ""
        await browser.close()


@pytest.mark.browser
async def test_the_topbar_reload_brings_a_filtered_list_up_to_date(tmp_path):
    """D16 on #2603: nothing streams the list, so the page's own reload button (#2240)
    is how it catches up — and it keeps the filters, because they are the address."""
    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path)
    _add(app, "first", title="Deploy one", worker_type="kanban")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(base + "/chats?source=kanban&q=deploy")
        await page.locator("#sessions a.session").first.wait_for()
        assert await page.locator("#sessions a.session").count() == 1

        later = _add(app, "later", title="Deploy two", worker_type="kanban")
        _add(app, "noise", title="Deploy by hand")  # not kanban: stays filtered out
        await page.locator("[data-page-refresh]").click()
        await page.locator(f'#sessions a.session[data-session-key="{later.key}"]').wait_for()
        assert await page.locator("#sessions a.session").count() == 2
        assert await page.locator("#q").input_value() == "deploy"
        assert "source=kanban" in page.url
        await browser.close()


@pytest.mark.browser
async def test_cards_still_expand_and_open_their_sub_agents(tmp_path):
    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path)
    parent = _add(app, "parent", title="Parent chat", last_prompt="the question",
                  last_text="**the** answer")
    kid = _add(app, "kid", title="Child agent", is_delegated=True)
    app.state.app_state.delegation_parents[kid.key] = parent.key

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 900})
        await page.goto(base + "/chats")
        card = page.locator(f'#sessions a.session[data-session-key="{parent.key}"]')
        await card.wait_for()
        await card.locator(".expand-btn").click()
        assert "expanded" in (await card.get_attribute("class"))
        assert await card.locator(".card-latest strong").inner_text() == "the"
        assert page.url == base + "/chats"  # the press did not open the chat

        rows = page.locator(f'.subagent-children[data-subagents-of="{parent.key}"]')
        assert "collapsed" in (await rows.get_attribute("class"))
        await card.locator(".subagent-toggle").click()
        assert "collapsed" not in (await rows.get_attribute("class"))
        await browser.close()


@pytest.mark.browser
async def test_back_from_a_chat_opened_directly_on_a_phone_is_the_front_page(tmp_path):
    """The chat page holds no list (#2603), so Back loads the Works for real; a chat opened
    from the chats page still slides back to that same list."""
    from urllib.parse import urlparse

    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path, with_transcript=True)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        phone = await browser.new_page(viewport={"width": 390, "height": 844})
        await phone.goto(f"{base}/sessions/{SID1}")
        await phone.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        await phone.locator("a.back").click()
        await phone.wait_for_function(
            "location.pathname === '/' && !!document.querySelector('#front-needs')"
        )
        assert await phone.locator(".session-detail").count() == 0

        await phone.goto(base + "/chats?q=hello")
        card = phone.locator(f'#sessions a.session[data-session-key="{SID1}"]')
        await card.click()
        await phone.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        await phone.evaluate("window.__sameDocument = true")
        await phone.locator("a.back").click()
        await phone.wait_for_function("document.body.classList.contains('mobile-list-open')")
        assert urlparse(phone.url).path == "/chats"
        assert urlparse(phone.url).query == "q=hello"
        assert await phone.evaluate("window.__sameDocument === true")
        assert await phone.locator("#q").input_value() == "hello"
        await browser.close()


@pytest.mark.browser
async def test_a_desktop_chat_page_mounts_the_works_beside_it_and_back_is_the_works(
    tmp_path,
):
    from urllib.parse import urlparse

    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path, with_transcript=True)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        desk = await browser.new_page(viewport={"width": 1280, "height": 900})
        await desk.goto(f"{base}/sessions/{SID1}")
        await desk.locator(".front-columns > .session-sidebar #front-needs").wait_for()
        sidebar = desk.locator(".session-layout > .session-sidebar")
        assert await sidebar.get_attribute("data-list-empty") is None
        assert await desk.locator("#sessions").count() == 0
        await desk.go_back()
        await desk.wait_for_function("!document.body.classList.contains('session-page')")
        assert urlparse(desk.url).path == "/"
        assert await desk.locator("#front-needs").is_visible()
        await browser.close()
