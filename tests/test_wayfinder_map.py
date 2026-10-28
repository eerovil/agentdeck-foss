"""A wayfinder map drawn as a diagram on its Work page (#3627)."""

from __future__ import annotations

import pytest

from agentdeck import wayfinder_map as wm

BODY = """## Destination

A spec for the [chat store](https://x) move, approved.

## Not yet specified

- How a restore works while chats run
- <a question you can tell is coming>

```markdown
## Not yet specified
- inside a fence, not fog
```

## Out of scope

- [Old ticket](https://x) — past it
"""


def ticket(key: str, state: str = wm.WAITING, *blocked_by: str) -> wm.Ticket:
    return wm.Ticket(key, f"#{key}", f"Ticket {key}", f"/works/{key}", state, blocked_by=blocked_by)


def test_tickets_stand_as_trees_down_their_arrows_then_loose_ones_fog_and_destination():
    assert wm.fog(BODY) == ("How a restore works while chats run",)
    assert wm.destination(BODY) == "A spec for the chat store move, approved."
    assert wm.fog("") == () and wm.destination("## Destination\n\n<what reaching…>") == ""

    tickets = [
        ticket("1", wm.DONE),
        ticket("2", wm.DONE),
        ticket("3", wm.RUNNING, "1"),
        ticket("4", wm.NEEDS_YOU, "1", "2"),
        ticket("5", wm.WAITING, "4", "3", "gone"),
        ticket("6", wm.WAITING, "7"),
        ticket("7", wm.WAITING, "6"),  # a loop still lays out
        ticket("10"),  # no arrow at all: apart from the trees, in issue-number order (#3694)
        ticket("9"),
    ]
    assert wm.depths(tickets) == {
        "1": 0,
        "2": 0,
        "3": 1,
        "4": 1,
        "5": 2,
        "6": 1,
        "7": 0,
        "10": 0,
        "9": 0,
    }
    diagram = wm.layout(tickets, fog_items=wm.fog(BODY), goal=wm.destination(BODY))
    box = {item.key: item for item in diagram.boxes}
    assert box["1"].y == box["2"].y < box["3"].y == box["4"].y < box["5"].y
    # Each blocked box stands under the blocker it hangs from (the deepest, then the lowest
    # number); sibling subtrees stand side by side, the trees in issue-number order.
    assert box["1"].cx == (box["3"].cx + box["4"].cx) / 2 and box["5"].cx == box["3"].cx
    assert box["4"].x < box["2"].x < box["7"].x and box["6"].cx == box["7"].cx
    assert box["5"].y < box["9"].y == box["10"].y and box["9"].x < box["10"].x
    tickets_only = [item for item in diagram.boxes if item.href]
    assert not any(
        a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h
        for index, a in enumerate(tickets_only)
        for b in tickets_only[index + 1 :]
    )
    [fog] = wm.fog_keys(wm.fog(BODY))
    assert box["9"].y < box[fog].y < box["destination"].y
    assert diagram.boxes[-1].key == "destination" and not box["destination"].href
    assert box["4"].href == "/works/4" and box[fog].state == wm.FOG
    # One arrow per blocker on the map, top to bottom; the loop's back edge is not drawn.
    assert len(diagram.arrows) == 6
    assert [state for state, _word, _n in diagram.legend] == [
        wm.NEEDS_YOU,
        wm.RUNNING,
        wm.WAITING,
        wm.DONE,
        wm.FOG,
    ]
    # Every box fits the drawing, the trees four columns wide.
    assert all(0 < item.x and item.x + item.w <= diagram.width for item in diagram.boxes)
    assert all(item.y + item.h <= diagram.height for item in diagram.boxes)
    long = wm._wrap("word " * 40, 170, 3)
    assert len(long) == 3 and long[-1].endswith("…")


def test_a_tickets_state_and_type():
    from agentdeck.work_story import IN_PROGRESS, NEEDS_YOU, QUIET

    assert wm.ticket_state(closed=True, section=NEEDS_YOU) == wm.DONE
    assert wm.ticket_state(closed=False, section=NEEDS_YOU) == wm.NEEDS_YOU
    assert wm.ticket_state(closed=False, section=IN_PROGRESS) == wm.RUNNING
    assert wm.ticket_state(closed=False, section=IN_PROGRESS, waits=True) == wm.WAITING
    assert wm.ticket_state(closed=False, section=QUIET) == wm.WAITING
    assert wm.ticket_state(closed=False, section=None) == wm.WAITING  # no Work yet
    assert wm.ticket_kind(["agent", "Wayfinder:Research"]) == "Research"
    assert wm.ticket_kind(["wayfinder:map"]) == "" and wm.is_map(["Wayfinder:Map"])


def test_moved_boxes_are_saved_on_the_deck_and_drawn_there(tmp_path):
    """#3627, the owner's decision: a box a person drags stands there on every device, until
    Reset. The deck keeps one layout per map; the server draws it and remembers each moved
    box's own place for Reset; arrows follow the moved box."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from agentdeck.db import Db
    from agentdeck.web import routes_works

    db = Db(tmp_path / "control.sqlite3")
    published = []
    app = FastAPI()
    app.include_router(routes_works.router)
    app.state.control_db = db
    app.state.app_state = SimpleNamespace(bus=SimpleNamespace(publish=published.append))
    url = "/api/works/github_issue:eerovil%2Fagentdeck%2310/map-layout"
    work_id = "github_issue:eerovil/agentdeck#10"
    one, fog = "github_issue:eerovil/agentdeck#11", "fog:0"
    with TestClient(app) as client:
        moved = {"box": one, "at": [500, -20.25]}
        assert client.put(url, json=moved).json() == {"work_id": work_id, "boxes": 1}
        # Another device moves another box: one box per request, so neither erases the other.
        assert client.put(url, json={"box": fog, "at": [1, 2]}).json()["boxes"] == 2
        assert db.wayfinder_layout(work_id) == {one: (500.0, -20.2), fog: (1.0, 2.0)}
        assert published  # every open page of the map redraws
        for bad in (
            {"box": one, "at": [1]},
            {"box": one, "at": [1, True]},
            {"box": one, "at": [1, 1e9]},
            {"box": "", "at": [1, 2]},
            {"positions": {one: [1, 2]}},  # the whole-layout write is gone
            [1],
        ):
            assert client.put(url, json=bad).status_code == 400
        assert client.put("/api/works/nope/map-layout", json=moved).status_code == 404
        assert db.wayfinder_layout(work_id) == {one: (500.0, -20.2), fog: (1.0, 2.0)}
        assert client.put(url, json={"box": fog, "at": None}).json()["boxes"] == 1
        assert client.put(url, json={"reset": True}).json()["boxes"] == 0
        assert db.wayfinder_layout(work_id) == {}

    # A fog box keeps its place when another bullet goes or comes: its key is its own text,
    # and identical bullets are told apart by order among themselves.
    a, b, c = "How restores work", "Whether the canary gets a schema", "Who owns the backups"
    [_, key_b] = wm.fog_keys([a, b])
    placed = {
        item.key: item
        for item in wm.layout([], fog_items=[b, c], goal="Done", moved={key_b: (700.0, 50.0)}).boxes
    }
    assert wm.fog_keys([b, c])[0] == key_b and (placed[key_b].x, placed[key_b].y) == (700, 50)
    assert placed[wm.fog_keys([b, c])[1]].home is None  # C did not take B's place
    assert wm.fog_keys([a, a.upper() + " ", b]) == (
        wm.fog_keys([a])[0],
        wm.fog_keys([a])[0] + ".2",
        key_b,
    )

    tickets = [ticket("1"), ticket("2", wm.WAITING, "1")]
    plain = wm.layout(tickets, fog_items=(), goal="Done")
    diagram = wm.layout(tickets, fog_items=(), goal="Done", moved={"1": (600.0, 400.0)})
    box = {item.key: item for item in diagram.boxes}
    home = {item.key: item for item in plain.boxes}["1"]
    assert (box["1"].x, box["1"].y, box["1"].home) == (600.0, 400.0, (home.x, home.y))
    assert box["2"].home is None
    assert diagram.width >= 600 + wm.BOX_W and diagram.height >= 400 + box["1"].h
    [arrow] = diagram.arrows  # still drawn, now from where the blocker stands
    assert arrow.path.startswith(f"M{600 + wm.BOX_W / 2:.1f},")


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_map_saves_each_drag_says_when_it_did_not_and_keeps_taps(tmp_path):
    """#3627 review: a drag saves only its own box, so two pages never erase each other's
    moves; a save the deck refused or never got is shown with Retry and is not lost when the
    section redraws; and a cancelled drag does not eat the next tap on a box."""
    from types import SimpleNamespace

    from conftest import async_playwright
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
    from test_web import _live_server

    from agentdeck import app as app_mod
    from agentdeck.db import Db
    from agentdeck.web import routes_works

    db = Db(tmp_path / "control.sqlite3")
    app = FastAPI()
    app.include_router(routes_works.router)
    app.state.control_db = db
    app.state.app_state = SimpleNamespace(bus=SimpleNamespace(publish=lambda topic: None))
    templates = Jinja2Templates(directory=str(app_mod._TEMPLATES_DIR))
    work_id = "github_issue:eerovil/agentdeck#10"
    tickets = [ticket("1"), ticket("2"), ticket("3", wm.WAITING, "1")]

    @app.get("/map", response_class=HTMLResponse)
    def page():
        diagram = wm.layout(
            tickets, fog_items=("Fog",), goal="Done", moved=db.wayfinder_layout(work_id)
        )
        part = templates.get_template("partials/wayfinder_map.html").render(
            diagram=diagram, key=work_id
        )
        return (
            '<!doctype html><link rel=stylesheet href="/static/app.css">'
            '<script src="/static/wayfinder_map.js"></script>'
            f'<main style="width:380px">{part}</main>'
        )

    app.mount("/static", StaticFiles(directory=str(app_mod._TEMPLATES_DIR.parent / "static")))

    async def drag(page, key: str, dx: float, dy: float) -> None:
        box = await page.locator(f'[data-wm-box="{key}"]').bounding_box()
        x, y = box["x"] + 20, box["y"] + 12
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + dx, y + dy, steps=6)
        await page.mouse.up()

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        one = await browser.new_page(viewport={"width": 400, "height": 800})
        two = await browser.new_page(viewport={"width": 400, "height": 800})
        for page in (one, two):
            await page.goto(base + "/map")
            await page.wait_for_selector(".wm-live")
        # Two pages each move a different box before either redraws: both moves stay.
        await drag(one, "1", 40, 30)
        await drag(two, "2", -30, 40)
        for _ in range(50):
            if len(db.wayfinder_layout(work_id)) == 2:
                break
            await one.wait_for_timeout(20)
        assert set(db.wayfinder_layout(work_id)) == {"1", "2"}
        assert one.url == base + "/map"  # a drag is not a tap

        # A refused save, then a lost one, is shown with Retry and survives a redraw.
        await one.route("**/map-layout", lambda route: route.fulfill(status=409, body="{}"))
        await drag(one, "3", 25, 25)
        await one.wait_for_selector("[data-wm-retry]:not([hidden])")
        await one.unroute("**/map-layout")
        await one.route("**/map-layout", lambda route: route.abort())
        await one.locator("[data-wm-retry]").click()
        await one.wait_for_timeout(200)
        assert await one.locator("[data-wm-retry]").is_visible()
        assert "3" not in db.wayfinder_layout(work_id)
        moved = await one.locator('[data-wm-box="3"]').get_attribute("transform")
        await one.evaluate(
            """() => { const map = document.querySelector('[data-wayfinder-map]');
              const copy = map.cloneNode(true); map.replaceWith(copy);
              document.dispatchEvent(new Event('htmx:afterSettle')); }"""
        )
        await one.wait_for_selector("[data-wm-retry]:not([hidden])")
        assert await one.locator('[data-wm-box="3"]').get_attribute("transform") == moved
        await one.unroute("**/map-layout")
        await one.locator("[data-wm-retry]").click()
        await one.wait_for_selector("[data-wm-retry]", state="hidden")
        assert "3" in db.wayfinder_layout(work_id)

        # A newer move that lands supersedes the failed one: Retry never sends the stale one.
        async def refused(route):
            await route.fulfill(status=409, body="{}")

        await one.route("**/map-layout", refused)
        await drag(one, "3", 30, 0)
        await one.wait_for_selector("[data-wm-retry]:not([hidden])")
        await one.unroute("**/map-layout")
        await drag(one, "3", 0, 30)
        await one.wait_for_selector("[data-wm-retry]", state="hidden")
        landed = db.wayfinder_layout(work_id)["3"]
        # So does a Reset that lands, for every box.
        await one.route("**/map-layout", refused)
        await drag(one, "1", 20, 20)
        await one.wait_for_selector("[data-wm-retry]:not([hidden])")
        await one.unroute("**/map-layout")
        assert db.wayfinder_layout(work_id)["3"] == landed
        await one.locator("[data-wm-reset]").click()
        await one.wait_for_selector("[data-wm-retry]", state="hidden")
        for _ in range(50):
            if not db.wayfinder_layout(work_id):
                break
            await one.wait_for_timeout(20)
        assert db.wayfinder_layout(work_id) == {}
        # A Reset that failed, then a move that landed: Retry replays both, in that order.
        await drag(one, "2", 10, 40)
        for _ in range(50):
            if db.wayfinder_layout(work_id):
                break
            await one.wait_for_timeout(20)
        await one.route(
            "**/map-layout",
            lambda route: (
                refused(route) if "reset" in (route.request.post_data or "") else route.continue_()
            ),
        )
        await one.locator("[data-wm-reset]").click()
        await one.wait_for_selector("[data-wm-retry]:not([hidden])")
        await one.unroute("**/map-layout")
        await drag(one, "1", 15, 25)
        await one.wait_for_timeout(200)
        assert set(db.wayfinder_layout(work_id)) == {"2", "1"}  # the Reset never landed
        assert await one.locator("[data-wm-retry]").is_visible()
        await one.locator("[data-wm-retry]").click()
        await one.wait_for_selector("[data-wm-retry]", state="hidden")
        for _ in range(50):
            if db.wayfinder_layout(work_id):
                break
            await one.wait_for_timeout(20)
        assert set(db.wayfinder_layout(work_id)) == {"1"}  # Reset, then the newer move

        # A drag that turns into a pinch when a second finger lands still saves its box.
        box = await two.locator('[data-wm-box="3"]').bounding_box()
        await two.evaluate(
            """([x, y]) => {
              const at = (type, id, px, py) => document.elementFromPoint(px, py).dispatchEvent(
                new PointerEvent(type, {bubbles: true, pointerId: id, pointerType: 'touch',
                                        clientX: px, clientY: py}));
              at('pointerdown', 11, x, y); at('pointermove', 11, x + 30, y + 20);
              at('pointerdown', 12, x + 120, y + 120); at('pointermove', 12, x + 140, y + 140);
              at('pointerup', 12, x + 140, y + 140); at('pointerup', 11, x + 30, y + 20); }""",
            [box["x"] + 20, box["y"] + 12],
        )
        for _ in range(50):
            if "3" in db.wayfinder_layout(work_id) and db.wayfinder_layout(work_id)["3"] != (
                landed
            ):
                break
            await two.wait_for_timeout(20)
        pinched = db.wayfinder_layout(work_id).get("3")
        assert pinched is not None and pinched != landed
        await two.reload()
        await two.wait_for_selector(".wm-live")
        assert await two.locator('[data-wm-box="3"]').get_attribute("transform") == (
            f"translate({pinched[0]:.1f} {pinched[1]:.1f})"
        )

        # A cancelled drag (the browser took the gesture) leaves the next tap working.
        box = await two.locator('[data-wm-box="3"]').bounding_box()
        await two.evaluate(
            """([x, y]) => { const frame = document.querySelector('[data-wm-frame]');
              const at = (type, dx) => frame.dispatchEvent(new PointerEvent(type, {
                bubbles: true, pointerId: 7, clientX: x + dx, clientY: y }));
              at('pointerdown', 0); at('pointermove', 30); at('pointercancel', 30); }""",
            [box["x"] + 20, box["y"] + 12],
        )
        await two.locator('[data-wm-box="1"]').click()
        await two.wait_for_url(base + "/works/1")  # the ticket's Work opens
        await browser.close()
