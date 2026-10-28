"""#182: the operator surface — the page, the fragment, and the controls."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import async_playwright
from fastapi.testclient import TestClient
from test_web import _live_server

from agentdeck import poller_ops
from agentdeck.poller_engine import health as engine_health
from agentdeck.poller_engine.state import EnginePaths, StateStore

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)

_MANIFEST = """
contract = 1

[board]
org = "shop"
project_number = 7
ownership_label = "agent"

[[repos]]
id = "store"
holds_issues = true
"""


def _seed(root, name="reference", *, at=None, **parts):
    store = StateStore(EnginePaths.from_base(root / "instances" / name / "state.json"))
    with store.transaction() as state:
        state.update(parts)
        engine_health.record_cycle(state, engine_health.CycleReport(scans=1), now=at or _NOW)


def _seed_board(root, *, issues):
    _seed(root, at=datetime.now(UTC), issues=issues)
    (root / "instances" / "reference" / "poller.toml").write_text(_MANIFEST)


def _issue(number, title, status):
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/shop/store/issues/{number}",
        "state": "OPEN",
        "labels": [],
        "assignees": [],
        "updatedAt": "2026-08-13T08:00:00Z",
        "projectItems": [
            {"number": 7, "owner": "shop", "title": "Store", "status": status}
        ],
    }


def _project_response(*issues, has_next=False, end_cursor=None):
    return {
        "data": {
            "repositoryOwner": {
                "projectV2": {
                    "items": {
                        "nodes": [
                            {
                                "content": {
                                    "number": issue["number"],
                                    "title": issue["title"],
                                    "url": issue["url"],
                                    "state": issue["state"],
                                    "repository": {"nameWithOwner": "shop/store"},
                                },
                                "fieldValueByName": {
                                    "name": issue["projectItems"][0]["status"]
                                },
                            }
                            for issue in issues
                        ],
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": end_cursor,
                        },
                    }
                }
            }
        }
    }


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """The real app with defaults, pointed at a poller root this test owns.

    Built from ``AppConfig`` rather than the host's config file, so the suite does not depend on
    a machine having one.
    """
    from agentdeck.app import create_app
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig

    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        poller=PollerConfig(root=str(tmp_path)),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path / "acc"))
        ],
    )
    client = TestClient(create_app(config))
    with client:
        yield client, tmp_path


def test_the_page_renders_with_no_instances(app_client):
    client, _root = app_client
    response = client.get("/pollers")
    assert response.status_code == 200
    assert "No Poller Instance is installed" in response.text


def test_the_page_shows_state_and_freshness_as_separate_signals(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    body = client.get("/pollers").text
    assert "reference" in body
    # Both signals appear, because "what it said" and "is that still true" differ.
    assert "state-ok" in body
    assert "freshness-fresh" in body


def test_a_stale_instance_is_not_presented_as_current(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC) - timedelta(hours=3))
    body = client.get("/pollers").text
    assert "state-stale" in body
    assert "3h ago" in body


def test_a_failure_later_wakes_ran_past_reads_as_dated_history(app_client):
    """A weeks-old "Last failure" read as today's breakage in red (#2384)."""
    client, root = app_client
    store = StateStore(EnginePaths.from_base(root / "instances" / "reference" / "state.json"))
    with store.transaction() as state:
        report = engine_health.CycleReport(scans=1)
        report.errors = ["configuration drifted from the activation"]
        engine_health.record_cycle(state, report, now=datetime.now(UTC) - timedelta(days=19))
    _seed(root, at=datetime.now(UTC))

    body = client.get("/pollers").text

    line = body[body.index('poller-last-failure">') :].split("</p>", 1)[0]
    assert "Last failure 19d ago: configuration drifted from the activation" in " ".join(
        line.split()
    )
    assert 'class="small muted poller-last-failure"' in body


def test_a_degraded_extension_says_which_one_and_why(app_client):
    """"extension-degraded" alone gave an operator nothing to act on (#2384)."""
    client, root = app_client
    store = StateStore(EnginePaths.from_base(root / "instances" / "reference" / "state.json"))
    with store.transaction() as state:
        report = engine_health.CycleReport(scans=1)
        report.extension_health = {
            "chatgpt-nudge": {
                "state": "degraded",
                "reason": "a reviewed pull request is parked with nothing to merge it",
            },
            "screenshots": {"state": "ok"},
        }
        engine_health.record_cycle(state, report, now=datetime.now(UTC))

    body = " ".join(client.get("/pollers").text.split())

    assert (
        "extension-degraded — chatgpt-nudge: a reviewed pull request is parked with nothing"
        " to merge it</li>"
    ) in body
    assert "screenshots:" not in body


def test_the_page_says_nothing_about_engine_promotion(app_client):
    """The engine deploys with AgentDeck (#2153); a leftover note from before is not news."""
    client, root = app_client
    (Path(root) / "promotion.json").write_text(
        json.dumps({"ok": False, "gate": "rebind", "attempts": 9, "reason": "FAIL rebind"})
    )
    body = client.get("/pollers").text
    assert "poller-promotion" not in body
    assert "Engine promotion" not in body


def test_the_fragment_returns_just_the_list(app_client, monkeypatch):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    body = client.get("/partials/pollers").text
    assert body.strip().startswith("<div id=\"poller-instances\"")
    assert "<html" not in body
    assert "Project settings changed" not in body

    # #3755: a project joined from its repository's file says when that file moved on.
    from agentdeck import gh_cli

    (root / "instances" / "reference" / "source.json").write_text(
        json.dumps({"repository": "shop/issues", "path": ".agentdeck/poller.toml", "sha": "a"})
    )

    async def gh(argv, *, timeout, repo=None, owner=None):
        return 0, b"b\n"

    monkeypatch.setattr(gh_cli, "run", gh)
    body = client.get("/partials/pollers").text
    assert "Project settings changed in\n          shop/issues" in body
    assert "<code>agentdeck-project update reference</code>" in body


def test_the_list_follows_its_stream_not_a_timer(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    body = client.get("/pollers").text
    # A self-polling list would fight open <details> and in-flight controls (AGENTS.md): the
    # rows follow the page's stream (#3228), keyed, and each fold remembers being open.
    assert 'sse-connect="/events?view=pollers"' in body
    assert 'sse-swap="pollers" hx-swap="keyed-rows"' in body
    assert 'data-key="instance-reference" data-rev="' in body
    assert 'data-keep-open="pollers-reference-controls"' in body
    assert "hx-trigger=\"every" not in body


@pytest.mark.asyncio
async def test_the_list_stream_sends_the_rows_on_connect_and_on_a_board_change(
    tmp_path, monkeypatch
):
    """#3228: the rows alone, keyed as the page has them, built once for every tab."""
    from test_change_budget import _EventsRequest, _until
    from test_web import _app_with_state

    import agentdeck.web.routes_sse as sse
    from agentdeck import work_counter
    from agentdeck.board_watch import BOARD_TOPIC
    from agentdeck.web.work_view import NEEDS_YOU_TOPIC

    app = _app_with_state(tmp_path)
    _seed(tmp_path / "poller", at=datetime.now(UTC))
    with work_counter.counting() as spent:
        stream = sse._stream(_EventsRequest(app, view="pollers"))
        other = sse._stream(_EventsRequest(app, view="pollers"))
        primed = await _until(stream, "event: pollers")
        assert primed.split("event: pollers", 1)[1] in await _until(other, "event: pollers")
    # One board read and render for both tabs.
    assert spent[work_counter.POLLER_ROWS] == 1
    await other.aclose()
    assert 'data-key="instance-reference"' in primed
    assert 'id="poller-instances"' not in primed  # the rows, not the list around them
    # A board change owes a redraw only after the interval; the top bar's own changes are
    # not held behind it (#3228 review).
    app.state.app_state.bus.publish(BOARD_TOPIC)
    app.state.app_state.bus.publish(NEEDS_YOU_TOPIC)
    assert "event: pollers" not in await _until(stream, "event: needs_you")
    monkeypatch.setattr(sse, "POLLERS_MIN_INTERVAL_S", 0.0)
    app.state.app_state.bus.publish(BOARD_TOPIC)
    assert 'data-key="instance-reference"' in await _until(stream, "event: pollers")
    await stream.aclose()


def test_controls_are_collapsed_so_a_row_keeps_its_height(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    body = client.get("/pollers").text
    assert "<details class=\"poller-controls\"" in body


def test_pausing_from_the_ui_pauses_and_re_renders(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    response = client.post("/pollers/reference/pause", data={"by": "eero"})
    assert response.status_code == 200
    assert "id=\"poller-instances\"" in response.text
    assert "Resume" in response.text          # the row now offers the opposite action
    view = poller_ops.read_instance(root, "reference", runner=lambda command: (1, ""))
    assert view.paused is True
    assert "eero" in view.pause_note
    resumed = client.post("/pollers/reference/resume")
    assert resumed.status_code == 200
    view = poller_ops.read_instance(root, "reference", runner=lambda command: (1, ""))
    assert view.paused is False


def test_only_pause_and_resume_are_offered(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    body = client.get("/pollers").text
    # The recovery buttons stored requests nothing ever performed, so they are gone (#2596).
    for action in ("clear-lock", "force-reconcile", "cold-reload", "rerun-cycle"):
        assert f"/pollers/reference/{action}" not in body
    assert "operator request" not in body


def test_an_unknown_control_is_refused(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    state_file = root / "instances" / "reference" / "state.json"
    before = state_file.read_bytes()
    for action in ("delete-everything", "clear-lock"):
        assert 400 <= client.post(f"/pollers/reference/{action}").status_code < 500
    assert state_file.read_bytes() == before


# --- the ChatGPT PR-review row went with the chatgpt-nudge extension (#3741) ---


def test_the_page_has_no_chatgpt_review_row_and_no_press_route(app_client):
    client, root = app_client
    _seed(root, at=datetime.now(UTC))
    assert "ChatGPT PR review" not in client.get("/pollers").text
    assert client.post("/pollers/reference/extensions/chatgpt-nudge").status_code in {404, 405}


def test_old_card_records_and_commands_add_no_rows(app_client):
    """The poller holds no cards since #3732; what an old state file still holds is history."""
    client, root = app_client
    _seed(
        root,
        at=datetime.now(UTC),
        issues={"store#5": {"status": "In progress", "blocked_by": ["store#4"]}},
        controls={"9": {"id": "9", "repo": "store", "number": 5, "kind": "claude",
                        "status": "queued", "created_at": "t"}},
    )
    body = client.get("/pollers").text
    assert "queued command" not in body
    assert "<strong>Working:</strong>" not in body and "Blocked:" not in body


def test_each_poller_shows_its_board_issues_and_statuses_from_the_shared_read_model(
    app_client,
):
    client, root = app_client
    _seed_board(
        root,
        issues={
            "store#5": {"status": "In progress", "started": True},
            "store#6": {"status": "In review", "started": True},
        },
    )
    client.app.state.issues._gh = "gh"

    async def fake_run(*_args, **_kwargs):
        return (
            0,
            json.dumps(
                _project_response(
                    _issue(5, "Fix checkout", "In progress"),
                    _issue(6, "Review refund", "In review"),
                )
            ),
        )

    client.app.state.issues._run = fake_run

    body = client.get("/pollers").text

    assert "Board · 2 issues" in body
    assert "shop/store#5" in body and "Fix checkout" in body
    assert "shop/store#6" in body and "Review refund" in body
    assert "In progress" in body and "In review" in body
    assert 'href="https://github.com/shop/store/issues/5"' in body
    assert 'class="poller-board-issue-link"' in body
    assert 'aria-label="Open shop/store#5 on GitHub"' in body
    assert '<details class="poller-board"' in body
    assert 'data-board-filter="In progress"' in body
    assert 'data-board-filter="In review"' in body
    assert "poller_boards.js" in body


def test_opening_the_poller_page_populates_an_empty_shared_issue_cache(app_client):
    client, root = app_client
    _seed_board(root, issues={})
    client.app.state.issues._gh = "gh"

    async def fake_run(*_args, **_kwargs):
        return (
            0,
            json.dumps(_project_response(_issue(5, "Follower issue", "Todo"))),
        )

    client.app.state.issues._run = fake_run

    body = client.get("/pollers").text

    assert "Board · 1 issue" in body
    assert "Follower issue" in body

    async def forbidden_run(*_args):
        raise AssertionError("a poller control must reuse the Project cache")

    client.app.state.issues._run = forbidden_run
    paused = client.post("/pollers/reference/pause", data={"by": "eero"}).text
    assert "Follower issue" in paused
    assert "Board · 1 issue" in paused


def test_an_unavailable_issue_read_is_shown_as_incomplete_not_as_an_empty_board(app_client):
    client, root = app_client
    _seed_board(root, issues={"store#5": {"status": "Todo"}})
    client.app.state.issues._gh = "gh"

    async def failed_run(*_args, **_kwargs):
        return (1, "")

    client.app.state.issues._run = failed_run

    body = client.get("/pollers").text

    assert "incomplete" in body
    assert "Missing issues are unknown, not absent." in body
    # An old card record in state is not an issue the read found (#3741).
    assert "shop/store#5" not in body


@pytest.mark.browser
async def test_the_board_expands_without_overflow_at_desktop_and_mobile(app_client):
    client, root = app_client
    _seed_board(
        root,
        issues={
            "store#5": {"status": "In progress", "started": True},
            "store#6": {"status": "Waiting for deployment verification", "started": True},
        },
    )
    client.app.state.issues._gh = "gh"

    async def fake_run(*_args, **_kwargs):
        return (
            0,
            json.dumps(
                _project_response(
                    _issue(5, "Fix checkout", "In progress"),
                    _issue(6, "Review refund", "Waiting for deployment verification"),
                )
            ),
        )

    client.app.state.issues._run = fake_run
    assert 'data-board-filter="Waiting for deployment verification"' in client.get(
        "/pollers"
    ).text

    async with async_playwright() as playwright, _live_server(client.app) as base:
        browser = await playwright.chromium.launch()
        for width in (1100, 320):
            page = await browser.new_page(viewport={"width": width, "height": 844})
            page.set_default_timeout(5_000)
            await page.goto(f"{base}/pollers", wait_until="domcontentloaded")
            card = page.locator('article.poller[data-instance="reference"]')
            board = card.locator("details.poller-board")
            controls = card.locator("details.poller-controls")
            assert not await board.evaluate("node => node.open")
            assert not await controls.evaluate("node => node.open")
            closed_height = await card.evaluate("node => node.getBoundingClientRect().height")

            await board.locator("summary").click()

            assert await board.locator("li").count() == 2
            issue_link = board.locator('a[href="https://github.com/shop/store/issues/5"]')
            assert await issue_link.locator(".pill").inner_text() == "In progress"
            row_box = await issue_link.locator("xpath=..").bounding_box()
            link_box = await issue_link.bounding_box()
            assert row_box and link_box
            assert link_box["width"] >= row_box["width"] * 0.95
            assert await issue_link.get_attribute("target") is None
            long_status = "Waiting for deployment verification"
            status_filter = board.locator(f'[data-board-filter="{long_status}"]')
            assert await status_filter.count() == 1
            await status_filter.click()
            assert await board.locator("li:not([hidden])").count() == 1
            assert f"Showing 1 {long_status} issue" in await board.inner_text()
            assert await card.evaluate("node => node.scrollWidth <= node.clientWidth")
            if width == 320:
                row = board.locator("li:not([hidden])")
                issue_width = await row.locator(".poller-board-issue").evaluate(
                    "node => node.getBoundingClientRect().width"
                )
                row_width = await row.evaluate("node => node.getBoundingClientRect().width")
                assert issue_width >= row_width * 0.45
            open_height = await card.evaluate("node => node.getBoundingClientRect().height")
            assert open_height > closed_height

            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir:
                visible_link = board.locator("li:not([hidden]) a").first
                await visible_link.hover()
                assert await visible_link.locator(".pill").count() == 1
                await card.screenshot(
                    path=Path(screenshot_dir) / f"issue-995-poller-link-{width}.png"
                )

            await page.evaluate(
                """() => {
                  const list = document.querySelector('#poller-instances');
                  const replacement = list.cloneNode(true);
                  replacement.querySelectorAll('details.poller-board').forEach((board) => {
                    board.removeAttribute('open');
                    board.removeAttribute('data-board-filter-active');
                    board.querySelectorAll('[data-board-status]').forEach((row) => {
                      row.removeAttribute('hidden');
                    });
                    board.querySelectorAll('[data-board-filter]').forEach((button) => {
                      button.setAttribute('aria-pressed', String(!button.dataset.boardFilter));
                    });
                  });
                  list.replaceWith(replacement);
                  replacement.dispatchEvent(new CustomEvent('htmx:load', {
                    bubbles: true,
                    detail: {elt: replacement}
                  }));
                }"""
            )
            board = card.locator("details.poller-board")
            assert await board.get_attribute("data-board-filter-active") == long_status
            assert await board.evaluate("node => node.open")
            assert await board.locator("li:not([hidden])").count() == 1
            visible_link = board.locator("li:not([hidden]) a")
            visible_box = await visible_link.bounding_box()
            assert visible_box
            await page.context.route(
                "https://github.com/**",
                lambda route: route.fulfill(status=200, body="GitHub issue"),
            )
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await visible_link.click(
                    position={
                        "x": visible_box["width"] - 4,
                        "y": visible_box["height"] / 2,
                    }
                )
            assert page.url == "https://github.com/shop/store/issues/6"
            await page.close()
        await browser.close()


# --- the Builder launch (#181) ---

def test_the_page_offers_the_builder_collapsed(app_client):
    client, _root = app_client
    body = client.get("/pollers").text
    assert "Build a Poller Project" in body
    # A new control must not add height until the operator opens it (AGENTS.md).
    assert '<details class="poller-build">' in body
    assert "approving a draft is not activation" in body.lower()


def test_the_launch_needs_a_project_paths_and_an_account(app_client):
    client, _root = app_client
    response = client.post("/pollers/build", data={"project": "reference"})
    assert response.status_code == 422




# --- the durable Builder run, and handing it on (#268) ---

@pytest.fixture
def builder_client(tmp_path):
    """The app with two accounts, so a run has somewhere to be handed to."""
    from agentdeck.app import create_app
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        poller=PollerConfig(root=str(tmp_path)),
        accounts=[
            AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path / "main")),
            AccountConfig(provider="claude_code", label="alt", config_dir=str(tmp_path / "alt")),
        ],
    )
    client = TestClient(create_app(config))
    with client:
        # A background scan of an account replaces its sessions with what is on disk, so one
        # landing mid-test dropped the session a test planted (#2693).
        client.portal.call(client.app.state.collector.stop)
        yield client, tmp_path


def _pin_usage(client, **pcts):
    """Fix what each account's usage is, so a handover test never reads the host's own."""
    from agentdeck.models import UsageSnapshot

    state = client.app.state.app_state
    for account in client.app.state.accounts:
        pct = pcts.get(account.label, 10.0)
        state.usage[account.key] = UsageSnapshot(
            account_key=account.key,
            five_hour_pct=pct,
            five_hour_resets_at=None,
            seven_day_pct=pct,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )


@pytest.fixture
def started(builder_client, monkeypatch):
    """Record every session the deck would start, instead of starting one."""
    from agentdeck.models import InjectResult

    client, _root = builder_client
    _pin_usage(client)
    calls = []

    async def fake_start_new(account, provider, cwd, message, images=None, **kwargs):
        calls.append(
            {
                "account": account.key,
                "cwd": str(cwd),
                "message": message,
                "session_token": kwargs.get("session_token"),
            }
        )
        return InjectResult(True)

    monkeypatch.setattr(client.app.state.injector, "start_new", fake_start_new)
    return calls


def _build(client, root, *, project="reference", accounts=("claude_code:main",)):
    return client.post(
        "/pollers/build",
        data={
            "project": project,
            "manifest": str(root / "poller.toml"),
            "overlay": str(root / "overlay.toml"),
            "account_key": list(accounts),
            "cwd": str(root),
        },
    )


def test_a_launch_is_recorded_as_a_durable_run(builder_client, started):
    from agentdeck import poller_builder

    client, root = builder_client
    response = _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    assert response.status_code == 202

    # The launch plan used to live only in the request; it has to outlive it for a later handover.
    run = poller_builder.read_run(root, "reference")
    assert run is not None
    assert run.account_key == "claude_code:main"
    assert run.order == ("claude_code:main", "claude_code:alt")
    assert run.manifest == str(root / "poller.toml")
    assert run.cwd == str(root)
    # The leg is bound to the session by the token the deck started it under, not by a guess.
    assert run.leg.session_token == started[0]["session_token"]


def test_a_project_id_that_is_a_path_is_refused(builder_client, started):
    client, root = builder_client
    response = _build(client, root, project="../escape")
    assert response.status_code == 422
    assert not (root.parent / "escape.json").exists()
    assert started == []


def test_the_page_shows_a_run_and_offers_the_next_account(builder_client, started):
    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))

    body = client.get("/pollers").text
    assert "reference" in body
    # Collapsed, so it adds no height at rest — but the summary already names who holds it.
    assert '<details class="poller-runs"' in body
    assert "Continue on claude_code:alt" in body


def test_a_handover_starts_the_next_leg_with_a_continuation_brief(builder_client, started):
    from agentdeck import poller_builder

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    run = poller_builder.read_run(root, "reference")

    response = client.post(
        "/pollers/builder/reference/handover", data={"intent": run.handover_intent}
    )
    assert response.status_code == 200

    # A new session on the next account, never a resume: the order may cross providers.
    assert len(started) == 2
    assert started[1]["account"] == "claude_code:alt"
    assert "same Builder effort" in started[1]["message"]
    assert str(root / "poller.toml") in started[1]["message"]
    assert started[1]["cwd"] == str(root)

    handed = poller_builder.read_run(root, "reference")
    assert handed.account_key == "claude_code:alt"
    assert [leg.account_key for leg in handed.legs] == ["claude_code:main", "claude_code:alt"]
    assert handed.leg.session_token == started[1]["session_token"]


def test_a_handover_continues_the_run_as_one_chat(builder_client, started):
    """A Builder run that moves account is still one conversation (#1267).

    The operator handed one effort to a second account; before chat identity
    reached this path the deck showed that as two unrelated chats, and the URL
    published for the run named the leg that had stopped.
    """
    from agentdeck import poller_builder
    from agentdeck.models import Session, SessionStatus

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    state = client.app.state.app_state
    first = "claude_code:main:builder-one"
    state.update_session(
        Session(
            key=first,
            account_key="claude_code:main",
            session_id="builder-one",
            status=SessionStatus.IDLE,
            title="Builder",
            cwd=root,
        )
    )
    state.bind_session_token(started[0]["session_token"], first)

    run = poller_builder.read_run(root, "reference")
    client.post("/pollers/builder/reference/handover", data={"intent": run.handover_intent})
    second = "claude_code:alt:builder-two"
    state.bind_session_token(started[1]["session_token"], second)

    chat = client.app.state.budget_db.chat_for_session(second)
    assert chat.legs == (first, second)
    assert chat.chat_id == client.app.state.budget_db.chat_for_session(first).chat_id


def test_a_refused_handover_leaves_the_run_free_to_move_later(builder_client, started,
                                                              monkeypatch):
    """A spawn nobody accepted must not leave the chat looking already moved."""
    from agentdeck import poller_builder
    from agentdeck.models import InjectResult, Session, SessionStatus

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    state = client.app.state.app_state
    first = "claude_code:main:builder-one"
    state.update_session(
        Session(
            key=first,
            account_key="claude_code:main",
            session_id="builder-one",
            status=SessionStatus.IDLE,
            title="Builder",
            cwd=root,
        )
    )
    state.bind_session_token(started[0]["session_token"], first)

    async def refuse(account, provider, cwd, message, images=None, **kwargs):
        started.append({"session_token": kwargs.get("session_token")})
        return InjectResult(False, "at_capacity")

    monkeypatch.setattr(client.app.state.injector, "start_new", refuse)
    run = poller_builder.read_run(root, "reference")
    client.post("/pollers/builder/reference/handover", data={"intent": run.handover_intent})

    # Nothing started, so nothing succeeds this chat — and the slot is free again.
    assert client.app.state.budget_db.reserve_chat_successor("later", first) == "new"


def test_a_repeated_press_does_not_start_a_second_leg(builder_client, started):
    from agentdeck import poller_builder

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    intent = poller_builder.read_run(root, "reference").handover_intent

    client.post("/pollers/builder/reference/handover", data={"intent": intent})
    second = client.post("/pollers/builder/reference/handover", data={"intent": intent})

    # Two legs editing one draft is exactly what the stable intent id exists to prevent.
    assert second.status_code == 200
    assert len(started) == 2
    assert len(poller_builder.read_run(root, "reference").legs) == 2


def test_a_handover_waits_for_a_leg_that_is_still_working(builder_client, started):
    from agentdeck import poller_builder
    from agentdeck.models import Session, SessionStatus

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main", "claude_code:alt"))
    run = poller_builder.read_run(root, "reference")

    state = client.app.state.app_state
    key = "claude_code:main:abc"
    state.bind_session_token(run.leg.session_token, key)
    state.sessions[key] = Session(
        key=key,
        account_key="claude_code:main",
        session_id="abc",
        status=SessionStatus.LIVE,
        thinking=True,
    )

    response = client.post(
        "/pollers/builder/reference/handover", data={"intent": run.handover_intent}
    )
    # Steering a live worker is allowed even over budget, so preempting its turn would lose it.
    assert response.status_code == 409
    assert len(started) == 1
    assert poller_builder.read_run(root, "reference").attempts == 0


def test_a_handover_with_nobody_left_holds_visibly(builder_client, started):
    from agentdeck import poller_builder

    client, root = builder_client
    _build(client, root, accounts=("claude_code:main",))
    run = poller_builder.read_run(root, "reference")

    response = client.post(
        "/pollers/builder/reference/handover", data={"intent": run.handover_intent}
    )
    assert response.status_code == 200
    assert len(started) == 1
    held = poller_builder.read_run(root, "reference")
    # A run that quietly looked finished would be the worst outcome.
    assert held.holding is True
    assert "no remaining account" in held.detail
    assert "Held" in response.text


def test_a_finished_run_can_be_forgotten_without_touching_its_drafts(builder_client, started):
    from agentdeck import poller_builder

    client, root = builder_client
    (root / "poller.toml").write_text("# the draft\n")
    _build(client, root)

    response = client.post("/pollers/builder/reference/forget")
    assert response.status_code == 200
    assert poller_builder.read_run(root, "reference") is None
    # The drafts are the point of the exercise; only the record is dropped.
    assert (root / "poller.toml").read_text() == "# the draft\n"
    assert '<details class="poller-runs"' not in response.text


def test_a_handover_for_an_unrecorded_project_is_not_found(builder_client, started):
    client, _root = builder_client
    response = client.post(
        "/pollers/builder/absent/handover", data={"intent": "absent:handover:0"}
    )
    assert response.status_code == 404
    assert started == []


# --- worker accounts (#901) -------------------------------------------------------------------

_OVERLAY = """\
contract = 1

[paths]
state = "/tmp/reference-state.json"

[worker]
# Hand-written note the deck must not eat.
account = "claude_code:test"
fallback_accounts = []
"""


@pytest.fixture
def accounts_client(app_client):
    """An installed instance whose Overlay names an account this deck actually has."""
    client, root = app_client
    _seed(root)
    (root / "instances" / "reference" / "overlay.toml").write_text(_OVERLAY)
    return client, root


def _overlay(root):
    return (root / "instances" / "reference" / "overlay.toml").read_text()


def test_the_controls_offer_the_hosts_accounts(accounts_client):
    client, _root = accounts_client
    response = client.get("/pollers")
    assert response.status_code == 200
    assert "Worker accounts" in response.text
    assert 'name="account" value="claude_code:test"' in response.text
    assert 'name="fallbacks" value="claude_code:test"' in response.text
    # Inside the existing Controls block, so the row costs no height until it is opened.
    assert '<details class="poller-settings">' not in response.text


def test_an_instance_with_no_overlay_says_so_instead_of_offering_a_form(app_client):
    client, root = app_client
    _seed(root)
    response = client.get("/pollers")
    assert "Not editable here" in response.text
    assert "poller-worker-accounts" not in response.text


def test_saving_writes_the_overlay_and_re_renders_the_list(accounts_client):
    client, root = accounts_client
    response = client.post(
        "/pollers/reference/settings/worker-accounts",
        data={"account": "claude_code:test", "fallbacks": ["claude_code:test"]},
    )
    assert response.status_code == 200
    assert 'id="poller-instances"' in response.text
    text = _overlay(root)
    assert 'account = "claude_code:test"' in text
    # The primary leads the order, so naming it again as a fallback adds nothing.
    assert "fallback_accounts = []" in text
    assert "Hand-written note" in text


def test_an_account_this_host_does_not_have_is_refused(accounts_client):
    client, root = accounts_client
    response = client.post(
        "/pollers/reference/settings/worker-accounts",
        data={"account": "claude_code:ghost"},
    )
    assert response.status_code == 200
    assert "has no account named claude_code:ghost" in response.text
    assert _overlay(root) == _OVERLAY


def test_an_account_already_in_the_overlay_may_be_kept(app_client):
    """A configured account this deck cannot see is still a legitimate choice."""
    client, root = app_client
    _seed(root)
    (root / "instances" / "reference" / "overlay.toml").write_text(
        _OVERLAY.replace('account = "claude_code:test"', 'account = "codex:elsewhere"')
    )
    response = client.post(
        "/pollers/reference/settings/worker-accounts",
        data={"account": "claude_code:test", "fallbacks": ["codex:elsewhere"]},
    )
    assert response.status_code == 200
    assert 'fallback_accounts = ["codex:elsewhere"]' in _overlay(root)


def test_saving_nothing_is_refused_rather_than_clearing_the_account(accounts_client):
    client, root = accounts_client
    response = client.post("/pollers/reference/settings/worker-accounts", data={})
    assert response.status_code == 200
    assert "choose the account" in response.text
    assert _overlay(root) == _OVERLAY


