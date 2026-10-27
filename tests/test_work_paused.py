"""A paused Work (#2548): a person's Pause puts the Poller's paused label on the issue.

The label is the whole state. The engine reads it on its next wake and starts nothing on the
card; the deck reads it from its cached issue (the webhook keeps it current within seconds),
falling back to the engine's own reading, and lists the Work under Paused until Resume takes
the label off. A closed issue wins over a pause.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_work_actions import _Gh
from test_work_ui import WORK, _app, _board, _card

from agentdeck import gh_cli
from agentdeck.work_actions import (
    PAUSE_BUTTON,
    RESUME_BUTTON,
    Refused,
    Step,
    pausable,
    pause_control,
    pause_refusal,
    plan,
)
from agentdeck.work_overview import is_paused, work_href
from agentdeck.work_story import PAUSED, front_sections

LABEL = "paused"


def _board_card(**fields):
    fields.setdefault("status_role", "in_progress")
    fields.setdefault("generation", "gen-2")
    fields.setdefault("started", True)
    fields.setdefault("paused_label", LABEL)
    return _card(**fields)


def _deck(tmp_path, monkeypatch, *, labels=None, gh=None, **fields):
    """A Work read through the deck's own board and cached issue record."""
    from agentdeck.web import routes_works

    card = _board_card(**fields)
    app = _app(tmp_path, board=_board(card))
    record = {"title": "Pause me", "state": "open", "is_pr": False, "comments": 1}
    if labels is not None:
        record["labels"] = labels
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): record}
    gh = gh or _Gh((0, b"", b""))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    monkeypatch.setattr(routes_works.shutil, "which", lambda name: "/usr/bin/gh")
    return app, record, gh


def _client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _press(client, action, action_id="press-0001"):
    return client.post(
        work_href(WORK.work_id) + "/action",
        data={"action": action, "client_action_id": action_id},
    )


def _ids(rows):
    return [row.card.work_id for row in rows]


# --- reading it ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("record", "engine", "label", "expected"),
    [
        ({"labels": ["agent", "Paused"]}, False, LABEL, True),  # the cache is read first
        ({"labels": ["agent"]}, True, LABEL, False),  # ...and a removed label resumes at once
        ({"title": "cached before labels were"}, True, LABEL, True),  # the engine's reading
        ({"title": "cached before labels were"}, False, LABEL, False),
        ({"labels": ["paused"]}, True, "", False),  # an instance with no label pauses nothing
        (None, True, LABEL, True),
    ],
)
def test_the_cached_issue_says_whether_a_work_is_paused(record, engine, label, expected):
    card = _board_card(paused=engine, paused_label=label)
    assert is_paused(card, record) is expected


def test_no_card_is_never_paused():
    assert is_paused(None, {"labels": [LABEL]}) is False


# --- through the deck ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_paused_work_is_listed_apart_and_needs_nobody(tmp_path, monkeypatch):
    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=["agent", LABEL])
    client = _client(app)

    overview = await app.state.work_overview.current()

    front = overview.front
    assert _ids(front.paused) == [WORK.work_id]
    assert _ids(front.needs_you) == [] and _ids(front.active) == []
    (row,) = front.paused
    assert row.section == PAUSED and row.attention is None
    assert row.status.headline == "Paused" and row.status.you != "Nothing needed"
    assert "running" not in {key for key, _count, _words in front.summary}
    page = client.get("/", params={"chats": "0"}).text
    # A group of the front page's own (#2566), not a link to a page of its own.
    assert 'data-front-group="paused"' in page and 'data-front-toggle="paused"' in page
    assert page.count(f'data-work-id="{WORK.work_id}"') == 1
    assert 'data-front-in="paused"' in page and 'value="resume"' in page
    assert "data-paused-link" not in page and "view=paused" not in page
    moved = client.get("/works", params={"view": "paused"}, follow_redirects=False)
    assert moved.status_code == 307 and moved.headers["location"].startswith("/")
    work_page = client.get(work_href(WORK.work_id)).text
    assert 'data-work-pause="resume"' in work_page and 'value="pause"' not in work_page


@pytest.mark.asyncio
async def test_star_on_the_work_page_marks_the_work_everywhere(tmp_path, monkeypatch):
    """#3116: Star sits on the Work page's status line; a starred Work shows a star on its
    front-page row, and the page then offers Unstar."""
    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=["agent"])
    client = _client(app)
    page = client.get(work_href(WORK.work_id)).text
    assert 'data-work-star="star"' in page and "fr-star" not in page

    pressed = client.post(work_href(WORK.work_id) + "/star", data={"action": "star"})
    assert pressed.status_code == 200 and "Starred" in pressed.text
    assert app.state.control_db.starred_works() == {WORK.work_id}
    front = (await app.state.work_overview.current()).front
    (card,) = [row.card for row in (*front.needs_you, *front.active, *front.waiting)]
    assert card.starred
    assert 'class="fr-star"' in client.get("/", params={"chats": "0"}).text
    assert 'data-work-star="unstar"' in client.get(work_href(WORK.work_id)).text

    assert client.post(work_href(WORK.work_id) + "/star", data={"action": "x"}).status_code == 422
    client.post(work_href(WORK.work_id) + "/star", data={"action": "unstar"})
    assert app.state.control_db.starred_works() == frozenset()


@pytest.mark.asyncio
async def test_a_turn_still_running_finishes_first(tmp_path, monkeypatch):
    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=[LABEL])
    overview = await app.state.work_overview.current()
    (row,) = overview.front.paused
    busy = replace(row.card, working_count=1)

    from agentdeck.work_story import work_status

    assert "current turn will finish first" in work_status(busy).summary


@pytest.mark.asyncio
async def test_resume_takes_a_persons_paused_label_off_as_the_owners_authority(
    tmp_path, monkeypatch
):
    app, record, gh = _deck(tmp_path, monkeypatch, labels=["agent", LABEL])
    client = _client(app)
    await app.state.work_overview.current()

    resumed = _press(client, "resume")

    assert resumed.status_code == 200 and "Resumed" in resumed.text
    ((argv, _stdin, repo),) = gh.calls
    assert argv == ["/usr/bin/gh", "issue", "edit", "2147", "--repo", "eerovil/agentdeck",
                    "--remove-label", LABEL]
    assert repo == "eerovil/agentdeck"


@pytest.mark.asyncio
async def test_resume_is_refused_on_a_work_that_is_not_paused(tmp_path, monkeypatch):
    app, _record, gh = _deck(tmp_path, monkeypatch, labels=[])
    response = _press(_client(app), "resume")
    assert response.status_code == 409 and "not offered" in response.text
    assert gh.calls == []


# --- what is offered ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_any_unfinished_work_may_be_paused_on_the_deck(tmp_path, monkeypatch):
    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=[])
    overview = await app.state.work_overview.current()
    (row,) = (*overview.front.active, *overview.front.needs_you)
    card = row.card

    # The deck pauses it itself (#2559); the Poller's label pause went with its cards (#3025).
    assert pausable(card) and pause_control(card) is PAUSE_BUTTON
    assert plan("pause", card, row.attention) == Step("deck_pause", "eerovil/agentdeck", 2147)
    finished = replace(card, awaiting_finish=True)
    assert "has finished" in pause_refusal(finished)
    assert not pausable(finished) and pause_control(finished) is None
    with pytest.raises(Refused, match="has finished"):
        plan("pause", finished, row.attention)
    paused = replace(card, paused=True, label_paused=True)
    assert not pausable(paused) and pause_control(paused) is RESUME_BUTTON
    assert plan("resume", paused, None) == Step("remove_label", "eerovil/agentdeck", 2147, LABEL)
    held = replace(card, paused=True, deck_paused=True)
    assert pause_control(held) is RESUME_BUTTON
    assert plan("resume", held, None) == Step("deck_resume", "eerovil/agentdeck", 2147)


@pytest.mark.asyncio
async def test_a_closed_issue_wins_over_a_pause(tmp_path, monkeypatch):
    app, record, _gh = _deck(tmp_path, monkeypatch, labels=[LABEL], done=True)
    record.update(state="closed", closedAt="2026-09-30T10:00:00Z")

    overview = await app.state.work_overview.current()

    assert overview.front.paused == ()
    assert all(not row.card.paused for row in (*overview.front.needs_you, *overview.front.finished))


def test_paused_rows_leave_every_other_section(tmp_path, monkeypatch):
    """``front_sections`` puts a paused row on the Paused list only, newest first."""
    from types import SimpleNamespace

    def row(number, minutes):
        from datetime import UTC, datetime, timedelta

        card = SimpleNamespace(
            work_id=f"w{number}", last_activity=datetime.now(UTC) - timedelta(minutes=minutes),
            source_label=f"eerovil/agentdeck#{number}",
        )
        return SimpleNamespace(card=card, section=PAUSED, finished_order=None, waits=False,
                               stopped_at=None)

    sections = front_sections([row(1, 30), row(2, 5)])
    assert _ids(sections.paused) == ["w2", "w1"]
    assert sections.needs_you == () and sections.active == () and sections.quiet == 0


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_work_page_puts_resume_at_the_end_of_the_headline(tmp_path, monkeypatch):
    """The control sits inside the headline, so the status keeps its height on a phone."""
    from conftest import async_playwright
    from test_web import _live_server

    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=[LABEL])
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + work_href(WORK.work_id))
        head = page.locator("#work-status .wd-status-head")
        button = head.locator('[data-work-pause="resume"] button[data-front-action="resume"]')
        await button.wait_for(timeout=15000)
        head_box = await head.bounding_box()
        button_box = await button.bounding_box()
        assert button_box["y"] < head_box["y"] + head_box["height"]
        assert "paused" in (await head.inner_text()).casefold()
        await browser.close()


# --- a Work no Poller card holds (#2559) ----------------------------------------------------------


def _chat_only(tmp_path, monkeypatch):
    """A Work whose only members are chats: no Poller card tracks its issue."""
    from test_work_ui import _add

    app = _app(tmp_path)
    record = {"title": "Chat only", "state": "open", "is_pr": False, "comments": 0}
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): record}
    _add(app, "claude_code:test:helper", WORK)
    gh = _Gh((0, b"", b""))
    monkeypatch.setattr(gh_cli, "run_once", gh)
    return app, gh


@pytest.mark.asyncio
async def test_a_work_without_a_card_is_paused_on_the_deck_until_resume(tmp_path, monkeypatch):
    app, gh = _chat_only(tmp_path, monkeypatch)
    client = _client(app)
    await app.state.work_overview.current()
    page = client.get(work_href(WORK.work_id)).text
    assert 'data-work-pause="pause"' in page and "data-work-pause-refused" not in page

    paused = _press(client, "pause")

    assert paused.status_code == 200 and "the deck sends nothing to its chats" in paused.text
    assert gh.calls == []  # nothing on GitHub: no label holds a Work no card tracks
    assert WORK.work_id in app.state.control_db.paused_works()
    front = (await app.state.work_overview.current()).front
    assert _ids(front.paused) == [WORK.work_id]
    (row,) = front.paused
    assert row.card.deck_paused and not row.card.label_paused
    assert "deck sends nothing to its chats" in row.status.summary
    assert 'data-work-pause="resume"' in client.get(work_href(WORK.work_id)).text

    resumed = _press(client, "resume", "press-0002")

    assert resumed.status_code == 200 and "Resumed" in resumed.text
    assert app.state.control_db.paused_works() == {}
    assert (await app.state.work_overview.current()).front.paused == ()


@pytest.mark.asyncio
async def test_a_deck_pause_ends_when_the_work_finishes(tmp_path, monkeypatch):
    app, _gh = _chat_only(tmp_path, monkeypatch)
    app.state.control_db.pause_work(WORK.work_id)
    _closed_record = {"title": "Chat only", "state": "closed", "is_pr": False,
                      "closedAt": "2026-09-30T10:00:00Z"}
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): _closed_record}

    overview = await app.state.work_overview.current()

    assert overview.front.paused == ()
    assert not any(card.paused for card in overview.cards)


@pytest.mark.asyncio
async def test_the_deck_starts_no_turn_in_a_paused_works_chats(tmp_path, monkeypatch):
    """Every deck send goes through the injector, which asks the app whether a Work the chat
    belongs to is paused; a label pause and a deck pause both hold it."""
    app, _gh = _chat_only(tmp_path, monkeypatch)
    client = _client(app)
    await app.state.work_overview.current()
    key = "claude_code:test:helper"
    assert app.state.injector.held(key) is None

    _press(client, "pause")

    held = app.state.injector.held(key)
    assert held and "eerovil/agentdeck#2147 is paused" in held and "resume it" in held
    assert app.state.injector.held("claude_code:test:elsewhere") is None
    _press(client, "resume", "press-0002")
    assert app.state.injector.held(key) is None


@pytest.mark.asyncio
async def test_a_message_queued_before_the_pause_is_never_sent(tmp_path):
    import asyncio

    from agentdeck.config import InjectConfig
    from agentdeck.inject import InjectionService
    from agentdeck.models import Account, Capability, InjectResult, Session, SessionStatus

    class _BlockingProvider:
        def __init__(self):
            self.release = asyncio.Event()
            self.messages = []

        async def inject(self, account, session, message, *, timeout_s):
            self.messages.append(message)
            await self.release.wait()
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True))
    provider = _BlockingProvider()
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session("codex:test:sid", account.key, "sid", SessionStatus.IDLE, cwd=tmp_path,
                      capabilities=frozenset({Capability.INJECT}))
    assert (await service.start(account, session, provider, "first")).accepted
    for _ in range(20):
        await asyncio.sleep(0)
        if provider.messages:
            break
    assert (await service.start(account, session, provider, "second")).accepted

    service.held = lambda _key: "eerovil/agentdeck#2147 is paused"
    refused = await service.start(account, session, provider, "third")
    provider.release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break

    assert not refused.accepted and refused.reason == "eerovil/agentdeck#2147 is paused"
    assert provider.messages == ["first"]
    states = {item.text: (item.state, item.reason) for item in service.status(session.key).items}
    assert states["second"] == ("failed", "eerovil/agentdeck#2147 is paused")


def test_nothing_paused_draws_no_paused_group(tmp_path, monkeypatch):
    """With nothing paused the front page carries no Paused header, so it is no taller (#2566)."""
    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=["agent"])

    page = _client(app).get("/", params={"chats": "0"}).text

    assert 'data-front-group="paused"' not in page and 'data-front-in="paused"' not in page


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_paused_group_folds_and_stays_folded_through_a_reload(tmp_path, monkeypatch):
    """Paused folds like every front-page group, and the fold survives a reload (#2566)."""
    from conftest import async_playwright
    from test_web import _live_server

    app, _record, _gh = _deck(tmp_path, monkeypatch, labels=[LABEL])
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + "/?chats=0")
        head = page.locator('#works-paused [data-front-group="paused"]')
        row = page.locator(f'#works-paused article[data-work-id="{WORK.work_id}"]')
        await row.locator('button[data-front-action="resume"]').wait_for(timeout=15000)
        assert "paused" in (await head.inner_text()).casefold()

        await head.locator("[data-front-toggle]").click()
        await row.wait_for(state="hidden")
        await page.reload()

        await head.wait_for()
        await row.wait_for(state="hidden")
        toggle = head.locator("[data-front-toggle]")
        assert await toggle.get_attribute("aria-expanded") == "false"
        await toggle.click()
        await row.wait_for(state="visible")
        await browser.close()


# --- the router's resume (#2956) -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_routers_resume_ends_a_deck_pause_and_takes_the_label_off(tmp_path, monkeypatch):
    """#2956: a paused deck-run Work is resumed by its own automatic news. Paused is read from
    the cached issue for the Work, so a pull request event (no labels of its own) sees it."""
    from types import SimpleNamespace

    app, gh = _chat_only(tmp_path, monkeypatch)
    router = app.state.work_router
    release = app.state.work_release
    release.ports = replace(
        release.ports, policy=lambda repository: SimpleNamespace(paused_label="Paused")
    )
    record = {"title": "Chat only", "state": "open", "is_pr": False, "labels": ["paused "]}
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): record}
    rereads = []

    async def reread(refs, *, limit):
        rereads.append(list(refs))
        record["labels"] = []
        return 1

    app.state.issues.refresh_issue_records = reread
    app.state.control_db.pause_work(WORK.work_id)
    assert router._paused(WORK.work_id)

    assert await router._resume(WORK.work_id) is True

    assert app.state.control_db.paused_works() == {}
    [call] = gh.calls
    assert call[0][-2:] == ["--remove-label", "paused"]  # as the issue spells it
    assert rereads == [[(WORK.repository, WORK.number)]]
    assert not router._paused(WORK.work_id)


@pytest.mark.asyncio
async def test_a_label_that_will_not_come_off_resumes_nothing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    app, _gh = _chat_only(tmp_path, monkeypatch)
    monkeypatch.setattr(gh_cli, "run_once", _Gh((1, b"", b"boom")))
    release = app.state.work_release
    release.ports = replace(
        release.ports, policy=lambda repository: SimpleNamespace(paused_label="paused")
    )
    record = {"title": "Chat only", "state": "open", "is_pr": False, "labels": ["paused"]}
    app.state.issues.cached_issues = lambda refs: {(WORK.repository, WORK.number): record}
    assert app.state.work_router._paused(WORK.work_id)
    assert await app.state.work_router._resume(WORK.work_id) is False


@pytest.mark.asyncio
async def test_a_deck_without_a_reviewer_counts_a_merge_press_as_the_review(
    tmp_path, monkeypatch
):
    """The app wires the release from the reviewer it has (#3623): none in the public copy,
    where ``chat_review`` is a stand-in, so a person's Merge press is the review pass."""
    from agentdeck import chat_review

    monkeypatch.setattr(chat_review, "PUBLIC_STUB", False, raising=False)  # the real module
    app, _gh = _chat_only(tmp_path, monkeypatch)
    assert app.state.work_release.ports.press_is_review is False
    monkeypatch.setattr(chat_review, "PUBLIC_STUB", True, raising=False)
    app, _gh = _chat_only(tmp_path / "public", monkeypatch)
    assert app.state.work_release.ports.press_is_review is True


async def test_the_routers_resume_wakes_the_release(tmp_path, monkeypatch):
    """#2956 review: a verdict the release held as "paused" merges once the router resumes
    the Work, as the Work page's Resume makes the release look again."""
    app, _gh = _chat_only(tmp_path, monkeypatch)
    release = app.state.work_release
    db = app.state.control_db
    looked = []

    async def evaluate(work_id):
        # Looks again only once the pause is over, so a held merge can go now.
        looked.append((work_id, work_id in db.paused_works()))
        return "merged"

    monkeypatch.setattr(release, "handles", lambda work_id: True)
    monkeypatch.setattr(release, "evaluate", evaluate)
    db.pause_work(WORK.work_id)

    assert await app.state.work_router._resume(WORK.work_id) is True
    assert looked == [(WORK.work_id, False)]

    async def broken(work_id):
        raise RuntimeError("gh is down")

    monkeypatch.setattr(release, "evaluate", broken)
    db.pause_work(WORK.work_id)
    assert await app.state.work_router._resume(WORK.work_id) is True, "resumed all the same"
