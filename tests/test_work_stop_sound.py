"""A short chime when a Work stops (#2742).

The front page plays two notes when a live push brings a new, unseen row into Recently
stopped (#2283). The browser's audio is a stub that counts notes, so these tests read how
many chimes the page asked for, through the rendered page and its real SSE swaps.
"""

from __future__ import annotations

import pytest

# Starts suspended, like a real AudioContext before the page has been clicked; `resume()`
# runs it. Each chime is two notes, so `__chimes` counts note pairs.
AUDIO_STUB = """
(() => {
  window.__notes = 0;
  window.__chimes = () => window.__notes / 2;
  class FakeParam { setValueAtTime() {} exponentialRampToValueAtTime() {} }
  class FakeContext {
    constructor() { this.state = 'suspended'; this.currentTime = 0; this.destination = {}; }
    resume() { this.state = 'running'; return Promise.resolve(); }
    createOscillator() {
      return { frequency: {}, connect() {}, start() { window.__notes += 1; }, stop() {} };
    }
    createGain() { return { gain: new FakeParam(), connect() {} }; }
  }
  window.AudioContext = FakeContext;
  document.addEventListener('htmx:sseOpen', () => { window.__sseOpen = true; }, true);
})();
"""


async def _chimes(page) -> float:
    return await page.evaluate("() => window.__chimes()")


async def _use(page, *others):
    """Click `page` and wait until it has taken the chime over from `others`.

    The click starts a Web Lock steal that the browser grants a moment later; a push in that
    gap would chime in no tab at all.
    """
    await page.locator("body").click(position={"x": 5, "y": 5})
    await page.wait_for_function("() => window.AgentDeckStopSound.leading()")
    for other in others:
        await other.wait_for_function("() => !window.AgentDeckStopSound.leading()")


async def _open(context, base):
    page = await context.new_page()
    await page.goto(base + "/")
    await page.wait_for_function("() => window.__sseOpen === true")
    return page


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_new_stop_chimes_once_and_only_when_it_may(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_stops import _deck, _pretend_it_left
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    service = app.state.work_overview
    await service.refresh()
    row = f'#works-stopped [data-work-id="{WORK.work_id}"]'

    async def stop_again(*pages):
        """The Work stops again; wait until every page holds the new stop."""
        before = [await p.get_attribute(row, "data-work-stopped") for p in pages]
        _pretend_it_left(app, WORK.work_id)
        await service.refresh()
        for p, old in zip(pages, before, strict=True):
            await p.wait_for_function(
                """([sel, old]) => {
                    const el = document.querySelector(sel);
                    return !!el && el.hasAttribute('data-work-unseen')
                        && el.getAttribute('data-work-stopped') !== old;
                }""",
                arg=[row, old],
            )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(AUDIO_STUB)
        page = await _open(context, base)
        await page.wait_for_selector(row, state="attached")
        assert await page.get_attribute(row, "data-work-stopped")

        # A stop already on the page when it loaded never chimes.
        assert await _chimes(page) == 0
        # Before any click the browser allows no sound, so a stop is skipped, not queued.
        await stop_again(page)
        assert await _chimes(page) == 0

        await _use(page)
        await stop_again(page)
        assert await _chimes(page) == 1
        # The same rows pushed again are not a new stop.
        await service.refresh()
        await page.wait_for_timeout(300)
        assert await _chimes(page) == 1

        # A Work in a hidden project stays quiet.
        project = await page.get_attribute(row, "data-work-project")
        assert project
        await page.evaluate(
            "(p) => localStorage.setItem("
            "'agentdeck-front-hidden-projects', JSON.stringify({[p]: true}))",
            project,
        )
        await stop_again(page)
        assert await _chimes(page) == 1
        await page.evaluate("() => localStorage.removeItem('agentdeck-front-hidden-projects')")

        # Switched off on this device: quiet.
        await page.evaluate("() => localStorage.setItem('agentdeck-stop-sound', 'off')")
        await stop_again(page)
        assert await _chimes(page) == 1
        await page.evaluate("() => localStorage.removeItem('agentdeck-stop-sound')")

        # Two open tabs share one chime, from the tab used last.
        other = await _open(context, base)
        await _use(other, page)
        await stop_again(page, other)
        await page.wait_for_timeout(300)
        assert (await _chimes(page), await _chimes(other)) == (1, 1)
        # Using the first tab again hands the chime back to it.
        await _use(page, other)
        await stop_again(page, other)
        await page.wait_for_timeout(300)
        assert (await _chimes(page), await _chimes(other)) == (2, 1)
        # A click on a page that cannot chime (here /settings) does not take it away.
        settings = await context.new_page()
        await settings.goto(base + "/settings")
        await settings.locator("h1").click()
        await stop_again(page, other)
        await page.wait_for_timeout(300)
        assert (await _chimes(page), await _chimes(other)) == (3, 1)
        assert await _chimes(settings) == 0
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_starred_works_stop_chimes_from_the_starred_list(tmp_path, monkeypatch):
    """#4121: Starred is the only list a starred Work is in, so its stops chime from there."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_stops import _deck, _pretend_it_left
    from test_work_ui import WORK

    app = _deck(tmp_path, monkeypatch)
    app.state.control_db.star_work(WORK.work_id)
    service = app.state.work_overview
    await service.refresh()
    row = f'#works-starred [data-work-id="{WORK.work_id}"]'

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(AUDIO_STUB)
        page = await _open(context, base)
        await page.wait_for_selector(row, state="attached")
        assert await page.locator(f'#works-stopped [data-work-id="{WORK.work_id}"]').count() == 0
        await _use(page)
        old = await page.get_attribute(row, "data-work-stopped")
        _pretend_it_left(app, WORK.work_id)
        await service.refresh()
        await page.wait_for_function(
            """([sel, old]) => {
                const el = document.querySelector(sel);
                return !!el && el.hasAttribute('data-work-unseen')
                    && el.getAttribute('data-work-stopped') !== old;
            }""",
            arg=[row, old],
        )
        assert await _chimes(page) == 1
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_star_on_a_stop_past_recently_stoppeds_cap_is_no_new_stop(tmp_path, monkeypatch):
    """#4121: a star moves an unseen row into Starred with the stop it already had, even one
    Recently stopped had no room for; only the Work's next stop chimes."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_stops import _deck, _pretend_it_left
    from test_work_ui import WORK

    from agentdeck import work_story

    monkeypatch.setattr(work_story, "MAX_STOPPED", 0)
    app = _deck(tmp_path, monkeypatch)
    service = app.state.work_overview
    await service.refresh()
    needs = f'article[data-work-id="{WORK.work_id}"]'  # in the Needs you group it stopped for
    row = f'#works-starred [data-work-id="{WORK.work_id}"]'
    newer = """([sel, old]) => {
        const el = document.querySelector(sel);
        return !!el && el.hasAttribute('data-work-unseen')
            && el.getAttribute('data-work-stopped') !== old;
    }"""

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(AUDIO_STUB)
        page = await _open(context, base)
        await page.wait_for_selector(needs, state="attached")
        await _use(page)

        # A stop that came after the page loaded, so only the push put it in Needs answer.
        old = await page.get_attribute(needs, "data-work-stopped")
        _pretend_it_left(app, WORK.work_id)
        await service.refresh()
        await page.wait_for_function(newer, arg=[needs, old])
        stop = await page.get_attribute(needs, "data-work-stopped")

        app.state.control_db.star_work(WORK.work_id)
        await service.refresh()
        await page.wait_for_selector(row, state="attached")
        assert await page.get_attribute(row, "data-work-stopped") == stop
        assert await _chimes(page) == 0

        _pretend_it_left(app, WORK.work_id)
        await service.refresh()
        await page.wait_for_function(newer, arg=[row, stop])
        assert await _chimes(page) == 1
        await browser.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_settings_switch_is_kept_per_device_and_previews_the_chime(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_stops import _deck

    app = _deck(tmp_path, monkeypatch)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        await context.add_init_script(AUDIO_STUB)
        page = await context.new_page()
        await page.goto(base + "/settings")
        box = page.locator("[data-stop-sound-setting]")
        await box.wait_for()
        assert await box.is_checked()

        await box.click()
        assert not await box.is_checked()
        assert await page.evaluate("() => localStorage.getItem('agentdeck-stop-sound')") == "off"
        assert await _chimes(page) == 0

        await box.click()
        assert await page.evaluate("() => localStorage.getItem('agentdeck-stop-sound')") == "on"
        await page.wait_for_function("() => window.__chimes() === 1")

        await page.reload()
        await box.wait_for()
        assert await box.is_checked()
        await page.evaluate("() => localStorage.setItem('agentdeck-stop-sound', 'off')")
        await page.reload()
        await box.wait_for()
        assert not await box.is_checked()
        await browser.close()
