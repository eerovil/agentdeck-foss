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


def test_a_merge_that_deploys_waits_for_its_deploy_before_asking_for_a_finish():
    """#2760: the issue closed on a merge into a repository that deploys on merge; for about a
    minute, until the engine started watching the deploy, it asked a person to finish it."""
    from types import SimpleNamespace

    from agentdeck.work_facts import DEPLOY_GRACE, MERGED, OutputFact, deploy_due

    closed = datetime(2026, 10, 9, 9, 20, tzinfo=UTC)
    card = SimpleNamespace(deploy_repos=("eerovil/agentdeck",))
    url = "https://github.com/eerovil/agentdeck/pull/2758"
    merged = (OutputFact(url, "EeroVil/AgentDeck", 2758, "card", MERGED),)
    soon = closed + timedelta(minutes=1)

    assert deploy_due(card, merged, closed, soon)
    assert not deploy_due(card, merged, closed, closed + DEPLOY_GRACE), "past the grace"
    assert not deploy_due(card, merged, closed, closed - timedelta(seconds=5)), "clock skew"
    assert not deploy_due(None, merged, closed, soon), "no Poller card"
    assert not deploy_due(card, merged, None, soon), "the issue is open"
    assert not deploy_due(SimpleNamespace(deploy_repos=()), merged, closed, soon)
    still_open = (replace(merged[0], state="open"),)
    assert not deploy_due(card, still_open, closed, soon), "nothing merged"
    elsewhere = (replace(merged[0], repository="eerovil/homr"),)
    assert not deploy_due(card, elsewhere, closed, soon), "merged where nothing deploys"


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
    """A Work whose Poller card finished and whose issue closed at ``closed_at``, with its
    worker's chat last active at ``active`` (ten minutes ago by default)."""
    from test_work_hidden import _board_card
    from test_work_ui import WORK, _add, _app, _board

    from agentdeck.web.work_view import SIGNOFF_CUTOFF

    card = replace(_board_card(), done=True, status="Done", dispatch_failed=False)
    app = _app(tmp_path, board=_board(card))
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
    return app, card, record


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

    app, _card, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(minutes=5), cutoff=NOW - timedelta(days=1)
    )
    client = _client(app)
    before = await app.state.work_overview.current()

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


@pytest.mark.asyncio
async def test_a_work_closed_before_the_cutoff_is_finished_as_before(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _card, _record = _deck(
        tmp_path, monkeypatch, closed_at=NOW - timedelta(hours=2), cutoff=NOW - timedelta(hours=1)
    )
    overview = await app.state.work_overview.current()

    assert _ids(overview.front.needs_you) == [] and _ids(overview.front.finished) == [WORK.work_id]
    response = _visibility(_client(app), "finish")
    assert response.status_code == 409 and app.state.control_db.work_signoffs() == {}


@pytest.mark.asyncio
async def test_a_reopened_work_closed_again_waits_for_a_new_finish(tmp_path, monkeypatch):
    from test_work_ui import WORK

    app, _card, record = _deck(
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

    app, _card, _record = _deck(
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

    app, _card, _record = _deck(
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


@pytest.mark.browser
@pytest.mark.asyncio
async def test_finish_takes_the_row_away_on_the_press_and_a_refusal_puts_it_back(
    tmp_path, monkeypatch
):
    """#3287: the row leaves Needs you as Finish is pressed, before the deck answers."""
    from conftest import async_playwright
    from test_web import _live_server
    from test_work_ui import WORK

    app, _card, _record = _deck(
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
