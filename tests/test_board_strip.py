"""#1007, #1691: the board strip — a board-less project's open issues, and what it refuses
to claim. The Poller's own cards were removed (#3741)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentdeck.board_strip import build_strip
from agentdeck.issue_context import IssueContext, PendingItem, PendingView
from agentdeck.poller_ops import BoardScope


def _scope(instance="reference", *repositories, boardless=True):
    return BoardScope(
        instance=instance,
        org="shop",
        issue_repositories=repositories or ("shop/store",),
        boardless=boardless,
    )


def _item(
    number,
    *,
    repository="shop/store",
    sessions=(),
    pulls=(),
    title="An issue",
):
    return PendingItem(
        repository=repository,
        number=number,
        issue=IssueContext(
            repository=repository,
            number=number,
            title=title,
            url=f"https://github.com/{repository}/issues/{number}",
            state="open",
        ),
        sessions=tuple(sessions),
        pull_requests=tuple(pulls),
    )


def _pull(number=7, status="open"):
    return {
        "repository": "shop/store",
        "number": number,
        "title": "the fix",
        "url": f"https://github.com/shop/store/pull/{number}",
        "status": status,
    }


def _view(*items, complete=True, board_complete=True, unknown=()):
    return PendingView(
        items=tuple(items),
        complete=complete,
        board_complete=board_complete,
        unknown=tuple(unknown),
    )


def test_an_issue_with_an_open_pull_request_carries_it():
    item = _item(11, pulls=[_pull(7)])
    strip = build_strip(_view(item), [_scope()])
    row = strip.instances[0].cards[0]
    assert row.pull_number == 7
    assert row.pull_url.endswith("/pull/7")
    # The row itself is the issue.
    assert row.url.endswith("/issues/11")


def test_a_merged_pull_request_is_not_carried():
    item = _item(11, pulls=[_pull(7, "merged")])
    row = build_strip(_view(item), [_scope()]).instances[0].cards[0]
    assert row.has_pull is False


def test_a_project_with_a_board_lists_no_issue():
    """Only a board-less project's issues are its board."""
    strip = build_strip(_view(_item(11, pulls=[_pull(7)])), [_scope(boardless=False)])
    assert strip.empty


def test_a_boardless_project_shows_the_issue_itself():
    """#1691: no Project means the open issue is the board, so it gets a row."""
    item = _item(11)
    strip = build_strip(_view(item), [_scope(boardless=True)])
    row = strip.instances[0].cards[0]
    assert row.identity == "shop/store#11"
    assert row.status == "Status unknown"
    assert row.url.endswith("/issues/11")
    assert strip.instances[0].overflow == 0


def test_a_boardless_project_leaves_out_a_closed_issue():
    item = PendingItem(repository="shop/store", number=11, issue_closed=True)
    strip = build_strip(_view(item), [_scope(boardless=True)])
    assert strip.empty


def test_a_boardless_project_leaves_out_an_issue_it_never_read():
    """An item that exists only because a chat named it is not an open issue."""
    item = PendingItem(repository="shop/store", number=11, pull_requests=(_pull(7),))
    strip = build_strip(_view(item), [_scope(boardless=True)])
    assert strip.empty


def test_a_boardless_backlog_is_cut_to_a_readable_strip():
    """The newest few, and the count it left out — never a cut list looking whole."""
    from agentdeck.board_strip import BOARDLESS_ROW_LIMIT

    extra = 3
    view = _view(
        *(
            _item(number)
            for number in range(100, 100 + BOARDLESS_ROW_LIMIT + extra)
        )
    )
    strip = build_strip(view, [_scope(boardless=True)])
    entry = strip.instances[0]
    assert len(entry.cards) == BOARDLESS_ROW_LIMIT
    assert entry.overflow == extra
    # Newest first, so the oldest are the ones left for `/pollers`.
    assert entry.cards[0].number == 100 + BOARDLESS_ROW_LIMIT + extra - 1
    assert min(row.number for row in entry.cards) == 100 + extra


def test_an_issue_no_scope_claims_is_dropped_rather_than_guessed():
    item = _item(11, repository="other/repo")
    strip = build_strip(_view(item), [_scope()])
    assert strip.empty


def test_issues_group_under_the_instance_that_declares_the_repository():
    view = _view(
        _item(11, pulls=[_pull(7)]),
        _item(12, repository="shop/abacus"),
    )
    strip = build_strip(
        view, [_scope("alpha", "shop/store"), _scope("beta", "shop/abacus")]
    )
    assert [entry.instance for entry in strip.instances] == ["alpha", "beta"]
    assert strip.card_count == 2


def test_an_instance_with_nothing_to_show_is_left_out():
    view = _view(PendingItem(repository="shop/store", number=11, issue_closed=True))
    strip = build_strip(view, [_scope("alpha", "shop/store"), _scope("beta", "shop/abacus")])
    assert strip.instances == ()


def test_an_unread_board_is_reported_rather_than_shown_as_a_zero():
    view = _view(board_complete=False, unknown=("shop/store",))
    strip = build_strip(view, [_scope()])
    assert strip.empty
    assert strip.complete is False
    assert strip.unknown == ("shop/store",)


def test_a_stale_issue_list_alone_does_not_warn():
    """It costs a row its title, which the row already falls back from (#1017)."""
    item = _item(11, pulls=[_pull(7)])
    strip = build_strip(_view(item, complete=False, unknown=("shop/store",)), [_scope()])
    assert strip.complete is True
    assert strip.card_count == 1


def test_an_unreadable_board_makes_the_strip_incomplete():
    strip = build_strip(_view(board_complete=False), [_scope()])
    assert strip.complete is False


# --- the page ---------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    from agentdeck.app import create_app
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig

    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        poller=PollerConfig(root=str(tmp_path)),
        accounts=[
            AccountConfig(
                provider="claude_code", label="test", config_dir=str(tmp_path / "acc")
            )
        ],
    )
    client = TestClient(create_app(config))
    with client:
        yield client


def test_the_dashboard_carries_the_strip_and_it_starts_shut(app_client):
    """Collapsed by default — a new control may not cost mobile height (#1007). Since #2219
    the strip is the chats inspector's; the front page has the board in one line."""
    body = app_client.get("/chats").text
    assert 'id="board-strip"' in body
    assert "<details class=\"board-strip\" id=\"board-strip\"" in body
    assert "board-strip\" id=\"board-strip\" data-board-strip open" not in body
    assert "/static/board_strip.js" in body


def test_a_row_opens_the_issue_and_the_pull_request_separately(app_client):
    """The row is the issue; the pull request keeps its own press beside it (#1036)."""
    from agentdeck.board_strip import BoardStrip, InstanceStrip, StripRow

    row = StripRow(
        repository="shop/store",
        number=11,
        title="A card",
        url="https://github.com/shop/store/issues/11",
        status="Status unknown",
        pull_number=7,
        pull_url="https://github.com/shop/store/pull/7",
    )
    strip = BoardStrip(instances=(InstanceStrip("reference", cards=(row,)),))
    html = app_client.app.state.templates.get_template(
        "partials/board_strip.html"
    ).render(board_strip=strip, asset_ver="test")

    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html
    issue_link = html.split('board-strip-issue', 1)[1].split(">", 1)[0]
    assert "/issues/11" in issue_link
    pull_link = html.split('board-strip-pr', 1)[1].split(">", 1)[0]
    assert "/pull/7" in pull_link


def test_a_cut_boardless_list_says_how_much_it_left_out(app_client):
    """#1691: the strip points at `/pollers` for the rest instead of looking whole."""
    from agentdeck.board_strip import BoardStrip, InstanceStrip, StripRow

    row = StripRow(
        repository="shop/store",
        number=11,
        title="An issue",
        url="https://github.com/shop/store/issues/11",
        status="Status unknown",
    )
    strip = BoardStrip(
        instances=(InstanceStrip("reference", cards=(row,), overflow=4),)
    )
    html = app_client.app.state.templates.get_template(
        "partials/board_strip.html"
    ).render(board_strip=strip, asset_ver="test")

    assert "4 more issues" in html
    assert 'href="/pollers"' in html


def test_the_strip_sits_above_the_chat_list_outside_any_swap_region(app_client):
    """No push may shut a strip the operator opened. The old Deckhand panel the strip
    once had to stay out of is on no page any more (#2256)."""
    body = app_client.get("/chats").text
    assert 'sse-swap="assistant"' not in body
    assert body.index('id="board-strip"') < body.index('id="sessions"')


async def _remembered(page, value: str) -> None:
    """Wait until the strip's choice has actually reached localStorage."""
    await page.wait_for_function(
        "expected => localStorage.getItem('agentdeck.boardStrip.open') === expected",
        arg=value,
    )


@pytest.mark.browser
@pytest.mark.asyncio
async def test_the_strip_stays_open_across_a_reload_once_the_operator_opens_it(
    tmp_path,
):
    """Shut is the default, but the operator's own press is remembered (#1007)."""
    from conftest import async_playwright
    from test_web import _app_with_state, _live_server

    app = _app_with_state(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/chats")
        strip = page.locator("#board-strip")
        await strip.wait_for()
        assert await strip.evaluate("el => el.open") is False

        await page.locator("#board-strip summary").click()
        assert await strip.evaluate("el => el.open") is True

        # The choice is written from the `toggle` event, which lands after the
        # click returns. Reloading before it does reads the previous answer back
        # and fails a test about persistence for a reason that is not about
        # persistence — rare here, routine on a busier machine (#1368).
        await _remembered(page, "1")
        await page.reload()
        await strip.wait_for()
        await page.wait_for_function(
            "document.querySelector('#board-strip').dataset.boardStripBound === '1'"
        )
        assert await strip.evaluate("el => el.open") is True

        # And shutting it is remembered just as faithfully.
        await page.locator("#board-strip summary").click()
        await _remembered(page, "0")
        await page.reload()
        await strip.wait_for()
        await page.wait_for_function(
            "document.querySelector('#board-strip').dataset.boardStripBound === '1'"
        )
        assert await strip.evaluate("el => el.open") is False
        await browser.close()


@pytest.mark.asyncio
async def test_a_chat_page_carries_no_strip(tmp_path):
    """The Works front page is what sits beside a chat now, and it has its own board line;
    the chat page's list no longer repeats the strip (#2346, replacing #1010)."""
    from httpx import ASGITransport, AsyncClient
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        detail = await client.get(f"/sessions/{key}")

    assert detail.status_code == 200
    # No chat list beside it either (#2603).
    assert 'id="sessions"' not in detail.text
    assert 'id="board-strip"' not in detail.text


@pytest.mark.asyncio
async def test_the_in_page_chat_open_does_not_read_the_board(tmp_path):
    """That response is only `.session-detail`, so the read would be pure latency."""
    from httpx import ASGITransport, AsyncClient
    from test_web import _app_with_state

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        detail = await client.get(
            f"/sessions/{key}", headers={"x-agentdeck-session-navigation": "detail"}
        )

    assert "board-strip" not in detail.text


# --- pull requests outliving their chat (#1012) -------------------------------


def _backlog_pull(number=7, *, branch="agent/issue-11", repository="shop/store"):
    return {
        "repository": repository,
        "number": number,
        "title": "the fix",
        "url": f"https://github.com/{repository}/pull/{number}",
        "status": "open",
        "draft": False,
        "head_branch": branch,
        "base_branch": "master",
    }


def _open_issue_list(*numbers, repository="shop/store"):
    from agentdeck.issue_context import RepositoryIssues

    return RepositoryIssues(
        repository,
        tuple(
            IssueContext(
                repository=repository, number=number, title="", url="", state="open"
            )
            for number in numbers
        ),
        present=True,
        age_s=0.0,
    )


def test_a_repositorys_own_open_pull_requests_join_by_branch():
    """The issue keeps its pull request after the worker chat is gone (#1012)."""
    from agentdeck.issue_context import IssueBacklog, RepositoryPulls, join_pending
    from agentdeck.poller_ops import BoardView

    scope = _scope()
    backlog = IssueBacklog(
        board=BoardView(scopes=(scope,)),
        repositories=(_open_issue_list(11),),
        pulls=(RepositoryPulls("shop/store", (_backlog_pull(7),)),),
    )

    strip = build_strip(join_pending(backlog, []), [scope])
    assert strip.instances[0].cards[0].pull_number == 7


def test_an_unread_pull_request_list_makes_the_strip_partial():
    """Unknown is not empty — the bar says so instead of showing a smaller count."""
    from agentdeck.issue_context import IssueBacklog, RepositoryPulls, join_pending
    from agentdeck.poller_ops import BoardScope, BoardView

    scope = BoardScope(
        instance="reference", org="shop", issue_repositories=("shop/store",)
    )
    backlog = IssueBacklog(
        board=BoardView(scopes=(scope,)),
        pulls=(RepositoryPulls("shop/store", (), False, "not cached"),),
    )

    strip = build_strip(join_pending(backlog, []), [scope])
    assert strip.complete is False


def test_a_pull_request_on_an_unrelated_branch_is_not_joined_to_an_issue():
    """Only an `issue-<n>` branch names an issue; nothing else is guessed."""
    from agentdeck.issue_context import IssueBacklog, RepositoryPulls, join_pending
    from agentdeck.poller_ops import BoardView

    scope = _scope()
    backlog = IssueBacklog(
        board=BoardView(scopes=(scope,)),
        repositories=(_open_issue_list(11),),
        pulls=(
            RepositoryPulls("shop/store", (_backlog_pull(7, branch="feat/rewrite"),)),
        ),
    )

    strip = build_strip(join_pending(backlog, []), [scope])
    # The issue is still listed — it just carries no pull request, because none
    # was matched rather than because one was guessed.
    assert strip.card_count == 1
    assert strip.instances[0].cards[0].has_pull is False


def test_a_stale_pull_request_cache_is_an_answer_not_an_unknown(tmp_path):
    """Only the 15-minute sweep refills a 5-minute cache, so stale must not read
    as unknown — the bar would say "partial" most of the time (#1016)."""
    from agentdeck.github_cache import GitHubLookup
    from agentdeck.issue_context import IssueResolver

    resolver = IssueResolver(poller_root=tmp_path)
    row = {
        "number": 7,
        "title": "the fix",
        "url": "https://github.com/shop/store/pull/7",
        "headRefName": "agent/issue-11",
        "baseRefName": "master",
        "isDraft": False,
    }
    resolver._metadata.peek = lambda key: GitHubLookup([row], False)

    entry = resolver._cached_pulls("shop/store")
    assert entry.complete is True
    assert [pull["number"] for pull in entry.pulls] == [7]


def test_an_absent_pull_request_cache_is_still_unknown(tmp_path):
    """Absence is the unknown the flag exists for, and it survives the change."""
    from agentdeck.issue_context import IssueResolver

    entry = IssueResolver(poller_root=tmp_path)._cached_pulls("shop/store")
    assert entry.complete is False
    assert entry.reason == "GitHub pull requests are not cached"


def _rotten_backlog(age_s: float):
    """A board-less project whose issue list is that old."""
    from agentdeck.issue_context import IssueBacklog, RepositoryIssues
    from agentdeck.poller_ops import BoardView

    scope = _scope()
    return scope, IssueBacklog(
        board=BoardView(scopes=(scope,)),
        repositories=(
            RepositoryIssues(
                "shop/store", (), False, "GitHub issue cache is stale", False, True, age_s
            ),
        ),
    )


def test_a_days_old_issue_list_makes_the_strip_say_partial():
    """Showing rows the deck cannot prove are still open is only honest while it
    says so (#1115)."""
    from agentdeck.issue_context import join_pending

    scope, backlog = _rotten_backlog(5 * 24 * 3600.0)

    strip = build_strip(join_pending(backlog, []), [scope])
    assert strip.complete is False
    assert any("shop/store" in entry for entry in strip.unknown)


# --- the way into the chat working an issue (#1694) ------------------------


def _session(key, title="working it"):
    from agentdeck.issue_context import SessionRef

    return SessionRef(key=key, title=title, matched_on="issue")


def _render_strip(row):
    """The strip's own template, rendered from one hand-built row."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from agentdeck import board_strip as strip_module
    from agentdeck.issue_latest_comment import issue_href

    root = Path(strip_module.__file__).parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)), autoescape=True)
    env.filters["issue_href"] = issue_href
    strip = strip_module.BoardStrip(
        instances=(strip_module.InstanceStrip(instance="reference", cards=(row,)),)
    )
    return env.get_template("partials/board_strip.html").render(
        board_strip=strip, asset_ver="test"
    )


def test_an_issue_with_a_chat_carries_the_way_into_it():
    """#1694: the chat working an issue is one press from it."""
    item = _item(11, sessions=[_session("claude_code:main:abc")])
    row = build_strip(_view(item), [_scope()]).instances[0].cards[0]
    assert row.has_session is True
    assert row.session_key == "claude_code:main:abc"
    assert row.session_path == "/sessions/claude_code:main:abc"
    assert row.session_title == "working it"


def test_an_issue_with_no_chat_offers_nothing_to_open():
    row = build_strip(_view(_item(11)), [_scope()]).instances[0].cards[0]
    assert row.has_session is False
    assert row.session_path == ""


def test_the_newest_chat_is_the_one_the_row_opens():
    """The join hands an item's chats over newest first, so the head is latest."""
    item = _item(
        11,
        sessions=[_session("codex:main:new"), _session("codex:main:old")],
    )
    row = build_strip(_view(item), [_scope()]).instances[0].cards[0]
    assert row.session_key == "codex:main:new"


def test_the_chat_link_comes_from_the_callers_own_resolver():
    """The dashboard publishes the durable `/chats/<id>` address when it has one."""
    item = _item(11, sessions=[_session("claude_code:main:abc")])
    strip = build_strip(
        _view(item),
        [_scope()],
        session_path=lambda key: f"/chats/chat-for-{key}",
    )
    row = strip.instances[0].cards[0]
    assert row.session_path == "/chats/chat-for-claude_code:main:abc"


def test_the_dashboard_resolves_chat_links_from_one_snapshot(app_client):
    """Chat identity is read once for the whole strip, not once per row: reading
    it a session at a time is what `chat_snapshot` exists to avoid."""
    from agentdeck.web import render

    state = app_client.app.state.app_state
    reads: list[str] = []
    original = state.chat_for_session

    def counting(session_key, chats=None):
        reads.append(session_key)
        return original(session_key, chats)

    state.chat_for_session = counting  # type: ignore[method-assign]
    try:
        request = type("R", (), {"app": app_client.app})()
        context = render.board_strip_context(request, state)
    finally:
        state.chat_for_session = original  # type: ignore[method-assign]

    assert "board_strip" in context
    # One read per chat at most, and only for rows that actually have one.
    assert len(reads) == len(set(reads))


def test_the_rendered_row_opens_the_chat_inside_the_deck():
    """A deck page, so no new tab, and `data-session-key` so the navigation
    script swaps it in place like any other chat row."""
    from agentdeck.board_strip import StripRow

    html = _render_strip(
        StripRow(
            repository="shop/store",
            number=11,
            title="A card",
            url="https://github.com/shop/store/issues/11",
            status="In progress",
            session_key="claude_code:main:abc",
            session_path="/chats/deadbeef",
            session_title="working it",
        )
    )

    assert 'class="pill board-strip-chat"' in html
    assert 'href="/chats/deadbeef"' in html
    assert 'data-session-key="claude_code:main:abc"' in html
    # The issue and pull request links open a new tab; a deck page must not.
    tag = html[html.index('class="pill board-strip-chat"') :]
    assert "target=" not in tag[: tag.index("</a>")]


def test_the_rendered_row_says_nothing_when_no_chat_exists():
    from agentdeck.board_strip import StripRow

    html = _render_strip(
        StripRow(
            repository="shop/store",
            number=11,
            title="A card",
            url="https://github.com/shop/store/issues/11",
            status="In progress",
        )
    )
    assert "board-strip-chat" not in html


