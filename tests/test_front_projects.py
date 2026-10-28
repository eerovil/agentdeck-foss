"""Show only a project's Works on the front page (#2311), one tab per project (#3226).

A Work's project is the Poller instance whose manifest declares its repository, so one
instance's several repositories are one project, and otherwise the repository's own name.
Hiding is the reader's own choice on this browser: nothing is written to the deck.
"""

from __future__ import annotations

import json
import re

import pytest

from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.work import WorkRef
from agentdeck.work_overview import work_href, work_project

DECK = WorkRef("eerovil/agentdeck", 2147)
BAZAAR = WorkRef("acmecorp/bazaar", 88)
STORE = WorkRef("Northwind/store", 5)


def _scopes() -> tuple[BoardScope, ...]:
    return (
        BoardScope("agentdeck", org="eerovil", issue_repositories=("eerovil/agentdeck",)),
        BoardScope("bazaar", org="acmecorp", issue_repositories=("acmecorp/bazaar",)),
        BoardScope(
            "trailhead",
            org="Northwind",
            issue_repositories=("Northwind/store", "Northwind/issues"),
        ),
    )


def test_a_works_project_is_the_instance_that_declares_its_repository():
    board = BoardView(scopes=_scopes())

    assert work_project("eerovil/agentdeck", board) == "agentdeck"
    # One instance's several repositories are one project, however the owner is spelled.
    assert work_project("northwind/STORE", board) == "trailhead"
    assert work_project("Northwind/issues", board) == "trailhead"
    # A repository no instance declares is its own project; the same name under another
    # owner is not the instance's.
    assert work_project("eerovil/homr", board) == "homr"
    assert work_project("someone/bazaar", board) == "bazaar"
    assert work_project("eerovil/homr", BoardView()) == "homr"
    assert work_project("eerovil/homr", None) == "homr"
    assert work_project("", board) == ""


def test_the_counts_line_is_counted_per_project_too(tmp_path):
    from dataclasses import replace

    from agentdeck.work_overview import WorkAttention, build_work_card
    from agentdeck.work_story import front_row, front_sections

    asking = WorkAttention(
        "claude_code:main:worker", "Worker chat", "waiting", "/chats/w",
        asks=True, question="Which branch?",
    )
    question = build_work_card(DECK, issue=None, title="Work", attention=(asking,))
    rows = [
        front_row(replace(question, project="agentdeck"), current=True),
        front_row(replace(question, work_id="github_issue:acmecorp/bazaar#88", project="bazaar"),
                  current=True),
        front_row(replace(question, work_id="github_issue:acmecorp/bazaar#89", project="bazaar"),
                  current=True),
    ]
    front = front_sections(rows)

    assert ("answer", 3, "questions") in front.summary
    assert front.summary_by_project["answer"] == {"agentdeck": 1, "bazaar": 2}
    assert front.summary_plurals["answer"] == ("question", "questions")


def _deck(tmp_path):
    from datetime import timedelta

    from test_work_ui import NOW, _add, _app

    app = _app(tmp_path, board=BoardView(scopes=_scopes()))
    # Each project last active at its own time, so the tabs are not in name order (#3399).
    for index, work in enumerate((DECK, BAZAAR, STORE)):
        _add(app, f"claude_code:test:worker{index}", work, thinking=True,
             last_activity=NOW - timedelta(minutes=30 - 10 * index))
    return app


def _row(html: str, work: WorkRef) -> str:
    # A Work's id is spelled in lower case, whatever its owner's own spelling.
    return html.split(f'data-work-id="{work.work_id.lower()}"', 1)[1].split(">", 1)[0]


def test_each_row_names_its_project_and_the_tabs_start_unbuilt(tmp_path):
    from test_work_ui import _client

    html = _client(_deck(tmp_path)).get("/").text

    assert 'data-work-project="agentdeck"' in _row(html, DECK)
    assert 'data-work-project="bazaar"' in _row(html, BAZAAR)
    assert 'data-work-project="trailhead"' in _row(html, STORE)
    running = re.search(r'data-front-summary="running" data-by-project=\'([^\']*)\'', html)
    assert running and json.loads(running.group(1)) == {"agentdeck": 1, "trailhead": 1, "bazaar": 1}
    # The tab line is empty until the script builds it, and outside every part a push replaces.
    tabs = html.split('id="front-tabs"', 1)[1].split("</nav>", 1)[0]
    assert tabs.rstrip().endswith("hidden>") and "sse-swap" not in tabs
    assert "data-projects-toggle" not in html
    # Each row carries the time its age shows, which orders the tabs (#3399).
    for work in (DECK, BAZAAR, STORE):
        article = html.split(f'data-work-id="{work.work_id.lower()}"', 1)[1]
        article = article.split("</article>", 1)[0]
        active = re.search(r'data-work-active="([^"]+)"', article)
        assert active and f'class="fr-age" datetime="{active.group(1)}"' in article


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_hidden_project_stays_hidden_through_a_push_and_a_reload(tmp_path):
    from datetime import timedelta

    from conftest import async_playwright
    from test_web import _live_server
    from test_work_ui import NOW, _add

    app = _deck(tmp_path)
    await app.state.work_overview.refresh()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844},
                                      service_workers="block", has_touch=True)
        await page.add_init_script(
            "document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true)"
        )
        # Every frame from the very first: how many rows of a hidden project were painted.
        # The filter ran deferred, after the first paint, so each reload drew them and then
        # hid them (#3108).
        await page.add_init_script("""(() => {
          window.__paintedHidden = 0;
          function frame() {
            const stored = localStorage.getItem('agentdeck-front-hidden-projects');
            const hidden = JSON.parse(stored || '{}');
            document.querySelectorAll('[data-work-project]').forEach(row => {
              const painted = row.getClientRects().length;
              if (hidden[row.dataset.workProject] && painted) window.__paintedHidden++;
            });
            if (document.readyState !== 'complete') requestAnimationFrame(frame);
          }
          requestAnimationFrame(frame);
        })()""")
        await page.goto(base + "/")
        await page.wait_for_function("() => window.__sseOpen === true")
        bazaar_row = page.locator(f'#works [data-work-id="{BAZAAR.work_id}"]')
        deck_row = page.locator(f'#works [data-work-id="{DECK.work_id}"]')
        await bazaar_row.wait_for()
        tabs = page.locator("#front-tabs [data-project-tab]")
        hidden = "() => JSON.parse(localStorage.getItem('agentdeck-front-hidden-projects'))"
        working = page.locator('[data-front-group="working"] .fr-group-count')
        running = page.locator('[data-front-summary="running"] b')
        # The project with the newest activity first; All last (#3399).
        order = "els => els.map(e => e.dataset.projectTab)"
        names = ["trailhead", "bazaar", "agentdeck"]
        assert await tabs.evaluate_all(order) == [*names, ""]
        assert await tabs.last.get_attribute("aria-pressed") == "true"
        assert await page.locator("#front-tabs").evaluate(
            "el => getComputedStyle(el).position") == "sticky"

        # A tap shows only that project.
        await page.locator('[data-project-tab="agentdeck"]').click()
        await bazaar_row.wait_for(state="hidden")
        assert await deck_row.is_visible()
        assert await page.evaluate(hidden) == {"trailhead": True, "bazaar": True}
        assert await page.locator('[data-project-tab="agentdeck"]').get_attribute(
            "aria-pressed") == "true"
        assert await tabs.last.get_attribute("aria-pressed") == "false"
        assert await working.inner_text() == "1"
        assert await running.inner_text() == "1"

        # A push that brings a new bazaar Work in hides it before anyone sees it, and leaves the
        # tabs where they were though bazaar is now the newest.
        more = WorkRef("acmecorp/bazaar", 90)
        _add(app, "claude_code:test:worker9", more, thinking=True,
             last_activity=NOW - timedelta(minutes=1))
        await app.state.work_overview.refresh()
        fresh_row = page.locator(f'#works [data-work-id="{more.work_id}"]')
        await fresh_row.wait_for(state="attached")
        assert not await fresh_row.is_visible()
        await page.wait_for_function(
            "() => document.querySelector('[data-front-summary=\"running\"] b')"
            "?.textContent === '1'"
        )
        assert await working.inner_text() == "1"
        assert await tabs.evaluate_all(order) == [*names, ""]

        # A reload sorts them again.
        await page.reload()
        await deck_row.wait_for()
        assert not await bazaar_row.is_visible()
        await page.wait_for_load_state("load")
        assert await page.evaluate("window.__paintedHidden") == 0
        assert await working.inner_text() == "1"
        names = ["bazaar", "trailhead", "agentdeck"]
        assert await tabs.evaluate_all(order) == [*names, ""]

        # A hold adds a project to what is shown, and the tap that ends it does nothing more.
        bazaar_tab = page.locator('[data-project-tab="bazaar"]')
        box = await bazaar_tab.bounding_box()
        await page.mouse.move(box["x"] + 5, box["y"] + 5)
        await page.mouse.down()
        await page.wait_for_timeout(600)
        await page.mouse.up()
        await bazaar_row.wait_for(state="visible")
        assert await page.evaluate(hidden) == {"trailhead": True}
        assert await working.inner_text() == "3"

        # All shows every project again.
        await tabs.last.click()
        await page.wait_for_function("() => localStorage.getItem("
                                     "'agentdeck-front-hidden-projects') === '{}'")
        assert await working.inner_text() == "4"
        assert await running.inner_text() == "4"

        # A sideways swipe on the list steps from All, the last tab, round to the first.
        await deck_row.evaluate("""row => {
          const at = x => new Touch({identifier: 1, target: row, clientX: x, clientY: 300});
          row.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, touches: [at(300)],
                                                          changedTouches: [at(300)]}));
          row.dispatchEvent(new TouchEvent('touchend', {bubbles: true, touches: [],
                                                        changedTouches: [at(150)]}));
        }""")
        first = names[0]
        assert await page.evaluate(hidden) == {n: True for n in names if n != first}
        await tabs.last.click()

        # A push that makes agentdeck the newest leaves the tabs where they were, and so does
        # opening a chat over the list.
        active = await deck_row.get_attribute("data-work-active")
        _add(app, "claude_code:test:worker0", DECK, thinking=True,
             last_activity=NOW - timedelta(seconds=30))
        await app.state.work_overview.refresh()
        await page.wait_for_function(
            "([id, old]) => document.querySelector(`#works [data-work-id=\"${id}\"]`)"
            "?.dataset.workActive !== old", arg=[DECK.work_id, active])
        assert await tabs.evaluate_all(order) == [*names, ""]

        # A desktop Work page mounts the front page beside itself after loading; its project
        # tabs and its folds work there too (#3114).
        await page.locator('[data-project-tab="agentdeck"]').click()
        # On a phone a chat slides over the front page, and the pinned tabs stay under it.
        await deck_row.locator("a.fr-body").click()
        await page.locator(".session-detail").wait_for()
        await page.wait_for_function("() => !document.body.classList.contains('mobile-list-open')")
        await page.wait_for_timeout(250)
        assert await page.evaluate("""() => {
          // The list is inert under a chat, which hit-testing skips; paint order is the question.
          document.querySelector('.session-sidebar').inert = false;
          const box = document.getElementById('front-tabs').getBoundingClientRect();
          const hit = document.elementFromPoint(box.left + 30, box.top + box.height / 2);
          return !hit || !hit.closest('#front-tabs');
        }""")
        assert await tabs.evaluate_all(order) == [*names, ""]
        await page.set_viewport_size({"width": 1440, "height": 900})
        await page.goto(base + work_href(DECK.work_id))
        side = page.locator(".front-columns > .session-sidebar")
        bazaar_row = side.locator(f'[data-work-id="{BAZAAR.work_id}"]')
        await side.locator(f'[data-work-id="{DECK.work_id}"]').wait_for()
        assert not await bazaar_row.is_visible()
        await page.wait_for_function(
            "() => document.querySelector('.session-sidebar [data-front-group=\"working\"]"
            " .fr-group-count')?.textContent === '1'"
        )
        await side.locator('[data-project-tab=""]').click()
        await bazaar_row.wait_for(state="visible")
        # A push that redraws the header between the press and the release still folds the
        # group the press started on: the browser clicks neither header (#3432).
        head = side.locator('[data-front-group="working"]')
        box = await head.bounding_box()
        await page.mouse.move(box["x"] + 40, box["y"] + box["height"] / 2)
        await page.mouse.down()
        pressed = await head.element_handle()
        _add(app, "claude_code:test:worker11", WorkRef("eerovil/agentdeck", 2148), thinking=True,
             last_activity=NOW - timedelta(minutes=40))
        await app.state.work_overview.refresh()
        await page.wait_for_function("head => !head.isConnected", arg=pressed)
        await page.mouse.up()
        await bazaar_row.wait_for(state="hidden")
        await head.click()
        await bazaar_row.wait_for(state="visible")
        await head.click()
        await bazaar_row.wait_for(state="hidden")

        # A project new to the page sorts the tabs again; once its last row leaves while it is
        # hidden, its tab stays but goes behind the projects with rows.
        side_tabs = side.locator("[data-project-tab]")
        homr = WorkRef("eerovil/homr", 7)
        _add(app, "claude_code:test:worker10", homr, thinking=True, last_activity=NOW)
        await app.state.work_overview.refresh()
        homr_row = side.locator(f'[data-work-id="{homr.work_id}"]')
        await homr_row.wait_for(state="attached")
        assert (await side_tabs.evaluate_all(order))[0] == "homr"
        await side.locator('[data-project-tab="agentdeck"]').click()
        app.state.app_state.set_issues("claude_code:test:worker10", ())
        await app.state.work_overview.refresh()
        await homr_row.wait_for(state="detached")
        assert (await side_tabs.evaluate_all(order))[-2:] == ["homr", ""]
        await browser.close()
