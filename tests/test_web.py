import asyncio
import base64
import json
import logging
import os
import re
import shutil
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import assert_eventually, async_playwright, resized, wait_until
from httpx import ASGITransport, AsyncClient
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import expect

from agentdeck import mac_runner
from agentdeck.app import create_app
from agentdeck.assistant import AssistantInsight, AssistantView
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig
from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.host_stats import HostStats
from agentdeck.inject import DelegationStatus, InjectionStatus
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    IssueRef,
    PendingInteraction,
    PollerReviewVerdict,
    PullRequestRef,
    Session,
    SessionStatus,
    SubagentProgress,
    UsageSnapshot,
)
from agentdeck.providers.claude_code.provider import worker_type
from agentdeck.providers.delivery import DeliveryReceipts


def _app_with_state(tmp_path, *, with_transcript=False, extra_accounts=()):
    import json

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path)),
            *extra_accounts,
        ],
        poller=PollerConfig(root=str(tmp_path / "poller")),
    )
    if with_transcript:
        proj = tmp_path / "projects" / "-tmp"
        proj.mkdir(parents=True)
        lines = [
            {
                "type": "user",
                "timestamp": "2026-07-15T07:41:00Z",
                "message": {"role": "user", "content": "first question"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-15T07:42:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "an answer here"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        (proj / "sid1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    app = create_app(config)
    issues = getattr(app.state, "issues", None)
    if issues is not None and hasattr(issues, "_metadata"):
        # The resolver's cache is the host's own by default; the Work pages read it (#2226).
        from agentdeck.github_cache import GitHubMetadataCache

        issues._metadata = GitHubMetadataCache(tmp_path / "github-metadata.sqlite3")
    state = app.state.app_state
    state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=42.0,
            five_hour_resets_at=None,
            seven_day_pct=7.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )
    )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@asynccontextmanager
async def _live_server(app):
    """Serve the app on a real port, for browser checks that need real headers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        # A graceful shutdown waits for open connections, and a browser page left
        # holding the dashboard's event stream never closes one — so a plain
        # `await server_task` here turns any failure inside the block into a
        # suite that hangs until the watchdog kills it fifteen minutes later,
        # naming no test. #2029 lost most of a day to exactly that: a one-line
        # assertion failure arrived as a wedge. Shutdown is now bounded, so the
        # real failure gets reported instead.
        server.should_exit = True
        done, _ = await asyncio.wait({server_task}, timeout=5)
        if not done:
            server.force_exit = True
            done, _ = await asyncio.wait({server_task}, timeout=5)
        if not done:
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task
        sock.close()


async def _scroll_into_view_through_swaps(page, locator, attempts: int = 5):
    """Scroll to an element on a page whose list swaps itself while you do.

    The deck's cards carry relative times, so a card's revision changes as those
    tick and the live list legitimately replaces rows underneath the browser. A
    plain `scroll_into_view_if_needed` then dies with "Element is not attached to
    the DOM" whenever a swap lands inside that call — an intermittent failure on
    master, and on the cluster's slower pods a frequent one. Re-resolving the
    locator after a swap is the honest wait; the swap itself is the product
    working as designed.
    """
    for _ in range(attempts - 1):
        try:
            await locator.scroll_into_view_if_needed()
            return
        except PlaywrightError:
            await page.wait_for_timeout(200)
    await locator.scroll_into_view_if_needed()


def _session_card(html: str, session_key: str) -> str:
    """One rendered row from the session list — never a Deckhand panel row, which
    names its own session key and is rendered above the list."""
    body = html.split('id="sessions"', 1)[-1]
    return body.split(f'data-session-key="{session_key}"', 1)[1].split("</a>", 1)[0]


class _CardList:
    """The card list where a test reads a page response's ``.text``."""

    def __init__(self, text: str) -> None:
        self.text = text


def _with_inline_cards_script(html: str) -> str:
    """A `/chats` page for `set_content`, which has no origin to load `/static/` from:
    the cards' layout script (#2603) rides inline instead."""
    script = (
        Path(__file__).parents[1] / "src/agentdeck/web/static/chats_page.js"
    ).read_text()
    return re.sub(
        r'<script src="/static/chats_page\.js[^"]*" defer></script>',
        lambda _match: f"<script>{script}</script>",
        html,
    )


def _session_keys(html: str, *, short: bool = False) -> set[str]:
    """The top-level cards the chats page's list rendered (#2603)."""
    body = html.split('<div id="sessions"', 1)[1]
    keys = set()
    for chunk in body.split('<a class="session ')[1:]:
        if chunk.startswith("subagent-row"):
            continue
        key = chunk.split('data-session-key="', 1)[1].split('"', 1)[0]
        keys.add(key.rsplit(":", 1)[-1] if short else key)
    return keys


def _list_every_chat(monkeypatch) -> None:
    """Pages render every chat's card, Work or not, for tests about the card itself."""
    from agentdeck.web import work_view

    monkeypatch.setattr(work_view, "work_chat_ids", lambda app: frozenset())


def _card_list(app) -> str:
    """Every chat's card as the list renders it, Work or not.

    The chats view leaves a Work's chats out (#2256); a test about how a card
    renders reads this instead, so a chat with an issue still has a card to read.
    """
    from agentdeck.web.render import (
        ChatListQuery,
        render_session_groups,
        session_list_context,
    )

    context = session_list_context(
        app.state.accounts,
        app.state.app_state.session_presentation(),
        injector=app.state.injector,
        assistant=app.state.assistant,
        query=ChatListQuery(),
    )
    context["rendered_groups"] = render_session_groups(app.state.templates, context)
    return '<div id="sessions">' + app.state.templates.get_template(
        "partials/session_list.html"
    ).render(**context)


def _done_bar(html: str) -> str:
    """The bottom-of-transcript Deckhand control, both of its rendered directions."""
    body = html.split('id="deckhand-done-bar"', 1)[1]
    return body.split("</section>", 1)[0].split("<div id=", 1)[0]


def _session_action_bar(html: str) -> str:
    """The bottom-of-transcript PR and Deckhand action row."""
    body = html.split('id="session-action-bar"', 1)[1]
    return body.split('id="pending-interaction-slot"', 1)[0]




def _add_large_session_history(app, count=70):
    """Add a deterministic recent-to-old history plus protected old families."""
    state = app.state.app_state
    state.sessions.clear()
    now = datetime.now(UTC)
    for index in range(count):
        state.update_session(
            Session(
                key=f"claude_code:test:history-{index:03d}",
                account_key="claude_code:test",
                session_id=f"history-{index:03d}",
                status=SessionStatus.LIVE,
                title=f"History Session {index:03d}",
                last_activity=now - timedelta(minutes=index),
                last_prompt="p" * 5_000,
                last_text="r" * 5_000,
            )
        )

    protected = {
        "waiting": Session(
            key="claude_code:test:waiting-old",
            account_key="claude_code:test",
            session_id="waiting-old",
            status=SessionStatus.LIVE,
            title="Waiting Old",
            question="Choose one?",
            last_activity=now - timedelta(days=20),
        ),
        "interactive": Session(
            key="claude_code:test:interactive-old",
            account_key="claude_code:test",
            session_id="interactive-old",
            status=SessionStatus.LIVE,
            title="Interactive Old",
            capabilities=frozenset({Capability.INTERACT}),
            last_activity=now - timedelta(days=21),
        ),
        "selected": Session(
            key="claude_code:test:selected-old",
            account_key="claude_code:test",
            session_id="selected-old",
            status=SessionStatus.LIVE,
            title="Selected Old",
            last_activity=now - timedelta(days=22),
        ),
        "attention": Session(
            key="claude_code:test:attention-old",
            account_key="claude_code:test",
            session_id="attention-old",
            status=SessionStatus.LIVE,
            title="Attention Old",
            last_activity=now - timedelta(days=23),
        ),
        "parent": Session(
            key="claude_code:test:parent-old",
            account_key="claude_code:test",
            session_id="parent-old",
            status=SessionStatus.LIVE,
            title="Parent Old",
            last_activity=now - timedelta(days=24),
        ),
        "child": Session(
            key="claude_code:test:child-old",
            account_key="claude_code:test",
            session_id="child-old",
            status=SessionStatus.LIVE,
            title="Delegated Child Old",
            is_delegated=True,
            last_activity=now - timedelta(days=24),
        ),
    }
    for session in protected.values():
        state.update_session(session)
    state.delegation_parents[protected["child"].key] = protected["parent"].key
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(
            AssistantInsight(
                session_key=protected["attention"].key,
                kind="waiting",
                headline="attention",
            ),
        ),
    )
    return protected


def test_session_list_window_bounds_history_and_keeps_actionable_families(tmp_path):
    from agentdeck.web.render import SESSION_HISTORY_PAGE_SIZE, session_list_context

    app = _app_with_state(tmp_path)
    protected = _add_large_session_history(app)
    presentation = app.state.app_state.session_presentation()
    context = session_list_context(
        app.state.accounts,
        presentation,
        injector=app.state.injector,
        assistant=app.state.assistant,
    )

    rendered = {session.key for session in context["sessions"]}
    assert len(context["sessions"]) == SESSION_HISTORY_PAGE_SIZE + 4
    assert {protected[name].key for name in (
        "waiting", "interactive", "attention", "parent"
    )} <= rendered
    # Nothing is selected on the chats page (#2603), so an old idle chat is history.
    assert protected["selected"].key not in rendered
    assert protected["child"].key not in rendered
    assert [child.key for child in context["children_of"][protected["parent"].key]] == [
        protected["child"].key
    ]
    assert context["history_shown"] == SESSION_HISTORY_PAGE_SIZE
    assert context["history_remaining"] == 31
    assert context["history_next_url"] == "/chats?history_limit=71"


async def test_session_history_load_expands_cards(tmp_path):
    app = _app_with_state(tmp_path)
    _add_large_session_history(app)
    async with _client(app) as client:
        initial = await client.get("/chats")
        expanded = await client.get("/chats", params={"history_limit": 80})

    initial_list = initial.text.split('<div id="sessions"', 1)[1]
    expanded_list = expanded.text.split('<div id="sessions"', 1)[1]
    assert initial_list.count('data-session-key="') == 45  # 44 roots + one child row
    assert 'data-history-limit="40"' in initial_list
    assert 'href="/chats?history_limit=71"' in initial_list
    assert "Load older" in initial_list
    assert expanded_list.count('data-session-key="') == 76  # 75 roots + one child row
    assert 'data-history-limit="80"' in expanded_list
    assert "Load older" not in expanded_list


def test_session_card_preview_is_utf8_bounded_and_marks_truncation():
    from agentdeck.inject import QueuedMessage
    from agentdeck.web.render import (
        SESSION_CARD_PREVIEW_BYTES,
        card_preview,
        session_queue_summaries,
    )

    preview = card_preview("🙂<&" * 2_000)
    assert preview.endswith("…")
    assert len(preview.encode("utf-8")) <= SESSION_CARD_PREVIEW_BYTES

    class StubInjector:
        def status(self, _key):
            return InjectionStatus(
                "running", items=(QueuedMessage(1, "q" * 5_000),)
            )

    summaries = session_queue_summaries(
        [Session("key", "account", "id", SessionStatus.LIVE)], StubInjector()
    )
    assert summaries["key"]["text"].endswith("…")
    assert len(summaries["key"]["text"].encode("utf-8")) <= SESSION_CARD_PREVIEW_BYTES


async def test_healthz(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "revision" in r.json()  # staging.sh waits on it after a deploy (#2628)


async def test_dashboard_renders_usage_and_session(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=23.0,
            memory_pct=61.0,
            memory_used_bytes=10,
            memory_total_bytes=20,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    assert r.status_code == 200
    assert "Hello World Session" in r.text
    assert "42%" in r.text
    assert "CPU" in r.text
    assert "23%" in r.text
    assert "MEM" in r.text
    assert "61%" in r.text
    assert 'class="acct host-acct"' in r.text
    # The "hide closed" filter toggle is present as a form field (#2603); there is
    # no "show done" any more (#2599).
    assert 'name="closed" value="0"' in r.text
    assert 'name="done"' not in r.text
    # …as are the collapsed account/project/source pickers (#355). One account is
    # configured here, so only the pickers that can narrow anything are offered.
    assert 'id="more-filters"' in r.text
    assert 'id="filter-panel"' in r.text
    assert 'name="source" value="kanban"' in r.text
    assert 'name="account"' not in r.text
    # The old Deckhand attention panel is on no page any more (#2256).
    assert 'id="assistant-panel"' not in r.text
    assert "Attention triage" not in r.text


def _quick_process_sampler(monkeypatch):
    from agentdeck import host_processes

    real = host_processes.sample_processes
    monkeypatch.setattr(
        host_processes,
        "sample_processes",
        lambda chats, **kw: real(chats, **{**kw, "window_s": 0.05}),
    )


async def test_processes_page_shows_owners_and_busiest_processes(tmp_path, monkeypatch):
    """#2663: the header's host row leads to a page that names who uses the CPU."""
    _quick_process_sampler(monkeypatch)
    app = _app_with_state(tmp_path)
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=97.0,
            memory_pct=52.0,
            memory_used_bytes=10,
            memory_total_bytes=20,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        dashboard = await c.get("/chats")
        page = await c.get("/processes")
        partial = await c.get("/partials/processes")
    assert 'href="/processes"' in dashboard.text
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "<h1>Processes</h1>" in page.text
    assert 'class="process-table process-owners"' in page.text
    assert 'class="process-table process-top"' in page.text
    assert "memory 52%" in page.text
    # Read from the real /proc: something is always running, so both tables have rows.
    assert re.search(r'process-owners">\s*<thead>.*?</thead>\s*<tbody>\s*<tr>', page.text, re.S)
    assert re.search(r'process-top">\s*<thead>.*?</thead>\s*<tbody>\s*<tr>', page.text, re.S)
    assert partial.status_code == 200
    assert "<h1>" not in partial.text
    assert 'class="process-table process-owners"' in partial.text


@pytest.mark.browser
async def test_host_figures_open_processes_collapsed_and_expanded(tmp_path, monkeypatch):
    """#2663: the CPU/MEM figures lead to /processes in both header states, while a tap
    anywhere else on the collapsed line still opens the usage panel."""
    _quick_process_sampler(monkeypatch)
    app = _app_with_state(tmp_path)
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=97.0,
            memory_pct=52.0,
            memory_used_bytes=10,
            memory_total_bytes=20,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/chats")
        await page.locator(".usage-mini .mini-host").click()
        await page.wait_for_url(base + "/processes")
        await page.locator(".process-summary").wait_for()

        await page.goto(base + "/chats")
        assert not await page.locator(".limit-bars").is_visible()
        # A tap at the line's start (the account figures) lands on the toggle laid over it.
        await page.locator(".usage-mini").click(position={"x": 8, "y": 8})
        await expect(page.locator(".limit-bars")).to_be_visible()
        await page.locator(".host-acct .bar-row").first.click()
        await page.wait_for_url(base + "/processes")
        await browser.close()


@pytest.mark.browser
async def test_processes_refresh_swaps_the_tables_in_place(tmp_path, monkeypatch):
    import itertools

    from agentdeck import host_processes

    # The same rows every time, only the measurement time moves: real /proc data
    # changes how many rows there are, which legitimately moves what is below them.
    rows = tuple(
        host_processes.ProcessRow(100 + i, f"Owner {i}", None, 40.0 - i, 1 << 20, 60.0, "cmd")
        for i in range(25)
    )
    owners = tuple(
        host_processes.OwnerRow(row.owner, None, row.cpu_pct, row.rss_bytes, 1) for row in rows[:12]
    )
    ticks = itertools.count(1)
    monkeypatch.setattr(
        host_processes,
        "sample_processes",
        lambda chats, **kw: host_processes.ProcessSnapshot(
            window_s=2.0,
            cores=4,
            total_cpu_pct=500.0,
            owners=owners,
            top=rows,
            measured_at=float(next(ticks)),
            skipped=0,
        ),
    )
    app = _app_with_state(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 500}, service_workers="block"
        )
        await page.goto(base + "/processes")
        summary = page.locator(".process-summary")
        await summary.wait_for()
        before = await summary.get_attribute("data-measured-at")
        await page.evaluate("window.scrollTo(0, 200)")
        assert await page.evaluate("window.scrollY") > 0
        # What must hold is what the reader sees: a row stays where it was on screen.
        # Not scrollY itself — a live header update above the page may land at any
        # moment, and the browser rightly scrolls to keep the content still (#2663).
        row_top = (
            "document.querySelector('.process-top tbody tr:nth-child(10)')"
            ".getBoundingClientRect().top"
        )
        before_top = await page.evaluate(row_top)
        # Clicked from script: Playwright's own click would scroll the button into view.
        await page.get_by_role("button", name="Refresh").evaluate("button => button.click()")
        await expect(summary).not_to_have_attribute("data-measured-at", before)
        assert await page.locator(".process-owners").count() == 1
        assert abs(await page.evaluate(row_top) - before_top) < 1
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-2663-processes-390.png"),
                full_page=True,
            )
        await browser.close()


async def test_budget_controls_and_write_routes_are_removed(tmp_path):
    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"

    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get(f"/sessions/{key}")
        defaults = await client.post("/settings/session-budget-defaults")
        grant = await client.post(f"/sessions/{key}/budget")

    assert "budget-defaults" not in dashboard.text
    assert "session-budget" not in detail.text
    assert defaults.status_code in {404, 405}
    assert grant.status_code in {404, 405}


@pytest.mark.parametrize("viewport_width", [390, 1100])
@pytest.mark.browser
async def test_budget_free_dashboard_and_session_render_at_phone_and_desktop(
    tmp_path, viewport_width
):
    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": viewport_width, "height": 844},
            service_workers="block",
        )
        await page.goto(base + "/chats")
        await page.locator(".session").wait_for()
        assert await page.locator("#budget-defaults").count() == 0
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir)
                    / f"issue-1497-dashboard-{viewport_width}.png"
                ),
                full_page=True,
            )

        await page.goto(base + f"/sessions/{key}")
        await page.locator(".session-detail").wait_for()
        assert await page.locator("#session-budget").count() == 0
        # The spend chip that said "No usage data" left with spend (#2864).
        assert "No usage data" not in await page.locator("main").inner_text()
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir)
                    / f"issue-1497-session-{viewport_width}.png"
                ),
                full_page=True,
            )
        await browser.close()




async def test_chat_model_preference_is_stored_on_the_chat_and_survives_the_leg(
    tmp_path,
):
    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    state = app.state.app_state

    def stored() -> str:
        return app.state.db.chat_model_preference(state.chat_identity_key(key))

    async with _client(app) as client:
        detail = await client.get(f"/sessions/{key}")
        saved = await client.post(
            f"/sessions/{key}/model-preference", data={"model": "opus"}
        )
        # It belongs to the logical chat, not to the leg the operator happened to open.
        after_save = stored()
        refused = await client.post(
            f"/sessions/{key}/model-preference", data={"model": "not-a-model"}
        )
        after_refusal = stored()
        cleared = await client.post(f"/sessions/{key}/model-preference", data={})
        after_clear = stored()

    assert 'id="session-model-preference"' in detail.text
    # Collapsed by default, so it costs no vertical space on a phone until asked for.
    assert '<details id="session-model-preference"' in detail.text
    assert "No preference" in detail.text
    assert "next leg boundary" in detail.text

    assert "Saved" in saved.text
    assert 'value="opus"' in saved.text and "selected" in saved.text
    assert after_save == "opus"

    assert "does not offer the model" in refused.text
    # A refused save must not quietly replace the stored choice.
    assert after_refusal == "opus"

    assert "no preference" in cleared.text
    assert after_clear == ""


async def test_session_permission_prompt_failure_stays_off_and_shows_error(
    tmp_path, monkeypatch
):
    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    provider = PROVIDERS["claude_code"]
    session = app.state.app_state.sessions[key]
    app.state.app_state.sessions[key] = replace(
        session,
        capabilities=session.capabilities | {Capability.PERMISSION_PROMPTS},
    )
    monkeypatch.setattr(
        provider,
        "permission_prompts_allowed",
        lambda _account, _session: False,
    )

    async def reject_update(_account, _session, _enabled):
        return InjectResult(False, "could not save permission prompt setting")

    monkeypatch.setattr(provider, "set_permission_prompts", reject_update)

    async with _client(app) as client:
        response = await client.post(
            f"/sessions/{key}/permission-prompts", data={"enabled": "true"}
        )

    assert response.status_code == 200
    assert "could not save permission prompt setting" in response.text
    assert "checked" not in response.text


@pytest.mark.browser
async def test_permission_prompt_checkbox_works_on_mobile_and_desktop(
    tmp_path, monkeypatch
):
    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    provider = PROVIDERS["claude_code"]
    setting = {"enabled": False}
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    app.state.app_state.sessions[key] = replace(
        session,
        capabilities=session.capabilities | {Capability.PERMISSION_PROMPTS},
    )
    monkeypatch.setattr(
        provider,
        "permission_prompts_allowed",
        lambda _account, _session: setting["enabled"],
    )

    async def set_permission_prompts(_account, _session, enabled):
        setting["enabled"] = enabled
        return InjectResult(True)

    monkeypatch.setattr(provider, "set_permission_prompts", set_permission_prompts)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(base + "/sessions/claude_code:test:sid1")
        checkbox = page.locator('#session-permission-prompts input[type="checkbox"]')
        await expect(checkbox).to_be_visible()
        await expect(checkbox).not_to_be_checked()
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        await checkbox.check()
        await expect(checkbox).to_be_checked()

        await resized(page, 1200, 800)
        await checkbox.uncheck()
        await expect(checkbox).not_to_be_checked()
        await browser.close()












async def test_bypass_permissions_chip_only_shows_when_offered_and_reports_a_refusal(
    tmp_path, monkeypatch
):
    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(provider, "bypass_permissions_on", lambda _a, _s: False)

    async def reject_update(_account, _session, _enabled):
        return InjectResult(False, "this chat already bypasses permissions")

    monkeypatch.setattr(provider, "set_bypass_permissions", reject_update)

    async with _client(app) as client:
        without = await client.get(f"/sessions/{key}")
        forbidden = await client.post(
            f"/sessions/{key}/bypass-permissions", data={"enabled": "true"}
        )
        session = app.state.app_state.sessions[key]
        app.state.app_state.sessions[key] = replace(
            session,
            capabilities=session.capabilities | {Capability.BYPASS_PERMISSIONS},
        )
        offered = await client.get(f"/sessions/{key}")
        refused = await client.post(
            f"/sessions/{key}/bypass-permissions", data={"enabled": "true"}
        )

    assert 'id="session-bypass-permissions"' not in without.text
    assert forbidden.status_code == 403
    assert 'id="session-bypass-permissions"' in offered.text
    assert refused.status_code == 200
    assert "this chat already bypasses permissions" in refused.text
    assert "checked" not in refused.text


async def test_a_new_chat_in_bypass_shows_the_chip_checked(tmp_path, monkeypatch):
    # New owned Claude chats start with the switch on (#2319).
    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    app.state.app_state.sessions[key] = replace(
        session,
        capabilities=session.capabilities | {Capability.BYPASS_PERMISSIONS},
    )
    monkeypatch.setattr(
        PROVIDERS["claude_code"], "bypass_permissions_on", lambda _a, _s: True
    )

    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")

    chip = page.text.split('id="session-bypass-permissions"', 1)[1].split("</label>", 1)[0]
    assert "checked" in chip
    assert "bypass-permissions-on" in chip


@pytest.mark.browser
async def test_bypass_permissions_checkbox_works_on_mobile_and_desktop(
    tmp_path, monkeypatch
):
    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    provider = PROVIDERS["claude_code"]
    setting = {"enabled": False, "calls": []}
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    app.state.app_state.sessions[key] = replace(
        session,
        capabilities=session.capabilities | {Capability.BYPASS_PERMISSIONS},
    )
    monkeypatch.setattr(
        provider, "bypass_permissions_on", lambda _a, _s: setting["enabled"]
    )

    async def set_bypass_permissions(_account, _session, enabled):
        setting["enabled"] = enabled
        setting["calls"].append(enabled)
        return InjectResult(True)

    monkeypatch.setattr(provider, "set_bypass_permissions", set_bypass_permissions)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(base + f"/sessions/{key}")
        checkbox = page.locator('#session-bypass-permissions input[type="checkbox"]')
        await expect(checkbox).to_be_visible()
        await expect(checkbox).not_to_be_checked()
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        await checkbox.check()
        await expect(checkbox).to_be_checked()
        on = page.locator("#session-bypass-permissions .bypass-permissions-on")
        await expect(on).to_be_visible()

        await resized(page, 1200, 800)
        await checkbox.uncheck()
        await expect(checkbox).not_to_be_checked()
        await browser.close()
    assert setting["calls"] == [True, False]


def _limit_bars(html: str) -> str:
    """Just the account status panel — the rest of the page carries its own text."""
    return html.split('id="limit-bars"', 1)[1].split("</header>", 1)[0]


def _seed_meters(app):
    """Give the seeded account both real meters."""
    state = app.state.app_state
    now = datetime.now(UTC)
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=42.0,
            five_hour_resets_at=now + timedelta(hours=2),
            seven_day_pct=7.0,
            seven_day_resets_at=now + timedelta(days=4),
            fetched_at=datetime.now(UTC),
        )
    )


async def test_a_rejected_meter_credential_reads_as_sign_in_again(tmp_path):
    """#758. Cursor's meter 401'd every poll for hours while the header kept
    rendering the last good percentage with a `data-since` from before the break.
    A refused credential must read as work for a human, not as a live meter."""
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    _seed_meters(app)
    state.mark_usage_credential_rejected(
        "claude_code:test", "401 for claude_code:test (credential from host auth.json)"
    )

    async with _client(app) as client:
        bars = _limit_bars((await client.get("/chats")).text)

    assert "42%" not in bars  # the frozen number is gone, not merely greyed out
    assert "data-since" not in bars  # and so is the timestamp that aged it
    assert "sign in again to read usage" in bars
    assert "no usage data yet" not in bars  # a different, non-actionable silence
    assert "credential from host auth.json" in bars  # which credential, on the title
    assert 'class="mini-auth crit"' in bars  # the collapsed row says so too


def _app_with_metered_accounts(tmp_path, labels):
    """A deck carrying several accounts with live meters."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label=label, config_dir=str(tmp_path))
            for label in labels
        ],
    )
    app = create_app(config)
    state = app.state.app_state
    state.update_session(
        Session(
            key=f"claude_code:{labels[0]}:sid1",
            account_key=f"claude_code:{labels[0]}",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    now = datetime.now(UTC)
    for label in labels:
        key = f"claude_code:{label}"
        state.set_usage(
            UsageSnapshot(
                account_key=key,
                five_hour_pct=42.0,
                five_hour_resets_at=now + timedelta(hours=2),
                seven_day_pct=7.0,
                seven_day_resets_at=now + timedelta(days=4),
                fetched_at=now,
            )
        )
    return app


async def _open_usage_panel(page, base):
    await page.goto(base + "/")
    await page.locator(".usage-mini").wait_for()
    await page.locator(".usage-mini").click()
    await page.locator(".acct-label a").first.wait_for(state="visible")


@pytest.mark.browser
async def test_expanded_usage_panel_scrolls_instead_of_swallowing_the_screen(tmp_path):
    """#538. The panel lives inside the sticky topbar, so an unbounded one is a
    dead end: a sticky box taller than the viewport keeps its top pinned at 0 at
    every scroll offset, so its own overflow can never be scrolled to *and* it
    paints over the entire page underneath — no session card can be seen or
    tapped again until it is collapsed blind."""
    app = _app_with_metered_accounts(tmp_path, ["main", "alt", "codex"])

    # Calibration and login moved out of this sticky panel, so several accounts
    # no longer add nested disclosures here.
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 640}, service_workers="block"
        )
        await _open_usage_panel(page, base)
        assert await page.locator(".cap-toggle").count() == 0
        assert await page.locator(".acct-auth").count() == 0

        # The header never grows past the screen…
        assert await page.evaluate(
            "document.querySelector('.topbar').getBoundingClientRect().bottom <= window.innerHeight"
        )
        # …the page underneath keeps receiving taps…
        assert await page.evaluate(
            "(() => {"
            " const tb = document.querySelector('.topbar').getBoundingClientRect();"
            " const el = document.elementFromPoint(window.innerWidth / 2, tb.bottom + 40);"
            " return !!el && !el.closest('.topbar');"
            "})()"
        ), "the sticky header still covers the whole viewport"
        assert await page.locator(".acct-label a").count() == 3
        await browser.close()


@pytest.mark.browser
async def test_usage_limits_live_on_exactly_one_account_page(tmp_path):
    app = _app_with_metered_accounts(tmp_path, ["main", "alt", "codex"])

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + "/accounts/claude_code%3Aalt")
        assert await page.locator("#account-title").inner_text() == "alt"
        assert await page.locator("#account-capacity").count() == 1
        # The dollar calibration detail was removed with spend (#2864).
        assert await page.locator(".account-calibration-detail").count() == 0
        assert await page.locator(".acct:visible").count() == 0
        await browser.close()


@pytest.mark.browser
async def test_the_bell_stays_hidden_until_push_support_is_known(tmp_path):
    """An undecided bell must not widen a 320px topbar (#1368).

    The button ships hidden, and `setState('busy')` used to unhide it before
    anything knew whether push was even supported — so the topbar overflowed by
    the bell's 24px for however long `/push/public-key` took to answer. Nothing
    caught it because the workstation answers that request before the first
    measurement; a dev-cluster pod did not, and failed two unrelated tests.
    """
    app = _app_with_state(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        answer = asyncio.Event()

        async def hold(route):
            await answer.wait()
            await route.continue_()

        # Hold the support answer open: this is the window the bug lived in.
        await page.route("**/push/public-key", hold)
        await page.goto(base + "/chats")
        await page.locator(".topbar-actions").wait_for()

        assert not await page.locator(".notif-bell").is_visible()
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        answer.set()
        await browser.close()


async def _deployment_pill_metrics(page_html, width, *, shot=None):
    """Open the topbar deploy pill at one viewport and measure what it renders.

    The pill's states differ only in colour and copy, so every state's check wants
    the same geometry: the panel stays on screen and the topbar does not grow when
    it opens (AGENTS.md's mobile-height invariant). `collapsedHeight` is the
    topbar before the click, so a caller can assert the open panel changed nothing.
    """
    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = page_html.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": 800})
        await page.set_content(html)
        collapsed = await page.locator(".topbar").bounding_box()
        assert collapsed is not None
        await page.locator(".deployment-status summary").click()
        panel = page.locator(".deployment-status-panel")
        await expect(panel).to_be_visible()
        # Open is not the same as laid out, and everything below is geometry.
        await wait_until(
            page,
            "the deployment status panel finished opening",
            "() => {"
            " const p = document.querySelector('.deployment-status-panel');"
            " return !!p && p.getBoundingClientRect().width > 0;"
            "}",
        )
        metrics = await panel.evaluate(
            """panel => ({
              dot: getComputedStyle(
                document.querySelector('.deployment-status-dot')).backgroundColor,
              note: getComputedStyle(
                document.querySelector('.deployment-status-note')).color,
              left: panel.getBoundingClientRect().left,
              right: panel.getBoundingClientRect().right,
              viewport: document.documentElement.clientWidth,
              headerHeight: document.querySelector('.topbar').getBoundingClientRect().height,
              pageOverflow: document.documentElement.scrollWidth
                - document.documentElement.clientWidth,
            })"""
        )
        metrics["collapsedHeight"] = collapsed["height"]
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if shot and screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / shot))
        await browser.close()
    return metrics


@pytest.mark.browser
async def test_deployment_status_shows_frontend_and_pending_runtime_without_growing_header(
    tmp_path, monkeypatch
):
    deploy_state = tmp_path / "deploy"
    deploy_state.mkdir()
    (deploy_state / "web_sha").write_text("a" * 40 + "\n")
    (deploy_state / "runtime_sha").write_text("b" * 40 + "\n")
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(deploy_state))
    subjects = {
        "a" * 40: "Show frontend and runtime deployment status",
        "b" * 40: "Keep runtime turns alive <safely>",
    }
    monkeypatch.setattr(
        "agentdeck.deployment._commit_subject",
        lambda _repo, sha: subjects.get(sha),
    )
    app = _app_with_state(tmp_path)

    async with _client(app) as client:
        response = await client.get("/chats")

    assert 'class="deployment-status deployment-status-pending"' in response.text
    assert "Runtime deployment pending." in response.text
    assert "Show frontend and runtime deployment status" in response.text
    assert "Keep runtime turns alive &lt;safely&gt;" in response.text
    assert f"<code>{'a' * 12}</code>" not in response.text
    assert f"<code>{'b' * 12}</code>" not in response.text

    # The runtime can advance after the frontend process has started, so the
    # next render must read the durable markers again rather than show startup state.
    (deploy_state / "runtime_sha").write_text("a" * 40 + "\n")
    async with _client(app) as client:
        synchronized = await client.get("/chats")
    assert 'class="deployment-status"' in synchronized.text
    assert "deployment-status-pending" not in synchronized.text
    assert "Frontend and runtime are in sync." in synchronized.text

    metrics = await _deployment_pill_metrics(
        response.text, 320, shot="issue-675-deployment-status-mobile.png"
    )

    assert metrics["left"] >= 0
    assert metrics["right"] <= metrics["viewport"]
    assert metrics["headerHeight"] == metrics["collapsedHeight"]
    assert metrics["pageOverflow"] <= 1


async def test_an_unapplied_poller_manifest_turns_a_synced_pill_amber(tmp_path, monkeypatch):
    """#2555: the code deployed, but a project still runs older settings. That is
    not a refusal, so the pill goes amber rather than red, beside the sync line."""
    deploy_state = tmp_path / "deploy"
    deploy_state.mkdir()
    (deploy_state / "web_sha").write_text("a" * 40 + "\n")
    (deploy_state / "runtime_sha").write_text("a" * 40 + "\n")
    (deploy_state / "manifest_failed").write_text(
        "bazaar: exit 77 refused: eerovil does not hold Release authority\n"
    )
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(deploy_state))
    monkeypatch.setattr("agentdeck.deployment._commit_subject", lambda _repo, _sha: "Ship")
    app = _app_with_state(tmp_path)

    async with _client(app) as client:
        response = await client.get("/chats")

    assert 'class="deployment-status deployment-status-warn"' in response.text
    assert "Frontend and runtime are in sync." in response.text
    assert (
        "Poller settings not applied: bazaar: exit 77 refused: eerovil does not hold"
        in response.text
    )
    assert "Frontend and runtime are in sync · Poller settings not applied" in response.text


@pytest.mark.browser
async def test_a_blocked_deploy_turns_the_pill_red_and_names_the_guard(
    tmp_path, monkeypatch
):
    """#747: `agentdeck-deploy.service` refused every tick for thirteen minutes and
    the deck showed a normal pill, so the only symptom was a page 404ing. The
    refusal reason the script records has to reach the topbar, red, and it has to
    outrank the amber pending state — a blocked deploy is why the runtime is
    behind, not a second thing to read."""
    deploy_state = tmp_path / "deploy"
    deploy_state.mkdir()
    (deploy_state / "web_sha").write_text("a" * 40 + "\n")
    (deploy_state / "runtime_sha").write_text("b" * 40 + "\n")
    (deploy_state / "blocked").write_text(
        "live checkout is dirty & a deploy is <waiting>\n"
    )
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(deploy_state))
    monkeypatch.setattr(
        "agentdeck.deployment._commit_subject",
        lambda _repo, sha: {"a" * 40: "Ship the pill", "b" * 40: "Older runtime"}.get(sha),
    )
    app = _app_with_state(tmp_path)

    async with _client(app) as client:
        response = await client.get("/chats")

    assert 'class="deployment-status deployment-status-blocked"' in response.text
    assert "deployment-status-pending" not in response.text
    assert (
        "Deploy blocked: live checkout is dirty &amp; a deploy is &lt;waiting&gt;"
        in response.text
    )
    assert "Runtime deployment pending." not in response.text

    # A host that has never recorded a release still has to show a stalled deploy,
    # otherwise the one state nobody can see anywhere else stays hidden.
    (deploy_state / "web_sha").unlink()
    (deploy_state / "runtime_sha").unlink()
    async with _client(app) as client:
        bare = await client.get("/chats")
    assert "deployment-status-blocked" in bare.text
    bare_panel = bare.text.split('class="deployment-status-panel"', 1)[1].split(
        "</details>", 1
    )[0]
    assert "unknown" not in bare_panel, (
        "two 'unknown' release rows would bury the one line that matters"
    )
    assert "Deploy blocked:" in bare_panel

    # Cleared by the deploy that finally lands: back to the ordinary green pill.
    (deploy_state / "blocked").unlink()
    (deploy_state / "web_sha").write_text("a" * 40 + "\n")
    (deploy_state / "runtime_sha").write_text("a" * 40 + "\n")
    async with _client(app) as client:
        recovered = await client.get("/chats")
    assert 'class="deployment-status"' in recovered.text
    assert "deployment-status-blocked" not in recovered.text
    assert "Frontend and runtime are in sync." in recovered.text

    # 320px is the narrowest topbar this deck supports and the one with no spare
    # width; the other two are what the committed evidence shows.
    for label, width in (("narrow", 320), ("mobile", 390), ("desktop", 1100)):
        shot = None if label == "narrow" else f"issue-747-deploy-blocked-{label}.png"
        metrics = await _deployment_pill_metrics(response.text, width, shot=shot)
        # --crit, the deck's red — not --warn, which the pending state already owns.
        assert metrics["dot"] == "rgb(229, 83, 75)", label
        assert metrics["note"] == "rgb(229, 83, 75)", label
        assert metrics["left"] >= 0, label
        assert metrics["right"] <= metrics["viewport"], label
        assert metrics["headerHeight"] == metrics["collapsedHeight"], label
        assert metrics["pageOverflow"] <= 1, label


@pytest.mark.browser
async def test_quiet_background_agent_renders_as_working(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    state = app.state.app_state
    session = state.sessions["claude_code:test:sid1"]
    state.update_session(
        replace(
            session,
            title="Claude root with background agents",
            thinking=True,
            lifecycle_active=True,
            activity="Background agent",
            last_progress=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    app.state.assistant.view = AssistantView(
        state="ready",
        analyzed_at=datetime.now(UTC),
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1100, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/chats")
        card = page.locator(
            '#sessions [data-session-key="claude_code:test:sid1"]'
        )
        await card.wait_for()

        for width in (1100, 320):
            await resized(page, width, 800)
            await expect(card.locator(".dot.thinking")).to_have_attribute(
                "title", "thinking"
            )
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await resized(page, 390, 800)
            await expect(card.locator(".dot.thinking")).to_be_visible()
            await page.screenshot(
                animations="disabled",
                path=str(
                    Path(screenshot_dir)
                    / "issue-1513-background-agent-working-mobile.png"
                ),
                full_page=True,
            )
        await browser.close()


async def test_dashboard_collapsed_usage_renders_weekly_only_plan(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=None,
            five_hour_resets_at=None,
            seven_day_pct=12.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        response = await c.get("/chats")
    panel = _limit_bars(response.text)
    assert '<b class="mini-pct">—</b>' in panel
    assert '<span class="mini-window-kind">5h</span>' in panel
    assert '<b class="mini-pct">12%</b>' in panel
    assert '<span class="mini-window-kind">7d</span>' in panel


def _seed_windows(app, *, five_hour, five_hour_in, seven_day, seven_day_in):
    """One account with both rolling windows metered and their reset boundaries."""
    now = datetime.now(UTC)
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=five_hour,
            five_hour_resets_at=now + five_hour_in,
            seven_day_pct=seven_day,
            seven_day_resets_at=now + seven_day_in,
            fetched_at=now,
        )
    )


async def test_the_collapsed_line_shows_both_windows_in_provider_order(tmp_path):
    """#1157. Neither meter disappears or trades places as usage changes."""
    app = _app_with_state(tmp_path)
    _seed_windows(
        app,
        five_hour=7.0,
        five_hour_in=timedelta(hours=1),
        seven_day=96.0,
        seven_day_in=timedelta(days=1),
    )

    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    compact = re.findall(
        r'<span class="mini-window">\s*<b class="mini-pct([^\"]*)">([^<]+)</b>\s*'
        r'<span class="mini-window-kind">([^<]+)</span>',
        panel,
    )
    assert compact[:2] == [("", "7%", "5h"), (" crit", "96%", "7d")]
    # Expanded and collapsed views both retain the provider's stable order.
    assert re.findall(r'<span class="bar-kind">([^<]*)</span>', panel)[:2] == ["5h", "7d"]


async def test_a_window_burning_faster_than_it_can_last_is_flagged(tmp_path):
    """#1137. 35% is nowhere near the 70/90 colour thresholds, but 35% burned in
    the first fifth of a five-hour window will not reach the reset."""
    app = _app_with_state(tmp_path)
    # 4h15m left of 5h is 45 minutes in — the second of ten parts, and far enough
    # from either edge of it that the clock ticking between seeding and rendering
    # cannot move the row into a neighbouring part.
    _seed_windows(
        app,
        five_hour=35.0,
        five_hour_in=timedelta(hours=4, minutes=15),
        seven_day=5.0,
        seven_day_in=timedelta(days=6),
    )

    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    assert 'class="pace-flag warn"' in panel  # one icon on the collapsed line
    assert "on pace would be about 20%" in panel
    five_hour_row, weekly_row = panel.split('<span class="bar-kind">')[1:3]
    assert 'class="bar-pace warn"' in five_hour_row
    assert 'class="bar-pace"' in weekly_row  # the young week is not flagged with it


async def test_each_meter_draws_the_elapsed_share_of_its_window_inside_the_bar(tmp_path):
    """#2181. A second layer in the same track shows how far through the window
    the clock is, so usage reads against time without a taller bar."""
    app = _app_with_state(tmp_path)
    _seed_windows(
        app,
        five_hour=7.0,
        five_hour_in=timedelta(hours=4),
        seven_day=45.0,
        seven_day_in=timedelta(days=3, hours=12),
    )

    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    five_hour_row, weekly_row = panel.split('<div class="bar-row">')[1:3]
    assert 'class="elapsed" style="width: 20.0%"' in five_hour_row
    assert 'class="elapsed" style="width: 50.0%"' in weekly_row
    # Inside the track, after the usage fill, so it is drawn over it.
    bar = five_hour_row.split('<div class="bar">', 1)[1].split('<span class="bar-pct">', 1)[0]
    assert bar.index('class="fill') < bar.index('class="elapsed"')


async def test_a_window_being_spent_at_its_own_pace_is_not_flagged(tmp_path):
    """The silent case, and the reason the flag is not just another threshold:
    42% with two hours of a five-hour window left is a window well spent."""
    app = _app_with_state(tmp_path)
    _seed_meters(app)

    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    assert "pace-flag" not in panel
    assert "Projected overspend" not in panel
    assert 'class="bar-pace"' in panel  # the cell is still there, keeping columns aligned


async def test_a_meter_far_past_pace_is_critical_rather_than_a_warning(tmp_path):
    app = _app_with_state(tmp_path)
    _seed_windows(
        app,
        five_hour=60.0,
        five_hour_in=timedelta(hours=4, minutes=15),
        seven_day=5.0,
        seven_day_in=timedelta(days=6),
    )

    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    assert 'class="pace-flag crit"' in panel
    assert "pace-flag warn" not in panel


async def _usage_line_height(page):
    """Height of the collapsed usage header, measured in one page call.

    The header fragment is swapped live, so an element resolved by `wait_for`
    can already be detached when `bounding_box` runs a moment later — which read
    as a `None` box rather than as a race (#1171). Reading it inside the page and
    retrying until it has a real height keeps the measurement on one live node.
    """
    handle = await page.wait_for_function(
        "() => {"
        "  const line = document.querySelector('.usage-mini');"
        "  if (!line) return null;"
        "  const height = line.getBoundingClientRect().height;"
        "  return height > 0 ? height : null;"
        "}"
    )
    return await handle.json_value()


@pytest.mark.browser
async def test_the_overspend_flag_stays_on_the_phone_line_without_growing_it(tmp_path):
    """#1137 against the header's vertical-space rule. The flag is the reason to
    open the panel, so unlike the secondary percentage it survives 320px — and it
    rides the existing line rather than adding a second one."""
    app = _app_with_state(tmp_path)
    _seed_windows(
        app,
        five_hour=35.0,
        five_hour_in=timedelta(hours=4, minutes=15),
        seven_day=5.0,
        seven_day_in=timedelta(days=6),
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/chats")
        flag = page.locator(".pace-flag")
        await flag.wait_for(state="visible")
        flagged_height = await _usage_line_height(page)

        # Same account back on pace: the icon goes, and the line it left is the
        # same height it was carrying the icon.
        _seed_windows(
            app,
            five_hour=35.0,
            five_hour_in=timedelta(hours=1),
            seven_day=5.0,
            seven_day_in=timedelta(days=6),
        )
        await page.goto(base + "/chats")
        # Measured through the same helper rather than through a `.mini-pct`
        # wait: #1157 gave every provider window its own percentage, so that
        # locator now matches more than one element and cannot settle anything.
        assert await _usage_line_height(page) == flagged_height
        assert await page.locator(".pace-flag").count() == 0
        await browser.close()


async def test_a_billing_cycle_account_shows_its_cycle_and_no_empty_rolling_rows(
    tmp_path, monkeypatch
):
    """A provider that meters a billing cycle, not 5h/7d (#730), gets that window
    drawn — and only that one, so the other accounts do not grow a third
    permanently-empty bar on a phone screen. No shipped provider declares one since
    Cursor went (#2600), so a stub stands in for it."""
    from agentdeck.models import MONTH
    from agentdeck.providers import PROVIDERS
    from agentdeck.providers.codex.provider import CodexProvider

    class BillingCycleProvider(CodexProvider):
        provider_id = "billing"
        usage_windows = (MONTH,)

    monkeypatch.setitem(PROVIDERS, "billing", BillingCycleProvider())
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path)),
            AccountConfig(provider="billing", label="curs", config_dir=str(tmp_path)),
        ],
    )
    app = create_app(config)
    state = app.state.app_state
    now = datetime.now(UTC)
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=42.0,
            five_hour_resets_at=now + timedelta(hours=2),
            seven_day_pct=7.0,
            seven_day_resets_at=now + timedelta(days=4),
            fetched_at=now,
        )
    )
    state.set_usage(
        UsageSnapshot(
            account_key="billing:curs",
            five_hour_pct=None,
            five_hour_resets_at=None,
            seven_day_pct=None,
            seven_day_resets_at=None,
            fetched_at=now,
            month_pct=63.0,
            month_resets_at=now + timedelta(days=28),
        )
    )
    async with _client(app) as c:
        panel = _limit_bars((await c.get("/chats")).text)

    def kinds(block: str) -> list[str]:
        return re.findall(r'<span class="bar-kind">([^<]*)</span>', block)

    cycle_block = panel.split(">curs</a>", 1)[1]
    claude_block = panel.split(">test</a>", 1)[1].split(">curs</a>", 1)[0]
    assert kinds(cycle_block) == ["mo"]
    assert kinds(claude_block) == ["5h", "7d"]
    # Collapsed line: a provider with one window gets the same labeled compact row,
    # so the figure is not an unexplained bare percentage.
    assert '<b class="mini-pct">63%</b>' in panel
    assert '<span class="mini-window-kind">mo</span>' in panel


def _three_attention_cards(app):
    """Seed three carded chats — one of them paused on a question, so the bulk
    press covers both dismissal kinds — and return their keys."""
    state = app.state.app_state
    assistant = app.state.assistant
    assistant.config.enabled = True
    keys = []
    for session_id, title, question in (
        ("sid1", "Hello World Session", None),
        ("sid2", "Second Session", None),
        ("sid3", "Third Session", "Which branch should I ship?"),
    ):
        key = f"claude_code:test:{session_id}"
        state.update_session(
            Session(
                key=key,
                account_key="claude_code:test",
                session_id=session_id,
                status=SessionStatus.LIVE,
                title=title,
                question=question,
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )
        keys.append(key)
    assistant.view = AssistantView(
        state="ready",
        insights=tuple(
            AssistantInsight(session_key=key, kind="finished", headline="finished")
            for key in keys
        ),
    )
    return keys


def _static_route_body(static_dir: Path, path: str):
    """(content type, body) for one shipped asset, or None when it is not one."""
    if not path.startswith("/static/"):
        return None
    asset = static_dir / path.rsplit("/", 1)[1]
    if not asset.is_file():
        return None
    kind = "text/css" if asset.suffix == ".css" else "text/javascript"
    return kind, asset.read_text()


@pytest.mark.browser
async def test_host_usage_fits_collapsed_header_with_both_windows(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.accounts.extend(
        [
            Account("claude_code:alt", "claude_code", "alt", tmp_path),
            Account("codex:codex", "codex", "codex", tmp_path),
        ]
    )
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=23.0,
            memory_pct=61.0,
            memory_used_bytes=20 * 1024**3,
            memory_total_bytes=32 * 1024**3,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _client(app) as client:
        response = await client.get("/chats")

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 412, "height": 800})
        await page.set_content(html)
        metrics = []
        widths = (320, 360, 412, 1100)
        for width in widths:
            await resized(page, width, 800)
            metrics.append(
                await page.evaluate(
                    """() => {
                        const usage = document.querySelector('.usage-mini');
                        const host = usage.querySelector('.mini-host');
                        return {
                            height: usage.getBoundingClientRect().height,
                            usageRight: usage.getBoundingClientRect().right,
                            hostRight: host.getBoundingClientRect().right,
                            hostDisplay: getComputedStyle(host).display,
                            caretDisplay: getComputedStyle(
                              usage.querySelector('.mini-caret')
                            ).display,
                            clientWidth: usage.clientWidth,
                            scrollWidth: usage.scrollWidth,
                            compactWindows: Array.from(
                              usage.querySelectorAll('.mini-acct:first-of-type .mini-window')
                            ).map(window => ({
                              visible: getComputedStyle(window).display !== 'none',
                              kind: window.querySelector('.mini-window-kind')?.textContent,
                            })),
                        };
                    }"""
                )
            )
        await browser.close()

    for width, size in zip(widths, metrics, strict=True):
        assert size["hostRight"] < size["usageRight"]
        assert size["height"] < 40
        assert size["hostDisplay"] == "flex"
        if width <= 480:
            assert size["scrollWidth"] <= size["clientWidth"]
            assert size["caretDisplay"] == "none"
        assert size["compactWindows"] == [
            {"visible": True, "kind": "5h"},
            {"visible": True, "kind": "7d"},
        ]


@pytest.mark.browser
async def test_mobile_session_header_emphasizes_current_account(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.accounts.append(
        Account("claude_code:alt", "claude_code", "alt", tmp_path)
    )
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.evaluate(
            """() => {
                const accounts = [...document.querySelectorAll('.mini-acct')];
                const current = document.querySelector('.mini-acct[aria-current="true"]');
                return {
                    accountCount: accounts.length,
                    currentCount: accounts.filter(account =>
                        account.getAttribute('aria-current') === 'true').length,
                    currentLabel: current.querySelector('.mini-label').textContent.trim(),
                    currentWeight: Number(getComputedStyle(
                        current.querySelector('.mini-label')
                    ).fontWeight),
                    otherWeight: Number(getComputedStyle(
                        accounts.find(account => account !== current)
                            .querySelector('.mini-label')
                    ).fontWeight),
                    headerHeight: document.querySelector('.usage-mini')
                        .getBoundingClientRect().height,
                    overflow: document.documentElement.scrollWidth
                        - document.documentElement.clientWidth,
                };
            }"""
        )
        await browser.close()

    assert metrics["accountCount"] == 2
    assert metrics["currentCount"] == 1
    assert metrics["currentLabel"] == "test"
    assert metrics["currentWeight"] == 800
    assert metrics["otherWeight"] == 400
    assert metrics["headerHeight"] < 40
    assert metrics["overflow"] == 0


def test_worker_type_classification():
    assert worker_type(True, False) == "kanban"  # kanban dispatch always wins
    assert worker_type(True, True) == "kanban"  # even when also RC-spawned
    assert worker_type(False, True) == "cloud"  # cloud/RC, non-kanban
    assert worker_type(False, False) == "you"  # your own interactive chat


async def test_card_colour_class_and_direct_claudeai_button(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:kb1",
            account_key="claude_code:test",
            session_id="kb1",
            status=SessionStatus.LIVE,
            title="store#2728 · Fix things",
            worker_type="kanban",
            deep_link="https://claude.ai/code/session_kb1",
            issue_url="https://github.com/Northwind/store/issues/2728",
            issue_status="open",
            issue_status_kind="open",
            last_role="agent",
            question="Should I push both commits?",
        )
    )
    # A chat with an issue is in a Work, which the chats view leaves out (#2256): read
    # the card itself.
    r = _CardList(_card_list(app))
    # Worker-type colour class on the card, and the default session (no
    # worker_type) falls back to the "you" stripe.
    assert "wt-kanban" in r.text
    assert "wt-you" in r.text
    # A direct claude.ai button, wired to the deep link — not the transcript URL.
    assert 'class="cc-btn"' in r.text
    assert 'data-href="https://claude.ai/code/session_kb1"' in r.text
    # And a GitHub issue button wired to the issue URL.
    assert 'class="gh-btn"' in r.text
    assert 'data-href="https://github.com/Northwind/store/issues/2728"' in r.text
    # The one work-item pill, naming the issue because this chat has no PR (#1690).
    assert "issue open" in r.text
    assert 'data-work-open="1"' in r.text
    # Trailing agent question surfaced separately.
    assert 'class="card-question"' in r.text
    assert "Should I push both commits?" in r.text






async def test_session_card_shows_deckhand_status_pill(tmp_path):
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    # One row per resolution branch:
    #   live      — a live waiting insight
    #   finished  — a finished handoff
    #   merged    — a finished handoff whose registered PR merged
    #   working   — mid-turn (no pill)
    #   idle      — resting without a handoff ("?")
    #   asking    — a pending question (waiting, straight off the session)
    #   subagent  — delegated child without an attention pill
    sessions = {
        "live": dict(last_role="agent", thinking=False),
        "finished": dict(last_role="agent", thinking=False),
        "merged": dict(last_role="agent", thinking=False),
        "working": dict(last_role="agent", thinking=True),
        "idle": dict(last_role="agent", thinking=False),
        "asking": dict(last_role="agent", thinking=False, question="Which option?"),
        "subagent": dict(last_role="agent", thinking=False, is_delegated=True),
    }
    for sid, extra in sessions.items():
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                **extra,
            )
        )
    app.state.app_state.recorded_delegation_parents[
        "claude_code:test:subagent"
    ] = "finished"
    # A merged PR changes only a finished Deckhand handoff to merged.
    assistant._contexts.update(
        {
            "claude_code:test:merged": GitContext(
                "acme/app", "feat", False,
                (PullRequestContext("acme/app", 42, "Ship it", "u", "merged"),),
            ),
        }
    )
    assistant.view = AssistantView(
        state="ready",
        insights=(
            AssistantInsight("claude_code:test:live", "waiting", "waiting"),
            AssistantInsight("claude_code:test:finished", "finished", "finished"),
            AssistantInsight("claude_code:test:merged", "finished", "finished"),
            AssistantInsight("claude_code:test:subagent", "finished", "finished"),
        ),
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    # These chats have no issue and no pull request, so the state has no work item
    # to sit on and the pill shrinks to a colour-only dot (#1690). The state is
    # never spelled out any more; the tooltip is where the words live.
    assert 'class="dh-pill dh-waiting dh-dot"' in r.text
    assert 'title="Deckhand: waiting"' in r.text
    assert 'class="dh-pill dh-finished dh-dot"' in r.text
    assert 'title="Deckhand: finished"' in r.text
    # Nothing can be marked done any more (#2599).
    assert "dh-done" not in r.text
    # Merged PR -> one merged Deckhand colour instead of a finished + merged pair,
    # and this row does have a work item, so the colour rides the pull request.
    assert r.text.count('class="dh-pill dh-finished dh-dot"') == 1
    merged_card = _session_card(r.text, "claude_code:test:merged")
    assert 'class="dh-pill dh-merged"' in merged_card
    assert "pr merged" in merged_card
    assert 'title="pull request #42 is merged · Deckhand: merged"' in merged_card
    # Only the chat that actually shipped carries it.
    assert r.text.count("dh-pill dh-merged") == 1
    # A resting chat that never crossed a stop edge has no Deckhand state.
    assert "dh-unknown" not in r.text
    subagent_card = _session_card(r.text, "claude_code:test:subagent")
    assert "Session subagent" in subagent_card
    assert "dh-pill" not in subagent_card


async def test_session_card_names_the_pull_request_or_the_issue_but_never_both(tmp_path):
    # #1690: one small pill per row. A chat that has reached a pull request is
    # read through it; a chat that has not is read through its issue. The row
    # never wears a GitHub badge beside the pill any more.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    for sid in ("shipping", "planning"):
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                issue_url="https://github.com/eerovil/agentdeck/issues/1690",
                issue_status="open",
                issue_status_kind="open",
                last_role="agent",
                thinking=False,
            )
        )
    assistant._contexts.update(
        {
            "claude_code:test:shipping": GitContext(
                "eerovil/agentdeck", "agent/issue-1690", False,
                (PullRequestContext(
                    "eerovil/agentdeck", 1691, "Ship it", "u", "open"
                ),),
            ),
        }
    )
    # Both chats have an issue, so both are in a Work, which the chats view leaves out
    # (#2256): read the cards themselves.
    r = _CardList(_card_list(app))

    shipping = _session_card(r.text, "claude_code:test:shipping")
    assert "pr open" in shipping
    assert "issue open" not in shipping
    assert shipping.count("dh-pill") == 1
    assert 'data-work-open="1"' in shipping

    # No pull request yet, so this row speaks for the issue instead.
    planning = _session_card(r.text, "claude_code:test:planning")
    assert "issue open" in planning
    assert "pr open" not in planning
    assert planning.count("dh-pill") == 1
    # The old flat GitHub badge is gone from the row entirely.
    assert "st-badge" not in shipping + planning


async def test_a_reviewed_head_ticks_the_row_pill_on_the_card(tmp_path):
    # #2121: the deck already records what a review decided about one commit, but
    # only the chat's own page said so. The row says it now, on the one pill it
    # already has, and only while that commit is still the pull request's head —
    # so "reviewed" on a row always means "what would merge has passed".
    from agentdeck.models import PollerReviewVerdict

    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    for sid in ("passed", "moved"):
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                last_role="agent",
                thinking=False,
            )
        )
    assistant._contexts.update(
        {
            f"claude_code:test:{sid}": GitContext(
                "eerovil/agentdeck",
                f"agent/issue-{number}",
                False,
                (
                    PullRequestContext(
                        "eerovil/agentdeck",
                        number,
                        "Ship it",
                        "u",
                        "open",
                        head_sha=sha,
                    ),
                ),
                head_sha=sha,
            )
            for sid, number, sha in (
                ("passed", 2121, "a" * 40),
                ("moved", 2122, "b" * 40),
            )
        }
    )
    # One verdict, about the commit `passed` is on and the commit `moved` has left.
    app.state.app_state.record_review_verdict(
        PollerReviewVerdict(
            repo="eerovil/agentdeck",
            sha="a" * 40,
            verdict="pass",
            result="release",
            recorded_at=datetime.now(UTC),
        )
    )
    assistant.view = AssistantView(
        state="ready",
        insights=tuple(
            AssistantInsight(f"claude_code:test:{sid}", "waiting", "waiting")
            for sid in ("passed", "moved")
        ),
    )

    async with _client(app) as c:
        r = await c.get("/chats")

    def pill(sid: str) -> str:
        # The row's own "mark done" button is a ✓ too, so the tick has to be
        # asserted on the pill and nowhere else.
        card = _session_card(r.text, f"claude_code:test:{sid}")
        assert card.count("dh-pill") == 1  # one pill, never a second chip
        return '<span class="dh-pill' + card.split('<span class="dh-pill', 1)[1].split(
            "</span>", 1
        )[0]

    passed = pill("passed")
    assert ">pr open ✓" in passed
    assert "dh-reviewed" in passed
    assert 'data-reviewed-sha="aaaaaaa"' in passed
    assert "latest commit aaaaaaa passed review" in passed
    # The review clause travels inside the work half of the title, before " · ".
    title = passed.split('title="', 1)[1].split('"', 1)[0]
    assert title.split(" · ")[0] == (
        "pull request #2121 is open (latest commit aaaaaaa passed review)"
    )
    assert title.split(" · ")[1] == "Deckhand: waiting"

    moved = pill("moved")
    assert ">pr open" in moved
    assert "✓" not in moved
    assert "dh-reviewed" not in moved

    # The tick is a glyph, and a glyph says nothing to a screen reader (#2125
    # review). The card's pill carries an accessible name that explains it and
    # names the commit — and `role="img"` is what makes that name count, because a
    # bare span is `role="generic"`, which does not support naming at all.
    assert 'role="img"' in passed
    label = passed.split('aria-label="', 1)[1].split('"', 1)[0]
    assert label == (
        "pull request #2121 is open (latest commit aaaaaaa passed review)"
        " · Deckhand: waiting"
    )
    # The unreviewed row is named too, and its name claims no review.
    assert 'role="img"' in moved
    moved_label = moved.split('aria-label="', 1)[1].split('"', 1)[0]
    assert moved_label == "pull request #2122 is open · Deckhand: waiting"
    # Pointer and screen reader are told the same thing, from one string.
    assert f'title="{label}"' in passed
    # The row whose head moved says nothing about a review anywhere.
    assert "passed review" not in moved


async def test_a_closed_work_item_is_marked_for_the_hide_closed_filter(tmp_path):
    # The list's "hide closed" filter reads this attribute rather than a colour
    # class, so a closed issue and a merged pull request must both carry the
    # closed mark while an open one does not (#1690).
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:done",
            account_key="claude_code:test",
            session_id="done",
            status=SessionStatus.LIVE,
            title="Session done",
            issue_url="https://github.com/eerovil/agentdeck/issues/1690",
            issue_status="closed",
            issue_status_kind="closed",
            last_role="agent",
            thinking=False,
        )
    )
    card = _session_card(_card_list(app), "claude_code:test:done")
    assert 'data-work-open="0"' in card
    assert "issue closed" in card


@pytest.mark.browser
async def test_merged_status_needs_a_real_merge_and_repaints_the_card(tmp_path):
    # Issue #567: the merged status is derived from the session's registered PRs,
    # so a PR closed without merging must not claim the work shipped — and a
    # merge landing later has to change the card revision, or the live list
    # would keep the pre-merge card mounted and the status would never appear.
    from agentdeck.web.render import render_session_groups, session_list_context

    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"
    state = app.state.app_state
    state.update_session(
        replace(
            state.sessions[key],
            title="agentdeck#567 · Add merged Deckhand status",
            last_prompt="Show merged only when Deckhand would otherwise say finished.",
            last_text="The pull request merged and the handoff is ready to acknowledge.",
            last_role="agent",
            thinking=False,
            issue_status="In Review",
            issue_status_kind="open",
        )
    )
    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(key, "finished", "finished"),),
    )

    def context():
        return session_list_context(
            app.state.accounts,
            state.session_presentation(),
            injector=app.state.injector,
            assistant=assistant,
        )

    assistant._contexts.update(
        {
            key: GitContext(
                "acme/app", "feat", False,
                (PullRequestContext("acme/app", 7, "Abandoned", "u", "closed"),),
            )
        }
    )
    abandoned = context()
    abandoned_group = render_session_groups(app.state.templates, abandoned)[0]
    assert abandoned["deckhand_status"][key].state == "finished"

    assistant._contexts.update(
        {
            key: GitContext(
                "acme/app", "feat", False,
                (PullRequestContext("acme/app", 7, "Shipped", "u", "merged"),),
            )
        }
    )
    shipped = context()
    shipped_group = render_session_groups(app.state.templates, shipped)[0]
    assert shipped["deckhand_status"][key].state == "merged"
    assert shipped_group != abandoned_group

    async with _client(app) as c:
        r = await c.get("/chats")
    card = _session_card(r.text, key)
    assert 'class="dh-pill dh-merged"' in card
    # The pill's words are the work item; the state's words moved into the second
    # half of the tooltip (#1690).
    assert 'title="pull request #7 is merged · Deckhand: merged"' in card
    assert "pr merged" in card
    assert assistant.view.insights[0].kind == "finished"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport_width in (390, 1100):
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": 844},
                service_workers="block",
            )
            await page.goto(base + "/chats")
            merged = page.locator(f'#sessions [data-session-key="{key}"] .dh-pill.dh-merged')
            await merged.wait_for(state="visible")
            assert await merged.inner_text() == "PR MERGED"
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir:
                await page.screenshot(
                    path=str(
                        Path(screenshot_dir)
                        / f"issue-567-merged-status-{viewport_width}.png"
                    )
                )
            await page.close()
        await browser.close()


async def test_merged_status_follows_the_dispatched_issue(tmp_path):
    # Issue #1744: a chat dispatched straight at a pull request has no *registered*
    # pull request of its own, so `has_merged_pr` is false for it. Its merge is
    # carried by the dispatched item's own badge, and the row used to keep the
    # green `finished` colour while its pill already read "issue merged".
    from agentdeck.web.render import session_list_context

    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"
    state = app.state.app_state

    def put(issue_status: str, issue_status_kind: str):
        state.update_session(
            replace(
                state.sessions[key],
                title="agentdeck#1730 · Finish control_db rename",
                last_prompt="Say merged when the work item merged.",
                last_text="The pull request merged and the handoff is ready.",
                last_role="agent",
                thinking=False,
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
            )
        )

    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(key, "finished", "finished"),),
    )

    def context():
        return session_list_context(
            app.state.accounts,
            state.session_presentation(),
            injector=app.state.injector,
            assistant=assistant,
        )

    put("open", "open")
    still_open = context()
    assert still_open["deckhand_status"][key].state == "finished"

    put("merged", "merged")
    merged = context()
    assert merged["deckhand_status"][key].state == "merged"

    async with _client(app) as c:
        r = await c.get("/chats")
    card = _session_card(r.text, key)
    assert 'class="dh-pill dh-merged"' in card
    # Presentation only: the attention kind stays `finished`.
    assert assistant.view.insights[0].kind == "finished"


@pytest.mark.browser
async def test_closed_status_follows_the_dispatched_issue(tmp_path):
    # Issue #1113: a finished handoff whose issue is closed reads `closed`, the
    # same way a merged pull request reads `merged`. It is derived from the
    # session's own issue badge, so an open issue must keep saying `finished`.
    from agentdeck.web.render import render_session_groups, session_list_context

    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"
    state = app.state.app_state

    def put(issue_status: str, issue_status_kind: str):
        state.update_session(
            replace(
                state.sessions[key],
                title="agentdeck#1113 · Add closed Deckhand status",
                last_prompt="Say closed when the issue is closed.",
                last_text="The issue is closed and the handoff is ready to acknowledge.",
                last_role="agent",
                thinking=False,
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
            )
        )

    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(key, "finished", "finished"),),
    )

    def context():
        return session_list_context(
            app.state.accounts,
            state.session_presentation(),
            injector=app.state.injector,
            assistant=assistant,
        )

    put("open", "open")
    still_open = context()
    open_group = render_session_groups(app.state.templates, still_open)[0]
    assert still_open["deckhand_status"][key].state == "finished"

    put("closed", "done")
    closed = context()
    closed_group = render_session_groups(app.state.templates, closed)[0]
    assert closed["deckhand_status"][key].state == "closed"
    # The row has to repaint, or the live list keeps the pre-close card mounted.
    assert closed_group != open_group

    async with _client(app) as c:
        r = await c.get("/chats")
    card = _session_card(r.text, key)
    assert 'class="dh-pill dh-closed"' in card
    # The state's words moved into the tooltip behind the work item (#1690).
    assert "· Deckhand: closed" in card
    # Presentation only: the attention kind stays `finished`.
    assert assistant.view.insights[0].kind == "finished"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport_width in (390, 1100):
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": 844},
                service_workers="block",
            )
            await page.goto(base + "/chats")
            pill = page.locator(
                f'#sessions [data-session-key="{key}"] .dh-pill.dh-closed'
            )
            await pill.wait_for(state="visible")
            # The pill spells out the work item now; its closed state is the
            # colour class the selector above already asserts (#1690).
            assert await pill.inner_text() == "ISSUE CLOSED"
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir:
                await page.screenshot(
                    path=str(
                        Path(screenshot_dir)
                        / f"issue-1113-closed-status-{viewport_width}.png"
                    )
                )
            await page.close()
        await browser.close()


@pytest.mark.browser
async def test_closed_status_replaces_waiting_presentation(tmp_path):
    # Issue #1270: closing the work item replaces a stale waiting label in both
    # rendered locations without changing the durable attention kind.
    from agentdeck.web.render import render_session_groups, session_list_context

    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"
    state = app.state.app_state

    def put(issue_status: str, issue_status_kind: str):
        state.update_session(
            replace(
                state.sessions[key],
                title="agentdeck#1270 · Show closed status while waiting",
                last_prompt="Show closed when the issue closes, even while waiting.",
                last_text="I still need an answer, but the issue has already closed.",
                last_role="agent",
                thinking=False,
                question="Should I continue?",
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
            )
        )

    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight(key, "waiting", "waiting"),),
    )

    def context():
        return session_list_context(
            app.state.accounts,
            state.session_presentation(),
            injector=app.state.injector,
            assistant=assistant,
        )

    put("open", "open")
    still_open = context()
    open_group = render_session_groups(app.state.templates, still_open)[0]
    assert still_open["deckhand_status"][key].state == "waiting"
    assert still_open["deckhand_status"][key].title == "Deckhand: waiting"

    put("closed", "done")
    closed = context()
    closed_group = render_session_groups(app.state.templates, closed)[0]
    assert closed["deckhand_status"][key].state == "closed"
    assert closed["deckhand_status"][key].title == "Deckhand: closed"
    assert closed_group != open_group

    async with _client(app) as client:
        dashboard = await client.get("/chats")
    card = _session_card(dashboard.text, key)
    assert 'class="dh-pill dh-closed"' in card
    assert assistant.view.insights[0].kind == "waiting"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport_width in (390, 1100):
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": 844},
                service_workers="block",
            )
            await page.goto(base + "/chats")
            pill = page.locator(
                f'#sessions [data-session-key="{key}"] .dh-pill.dh-closed'
            )
            await pill.wait_for(state="visible")
            # The pill spells out the work item now; its closed state is the
            # colour class the selector above already asserts (#1690).
            assert await pill.inner_text() == "ISSUE CLOSED"
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir:
                await page.screenshot(
                    path=str(
                        Path(screenshot_dir)
                        / f"issue-1270-closed-waiting-{viewport_width}.png"
                    )
                )
            await page.close()
        await browser.close()


async def test_hide_closed_reads_the_work_item_pill(tmp_path, monkeypatch):
    # The chats view leaves a Work's chats out (#2256); this is about how the card
    # itself renders, so every chat is listed here.
    _list_every_chat(monkeypatch)
    # The filter used to look for a GitHub badge that is not rendered any more,
    # so it now reads the work-item pill's own open/closed mark (#1690). A merged
    # pull request counts as closed; a chat with no work item is never hidden.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    for sid in ("open", "merged", "bare"):
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                issue_url=(
                    None if sid == "bare"
                    else "https://github.com/eerovil/agentdeck/issues/1690"
                ),
                issue_status=None if sid == "bare" else "open",
                issue_status_kind=None if sid == "bare" else "open",
                last_role="agent",
                thinking=False,
            )
        )
    assistant._contexts.update(
        {
            "claude_code:test:merged": GitContext(
                "eerovil/agentdeck", "agent/issue-1690", False,
                (PullRequestContext(
                    "eerovil/agentdeck", 1691, "Ship it", "u", "merged"
                ),),
            ),
        }
    )
    async with _client(app) as c:
        everything = await c.get("/chats")
        open_only = await c.get("/chats", params={"closed": "0"})

    assert {"open", "merged", "bare"} <= _session_keys(everything.text, short=True)
    assert 'data-work-open="0"' in _session_card(everything.text, "claude_code:test:merged")
    hidden = _session_keys(open_only.text, short=True)
    assert "merged" not in hidden and {"open", "bare"} <= hidden


def _multi_account_deck(tmp_path):
    """A deck spanning two accounts, two projects and all three sources."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path)),
            AccountConfig(provider="codex", label="alt", config_dir=str(tmp_path)),
        ],
    )
    app = create_app(config)
    rows = [
        ("claude_code:main", "mine", "you", "/repos/agentdeck", "Deck polish"),
        ("claude_code:main", "worker", "kanban", "/repos/agentdeck", "Kanban worker"),
        ("claude_code:main", "spawned", "cloud", "/repos/bazaar", "Agent spawned"),
        ("codex:alt", "codexmine", "you", "/repos/bazaar", "Bazaar fix"),
    ]
    for account_key, sid, source, cwd, title in rows:
        app.state.app_state.update_session(
            Session(
                key=f"{account_key}:{sid}",
                account_key=account_key,
                session_id=sid,
                status=SessionStatus.LIVE,
                title=title,
                cwd=Path(cwd),
                worker_type=source,
                last_role="agent",
            )
        )
    return app


async def test_account_project_and_source_filters_narrow_the_deck(tmp_path, monkeypatch):
    # Issue #355, applied by the server since #2603: each picker narrows the list,
    # within a picker the choices add up, and across pickers they narrow together.
    monkeypatch.setattr("agentdeck.web.work_view.work_chat_ids", lambda app: frozenset())
    app = _multi_account_deck(tmp_path)
    async with _client(app) as c:
        def keys(params):
            return c.get("/chats", params=params)

        everything = await keys({})
        alt = await keys({"account": "codex:alt"})
        bazaar = await keys({"project": "bazaar"})
        workers = await keys({"source": ["kanban", "cloud"]})
        both = await keys({"project": "bazaar", "source": "you"})

    assert _session_keys(everything.text, short=True) >= {
        "mine", "worker", "spawned", "codexmine"
    }
    assert _session_keys(alt.text, short=True) == {"codexmine"}
    assert _session_keys(bazaar.text, short=True) == {"spawned", "codexmine"}
    assert _session_keys(workers.text, short=True) == {"worker", "spawned"}
    assert _session_keys(both.text, short=True) == {"codexmine"}
    # Two accounts, so the account picker is offered; the panel opens on a filtered load.
    assert 'name="account" value="codex:alt" checked' in alt.text
    assert '<div id="filter-panel" class="filter-panel">' in alt.text
    assert '<div id="filter-panel" class="filter-panel" hidden>' in everything.text


async def test_the_chats_page_shows_deckhand_pills_on_first_render(tmp_path):
    # Regression: a page that built its list context by hand omitted deckhand_status,
    # so a card flashed "?" for real verdicts until SSE corrected it. Nothing corrects
    # it now (#2603), so the pill must be right in the page itself.
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1", account_key="claude_code:test",
            session_id="sid1", status=SessionStatus.LIVE, title="Sid1",
            last_role="agent", thinking=False,
        )
    )
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight("claude_code:test:sid1", "finished", "finished"),),
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    # No issue and no pull request, so the state is the dot's colour (#1690).
    assert 'class="dh-pill dh-finished dh-dot"' in r.text
    assert 'title="Deckhand: finished"' in r.text


@pytest.mark.browser
async def test_deckhand_pill_fits_narrow_mobile(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:m1", account_key="claude_code:test",
            session_id="m1", status=SessionStatus.LIVE,
            title="A reasonably long session title that might wrap on a phone",
            last_role="agent", thinking=False,
            issue_status="open", issue_status_kind="open",
            cwd=Path("/home/me/some/deep/project/dir"),
            kind="chat", context_tokens=123456,
        )
    )
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight("claude_code:test:m1", "waiting", "waiting"),),
    )
    async with _client(app) as client:
        response = await client.get("/chats")
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        size = await page.evaluate(
            """() => {
              const pill = document.querySelector('.dh-pill');
              const card = pill && pill.closest('.session');
              return {
                pillVisible: !!pill && pill.getBoundingClientRect().width > 0,
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
                cardOverflow: card ? card.scrollWidth - card.clientWidth : 0,
              };
            }"""
        )
        await browser.close()
    # The pill renders, and neither the page nor its card overflows at 320px.
    assert size["pillVisible"]
    assert size["bodyOverflow"] <= 1
    assert size["cardOverflow"] <= 1


@pytest.mark.browser
async def test_long_card_title_gets_full_width_three_line_layout(tmp_path, monkeypatch):
    # The chats view leaves a Work's chats out (#2256); this is about how the card
    # itself renders, so every chat is listed here.
    _list_every_chat(monkeypatch)
    app = _app_with_state(tmp_path)
    long_title = (
        "store#2728 · Investigate refund validation failures and deploy the durable fix"
    )
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title=long_title,
            issue_url="https://github.com/example/store/issues/2728",
            deep_link="https://claude.ai/code/session_sid1",
            deep_link_label="open in claude.ai",
            last_prompt="Please deploy it",
        )
    )
    async with _client(app) as client:
        response = await client.get("/chats")

    assert response.text.index('class="session-actions"') < response.text.index(
        'class="session-main"'
    )
    assert f'class="card-title" title="{long_title}"' in response.text

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.locator('.session[data-session-key="claude_code:test:sid1"]').evaluate(
            """card => {
              const title = card.querySelector('.card-title');
              const actions = card.querySelector('.session-actions');
              const cardRect = card.getBoundingClientRect();
              const titleRect = title.getBoundingClientRect();
              return {
                clamp: getComputedStyle(title).webkitLineClamp,
                titleWidth: titleRect.width,
                cardWidth: cardRect.width,
                actionsBottom: actions.getBoundingClientRect().bottom,
                titleTop: titleRect.top,
                scrollWidth: document.documentElement.scrollWidth,
                viewportWidth: document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert metrics["clamp"] == "3"
    assert metrics["titleWidth"] > metrics["cardWidth"] * 0.8
    assert metrics["actionsBottom"] <= metrics["titleTop"]
    assert metrics["scrollWidth"] <= metrics["viewportWidth"]


@pytest.mark.browser
async def test_generated_title_starts_with_project_in_existing_card_header(tmp_path, monkeypatch):
    # The chats view leaves a Work's chats out (#2256); this is about how the card
    # itself renders, so every chat is listed here.
    _list_every_chat(monkeypatch)
    app = _app_with_state(tmp_path)
    session = Session(
        key="codex:test:generated",
        account_key="codex:test",
        session_id="generated",
        status=SessionStatus.LIVE,
        cwd=tmp_path / "agentdeck" / ".worktrees" / "issue-159",
        title="Requested changes",
        issue_url="https://github.com/eerovil/agentdeck/issues/159",
        deep_link="https://chatgpt.com/codex/session",
        deep_link_label="open in Codex",
        last_prompt="Implement the requested title change",
        subagent_count=2,
    )
    app.state.app_state.update_session(session)
    app.state.app_state.set_generated_title(session.key, "Prefix generated titles", "sig")

    async with _client(app) as client:
        response = await client.get("/chats")

    card = _session_card(response.text, session.key)
    assert card.index('class="session-project"') < card.index('class="card-title"')
    assert '>agentdeck</span>' in card
    assert 'title="agentdeck#159 · Prefix generated titles"' in card
    assert '>#159 · Prefix generated titles</span>' in card

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.locator(
            '.session[data-session-key="codex:test:generated"]'
        ).evaluate(
            """card => {
              const project = card.querySelector('.session-project');
              const title = card.querySelector('.card-title');
              return {
                projectText: project.textContent.trim(),
                projectWidth: project.getBoundingClientRect().width,
                projectBottom: project.getBoundingClientRect().bottom,
                titleTop: title.getBoundingClientRect().top,
                scrollWidth: document.documentElement.scrollWidth,
                viewportWidth: document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert metrics["projectText"] == "agentdeck"
    assert metrics["projectWidth"] >= 72
    assert metrics["projectBottom"] <= metrics["titleTop"]
    assert metrics["scrollWidth"] <= metrics["viewportWidth"]


async def test_pwa_routes(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw = await c.get("/sw.js")
        mf = await c.get("/manifest.webmanifest")
        dash = await c.get("/chats")
    # Service worker: served from root (so its scope covers the whole app),
    # with the correct JS type and the scope-broadening header.
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("text/javascript")
    assert sw.headers["service-worker-allowed"] == "/"
    assert "addEventListener('fetch'" in sw.text
    # The cache name is stamped with a content hash (no unresolved placeholder),
    # so any asset change busts the SW cache without a manual version bump.
    assert "__CACHE_STAMP__" not in sw.text
    assert "agentdeck-static-" in sw.text
    assert "/static/mobile_session_stack.js" in sw.text
    assert "/static/chats_page.js" in sw.text
    assert "/static/message_collapse.js" in sw.text
    # Manifest: correct content type + installability essentials.
    assert mf.status_code == 200
    assert mf.headers["content-type"].startswith("application/manifest+json")
    body = mf.json()
    assert body["display"] == "standalone"
    assert any(i.get("purpose") == "maskable" for i in body["icons"])
    # The page registers the worker and links the manifest.
    assert "/manifest.webmanifest" in dash.text
    assert "/static/message_collapse.js" in dash.text
    assert "serviceWorker.register('/sw.js')" in dash.text
    # Live-stream recovery hook remains present without a footer below the composer.
    assert "visibilitychange" in dash.text


def test_pwa_cache_stamp_covers_every_static_asset(tmp_path, monkeypatch):
    from agentdeck.web import routes_pwa

    (tmp_path / "app.css").write_text("before")
    (tmp_path / "less_common.js").write_text("before")
    monkeypatch.setattr(routes_pwa, "_STATIC_DIR", tmp_path)
    before = routes_pwa.cache_stamp()

    (tmp_path / "less_common.js").write_text("after")

    assert routes_pwa.cache_stamp() != before


@pytest.mark.browser
async def test_restarting_screen_replaces_a_gateway_error_on_navigation(tmp_path):
    """Issue #606: a navigation while the deck is down must not render the raw 502.

    The deck sits behind ``tailscale serve``, which answers 502 for the seconds a
    deploy takes to bring agentdeck.service back. Drive the *shipped* worker source
    so this covers the handler that actually ships, not a copy of it.
    """
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw_source = (await c.get("/sw.js")).text
        screen = await c.get("/static/restarting.html")

    # The screen itself is a real, cached shell asset that polls the app back up.
    assert screen.status_code == 200
    assert screen.headers["content-type"].startswith("text/html")
    assert "data-restarting-screen" in screen.text
    assert "/healthz" in screen.text
    assert "/static/restarting.html" in sw_source

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main></main>")
        results = await page.evaluate(
            """async (swSource) => {
                const handlers = {};
                const mockSelf = {
                    location: { origin: 'https://deck.example' },
                    addEventListener: (type, fn) => { handlers[type] = fn; },
                    skipWaiting: () => {},
                    registration: {},
                    clients: {},
                };
                const noop = () => Promise.resolve();

                async function run(opts) {
                    const cache = {
                        match: async (url) => (
                            opts.cached === false || url !== '/static/restarting.html'
                                ? undefined
                                : new Response('RESTARTING SCREEN',
                                               { headers: { 'content-type': 'text/html' } })
                        ),
                        put: noop,
                        add: noop,
                    };
                    const mockCaches = { open: async () => cache, keys: async () => [],
                                         match: async () => null, delete: noop };
                    const mockFetch = async () => {
                        if (opts.network === 'fail') throw new TypeError('failed to fetch');
                        return new Response(opts.body || 'APP PAGE', { status: opts.status });
                    };
                    new Function('self', 'caches', 'fetch', swSource)(
                        mockSelf, mockCaches, mockFetch);
                    let promise = null;
                    handlers.fetch({
                        request: { method: 'GET', url: opts.url, mode: opts.mode },
                        respondWith: (p) => { promise = p; },
                    });
                    if (!promise) return { intercepted: false };
                    const res = await promise;
                    return { intercepted: true, status: res.status, body: await res.text() };
                }

                const NAV = 'https://deck.example/sessions/claude_code:test:sid1';
                return {
                    proxy502: await run({ url: NAV, mode: 'navigate', status: 502,
                                          body: '502 Bad Gateway' }),
                    proxy503: await run({ url: NAV, mode: 'navigate', status: 503 }),
                    unreachable: await run({ url: NAV, mode: 'navigate', network: 'fail' }),
                    healthy: await run({ url: NAV, mode: 'navigate', status: 200,
                                         body: 'THE SESSION PAGE' }),
                    notFound: await run({ url: NAV, mode: 'navigate', status: 404,
                                          body: 'APP SAYS NOT FOUND' }),
                    coldCache: await run({ url: NAV, mode: 'navigate', status: 502,
                                           body: '502 Bad Gateway', cached: false }),
                    partial: await run({ url: 'https://deck.example/partials/sessions',
                                         mode: 'cors', status: 502 }),
                    stream: await run({ url: 'https://deck.example/events',
                                        mode: 'cors', status: 200 }),
                };
            }""",
            sw_source,
        )
        await browser.close()

    # The proxy's "upstream is gone" statuses and an outright unreachable origin
    # all become the restarting screen instead of a 502 page.
    assert results["proxy502"]["body"] == "RESTARTING SCREEN"
    assert results["proxy503"]["body"] == "RESTARTING SCREEN"
    assert results["unreachable"]["body"] == "RESTARTING SCREEN"
    # A working navigation is untouched — no caching, no substitution — and the
    # app's own error pages still reach the user.
    assert results["healthy"]["body"] == "THE SESSION PAGE"
    assert results["notFound"]["status"] == 404
    assert results["notFound"]["body"] == "APP SAYS NOT FOUND"
    # Cache miss (half-finished install) degrades to today's behavior, never to a
    # blank screen.
    assert results["coldCache"]["status"] == 502
    # Fragments and the SSE stream stay entirely network-owned: respondWith() must
    # never wrap them, or the live tail is buffered and dynamic HTML goes stale.
    assert results["partial"]["intercepted"] is False
    assert results["stream"]["intercepted"] is False


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_restarting_screen_recovers_the_page_when_the_deck_comes_back(tmp_path):
    """Issue #606, end to end: real worker, real outage, real recovery.

    A live server is replaced by a stub that answers 502 the way ``tailscale
    serve`` does with its upstream down, then the real app is put back. The
    browser must show the restarting screen for the outage and land back on the
    page that was asked for — without anyone touching it.
    """

    async def bad_gateway(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({
            "type": "http.response.start",
            "status": 502,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": b"502 Bad Gateway"})

    def bind(port):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen()
        return sock

    @asynccontextmanager
    async def serving(served, port):
        # Each server binds its own socket (uvicorn takes ownership of the one it
        # is handed); the *port* is what stays put, standing in for the proxy's
        # fixed upstream address.
        sock = bind(port)
        server = uvicorn.Server(uvicorn.Config(served, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)
        try:
            yield sock.getsockname()[1]
        finally:
            # Kill it outright rather than shutting down gracefully. The browser
            # stays open across every swap, and uvicorn's graceful path ends in
            # asyncio.Server.wait_closed(), which since 3.12 waits for every
            # *connection* to close — the page's SSE stream never does, so a
            # graceful stop hangs forever. Aborting live connections and dropping
            # the listener is also what a deploy restart looks like from the
            # browser's side.
            for conn in list(server.server_state.connections):
                transport = getattr(conn, "transport", None)
                if transport is not None:
                    transport.abort()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            for listener in server.servers:
                listener.close()
            with suppress(OSError):
                sock.close()

    app = _app_with_state(tmp_path)
    session_path = "/sessions/claude_code:test:sid1"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # The worker is the subject here, so this page opts out of the harness
        # default that blocks it — and out of its `chats=1`, because the address
        # bar is compared exactly below and has to hold the URL as typed (#1525).
        page = await browser.new_page(
            viewport={"width": 390, "height": 844},
            service_workers="allow",
        )

        async with serving(app, 0) as port:
            base = f"http://127.0.0.1:{port}"
            await page.goto(f"{base}{session_path}")
            # The worker only helps once it controls the page and has the screen
            # cached; both happen on the first visit.
            await page.wait_for_function(
                "() => navigator.serviceWorker.controller !== null", timeout=20000
            )
            await page.wait_for_function(
                """async () => {
                    for (const key of await caches.keys()) {
                        const cache = await caches.open(key);
                        if (await cache.match('/static/restarting.html')) return true;
                    }
                    return false;
                }""",
                timeout=20000,
            )
            assert await page.locator(".session-detail").count() == 1

        # Deploy window: the proxy is up, the app is not.
        async with serving(bad_gateway, port):
            await page.goto(f"{base}{session_path}")
            await page.locator("body[data-restarting-screen]").wait_for(timeout=10000)
            assert "502" not in await page.locator("body").inner_text()
            assert await page.locator("h1").inner_text() == "Restarting"
            # The address bar still holds the page the user asked for, which is
            # what makes the reload below land there.
            assert page.url == f"{base}{session_path}"

        # The deck comes back: no tap, no reload — the screen recovers itself.
        async with serving(app, port):
            await page.locator(".session-detail").wait_for(timeout=25000)
            assert page.url == f"{base}{session_path}"

        await browser.close()


async def _worker_claim_context(browser, width, height):
    """A context that counts claims seen by *this* document.

    ``service_workers="allow"`` opts out of the harness default, since a real
    install/activate/claim is the subject here and it is coverage nothing else
    has.
    """
    context = await browser.new_context(
        viewport={"width": width, "height": height}, service_workers="allow"
    )
    await context.add_init_script(
        """
        window.__claims = 0;
        navigator.serviceWorker.addEventListener('controllerchange', function () {
          window.__claims += 1;
        });
        """
    )
    return context



async def _open_chat_from_chats_view(page, base, key):
    """Open ``key`` from the chats view, so the chats list stays beside it.

    A desktop chat page loaded on its own sits in the front page's columns instead
    (#2346); the tests that read the chats sidebar, its New chat or the two-pane
    layout reach the chat the way that layout is still reached.
    """
    await page.goto(base + "/chats")
    await page.locator(f'#sessions a[data-session-key="{key}"]').first.click()
    await page.locator(
        f'.session-layout > .session-detail[data-session-key="{key}"]'
    ).wait_for()


async def _settled_worker_page(context, url):
    """Open ``url``, wait out the fresh visit's *first* claim, and mark it.

    Nothing served this document, so that claim replaces nothing and #720 leaves
    the page alone: no reload, and the document ends up controlled with the count
    at 1. Zeroing it there means every assertion below reads only the replacement
    claim the test goes on to trigger.

    ``window.__preReload`` is the reload detector, and it is deliberately not the
    claim count. The count is per-document, so a reload zeroes it — but only for
    an instant: the fresh document registers plain ``/sw.js`` over the ``deploy=2``
    worker at load, is claimed again, and the count goes straight back to 1.
    Waiting for a *transient* 0 is a race. The marker only ever disappears, and
    only a fresh document can make that happen, since the init script never sets
    it.
    """
    page = await context.new_page()
    await page.goto(url)
    await page.wait_for_function(
        "() => navigator.serviceWorker.controller !== null && window.__claims === 1",
        timeout=20000,
    )
    await page.wait_for_timeout(300)
    await page.evaluate("() => { window.__claims = 0; window.__preReload = true; }")
    return page


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_a_service_worker_claim_keeps_an_open_panel_and_its_typed_value(
    tmp_path, monkeypatch
):
    """Issue #726, with a real worker: a claim must not wipe work in progress.

    The worker claims the page again right after a frontend deploy. Only a
    non-empty message draft used to hold that reload back, so an open New chat
    panel with a typed working directory was closed and cleared under the person
    editing it. The reload is deferred, not dropped: it lands once the panel is
    closed and the field is back to what the server rendered.
    """
    app = _continuable_session_app(tmp_path, monkeypatch)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 390, 844)
        page = await _settled_worker_page(context, base)

        control = page.locator("details.new-chat")
        # The panel's own summary, not the "Edit advanced" one nested inside it.
        panel_summary = control.locator("> summary")
        await panel_summary.click()
        field = control.locator("#new-chat-cwd")
        rendered = await field.evaluate("element => element.defaultValue")
        await field.fill("/tmp/claim-preserves-this")
        # Blur, so what holds the reload back is the open panel and the typed
        # amount themselves rather than the caret sitting in the field.
        await field.evaluate("element => element.blur()")

        # A deploy lands: a new script URL for the same scope installs a genuine
        # new worker, which skipWaiting()s and claims this page.
        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        await page.wait_for_function("() => window.__claims === 1", timeout=20000)
        assert "deploy=2" in await page.evaluate(
            "() => navigator.serviceWorker.controller.scriptURL"
        )
        # Past the handler's own 5s re-check, so the guard is proven to hold on
        # the retry and not merely on the first attempt.
        await page.wait_for_timeout(7000)

        # Still the same document — and still the panel the person opened, with
        # the path they typed.
        assert await page.evaluate("() => window.__preReload") is True
        assert await control.get_attribute("open") is not None
        assert await field.input_value() == "/tmp/claim-preserves-this"

        # Deferred, not dropped: with nothing left in progress the page picks up
        # the new shell on its own, without waiting for a navigation. The marker
        # going away is what proves it — only a fresh document loses it.
        await field.fill(rendered)
        await panel_summary.click()
        await page.wait_for_function(
            "() => window.__preReload === undefined", timeout=20000
        )

        await browser.close()


def _continuable_session_app(tmp_path, monkeypatch):
    """An app whose one session renders the continue-chat pickers.

    That form appears only for a session the deck could actually continue: new
    chats enabled, a readable transcript, and a known cwd.
    """
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    app.state.app_state.update_session(replace(session, cwd=tmp_path))
    return app


# The two halves are separate tests on purpose: one page must survive the claim
# and the other must be reloaded by it, and a document that reloaded can no longer
# answer for the controls that were on it.
@pytest.mark.browser
@pytest.mark.serviceworker
async def test_a_service_worker_claim_waits_for_a_picked_model(tmp_path, monkeypatch):
    """Issue #726, the other half of "preserve selected controls".

    The continue-chat pickers are a bare form on the session page — no
    ``<details>`` holding the reload back — so a model someone picked and has not
    sent yet is exactly the state a claim would discard.
    """
    app = _continuable_session_app(tmp_path, monkeypatch)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 1100, 900)
        page = await _settled_worker_page(
            context, f"{base}/sessions/claude_code:test:sid1"
        )

        picker = page.locator("#continue-chat-model")
        # model_picker.js rebuilds this <select> for the selected account's
        # provider, so wait for the rebuilt options rather than the markup's one.
        await page.wait_for_function(
            "() => document.querySelector('#continue-chat-model').options.length > 1",
            timeout=10000,
        )
        chosen = await picker.evaluate(
            """element => {
                for (const option of element.options) {
                    if (option.value) return option.value;
                }
                return '';
            }"""
        )
        assert chosen
        await picker.select_option(chosen)
        # Blur, so what holds the reload back is the picked model itself rather
        # than the focus sitting on the control.
        await picker.evaluate("element => element.blur()")

        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        await page.wait_for_function("() => window.__claims === 1", timeout=20000)
        # Past the handler's own 5s re-check, so the guard holds on the retry too.
        await page.wait_for_timeout(7000)
        assert await page.evaluate("() => window.__preReload") is True
        assert await picker.input_value() == chosen

        await browser.close()


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_a_service_worker_claim_lands_over_a_typed_deck_filter(
    tmp_path, monkeypatch
):
    """The deliberate counter-example to a held-back update.

    The chats page's filter lives in its address (#2603), so a query that filtered
    the list is the field's own markup value and a reload puts it back by itself:
    holding the update back for it would leave the page running stale assets for as
    long as it stays open. (A query typed but not yet applied is unsent text, and
    holds the update back like any other.)
    """
    app = _continuable_session_app(tmp_path, monkeypatch)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 1100, 900)
        page = await _settled_worker_page(context, f"{base}/chats?q=hello")
        assert await page.locator("#q").input_value() == "hello"

        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        # The reload lands — the marker only a pre-reload document carries is
        # gone — and the address puts the query back on the new document. Both
        # conditions in one wait: reading the field the instant the document
        # appears could find it before the new page has parsed.
        await page.wait_for_function(
            """() => window.__preReload === undefined
                     && document.querySelector('#q')
                     && document.querySelector('#q').value === 'hello'""",
            timeout=20000,
        )

        await browser.close()


@pytest.mark.browser
async def test_pwa_focus_replaces_a_stale_stream_without_reloading_or_wiping_state(
    tmp_path,
):
    """A resumed mobile PWA can regain focus without a visibility transition.

    The old EventSource may still claim OPEN even though its socket was frozen.  A
    focus signal must replace that source in place: a document reload would lose
    focused/selected controls and deliberate scroll state.
    """
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        dashboard = await client.get("/chats")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            window.__agentdeckNow = 1_000;
            Date.now = () => window.__agentdeckNow;
            window.__eventSources = [];
            class FakeEventSource extends EventTarget {
              static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
              constructor(url) {
                super();
                this.url = url;
                this.readyState = FakeEventSource.OPEN;
                window.__eventSources.push(this);
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix(
                "http://agentdeck.test"
            )
            if path == "/":
                await route.fulfill(
                    status=200, content_type="text/html", body=dashboard.text
                )
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/")
        await page.wait_for_function("window.__eventSources.length === 1")
        search = page.locator("#q")
        await search.fill("preserve this")
        await search.focus()
        await page.locator("#sessions").evaluate(
            """node => {
              node.style.height = '20px';
              node.style.overflow = 'auto';
              var filler = document.createElement('div');
              filler.style.height = '200px';
              node.appendChild(filler);
              node.scrollTop = 37;
            }"""
        )

        await page.evaluate("window.dispatchEvent(new Event('focus'))")
        assert await page.evaluate("window.__eventSources.length") == 1
        await page.evaluate(
            "window.__agentdeckNow = 80_000; window.dispatchEvent(new Event('focus'))"
        )
        await page.wait_for_function("window.__eventSources.length === 2")
        result = await page.evaluate(
            """() => ({
              sourceStates: window.__eventSources.map(source => source.readyState),
              value: document.querySelector('#q').value,
              focused: document.activeElement === document.querySelector('#q'),
              scrollTop: document.querySelector('#sessions').scrollTop,
              navigationEntries: performance.getEntriesByType('navigation').length,
            })"""
        )
        await browser.close()

    assert result == {
        "sourceStates": [2, 1],
        "value": "preserve this",
        "focused": True,
        "scrollTop": 37,
        "navigationEntries": 1,
    }


async def test_push_client_assets_and_bell(tmp_path):
    # Issue #7 (#12): service worker push handlers, the opt-in bell, and the
    # client module are wired into the shell.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw = await c.get("/sw.js")
        dash = await c.get("/chats")
        pushjs = await c.get("/static/push.js")
    # Service worker handles push + click, and caches the client module.
    assert "addEventListener('push'" in sw.text
    assert "addEventListener('notificationclick'" in sw.text
    assert "/static/push.js" in sw.text
    # The shell renders the (initially hidden) bell and loads the module.
    assert 'class="notif-bell"' in dash.text
    assert "/static/push.js" in dash.text
    # The client module talks to the backend endpoints.
    assert pushjs.status_code == 200
    assert "/push/public-key" in pushjs.text
    assert "/push/subscribe" in pushjs.text
    assert "/push/unsubscribe" in pushjs.text
    assert "streamStale" in dash.text
    assert 'class="build"' not in dash.text
    # Dashboard HTML must not be cached, so a deploy's inline JS actually lands.
    assert dash.headers["cache-control"] == "no-cache"


@pytest.mark.browser
async def test_notification_click_opens_its_chat_and_never_no_ops(tmp_path):
    # Issue #35: tapping a push notification must reliably surface its page,
    # freshly loaded, and never a silent no-op; since #3348 that page is the
    # notification's chat, and only a foreign or missing url goes home. Drive the *real*
    # service-worker source under a mocked ServiceWorkerGlobalScope so we test the shipped
    # notificationclick handler, not a hand-copied excerpt.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw_source = (await c.get("/sw.js")).text
        dash = (await c.get("/chats")).text

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main></main>")
        results = await page.evaluate(
            """async (swSource) => {
                const handlers = {};
                const mockSelf = {
                    location: { origin: 'https://deck.example' },
                    addEventListener: (type, fn) => { handlers[type] = fn; },
                    skipWaiting: () => {},
                    registration: {},
                    clients: {},
                };
                const noop = () => Promise.resolve();
                const mockCaches = { open: noop, keys: () => Promise.resolve([]),
                                     match: () => Promise.resolve(null), delete: noop };
                // Execute the shipped worker with self/caches bound to the mocks.
                new Function('self', 'caches', swSource)(mockSelf, mockCaches);

                async function run(clients, dataUrl) {
                    const rec = { focused: [], navigated: [], posted: [], opened: [] };
                    mockSelf.clients = {
                        matchAll: async () => clients.map((c) => {
                            const client = {
                                url: c.url,
                                focus: async () => {
                                    if (c.focus === 'reject') throw new Error('focus failed');
                                    rec.focused.push(c.url);
                                },
                                postMessage: (m) => rec.posted.push(m),
                            };
                            if (c.navigate !== 'missing') {
                                client.navigate = async (u) => {
                                    if (c.navigate === 'reject') throw new Error('cannot navigate');
                                    rec.navigated.push(u);
                                    return client;
                                };
                            }
                            return client;
                        }),
                        openWindow: async (u) => { rec.opened.push(u); return {}; },
                    };
                    const ev = {
                        notification: { close() {}, data: { url: dataUrl } },
                        _p: null,
                        waitUntil(p) { this._p = p; },
                    };
                    handlers.notificationclick(ev);
                    await ev._p;
                    return rec;
                }

                const O = 'https://deck.example';
                return {
                    existing: await run([{ url: O + '/chat/x', navigate: true }], '/chat/x'),
                    none: await run([], '/chat/x'),
                    uncontrolled:
                        await run([{ url: O + '/chat/x', navigate: 'reject' }], '/chat/x'),
                    noNavigate:
                        await run([{ url: O + '/chat/x', navigate: 'missing' }], '/chat/x'),
                    crossOrigin: await run([
                        { url: 'https://evil.example/', navigate: true },
                        { url: O + '/chat/y', navigate: true },
                    ], '/chat/y'),
                    foreignUrl: await run([], 'https://evil.example/chat/z'),
                    noUrl: await run([], undefined),
                };
            }""",
            sw_source,
        )
        await browser.close()

    home = "https://deck.example/"
    chat = "https://deck.example/chat/x"
    # An existing app window is focused and navigated to the notification's chat,
    # and no duplicate window is opened.
    assert results["existing"]["navigated"] == [chat]
    assert results["existing"]["focused"] == [chat]
    assert results["existing"]["opened"] == []
    assert results["existing"]["posted"] == []
    # No app window to reuse → open a fresh one at the chat.
    assert results["none"]["opened"] == [chat]
    assert results["none"]["navigated"] == []
    # navigate() rejects (frozen/uncontrolled client): still focused, and the page
    # is asked to go there itself — never a no-op, never left on the stale view.
    assert results["uncontrolled"]["focused"] == [chat]
    assert results["uncontrolled"]["navigated"] == []
    assert results["uncontrolled"]["posted"] == [{"type": "agentdeck:open", "path": "/chat/x"}]
    assert results["uncontrolled"]["opened"] == []
    # navigate() unsupported: same page-driven fallback.
    assert results["noNavigate"]["posted"] == [{"type": "agentdeck:open", "path": "/chat/x"}]
    assert results["noNavigate"]["opened"] == []
    # A cross-origin window is skipped; the same-origin app window is routed.
    assert results["crossOrigin"]["navigated"] == ["https://deck.example/chat/y"]
    assert results["crossOrigin"]["opened"] == []
    # A url on another origin, or none, goes home.
    assert results["foreignUrl"]["opened"] == [home]
    assert results["noUrl"]["opened"] == [home]

    # The page half of the fallback is wired in: the shell listens for the SW's
    # message, and still takes an older worker's go-home one.
    assert "agentdeck:open'" in dash and "agentdeck:open-home" in dash
    assert "navigator.serviceWorker.addEventListener('message'" in dash


@pytest.mark.browser
async def test_push_suppressed_only_when_a_window_is_foreground(tmp_path):
    # Issue #36: a push must not stack a notification when this device already has
    # the app open and visible, but must still notify when it's closed or
    # backgrounded. Drive the shipped push handler under a mocked worker scope.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw_source = (await c.get("/sw.js")).text

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main></main>")
        results = await page.evaluate(
            """async (swSource) => {
                const handlers = {};
                const shown = [];
                const closed = [];
                const mockSelf = {
                    location: { origin: 'https://deck.example' },
                    addEventListener: (type, fn) => { handlers[type] = fn; },
                    skipWaiting: () => {},
                    registration: {
                        showNotification: (title, opts) => { shown.push({ title, opts }); },
                        getNotifications: async ({ tag }) =>
                            tag === 'work:w' ? [{ close: () => closed.push(tag) }] : [],
                    },
                    clients: {},
                };
                const noop = () => Promise.resolve();
                const mockCaches = { open: noop, keys: () => Promise.resolve([]),
                                     match: () => Promise.resolve(null), delete: noop };
                new Function('self', 'caches', swSource)(mockSelf, mockCaches);

                async function run(visibilities, payload) {
                    shown.length = 0;
                    mockSelf.clients = {
                        matchAll: async () =>
                            visibilities.map((v) => ({ visibilityState: v })),
                    };
                    const message = payload || { title: 'T', body: 'B', url: '/chat/x' };
                    const ev = {
                        data: { json: () => message },
                        _p: null,
                        waitUntil(p) { this._p = p; },
                    };
                    handlers.push(ev);
                    await ev._p;
                    return shown.map((s) => ({ title: s.title, tag: s.opts.tag }));
                }

                return {
                    foreground: await run(['visible']),
                    backgrounded: await run(['hidden']),
                    mixed: await run(['hidden', 'visible']),
                    none: await run([]),
                    clear: await run([], { clear: ['work:w', 'needs-you'] }),
                    closed,
                };
            }""",
            sw_source,
        )
        await browser.close()

    # A visible window on this device suppresses the notification...
    assert results["foreground"] == []
    assert results["mixed"] == []
    # ...but a hidden/backgrounded app, or no window at all, still notifies.
    assert results["backgrounded"] == [{"title": "T", "tag": "/chat/x"}]
    assert results["none"] == [{"title": "T", "tag": "/chat/x"}]
    # A clear push (#3365) closes the tags it names that are showing, and shows nothing.
    assert results["clear"] == [] and results["closed"] == ["work:w"]


@pytest.mark.browser
async def test_markdown_links_reject_unsafe_schemes_and_attribute_breakout(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get("/chats")
    start = response.text.index("function mdEscape")
    end = response.text.index("// Connection hygiene", start)
    renderer = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main id=output></main>")
        await page.add_script_tag(content=renderer)
        rendered = await page.evaluate(
            """inputs => inputs.map(input => {
                const output = document.querySelector('#output');
                output.innerHTML = mdToHtml(input);
                const link = output.querySelector('a');
                return {
                    html: output.innerHTML,
                    href: link && link.getAttribute('href'),
                    attributes: link && Array.from(link.attributes, attr => attr.name),
                };
            })""",
            [
                "[x](javascript:alert(1))",
                "[x](data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;)",
                "[x](vbscript:msgbox(1))",
                '[x](https://e.com/"onmouseover=alert(1))',
                "[x](https://e.com)",
                "[preview](agentdeck-preview:demo/index.html)",
                "[relative](demo/index.html)",
                "[escape](agentdeck-preview:../outside.html)",
                "[absolute](agentdeck-preview:/tmp/outside.html)",
                "[source](agentdeck-preview:demo/index.py)",
            ],
        )
        preview_with_session = await page.evaluate(
            """() => {
                const output = document.querySelector('#output');
                output.innerHTML = mdToHtml(
                  '[preview](agentdeck-preview:demo/My%20Page.html#results)',
                  'claude_code:test:sid1'
                );
                const link = output.querySelector('a');
                return {
                  href: link && link.getAttribute('href'),
                  target: link && link.getAttribute('target'),
                  rel: link && link.getAttribute('rel'),
                };
            }"""
        )
        await browser.close()

    for result in rendered[:3]:
        assert result["href"] is None
        assert "<a " not in result["html"]
    quote_result = rendered[3]
    assert quote_result["href"] == 'https://e.com/"onmouseover=alert(1'
    assert quote_result["attributes"] == ["href", "target", "rel"]
    assert " onmouseover=" not in quote_result["html"]
    assert rendered[4]["href"] == "https://e.com"
    assert '<a href="https://e.com" target="_blank" rel="noopener">x</a>' in rendered[4]["html"]
    for result in (rendered[5], *rendered[7:]):
        assert result["href"] is None
        assert "<a " not in result["html"]
    assert rendered[6]["href"] == "demo/index.html"
    assert preview_with_session == {
        "href": (
            "/sessions/claude_code%3Atest%3Asid1/preview/"
            "demo/My%20Page.html#results"
        ),
        "target": "_blank",
        "rel": "noopener",
    }


@pytest.mark.browser
async def test_transcript_plain_web_urls_render_as_safe_links(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    lines[0]["message"]["content"] = (
        "See https://github.com/eerovil/agentdeck/pull/65. "
        "Keep `https://example.test/code` literal and "
        "[open docs](https://example.test/docs). "
        "[Open preview](demo/index.html)."
    )
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = []
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(response.text)
            message = page.locator(".ev.user .ev-text")
            links = message.locator("a")
            await links.first.wait_for()
            results.append(
                {
                    "links": await links.evaluate_all(
                        """links => links.map(link => ({
                          text: link.textContent,
                          href: link.getAttribute('href'),
                          target: link.getAttribute('target'),
                          rel: link.getAttribute('rel'),
                        }))"""
                    ),
                    "code_links": await message.locator("code a").count(),
                    "code_text": await message.locator("code").text_content(),
                }
            )
            await page.close()
        await browser.close()

    expected_links = [
        {
            "text": "https://github.com/eerovil/agentdeck/pull/65",
            "href": "https://github.com/eerovil/agentdeck/pull/65",
            "target": "_blank",
            "rel": "noopener",
        },
        {
            "text": "open docs",
            "href": "https://example.test/docs",
            "target": "_blank",
            "rel": "noopener",
        },
        {
            "text": "Open preview",
            "href": (
                "/sessions/claude_code%3Atest%3Asid1/preview/demo/index.html"
            ),
            "target": "_blank",
            "rel": "noopener",
        },
    ]
    for result in results:
        assert result["links"] == expected_links
        assert result["code_links"] == 0
        assert result["code_text"] == "https://example.test/code"


@pytest.mark.browser
async def test_expanded_card_uses_session_preview_and_absolute_file_links(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Generated files",
            last_text=(
                "[Open preview](report/index.html) "
                "[Open report](/tmp/report.pdf)"
            ),
        )
    )
    async with _client(app) as client:
        response = await client.get("/chats")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = []
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(_with_inline_cards_script(response.text))
            card = page.locator(
                'a.session[data-session-key="claude_code:test:sid1"]'
            )
            await card.locator(".expand-btn").click()
            links = card.locator(".latest-line.agent a")
            await links.first.wait_for()
            results.append(
                await links.evaluate_all(
                    "links => links.map(link => link.getAttribute('href'))"
                )
            )
            await page.close()
        await browser.close()

    assert results == [
        [
            "/sessions/claude_code%3Atest%3Asid1/preview/report/index.html",
            "/tmp/report.pdf",
        ],
        [
            "/sessions/claude_code%3Atest%3Asid1/preview/report/index.html",
            "/tmp/report.pdf",
        ],
    ]


@pytest.mark.browser
async def test_transcript_markdown_tables_render_and_stay_within_mobile_message(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    lines[0]["message"]["content"] = (
        "| Name | Why / note |\n"
        "| :--- | ---: |\n"
        "| **Cloud Nine** | idiom kept; `weather` negated |\n"
        "| Trace | [copy](https://example.test/copy) |\n"
        "| Unsafe | <img src=x onerror=alert(1)> |\n"
        "\n"
        "| **Area** | `Owner` | Status | Next review |\n"
        "| --- | --- | :---: | ---: |\n"
        "| Runtime ownership | deckhand-operations-team | blocked | 2026-08-14 |\n"
        "| Transcript parsing | providers-guild | ok |  |\n"
        "\n"
        "Ordinary | pipe text\n"
        "\n"
        "```md\n"
        "| fenced | text |\n"
        "| --- | --- |\n"
        "```"
    )
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")
    stylesheet = (
        Path(__file__).parents[1] / "src/agentdeck/web/static/app.css"
    ).read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = []
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(response.text)
            await page.add_style_tag(content=stylesheet)
            message = page.locator(".ev.user .ev-text")
            table = message.locator("table").first
            wrapper = message.locator(".md-table-scroll").first
            wide = message.locator(".md-table-scroll").nth(1)
            await table.wait_for()
            results.append(
                {
                    "width": width,
                    "headers": await table.locator("th").all_text_contents(),
                    "rows": await table.locator("tbody tr").all_text_contents(),
                    "strong": await table.locator("strong").all_text_contents(),
                    "code": await table.locator("code").all_text_contents(),
                    "link": await table.locator("a").get_attribute("href"),
                    "alignments": await table.locator("th").evaluate_all(
                        "cells => cells.map(cell => getComputedStyle(cell).textAlign)"
                    ),
                    "tables": await message.locator("table").count(),
                    "active_html": await table.locator("img, script").count(),
                    "plain_text": await message.text_content(),
                    "wrapper_scrolls": await wrapper.evaluate(
                        "element => element.scrollWidth > element.clientWidth"
                    ),
                    "wrapper_inside_viewport": await wrapper.evaluate(
                        "element => element.getBoundingClientRect().right <= innerWidth"
                    ),
                    "page_overflow": await page.evaluate(
                        "document.documentElement.scrollWidth > window.innerWidth"
                    ),
                    "narrow_is_stackable": await wrapper.evaluate(
                        "element => element.classList.contains('md-table-stackable')"
                    ),
                    "wide_is_stackable": await wide.evaluate(
                        "element => element.classList.contains('md-table-stackable')"
                    ),
                    "wide_scrolls": await wide.evaluate(
                        "element => element.scrollWidth > element.clientWidth"
                    ),
                    "wide_head_hidden": await wide.evaluate(
                        "element => getComputedStyle(element.querySelector('thead'))"
                        ".display === 'none'"
                    ),
                    # Stacked, every cell of a row spans the row's whole width and
                    # is introduced by its own column header.
                    "wide_cells_full_width": await wide.evaluate(
                        "element => {"
                        "  const row = element.querySelector('tbody tr');"
                        "  const cells = [...row.querySelectorAll('td')];"
                        "  return cells.every(cell => cell.getBoundingClientRect().width"
                        "    >= row.getBoundingClientRect().width - 20);"
                        "}"
                    ),
                    "wide_labels": await wide.evaluate(
                        "element => [...element.querySelectorAll('tbody tr:first-child td')]"
                        ".map(cell => cell.dataset.label)"
                    ),
                    "wide_label_shown": await wide.evaluate(
                        "element => getComputedStyle("
                        "  element.querySelector('tbody td'), '::before').content"
                    ),
                    "wide_empty_cell_hidden": await wide.evaluate(
                        "element => getComputedStyle("
                        "  element.querySelector('tbody tr:last-child td:last-child'))"
                        ".display === 'none'"
                    ),
                }
            )
            await page.close()
        await browser.close()

    for result in results:
        narrow = result["width"] == 320
        assert result["headers"] == ["Name", "Why / note"]
        assert result["rows"] == [
            "Cloud Nineidiom kept; weather negated",
            "Tracecopy",
            "Unsafe<img src=x onerror=alert(1)>",
        ]
        assert result["strong"] == ["Cloud Nine"]
        assert result["code"] == ["weather"]
        assert result["link"] == "https://example.test/copy"
        assert result["alignments"] == ["left", "right"]
        assert result["tables"] == 2
        assert result["active_html"] == 0
        assert "Ordinary | pipe text" in result["plain_text"]
        assert "| fenced | text |" in result["plain_text"]
        # Nothing is ever pushed out of reach sideways — not the page, not the
        # two-column table that used to force a drag on a phone, not the wide one.
        assert result["wrapper_scrolls"] is False
        assert result["wide_scrolls"] is False
        assert result["wrapper_inside_viewport"] is True
        assert result["page_overflow"] is False
        assert result["narrow_is_stackable"] is False
        assert result["wide_is_stackable"] is True
        assert result["wide_labels"] == ["Area", "Owner", "Status", "Next review"]
        assert result["wide_head_hidden"] is narrow
        assert result["wide_cells_full_width"] is narrow
        assert result["wide_empty_cell_hidden"] is narrow
        assert ("Area" in result["wide_label_shown"]) is narrow


async def test_local_markdown_file_opens_from_absolute_path_with_line_suffix(tmp_path):
    app = _app_with_state(tmp_path)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# Handoff\n\n- first\n- second\n")

    async with _client(app) as client:
        rendered = await client.get(f"{handoff}:1")
        source = await client.get(f"{handoff}:3")

    assert rendered.status_code == 200
    assert 'id="local-markdown"' in rendered.text
    assert "# Handoff" in rendered.text
    assert rendered.headers["cache-control"] == "no-store"
    assert source.status_code == 200
    assert 'class="local-source"' in source.text
    assert 'id="L3" class="selected"' in source.text
    assert "first" in source.text


async def test_local_file_route_previews_html_and_serves_binary_inline(tmp_path):
    app = _app_with_state(tmp_path)
    active = tmp_path / "page.html"
    active.write_text("<script>alert('no')</script>")
    binary = tmp_path / "asset.bin"
    binary.write_bytes(b"\x00\x01agentdeck")

    async with _client(app) as client:
        active_response = await client.get(str(active))
        source_response = await client.get(f"{active}?raw=1")
        numbered_response = await client.get(f"{active}:1")
        binary_response = await client.get(str(binary))
        missing_response = await client.get(str(tmp_path / "missing.txt"))

    # An .html link renders; its markup stays one click (or one :line) away.
    assert active_response.status_code == 200
    assert 'sandbox="allow-scripts"' in active_response.text
    assert f"{active}?raw=1" in active_response.text
    assert "alert('no')" not in active_response.text
    for response in (source_response, numbered_response):
        assert response.status_code == 200
        assert "&lt;script&gt;alert" in response.text
        assert "<script>alert('no')</script>" not in response.text
    assert binary_response.status_code == 200
    assert binary_response.content == b"\x00\x01agentdeck"
    assert binary_response.headers["content-disposition"].startswith("inline;")
    assert missing_response.status_code == 404


async def test_local_file_route_hides_private_turn_uploads(tmp_path, monkeypatch):
    from agentdeck.web import uploads

    private_root = tmp_path / "private-uploads"
    monkeypatch.setattr(uploads, "GENERIC_UPLOAD_ROOT", private_root)
    attachment = private_root / "lease" / "1-deadbeef.upload"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"private")
    app = _app_with_state(tmp_path)

    async with _client(app) as client:
        response = await client.get(str(attachment))

    assert response.status_code == 404
    assert response.json() == {"detail": "local file not found"}


@pytest.mark.browser
async def test_absolute_html_link_renders_with_its_own_directory_as_the_bundle(tmp_path):
    app = _app_with_state(tmp_path)
    report = tmp_path / "report"
    report.mkdir()
    (report / "index.html").write_text(
        "<!doctype html><link rel=stylesheet href=app.css>"
        "<body><h1>Quarterly report</h1>"
        "<script>document.body.dataset.ready = 'yes'</script>"
    )
    (report / "app.css").write_text("h1 { color: tomato; }")
    (tmp_path / "secret.json").write_text('{"secret": true}')
    linked = tmp_path / "linked.html"
    linked.symlink_to(report / "index.html")

    async with _client(app) as client:
        wrapper = await client.get(str(report / "index.html"))
        preview_src = re.search(r'<iframe src="([^"]+)"', wrapper.text).group(1)
        asset_base = preview_src.rsplit("/", 1)[0]
        html = await client.get(preview_src)
        css = await client.get(f"{asset_base}/app.css")
        outside = await client.get(f"{asset_base}/%2e%2e/secret.json")
        token = asset_base.rsplit("/", 1)[1]
        forged = await client.get(
            f"{asset_base.rsplit('/', 1)[0]}/{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
            "/index.html"
        )
        # A symlinked entry point renders the file it actually points at.
        symlinked_wrapper = await client.get(str(linked))

    assert wrapper.status_code == 200
    assert str(report / "index.html") in wrapper.text
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "<h1>Quarterly report</h1>" in html.text
    assert "sandbox allow-scripts" in html.headers["content-security-policy"]
    assert css.status_code == 200
    assert outside.status_code == 404
    assert forged.status_code == 404
    assert re.search(r'<iframe src="([^"]+)"', symlinked_wrapper.text).group(1) == preview_src

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}{report / 'index.html'}")
        frame = page.frame_locator("iframe")
        await frame.locator("body[data-ready=yes]").wait_for()
        rendered = await frame.locator("h1").evaluate(
            "el => ({ text: el.textContent, color: getComputedStyle(el).color })"
        )
        await browser.close()

    assert rendered == {"text": "Quarterly report", "color": "rgb(255, 99, 71)"}


@pytest.mark.browser
async def test_session_html_preview_serves_relative_assets_in_an_opaque_sandbox(tmp_path):
    app = _app_with_state(tmp_path)
    control_attempts = []

    @app.post("/preview-control-probe")
    async def preview_control_probe():
        control_attempts.append(True)
        return {"ok": True}

    site = tmp_path / "site"
    (site / "demo" / "styles").mkdir(parents=True)
    (site / "demo" / "index.html").write_text(
        "<!doctype html><link rel=stylesheet href=styles/app.css>"
        "<body><h1>Rendered preview</h1><img id=pixel src=pixel.png>"
        "<script src=app.js></script>"
        "<script type=module src=module.mjs></script>"
        "<script>Promise.allSettled(["
        "fetch('data.json').then(()=>document.body.dataset.fetch='allowed')"
        ".catch(()=>document.body.dataset.fetch='blocked'),"
        "fetch('/preview-control-probe',{method:'POST',body:'x'})"
        ".then(()=>document.body.dataset.control='allowed')"
        ".catch(()=>document.body.dataset.control='blocked'),"
        "fetch('https://example.com').then(()=>document.body.dataset.external='allowed')"
        ".catch(()=>document.body.dataset.external='blocked'),"
        "Promise.resolve().then(()=>navigator.serviceWorker.register('sw.js'))"
        ".then(()=>document.body.dataset.worker='allowed')"
        ".catch(()=>document.body.dataset.worker='blocked')"
        "]).then(()=>document.body.dataset.settled='yes')</script>"
    )
    (site / "demo" / "styles" / "app.css").write_text("h1 { color: tomato; }")
    (site / "demo" / "app.js").write_text(
        "document.body.dataset.ready = 'yes';"
        "const p=document.querySelector('#pixel');"
        "const loaded=()=>document.body.dataset.asset='yes';"
        "p.addEventListener('load',loaded);if(p.complete)loaded();"
    )
    (site / "demo" / "module.mjs").write_text("document.body.dataset.module = 'yes';")
    (site / "demo" / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    (site / "demo" / "data.json").write_text('{"ok":"yes"}')
    (site / "demo" / "pixel.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    (site / "demo" / "evil.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'><script><![CDATA["
        "fetch('/healthz').then(()=>document.documentElement.dataset.control='allowed')"
        ".catch(()=>document.documentElement.dataset.control='blocked')"
        ".finally(()=>document.documentElement.dataset.settled='yes')"
        "]]></script></svg>"
    )
    (site / "demo" / ".env").write_text("SECRET=not-for-the-preview")
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.cwd = site

    async with _client(app) as client:
        wrapper = await client.get(
            "/sessions/claude_code:test:sid1/preview/demo/index.html",
            follow_redirects=True,
        )
        preview_src = re.search(r'<iframe src="([^"]+)"', wrapper.text).group(1)
        asset_base = preview_src.rsplit("/", 1)[0]
        html = await client.get(preview_src)
        css = await client.get(f"{asset_base}/styles/app.css")
        script = await client.get(f"{asset_base}/app.js")
        data = await client.get(f"{asset_base}/data.json")
        dotfile = await client.get(f"{asset_base}/.env")
        health = await client.get("/healthz")

    assert wrapper.status_code == 200
    assert str(wrapper.url) == f"http://t{site}/demo/index.html"
    assert 'sandbox="allow-scripts"' in wrapper.text
    assert "demo/index.html" in wrapper.text
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "<h1>Rendered preview</h1>" in html.text
    assert "sandbox allow-scripts" in html.headers["content-security-policy"]
    assert "allow-same-origin" not in html.headers["content-security-policy"]
    assert "allow-forms" not in html.headers["content-security-policy"]
    assert "default-src 'none'" in html.headers["content-security-policy"]
    assert "connect-src 'none'" in html.headers["content-security-policy"]
    assert html.headers["cache-control"] == "private, no-store"
    assert html.headers["cross-origin-opener-policy"] == "same-origin"
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["access-control-allow-origin"] == "null"
    assert script.status_code == 200
    assert "dataset.ready" in script.text
    assert data.headers["access-control-allow-origin"] == "null"
    assert dotfile.status_code == 404
    assert "access-control-allow-origin" not in health.headers

    async with _live_server(app) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            # The sandbox's own refusal is the subject: with the harness default
            # of service_workers="block", worker="blocked" below would pass even
            # if the CSP let a preview register one.
            page = await browser.new_page(
                viewport={"width": 320, "height": 640}, service_workers="allow"
            )
            await page.goto(
                f"{base_url}/sessions/claude_code:test:sid1/preview/demo/index.html"
            )
            frame = page.frame_locator("iframe")
            await frame.locator("body[data-settled=yes]").wait_for()
            browser_result = await frame.locator("body").evaluate(
                """() => ({
                  script: document.body.dataset.ready,
                  asset: document.body.dataset.asset,
                  fetch: document.body.dataset.fetch,
                  control: document.body.dataset.control,
                  external: document.body.dataset.external,
                  worker: document.body.dataset.worker,
                  module: document.body.dataset.module,
                  popup: window.open('about:blank') ? 'allowed' : 'blocked',
                  color: getComputedStyle(document.querySelector('h1')).color,
                })"""
            )
            iframe_src = await page.locator("iframe").get_attribute("src")
            svg_page = await browser.new_page()
            await svg_page.goto(iframe_src.rsplit("/", 1)[0] + "/evil.svg")
            await svg_page.wait_for_function(
                "document.documentElement.dataset.settled === 'yes'"
            )
            svg_control = await svg_page.evaluate(
                "document.documentElement.dataset.control"
            )
            await browser.close()

    assert browser_result == {
        "script": "yes",
        "asset": "yes",
        "fetch": "blocked",
        "control": "blocked",
        "external": "blocked",
        "worker": "blocked",
        "module": "yes",
        "popup": "blocked",
        "color": "rgb(255, 99, 71)",
    }
    assert control_attempts == []
    assert svg_control == "blocked"


async def test_session_html_preview_rejects_traversal_and_symlink_escape(tmp_path):
    app = _app_with_state(tmp_path)
    site = tmp_path / "site"
    (site / "demo").mkdir(parents=True)
    (site / "other").mkdir()
    (site / "demo" / "index.html").write_text("inside")
    (site / "demo" / "source.py").write_text("secret = True")
    (site / "other" / "secret.json").write_text('{"secret": true}')
    os.mkfifo(site / "demo" / "pipe.txt")
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    (site / "escape.html").symlink_to(outside)
    (site / "demo" / "escape-dir").symlink_to(site / "other", target_is_directory=True)
    swapped_root = tmp_path / "swapped-root"
    swapped_root.mkdir()
    (swapped_root / "index.html").write_text("a different directory")
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.cwd = site

    async with _client(app) as client:
        wrapper = await client.get(
            "/sessions/claude_code:test:sid1/preview/demo/index.html",
            follow_redirects=True,
        )
        preview_src = re.search(r'<iframe src="([^"]+)"', wrapper.text).group(1)
        asset_base = preview_src.rsplit("/", 1)[0]
        traversal = await client.get(
            "/sessions/claude_code:test:sid1/preview/%2e%2e/outside.html"
        )
        nested_traversal = await client.get(
            "/sessions/claude_code:test:sid1/preview/nested/../../outside.html"
        )
        symlink = await client.get(
            "/sessions/claude_code:test:sid1/preview/escape.html"
        )
        bundle_escape = await client.get(
            f"{asset_base}/%2e%2e/other/secret.json"
        )
        symlink_directory = await client.get(
            f"{asset_base}/escape-dir/secret.json"
        )
        special_file = await client.get(f"{asset_base}/pipe.txt")
        unknown_type = await client.get(f"{asset_base}/source.py")
        token = preview_src.split("/")[-2]
        replacement = ("A" if token[-1] != "A" else "B")
        tampered = await client.get(
            preview_src.replace(f"/{token}/", f"/{token[:-1] + replacement}/")
        )
        directory = await client.get(
            "/sessions/claude_code:test:sid1/preview/."
        )
        unknown_session = await client.get(
            "/sessions/claude_code:test:missing/preview/index.html"
        )
        # Swapping the bundle directory itself out from under a live capability
        # invalidates it, even though the path it names still resolves.
        (site / "demo").rename(tmp_path / "demo-was-here")
        swapped_root.rename(site / "demo")
        rebound_capability = await client.get(preview_src)
        session.cwd = None
        missing_cwd = await client.get(
            "/sessions/claude_code:test:sid1/preview/index.html"
        )

    assert traversal.status_code == 404
    assert nested_traversal.status_code == 404
    assert symlink.status_code == 404
    assert bundle_escape.status_code == 404
    assert symlink_directory.status_code == 404
    assert special_file.status_code == 404
    assert unknown_type.status_code == 404
    assert tampered.status_code == 404
    assert directory.status_code == 404
    assert unknown_session.status_code == 404
    assert rebound_capability.status_code == 404
    assert missing_cwd.status_code == 404


async def test_dashboard_has_list_search(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/chats")
    # The filter is a plain GET form above the list (#2603).
    assert 'id="q"' in r.text and 'name="q"' in r.text
    assert r.text.index('id="q"') < r.text.index('id="sessions"')
    assert '<form class="chat-filters" method="get" action="/chats"' in r.text


def test_a_deck_with_no_chat_board_has_no_board_service(tmp_path):
    """The Work page and the chat board read this; a deck that never configured one gets None."""
    from agentdeck.web.render import chat_board_service

    assert chat_board_service(_app_with_state(tmp_path).state) is None


async def test_a_chat_page_shows_no_chatgpt_review_chip(tmp_path):
    """The chip left chat pages (#2864): it stays on the Work page and the chat board."""
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1")

    assert r.status_code == 200
    assert "chat-chip" not in r.text


async def test_dashboard_has_no_server_side_deep_search(tmp_path):
    """The server-side deep search was removed (#2864); the on-page filter box stays."""
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/chats")
        gone = await c.get("/deep-search?q=anything")

    assert 'id="q"' in r.text
    assert "deep-search" not in r.text
    assert "deep_search.js" not in r.text
    assert gone.status_code == 404
    assert not hasattr(app.state, "session_search")


@pytest.mark.browser
async def test_clipboard_screenshot_attaches_to_chat_composer(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get("/chats")

    marker = "// Chat composers: attach arbitrary files"
    start = response.text.index(marker)
    end = response.text.index("// Keep unsent prompts", start)
    script = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(
            '<form><textarea name="message"></textarea>'
            '<input type="file" name="images" multiple></form>'
        )
        await page.add_script_tag(content=script)
        result = await page.evaluate(
            """async () => {
                const transfer = new DataTransfer();
                transfer.items.add(new File(
                    [new Uint8Array([137, 80, 78, 71])], 'screenshot.png',
                    {type: 'image/png'}
                ));
                const textarea = document.querySelector('textarea');
                const event = new ClipboardEvent('paste', {
                    clipboardData: transfer, bubbles: true, cancelable: true
                });
                textarea.dispatchEvent(event);
                const input = document.querySelector('input[name="images"]');
                return {
                    prevented: event.defaultPrevented,
                    files: input.files.length,
                    name: input.files[0].name,
                    previews: document.querySelectorAll('.paste-preview').length,
                    count: document.querySelector('.paste-count').textContent,
                };
            }"""
        )
        await browser.close()

    assert result == {
        "prevented": True,
        "files": 1,
        "name": "screenshot.png",
        "previews": 1,
        "count": "1 file attached",
    }


@pytest.mark.browser
async def test_session_bottom_follow_stops_on_manual_scroll_and_resumes_near_end():
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    bottom_follow = (static_dir / "session_bottom_follow.js").read_text()
    filler = "".join(
        f'<div class="ev" style="min-height:80px">older message {index}</div>'
        for index in range(30)
    )
    html = f"""
      <style>{css}</style>
      <body class="session-page session-stack-page"><main>
        <div class="session-layout">
          <aside class="session-sidebar"></aside>
          <section class="session-detail">
            <div class="transcript">{filler}
              <div class="ev user pending-message" data-pending-message data-queue-id="1">
                <div class="ev-text">queued message</div>
              </div>
              <div class="ev user pending-message" data-pending-message data-queue-id="2">
                <div class="ev-text">queued message</div>
              </div>
            </div>
            <div id="tool-activity"></div>
            <form class="inject-form"><div id="inject-result"></div></form>
          </section>
        </div>
      </main></body>
    """

    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 700})
            await page.set_content(html)
            await page.add_script_tag(content=bottom_follow)
            result = await page.evaluate(
                """async () => {
                  const frame = () => new Promise(requestAnimationFrame);
                  const settle = async () => { await frame(); await frame(); };
                  const root = document.querySelector('.session-detail');
                  const transcript = document.querySelector('.transcript');
                  const status = document.querySelector('#inject-result');
                  const form = document.querySelector('.inject-form');
                  const bottomGap = () => root.scrollHeight - root.clientHeight - root.scrollTop;
                  const swap = target => {
                    target.dispatchEvent(new CustomEvent('htmx:beforeSwap', {bubbles: true}));
                    target.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true}));
                  };
                  const appendTranscript = html => {
                    transcript.dispatchEvent(new CustomEvent(
                      'htmx:sseBeforeMessage', {bubbles: true}
                    ));
                    transcript.insertAdjacentHTML('beforeend', html);
                    transcript.dispatchEvent(new CustomEvent(
                      'htmx:sseMessage', {bubbles: true}
                    ));
                  };

                  await settle();
                  const initialGap = bottomGap();
                  document.body.dispatchEvent(new CustomEvent('agentdeck:optimistic-send'));
                  await settle();

                  root.scrollTo(0, root.scrollHeight - root.clientHeight - 400);
                  await settle();
                  const readingPosition = root.scrollTop;

                  // The request can finish after the reader has already moved.
                  // Its late acknowledgement must not re-arm bottom-follow.
                  form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                    bubbles: true, detail: {successful: true, elt: form}
                  }));
                  appendTranscript(
                    '<div class="ev user" data-observed-queue-id="1" style="min-height:80px">' +
                    '<div class="ev-text">queued message</div></div>'
                  );
                  swap(status);
                  await settle();
                  appendTranscript('<div class="ev" style="min-height:80px">working</div>');
                  swap(status);
                  await settle();
                  appendTranscript('<div class="ev" style="min-height:80px">reply</div>');
                  swap(status);
                  await settle();
                  const positionWhileReading = root.scrollTop;
                  const pendingAfterReconcile = document.querySelectorAll(
                    '[data-pending-message]'
                  ).length;
                  const remainingQueueId = document.querySelector(
                    '[data-pending-message]'
                  )?.dataset.queueId;

                  root.scrollTo(0, root.scrollHeight - root.clientHeight - 20);
                  await settle();
                  appendTranscript(
                    '<div class="ev" style="min-height:120px">continued reply</div>'
                  );
                  await settle();
                  return {
                    initialGap,
                    readingPosition,
                    positionWhileReading,
                    pendingAfterReconcile,
                    remainingQueueId,
                    resumedGap: bottomGap(),
                  };
                }"""
            )
            results.append(result)
            await page.close()
        await browser.close()

    for result in results:
        assert result["initialGap"] <= 1
        assert result["positionWhileReading"] == result["readingPosition"]
        assert result["pendingAfterReconcile"] == 1
        assert result["remainingQueueId"] == "2"
        assert result["resumedGap"] <= 1


def _app_with_a_long_transcript(tmp_path):
    """A real Claude chat with enough history that its page has to scroll."""
    project = tmp_path / "projects" / "-tmp"
    project.mkdir(parents=True)
    started = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)
    lines = []
    for turn in range(20):
        stamp = started + timedelta(minutes=turn * 2)
        lines.append(
            {
                "type": "user",
                "timestamp": stamp.isoformat(),
                "message": {"role": "user", "content": f"Question {turn + 1}?"},
            }
        )
        lines.append(
            {
                "type": "assistant",
                "timestamp": (stamp + timedelta(minutes=1)).isoformat(),
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": f"Answer {turn + 1}."}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
    lines[-1]["message"]["content"][0]["text"] = "the newest message"
    (project / "sid1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            show_when_idle=True,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    app.state.config.inject.enabled = True
    return app


# A chat holding a screenshot: the picture's bytes are what `load` waits for.
_SLOW_IMAGE_PATH = "/slow-screenshot.png"
_ATTACH_SLOW_IMAGE = f"""
window.__attachSlowImage = function () {{
  var transcript = document.querySelector('.transcript');
  if (!transcript) return;
  var image = document.createElement('img');
  image.src = '{_SLOW_IMAGE_PATH}';
  image.width = 320;
  image.height = 240;
  image.alt = 'a screenshot in the newest message';
  transcript.appendChild(image);
}};
document.addEventListener('DOMContentLoaded', window.__attachSlowImage);
"""
_BOTTOM_GAP = """() => {
  const root = document.querySelector('.session-detail');
  return root.scrollHeight - root.clientHeight - root.scrollTop;
}"""


@pytest.mark.browser
async def test_opening_a_chat_lands_on_its_newest_message_before_the_picture_arrives(
    tmp_path,
):
    # Issue #851: the chat used to sit at the top of its history until the whole
    # document had loaded — every image included — and only then jump to the
    # newest message. Both ways in are covered: a session link opened as a page,
    # and a card opened in place from the deck, which mounts the chat with the
    # document already loaded and has no second `load` event to wait for.
    app = _app_with_a_long_transcript(tmp_path)
    release_image = asyncio.Event()
    pixel = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )

    async def serve(route):
        await release_image.wait()
        await route.fulfill(status=200, content_type="image/png", body=pixel)

    results = {}
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for opened_in_place in (False, True):
            release_image.clear()
            context = await browser.new_context(
                viewport={"width": 390, "height": 844}, service_workers="block"
            )
            await context.add_init_script(_ATTACH_SLOW_IMAGE)
            page = await context.new_page()
            # Only the picture is held back: routing the whole page would proxy
            # the SSE stream too and the document would never finish loading.
            await page.route(f"**{_SLOW_IMAGE_PATH}", serve)

            if opened_in_place:
                await page.goto(f"{base}/chats", wait_until="load")
                await page.locator("#sessions a.session").first.click()
                await page.wait_for_function(
                    "() => location.pathname.startsWith('/chats/')"
                )
                # The chat arrives as a fragment, so its picture is attached the
                # way the transcript's own would be: after the mount.
                await page.wait_for_selector(".transcript .ev")
                await page.evaluate("() => window.__attachSlowImage()")
            else:
                await page.goto(
                    f"{base}/sessions/claude_code:test:sid1",
                    wait_until="domcontentloaded",
                )
                await page.wait_for_selector(".transcript .ev")

            await page.wait_for_selector(".transcript img")
            await page.wait_for_function(
                "() => !document.querySelector('.transcript img').complete"
            )
            await page.evaluate(
                """() => new Promise(resolve =>
                     requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
            )
            waiting = await page.evaluate(_BOTTOM_GAP)
            newest_visible = await page.locator("text=the newest message").is_visible()

            release_image.set()
            await page.wait_for_function(
                "() => document.querySelector('.transcript img').complete"
            )
            await page.evaluate(
                """() => new Promise(resolve =>
                     requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
            )
            settled = await page.evaluate(_BOTTOM_GAP)

            # Someone typing owns the view: later growth must not move it.
            await page.locator("#inject-message").focus()
            typing_at = await page.evaluate(
                "() => document.querySelector('.session-detail').scrollTop"
            )
            await page.evaluate(
                "() => { document.querySelector('.transcript img').style.minHeight = '400px'; }"
            )
            await page.evaluate(
                """() => new Promise(resolve =>
                     requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
            )
            still_typing_at = await page.evaluate(
                "() => document.querySelector('.session-detail').scrollTop"
            )

            # A reader who moves keeps their place, picture or no picture.
            await page.evaluate(
                "() => { const root = document.querySelector('.session-detail');"
                " root.scrollTo(0, root.scrollHeight - root.clientHeight - 400); }"
            )
            await page.evaluate(
                """() => new Promise(resolve =>
                     requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
            )
            reading = await page.evaluate(
                "() => document.querySelector('.session-detail').scrollTop"
            )
            await page.evaluate(
                "() => { document.querySelector('.transcript .ev:last-of-type')"
                ".style.minHeight = '500px'; }"
            )
            await page.evaluate(
                """() => new Promise(resolve =>
                     requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
            )
            still_reading = await page.evaluate(
                "() => document.querySelector('.session-detail').scrollTop"
            )

            results["in place" if opened_in_place else "as a page"] = {
                "waiting": waiting,
                "newestVisible": newest_visible,
                "settled": settled,
                "typingAt": typing_at,
                "stillTypingAt": still_typing_at,
                "reading": reading,
                "stillReading": still_reading,
            }
            await page.unroute_all(behavior="ignoreErrors")
            await context.close()
        await browser.close()

    for how, result in results.items():
        # The end of the chat is there while the picture is still on the wire.
        # A pixel or two of slack: rounding, not a scroll position.
        assert result["waiting"] <= 4, f"opened {how}: {result}"
        assert result["newestVisible"], f"opened {how}: {result}"
        # And the picture landing does not leave the reader above the end.
        assert result["settled"] <= 4, f"opened {how}: {result}"
        # A caret in the composer, and a scroll away, both end the follow.
        assert result["stillTypingAt"] == result["typingAt"], f"opened {how}: {result}"
        assert result["stillReading"] == result["reading"], f"opened {how}: {result}"


@pytest.mark.browser
async def test_a_button_jumps_up_to_your_latest_message(tmp_path):
    # Issue #1810: after a long answer the reader is parked at the end of it and
    # has to scroll by hand to find where their own question started.
    app = _app_with_a_long_transcript(tmp_path)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base}/sessions/claude_code:test:sid1", wait_until="load")
        # A long final answer is the case that hurts: the chat opens parked at
        # the end of it, several screens below the question that produced it.
        await page.evaluate(
            """() => {
                 const rows = document.querySelectorAll('.transcript .ev.assistant');
                 rows[rows.length - 1].style.minHeight = '2000px';
               }"""
        )
        await page.wait_for_function(
            """() => {
                 const root = document.querySelector('.session-detail');
                 return root.scrollHeight - root.clientHeight - root.scrollTop <= 4;
               }"""
        )
        at_the_end = await page.evaluate(
            "() => document.querySelector('.session-detail').scrollTop"
        )

        button = page.locator("[data-last-user-jump]")
        assert await button.is_visible()
        await button.click()
        # A live chat keeps growing while the jump animates. Bottom-follow must
        # already have let go, or this taller transcript pulls the reader
        # straight back down to the newest message.
        await page.evaluate(
            """() => {
                 const row = document.createElement('div');
                 row.className = 'ev assistant';
                 row.style.minHeight = '400px';
                 row.textContent = 'a later answer';
                 document.querySelector('.transcript').appendChild(row);
               }"""
        )
        # Smooth scrolling, so wait for the last user message to settle just
        # under the pinned header rather than reading one frame of the animation.
        await page.wait_for_function(
            """() => {
                 const rows = document.querySelectorAll(
                   '.transcript .ev.user:not(.pending-message)');
                 const target = rows[rows.length - 1];
                 const pane = document.querySelector('.session-detail');
                 const head = document.querySelector('.session-detail-head');
                 const gap = target.getBoundingClientRect().top
                   - pane.getBoundingClientRect().top
                   - head.getBoundingClientRect().height;
                 return Math.abs(gap - 8) <= 2;
               }"""
        )
        landed = await page.evaluate(
            """() => {
                 const rows = document.querySelectorAll(
                   '.transcript .ev.user:not(.pending-message)');
                 const target = rows[rows.length - 1];
                 return {
                   text: target.innerText,
                   scrollTop: document.querySelector('.session-detail').scrollTop,
                 };
               }"""
        )
        assert "Question 20?" in landed["text"]
        assert landed["scrollTop"] < at_the_end

        await page.evaluate(
            """() => new Promise(resolve =>
                 requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
        )
        still_reading = await page.evaluate(
            "() => document.querySelector('.session-detail').scrollTop"
        )
        # A pixel of slack: the smooth scroll's own last frame, not a jump back.
        assert abs(still_reading - landed["scrollTop"]) <= 2
        await context.close()
        await browser.close()


@pytest.mark.browser
async def test_message_draft_survives_reload_and_newer_text_is_not_cleared(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")
    html = response.text.replace(
        "</main>",
        '<form class="inject-form" data-clear-on-send '
        'hx-post="/sessions/codex:test:draft/inject">'
        '<textarea id="inject-message" name="message"></textarea>'
        '<input type="file" name="images"></form></main>',
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 412, "height": 800})

        async def serve(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.fulfill(status=200, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        prompt = page.locator("#inject-message")
        await prompt.fill("unfinished draft")
        await page.reload()
        restored = await prompt.input_value()

        result = await page.evaluate(
            """() => {
                const form = document.querySelector('.inject-form');
                const input = form.querySelector('textarea[name="message"]');
                input.value = 'message being sent';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                // Real htmx fires beforeSend next; the box empties there (#2843).
                form.dispatchEvent(new CustomEvent('htmx:beforeSend', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                input.value = 'new draft typed while sending';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                const newerDraft = input.value;

                input.value = 'sent normally';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                // Real htmx fires beforeSend next; the box empties there (#2843).
                form.dispatchEvent(new CustomEvent('htmx:beforeSend', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                const afterNormalSend = input.value;

                input.value = 'next draft while queued';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                // HTMX re-homes completion events from a polling status node
                // after outerHTML removes it. That event reaches the connected
                // parent form without a matching form beforeRequest.
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                return {
                  newerDraft,
                  afterNormalSend,
                  afterQueuedStatusPoll: input.value,
                };
            }"""
        )
        await page.reload()
        afterSentReload = await prompt.input_value()
        await browser.close()

    assert restored == "unfinished draft"
    assert result == {
        "newerDraft": "new draft typed while sending",
        "afterNormalSend": "",
        "afterQueuedStatusPoll": "next draft while queued",
    }
    assert afterSentReload == "next draft while queued"


@pytest.mark.browser
async def test_working_marker_is_an_overlay_that_does_not_change_page_height(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    # The elapsed ticker is a static module now (so a detail pane mounted by
    # client-side navigation also gets it); set_content has no server, so inline it.
    ticker = (static_dir / "activity_elapsed.js").read_text()
    html = response.text.replace(
        "</head>", f"<style>{css}</style><script>{ticker}</script></head>"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        await page.set_content(html)
        result = await page.evaluate(
            """() => {
                const stage = document.querySelector('.transcript-stage');
                const activity = document.querySelector('#tool-activity');
                activity.replaceChildren();
                const before = stage.getBoundingClientRect().height;
                activity.innerHTML = '<div class="ev tool-wait" data-activity-elapsed="3">' +
                  '<span class="tool-wait-label">Working</span>' +
                  '<span class="tool-wait-elapsed"></span></div>';
                activity.firstElementChild._agentdeckMountedAt = Date.now() - 5000;
                const after = stage.getBoundingClientRect().height;
                return {
                    before,
                    after,
                    position: getComputedStyle(activity).position,
                    transcriptBottom: document.querySelector('.transcript')
                      .getBoundingClientRect().bottom,
                    activityTop: activity.getBoundingClientRect().top,
                    bottomGutter: getComputedStyle(stage).paddingBottom,
                };
            }"""
        )
        await page.wait_for_timeout(1100)
        elapsed = await page.locator(".tool-wait-elapsed").text_content()
        await browser.close()

    assert result["before"] == result["after"]
    assert result["position"] == "absolute"
    assert result["bottomGutter"] == "40px"
    assert result["activityTop"] >= result["transcriptBottom"]
    assert int(elapsed.removesuffix("s")) >= 8


@pytest.mark.browser
async def test_mobile_session_composer_is_compact():
    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section>
        <form id="message-form" class="inject-form">
          <label for="inject-message">Message</label>
          <div class="composer-input">
            <textarea id="inject-message" rows="3"></textarea>
            <div class="composer-suggestions" hidden></div>
          </div>
          <label class="image-picker">
            ＋ Attach file <span class="paste-hint">or paste screenshot</span>
          </label>
          <input class="image-input" type="file">
          <div class="composer-actions">
            <span class="composer-hint">Enter to send</span>
            <details class="effort-menu">
              <summary>Effort</summary>
              <div class="effort-menu-panel">
                <label for="inject-effort">Reasoning effort</label>
                <select id="inject-effort">
                  <option>Keep current</option><option>High</option>
                </select>
              </div>
            </details>
            <div class="composer-controls">
              <button class="stop-button" type="submit" form="interrupt-form">Stop</button>
              <button>Send</button>
            </div>
          </div>
          <div id="inject-result" class="inject-result running">
            <span class="send-spinner"></span>
          </div>
        </form><form id="interrupt-form"></form>
      </section></main></body>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.evaluate(
            """() => {
                const form = document.querySelector('.inject-form');
                const textarea = form.querySelector('textarea');
                const picker = form.querySelector('.image-picker');
                const actions = form.querySelector('.composer-actions');
                const indicator = form.querySelector('#inject-result');
                const effort = form.querySelector('.effort-menu');
                const stop = form.querySelector('.stop-button').getBoundingClientRect();
                const send = form.querySelector('.composer-controls button:last-child')
                  .getBoundingClientRect();
                const main = document.querySelector('main');
                const closedHeight = form.getBoundingClientRect().height;
                const closedByDefault = !effort.open;
                effort.open = true;
                const openHeight = form.getBoundingClientRect().height;
                const panelVisible = effort.querySelector('.effort-menu-panel')
                  .getBoundingClientRect().height > 0;
                effort.open = false;
                effort.remove();
                const noEffortHeight = form.getBoundingClientRect().height;
                return {
                    formHeight: closedHeight,
                    noEffortHeight,
                    openHeight,
                    closedByDefault,
                    panelVisible,
                    textareaHeight: textarea.getBoundingClientRect().height,
                    controlsAligned: Math.abs(
                        picker.getBoundingClientRect().top - actions.getBoundingClientRect().top
                    ) < 2,
                    hintHidden: getComputedStyle(actions.querySelector('.composer-hint')).display,
                    pasteHintHidden: getComputedStyle(form.querySelector('.paste-hint')).display,
                    indicatorPosition: getComputedStyle(indicator).position,
                    stopBesideSend: stop.right < send.left && Math.abs(stop.top - send.top) < 2,
                    controlsInsideForm: send.right <= form.getBoundingClientRect().right,
                    stopUsesInterruptForm: form.querySelector('.stop-button').form.id
                      === 'interrupt-form',
                    keyboardGap: main.getBoundingClientRect().bottom
                        - form.getBoundingClientRect().bottom,
                };
            }"""
        )
        await browser.close()

    assert metrics["formHeight"] < 145
    assert metrics["formHeight"] == metrics["noEffortHeight"] == metrics["openHeight"]
    assert metrics["closedByDefault"]
    assert metrics["panelVisible"]
    assert metrics["textareaHeight"] == 58
    assert metrics["controlsAligned"]
    assert metrics["hintHidden"] == "none"
    assert metrics["pasteHintHidden"] == "none"
    assert metrics["indicatorPosition"] == "absolute"
    assert metrics["stopBesideSend"]
    assert metrics["controlsInsideForm"]
    assert metrics["stopUsesInterruptForm"]
    assert metrics["keyboardGap"] == 0


@pytest.mark.browser
async def test_composer_autocomplete_filters_selects_and_does_not_submit_early():
    script = (
        Path(__file__).parents[1]
        / "src/agentdeck/web/static/composer_autocomplete.js"
    ).read_text()
    html = """
      <form id="message-form">
        <div data-composer-autocomplete>
          <textarea name="message" aria-expanded="false"></textarea>
          <div id="suggestions" data-composer-suggestions role="listbox" hidden></div>
          <script type="application/json">[
            {"value":"/compact","label":"/compact","description":"Compact","kind":"command"},
            {"value":"/context","label":"/context","description":"Context","kind":"command"},
            {"value":"$rate-my-pr","label":"$rate-my-pr","description":"Review","kind":"skill"}
          ]</script>
        </div>
      </form>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html)
        await page.evaluate(
            """() => {
              window.submitCount = 0;
              document.querySelector('form').addEventListener('submit', event => {
                event.preventDefault(); window.submitCount += 1;
              });
              document.addEventListener('keydown', event => {
                if (event.key === 'Enter' && !event.shiftKey) event.target.form.requestSubmit();
              });
            }"""
        )
        await page.add_script_tag(content=script)
        await page.evaluate("window.dispatchEvent(new Event('DOMContentLoaded'))")
        prompt = page.locator('textarea[name="message"]')

        await prompt.fill("/con")
        assert await prompt.get_attribute("aria-expanded") == "true"
        assert await page.locator('[role="option"]').all_text_contents() == [
            "/contextcommandContext"
        ]
        await prompt.press("Enter")
        assert await prompt.input_value() == "/context "
        assert await page.evaluate("window.submitCount") == 0

        await prompt.press("Enter")
        assert await page.evaluate("window.submitCount") == 1

        await prompt.fill("/rate")
        assert await page.locator('[role="option"]').all_text_contents() == [
            "$rate-my-prskillReview"
        ]
        await prompt.press("Tab")
        assert await prompt.input_value() == "$rate-my-pr "

        await prompt.fill("Please use $rate")
        assert await page.locator('[role="option"]').count() == 1
        await prompt.press("Tab")
        assert await prompt.input_value() == "Please use $rate-my-pr "
        await browser.close()


@pytest.mark.parametrize("viewport_width", [390, 1100])
@pytest.mark.browser
async def test_rendered_session_composer_autocompletes_installed_skill(
    tmp_path, viewport_width
):
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    project = tmp_path / "project"
    skill = project / ".claude" / "skills" / "project-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: project-review\ndescription: Review this project's current changes\n---\n"
    )
    session_key = "claude_code:test:sid1"
    app.state.app_state.update_session(
        replace(
            app.state.app_state.sessions[session_key],
            cwd=project,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": viewport_width, "height": 844})
        await page.goto(f"{base}/sessions/{session_key}")
        prompt = page.locator("#inject-message")
        await prompt.fill("/project")
        panel = page.locator("#composer-suggestions")
        await panel.wait_for(state="visible")
        assert await panel.locator('[role="option"]').all_text_contents() == [
            "/project-reviewskillReview this project's current changes"
        ]
        assert await prompt.get_attribute("aria-activedescendant") == (
            "composer-suggestions-option-0"
        )
        await page.locator(".inject-form").scroll_into_view_if_needed()
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir and viewport_width == 390:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-673-skill-autocomplete-mobile.png")
            )
        await panel.locator('[role="option"]').click()
        assert await prompt.input_value() == "/project-review "
        assert await prompt.get_attribute("aria-expanded") == "false"
        await browser.close()


@pytest.mark.parametrize("viewport_width", [390, 1100])
@pytest.mark.browser
async def test_rendered_codex_composer_slash_finds_native_skill(
    tmp_path, viewport_width
):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.config.inject.enabled = True
    project = tmp_path / "project"
    skill = project / ".agents" / "skills" / "project-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: project-review\ndescription: Review this project's current changes\n---\n"
    )
    session_key = "codex:test:sid1"
    app.state.app_state.update_session(
        Session(
            key=session_key,
            account_key="codex:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            cwd=project,
            title="Codex slash skill alias",
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": viewport_width, "height": 844})
        await page.goto(f"{base}/sessions/{session_key}")
        prompt = page.locator("#inject-message")
        await prompt.fill("/project")
        panel = page.locator("#composer-suggestions")
        await panel.wait_for(state="visible")
        assert await panel.locator('[role="option"]').all_text_contents() == [
            "$project-reviewskillReview this project's current changes"
        ]
        await page.locator(".inject-form").scroll_into_view_if_needed()
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir and viewport_width == 390:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir)
                    / "issue-673-codex-slash-autocomplete-mobile.png"
                )
            )
        await panel.locator('[role="option"]').click()
        assert await prompt.input_value() == "$project-review "
        assert await prompt.get_attribute("aria-expanded") == "false"
        await browser.close()


async def test_card_shows_agent_response(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            last_prompt="what is the answer",
            last_text="the answer is 42",
        )
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    assert "what is the answer" in r.text  # user's prompt
    assert "the answer is 42" in r.text  # agent's reply is now in the list view


PROMPT_290 = "deploy the staging fix and tell me when it is live"
REPLY_290 = " ".join(
    f"Sentence {n} of a very long reply that easily overflows the card window."
    for n in range(1, 15)
)


def _session_290(**overrides):
    fields = dict(
        key="claude_code:test:sid1",
        account_key="claude_code:test",
        session_id="sid1",
        status=SessionStatus.LIVE,
        title="Staging deploy",
        last_prompt=PROMPT_290,
        last_text=REPLY_290,
        last_role="agent",
    )
    fields.update(overrides)
    return Session(**fields)


@pytest.mark.browser
async def test_your_latest_message_is_pinned_outside_the_scrolling_reply_window(tmp_path):
    """Issue #290: the reply window scrolls to its newest text, so a long agent
    reply used to push your own prompt out of the card. It now has its own row."""
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(_session_290())
    async with _client(app) as client:
        response = await client.get("/chats")

    card = _session_card(response.text, "claude_code:test:sid1")
    assert 'class="card-mine"' in card
    assert card.index('class="card-mine"') < card.index('class="card-latest"')
    # the prompt lives in the pinned row only — the window streams the agent side
    assert PROMPT_290 in card.split('class="card-latest"', 1)[0]
    scroll = card.split('class="latest-scroll"', 1)[1].split("</span></div>", 1)[0]
    assert PROMPT_290 not in scroll
    assert REPLY_290 in scroll
    # data-prompt still carries it: the list filter and the JS feed both read it
    assert f'data-prompt="{PROMPT_290}"' in card

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    html = _with_inline_cards_script(html)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        metrics = {}
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(html)
            # the marquee ran: setup() offsets the window to its newest text
            await page.wait_for_function(
                "() => { const i = document.querySelector('.card-latest .latest-scroll');"
                " return i && i.style.transform !== ''; }"
            )
            metrics[width] = await page.locator(
                '.session[data-session-key="claude_code:test:sid1"]'
            ).evaluate(
                """(card, prompt) => {
                  const mine = card.querySelector('.card-mine');
                  const window_ = card.querySelector('.card-latest');
                  const mineRect = mine.getBoundingClientRect();
                  const cardRect = card.getBoundingClientRect();
                  return {
                    mineText: mine.textContent,
                    mineHeight: mineRect.height,
                    insideCard: mineRect.top >= cardRect.top
                      && mineRect.bottom <= cardRect.bottom,
                    windowHasPrompt: window_.textContent.includes(prompt),
                    userLinesInWindow: window_.querySelectorAll(
                      '.latest-line:not(.agent)'
                    ).length,
                    scrollWidth: document.documentElement.scrollWidth,
                    viewportWidth: document.documentElement.clientWidth,
                  };
                }""",
                PROMPT_290,
            )
            await page.close()
        await browser.close()

    for width, m in metrics.items():
        assert PROMPT_290 in m["mineText"], width
        assert m["mineHeight"] > 0, width          # actually painted, not clipped away
        assert m["insideCard"], width              # and inside the card, not overflowing it
        assert not m["windowHasPrompt"], width     # no duplicate copy in the reply window
        assert m["userLinesInWindow"] == 0, width
        assert m["scrollWidth"] <= m["viewportWidth"], width


@pytest.mark.browser
async def test_pinned_prompt_shows_before_any_reply_without_a_blank_window(tmp_path):
    """A just-sent message is the whole point of the pin: it must show with no
    agent reply yet, and the empty reply window must not leave a gap."""
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(_session_290(last_text=None, last_role="user"))
    async with _client(app) as client:
        response = await client.get("/chats")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.locator(
            '.session[data-session-key="claude_code:test:sid1"]'
        ).evaluate(
            """card => {
              const mine = card.querySelector('.card-mine');
              const window_ = card.querySelector('.card-latest');
              const meta = card.querySelector('.session-meta');
              return {
                mineText: mine.textContent,
                mineHeight: mine.getBoundingClientRect().height,
                windowHeight: window_.getBoundingClientRect().height,
                gap: meta.getBoundingClientRect().top
                  - mine.getBoundingClientRect().bottom,
              };
            }"""
        )
        await browser.close()

    assert PROMPT_290 in metrics["mineText"]
    assert metrics["mineHeight"] > 0
    assert metrics["windowHeight"] == 0        # no blank reply window reserved
    assert metrics["gap"] < 20                 # meta follows the prompt directly


def test_a_runtime_failure_written_as_a_reply_renders_as_a_failure(tmp_path):
    """Claude Code's API-error apology arrives as an ordinary assistant message,
    so without a marker of its own it reads as the model's considered answer
    (#1463). The class is what the stylesheet colours it by."""
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    templates = _app_with_state(tmp_path).state.templates
    html = render_transcript_events(
        templates,
        [
            TranscriptEvent(seq=1, role="assistant", text="here is my answer"),
            TranscriptEvent(
                seq=2, role="assistant", text="API Error: 529 Overloaded.", failed=True
            ),
        ],
        session_key="k",
    )

    assert 'class="ev assistant failed"' in html
    assert 'class="ev assistant"' in html  # the real reply is untouched
    assert "API Error: 529 Overloaded." in html


def test_tool_calls_visible_outputs_hidden_and_live_marker(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import (
        SESSION_FRAGMENTS_BY_NAME,
        SessionView,
        render_transcript_events,
    )

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    timestamp = datetime(2026, 7, 15, 7, 42, tzinfo=UTC)
    events = [
        TranscriptEvent(seq=1, role="tool", text="huge noisy tool result"),
        TranscriptEvent(
            seq=2,
            role="assistant",
            tool_name="Bash",
            tool_summary="cmd: uv run pytest -q",
            tool_detail="uv run pytest -q --verbose tests/test_web.py",
            text=None,
            ts=timestamp,
        ),
        TranscriptEvent(
            seq=3,
            role="assistant",
            tool_name="exec",
            tool_display_name="Approval",
            tool_summary="reason: Allow deploying this change?",
            tool_detail="Reason\nAllow deploying this change?\n\nCommand\ngit push",
        ),
        TranscriptEvent(seq=4, role="assistant", text="here is my answer"),
        TranscriptEvent(seq=5, role="user", text="queued follow-up", queued=True),
    ]
    html = render_transcript_events(templates, events)
    assert "huge noisy tool result" not in html  # past tool result dropped
    assert "tool-call" in html
    assert '<details class="ev tool tool-call" data-tool-key="Bash">' in html
    assert "Bash" in html
    assert "cmd: uv run pytest -q" in html
    assert "uv run pytest -q --verbose tests/test_web.py" in html
    assert "Approval" in html
    assert "reason: Allow deploying this change?" in html
    assert "Reason\nAllow deploying this change?" in html
    assert "here is my answer" in html  # real assistant text kept
    assert "queued follow-up" in html  # queued turns look like ordinary user chat
    assert "user · queued" not in html
    assert 'class="ev-time"' in html
    assert 'datetime="2026-07-15T07:42:00+00:00"' in html
    # the live marker appears only while actively working
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    activity_fragment = SESSION_FRAGMENTS_BY_NAME["tools"]
    activity = activity_fragment.render(
        templates,
        SessionView(
            current=session,
            effective=session,
            label="Using tools",
            age=12.9,
        ),
    )
    assert "Using tools" in activity
    assert 'data-activity-elapsed="12"' in activity
    assert ">12s</span>" in activity
    assert "tool-wait" not in activity_fragment.render(
        templates, SessionView(current=session, effective=session)
    )


@pytest.mark.browser
async def test_long_user_and_system_messages_collapse_but_assistant_stays_open(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    collapse = (static_dir / "message_collapse.js").read_text()
    events = [
        TranscriptEvent(seq=1, role="user", text="Check this\nKeep it narrow\nThanks"),
        TranscriptEvent(
            seq=2,
            role="user",
            text=(
                "Please update the transcript view.\n"
                "Keep assistant replies fully visible.\n"
                "Collapse long user messages.\n"
                "Make expansion available."
            ),
        ),
        TranscriptEvent(
            seq=3,
            role="system",
            text="Workspace policy\nUse this worktree\nRun browser tests\nOpen a pull request",
        ),
        TranscriptEvent(
            seq=4,
            role="assistant",
            text=(
                "Implemented the shared view.\n"
                "User messages collapse.\n"
                "System messages collapse.\n"
                "Assistant replies stay open."
            ),
        ),
        TranscriptEvent(
            seq=5,
            role="user",
            text=(
                "This long user message wraps naturally on a narrow screen and "
                "becomes a compact preview without changing the assistant response."
            ),
        ),
    ]
    rendered = render_transcript_events(app.state.templates, events, session_key="k")
    appended = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=6,
                role="system",
                text=(
                    "Live policy update\nPreserve scroll position\n"
                    "Keep focus stable\nApply the same collapse"
                ),
            )
        ],
        session_key="k",
    )
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section class="session-detail">
        <div class="transcript" data-session-key="k">{rendered}</div>
      </section></main></body>
    """

    results = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(html)
            await page.add_script_tag(content=collapse)
            results[width] = await page.evaluate(
                """async (appended) => {
                  const frame = () => new Promise(requestAnimationFrame);
                  await frame(); await frame();
                  let settledMutations = 0;
                  const settleObserver = new MutationObserver(records => {
                    settledMutations += records.reduce(
                      (count, record) => count + record.addedNodes.length
                        + record.removedNodes.length,
                      0
                    );
                  });
                  settleObserver.observe(document.querySelector('.transcript'), {
                    childList: true, subtree: true,
                  });
                  await new Promise(resolve => setTimeout(resolve, 80));
                  settleObserver.disconnect();
                  const rows = Array.from(document.querySelectorAll('.transcript > .ev'));
                  const height = row => Math.round(
                    row.querySelector('.ev-message-body, .ev-text').getBoundingClientRect().height
                  );
                  const initial = rows.map(row => ({
                    role: row.classList.contains('user') ? 'user'
                      : row.classList.contains('system') ? 'system' : 'assistant',
                    collapsible: row.classList.contains('message-collapsible'),
                    button: !!row.querySelector('.message-collapse-toggle'),
                    buttonVisible: !!row.querySelector('.message-collapse-toggle') &&
                      getComputedStyle(row.querySelector('.message-collapse-toggle'))
                        .display !== 'none',
                    height: height(row),
                  }));
                  rows[1].querySelector('.message-collapse-toggle').click();
                  await frame();
                  const expanded = {
                    height: height(rows[1]),
                    label: rows[1].querySelector('.message-collapse-toggle').textContent,
                    aria: rows[1].querySelector('.message-collapse-toggle')
                      .getAttribute('aria-expanded'),
                  };
                  document.querySelector('.transcript')
                    .insertAdjacentHTML('beforeend', appended);
                  await frame(); await frame();
                  const live = document.querySelector('.transcript > .ev:last-child');
                  return {
                    initial, expanded, settledMutations,
                    liveCollapsible: live.classList.contains('message-collapsible'),
                    liveLabel: live.querySelector('.message-collapse-toggle').textContent,
                    overflowX: document.documentElement.scrollWidth - innerWidth,
                  };
                }""",
                appended,
            )
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir and width == 320:
                assert results[width]["expanded"]["aria"] == "true"
                assert results[width]["liveCollapsible"] is True
                await page.locator(".transcript").screenshot(
                    path=str(
                        Path(screenshot_dir) / "issue-1425-message-collapse-mobile.png"
                    )
                )
            await page.close()
        await browser.close()

    for width, result in results.items():
        short, user, system, assistant, wrapped = result["initial"]
        assert result["settledMutations"] == 0, width
        assert short == {
            "role": "user", "collapsible": False, "button": True,
            "buttonVisible": False, "height": 61,
        }, width
        assert user["collapsible"] is True, width
        assert system["collapsible"] is True, width
        assert user["buttonVisible"] is True, width
        assert system["buttonVisible"] is True, width
        assert user["height"] == system["height"] == 61, width
        assert assistant["button"] is False, width
        assert assistant["height"] > user["height"], width
        assert wrapped["collapsible"] is (width == 320), width
        assert result["expanded"]["height"] > user["height"], width
        assert result["expanded"]["label"] == "Show less", width
        assert result["expanded"]["aria"] == "true", width
        assert result["liveCollapsible"] is True, width
        assert result["liveLabel"] == "Show more", width
        assert result["overflowX"] == 0, width


@pytest.mark.browser
async def test_expanded_message_keeps_its_place_while_events_arrive(tmp_path):
    """Issue #1845: an open message must not be re-measured on every new event.

    Measuring clamps the row back to three lines for one layout. With a long
    message open that shortens the page enough for the browser to pin the
    reader's scroll position to the new bottom, and re-expanding does not put it
    back — so a working agent dragged the reader to the bottom once a second.
    """
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    collapse = (static_dir / "message_collapse.js").read_text()
    follow = (static_dir / "session_bottom_follow.js").read_text()
    long_text = "\n".join(f"Instruction line {i} for the agent." for i in range(200))
    rendered = render_transcript_events(
        app.state.templates,
        [TranscriptEvent(seq=1, role="user", text=long_text)],
        session_key="k",
    )
    appended = render_transcript_events(
        app.state.templates,
        [TranscriptEvent(seq=2, role="assistant", text="Working on it.")],
        session_key="k",
    )
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section class="session-detail">
        <div class="transcript" data-session-key="k">{rendered}</div>
      </section></main></body>
    """

    settle = """async () => {
      const frame = () => new Promise(requestAnimationFrame);
      await frame(); await frame(); await frame();
      const row = document.querySelector('.transcript > .ev.user');
      return {
        scrollTop: window.__pane.scrollTop,
        stillExpanded: row.classList.contains('message-expanded'),
        label: row.querySelector('.message-collapse-toggle').textContent,
      };
    }"""

    results = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (900, 320):
            page = await browser.new_page(viewport={"width": width, "height": 600})
            await page.set_content(html)
            # The real pair: collapse measurement alongside the chat's own
            # bottom-follow, so the reader's place is tested through the
            # lifecycle that actually moves it.
            await page.add_script_tag(content=collapse)
            await page.add_script_tag(content=follow)
            events = await page.evaluate(
                """async (appended) => {
                  const frame = () => new Promise(requestAnimationFrame);
                  await frame(); await frame();
                  const row = document.querySelector('.transcript > .ev.user');
                  row.querySelector('.message-collapse-toggle').click();
                  await frame(); await frame();
                  // Whichever box actually scrolls on this layout, as the
                  // chat's own bottom-follow picks it.
                  const detail = document.querySelector('.session-detail');
                  window.__pane = getComputedStyle(detail).overflowY === 'auto'
                    ? detail : document.scrollingElement;
                  // Read some way down the open message, as a reader would.
                  // This also hands bottom-follow back, exactly as scrolling up
                  // out of the newest message does.
                  window.__pane.scrollTop = 900;
                  await frame();
                  const before = window.__pane.scrollTop;
                  const transcript = document.querySelector('.transcript');
                  // Three events arriving while the agent works.
                  for (let i = 0; i < 3; i++) {
                    transcript.insertAdjacentHTML('beforeend', appended);
                    await frame(); await frame();
                  }
                  return {before, after: window.__pane.scrollTop};
                }""",
                appended,
            )
            # Height only — a mobile keyboard opening, the URL bar collapsing.
            await page.set_viewport_size({"width": width, "height": 650})
            taller = await page.evaluate(settle)
            # Width too — the one change that can rewrap a message.
            await page.set_viewport_size({"width": width - 40, "height": 650})
            narrower = await page.evaluate(settle)
            results[width] = {
                "events": events, "taller": taller, "narrower": narrower
            }
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir and width == 900:
                assert taller["stillExpanded"] is True
                await page.screenshot(
                    path=str(Path(screenshot_dir) / "issue-1845-after-events.png")
                )
            await page.close()
        await browser.close()

    for width, result in results.items():
        assert result["events"]["before"] == 900, width
        assert result["events"]["after"] == 900, width
        assert result["taller"] == {
            "scrollTop": 900, "stillExpanded": True, "label": "Show less"
        }, width
        assert result["narrower"] == {
            "scrollTop": 900, "stillExpanded": True, "label": "Show less"
        }, width


@pytest.mark.browser
async def test_repeated_tool_calls_merge_into_one_card_and_expand(tmp_path):
    """Issue #334: a run of same-tool calls is one card, and every call expands."""
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    groups = (static_dir / "tool_call_groups.js").read_text()

    def shell(seq, index):
        command = f"cd /var/home/me/agentdeck && rg -n 'pattern {index}' " + "src " * 40
        return TranscriptEvent(
            seq=seq,
            role="assistant",
            tool_name="Bash",
            tool_summary=f"command: {command}"[:160],
            tool_detail=f"Command\n{command}",
            ts=datetime(2026, 7, 15, 7, 42, tzinfo=UTC),
        )

    events = [shell(seq, seq) for seq in (1, 2, 3, 4)] + [
        TranscriptEvent(
            seq=5,
            role="assistant",
            tool_name="Read",
            tool_summary="file path: /var/home/me/agentdeck/AGENTS.md",
            tool_detail="File path\n/var/home/me/agentdeck/AGENTS.md",
        )
    ]
    rendered = render_transcript_events(templates, events, session_key="k")
    appended = render_transcript_events(templates, [shell(6, 6)], session_key="k")
    full_command = "rg -n 'pattern 1' " + "src " * 40
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section class="session-detail">
        <div class="transcript" data-session-key="k">{rendered}</div>
      </section></main></body>
    """

    results = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(html)
            await page.add_script_tag(content=groups)
            results[width] = await page.evaluate(
                """async (appended) => {
                  const frame = () => new Promise(requestAnimationFrame);
                  const transcript = document.querySelector('.transcript');
                  const rows = () => Array.from(transcript.querySelectorAll('.tool-call'));
                  const classesOf = () => rows().map(row => (
                    row.classList.contains('tool-run-lead') ? 'lead'
                      : row.classList.contains('tool-run-item') ? 'item' : 'alone'
                  ));
                  const visible = selector => rows()
                    .filter(row => {
                      const node = row.querySelector(selector);
                      return node && getComputedStyle(node).display !== 'none';
                    }).length;
                  await frame();

                  const before = classesOf();
                  const count = transcript.querySelector('.tool-run-count').textContent;
                  const shellRows = rows().slice(0, 4);
                  // Only the outer corners of the merged card stay rounded.
                  const corners = shellRows.map(row => {
                    const style = getComputedStyle(row);
                    return [style.borderTopLeftRadius, style.borderBottomLeftRadius]
                      .map(value => parseFloat(value) > 0 ? 'round' : 'square').join('/');
                  });
                  const seams = shellRows.slice(1).map((row, index) => Math.round(
                    row.getBoundingClientRect().top -
                    shellRows[index].getBoundingClientRect().bottom
                  ));
                  const readGap = Math.round(
                    rows()[4].getBoundingClientRect().top -
                    shellRows[3].getBoundingClientRect().bottom
                  );
                  const mergedHeight = Math.round(
                    shellRows[3].getBoundingClientRect().bottom -
                    shellRows[0].getBoundingClientRect().top
                  );
                  const headsVisible = visible('.tool-name');
                  const peeksVisible = visible('.tool-peek');

                  // A live SSE append lands after the unrelated Read call, so it
                  // starts a new run instead of rejoining the earlier one.
                  transcript.insertAdjacentHTML('beforeend', appended);
                  await frame();
                  const afterAppend = classesOf();
                  transcript.insertAdjacentHTML('beforeend', appended);
                  await frame();
                  const afterSecondAppend = classesOf();
                  const newRunCount = rows()[5].querySelector('.tool-run-count').textContent;

                  // Every call still expands to its own full input.
                  const target = shellRows[0];
                  target.querySelector('summary').click();
                  await frame();
                  return {
                    before, count, corners, seams, readGap, mergedHeight,
                    headsVisible, peeksVisible, afterAppend, afterSecondAppend,
                    newRunCount,
                    rowHeight: Math.round(shellRows[1].getBoundingClientRect().height),
                    expanded: target.querySelector('.ev-text').textContent,
                    expandedHeight:
                      target.querySelector('.ev-text').getBoundingClientRect().height,
                    overflowX: transcript.scrollWidth - transcript.clientWidth,
                  };
                }""",
                appended,
            )
            await page.close()
        await browser.close()

    for width, result in results.items():
        assert result["before"] == ["lead", "item", "item", "item", "alone"], width
        assert result["count"] == "×4", width
        assert result["seams"] == [0, 0, 0], width  # one card, no gaps inside the run
        # The turn's open summary line (#3200) caps the card, so its first row
        # is square on top too.
        assert result["corners"] == [
            "square/square", "square/square", "square/square", "square/round",
        ], width
        assert result["readGap"] == 8, width  # a different tool keeps its own card
        assert result["headsVisible"] == 2, width  # one Bash head, one Read head
        assert result["peeksVisible"] == 5, width  # every call keeps its own peek
        assert result["rowHeight"] < 40, width  # each merged row is a single line
        # Four calls now cost less than four separate carded rows would.
        assert result["mergedHeight"] < 4 * (result["rowHeight"] + 8), width
        assert result["afterAppend"] == [
            "lead", "item", "item", "item", "alone", "alone",
        ], width
        assert result["afterSecondAppend"] == [
            "lead", "item", "item", "item", "alone", "lead", "item",
        ], width
        assert result["newRunCount"] == "×2", width
        assert full_command in result["expanded"], width
        assert result["expandedHeight"] > 0, width
        assert result["overflowX"] == 0, width  # long commands never widen the page


@pytest.mark.browser
async def test_in_between_replies_fold_into_the_tool_card_and_deck_notices_are_one_line(
    tmp_path,
):
    """Issue #3200: only the reply that ends a turn reads as a message, across the
    turns the deck woke up only the latest one does, and the deck's own messages are
    one grey line; all of them open on a tap."""
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    groups = (static_dir / "tool_call_groups.js").read_text()
    ts = datetime(2026, 10, 16, 14, 20, tzinfo=UTC)

    def bash(seq):
        return TranscriptEvent(
            seq=seq, role="assistant", tool_name="Bash", tool_summary=f"command: ls {seq}",
            tool_detail=f"Command\nls {seq}", ts=ts,
        )

    def say(seq, text):
        return TranscriptEvent(seq=seq, role="assistant", text=text, ts=ts)

    wake = (
        "AgentDeck wake-on: pull request northwind/abacus#2124 was merged as"
        " 529d1c4df6d8. You asked to be woken for it. " + "Webhooks are a hint. " * 20
    )
    events = [
        TranscriptEvent(seq=1, role="user", text=wake, ts=ts),
        say(2, "Find the code."),
        bash(3),
        say(4, "Body didn't show; let me get it."),
        bash(5),
        bash(6),
        say(7, "Done: the fix is pushed.\n\nSecond paragraph of the real reply."),
        TranscriptEvent(
            seq=8, role="user", ts=ts,
            text="AgentDeck: your last turn on o/r#1 ended without a next step.",
        ),
        bash(9),
        say(10, "Still waiting for the review."),
        TranscriptEvent(seq=11, role="user", text="Is it merged?", ts=ts),
        say(12, "Not yet."),
        TranscriptEvent(seq=13, role="user", text=wake, ts=ts),
        say(14, "Merged now."),
        TranscriptEvent(seq=15, role="user", text=wake, ts=ts),
        say(16, "Checked it live."),
        TranscriptEvent(seq=17, role="user", text=wake, ts=ts),
        say(18, "Still live."),
    ]
    rendered = render_transcript_events(app.state.templates, events, session_key="k")
    working = render_transcript_events(
        app.state.templates,
        [say(19, "Looking at the logs now."), bash(20), bash(21)],
        session_key="k",
    )
    answered = render_transcript_events(
        app.state.templates, [say(22, "Found it.")], session_key="k"
    )
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section class="session-detail">
        <div class="transcript" data-session-key="k">{rendered}</div>
      </section></main></body>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 360, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=groups)
        result = await page.evaluate(
            """async (live) => {
              const frame = () => new Promise(requestAnimationFrame);
              await frame();
              const transcript = document.querySelector('.transcript');
              const rows = Array.from(transcript.children)
                .filter(row => !row.classList.contains('tool-block-summary'));
              const lines = transcript.querySelectorAll('.tool-block-summary');
              const line = lines[0];
              const closed = {
                lines: lines.length,
                text: line.textContent,
                before: line.nextElementSibling === rows[1],
                hidden: rows.filter(row => row.getBoundingClientRect().height === 0).length,
                height: Math.round(line.getBoundingClientRect().height),
              };
              // Steps a deck notice follows, with no reply to read, are a quiet
              // grey line; steps a visible reply follows stay an orange card.
              const seqRow = seq => transcript.querySelector(`[data-event-seq="${seq}"]`);
              const quiet = {
                line: line.classList.contains('tool-quiet'),
                lineBorder: getComputedStyle(line).borderLeftWidth,
                answered: [...transcript.querySelectorAll('.tool-call')]
                  .find(row => row.textContent.includes('ls 9')).classList.contains('tool-quiet'),
                lone: seqRow(14).classList.contains('tool-quiet'),
              };
              line.click();
              await frame();
              const shape = rows.map(row => [
                row.classList.contains('deck-notice') ? 'deck'
                  : row.classList.contains('ev-folded') ? 'note'
                  : row.classList.contains('tool-call') ? 'tool'
                  : row.classList.contains('user') ? 'you' : 'reply',
                ['tool-run-lead', 'tool-run-item', 'tool-run-joined', 'tool-run-end']
                  .filter(name => row.classList.contains(name)).join(' ')
              ].join(':'));
              const height = row => Math.round(row.getBoundingClientRect().height);
              const seams = [1, 2, 3, 4, 5].map(i => Math.round(
                rows[i + 1].getBoundingClientRect().top - rows[i].getBoundingClientRect().bottom
              ));
              const heights = { deck: height(rows[0]), note: height(rows[1]) };
              const count = rows[2].querySelector('.tool-run-count').textContent;
              rows[3].click();
              await frame();
              const unfolded = rows[3].classList.contains('ev-unfolded');
              const unfoldedHeight = height(rows[3]);
              rows[0].querySelector('summary').click();
              await frame();
              // The agent is still working: its latest line reads in full.
              transcript.insertAdjacentHTML('beforeend', live[0]);
              await frame();
              const liveRow = () => transcript.querySelector('[data-event-seq="19"]');
              const working = liveRow().classList.contains('ev-folded');
              transcript.insertAdjacentHTML('beforeend', live[1]);
              await frame();
              const answered = liveRow().classList.contains('ev-folded');
              return { closed, shape, heights, seams, count, unfolded, unfoldedHeight,
                       deckOpen: height(rows[0]), working, answered, quiet };
            }""",
            [working, answered],
        )
        await browser.close()

    # A finished turn's steps sit behind one line until it is tapped.
    assert result["closed"] == {
        "lines": 1, "text": result["closed"]["text"], "before": True,
        "hidden": 6, "height": result["closed"]["height"],
    }
    # The deck-woken turn's folded reply is one of those steps.
    assert result["closed"]["text"].startswith("6 stepsBash ×3 · 3 notes")
    assert result["closed"]["height"] < 32
    assert result["shape"] == [
        "deck:",
        "note:tool-run-lead",
        # A note between two calls of one tool sits inside that tool's run.
        "tool:tool-run-lead tool-run-joined",
        "note:tool-run-item",
        "tool:tool-run-item",
        "tool:tool-run-item tool-run-end",
        # The deck woke the agent again and it replied again, so this one folds.
        "note:",
        "deck:",
        "tool:",
        # The latest reply before the person's own message stays in full.
        "reply:",
        "you:",
        # A reply to the person stays, though the deck woke the agent after it.
        "reply:",
        "deck:",
        # Lone folded replies are their own one-line rows, with no "1 step" line.
        "note:",
        "deck:",
        "note:",
        "deck:",
        "reply:",
    ]
    assert result["closed"]["lines"] == 1
    # Notes and tool calls of one turn read as one card with no gaps inside it.
    assert result["seams"][:4] == [0, 0, 0, 0]
    assert result["seams"][4] == 8  # the real reply is its own message
    assert result["count"] == "×3"
    assert result["heights"]["deck"] < 24  # one line, though the text is long
    assert result["heights"]["note"] < 32
    assert result["unfolded"] and result["unfoldedHeight"] > result["heights"]["note"]
    assert result["deckOpen"] > 60
    assert result["quiet"] == {
        "line": True, "lineBorder": "0px", "answered": False, "lone": True,
    }
    assert result["working"] is False  # shown in full while the turn is running
    assert result["answered"] is True  # folded once the turn's reply arrived


@pytest.mark.browser
async def test_a_reply_to_the_person_stays_in_full_behind_task_notification_turns(tmp_path):
    """Issue #3377: the chat hid a ``<task-notification>`` completely, so the reply to the
    person and the automatic reply after it read as one turn, and the answer folded away
    into a closed steps line. Each notification is now a grey line that ends the turn."""
    from agentdeck.providers.claude_code import transcripts
    from agentdeck.web.render import render_transcript_events

    def notification(summary):
        return {
            "type": "user", "timestamp": "2026-10-21T16:50:47.240Z",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": (
                "<task-notification>\n<task-id>aec8</task-id>\n<status>completed</status>\n"
                f"<summary>{summary}</summary>\n<result>Done.</result>\n</task-notification>"
            )},
        }

    def say(text):
        return {
            "type": "assistant", "timestamp": "2026-10-21T16:49:47.454Z",
            "message": {"role": "assistant", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": text}]},
        }

    lines = [
        {"type": "user", "timestamp": "2026-10-21T16:49:30.458Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Give me a zip"}]}},
        say("The zip is ready: zip-link"),
        notification('Agent "Song: Jesu dulcis" is waiting on its own background work'),
        say("Jesu dulcis is being sung now."),
        notification("Background command finished"),
        say("Jesu dulcis is live."),
        notification('Agent "Song: Happy Together" completed'),
        say("Happy Together is finished."),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    events = transcripts.read_events(path).events
    app = _app_with_state(tmp_path)
    rendered = render_transcript_events(app.state.templates, events, session_key="k")
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    html = f"""
      <style>{(static_dir / "app.css").read_text()}</style>
      <body class="session-page"><main><section class="session-detail">
        <div class="transcript" data-session-key="k">{rendered}</div>
      </section></main></body>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 360, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=(static_dir / "tool_call_groups.js").read_text())
        rows = await page.evaluate(
            """async () => {
              await new Promise(requestAnimationFrame);
              return [...document.querySelector('.transcript').children].map(row => [
                row.classList.contains('deck-notice') ? 'notice'
                  : row.classList.contains('ev-folded') ? 'folded'
                  : row.classList.contains('user') ? 'you' : 'reply',
                row.getBoundingClientRect().height > 0,
                row.textContent.includes('zip-link'),
              ]);
            }"""
        )
        await browser.close()

    assert [kind for kind, _shown, _zip in rows] == [
        "you", "reply", "notice", "folded", "notice", "folded", "notice", "reply",
    ]
    # The answer to the person is a full, shown reply, though three automatic turns
    # ended after it.
    assert rows[1] == ["reply", True, True]
    assert all(shown for _kind, shown, _zip in rows)


def test_transcript_user_message_renders_attached_images(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    html = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=1,
                role="user",
                text="See this",
                image_media_types=("image/png", "image/jpeg"),
            )
        ],
        session_key="claude_code:test:sid1",
    )

    assert 'aria-label="2 attached images"' in html
    assert html.count('class="ev-image"') == 2
    assert 'src="/sessions/claude_code:test:sid1/transcript-images/1/0"' in html
    assert 'alt="Attached image 2"' in html

    image_only = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=2,
                role="user",
                image_media_types=("image/png",),
            )
        ],
        session_key="claude_code:test:sid1",
    )
    assert 'class="ev user"' in image_only
    assert 'class="ev-image"' in image_only


def test_transcript_user_message_renders_observed_pending_identity(tmp_path):
    from agentdeck.inject import QueuedMessage
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    event = TranscriptEvent(seq=7, role="user", text="same text")
    item = QueuedMessage(41, "same text", client_action_id="action-41")

    html = render_transcript_events(
        app.state.templates,
        [event],
        session_key="claude_code:test:sid1",
        observed_messages={7: item},
    )

    assert 'data-observed-queue-id="41"' in html
    assert 'data-observed-action-id="action-41"' in html


async def test_transcript_image_is_served_outside_the_html_response(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    png = b"\x89PNG\r\n\x1a\nsmall"
    lines[0]["message"]["content"] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode(),
            },
        },
        {"type": "text", "text": "first question"},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        page = await client.get("/sessions/claude_code:test:sid1")
        image = await client.get(
            "/sessions/claude_code:test:sid1/transcript-images/1/0"
        )
        missing = await client.get(
            "/sessions/claude_code:test:sid1/transcript-images/1/1"
        )

    assert "data:image" not in page.text
    assert "/sessions/claude_code:test:sid1/transcript-images/1/0" in page.text
    assert image.status_code == 200
    assert image.content == png
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-store"
    assert missing.status_code == 404


def test_ask_user_question_renders_in_transcript(tmp_path):
    """An AskUserQuestion line is a tool_use with no text, so the generic
    tool-drop rule would hide it — but it carries the prompt in `question` and
    must stay visible so the choice the agent is waiting on shows in the view."""
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    events = [
        TranscriptEvent(
            seq=1,
            role="assistant",
            tool_name="AskUserQuestion",
            question="Which database should we use?",
            text=None,
        ),
    ]
    html = render_transcript_events(templates, events)
    assert "Which database should we use?" in html
    assert "ev-question" in html


def test_activity_label_fallback():
    from agentdeck.models import (
        TokenTotals,
        TranscriptEvent,
        detailed_activity_label,
        transcript_event_is_progress,
    )
    from agentdeck.web.render import activity_label

    tool_call = TranscriptEvent(seq=1, role="assistant", tool_name="Bash", text=None)
    tool_result = TranscriptEvent(seq=1, role="tool", text="output")
    user_msg = TranscriptEvent(seq=1, role="user", text="do it")
    reply = TranscriptEvent(seq=1, role="assistant", text="done")
    continuing_reply = TranscriptEvent(
        seq=2, role="assistant", text="checking", turn_continues=True
    )
    terminal_reply = TranscriptEvent(
        seq=3, role="assistant", text="done", turn_continues=False
    )

    # a long tool run: quiet (not streaming) but last line is a tool → still busy
    assert activity_label(True, False, tool_call) == "Using tools"
    assert activity_label(True, False, tool_result) == "Using tools"
    # unanswered prompt while quiet → working (slow first token doesn't read idle)
    assert activity_label(True, False, user_msg) == "Working"
    # finished reply, quiet → idle (no marker)
    assert activity_label(True, False, reply) is None
    # finished reply but actively writing → working
    assert activity_label(True, True, reply) == "Working"
    # Provider lifecycle beats write recency in both directions.
    assert activity_label(True, False, continuing_reply, age_s=60) == "Working"
    assert activity_label(True, True, terminal_reply) is None
    # dead process → never a marker
    assert activity_label(False, True, tool_call) is None
    # open turn but no write for ages → stalled worker, not "Using tools" forever
    assert activity_label(True, False, tool_call, age_s=10_000) is None
    assert activity_label(True, False, user_msg, age_s=10_000) is None
    assert activity_label(True, False, continuing_reply, age_s=599) == "Working"
    assert activity_label(True, False, continuing_reply, age_s=600) is None
    # an unanswered AskUserQuestion is waiting on the user, not busy — even though
    # it is a tool_use and even right after it was written (streaming).
    ask = TranscriptEvent(seq=1, role="assistant", tool_name="AskUserQuestion", question="Pick?")
    assert activity_label(True, False, ask) is None
    assert activity_label(True, True, ask) is None

    command = TranscriptEvent(
        seq=2,
        role="assistant",
        tool_name="exec_command",
        tool_summary="cmd: uv run pytest tests/test_web.py",
    )
    path = TranscriptEvent(
        seq=3,
        role="assistant",
        tool_name="view_image",
        tool_summary="path: /tmp/screenshot.png",
    )
    assert detailed_activity_label("Using tools", command) == (
        "Running: uv run pytest tests/test_web.py"
    )
    assert detailed_activity_label("Using tools", path) == "Accessing: /tmp/screenshot.png"
    # A described call peeks as prose (issue #581), so the marker names the tool
    # instead of an action parsed off a `key: value` peek.
    described = TranscriptEvent(
        seq=3,
        role="assistant",
        tool_name="Bash",
        tool_summary="Read CI status once",
    )
    assert detailed_activity_label("Using tools", described) == "Bash: Read CI status once"
    # …and the provider's raw shell id never reaches that sentence either.
    described_shell = TranscriptEvent(
        seq=4,
        role="assistant",
        tool_name="exec_command",
        tool_summary="Locate the peek builder",
    )
    assert detailed_activity_label("Using tools", described_shell) == (
        "Shell: Locate the peek builder"
    )
    reasoning = TranscriptEvent(seq=4, role="system", tool_name="reasoning")
    waiting = TranscriptEvent(seq=5, role="assistant", tool_name="wait")
    assert detailed_activity_label("Using tools", reasoning) == "Thinking"
    assert detailed_activity_label("Using tools", waiting) == "Waiting for command output"
    assert detailed_activity_label("Working", command) == "Working"
    usage_only = TranscriptEvent(
        seq=6, role="system", tokens=TokenTotals(input_tokens=1)
    )
    assert transcript_event_is_progress(usage_only) is False
    assert transcript_event_is_progress(reasoning) is True


def test_activity_label_says_what_it_is_working_on():
    """#1457: no tool call in last position still names the work."""
    from agentdeck.models import TranscriptEvent, detailed_activity_label

    prompt = TranscriptEvent(seq=1, role="user", text="Fix the flaky poller test")
    assert detailed_activity_label("Working", prompt) == "Working: Fix the flaky poller test"
    # Markdown decoration is noise at badge size; the words survive it.
    narration = TranscriptEvent(
        seq=2,
        role="assistant",
        text="\n- **Running** the `pytest` suite now\nthen I will push",
        turn_continues=True,
    )
    assert detailed_activity_label("Working", narration) == (
        "Working: Running the pytest suite now"
    )
    long_line = TranscriptEvent(
        seq=3,
        role="assistant",
        text="Rewriting the poller dispatch so a wedged worker is observed and released",
    )
    assert detailed_activity_label("Working", long_line) == (
        "Working: Rewriting the poller dispatch so a wedged worker is…"
    )
    # A prompt typed while the agent was busy is not what it is working on.
    queued = TranscriptEvent(seq=4, role="user", text="and also rebase", queued=True)
    assert detailed_activity_label("Working", queued) == "Working"
    assert detailed_activity_label("Working", TranscriptEvent(seq=5, role="user")) == "Working"
    assert detailed_activity_label("Working", None) == "Working"


def test_activity_label_keeps_the_tool_behind_a_result():
    """#1457: a tool result carries no tool name — the call behind it does."""
    from agentdeck.models import TranscriptEvent, detailed_activity_label

    call = TranscriptEvent(
        seq=1,
        role="assistant",
        tool_name="Bash",
        tool_summary="cmd: uv run pytest -q",
    )
    result = TranscriptEvent(seq=2, role="tool", text="1 failed")
    assert detailed_activity_label("Using tools", result) == "Using tools"
    assert detailed_activity_label("Using tools", result, [call, result]) == (
        "Running: uv run pytest -q"
    )
    # A prompt starts a new turn, so tools behind it are not what is running now.
    prompt = TranscriptEvent(seq=2, role="user", text="try again")
    assert detailed_activity_label("Using tools", result, [call, prompt, result]) == (
        "Using tools"
    )


def test_resolve_activity_label_pipeline():
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import resolve_activity_label

    tool_call = TranscriptEvent(seq=1, role="assistant", tool_name="Bash", text=None)
    reply = TranscriptEvent(seq=1, role="assistant", text="done")

    def resolve(**kw):
        base = dict(
            has_question=False,
            live=True,
            streaming=False,
            last_event=tool_call,
            age_s=0.0,
            has_working_subagent=False,
        )
        return resolve_activity_label(**{**base, **kw})

    # A surfaced question suppresses the badge even mid-tool — this is the guard
    # the page-load path used to omit (it covers trailing natural-language
    # questions, not only AskUserQuestion tool events).
    assert resolve(has_question=True) is None
    # Normal open-turn label flows through activity_label ("Using tools") and is
    # then refined by detailed_activity_label to name the tool.
    assert resolve() == "Using Bash"
    command = TranscriptEvent(seq=2, role="assistant", tool_name="reasoning")
    assert resolve(last_event=command) == "Thinking"
    # The tail reaches the call behind a result, so the badge does not fall back
    # to the generic label the moment a tool returns (#1457).
    result = TranscriptEvent(seq=2, role="tool", text="ok")
    assert resolve(last_event=result) == "Using tools"
    assert resolve(last_event=result, recent_events=[tool_call, result]) == "Using Bash"
    # Idle + no subagent → nothing; idle + a working subagent → "Working".
    assert resolve(last_event=reply, streaming=False) is None
    assert resolve(last_event=reply, streaming=False, has_working_subagent=True) == "Working"
    # A not-live session yields no own-turn label, but an active nested subagent
    # still surfaces "Working" through the fallback (unchanged from before).
    assert resolve(live=False, has_working_subagent=True) == "Working"
    assert resolve(live=False, has_working_subagent=False) is None


async def test_session_detail_hides_activity_badge_when_question_pending(tmp_path):
    # Regression: the initial page render must apply the same question guard the
    # SSE tail does, so a LIVE session surfacing a question shows no activity
    # badge on load (previously it flashed "Working" until the SSE tick blanked
    # it ~1.5s later).
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state

    def _seed(question):
        state.update_session(
            Session(
                key="claude_code:test:sid1",
                account_key="claude_code:test",
                session_id="sid1",
                status=SessionStatus.LIVE,
                thinking=True,  # streaming → activity_label would say "Working"
                question=question,
                title="Hello World Session",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

    async with _client(app) as client:
        _seed("Which option do you want?")
        with_question = await client.get("/sessions/claude_code:test:sid1")
        _seed(None)
        without_question = await client.get("/sessions/claude_code:test:sid1")

    # Same streaming session: the badge is suppressed only because a question is
    # surfaced — proving the guard, not merely an always-empty marker.
    assert "tool-wait-label" not in with_question.text
    assert "tool-wait-label" in without_question.text
    assert "Working" in without_question.text


def test_working_sessions_sort_first(tmp_path):
    from datetime import UTC, datetime, timedelta

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)
    # a quiet LIVE session, more recently active than the busy one
    state.update_session(
        Session(
            key="claude_code:test:quiet",
            account_key="claude_code:test",
            session_id="quiet",
            status=SessionStatus.LIVE,
            thinking=False,
            last_activity=now,
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:busy",
            account_key="claude_code:test",
            session_id="busy",
            status=SessionStatus.LIVE,
            thinking=True,
            activity="Using tools",
            last_activity=now - timedelta(minutes=3),
        )
    )
    keys = [s.session_id for s in state.visible_sessions()]
    assert keys.index("busy") < keys.index("quiet")  # busy floats above, despite older mtime


def test_a_busy_child_does_not_lift_its_parent_in_the_list(tmp_path):
    """#2864: each chat sorts by its own activity. The parent still reads Working on its card,
    but an idle parent with a working delegated child sorts as the idle chat it is."""
    from datetime import UTC, datetime, timedelta

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)

    def chat(name, *, thinking=False, minutes=0):
        state.update_session(
            Session(
                key=f"claude_code:test:{name}",
                account_key="claude_code:test",
                session_id=name,
                status=SessionStatus.LIVE,
                thinking=thinking,
                last_activity=now - timedelta(minutes=minutes),
            )
        )

    chat("recent", minutes=1)
    chat("parent", minutes=30)
    chat("child", thinking=True, minutes=0)
    state.delegation_parents["claude_code:test:child"] = "claude_code:test:parent"

    presentation = state.session_presentation()
    order = [session.session_id for session in presentation.top_level]
    parent = next(s for s in presentation.top_level if s.session_id == "parent")

    assert order.index("recent") < order.index("parent")
    assert parent.thinking is True, "the card still shows the child's work"


def test_active_sessions_keep_stable_relative_order(tmp_path):
    from datetime import UTC, datetime, timedelta

    from agentdeck.state import AppState

    state = AppState()
    now = datetime.now(UTC)
    first = Session(
        key="codex:test:first",
        account_key="codex:test",
        session_id="first",
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=now,
    )
    second = Session(
        key="codex:test:second",
        account_key="codex:test",
        session_id="second",
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=now - timedelta(seconds=1),
    )
    state.update_session(first)
    state.update_session(second)
    assert [session.session_id for session in state.visible_sessions()] == [
        "first",
        "second",
    ]

    # A new event makes the second chat most recent, but active cards do not
    # fight for the top spot on every transcript write.
    state.update_session(
        Session(**{**second.__dict__, "last_activity": now + timedelta(seconds=1)})
    )
    assert [session.session_id for session in state.visible_sessions()] == [
        "first",
        "second",
    ]


def test_session_presentation_nests_subagents_under_parent():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="a:p", account_key="a", session_id="p", status=SessionStatus.LIVE
    )
    child = Session(
        key="a:c",
        account_key="a",
        session_id="c",
        status=SessionStatus.LIVE,
        thinking=True,  # a live subagent nests under its parent
        parent_session_key="a:p",
    )
    dead = Session(
        key="a:d",
        account_key="a",
        session_id="d",
        status=SessionStatus.IDLE,
        thinking=False,  # a finished subagent is dead weight — dropped, not nested
        parent_session_key="a:p",
        show_when_idle=True,
    )
    orphan = Session(
        key="a:o",
        account_key="a",
        session_id="o",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key="a:gone",  # parent not visible
    )
    for s in (parent, child, dead, orphan):
        state.update_session(s)

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    top_keys = {s.key for s in top}
    assert "a:p" in top_keys  # parent stays top-level
    assert "a:c" not in top_keys  # child is nested, not top-level
    assert "a:d" not in top_keys  # dead subagent is dropped, not floated top-level
    assert "a:o" not in top_keys  # orphan (parent gone) is dropped, not floated top-level
    assert [c.key for c in children["a:p"]] == ["a:c"]  # only the live subagent nests
    assert "a:o" not in children  # orphan has no visible parent to nest under
    effective_parent = next(session for session in top if session.key == parent.key)
    assert effective_parent.thinking is True
    assert effective_parent.activity == "Working"
    assert state.sessions[parent.key].thinking is False  # provider state stays raw
    assert presentation.working_count == 1
    assert presentation.display(parent) is effective_parent
    with pytest.raises(TypeError):
        children["another"] = ()

    state.update_session(replace(child, status=SessionStatus.IDLE, thinking=False))
    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    effective_parent = next(session for session in top if session.key == parent.key)
    assert effective_parent.thinking is False
    assert parent.key not in children






def test_session_presentation_keeps_cross_account_subagents_visible():
    """A subagent of a delegated worker must not vanish from the session view.

    The display tree is one level deep, but the real graph is not: a Claude
    coordinator delegates cross-account to a worker, and that worker spawns its
    own subagent. Anchoring to the nearest top-level ancestor keeps the deepest
    session on screen instead of dropping it for being two levels down.
    """
    from agentdeck.state import AppState

    state = AppState()
    coordinator = Session(
        key="claude_code:alt:top",
        account_key="claude_code:alt",
        session_id="top",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    worker = Session(  # cross-account delegated child of the coordinator
        key="claude_code:main:worker",
        account_key="claude_code:main",
        session_id="worker",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    grandchild = Session(  # the delegated worker's own native subagent
        key="claude_code:main:grandchild",
        account_key="claude_code:main",
        session_id="grandchild",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key="claude_code:main:worker",
    )
    for session in (coordinator, worker, grandchild):
        state.update_session(session)
    state.mark_delegated_session(worker.key, "top")

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of

    assert [s.key for s in top] == [coordinator.key]
    # Both descendants flatten onto the one visible top-level row.
    assert [s.key for s in children[coordinator.key]] == [worker.key, grandchild.key]
    rendered = {s.key for s in top} | {
        s.key for group in children.values() for s in group
    }
    assert rendered == {s.key for s in presentation.visible}  # nothing dropped


def test_session_presentation_still_drops_a_chain_that_leaves_the_visible_set():
    """Anchoring must not float a genuinely orphaned subagent to top level."""
    from agentdeck.state import AppState

    state = AppState()
    absent_parent_child = Session(
        key="a:mid",
        account_key="a",
        session_id="mid",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key="a:gone",  # never visible
    )
    grandchild = Session(
        key="a:leaf",
        account_key="a",
        session_id="leaf",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key="a:mid",
    )
    for session in (absent_parent_child, grandchild):
        state.update_session(session)

    presentation = state.session_presentation()

    assert presentation.top_level == ()
    assert presentation.children_of == {}


def test_descendant_progress_sustains_parent_without_hiding_own_stall():
    from agentdeck.state import AppState

    now = datetime.now(UTC)
    state = AppState()
    parent = Session(
        key="a:p",
        account_key="a",
        session_id="p",
        status=SessionStatus.LIVE,
        stalled=True,
        last_progress=now - timedelta(minutes=12),
    )
    child = Session(
        key="a:c",
        account_key="a",
        session_id="c",
        status=SessionStatus.LIVE,
        thinking=True,
        last_progress=now,
        parent_session_key=parent.key,
    )
    state.update_session(parent)
    state.update_session(child)

    effective = state.session_presentation().display(parent)

    assert effective.thinking is True
    assert effective.stalled is True
    assert effective.last_progress == now
    assert state.sessions[parent.key].stalled is True  # display projection only


def test_session_presentation_includes_embedded_subagents_in_parent_activity():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="codex:p",
        account_key="codex",
        session_id="p",
        status=SessionStatus.IDLE,
        thinking=False,
        subagent_count=2,
        show_when_idle=True,
    )
    state.update_session(parent)

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of

    assert children == {}
    assert top[0].thinking is True
    assert top[0].activity == "Working"
    assert state.sessions[parent.key].thinking is False
    assert presentation.working_count == 1
    assert presentation.has_working_subagent(parent) is True


def test_a_finished_subagent_leaves_no_working_marker_on_the_page(tmp_path):
    """#1708: the four markers a stale subagent kept alive, cleared together.

    A finished chat was seen wearing a THINKING header, a ticking activity
    badge, a "1 working" subagent section and the dead child row inside it —
    all four rolled up from one subagent whose record still said working. They
    share a cause, so they are asserted together, and against the same
    projection the first paint and an SSE reconnect both render from.
    """
    from agentdeck.state import AppState
    from agentdeck.web.render import (
        SESSION_FRAGMENTS_BY_NAME,
        SessionView,
        resolve_activity_label,
    )

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    state = AppState()
    parent = Session(
        key="claude_code:test:parent",
        account_key="claude_code:test",
        session_id="parent",
        status=SessionStatus.LIVE,
        thinking=False,  # the parent's own turn ended terminally
        show_when_idle=True,
    )

    def _page(child_thinking: bool):
        state.update_session(parent)
        state.update_session(
            Session(
                key="claude_code:test:agent-atask1",
                account_key="claude_code:test",
                session_id="agent-atask1",
                status=SessionStatus.LIVE if child_thinking else SessionStatus.IDLE,
                thinking=child_thinking,
                activity="Working" if child_thinking else None,
                title="Covering the architecture",
                kind="subagent",
                parent_session_key=parent.key,
                show_when_idle=True,
            )
        )
        presentation = state.session_presentation()
        label = resolve_activity_label(
            has_question=False,
            live=True,
            streaming=False,
            last_event=None,
            age_s=1.0,
            has_working_subagent=presentation.has_working_subagent(parent),
            lifecycle_active=False,  # terminal assistant event
        )
        view = SessionView(
            current=parent,
            effective=presentation.display(parent),
            busy=label is not None,
            label=label,
            age=1.0,
            active_child_sessions=presentation.active_child_sessions(parent),
        )
        return label, {
            name: SESSION_FRAGMENTS_BY_NAME[name].render(templates, view)
            for name in ("status", "subagents", "tools")
        }

    # While the subagent really is working, every marker is expected.
    label, working = _page(True)
    assert label == "Working"
    assert "1 working" in working["subagents"]
    assert "Covering the architecture" in working["subagents"]

    # Its result lands, so nothing on the page may still say the chat is busy.
    label, idle = _page(False)
    assert label is None
    assert "working" not in idle["subagents"]
    assert "Covering the architecture" not in idle["subagents"]
    assert "Working" not in idle["tools"]
    assert "thinking" not in idle["status"].lower()


def test_active_child_sessions_excludes_duplicate_embedded_subagent():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="codex:test:parent",
        account_key="codex:test",
        session_id="parent",
        status=SessionStatus.LIVE,
        subagent_count=1,
        subagents=(
            SubagentProgress(agent_id="native", status="working"),
        ),
    )
    native_child = Session(
        key="codex:test:native",
        account_key="codex:test",
        session_id="native",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key=parent.key,
    )
    delegated_child = Session(
        key="codex:test:delegated",
        account_key="codex:test",
        session_id="delegated",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    for session in (parent, native_child, delegated_child):
        state.update_session(session)
    state.mark_delegated_session(delegated_child.key, parent_session_id=parent.session_id)

    presentation = state.session_presentation()

    assert presentation.active_child_sessions(parent) == (
        presentation.display(delegated_child),
    )


def test_session_tree_nests_cross_provider_delegation():
    from agentdeck.state import AppState

    state = AppState()
    # A Claude chat that delegated a Codex session: child and parent come from
    # different provider scans, so the link lives in state.delegation_parents.
    parent = Session(
        key="claude_code:main:p",
        account_key="claude_code:main",
        session_id="p",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,  # no parent_session_key of its own
        thinking=True,
    )
    for s in (parent, child):
        state.update_session(s)
    with state.bus.subscribe("sessions") as events:
        state.set_delegation_parents(
            "claude_code:main", {"codex:codex:c": "claude_code:main:p"}
        )
        # The child that moved and the parent it moved under (#2865).
        assert events.get_nowait() == (
            "sessions", frozenset({"codex:codex:c", "claude_code:main:p"})
        )
        state.set_delegation_parents(
            "claude_code:main", {"codex:codex:c": "claude_code:main:p"}
        )
        assert events.get_nowait() is None

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}  # nested cross-provider
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]
    effective_parent = next(s for s in top if s.key == "claude_code:main:p")
    assert effective_parent.thinking is True


def test_mark_delegated_session_records_parent_and_nests():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="claude_code:main:p",
        account_key="claude_code:main",
        session_id="p",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,
    )
    for s in (parent, child):
        state.update_session(s)
    # The delegation bridge records the invoking session by its raw id; state
    # resolves it to the parent's full key at render, without any transcript
    # marker.
    state.mark_delegated_session("codex:codex:c", parent_session_id="p")

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]
    assert state.sessions["codex:codex:c"].is_delegated is True


def test_recorded_delegation_parent_self_heals_when_parent_appears():
    from agentdeck.state import AppState

    state = AppState()
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,
    )
    state.update_session(child)
    # Parent not yet scanned in when the delegation starts: record the raw id
    # anyway; the child stays top-level for now rather than nesting under a
    # phantom.
    state.mark_delegated_session("codex:codex:c", parent_session_id="p")
    top = state.session_presentation().top_level
    assert "codex:codex:c" in {s.key for s in top}

    # Once the parent is scanned in, lazy resolution nests the child with no
    # second mark_delegated_session call — the one-shot race self-heals.
    state.update_session(
        Session(
            key="claude_code:main:p",
            account_key="claude_code:main",
            session_id="p",
            status=SessionStatus.LIVE,
        )
    )
    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]


def test_list_subagents_extracts_parent_uuid_from_path(tmp_path):
    from agentdeck.providers.claude_code.provider import _list_subagents

    sub = (
        tmp_path
        / "projects"
        / "-var-home-eero-trailhead"
        / "parent-uuid-1234"
        / "subagents"
        / "agent-abc123.jsonl"
    )
    sub.parent.mkdir(parents=True)
    sub.write_text('{"isSidechain": true, "type": "user"}\n')

    out = _list_subagents(tmp_path)
    assert out == {"agent-abc123": (sub, "parent-uuid-1234")}


def test_subagent_task_reads_sidechain_first_prompt(tmp_path):
    from agentdeck.providers.claude_code.provider import _subagent_task

    path = tmp_path / "agent-x.jsonl"
    # isSidechain user lines are dropped by transcript_meta; _subagent_task must
    # still surface the first user prompt (the Task description) for the title.
    path.write_text(
        '{"type": "summary", "summary": "ignore"}\n'
        '{"type": "user", "isSidechain": true, "message": {"content": '
        '[{"type": "text", "text": "Audit the parser for bugs"}]}}\n'
        '{"type": "user", "message": {"content": "later prompt"}}\n'
    )
    assert _subagent_task(path) == "Audit the parser for bugs"


def test_visible_idle_sessions_are_not_penalized_in_sort(tmp_path):
    from datetime import UTC, datetime, timedelta

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)
    state.update_session(
        Session(
            key="codex:test:live",
            account_key="codex:test",
            session_id="live",
            status=SessionStatus.LIVE,
            last_activity=now - timedelta(minutes=3),
            show_when_idle=True,
        )
    )
    state.update_session(
        Session(
            key="codex:test:idle",
            account_key="codex:test",
            session_id="idle",
            status=SessionStatus.IDLE,
            last_activity=now,
            show_when_idle=True,
        )
    )

    keys = [s.session_id for s in state.visible_sessions()]
    assert keys.index("idle") < keys.index("live")


async def test_working_card_uses_pulsing_dot_without_text_badge(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:think1",
            account_key="claude_code:test",
            session_id="think1",
            status=SessionStatus.LIVE,
            thinking=True,
            title="Busy session",
        )
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    assert "thinking-badge" not in r.text
    assert "dot live thinking" in r.text
    assert 'data-session-key="claude_code:test:think1"' in r.text
    assert 'data-working="1"' in r.text


@pytest.mark.browser
async def test_opening_session_card_acknowledges_and_times_navigation(tmp_path):
    app = _app_with_state(tmp_path)
    chat_path = app.state.app_state.sessions["claude_code:test:sid1"].detail_path
    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get(
            chat_path,
            headers={"X-AgentDeck-Session-Navigation": "detail"},
        )

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    action_script = (static_dir / "action_timing.js").read_text()
    navigation_script = (static_dir / "session_navigation.js").read_text()
    htmx_script = (static_dir / "htmx.min.js").read_text()
    sse_script = (static_dir / "sse.js").read_text()
    stylesheet = (static_dir / "app.css").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            release = asyncio.Event()
            requested = asyncio.Event()
            navigation_headers = {}
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()
            await page.add_init_script(
                """
                window.__eventSources = [];
                class FakeEventSource extends EventTarget {
                  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
                  constructor(url) {
                    super(); this.url = url; this.readyState = FakeEventSource.OPEN;
                    window.__eventSources.push(this);
                    queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
                  }
                  close() { this.readyState = FakeEventSource.CLOSED; }
                }
                window.EventSource = FakeEventSource;
                """
            )

            async def serve(
                route,
                request,
                requested=requested,
                release=release,
                navigation_headers=navigation_headers,
            ):
                path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
                if path == "/":
                    await route.fulfill(status=200, content_type="text/html", body=dashboard.text)
                elif path == chat_path:
                    navigation_headers.update(request.headers)
                    requested.set()
                    await release.wait()
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        headers={"Server-Timing": "transcript;dur=12.3, total;dur=20.1"},
                        body=detail.text,
                    )
                elif path == "/static/action_timing.js":
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=action_script
                    )
                elif path == "/static/session_navigation.js":
                    await route.fulfill(
                        status=200,
                        content_type="text/javascript",
                        body=navigation_script,
                    )
                elif path == "/static/htmx.min.js":
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=htmx_script
                    )
                elif path == "/static/sse.js":
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=sse_script
                    )
                elif path == "/static/app.css":
                    await route.fulfill(status=200, content_type="text/css", body=stylesheet)
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/")
            await page.locator("#sessions").evaluate(
                "node => { node.dataset.mountIdentity = 'preserved'; node.scrollTop = 37; }"
            )
            card = page.locator('a.session[data-session-key="claude_code:test:sid1"]')
            immediate_result = asyncio.get_running_loop().create_future()

            def report_immediate(value, immediate_result=immediate_result):
                if not immediate_result.done():
                    immediate_result.set_result(value)

            await page.expose_function("reportOpeningState", report_immediate)
            await page.evaluate(
                """() => document.addEventListener('click', event => {
                  var card = event.target.closest('a.session');
                  if (!card) return;
                  window.reportOpeningState({
                    opening: card.classList.contains('opening'),
                    busy: card.getAttribute('aria-busy'),
                    label: getComputedStyle(card, '::after').content,
                    timing: JSON.parse(JSON.stringify(
                      Array.from(AgentDeckActionTiming.records.values())[0]
                    )),
                    overflow: document.documentElement.scrollWidth > innerWidth,
                  });
                }, {once: true})"""
            )
            click = asyncio.create_task(card.click())
            immediate = await asyncio.wait_for(immediate_result, timeout=5)
            await requested.wait()
            await asyncio.sleep(0.15)
            release.set()
            await click
            await page.wait_for_url(f"**{chat_path}")
            await page.wait_for_function(
                "AgentDeckActionTiming.summary().open_session?.settled_ms !== null"
            )
            completed = await page.evaluate(
                """() => ({
                  record: AgentDeckActionTiming.snapshot()[0],
                  summary: AgentDeckActionTiming.summary().open_session,
                  listIdentity: document.querySelector('#sessions').dataset.mountIdentity,
                  navigationEntries: performance.getEntriesByType('navigation').length,
                  streamUrl: window.__eventSources.at(-1).url,
                  streamUrls: window.__eventSources.map(source => source.url),
                  eventSourceCount: window.__eventSources.length,
                  activeStreams: window.__eventSources.filter(
                    source => source.readyState !== EventSource.CLOSED
                  ).length,
                  overflow: document.documentElement.scrollWidth > innerWidth,
                })"""
            )
            await page.go_back()
            restored = await page.locator(
                'a.session[data-session-key="claude_code:test:sid1"]'
            ).evaluate(
                "card => ({opening: card.classList.contains('opening'), "
                "busy: card.getAttribute('aria-busy')})"
            )
            await context.close()

            assert immediate["opening"] is True
            assert immediate["busy"] == "true"
            assert immediate["label"] == '"Opening…"'
            assert immediate["overflow"] is False
            assert immediate["timing"]["action"] == "open_session"
            assert (
                immediate["timing"]["epochMarks"]["acknowledged"]
                - immediate["timing"]["epochMarks"]["interaction"]
                < 16
            )
            assert completed["record"]["sessionKey"] == "claude_code:test:sid1"
            assert completed["record"]["successful"] is True
            assert navigation_headers["x-agentdeck-action"] == "open_session"
            assert navigation_headers["x-agentdeck-action-id"] == completed["record"]["id"]
            assert completed["record"]["serverTiming"] == (
                "transcript;dur=12.3, total;dur=20.1"
            )
            assert completed["listIdentity"] == "preserved"
            assert completed["navigationEntries"] == 1
            assert completed["streamUrl"].startswith(
                "/events/sessions/claude_code:test:sid1"
            )
            assert completed["eventSourceCount"] == 2, completed["streamUrls"]
            assert completed["activeStreams"] == 1
            assert completed["summary"]["samples"] == 1
            assert completed["summary"]["http_ms"]["p50"] >= 100
            assert completed["summary"]["settled_ms"]["p50"] >= 100
            assert completed["overflow"] is False
            assert restored == {"opening": False, "busy": None}
        await browser.close()


async def test_both_pages_load_the_shared_session_navigation_modules(tmp_path):
    """The dashboard mounts the detail pane itself now, so every module the
    chat needs has to be on the list page too — not only on session.html."""
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get("/sessions/claude_code:test:sid1")

    shared = (
        "/static/session_navigation.js",
        "/static/session_panels.js",
        "/static/mobile_session_stack.js",
        "/static/session_bottom_follow.js",
        "/static/activity_elapsed.js",
    )
    for page in (dashboard, detail):
        for asset in shared:
            assert asset in page.text, f"{asset} missing from {page.request.url.path}"
    # The chats page's head is the desktop close control; a chat page has no list (#2603).
    assert 'class="session-sidebar-head"' in dashboard.text
    assert 'class="session-sidebar-head"' not in detail.text


async def test_session_navigation_response_omits_the_redundant_sidebar_and_is_timed(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get(
            "/sessions/claude_code:test:sid1",
            headers={
                "X-AgentDeck-Session-Navigation": "detail",
                "X-AgentDeck-Action-ID": "open-session-test",
            },
        )

    assert response.status_code == 200
    assert 'class="session-detail"' in response.text
    assert 'class="session-sidebar"' not in response.text
    assert "Server-Timing" in response.headers
    assert "total;dur=" in response.headers["Server-Timing"]


async def test_extra_session_pane_is_a_self_contained_detail_document(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get(
            "/sessions/claude_code:test:sid1", params={"pane": "extra"}
        )

    assert response.status_code == 200
    assert 'class="session-pane-page"' in response.text
    assert 'class="session-detail"' in response.text
    assert 'class="session-sidebar"' not in response.text
    assert 'class="session-diff"' not in response.text
    assert "data-session-extra-add" not in response.text
    assert 'sse-connect="/events/sessions/claude_code:test:sid1' in response.text


async def _open_a_long_chat(page, base):
    """A chat with a transcript taller than the pane, so the sticky header rows
    have something to stay in front of."""
    key = "claude_code:test:sid1"
    await page.goto(f"{base}/sessions/{key}")
    await page.locator(".session-detail-head .session-title").wait_for()
    await page.evaluate(
        """() => {
            const spacer = document.createElement('div');
            spacer.style.height = '3000px';
            document.querySelector('.transcript').appendChild(spacer);
        }"""
    )


_MEASURE_CHAT_HEADER = """async () => {
    const pane = document.querySelector('.session-detail');
    pane.scrollTop = pane.scrollHeight;
    await new Promise(resolve => requestAnimationFrame(resolve));
    const paneTop = pane.getBoundingClientRect().top;
    const head = document.querySelector('.session-detail-head');
    const meta = document.querySelector('.session-detail > .detail-meta');
    const title = document.querySelector('.session-detail-head .session-title');
    const add = document.querySelector('[data-session-extra-add]');
    const headRect = head.getBoundingClientRect();
    const metaRect = meta.getBoundingClientRect();
    const titleRect = title.getBoundingClientRect();
    return {
        scrolled: pane.scrollTop > 100,
        headTop: Math.round(headRect.top - paneTop),
        headHeight: Math.round(headRect.height),
        headFont: getComputedStyle(head).fontSize,
        titlePinned: titleRect.width > 0 && Math.round(titleRect.top - paneTop) >= 0
            && titleRect.bottom <= headRect.bottom + 1,
        metaPosition: getComputedStyle(meta).position,
        metaHeight: Math.round(metaRect.height),
        metaUnderHead: Math.round(metaRect.top - headRect.bottom),
        metaStillOnScreen: metaRect.bottom > paneTop,
        addVisible: add ? add.getBoundingClientRect().width > 0 : false,
        addInHead: add ? head.contains(add) : false,
    };
}"""


@pytest.mark.browser
async def test_chat_header_stays_pinned_while_the_transcript_scrolls(tmp_path):
    """The chat's title line stays at the top of the transcript's own scroll
    pane, so a long chat keeps saying which chat it is and the extra-pane
    button stays reachable. Desktop pins the chip row under it as a second
    normal-font line; the phone pins the one small line only (#1571)."""
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1400, "height": 800}, service_workers="block"
        )
        await _open_a_long_chat(page, base)
        desktop = await page.evaluate(_MEASURE_CHAT_HEADER)
        await resized(page, 390, 740)
        mobile = await page.evaluate(_MEASURE_CHAT_HEADER)
        await browser.close()

    assert desktop["scrolled"] is True
    assert desktop["headTop"] == 0
    assert desktop["titlePinned"] is True
    assert desktop["addVisible"] is True
    assert desktop["addInHead"] is True
    # Two lines: the chip row is pinned directly under the title row.
    assert desktop["metaPosition"] == "sticky"
    assert desktop["metaUnderHead"] == 0
    assert desktop["headFont"] == "15px"

    assert mobile["scrolled"] is True
    assert mobile["headTop"] == 0
    assert mobile["titlePinned"] is True
    # One line, in the smaller font, and nothing else pinned with it: the chip
    # row scrolls away with the transcript.
    assert mobile["headHeight"] == 34
    assert mobile["headFont"] == "13px"
    assert mobile["metaPosition"] == "static"
    assert mobile["metaStillOnScreen"] is False
    # The extra-pane feature is desktop-only, so the phone header carries the
    # title alone rather than a button that cannot do anything.
    assert mobile["addVisible"] is False


@pytest.mark.browser
async def test_opening_a_chat_control_leaves_the_pinned_header_alone(tmp_path):
    """✎ Rename opens into the control row below the pinned header, not into the
    header itself, so nothing about the header changes while it is open (#1993).

    The header is two lines of fixed height. A control that expanded inside it
    would either be clipped or make the pinned box taller than that height says
    it is, which is why every expanding control now lives below it.
    """
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1400, "height": 800}, service_workers="block"
        )
        await _open_a_long_chat(page, base)
        shut_state = await page.evaluate(_MEASURE_OPEN_FORM)
        await page.click("#session-title-editor > summary")
        await page.fill("#session-title-input", "A name of my own")
        open_state = await page.evaluate(_MEASURE_OPEN_FORM)
        # Read the form's own place in the page from the top of the chat, where
        # the header is not covering anything: scrolled to the bottom it has
        # left the screen entirely, which says nothing about where it sits.
        at_top = await page.evaluate(_MEASURE_CONTROL_ROW_AT_TOP)
        await browser.close()

    assert open_state["scrolled"] is True
    assert open_state["formVisible"] is True
    # The control row is not part of the pinned header and never was sticky, so
    # the open form is free to be as tall as it needs to be.
    assert open_state["controlsPosition"] == "static"
    assert at_top["formTop"] >= at_top["metaBottom"]
    # Both header lines stay pinned, at exactly the height they had while the
    # editor was shut.
    assert open_state["headPosition"] == "sticky"
    assert open_state["metaPosition"] == "sticky"
    assert open_state["metaBottom"] == shut_state["metaBottom"]


@pytest.mark.browser
async def test_the_mac_approval_frame_does_not_break_the_pinned_header(
    tmp_path, monkeypatch
):
    """The Mac approvals frame is 60vh tall when opened, and it used to open
    inside the pinned two-line header (#1993). A sticky box taller than the
    viewport pins its top at 0 and never scrolls again, so the header stopped
    behaving like a header at all.

    It lives in the control row below the header now, and it draws nothing at
    all until it is opened: the frame gets its full height, the header keeps its
    own, and a shut control costs one chip.
    """
    monkeypatch.setenv(mac_runner.BASE_URL_ENV, "http://mac.invalid")
    monkeypatch.setenv(mac_runner.TOKEN_ENV, "outbound")
    monkeypatch.setenv(mac_runner.STATE_ENV, str(tmp_path / "mac-runner.json"))
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1400, "height": 800}, service_workers="block"
        )
        # The frame points at this same server rather than at the real Mac: the
        # box it reserves is what this measures, and a load that has to time out
        # against a name that does not resolve costs the suite a minute and a
        # half of doing nothing.
        monkeypatch.setenv(mac_runner.BASE_URL_ENV, base)
        await _open_a_long_chat(page, base)
        shut_state = await page.evaluate(_MEASURE_MAC_FRAME)
        await page.click(".session-mac-runner-frame > summary")
        await page.locator(".session-mac-runner-frame iframe").wait_for()
        open_state = await page.evaluate(_MEASURE_MAC_FRAME)
        await browser.close()

    assert shut_state["frameVisible"] is False
    # Shut, the control is a chip in the row: it costs one line, not a frame.
    assert shut_state["controlsHeight"] < 60

    assert open_state["frameVisible"] is True
    assert open_state["frameHeight"] > 300
    # The frame is below the header, in the row that scrolls, and the header is
    # exactly as tall as it was before the frame existed on screen.
    assert open_state["frameTop"] >= open_state["metaBottom"]
    assert open_state["controlsPosition"] == "static"
    assert open_state["headHeight"] == shut_state["headHeight"]
    assert open_state["metaBottom"] == shut_state["metaBottom"]
    # And the header is still short enough to be a sticky header at all: a
    # sticky box taller than its pane can never leave the top of it.
    assert open_state["metaBottom"] < open_state["paneHeight"]


# One animation frame is when the browser gets around to laying something out,
# not when it has finished: a scroll, an opened <details> and a sticky
# recalculation can take several, and reading after exactly one is asserting on
# a moment (#1826). Wait for the box being measured to stop moving instead.
_SETTLE = """
    const settle = async (element) => {
        let previous = null;
        for (let frame = 0; frame < 60; frame += 1) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            const current = JSON.stringify(element.getBoundingClientRect());
            if (current === previous) return;
            previous = current;
        }
    };
"""


_MEASURE_CONTROL_ROW_AT_TOP = (
    """async () => {"""
    + _SETTLE
    + """
    const pane = document.querySelector('.session-detail');
    pane.scrollTop = 0;
    const meta = document.querySelector('.session-detail > .detail-meta');
    const form = document.querySelector('#session-title-input');
    await settle(form);
    const paneTop = pane.getBoundingClientRect().top;
    return {
        metaBottom: Math.round(meta.getBoundingClientRect().bottom - paneTop),
        formTop: Math.round(form.getBoundingClientRect().top - paneTop),
    };
}"""
)


_MEASURE_MAC_FRAME = (
    """async () => {"""
    + _SETTLE
    + """
    const pane = document.querySelector('.session-detail');
    const head = document.querySelector('.session-detail-head');
    const meta = document.querySelector('.session-detail > .detail-meta');
    const controls = document.querySelector('.session-detail > .detail-controls');
    const frame = document.querySelector('.session-mac-runner-frame iframe');
    // The frame is what grows and shrinks here; with the control shut there is
    // no frame box to watch, so the row it sits in stands in for it.
    await settle(frame || controls);
    const paneRect = pane.getBoundingClientRect();
    const frameRect = frame ? frame.getBoundingClientRect() : null;
    return {
        headHeight: Math.round(head.getBoundingClientRect().height),
        metaBottom: Math.round(meta.getBoundingClientRect().bottom - paneRect.top),
        controlsPosition: getComputedStyle(controls).position,
        controlsHeight: Math.round(controls.getBoundingClientRect().height),
        paneHeight: Math.round(paneRect.height),
        frameVisible: !!frameRect && frameRect.height > 0,
        frameHeight: frameRect ? Math.round(frameRect.height) : 0,
        frameTop: frameRect ? Math.round(frameRect.top - paneRect.top) : 0,
    };
}"""
)


_MEASURE_OPEN_FORM = (
    """async () => {"""
    + _SETTLE
    + """
    const pane = document.querySelector('.session-detail');
    const head = document.querySelector('.session-detail-head');
    const meta = document.querySelector('.session-detail > .detail-meta');
    const controls = document.querySelector('.session-detail > .detail-controls');
    const form = document.querySelector('#session-title-input');
    pane.scrollTop = pane.scrollHeight;
    await settle(meta);
    const paneTop = pane.getBoundingClientRect().top;
    const formRect = form ? form.getBoundingClientRect() : null;
    return {
        scrolled: pane.scrollTop > 100,
        headPosition: getComputedStyle(head).position,
        metaPosition: getComputedStyle(meta).position,
        controlsPosition: getComputedStyle(controls).position,
        metaBottom: Math.round(meta.getBoundingClientRect().bottom - paneTop),
        formVisible: !!formRect && formRect.width > 0 && formRect.height > 0,
    };
}"""
)


@pytest.mark.browser
async def test_a_wide_extra_pane_keeps_its_header_flush_with_the_pane_top(tmp_path):
    """An extra-pane iframe can itself be wider than the desktop breakpoint on
    an ultrawide display, and the desktop block has its own padding override for
    that pane. A sticky row parks below its scroll pane's top padding, so any
    padding restored there would put a band above the header for the transcript
    to scroll through (#1571)."""
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1400, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/sessions/claude_code:test:sid1?pane=extra")
        await page.locator(".session-detail-head .session-title").wait_for()
        await page.evaluate(
            """() => {
                const spacer = document.createElement('div');
                spacer.style.height = '3000px';
                document.querySelector('.transcript').appendChild(spacer);
            }"""
        )
        pane = await page.evaluate(
            """async () => {
                const detail = document.querySelector('.session-detail');
                detail.scrollTop = detail.scrollHeight;
                await new Promise(resolve => requestAnimationFrame(resolve));
                const head = document.querySelector('.session-detail-head');
                const style = getComputedStyle(detail);
                return {
                    // htmx adds a transient htmx-request class to the body
                    // while a background request is in flight (#2702).
                    panePage: document.body.classList.contains('session-pane-page'),
                    scrolled: detail.scrollTop > 100,
                    paddingTop: style.paddingTop,
                    paddingLeft: style.paddingLeft,
                    paddingBottom: style.paddingBottom,
                    headTop: Math.round(
                        head.getBoundingClientRect().top
                        - detail.getBoundingClientRect().top
                    ),
                    headPosition: getComputedStyle(head).position,
                };
            }"""
        )
        await browser.close()

    assert pane["panePage"] is True
    assert pane["scrolled"] is True
    assert pane["headPosition"] == "sticky"
    assert pane["headTop"] == 0
    # The gap is only gone from the top: the pane keeps its side and bottom room.
    assert pane["paddingTop"] == "0px"
    assert pane["paddingLeft"] == "12px"
    assert pane["paddingBottom"] == "12px"


@pytest.mark.browser
async def test_desktop_keeps_two_extra_chats_while_the_normal_pane_navigates(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    transcript_dir = tmp_path / "projects" / "-tmp"
    for number, title in ((2, "Second chat"), (3, "Third chat")):
        session_id = f"sid{number}"
        (transcript_dir / f"{session_id}.jsonl").write_text(
            (transcript_dir / "sid1.jsonl").read_text()
        )
        state.update_session(
            Session(
                key=f"claude_code:test:{session_id}",
                account_key="claude_code:test",
                session_id=session_id,
                status=SessionStatus.LIVE,
                title=title,
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1800, "height": 900}, service_workers="block"
        )
        await _open_chat_from_chats_view(page, base, "claude_code:test:sid1")
        add = page.locator("[data-session-extra-add]")
        await expect(add).to_be_visible()

        await add.click()
        await expect(page.locator(".session-extra-pane")).to_have_count(2)
        await expect(page.locator(".session-extra-frame")).to_have_count(1)
        # The first frame: the chat list opened it at its durable `/chats/` address.
        first = page.frame_locator(".session-extra-frame").first
        await expect(
            first.locator('.session-detail[data-session-key="claude_code:test:sid1"]')
        ).to_be_visible()
        assert (
            await first.locator("body").get_attribute("sse-connect")
        ).startswith("/events/sessions/claude_code:test:sid1")
        await page.locator(".session-extra-frame").evaluate(
            "frame => { frame.contentWindow.name = 'preserved-extra-chat'; }"
        )
        await first.locator("body").evaluate(
            """body => {
              const form = document.createElement('form');
              form.dataset.clearOnSend = '';
              const draft = document.createElement('textarea');
              draft.name = 'message'; draft.value = 'unfinished';
              form.appendChild(draft); body.appendChild(form);
            }"""
        )
        assert await page.evaluate("window.AgentDeckWorkInProgress()")
        await first.locator('textarea[name="message"]').evaluate("draft => draft.remove()")

        await page.locator('[data-session-key="claude_code:test:sid2"]').first.click()
        await expect(
            page.locator(
                '.session-layout > .session-detail[data-session-key="claude_code:test:sid2"]'
            )
        ).to_be_visible()
        await expect(
            first.locator('.session-detail[data-session-key="claude_code:test:sid1"]')
        ).to_be_visible()
        assert await page.locator(".session-extra-frame").evaluate(
            "frame => frame.contentWindow.name"
        ) == "preserved-extra-chat"

        await add.click()
        await page.locator('[data-session-key="claude_code:test:sid3"]').first.click()
        await expect(
            page.locator(
                '.session-layout > .session-detail[data-session-key="claude_code:test:sid3"]'
            )
        ).to_be_visible()
        await expect(page.locator(".session-extra-frame")).to_have_count(2)
        second = page.frame_locator('.session-extra-frame[data-session-path^="/chats/"]').last
        await expect(
            second.locator('.session-detail[data-session-key="claude_code:test:sid2"]')
        ).to_be_visible()
        assert (
            await second.locator("body").get_attribute("sse-connect")
        ).startswith("/events/sessions/claude_code:test:sid2")
        assert (
            await page.locator("body").get_attribute("sse-connect")
        ).startswith("/events/sessions/claude_code:test:sid3")
        assert await add.is_disabled()

        stored = await page.evaluate(
            "JSON.parse(localStorage.getItem('agentdeckExtraSessionPanes'))"
        )
        assert [item["title"] for item in stored] == ["Hello World Session", "Second chat"]

        await resized(page, 3840, 900)

        # `resized` waits for the outer page, but each pane is its own document
        # and relays out after it, so these two are read together until they
        # agree rather than sampled once (#1826).
        async def ultrawide_pane():
            display = await first.locator(".session-layout").evaluate(
                "layout => getComputedStyle(layout).display"
            )
            width = await first.locator(".session-detail").evaluate(
                "detail => detail.getBoundingClientRect().width"
            )
            assert (display, width > 1000) == ("block", True), (
                f"display={display!r} width={width}"
            )

        await assert_eventually(
            "the first pane went single-column and full width at 3840px",
            ultrawide_pane,
        )
        await resized(page, 1800, 900)

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            assert await page.locator(".session-extra-frame").count() == 2
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1506-desktop-session-panes.png")
            )

        await page.reload()
        await expect(page.locator(".session-extra-frame")).to_have_count(2)
        await expect(
            page.locator(
                '.session-layout > .session-detail[data-session-key="claude_code:test:sid3"]'
            )
        ).to_be_visible()

        await page.locator("[data-session-extra-close]").first.click()
        await expect(page.locator(".session-extra-frame")).to_have_count(1)
        assert not await add.is_disabled()

        await resized(page, 1100, 844)
        await expect(page.locator(".session-extra-pane")).to_have_count(0)
        await expect(add).to_be_hidden()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")

        await resized(page, 390, 844)
        await expect(page.locator(".session-extra-pane")).to_have_count(0)
        await expect(add).to_be_hidden()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await browser.close()


async def test_opening_a_chat_never_waits_for_git_or_github(tmp_path, monkeypatch):
    """#766: the chat page renders from what is cached, not from `git status`.

    Opening a chat Deckhand has not triaged used to await a full context resolve
    — two `git` runs plus a `gh pr view` per registered PR — with the whole page
    behind it. Measured on a loaded host that span ran from 0 ms (warm cache) to
    7.0 s (`git status` reaching GitContextResolver.COMMAND_TIMEOUT_S). Holding
    the resolver open here would hang the request if anything still awaited it.
    """
    app = _app_with_state(tmp_path, with_transcript=True)
    assistant = app.state.assistant
    session_key = "claude_code:test:sid1"
    resolving = asyncio.Event()
    release = asyncio.Event()
    resolve_calls: list[list[str]] = []

    async def held_resolve(sessions, *, force=False):
        resolve_calls.append([session.key for session in sessions])
        resolving.set()
        await release.wait()
        return {
            session_key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-766",
                dirty=False,
            )
        }

    monkeypatch.setattr(assistant.context_resolver, "resolve", held_resolve)

    async with _client(app) as client:
        headers = {"X-AgentDeck-Session-Navigation": "detail"}
        response = await asyncio.wait_for(
            client.get(f"/sessions/{session_key}", headers=headers), timeout=10.0
        )
        assert response.status_code == 200
        # The resolve really did start, and really is still running.
        await asyncio.wait_for(resolving.wait(), timeout=5.0)
        assert not release.is_set()
        # The context it will produce is simply absent from the first paint;
        # `assistant-session` and `related-links` are both live fragments.
        assert "agent/issue-766" not in response.text

        # A second open while the first resolve is in flight must not queue
        # another `git status` behind it.
        second = await asyncio.wait_for(
            client.get(f"/sessions/{session_key}", headers=headers), timeout=10.0
        )
        assert second.status_code == 200
        assert resolve_calls == [[session_key]]

        release.set()
        for _ in range(400):
            await asyncio.sleep(0)
            if assistant.contexts.get(session_key) is not None:
                break
        resolved = assistant.contexts.get(session_key)
        assert resolved is not None and resolved.branch == "agent/issue-766"

        # Now that it is cached and matches the registration, the open is served
        # from it without starting anything at all.
        landed = await client.get(f"/sessions/{session_key}", headers=headers)
        assert "agent/issue-766" in landed.text
        assert resolve_calls == [[session_key]]


async def test_in_page_chat_open_skips_the_new_chat_directory_scan(
    tmp_path, monkeypatch
):
    """#766: the picker the detail pane does not render costs it no filesystem.

    `_show_new_chat_cwd_option` resolves a path and stats `.git` twice. It used
    to run once per session — a realpath bazaar across the whole deck — to build
    a `<datalist>` that lives in `partials/session_list_controls.html` alone.
    """
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import routes_pages

    # The picker only exists for an account that can start a chat.
    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )

    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    shared_cwd = tmp_path / "checkout"
    shared_cwd.mkdir()
    for index in range(5):
        state.update_session(
            Session(
                key=f"claude_code:test:cwd{index}",
                account_key="claude_code:test",
                session_id=f"cwd{index}",
                status=SessionStatus.IDLE,
                cwd=shared_cwd,
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

    checked: list[str] = []
    real_option = routes_pages._show_new_chat_cwd_option

    def counting_option(cwd):
        checked.append(str(cwd))
        return real_option(cwd)

    monkeypatch.setattr(routes_pages, "_show_new_chat_cwd_option", counting_option)

    async with _client(app) as client:
        detail = await client.get(
            "/sessions/claude_code:test:sid1",
            headers={"X-AgentDeck-Session-Navigation": "detail"},
        )
        assert detail.status_code == 200
        assert checked == []
        # The continue-chat form still renders; only the deck-wide picker is gone.
        assert 'class="session-detail"' in detail.text

        full = await client.get("/sessions/claude_code:test:sid1")
        # A chat page carries no New chat form at all (#2603), so it scans nothing either.
        assert full.status_code == 200
        assert checked == []
        chats = await client.get("/chats")

    assert chats.status_code == 200
    assert checked, "the chats page still offers the New-chat directory picker"
    # One check per distinct directory, not one per session sitting in it.
    assert len(checked) == len(set(checked))


async def test_the_new_chat_directories_are_checked_again_only_when_they_change(
    tmp_path, monkeypatch
):
    """#2841: the checks ran on every front-page open. The answer stands until the set of
    chat directories changes, and a chat in a new directory is offered at once."""
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import routes_pages

    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: True)
    monkeypatch.setattr(routes_pages, "_cwd_options_memo", None)
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    first = tmp_path / "first"
    first.mkdir()

    def add(name: str, cwd: Path) -> None:
        state.update_session(
            Session(
                key=f"claude_code:test:{name}",
                account_key="claude_code:test",
                session_id=name,
                status=SessionStatus.IDLE,
                cwd=cwd,
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

    add("one", first)
    checked: list[str] = []
    real_option = routes_pages._show_new_chat_cwd_option

    def counting_option(cwd):
        checked.append(str(cwd))
        return real_option(cwd)

    monkeypatch.setattr(routes_pages, "_show_new_chat_cwd_option", counting_option)
    keys = {"claude_code:test"}
    found = routes_pages._new_chat_cwd_options(state, keys)
    assert checked, "the first render checks the directories"
    checked.clear()
    assert routes_pages._new_chat_cwd_options(state, keys) == found
    assert checked == []

    second = tmp_path / "second"
    second.mkdir()
    add("two", second)
    again = routes_pages._new_chat_cwd_options(state, keys)
    assert checked, "a new directory is checked"
    assert (str(second) in again) == real_option(second)


async def test_a_slow_front_page_or_chat_page_logs_its_steps(tmp_path, monkeypatch, caplog):
    """#2841: like the Work page's, a slow open says which step it spent its time in."""
    import logging

    from agentdeck.web import routes_pages

    monkeypatch.setattr(routes_pages, "SLOW_PAGE_MS", -1.0)
    app = _app_with_state(tmp_path, with_transcript=True)
    with caplog.at_level(logging.WARNING, logger="agentdeck.web.routes_pages"):
        async with _client(app) as client:
            front = await client.get("/")
            chat = await client.get("/sessions/claude_code:test:sid1")
    assert front.status_code == 200 and chat.status_code == 200
    assert "overview;dur=" in front.headers["server-timing"]
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("slow front page / ") and "controls=" in m for m in messages)
    assert any(
        m.startswith("slow chat page claude_code:test:sid1 ") and "transcript=" in m
        for m in messages
    )


async def test_direct_session_navigation_is_timed_and_compressed(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get(
            "/sessions/claude_code:test:sid1",
            headers={"Accept-Encoding": "gzip"},
        )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert "transcript;dur=" in response.headers["server-timing"]
    # A chat page builds no chat list any more (#2603).
    assert "session_list;dur=" not in response.headers["server-timing"]
    assert "total;dur=" in response.headers["server-timing"]


async def test_static_assets_are_compressed_and_always_revalidated(tmp_path):
    app = _app_with_state(tmp_path)
    asset_version = app.state.templates.env.globals["asset_ver"]
    async with _client(app) as client:
        versioned = await client.get(
            f"/static/app.css?v={asset_version}",
            headers={"Accept-Encoding": "gzip"},
        )
        unversioned = await client.get("/static/app.css")
        stale_version = await client.get("/static/app.css?v=stale")

    assert versioned.headers["content-encoding"] == "gzip"
    assert versioned.headers["cache-control"] == "public, no-cache"
    assert unversioned.headers["cache-control"] == "public, no-cache"
    assert stale_version.headers["cache-control"] == "public, no-cache"


async def test_static_byte_ranges_are_not_gzipped(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get(
            "/static/app.css",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-999"},
        )

    assert response.status_code == 206
    assert response.headers["content-range"].startswith("bytes 0-999/")
    assert "content-encoding" not in response.headers
    assert len(response.content) == 1000


@pytest.mark.browser
async def test_closing_a_session_restores_the_live_list_without_reloading_it(tmp_path):
    """Desktop hides the topbar arrow, so the sidebar head is the close control.
    The sidebar head and shared brand must both close a client-opened chat without
    reloading it: the live list keeps its node, scroll, and stream."""
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get("/sessions/claude_code:test:sid1")
    canonical_path = app.state.app_state.chat_path_for_session(
        "claude_code:test:sid1"
    )

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/session_navigation.js": (static_dir / "session_navigation.js").read_text(),
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
    }
    stylesheet = (static_dir / "app.css").read_text()
    list_requests = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1200, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            window.__eventSources = [];
            class FakeEventSource extends EventTarget {
              static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
              constructor(url) {
                super(); this.url = url; this.readyState = FakeEventSource.OPEN;
                window.__eventSources.push(this);
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route, request):
            nonlocal list_requests
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/":
                list_requests += 1
                await route.fulfill(status=200, content_type="text/html", body=dashboard.text)
            elif path == canonical_path:
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/")
        await page.locator("#sessions").evaluate(
            "node => { node.dataset.mountIdentity = 'preserved'; }"
        )
        assert list_requests == 1

        await page.click('a.session[data-session-key="claude_code:test:sid1"]')
        await page.wait_for_selector(".session-detail .transcript")
        close = page.locator(".session-sidebar-head a[href='/']")
        await expect(close).to_be_visible()

        await close.click()
        await page.wait_for_selector(".session-detail", state="detached")

        # The shared brand is another home link. On a session page it must use
        # the same client-side close path instead of re-requesting the deck.
        await page.click('a.session[data-session-key="claude_code:test:sid1"]')
        await page.wait_for_selector(".session-detail .transcript")
        brand = page.locator("body.session-page a.brand[href='/']")
        await expect(brand).to_be_visible()
        await brand.click()
        await page.wait_for_selector(".session-detail", state="detached")
        closed = await page.evaluate(
            """() => ({
              listIdentity: document.querySelector('#sessions').dataset.mountIdentity,
              navigationEntries: performance.getEntriesByType('navigation').length,
              path: location.pathname,
              listPage: document.body.classList.contains('session-list-page'),
              headHidden: getComputedStyle(
                document.querySelector('.session-sidebar-head')
              ).display === 'none',
              selected: document.querySelectorAll('#sessions a.session.selected').length,
              streamUrl: window.__eventSources.at(-1).url,
              activeStreams: window.__eventSources.filter(
                source => source.readyState !== EventSource.CLOSED
              ).length,
            })"""
        )
        await context.close()
        await browser.close()

    assert list_requests == 1, "closing a session must not re-request the dashboard"
    assert closed["listIdentity"] == "preserved"
    assert closed["navigationEntries"] == 1
    assert closed["path"] == "/"
    assert closed["listPage"] is True
    assert closed["headHidden"] is True
    assert closed["selected"] == 0
    # The page's own stream comes back, the topbar's alone (#2603).
    assert closed["streamUrl"] == "/events"
    assert closed["activeStreams"] == 1


_CODE_BLOCK_SOURCE = 'def greet(name):\n    return f"hi {name}"'
_CODE_BLOCK_MESSAGE = (
    "Try this:\n\n```python\n" + _CODE_BLOCK_SOURCE + "\n```\n\nThen `run` it."
)


def _seed_code_block_transcript(tmp_path):
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    lines[0]["message"]["content"] = _CODE_BLOCK_MESSAGE
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))


@pytest.mark.browser
async def test_transcript_code_blocks_copy_to_the_clipboard(tmp_path):
    """A fenced code block carries its own copy button (#1475).

    The button must hand over the code and nothing else — a control labelled with
    the word "Copy" would sit inside the region a reader drags across by hand, so
    their clipboard would carry the label too. It also must not make the message
    taller, and it must stay reachable when the block scrolls sideways.
    """
    app = _app_with_state(tmp_path, with_transcript=True)
    _seed_code_block_transcript(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = {}
        for width in (1200, 320):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            page = await context.new_page()
            await page.goto(f"{base}/sessions/claude_code:test:sid1")
            block = page.locator(".ev.user .ev-text .md-code")
            await block.wait_for()
            button = block.locator(".code-copy")
            # Height is measured before and after so the control cannot be said to
            # have cost the reader vertical space on a phone.
            without_button = await block.evaluate(
                """element => {
                  const button = element.querySelector('.code-copy');
                  button.style.display = 'none';
                  const height = element.getBoundingClientRect().height;
                  button.style.display = '';
                  return height;
                }"""
            )
            await button.click()
            await expect(button).to_have_class(re.compile(r"\bcopied\b"))
            results[width] = {
                "blocks": await page.locator(".ev.user .ev-text .md-code").count(),
                "buttons": await button.count(),
                "code": await block.locator("pre code").text_content(),
                "clipboard": await page.evaluate("navigator.clipboard.readText()"),
                "message_text": await page.locator(".ev.user .ev-text").text_content(),
                "position": await button.evaluate(
                    "element => getComputedStyle(element).position"
                ),
                "height_with_button": await block.evaluate(
                    "element => element.getBoundingClientRect().height"
                ),
                "height_without_button": without_button,
                # The <pre> is the sideways scroller, so the button has to hang
                # outside it or it would slide off with a long line.
                "button_outside_scroller": await button.evaluate(
                    "element => !element.closest('pre')"
                ),
                "button_inside_viewport": await button.evaluate(
                    "element => {"
                    "  const box = element.getBoundingClientRect();"
                    "  return box.right <= innerWidth && box.left >= 0 && box.width > 0;"
                    "}"
                ),
                "overlaps_code": await block.evaluate(
                    "element => {"
                    "  const button = element.querySelector('.code-copy')"
                    "    .getBoundingClientRect();"
                    "  const pre = element.querySelector('pre').getBoundingClientRect();"
                    "  return button.right > pre.right;"
                    "}"
                ),
            }
            await context.close()
        await browser.close()

    for width, result in results.items():
        assert result["blocks"] == 1, width
        assert result["buttons"] == 1, width
        assert result["code"] == _CODE_BLOCK_SOURCE, width
        assert result["clipboard"] == _CODE_BLOCK_SOURCE, width
        # The button contributes no text, so a hand selection of the block is clean.
        assert "Copy" not in result["message_text"], width
        assert _CODE_BLOCK_SOURCE in result["message_text"], width
        assert result["position"] == "absolute", width
        assert result["height_with_button"] == result["height_without_button"], width
        assert result["button_outside_scroller"] is True, width
        assert result["button_inside_viewport"] is True, width
        assert result["overlaps_code"] is False, width


@pytest.mark.browser
async def test_code_block_copy_falls_back_without_the_clipboard_api(tmp_path):
    """The deck is reachable over plain http on a LAN address, where the async
    clipboard API is absent. The button still has to copy there, so it falls back
    to a selected off-screen textarea and reports failure rather than pretending."""
    app = _app_with_state(tmp_path, with_transcript=True)
    _seed_code_block_transcript(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            Object.defineProperty(window, 'isSecureContext', { value: false });
            Object.defineProperty(navigator, 'clipboard', { value: undefined });
            window.__execCopies = [];
            window.__execAllow = true;
            document.execCommand = function (command) {
              if (command !== 'copy') return false;
              const field = document.activeElement;
              window.__execCopies.push(field && field.value);
              return window.__execAllow;
            };
            """
        )
        await page.goto(f"{base}/sessions/claude_code:test:sid1")
        button = page.locator(".ev.user .ev-text .md-code .code-copy")
        await button.wait_for()
        await button.click()
        await expect(button).to_have_class(re.compile(r"\bcopied\b"))
        copies = await page.evaluate("window.__execCopies")
        left_behind = await page.locator("textarea").count()

        await page.evaluate("window.__execAllow = false")
        await button.click()
        await expect(button).to_have_class(re.compile(r"\bcopy-failed\b"))
        failed_label = await button.get_attribute("aria-label")
        await browser.close()

    assert copies == [_CODE_BLOCK_SOURCE]
    assert left_behind == 0, "the fallback textarea must not stay in the page"
    assert failed_label == "Copy failed"


_LIVE_HTML = (
    '<audio controls src="/var/tmp/sample.wav"></audio>'
    '<div id="probe" style="height:120px">hello</div>'
    "<script>let reach = 'blocked';"
    "try { parent.document.title; reach = 'open'; } catch (e) {}"
    "document.getElementById('probe').dataset.reach = reach;"
    "fetch('/pollers/demo/pause', {method: 'POST', mode: 'no-cors'}).then("
    "() => { document.getElementById('probe').dataset.post = 'sent'; },"
    "() => { document.getElementById('probe').dataset.post = 'refused'; });"
    "navigator.sendBeacon('/pollers/demo/pause');</script>"
)


@pytest.mark.browser
async def test_an_agent_reply_shows_its_html_live_block_working(tmp_path):
    """An `html-live` block in an agent's reply is shown working, in a sandboxed
    frame sized to its content (#3547); the same block from the person stays code."""
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    block = "Two players:\n\n```html-live\n" + _LIVE_HTML + "\n```\n\nPick one."
    lines[0]["message"]["content"] = block
    lines[1]["message"]["content"][0]["text"] = block
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        posts = []
        page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
        await page.goto(f"{base}/sessions/claude_code:test:sid1")
        frame = page.locator(".ev.assistant .ev-text iframe.md-live")
        await frame.wait_for()
        inner = page.frame_locator(".ev.assistant .ev-text iframe.md-live")
        await expect(inner.locator("#probe")).to_have_attribute("data-reach", "blocked")
        # No login guards the deck's routes, so the frame may not send it requests.
        await expect(inner.locator("#probe")).to_have_attribute("data-post", "refused")
        # The frame starts at no height and grows to what its content reports.
        await page.wait_for_function(
            "() => document.querySelector('iframe.md-live').offsetHeight >= 120"
        )
        result = {
            "sandbox": await frame.get_attribute("sandbox"),
            "height": await frame.evaluate("element => element.offsetHeight"),
            "audio_src": await inner.locator("audio").evaluate("element => element.src"),
            "assistant_text": await page.locator(".ev.assistant .ev-text").text_content(),
            "user_frames": await page.locator(".ev.user iframe").count(),
            "user_code": await page.locator(".ev.user .md-code code").text_content(),
        }
        await context.close()
        await browser.close()

    assert result["sandbox"] == "allow-scripts"
    assert 120 <= result["height"] < 200
    assert result["audio_src"] == f"{base}/var/tmp/sample.wav"
    assert "Two players:" in result["assistant_text"]
    assert "Pick one." in result["assistant_text"]
    assert result["user_frames"] == 0
    assert not [url for url in posts if "/pollers/" in url]
    assert result["user_code"] == _LIVE_HTML


@pytest.mark.browser
async def test_markdown_renders_when_a_session_is_opened_client_side(tmp_path):
    """Opening a chat from the list mounts the detail pane without an HTMX swap,
    so the markdown enhancer has to run on the navigation event too — otherwise
    the transcript stays raw asterisks until the reader reloads the page."""
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    lines[0]["message"]["content"] = (
        "**Rendered on open** with [docs](https://example.test/docs)"
    )
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get("/sessions/claude_code:test:sid1")
    canonical_path = app.state.app_state.chat_path_for_session(
        "claude_code:test:sid1"
    )

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/session_navigation.js": (static_dir / "session_navigation.js").read_text(),
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
    }
    stylesheet = (static_dir / "app.css").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = {}
        for width in (1200, 320):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()
            await page.add_init_script(
                """
                class FakeEventSource extends EventTarget {
                  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
                  constructor(url) {
                    super(); this.url = url; this.readyState = FakeEventSource.OPEN;
                  }
                  close() { this.readyState = FakeEventSource.CLOSED; }
                }
                window.EventSource = FakeEventSource;
                """
            )

            async def serve(route, request):
                path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
                if path == "/":
                    await route.fulfill(
                        status=200, content_type="text/html", body=dashboard.text
                    )
                elif path == canonical_path:
                    await route.fulfill(
                        status=200, content_type="text/html", body=detail.text
                    )
                elif path in scripts:
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=scripts[path]
                    )
                elif path == "/static/app.css":
                    await route.fulfill(status=200, content_type="text/css", body=stylesheet)
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/")
            await page.click('a.session[data-session-key="claude_code:test:sid1"]')
            await page.wait_for_selector(".session-detail .transcript")
            message = page.locator(".session-detail .ev.user .ev-text")
            await message.locator("strong").wait_for(timeout=3000)
            results[width] = await page.evaluate(
                """() => {
                  const message = document.querySelector('.session-detail .ev.user .ev-text');
                  return {
                    enhanced: message.getAttribute('data-md'),
                    strong: message.querySelector('strong').textContent,
                    link: message.querySelector('a').getAttribute('href'),
                    asterisks: message.textContent.includes('**'),
                  };
                }"""
            )
            await context.close()
        await browser.close()

    for width, result in results.items():
        assert result == {
            "enhanced": "",
            "strong": "Rendered on open",
            "link": "https://example.test/docs",
            "asterisks": False,
        }, f"markdown not rendered at {width}px after a client-side open"


@pytest.mark.browser
async def test_mobile_chat_layers_over_live_list_and_back_and_swipe_are_instant(tmp_path):
    # On a phone the list sits under a chat when the chat was opened from it; a chat
    # page loaded on its own carries no list (#2603).
    app = _app_with_state(tmp_path)
    chat_path = app.state.app_state.sessions["claude_code:test:sid1"].detail_path
    async with _client(app) as client:
        chats = await client.get("/chats")
        detail = await client.get(
            chat_path, headers={"X-AgentDeck-Session-Navigation": "detail"}
        )

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()
    navigation_script = (static_dir / "session_navigation.js").read_text()
    list_requests = 0
    chats_loads = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()

        async def serve(route, request):
            nonlocal list_requests, chats_loads
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == chat_path:
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path == "/chats":
                chats_loads += 1
                await route.fulfill(status=200, content_type="text/html", body=chats.text)
            elif path == "/":
                list_requests += 1
                await route.fulfill(status=500, body="list navigation should stay client-side")
            elif path == "/static/mobile_session_stack.js":
                await route.fulfill(status=200, content_type="text/javascript", body=stack_script)
            elif path == "/static/session_navigation.js":
                await route.fulfill(
                    status=200, content_type="text/javascript", body=navigation_script
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/chats")
        await page.locator('a.session[data-session-key="claude_code:test:sid1"]').click()
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )

        initial = await page.evaluate(
            """() => ({
              path: location.pathname,
              sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
              sidebarHidden: document.querySelector('.session-sidebar').getAttribute('aria-hidden'),
              detailHidden: document.querySelector('.session-detail').getAttribute('aria-hidden'),
              listCard: Boolean(document.querySelector('.session-sidebar a.session')),
              overflow: document.documentElement.scrollWidth > innerWidth,
            })"""
        )

        modified_brand = await page.locator("a.brand").evaluate(
            """brand => {
              const click = new MouseEvent('click', {
                bubbles: true, cancelable: true, button: 0, ctrlKey: true,
              });
              return {
                allowed: brand.dispatchEvent(click),
                path: location.pathname,
                listOpen: document.body.classList.contains('mobile-list-open'),
              };
            }"""
        )
        await page.locator("a.back").click()
        await page.wait_for_function(
            "location.pathname === '/chats' && document.body.classList.contains('mobile-list-open')"
        )
        after_back = await page.evaluate(
            """() => ({
              path: location.pathname,
              detailInert: document.querySelector('.session-detail').inert,
              sidebarInert: document.querySelector('.session-sidebar').inert,
              backVisibility: getComputedStyle(document.querySelector('.back')).visibility,
            })"""
        )

        await page.evaluate("history.forward()")
        await page.wait_for_function(
            "location.pathname !== '/chats' && "
            "!document.body.classList.contains('mobile-list-open')"
        )
        # A vertical gesture near the edge remains transcript scrolling; it must
        # not claim the stack or leave a partial transform behind.
        vertical = await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const fire = (type, x, y) => detail.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 6, pointerType: 'touch',
                isPrimary: true, clientX: x, clientY: y,
              }));
              fire('pointerdown', 8, 200);
              fire('pointermove', 15, 280);
              fire('pointerup', 15, 280);
              return {
                path: location.pathname,
                dragging: document.body.classList.contains('mobile-stack-dragging'),
                inlineTransform: detail.style.getPropertyValue('--mobile-chat-x'),
              };
            }"""
        )
        # A fast swipe can be sampled as only down/up. Gesture ownership must
        # come from the full displacement, not depend on an intermediate move.
        await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const fire = (type, x, y) => detail.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch',
                isPrimary: true, clientX: x, clientY: y,
              }));
              fire('pointerdown', 8, 300);
              fire('pointerup', 190, 304);
            }"""
        )
        await page.wait_for_function(
            "location.pathname === '/chats' && document.body.classList.contains('mobile-list-open')"
        )
        after_fast_swipe = await page.evaluate(
            """() => ({
              path: location.pathname,
              listOpen: document.body.classList.contains('mobile-list-open'),
            })"""
        )

        await page.evaluate("history.forward()")
        await page.wait_for_function(
            "location.pathname !== '/chats' && "
            "!document.body.classList.contains('mobile-list-open')"
        )
        await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const fire = (type, x, y) => detail.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 8, pointerType: 'touch',
                isPrimary: true, clientX: x, clientY: y,
              }));
              fire('pointerdown', 8, 300);
              fire('pointermove', 20, 314);
              fire('pointermove', 190, 304);
              fire('pointerup', 190, 304);
            }"""
        )
        await page.wait_for_function(
            "location.pathname === '/chats' && document.body.classList.contains('mobile-list-open')"
        )
        after_swipe = await page.evaluate(
            """() => ({
              path: location.pathname,
              listOpen: document.body.classList.contains('mobile-list-open'),
              listCard: Boolean(document.querySelector('.session-sidebar a.session')),
              overflow: document.documentElement.scrollWidth > innerWidth,
            })"""
        )
        # The chats view is showing, not the front page, so the house is a real link
        # to `/` rather than one more "reveal the list" that does nothing (#2266).
        assert list_requests == 0
        assert chats_loads == 1, "every move between the list and the chat stayed in page"
        await page.locator("a.brand").click()
        await page.wait_for_function("location.pathname === '/'")
        await browser.close()

    assert list_requests == 1
    assert initial == {
        "path": chat_path,
        "sidebar": "block",
        "sidebarHidden": "true",
        "detailHidden": "false",
        "listCard": True,
        "overflow": False,
    }
    assert modified_brand == {
        "allowed": True,
        "path": chat_path,
        "listOpen": False,
    }
    accessible_list = {
        "path": "/chats",
        "detailInert": True,
        "sidebarInert": False,
        "backVisibility": "hidden",
    }
    assert after_back == accessible_list
    assert vertical == {
        "path": chat_path,
        "dragging": False,
        "inlineTransform": "",
    }
    assert after_fast_swipe == {"path": "/chats", "listOpen": True}
    assert after_swipe == {
        "path": "/chats",
        "listOpen": True,
        "listCard": True,
        "overflow": False,
    }


@pytest.mark.browser
async def test_back_arrow_loads_the_deck_when_no_list_is_mounted(tmp_path):
    """The chat body served for in-page navigation carries no sidebar. Landing on
    one of those as the whole document used to make the back arrow slide the chat
    aside over nothing — an empty page under a live topbar, unrecoverable because
    pressing back again did the same thing (#2047)."""
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        detail = await client.get(
            "/sessions/claude_code:test:sid1",
            headers={"X-AgentDeck-Session-Navigation": "detail"},
        )
        dashboard = await client.get("/chats")
    assert ".session-sidebar" not in detail.text
    assert "X-AgentDeck-Session-Navigation" in detail.headers["Vary"]

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()
    navigation_script = (static_dir / "session_navigation.js").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/claude_code:test:sid1":
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path == "/":
                await route.fulfill(
                    status=200, content_type="text/html", body=dashboard.text
                )
            elif path == "/static/mobile_session_stack.js":
                await route.fulfill(status=200, content_type="text/javascript", body=stack_script)
            elif path == "/static/session_navigation.js":
                await route.fulfill(
                    status=200, content_type="text/javascript", body=navigation_script
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        await page.wait_for_selector(".session-detail")
        landed = await page.evaluate(
            """() => ({
              sidebar: Boolean(document.querySelector('.session-sidebar')),
              detail: Boolean(document.querySelector('.session-detail')),
            })"""
        )

        await page.locator("a.back").click()
        await page.wait_for_function("location.pathname === '/'")
        await page.wait_for_selector("#sessions")
        recovered = await page.evaluate(
            """() => ({
              path: location.pathname,
              listPage: document.body.classList.contains('session-list-page'),
              deck: Boolean(document.querySelector('#sessions')),
              mainEmpty: document.querySelector('main').clientHeight === 0,
            })"""
        )
        await context.close()
        await browser.close()

    assert landed == {"sidebar": False, "detail": True}
    assert recovered == {
        "path": "/",
        "listPage": True,
        "deck": True,
        "mainEmpty": False,
    }


@pytest.mark.browser
async def test_mobile_fast_swipe_toward_earlier_messages_jumps_to_top(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        detail = await client.get("/sessions/claude_code:test:sid1")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()
    navigation_script = (static_dir / "session_navigation.js").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800},
            has_touch=True,
            service_workers="block",
        )
        page = await context.new_page()

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/claude_code:test:sid1":
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path == "/static/mobile_session_stack.js":
                await route.fulfill(
                    status=200, content_type="text/javascript", body=stack_script
                )
            elif path == "/static/session_navigation.js":
                await route.fulfill(
                    status=200, content_type="text/javascript", body=navigation_script
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        result = await page.evaluate(
            """async () => {
              const detail = document.querySelector('.session-detail');
              const transcript = document.querySelector('.transcript');
              const frame = () => new Promise(requestAnimationFrame);
              const settle = async () => { await frame(); await frame(); };
              const waitForTop = async () => {
                for (let index = 0; index < 120; index += 1) {
                  if (detail.scrollTop <= 1) return detail.scrollTop;
                  await frame();
                }
                return detail.scrollTop;
              };
              const fireTouch = (type, x, y, identifier) => {
                const touch = new Touch({
                  identifier, target: detail,
                  clientX: x, clientY: y, screenX: x, screenY: y,
                  pageX: x, pageY: y,
                });
                const active = type === 'touchend' ? [] : [touch];
                detail.dispatchEvent(new TouchEvent(type, {
                  bubbles: true, cancelable: true,
                  touches: active, targetTouches: active, changedTouches: [touch],
                }));
              };
              const swipe = async (from, to, delay, identifier) => {
                fireTouch('touchstart', from[0], from[1], identifier);
                await new Promise(resolve => setTimeout(resolve, delay));
                fireTouch('touchend', to[0], to[1], identifier);
                await settle();
                return detail.scrollTop;
              };

              transcript.insertAdjacentHTML(
                'afterbegin',
                Array.from({length: 50}, (_, index) =>
                  `<div class="ev" style="min-height:80px">older ${index}</div>`
                ).join('')
              );
              detail.scrollTo(0, detail.scrollHeight);
              await settle();
              const initial = detail.scrollTop;
              const nativeScrollTo = detail.scrollTo.bind(detail);
              const topScrolls = [];
              detail.scrollTo = (...args) => {
                if (typeof args[0] === 'object') topScrolls.push(args[0]);
                nativeScrollTo(...args);
              };
              const opposite = await swipe([160, 330], [160, 170], 20, 1);
              const horizontal = await swipe([40, 200], [220, 210], 20, 2);
              const shortFast = await swipe([160, 170], [160, 330], 20, 3);
              const longSlow = await swipe([160, 60], [160, 340], 300, 4);

              detail.scrollTo(0, detail.clientHeight * 1.5);
              await settle();
              const nearTopStart = detail.scrollTop;
              const nearTop = await swipe([160, 60], [160, 340], 20, 5);

              detail.scrollTo(0, detail.scrollHeight);
              await settle();
              const beforeFast = detail.scrollTop;
              // Sample the smooth scroll at its first observable movement, not
              // a fixed two frames in: when the animation starts is the
              // browser's business, and on a slower machine two frames after
              // touchend it has not moved yet. An instant jump still fails the
              // assertion — its first observed position is already 0.
              fireTouch('touchstart', 160, 60, 6);
              await new Promise(resolve => setTimeout(resolve, 20));
              fireTouch('touchend', 160, 340, 6);
              let smoothStart = detail.scrollTop;
              for (let index = 0; index < 120; index += 1) {
                await frame();
                if (detail.scrollTop !== beforeFast) {
                  smoothStart = detail.scrollTop;
                  break;
                }
              }
              const fast = await waitForTop();
              return {
                initial, opposite, horizontal, shortFast, longSlow,
                nearTopStart, nearTop, beforeFast, smoothStart, fast, topScrolls,
              };
            }"""
        )
        await browser.close()

    assert result["initial"] > 1000
    assert result["opposite"] == result["initial"]
    assert result["horizontal"] == result["initial"]
    assert result["shortFast"] == result["initial"]
    assert result["longSlow"] == result["initial"]
    assert result["nearTop"] == result["nearTopStart"]
    assert result["beforeFast"] == result["initial"]
    assert 0 < result["smoothStart"] < result["beforeFast"]
    assert result["fast"] <= 1
    assert result["topScrolls"] == [{"top": 0, "behavior": "smooth"}]


async def test_subagent_count_renders_on_card_and_detail_header(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.subagent_count = 2
    session.subagents = (
        SubagentProgress(
            agent_id="agent-1",
            nickname="Faraday",
            role="scout",
            task="Inventory the Bazaar migration surface",
            status="working",
            updated_at=datetime(2026, 7, 17, 7, 42, tzinfo=UTC),
        ),
        SubagentProgress(
            agent_id="agent-2",
            nickname="Banach",
            role="investigator",
            task="Audit the canonical engine boundary",
            status="finished",
            result="Found one status-normalization bug.",
            updated_at=datetime(2026, 7, 17, 7, 43, tzinfo=UTC),
        ),
    )

    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get("/sessions/claude_code:test:sid1")

    assert "2 sub-agents" in dashboard.text
    assert 'title="This chat is running spawned agents"' in dashboard.text
    assert "2 sub-agents" in detail.text
    assert 'aria-label="Subagent progress"' in detail.text
    assert 'class="subagent-activity active"' in detail.text
    assert "Faraday" in detail.text
    assert "scout" in detail.text
    assert "Inventory the Bazaar migration surface" in detail.text
    assert "Banach" in detail.text
    assert "Found one status-normalization bug." in detail.text
    assert (
        detail.text.index("an answer here")
        < detail.text.index('id="tool-activity"')
        < detail.text.index('id="subagent-activity"')
    )
    # A stale raw subagent count must not keep the session card Working
    # forever when its terminal envelope was missed.
    assert 'data-working="1"' not in _session_card(dashboard.text, session.key)
    assert '<span class="status-tag thinking">thinking</span>' not in detail.text
    assert "1 working" not in dashboard.text


@pytest.mark.browser
async def test_subagent_name_stays_bounded_on_narrow_mobile(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.subagent_count = 1
    session.subagents = (
        SubagentProgress(
            agent_id="agent-1",
            nickname=(
                "Translate every remaining dialogue line in the entire game without "
                "changing control tokens"
            ),
            role="investigator",
            task="Inspect the requested translation surface",
            status="working",
        ),
    )

    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")
    stylesheet = (
        Path(__file__).parents[1] / "src/agentdeck/web/static/app.css"
    ).read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(response.text)
        await page.add_style_tag(content=stylesheet)
        metrics = await page.locator(".subagent-name strong").evaluate(
            """name => {
              const rect = name.getBoundingClientRect();
              const style = getComputedStyle(name);
              const status = name.parentElement.querySelector('.subagent-status')
                .getBoundingClientRect();
              const copy = name.closest('.subagent-copy').getBoundingClientRect();
              return {
                height: rect.height,
                lineHeight: parseFloat(style.lineHeight),
                overflow: style.overflow,
                textOverflow: style.textOverflow,
                whiteSpace: style.whiteSpace,
                isTruncated: name.scrollWidth > name.clientWidth,
                statusInsideCopy: status.right <= copy.right + 1,
                pageOverflow: document.documentElement.scrollWidth
                  - document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert metrics["height"] <= metrics["lineHeight"] + 1
    assert metrics["overflow"] == "hidden"
    assert metrics["textOverflow"] == "ellipsis"
    assert metrics["whiteSpace"] == "nowrap"
    assert metrics["isTruncated"]
    assert metrics["statusInsideCopy"]
    assert metrics["pageOverflow"] <= 1


async def test_nested_subagents_collapse_by_default_with_toggle(tmp_path):
    # Issue #5: nested sub-agent rows render collapsed by default, behind a
    # count-pill toggle on the parent card, with an activity hint when a child
    # is working. The rows stay in the DOM (collapse is CSS only).
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    state.update_session(
        Session(
            key="claude_code:test:childA",
            account_key="claude_code:test",
            session_id="childA",
            status=SessionStatus.LIVE,
            title="Scout the parser",
            parent_session_key="claude_code:test:sid1",
            thinking=True,  # a working child → the pill shows the working hint
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:childB",
            account_key="claude_code:test",
            session_id="childB",
            status=SessionStatus.LIVE,
            title="Audit the boundary",
            parent_session_key="claude_code:test:sid1",
            thinking=True,  # a second live child → both nest under the toggle
        )
    )

    async with _client(app) as client:
        dashboard = await client.get("/chats")
        detail = await client.get("/sessions/claude_code:test:sid1")

    text = dashboard.text
    # Collapsed by default, tied to the parent key.
    assert 'class="subagent-children collapsed" data-subagents-of="claude_code:test:sid1"' in text
    # Count-pill toggle affordance, collapsed (aria-expanded=false).
    assert 'class="subagent-toggle"' in text
    assert 'data-key="claude_code:test:sid1"' in text
    assert 'aria-expanded="false"' in text
    assert "2 sub-agents" in text
    # A working child surfaces the activity hint even while collapsed.
    assert "subagent-hint working" in text
    # The rows themselves are still rendered (hidden via CSS, not omitted).
    assert "Scout the parser" in text
    assert "Audit the boundary" in text
    assert 'data-working="1"' in _session_card(text, "claude_code:test:sid1")
    # Working only through its children: thinking, marked for voice mode (#3529).
    assert '<span class="status-tag thinking" data-background>thinking</span>' in detail.text
    assert 'aria-label="Subagent progress"' in detail.text
    assert "Scout the parser" in detail.text
    assert "Audit the boundary" in detail.text
    assert (
        '<a class="subagent-row working" href="/sessions/claude_code:test:childA"'
        in detail.text
    )
    assert (
        '<a class="subagent-row working" href="/sessions/claude_code:test:childB"'
        in detail.text
    )


def test_subagent_notification_renders_as_compact_expandable_row(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    html = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=1,
                role="system",
                text="Audit complete.\n\nFull evidence follows.",
                tool_name="subagent",
                tool_summary="Audit complete.",
                subagent_status="finished",
                subagent_id="agent-1",
                subagent_name="Faraday",
            )
        ],
    )

    assert '<details class="ev tool subagent-update finished">' in html
    assert "Faraday finished" in html
    assert "Audit complete." in html
    assert "Full evidence follows." in html


async def test_dashboard_leaves_a_finished_chat_unhighlighted(tmp_path):
    """A chat that stops working goes idle in place: no amber card, no "just
    finished" badge, and no hoisting above the server's ordering (issue #550)."""
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:think1",
            account_key="claude_code:test",
            session_id="think1",
            status=SessionStatus.LIVE,
            thinking=True,
            title="Busy session",
        )
    )
    async with _client(app) as client:
        response = await client.get("/chats")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    surfaces = {
        "dashboard": response.text,
        "app.css": (static_dir / "app.css").read_text(),
    }
    for name, text in surfaces.items():
        for marker in ("recently-stopped", "recentlyStopped", "just finished", "\u2605"):
            assert marker not in text, f"{marker!r} still in {name}"


def test_display_state_alive_vs_thinking():
    from agentdeck.models import Session, SessionStatus

    def s(**kw):
        return Session(key="k", account_key="a", session_id="s", **kw)

    assert s(status=SessionStatus.LIVE, thinking=True).display_state == "thinking"
    assert s(status=SessionStatus.LIVE, thinking=False).display_state == "idle"  # alive, resting
    assert s(status=SessionStatus.IDLE).display_state == "idle"
    assert s(status=SessionStatus.REMOTE).display_state == "remote"


async def test_idle_sessions_hidden_but_reachable(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:idle1",
            account_key="claude_code:test",
            session_id="idle1",
            status=SessionStatus.IDLE,
            title="An Old Finished Session",
        )
    )
    async with _client(app) as c:
        listing = await c.get("/chats")
        direct = await c.get("/sessions/claude_code:test:idle1")
    assert "An Old Finished Session" not in listing.text  # hidden from the list
    assert "Hello World Session" in listing.text  # the live one still shows
    assert direct.status_code == 200  # but reachable by direct URL (read-only view)


async def test_provider_can_keep_idle_sessions_visible(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="codex:test:idle1",
            account_key="codex:test",
            session_id="idle1",
            status=SessionStatus.IDLE,
            title="A Quiet Codex Chat",
            show_when_idle=True,
        )
    )
    async with _client(app) as c:
        listing = await c.get("/chats")
    assert "A Quiet Codex Chat" in listing.text


async def test_partial_limit_bars(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/partials/limit-bars")
    assert r.status_code == 200
    assert "42%" in r.text
    assert "7%" in r.text


def test_usage_stale_is_age_based_not_last_poll(tmp_path):
    # Issue #6: "stale" reflects data age, not a single rate-limited poll.
    from dataclasses import replace
    from datetime import timedelta

    from agentdeck.models import Account
    from agentdeck.state import AppState
    from agentdeck.web.render import _usage_rows

    state = AppState()
    state.usage_stale_after_s = 300.0
    acc = Account("claude_code:main", "claude_code", "main", tmp_path)
    now = datetime.now(UTC)
    fresh_but_failed = UsageSnapshot(
        account_key=acc.key,
        five_hour_pct=90.0,
        five_hour_resets_at=None,
        seven_day_pct=42.0,
        seven_day_resets_at=None,
        fetched_at=now - timedelta(seconds=60),
        stale=True,  # last poll 429'd, but the numbers are a minute old
    )
    state.usage[acc.key] = fresh_but_failed
    assert _usage_rows([acc], state)[0]["stale"] is False

    # Genuinely old data reads as stale even if the last poll succeeded.
    state.usage[acc.key] = replace(
        fresh_but_failed, fetched_at=now - timedelta(seconds=400), stale=False
    )
    row = _usage_rows([acc], state)[0]
    assert row["stale"] is True
    assert row["stale_after"] == 300.0


async def test_old_usage_renders_stale_badge_and_threshold_attr(tmp_path):
    from datetime import timedelta

    app = _app_with_state(tmp_path)
    # threshold = max(3 * usage_interval_s, 300) = 900s for the default config
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=90.0,
            five_hour_resets_at=None,
            seven_day_pct=42.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC) - timedelta(seconds=1000),
        )
    )
    async with _client(app) as c:
        r = await c.get("/partials/limit-bars")
    assert r.status_code == 200
    assert "stale-tag" in r.text  # server marks the aged snapshot stale
    assert 'data-stale-after="900.0"' in r.text  # client can flip it live


async def test_session_detail_renders_transcript(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    # The transcript and its stream cursor are the subject, so this reads the
    # page an operator actually lands on: sidebar list shut, no `chats=1` on the
    # stream URL asserted below (#1525).
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1")
    assert r.status_code == 200
    assert "first question" in r.text
    assert "an answer here" in r.text
    assert "claude-opus-4-8" in r.text
    assert "15 tok" in r.text  # 10 input + 5 output summed from usage
    assert r.text.count('class="ev-time"') == 2
    assert 'datetime="2026-07-15T07:41:00+00:00"' in r.text
    assert 'datetime="2026-07-15T07:42:00+00:00"' in r.text
    # usage bars paint server-side in the topbar (no separate /events socket)
    assert "42%" in r.text
    assert (
        'class="mini-acct active" aria-current="true" title="Current chat account"'
        in r.text
    )
    # the page binds its single SSE connection to the per-session stream
    assert 'sse-connect="/events/sessions/claude_code:test:sid1?after_seq=2"' in r.text


async def test_chat_route_and_old_leg_alias_survive_link_before_scan(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    source_key = "claude_code:test:sid1"
    target_key = "claude_code:test:sid2"
    target_singleton = app.state.budget_db.ensure_session_chats((target_key,))[0]
    chat = app.state.budget_db.append_chat_leg(source_key, target_key)

    async with _client(app) as client:
        canonical_during_race = await client.get(f"/chats/{chat.chat_id}")
        absorbed_alias_during_race = await client.get(
            f"/chats/{target_singleton.chat_id}"
        )
        old_alias_during_race = await client.get(f"/sessions/{source_key}")

        # The durable tail exists before a provider scan can resolve it. Both
        # URLs keep rendering the source rather than transiently returning 404.
        assert canonical_during_race.status_code == 200
        assert absorbed_alias_during_race.status_code == 200
        assert old_alias_during_race.status_code == 200
        assert "an answer here" in canonical_during_race.text

        state.update_session(
            Session(
                key=target_key,
                account_key="claude_code:test",
                session_id="sid2",
                status=SessionStatus.IDLE,
                title="Current leg",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )
        canonical_after_scan = await client.get(f"/chats/{chat.chat_id}")
        absorbed_alias_after_scan = await client.get(
            f"/chats/{target_singleton.chat_id}"
        )
        old_alias_after_scan = await client.get(f"/sessions/{source_key}")

    assert canonical_after_scan.status_code == 200
    assert absorbed_alias_after_scan.status_code == 200
    assert old_alias_after_scan.status_code == 200
    assert 'sse-connect="/events/sessions/claude_code:test:sid2' in canonical_after_scan.text
    assert (
        'sse-connect="/events/sessions/claude_code:test:sid2'
        in absorbed_alias_after_scan.text
    )
    assert 'sse-connect="/events/sessions/claude_code:test:sid2' in old_alias_after_scan.text


@pytest.mark.browser
async def test_chat_row_switches_current_leg_in_place_without_mobile_height_cost(tmp_path):
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    source_key = "claude_code:test:sid1"
    target_key = "claude_code:alt:sid2"
    source = replace(
        state.sessions[source_key],
        title="Durable row",
        last_text="Source preview",
        last_role="agent",
    )
    state.update_session(source)
    state.update_session(
        Session(
            key="claude_code:test:child",
            account_key="claude_code:test",
            session_id="child",
            status=SessionStatus.LIVE,
            thinking=True,
            title="Working child",
            parent_session_key=source_key,
        )
    )
    source_chat = state.chat_for_session(source_key)
    assert source_chat is not None
    app.state.accounts.append(
        Account("claude_code:alt", "claude_code", "alt", tmp_path)
    )
    app.state.budget_db.ensure_session_chats((target_key,))

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + "/chats")
        row = page.locator(f'[data-chat-key="{source_chat.chat_id}"]')
        await row.wait_for(state="visible")
        before_height = await row.evaluate("element => element.getBoundingClientRect().height")
        await row.locator("[data-subagents-toggle]").click()
        await row.locator("[data-subagents-toggle]").focus()
        children = page.locator(
            f'.subagent-children[data-parent-chat-key="{source_chat.chat_id}"]'
        )
        await expect(children).not_to_have_class(re.compile(r"\bcollapsed\b"))

        linked = app.state.budget_db.append_chat_leg(source_key, target_key)
        assert linked.chat_id == source_chat.chat_id
        state.update_session(
            Session(
                key=target_key,
                account_key="claude_code:alt",
                session_id="sid2",
                status=SessionStatus.LIVE,
                title="Durable row",
                last_text="Current preview",
                last_role="agent",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

        # Nothing streams the list (#2603); the page's own reload brings the switch in.
        await page.reload()
        current_selector = (
            f'[data-chat-key="{source_chat.chat_id}"][data-session-key="{target_key}"]'
        )
        current = page.locator(current_selector)
        await expect(current).to_have_count(1)
        await expect(current.locator(".latest-scroll")).to_contain_text("Current preview")
        await expect(current.locator(".acct-badge")).to_have_text("test▸alt")
        await current.locator("[data-subagents-toggle]").click()
        await expect(children).not_to_have_class(re.compile(r"\bcollapsed\b"))
        assert await page.locator(
            f'#sessions [data-chat-key="{source_chat.chat_id}"]'
        ).count() == 1
        assert await page.locator(f'#sessions [data-session-key="{source_key}"]').count() == 0
        await wait_until(
            page,
            "switching the current leg cost the row no height",
            "(args) => {"
            " const el = document.querySelector(args.selector);"
            " return !!el && el.getBoundingClientRect().height === args.expected;"
            "}",
            arg={"selector": current_selector, "expected": before_height},
        )
        assert await page.evaluate(
            "document.querySelector('#sessions').scrollWidth <= "
            "document.querySelector('#sessions').clientWidth"
        )
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1264-chat-chain-mobile.png"),
                full_page=True,
            )
        await page.close()
        await browser.close()

    async with _client(app) as client:
        dashboard = await client.get("/chats")
    for response in (dashboard,):
        assert response.text.count(f'data-chat-key="{source_chat.chat_id}"') == 1
        assert f'data-session-key="{source_key}"' not in response.text
        assert f'data-session-key="{target_key}"' in response.text


async def test_session_agent_owns_its_related_issue_and_pull_request_lists(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    session_key = "claude_code:test:sid1"
    issue_path = f"/api/sessions/{session_key}/issues"
    path = f"/api/sessions/{session_key}/pull-requests"

    async with _client(app) as client:
        assert (await client.get(issue_path)).json() == {
            "session_key": session_key,
            "issues": [],
        }
        issue_added = await client.post(
            issue_path,
            json={"issue": "https://github.com/eerovil/agentdeck/issues/961"},
        )
        assert issue_added.status_code == 200
        assert issue_added.json()["issues"] == [
            {
                "repository": "eerovil/agentdeck",
                "number": 961,
                "url": "https://github.com/eerovil/agentdeck/issues/961",
            }
        ]
        issue_again = await client.post(
            issue_path, json={"issue": "eerovil/agentdeck#961"}
        )
        assert [issue["number"] for issue in issue_again.json()["issues"]] == [961]

        for value in ("#961", "961", "https://github.com/eerovil/agentdeck/pull/961"):
            rejected = await client.post(issue_path, json={"issue": value})
            assert rejected.status_code == 422

        issue_replaced = await client.put(
            issue_path,
            json={"issues": ["eerovil/agentdeck#962", "acmecorp/bazaar#7"]},
        )
        assert [issue["number"] for issue in issue_replaced.json()["issues"]] == [962, 7]
        issue_removed = await client.delete(
            f"{issue_path}?issue=acmecorp/bazaar%237"
        )
        assert [issue["repository"] for issue in issue_removed.json()["issues"]] == [
            "eerovil/agentdeck"
        ]

        assert (await client.get(path)).json() == {
            "session_key": session_key,
            "pull_requests": [],
        }

        added = await client.post(
            path, json={"pull_request": "https://github.com/eerovil/agentdeck/pull/91"}
        )
        assert added.status_code == 200
        assert added.json()["pull_requests"] == [
            {
                "repository": "eerovil/agentdeck",
                "number": 91,
                "url": "https://github.com/eerovil/agentdeck/pull/91",
            }
        ]
        # The owning-repo shorthand from kanban handoffs is accepted too, and
        # re-registering a known PR is not a duplicate.
        again = await client.post(path, json={"pull_request": "eerovil/agentdeck#91"})
        assert [pr["number"] for pr in again.json()["pull_requests"]] == [91]

        # A bare number has no repository, which is exactly the guess this
        # registry removes.
        for value in ("#91", "91", "not a pull request"):
            rejected = await client.post(path, json={"pull_request": value})
            assert rejected.status_code == 422

        replaced = await client.put(
            path,
            json={
                "pull_requests": [
                    "https://github.com/eerovil/agentdeck/pull/92",
                    "acmecorp/bazaar#7",
                ]
            },
        )
        assert [pr["number"] for pr in replaced.json()["pull_requests"]] == [92, 7]

        removed = await client.delete(f"{path}?pull_request=acmecorp/bazaar%237")
        assert [pr["repository"] for pr in removed.json()["pull_requests"]] == [
            "eerovil/agentdeck"
        ]

        # In two Works, a new pull request must say which it is for, at registration
        # (#3392), and only one of the chat's own; the earlier one keeps its Work.
        await client.post(issue_path, json={"issue": "eerovil/agentdeck#963"})
        unplaced = await client.post(path, json={"pull_request": "eerovil/agentdeck#93"})
        assert unplaced.status_code == 409
        assert "several Works" in unplaced.json()["detail"]
        stranger = await client.post(
            path, json={"pull_request": "eerovil/agentdeck#93", "work_id": "eerovil/x#1"}
        )
        assert stranger.status_code == 409
        placed = await client.post(
            path, json={"pull_request": "eerovil/agentdeck#93", "work_id": "eerovil/agentdeck#963"}
        )
        assert [pr["number"] for pr in placed.json()["pull_requests"]] == [92, 93]
        kept = await client.put(
            path, json={"pull_requests": ["eerovil/agentdeck#92", "eerovil/agentdeck#93"]}
        )
        assert kept.status_code == 200
        assert {ref.number: ref.work_id for ref in state.pull_requests_for(session_key)} == {
            92: "github_issue:eerovil/agentdeck#962",
            93: "github_issue:eerovil/agentdeck#963",
        }
        moved = await client.put(
            path,
            json={"pull_requests": ["eerovil/agentdeck#92"], "work_id": "eerovil/agentdeck#963"},
        )
        assert moved.status_code == 200, "a named Work moves one already registered"
        assert state.pull_requests_for(session_key)[0].work_id == (
            "github_issue:eerovil/agentdeck#963"
        )
        await client.delete(f"{path}?pull_request=eerovil/agentdeck%2393")
        await client.delete(f"{issue_path}?issue=eerovil/agentdeck%23963")

    assert state.sessions[session_key].issues == (IssueRef("eerovil/agentdeck", 962),)
    # The PR registration is what a Deckhand context resolve now reads.
    assert state.sessions[session_key].pull_requests == (
        PullRequestRef("eerovil/agentdeck", 92),
    )


async def test_unknown_session_cannot_register_pull_requests(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/api/sessions/claude_code:test:missing/pull-requests",
            json={"pull_request": "eerovil/agentdeck#1"},
        )
        issue_response = await client.post(
            "/api/sessions/claude_code:test:missing/issues",
            json={"issue": "eerovil/agentdeck#1"},
        )
    assert response.status_code == 404
    assert issue_response.status_code == 404


async def test_session_agent_owns_turn_scoped_attention_state(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    session_key = "claude_code:test:sid1"
    path = f"/api/sessions/{session_key}/attention"

    async with _client(app) as client:
        assert (await client.get(path)).json()["state"] is None

        waiting = await client.put(path, json={"state": "waiting"})
        assert waiting.status_code == 200
        assert waiting.json()["state"] == "waiting"
        assert waiting.json()["updated_at"] is not None

        finished = await client.put(path, json={"state": "finished"})
        assert finished.status_code == 200
        assert finished.json()["state"] == "finished"

        rejected = await client.put(path, json={"state": "blocked"})
        assert rejected.status_code == 422

        cleared = await client.delete(path)
        assert cleared.status_code == 200
        assert cleared.json()["state"] is None


async def test_context_delivery_attention_exposes_its_durable_detail(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    session_key = "claude_code:test:sid1"
    app.state.app_state.set_attention_report(
        session_key,
        "waiting",
        "Context delivery poller:d1 remains fenced; confirm or abandon it.",
    )

    async with _client(app) as client:
        payload = (await client.get(f"/api/sessions/{session_key}/attention")).json()

    assert payload["state"] == "waiting"
    assert payload["detail"] == (
        "Context delivery poller:d1 remains fenced; confirm or abandon it."
    )


async def test_context_delivery_resolution_requires_exact_id_choice_and_rationale(
    tmp_path,
):
    app = _app_with_state(tmp_path, with_transcript=True)
    db = Db(tmp_path / "context-resolution.db")
    app.state.app_state.budget_db = db
    row = db.reserve_context_delivery(
        ContextDelivery(
            action_id="action",
            attempt=1,
            provider_id="claude_code",
            account_key="claude_code:test",
            worker_key="agentdeck#1479",
            delivery_id="poller:d1",
            request_sha256="a" * 64,
            chat_id="chat",
            source_session_key="claude_code:test:sid1",
            source_session_id="sid1",
            mode="enforce",
            proposed_action="none",
            executed_action="none",
            reason="below_threshold",
        )
    )
    assert row is not None
    assert db.mark_uncertain(row.action_id, provider_write_state="unknown")
    path = "/api/workers/accounts/claude_code/test/context-delivery-resolution"
    resolution = {
        "key": "agentdeck#1479",
        "delivery_id": "poller:d1",
        "choice": "abandoned",
        "rationale": "operator cannot establish whether the provider accepted it",
    }

    # The web route settles the durable worker receipt through the runtime, so
    # stand in for it with the real receipt fence behind a mock transport.
    receipts = DeliveryReceipts(
        {
            "poller:d1": {
                "fingerprint": "f" * 64,
                "session_id": "sid1",
                "at": 1.0,
                "delivery_stage": "write_may_have_started",
                "phase_delivery_id": "poller:d1",
                "source_session_id": "sid1",
            }
        }
    )

    def runtime_handle(runtime_request):
        payload = json.loads(runtime_request.content)
        result = receipts.resolve(
            payload["delivery_id"],
            payload["choice"],
            payload["rationale"],
            session_id="sid1",
        )
        return httpx.Response(
            200,
            json={
                "accepted": result.accepted,
                "action": result.action,
                "reason": result.reason,
                "session_id": result.session_id,
            },
        )

    app.state.runtime_http = AsyncClient(
        transport=httpx.MockTransport(runtime_handle), base_url="http://runtime"
    )

    async with _client(app) as client:
        missing = await client.put(path, json={**resolution, "delivery_id": "wrong"})
        invalid = await client.put(path, json={**resolution, "choice": "resend"})
        first = await client.put(path, json=resolution)
        replay = await client.put(path, json=resolution)
        conflict = await client.put(path, json={**resolution, "choice": "accepted"})

    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert first.status_code == replay.status_code == 200
    resolved = first.json()["context_delivery"]
    assert resolved["status"] == "completed"
    assert resolved["resolution_choice"] == "abandoned"
    assert conflict.status_code == 409
    await app.state.runtime_http.aclose()
    db.close()


async def test_unknown_session_cannot_report_attention(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.put(
            "/api/sessions/claude_code:test:missing/attention",
            json={"state": "waiting"},
        )
    assert response.status_code == 404












# --- the pinned desk row and the babysit button (#824) ---------------------
























































async def test_session_agent_can_name_its_own_chat(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    session_key = "claude_code:test:sid1"
    path = f"/api/sessions/{session_key}/title"

    async with _client(app) as client:
        assert (await client.get(path)).json()["title"] is None

        named = await client.put(path, json={"title": "Replace PR parsing with a registry"})
        assert named.status_code == 200
        assert named.json()["title"] == "Replace PR parsing with a registry"
        assert named.json()["source"] == "agent"
        assert named.json()["native_title"] == "Hello World Session"

        page = await client.get(f"/sessions/{session_key}")
        assert "Replace PR parsing with a registry" in page.text

        assert (await client.put(path, json={"title": "   "})).status_code == 422
        assert state.sessions[session_key].display_title == (
            "Replace PR parsing with a registry"
        )

        cleared = await client.delete(path)
        assert cleared.json()["title"] is None
        assert cleared.json()["display_title"] == "Hello World Session"

    assert state.sessions[session_key].generated_title is None


async def test_operator_can_rename_a_chat_and_hand_naming_back(tmp_path):
    # #1569: the ✎ Rename control writes the same stored title the chat's own
    # naming writes, and clearing it returns the provider-native name.
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    session_key = "claude_code:test:sid1"
    path = f"/sessions/{session_key}/title"

    async with _client(app) as client:
        page = await client.get(f"/sessions/{session_key}")
        assert 'id="session-title-editor"' in page.text

        saved = await client.post(path, data={"action": "save", "title": "Rename the chat"})
        assert saved.status_code == 200
        assert "Rename the chat" in saved.text
        assert state.sessions[session_key].display_title == "Rename the chat"

        # A refused save re-renders what was typed instead of swallowing it.
        refused = await client.post(path, data={"action": "save", "title": "   "})
        assert "Give the chat a name first." in refused.text
        assert state.sessions[session_key].display_title == "Rename the chat"

        # The chat's own naming does not overwrite a hand-typed title.
        api = await client.put(
            f"/api/sessions/{session_key}/title", json={"title": "Something else"}
        )
        assert api.json()["title"] == "Rename the chat"
        assert api.json()["source"] == "user"

        # Nor may the chat erase it and fall back to the provider's own name.
        api_cleared = await client.delete(f"/api/sessions/{session_key}/title")
        assert api_cleared.json()["title"] == "Rename the chat"
        assert state.sessions[session_key].display_title == "Rename the chat"

        cleared = await client.post(path, data={"action": "clear"})
        assert "Cleared" in cleared.text

    assert state.sessions[session_key].generated_title is None
    assert state.sessions[session_key].display_title == "Hello World Session"




@pytest.mark.browser
async def test_rename_control_edits_and_clears_a_chat_title(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    key = "claude_code:test:sid1"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + f"/sessions/{key}")
        editor = page.locator("#session-title-editor")
        await expect(editor).to_be_visible()
        # Collapsed by default: the text box costs no height until it is opened.
        await expect(page.locator("#session-title-input")).to_be_hidden()

        await editor.locator("summary").click()
        await page.fill("#session-title-input", "Chase the flaky poller wake")
        await page.click('#session-title-editor button[value="save"]')
        await expect(page.locator("#session-title-editor .account-save-ok")).to_be_visible()
        await expect(page.locator(".session-head .session-title")).to_contain_text(
            "Chase the flaky poller wake"
        )
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        await page.click('#session-title-editor button[value="clear"]')
        await expect(page.locator(".session-head .session-title")).to_contain_text(
            "Hello World Session"
        )
        await browser.close()


async def test_related_issue_chip_carries_the_resolved_github_state(tmp_path):
    # The chip below the transcript says `closed` next to the issue number when
    # the deck has already resolved that state for the card, and stays a bare
    # "Issue #n" for a registered issue it has no resolved state for (#1113).
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.add_issue(key, IssueRef("eerovil/agentdeck", 1113))
    state.add_issue(key, IssueRef("eerovil/agentdeck", 2000))
    state.update_session(
        replace(
            state.sessions[key],
            issue_url="https://github.com/eerovil/agentdeck/issues/1113",
            issue_status="closed",
            issue_status_kind="done",
        )
    )

    async with _client(app) as client:
        response = await client.get(f"/sessions/{key}")

    action_bar = _session_action_bar(response.text)
    assert 'class="chip issue-link issue-done"' in action_bar
    assert "Issue #1113 · closed" in " ".join(action_bar.split())
    # The other registered issue has no resolved state, so it says nothing.
    assert 'class="chip issue-link"' in action_bar
    assert "Issue #2000" in " ".join(action_bar.split())
    assert "Issue #2000 ·" not in " ".join(action_bar.split())


@pytest.mark.browser
async def test_related_issue_chip_shows_its_state_in_the_action_bar(tmp_path):
    # The same chip, rendered: the state has to be legible in the real action bar
    # at both widths, not merely present in the markup (#1113).
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.add_issue(key, IssueRef("eerovil/agentdeck", 1113))
    state.update_session(
        replace(
            state.sessions[key],
            issue_url="https://github.com/eerovil/agentdeck/issues/1113",
            issue_status="closed",
            issue_status_kind="done",
        )
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport_width in (390, 1100):
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": 844},
                service_workers="block",
            )
            await page.goto(f"{base}/sessions/{key}")
            chip = page.locator("#session-related-links .chip.issue-link.issue-done")
            await chip.wait_for(state="visible")
            assert "closed" in (await chip.inner_text())
            screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
            if screenshot_dir:
                await page.locator("#session-action-bar").screenshot(
                    path=str(
                        Path(screenshot_dir)
                        / f"issue-1113-issue-chip-state-{viewport_width}.png"
                    )
                )
            await page.close()
        await browser.close()


@pytest.mark.browser
async def test_session_detail_uses_responsive_split_view(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    app.state.app_state.update_session(
        replace(
            app.state.app_state.sessions["claude_code:test:sid1"],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    app.state.app_state.add_issue(
        "claude_code:test:sid1", IssueRef("eerovil/agentdeck", 961)
    )
    # The issue puts sid1 in a Work, which the sidebar's chats view leaves out
    # (#2256), so the sidebar has a chat outside every Work to list.
    app.state.app_state.update_session(
        replace(
            app.state.app_state.sessions["claude_code:test:sid1"],
            key="claude_code:test:loose",
            session_id="loose",
            chat_id=None,
            title="A chat outside every Work",
            issues=(),
        )
    )
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="waiting",
                headline="waiting",
            ),
        ),
    )
    app.state.assistant._contexts.update(
        {
            "claude_code:test:sid1": GitContext(
                repository="eerovil/agentdeck",
                branch="feature/deckhand",
                dirty=False,
                pull_requests=(
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=91,
                        title="Add PR context",
                        url="https://github.com/eerovil/agentdeck/pull/91",
                        status="merged",
                    ),
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=92,
                        title="Follow-up PR",
                        url="https://github.com/eerovil/agentdeck/pull/92",
                        status="open",
                        draft=True,
                    ),
                ),
            )
        }
    )
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    assert 'class="session-page session-stack-page"' in response.text
    assert 'class="session-layout"' in response.text
    assert 'class="session-sidebar" aria-label="Works"' in response.text
    assert 'class="session-detail" aria-label="Selected chat"' in response.text
    # The sidebar is only the slot the desktop fills with the Works: no chat list
    # (#2603) and not the old Deckhand attention panel (#2256).
    assert 'id="assistant-panel"' not in response.text
    sidebar = response.text.split('class="session-sidebar"', 1)[1].split("</aside>", 1)[0]
    assert "data-list-empty" in sidebar and "data-session-key" not in sidebar
    assert 'id="assistant-session-details"' in response.text
    # The chat's own Deckhand section is its repository line alone (#2599):
    # opening the chat is what clears its attention, so no card row shows here.
    assert 'class="assistant-chat-detail ' not in response.text
    assert "data-deckhand-row" not in response.text
    assert "feature/deckhand" in response.text
    assert 'class="chip pr-link pr-merged"' in response.text
    assert 'class="chip issue-link"' in response.text
    assert 'href="https://github.com/eerovil/agentdeck/issues/961"' in response.text
    assert "Issue #961" in response.text
    assert 'href="https://github.com/eerovil/agentdeck/pull/91"' in response.text
    assert "PR #91 · merged" in response.text
    assert 'class="chip pr-link pr-open"' in response.text
    assert 'href="https://github.com/eerovil/agentdeck/pull/92"' in response.text
    assert "PR #92 · draft" in response.text
    action_bar = _session_action_bar(response.text)
    detail_meta = response.text.split('class="detail-meta"', 1)[1].split("</div>", 1)[0]
    assert "Issue #961" in action_bar
    assert "PR #91 · merged" in action_bar
    assert "PR #92 · draft" in action_bar
    assert "Deckhand done" not in action_bar  # Done is gone (#2599)
    assert "PR #91" not in detail_meta

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        await page.set_content(html)
        await page.locator("#inject-message").fill("unsent sidebar draft")
        await page.evaluate(
            "document.querySelector('.session-detail').scrollTop = "
            "document.querySelector('.session-detail').scrollHeight"
        )
        desktop = await page.evaluate(
            """() => ({
                sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
                columns: getComputedStyle(document.querySelector('.session-layout'))
                    .gridTemplateColumns,
                detailOverflow: getComputedStyle(document.querySelector('.session-detail'))
                    .overflowY,
                back: getComputedStyle(document.querySelector('.back')).display,
                headerPosition: getComputedStyle(document.querySelector('.session-detail-head'))
                    .position,
                prLinksVisible: [...document.querySelectorAll('.pr-link')].every(link => {
                    const linkRect = link.getBoundingClientRect();
                    const detailRect = document.querySelector('.session-detail')
                        .getBoundingClientRect();
                    return linkRect.top >= detailRect.top && linkRect.bottom <= detailRect.bottom;
                }),
            })"""
        )
        await resized(page, 800, 800)
        mobile = await page.evaluate(
            """() => ({
                sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
                layout: getComputedStyle(document.querySelector('.session-layout')).display,
                detailOverflow: getComputedStyle(document.querySelector('.session-detail'))
                    .overflowY,
                back: getComputedStyle(document.querySelector('.back')).display,
                headerPosition: getComputedStyle(document.querySelector('.session-detail-head'))
                    .position,
            })"""
        )
        await resized(page, 320, 800)
        narrow = await page.evaluate(
            """() => ({
                pageOverflow: document.documentElement.scrollWidth >
                    document.documentElement.clientWidth,
                timestampsVisible: [...document.querySelectorAll('.ev-time')].every(time => {
                    const rect = time.getBoundingClientRect();
                    const parent = time.closest('.ev-head, summary, .tool-call-head')
                        .getBoundingClientRect();
                    return rect.width > 0 && rect.left >= parent.left && rect.right <= parent.right;
                }),
            })"""
        )
        await resized(page, 1200, 800)
        deferred_cards = await page.evaluate(
            """async () => {
                const sidebar = document.querySelector('.session-sidebar');
                sidebar.innerHTML = '<div class="session-list">' +
                    Array.from({length: 100}, (_, index) =>
                        '<a class="session" href="#' + index + '">' +
                        '<span class="card-title">session ' + index + '</span></a>'
                    ).join('') + '</div>';
                sidebar.scrollTop = 0;
                await new Promise(resolve => requestAnimationFrame(() =>
                    requestAnimationFrame(resolve)));
                const cards = sidebar.querySelectorAll('a.session');
                const firstTitle = cards[0].querySelector('.card-title');
                const lastTitle = cards[cards.length - 1].querySelector('.card-title');
                const options = {contentVisibilityAuto: true};
                const initial = {
                    contentVisibility: getComputedStyle(cards[0]).contentVisibility,
                    intrinsicBlockSize: getComputedStyle(cards[0]).containIntrinsicBlockSize,
                    firstVisible: firstTitle.checkVisibility(options),
                    lastVisible: lastTitle.checkVisibility(options),
                };
                cards[cards.length - 1].focus();
                await new Promise(resolve => requestAnimationFrame(() =>
                    requestAnimationFrame(resolve)));
                return {
                    initial,
                    lastVisibleAfterFocus: lastTitle.checkVisibility(options),
                    lastFocused: document.activeElement === cards[cards.length - 1],
                    sidebarScrolled: sidebar.scrollTop > 0,
                };
            }"""
        )
        draft = await page.locator("#inject-message").input_value()
        await browser.close()

    assert desktop["sidebar"] == "block"
    assert len(desktop["columns"].split()) == 2
    assert desktop["detailOverflow"] == "auto"
    assert desktop["back"] == "none"
    # The header is pinned (#1571).
    assert desktop["headerPosition"] == "sticky"
    assert desktop["prLinksVisible"] is True
    assert deferred_cards == {
        "initial": {
            "contentVisibility": "auto",
            "intrinsicBlockSize": "auto 148px",
            "firstVisible": True,
            "lastVisible": False,
        },
        "lastVisibleAfterFocus": True,
        "lastFocused": True,
        "sidebarScrolled": True,
    }
    assert draft == "unsent sidebar draft"
    assert mobile == {
        "sidebar": "block",
        "layout": "block",
        "detailOverflow": "auto",
        "back": "flex",
        "headerPosition": "sticky",
    }
    assert narrow == {"pageOverflow": False, "timestampsVisible": True}


async def test_session_detail_renders_ask_user_question_and_current_answer(tmp_path):
    """End-to-end: an AskUserQuestion line (a tool_use with no text block) is
    written to the transcript file and must survive the JSONL → parse → HTTP
    render path — the choice the agent is waiting on shows on the detail page.
    Regression for the viewer silently dropping it as generic tool noise."""
    import json

    proj = tmp_path / "projects" / "-tmp"
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": "set up the db"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which database should we use?",
                                    "header": "DB",
                                    "options": [{"label": "Postgres", "description": "..."}],
                                }
                            ]
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": (
                            'The user answered: "Which database should we use?"="Postgres". '
                            "Read the answers carefully — they may request clarification, "
                            "changes, or that you not proceed — and follow what they actually say."
                        ),
                    }
                ],
            },
        },
    ]
    (proj / "ask1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:ask1",
            account_key="claude_code:test",
            session_id="ask1",
            status=SessionStatus.LIVE,
            title="DB setup",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:ask1")
    assert r.status_code == 200
    assert "Which database should we use?" in r.text  # the choice question is visible
    assert "ev-question" in r.text  # rendered via the dedicated question block
    assert "Which database should we use?: Postgres" in r.text
    assert "ev-answer" in r.text


async def test_session_detail_unknown_404(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:doesnotexist")
    assert r.status_code == 404


async def test_transcript_load_earlier_partial(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as c:
        r = await c.get("/partials/sessions/claude_code:test:sid1/transcript?before=0")
    assert r.status_code == 200
    assert "an answer here" in r.text


async def test_sse_initial_events(tmp_path):
    """The stream primes the client with the topbar on connect, and no chat list (#2603).

    Driven at the generator level: httpx's ASGITransport buffers the whole
    response body, which never completes for an unbounded SSE stream.
    """
    from agentdeck.web.routes_sse import _stream

    app = _app_with_state(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # An old address asking for the list is not sent one.
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return True

    gen = _stream(FakeRequest(app))
    try:
        events = [event async for event in gen]
    finally:
        await gen.aclose()

    both = "".join(events)
    assert "event: usage" in both
    assert "event: sessions" not in both
    assert "42%" in both  # rendered usage fragment rode the stream
    assert "Hello World Session" not in both


async def test_a_deploy_refusal_turns_the_pill_red_on_an_open_tab(tmp_path, monkeypatch):
    """#747 made the pill red at render time only; an operator watching the deck
    saw a healthy pill while the deploy timer refused every two minutes (#786).
    A `deployment` bus event now pushes the fragment over the live stream."""
    from agentdeck.web.routes_sse import _stream

    state_dir = tmp_path / "deploy-state"
    state_dir.mkdir()
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(state_dir))
    app = _app_with_state(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    gen = _stream(FakeRequest(app))
    try:
        # The usage prime; no old Deckhand panel (#2256) and no chat list (#2603).
        await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        (state_dir / "blocked").write_text(
            "live checkout is dirty and deadbeef fix things is waiting to deploy\n"
        )
        app.state.app_state.deployment_changed()
        pushed = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        # The Work front page's own primes, and the top bar's Needs you list (#3239).
        while pushed.startswith(("event: works", "event: needs_you")):
            pushed = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    assert "event: deployment" in pushed
    assert "deployment-status-blocked" in pushed
    assert "live checkout is dirty" in pushed


async def test_a_chat_running_in_the_live_deploy_checkout_is_badged(tmp_path, monkeypatch):
    """Agents editing the deploy checkout block or damage deploys silently; the
    card says where the chat lives (#786)."""
    repo = tmp_path / "live-repo"
    repo.mkdir()
    monkeypatch.setenv("AGENTDECK_REPO", str(repo))
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid2",
            account_key="claude_code:test",
            session_id="sid2",
            status=SessionStatus.LIVE,
            title="Editing the live checkout",
            cwd=repo,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    async with _client(app) as c:
        r = await c.get("/chats")
    assert r.status_code == 200
    # Exactly the checkout-rooted chat is badged — sid1 has no cwd at all.
    assert r.text.count("live-checkout-badge") == 1


async def test_session_sse_primes_the_topbar_and_deckhand_but_no_list(tmp_path):
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="waiting",
                headline="waiting",
            ),
        ),
    )

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # An old address asking for the sidebar list is not sent one (#2603).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    key = "claude_code:test:sid1"
    gen = _session_stream(FakeRequest(app), key)
    try:
        usage = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        # No old Deckhand attention panel (#2256) and no chat list (#2603) after it.
        assistant_session = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    assert "event: usage" in usage
    assert 'class="mini-acct active" aria-current="true"' in usage
    assert "event: assistant-session" in assistant_session


async def test_idle_session_sse_reuses_presentation_until_state_changes(
    tmp_path, monkeypatch
):
    """An open chat must not rebuild the whole deck while nothing changed (#2865).

    Each wake here is a heartbeat or a usage publication; neither moves a chat, so
    the projection the stream primed with is the only one built.
    """
    from agentdeck import work_counter
    from agentdeck.web import routes_sse
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    session_key = "claude_code:test:sid1"
    app.state.assistant.session_context = lambda _session: None
    monkeypatch.setattr(routes_sse, "HEARTBEAT_S", 0.001)

    class DisconnectAfterWakes:
        def __init__(self, application):
            self.app = application
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            if self.checks == 3:
                state.usage_changed()
            if self.checks == 4:
                state.sessions_changed((session_key,))
            return self.checks > 5

    request = DisconnectAfterWakes(app)
    with work_counter.counting() as spent:
        events = [event async for event in _session_stream(request, session_key)]

    assert request.checks == 6
    assert any("event: status" in event for event in events)
    # The prime's build, and one more for the chat change; the usage wake built none.
    assert spent[work_counter.SESSION_PRESENTATION] == 2


async def test_a_host_sample_alone_does_not_rebuild_the_session_projection(
    tmp_path, monkeypatch
):
    """The host CPU/memory sample lands every three seconds and forever.

    It re-renders the topbar and nothing else, so it must not count as a reason
    to walk the whole deck — while it shared the ``usage`` topic, the reuse
    above never held on any tick of any attached browser (#1310).
    """
    from agentdeck import work_counter
    from agentdeck.host_stats import HostStats
    from agentdeck.web import routes_sse
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    session_key = "claude_code:test:sid1"
    app.state.assistant.session_context = lambda _session: None
    monkeypatch.setattr(routes_sse, "HEARTBEAT_S", 0.001)

    class SampleHostEachWake:
        def __init__(self, application):
            self.app = application
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            state.set_host_stats(
                HostStats(
                    cpu_pct=float(self.checks),
                    memory_pct=10.0,
                    memory_used_bytes=1,
                    memory_total_bytes=10,
                    sampled_at=datetime.now(UTC),
                )
            )
            return self.checks > 4

    request = SampleHostEachWake(app)
    state.session_presentation()
    with work_counter.counting() as spent:
        events = [event async for event in _session_stream(request, session_key)]

    assert spent[work_counter.SESSION_PRESENTATION] == 0
    # The topbar still moves: that is what the sample is for.
    assert any("event: usage" in event for event in events)


async def test_session_page_uses_the_same_render_as_each_live_fragment(
    tmp_path, monkeypatch
):
    from agentdeck.web import routes_pages
    from agentdeck.web.render import SESSION_FRAGMENTS

    app = _app_with_state(tmp_path)
    app.state.config.inject.enabled = True
    captured = {}
    render_fragments = routes_pages.render_session_fragments

    def capture_fragments(templates, view):
        captured["view"] = view
        return render_fragments(templates, view)

    monkeypatch.setattr(routes_pages, "render_session_fragments", capture_fragments)
    parent = app.state.app_state.sessions["claude_code:test:sid1"]
    app.state.app_state.update_session(
        replace(
            parent,
            capabilities=frozenset({*parent.capabilities, Capability.INJECT}),
        )
    )
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:child",
            account_key="claude_code:test",
            session_id="child",
            status=SessionStatus.LIVE,
            thinking=True,
            parent_session_key="claude_code:test:sid1",
        )
    )

    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    assert response.status_code == 200
    view = captured["view"]
    assert view.current.thinking is False
    assert view.busy is True
    target_ids = {
        "status": "session-status",
        "subagents": "subagent-activity",
        "composer-controls": "composer-controls",
        "tools": "tool-activity",
        "related-links": "session-related-links",
        "interaction": "pending-interaction-slot",
    }
    for fragment in SESSION_FRAGMENTS:
        rendered = fragment.render(app.state.templates, view)
        marker = f'id="{target_ids[fragment.name]}"'
        assert marker in response.text, fragment.name
        target_start = response.text.index(marker)
        content_start = response.text.index(">", target_start) + 1
        assert response.text.startswith(rendered, content_start), fragment.name

    status = next(fragment for fragment in SESSION_FRAGMENTS if fragment.name == "status")
    assert "status-tag thinking" in status.render(app.state.templates, view)


def test_live_fragment_signatures_ignore_tool_age_only(tmp_path):
    from agentdeck.web.render import SESSION_FRAGMENTS, SessionView

    app = _app_with_state(tmp_path)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    fragments = {fragment.name: fragment for fragment in SESSION_FRAGMENTS}
    assert tuple(fragments) == (
        "status",
        "subagents",
        "composer-controls",
        "tools",
        "related-links",
        "interaction",
    )

    signatures = {}
    first = SessionView(
        current=session,
        effective=session,
        label="Working",
        age=1,
    )
    later = replace(first, age=99)
    assert fragments["tools"].changed(first, signatures) is True
    assert fragments["tools"].changed(later, signatures) is False
    assert fragments["tools"].render(app.state.templates, first) != fragments[
        "tools"
    ].render(app.state.templates, later)


async def test_session_sse_primes_and_pushes_each_live_fragment(
    tmp_path, monkeypatch
):
    from agentdeck.providers import PROVIDERS
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    parent_key = "claude_code:test:sid1"
    interaction = {"current": None}
    provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(
        provider,
        "pending_interaction",
        lambda account, session: interaction["current"],
    )
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:child",
            account_key="claude_code:test",
            session_id="child",
            status=SessionStatus.LIVE,
            thinking=True,
            parent_session_key=parent_key,
        )
    )

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    gen = _session_stream(FakeRequest(app), parent_key)

    def topic(event):
        return event.split("event: ", 1)[1].split("\n", 1)[0]

    async def next_until(expected, *, limit=12):
        seen = []
        for _ in range(limit):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            seen.append(topic(event))
            if expected.issubset(seen):
                return seen
        pytest.fail(f"missing topics {expected - set(seen)}; saw {seen}")

    try:
        events = [
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            for _ in range(9)
        ]
        topics = [topic(event) for event in events]
        # No old Deckhand attention panel (#2256), no chat list (#2603) and no done
        # bar (#2599) around the chat's own Deckhand section any more,
        # and no pin shelf (#3478).
        assert topics == [
            "usage",
            "assistant-session",
            "held-messages",
            "status",
            "subagents",
            "composer-controls",
            "tools",
            "related-links",
            "needs_you",  # the top bar's list, once per connection (#3239)
        ]
        status, subagents = events[3:5]
        assert "event: status" in status
        assert 'status-tag thinking' in status
        assert "event: subagents" in subagents
        assert "child" in subagents

        app.state.app_state.update_session(
            replace(app.state.app_state.sessions[parent_key], title="Renamed chat")
        )
        updated_status = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        assert topic(updated_status) == "status"
        assert "Renamed chat" in updated_status

        child = app.state.app_state.sessions["claude_code:test:child"]
        app.state.app_state.update_session(
            replace(child, status=SessionStatus.IDLE, thinking=False)
        )
        changed = await next_until({"subagents", "tools"})
        assert changed.index("subagents") < changed.index("tools")

        parent = app.state.app_state.sessions[parent_key]
        app.state.app_state.update_session(
            replace(
                parent,
                capabilities=frozenset({*parent.capabilities, Capability.INTERRUPT}),
            )
        )
        assert "composer-controls" in await next_until({"composer-controls"})

        interaction["current"] = PendingInteraction(
            id="approval-1",
            kind="approval",
            thread_id="sid1",
            turn_id="turn-1",
            title="Allow this command?",
        )
        # A worker's interaction reaches the deck as a change to its chat (#2865).
        app.state.app_state.sessions_changed((parent_key,))
        assert "interaction" in await next_until({"interaction"})
    finally:
        await gen.aclose()


async def test_session_sse_refreshes_related_links_after_live_registration(
    tmp_path, monkeypatch
):
    # An agent registers its issue and PR while its chat page is open (#961).
    # The bottom-of-transcript chips must pick both up over SSE, and only the
    # chips: replacing the Deckhand control or the interaction slot beside them
    # would wipe an in-progress answer.
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    session_key = "claude_code:test:sid1"
    assistant = app.state.assistant
    resolved = {
        "claude_code:test:sid1": GitContext(
            repository="eerovil/agentdeck",
            branch="agent/issue-686",
            dirty=False,
            pull_requests=(
                PullRequestContext(
                    repository="eerovil/agentdeck",
                    number=687,
                    title="Refresh session PR action links",
                    url="https://github.com/eerovil/agentdeck/pull/687",
                    status="open",
                ),
            ),
        )
    }

    async def fake_resolve(sessions, *, force=False):
        return {
            session.key: (
                resolved[session.key]
                if session.pull_requests
                else GitContext(
                    repository="eerovil/agentdeck",
                    branch="agent/issue-686",
                    dirty=False,
                )
            )
            for session in sessions
            if session.key in resolved
        }

    monkeypatch.setattr(assistant.context_resolver, "resolve", fake_resolve)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    gen = _session_stream(FakeRequest(app), session_key)

    def topic(event):
        return event.split("event: ", 1)[1].split("\n", 1)[0]

    async def next_until(expected, *, limit=16):
        seen = []
        for _ in range(limit):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            seen.append((topic(event), event))
            if expected.issubset({name for name, _ in seen}):
                return seen
        pytest.fail(f"missing {expected}; saw {[name for name, _ in seen]}")

    try:
        primed = await next_until({"related-links"})
        empty = next(event for name, event in primed if name == "related-links")
        assert "pr-link" not in empty
        assert "issue-link" not in empty

        app.state.app_state.add_issue(
            session_key,
            IssueRef(repository="eerovil/agentdeck", number=961),
        )
        issue_seen = await next_until({"related-links"})
        issue_chips = [
            event for name, event in issue_seen if name == "related-links"
        ][-1]
        assert 'href="https://github.com/eerovil/agentdeck/issues/961"' in issue_chips
        assert "Issue #961" in issue_chips

        app.state.app_state.add_pull_request(
            session_key,
            PullRequestRef(repository="eerovil/agentdeck", number=687),
        )
        seen = await next_until({"related-links"})
        chips = [event for name, event in seen if name == "related-links"][-1]
        assert "Issue #961" in chips
        assert 'href="https://github.com/eerovil/agentdeck/pull/687"' in chips
        assert "PR #687 · open" in chips
        assert "assistant-session" not in {name for name, _ in seen}
        assert "interaction" not in {name for name, _ in seen}
    finally:
        await gen.aclose()


async def test_session_sse_repairs_registration_between_page_paint_and_connect(
    tmp_path, monkeypatch
):
    """#686's repair still lands — but behind the stream, never in front of it.

    The reconcile used to be awaited before the first fragment, so a chat whose
    context was not cached held the whole prime behind `git status`/`gh pr view`
    (#766). Here the resolver is held open: the stream must still prime, and the
    chips must arrive on the first tick after the resolve completes.
    """
    from agentdeck.web.routes_sse import _session_stream

    # No tick to shorten (#2865): the context landing wakes the chat's page itself.

    app = _app_with_state(tmp_path)
    session_key = "claude_code:test:sid1"
    assistant = app.state.assistant
    assistant._contexts.update(
        {
            session_key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-686",
                dirty=False,
            )
        }
    )
    app.state.app_state.add_pull_request(
        session_key,
        PullRequestRef(repository="eerovil/agentdeck", number=687),
    )

    resolving = asyncio.Event()
    release = asyncio.Event()

    async def fake_resolve(sessions, *, force=False):
        assert [session.key for session in sessions] == [session_key]
        resolving.set()
        await release.wait()
        return {
            session_key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-686",
                dirty=False,
                pull_requests=(
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=687,
                        title="Refresh session PR action links",
                        url="https://github.com/eerovil/agentdeck/pull/687",
                        status="open",
                    ),
                ),
            )
        }

    monkeypatch.setattr(assistant.context_resolver, "resolve", fake_resolve)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    gen = _session_stream(FakeRequest(app), session_key)
    try:
        # The stream primes while the repair is still running: no fragment waits
        # on the resolver, and the first chips render from what was cached.
        primed = None
        for _ in range(16):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if "event: related-links" in event:
                primed = event
                break
        assert primed is not None, "stream did not prime the related-links fragment"
        assert "pr-link" not in primed
        await asyncio.wait_for(resolving.wait(), timeout=5.0)

        release.set()
        for _ in range(16):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if "event: related-links" in event:
                assert 'href="https://github.com/eerovil/agentdeck/pull/687"' in event
                break
        else:
            pytest.fail("stream never published the repaired registration")
    finally:
        release.set()
        await gen.aclose()


async def test_dead_subagents_drop_from_the_session_card(tmp_path):
    # A finished Task/Agent subagent (its transcript went quiet, so `thinking`
    # aged out) is dead weight: it must stop padding the parent's sub-agent count
    # and nested rows. Only live subagents nest; when none are live, no pill.
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    parent_key = "claude_code:test:sid1"

    def _subagent(sid, *, thinking, title):
        return Session(
            key=f"claude_code:test:{sid}",
            account_key="claude_code:test",
            session_id=sid,
            status=SessionStatus.LIVE if thinking else SessionStatus.IDLE,
            thinking=thinking,
            kind="subagent",
            title=title,
            is_delegated=not thinking,
            parent_session_key=parent_key,
            show_when_idle=True,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )

    state.update_session(_subagent("sub-live", thinking=True, title="Live subagent"))
    state.update_session(_subagent("sub-done", thinking=False, title="Finished subagent"))

    async with _client(app) as client:
        r = await client.get("/chats")
    # Only the live subagent counts, and the finished one is gone entirely.
    assert "1 sub-agent" in r.text
    assert "2 sub-agent" not in r.text  # never the stale count that includes the dead one
    assert "Live subagent" in r.text
    assert "Finished subagent" not in r.text

    # When the last live subagent finishes, the pill disappears completely.
    state.update_session(_subagent("sub-live", thinking=False, title="Live subagent"))
    async with _client(app) as client:
        r2 = await client.get("/chats")
    assert "1 sub-agent" not in r2.text  # no count pill at all
    assert "Live subagent" not in r2.text  # and its row is gone too


@pytest.mark.browser
async def test_model_picker_filters_options_by_account_provider(tmp_path):
    """model_picker.js keeps the model <select> in sync with the chosen account's
    provider, at desktop and the narrowest supported mobile width. The app fixture
    only wires a Codex account, so drive the JS directly against a two-provider
    form that mirrors the dashboard markup."""
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    picker = (static_dir / "model_picker.js").read_text()
    html = f"""<!doctype html><html><head><style>{css}</style></head>
      <body><div class="new-chat" open><form data-model-picker>
        <select id="new-chat-account" name="account_key" data-model-picker-account>
          <option value="codex:a" data-provider="codex"
                  data-default-model="Sol" data-default-effort="Medium">Codex A</option>
          <option value="claude:b" data-provider="claude_code"
                  data-default-model="Opus 5" data-default-effort="High">Claude B</option>
        </select>
        <div class="model-field" id="new-chat-model-field" hidden data-model-picker-field>
          <label for="new-chat-model">Model</label>
          <select id="new-chat-model" name="model" data-model-picker-select>
            <option value="">Default (account)</option>
            <option value="gpt-5.6-luna" data-provider="codex">Luna</option>
            <option value="gpt-5.6-sol" data-provider="codex">Sol</option>
            <option value="opus" data-provider="claude_code">Opus 5</option>
            <option value="haiku" data-provider="claude_code">Haiku</option>
          </select>
        </div>
        <div id="new-chat-effort-field" hidden data-effort-picker-field>
          <select id="new-chat-effort" data-effort-picker-select>
            <option value="">Default (account)</option>
            <option value="low" data-provider="codex">Low</option>
            <option value="medium" data-provider="codex">Medium</option>
            <option value="high" data-provider="claude_code">High</option>
            <option value="xhigh" data-provider="claude_code">Extra high</option>
          </select>
        </div>
      </form></div>
      <div class="continue-chat"><form data-model-picker>
        <select id="continue-chat-account" data-model-picker-account>
          <option value="claude:b" data-provider="claude_code"
                  data-default-model="Opus 5" data-default-effort="High">Claude B</option>
          <option value="codex:a" data-provider="codex"
                  data-default-model="Sol" data-default-effort="Medium">Codex A</option>
        </select>
        <div id="continue-chat-model-field" hidden data-model-picker-field>
          <select id="continue-chat-model" data-model-picker-select>
            <option value="">Default (account)</option>
            <option value="gpt-5.6-luna" data-provider="codex">Luna</option>
            <option value="opus" data-provider="claude_code">Opus 5</option>
          </select>
        </div>
        <div id="continue-chat-effort-field" hidden data-effort-picker-field>
          <select id="continue-chat-effort" data-effort-picker-select>
            <option value="">Default (account)</option>
            <option value="medium" data-provider="codex">Medium</option>
            <option value="xhigh" data-provider="claude_code">Extra high</option>
          </select>
        </div>
      </form></div></body></html>"""

    def options(page):
        return page.evaluate(
            """() => {
              const sel = document.getElementById('new-chat-model');
              const field = document.getElementById('new-chat-model-field');
              return {
                values: Array.from(sel.options).map(o => o.value),
                defaultLabel: sel.options[0].textContent,
                effortDefaultLabel: document
                  .getElementById('new-chat-effort').options[0].textContent,
                selected: sel.value,
                hidden: field.hidden,
              };
            }"""
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=picker)

        # Codex selected first: only its models (plus Default) are offered.
        first = await options(page)
        assert first["values"] == ["", "gpt-5.6-luna", "gpt-5.6-sol"]
        assert first["defaultLabel"] == "Default (Sol)"
        assert first["effortDefaultLabel"] == "Default (Medium)"
        assert first["hidden"] is False

        # Pick a Codex model, then switch to the Claude account: the stale Codex
        # slug is dropped and selection falls back to Default.
        await page.select_option("#new-chat-model", "gpt-5.6-sol")
        await page.select_option("#new-chat-account", "claude:b")
        after = await options(page)
        assert after["values"] == ["", "opus", "haiku"]
        assert after["defaultLabel"] == "Default (Opus 5)"
        assert after["effortDefaultLabel"] == "Default (High)"
        assert after["selected"] == ""
        assert after["hidden"] is False

        # Behaviour holds at desktop width too.
        await resized(page, 900, 800)
        await page.select_option("#new-chat-account", "codex:a")
        desktop = await options(page)
        assert desktop["values"] == ["", "gpt-5.6-luna", "gpt-5.6-sol"]

        # A second continuation form is initialized independently.
        assert await page.locator("#continue-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "opus"]
        assert await page.locator("#continue-chat-effort option").first.text_content() == (
            "Default (High)"
        )
        await page.select_option("#continue-chat-account", "codex:a")
        assert await page.locator("#continue-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "gpt-5.6-luna"]
        assert await page.locator("#continue-chat-effort option").first.text_content() == (
            "Default (Medium)"
        )
        await browser.close()


@pytest.mark.browser
async def test_account_buttons_drive_the_picker_and_the_read_only_line(tmp_path):
    """#1912. The new-chat form's account control is a radio group, not a
    <select>, and the collapsed "Edit advanced" line reads out what the chat
    would actually use — so hiding the two selects hides no information."""
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    picker = (static_dir / "model_picker.js").read_text()
    html = f"""<!doctype html><html><head><style>{css}</style></head>
      <body><details class="new-chat" open><form data-model-picker>
        <div class="account-picker" id="new-chat-account" role="radiogroup"
             data-model-picker-account>
          <label class="account-choice" for="acct-1">
            <input type="radio" id="acct-1" name="account_key" value="codex:a" checked
                   data-provider="codex" data-default-model="Sol"
                   data-default-effort="Medium">
            <span class="account-choice-label">main</span>
          </label>
          <label class="account-choice" for="acct-2">
            <input type="radio" id="acct-2" name="account_key" value="claude:b"
                   data-provider="claude_code" data-default-model="Opus 5"
                   data-default-effort="High">
            <span class="account-choice-label">alt</span>
          </label>
        </div>
        <details class="advanced-settings">
          <summary><span>Edit advanced</span>
            <span class="advanced-summary" data-advanced-summary>Sol · Medium</span>
          </summary>
          <div class="model-field" id="new-chat-model-field" data-model-picker-field>
            <select id="new-chat-model" name="model" data-model-picker-select>
              <option value="">Default (Sol)</option>
              <option value="gpt-5.6-luna" data-provider="codex">Luna</option>
              <option value="opus" data-provider="claude_code">Opus 5</option>
            </select>
          </div>
          <div class="model-field" id="new-chat-effort-field" data-effort-picker-field>
            <select id="new-chat-effort" name="effort" data-effort-picker-select>
              <option value="">Default (Medium)</option>
              <option value="low" data-provider="codex">Low</option>
              <option value="xhigh" data-provider="claude_code">Extra high</option>
            </select>
          </div>
        </details>
      </form></details></body></html>"""

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=picker)

        summary = page.locator("[data-advanced-summary]")
        assert await summary.text_content() == "Sol · Medium"
        assert await page.locator("#new-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "gpt-5.6-luna"]

        # Tapping the other account button rebuilds the selects for its provider
        # and moves the read-only line with them.
        await page.locator('label[for="acct-2"]').click()
        assert await page.locator("#new-chat-account input:checked").get_attribute(
            "value"
        ) == "claude:b"
        assert await page.locator("#new-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "opus"]
        assert await summary.text_content() == "Opus 5 · High"

        # The selects really are hidden until the disclosure is opened.
        assert not await page.locator("#new-chat-effort").is_visible()
        await page.locator(".advanced-settings > summary").click()
        # A deliberate choice shows through the collapsed disclosure afterwards.
        await page.select_option("#new-chat-effort", "xhigh")
        assert await summary.text_content() == "Opus 5 · Extra high"

        # The buttons fit the narrowest supported phone without a sideways scroll.
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        await browser.close()


@pytest.mark.browser
async def test_choosing_an_account_leaves_the_chat_page_where_it_was(tmp_path, monkeypatch):
    """#1985: the account radios are parked off screen so they stay focusable,
    and their containing block has to be the form. Left to the page, they were
    placed at the coordinates the sidebar's own content gave them — far below the
    fold, because New chat sits under Deckhand and the board strip — which
    stretched the document that far. Choosing an account then focused a radio the
    browser thought was off screen and scrolled the whole page down to reach it,
    and `body.session-page` is `overflow: hidden`, so the operator could not
    scroll back."""
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda _account: True
    )
    monkeypatch.setattr(PROVIDERS["codex"], "can_start_session", lambda _account: True)
    app = _app_with_state(
        tmp_path,
        with_transcript=True,
        extra_accounts=[
            AccountConfig(provider="codex", label="codex", config_dir=str(tmp_path / "cx"))
        ],
    )
    app.state.config.inject.enabled = True

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # Closed in a finally: an exception in here would otherwise leave
        # Chromium open and hang teardown instead of reporting the failure.
        try:
            # Wide enough for the desktop chat page, where the sidebar is its own
            # scroll pane and the page itself cannot scroll at all.
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            await _open_chat_from_chats_view(page, base, "claude_code:test:sid1")
            await page.locator(".new-chat > summary").click()
            # The picker has to be on screen before its position means anything: a
            # collapsed disclosure measures as a zero-height box at the top of the
            # page, and the scroll below would aim above the pane and land at zero.
            await page.locator("#new-chat-account").wait_for(state="visible")
            # Deckhand and the board strip sit above New chat on a real deck, which
            # is what pushes the form this far down its own scroll pane — and that
            # depth is the whole reproduction. Nothing below asks the pane to
            # scroll: how far a browser will scroll a pane, and how much it nudges
            # one to reveal a focused control, differ between this host and the pod
            # runner, and neither is what this test is about.
            form_is_deep = await page.evaluate(
                """() => {
                  const side = document.querySelector('.session-sidebar');
                  const spacer = document.createElement('div');
                  spacer.style.height = '2000px';
                  side.insertBefore(spacer, side.firstChild);
                  const picker = document.querySelector('#new-chat-account');
                  return picker.getBoundingClientRect().top
                    - side.getBoundingClientRect().top + side.scrollTop > 1000;
                }"""
            )
            # Everything is measured before anything is asserted: a failing assert
            # inside the browser block would leave Chromium open and hang teardown
            # rather than report the defect.
            # The off-screen radio travels with the button it belongs to…
            radio_rides_along = await page.evaluate(
                """() => {
                  const label = document.querySelector('.new-chat .account-choice');
                  const radio = label.querySelector('input');
                  const l = label.getBoundingClientRect();
                  const r = radio.getBoundingClientRect();
                  return r.top >= l.top - 1 && r.bottom <= l.bottom + 1;
                }"""
            )
            # …so it stretches nothing: this page is exactly one viewport tall.
            page_height = await page.evaluate(
                "[document.documentElement.scrollHeight, window.innerHeight]"
            )

            # And choosing the codex account leaves the page where it was. Focusing
            # the radio is what moved it: the browser scrolls to reveal a control
            # it believes is off screen, and here there is nothing off screen to
            # reveal.
            await page.locator(".new-chat .account-choice", has_text="codex").click()
            chose_codex = await page.evaluate(
                "(document.querySelector('.new-chat [name=account_key]:checked') || {})"
                ".getAttribute && document.querySelector("
                "'.new-chat [name=account_key]:checked').getAttribute('data-provider')"
            )
            page_moved = await page.evaluate("window.scrollY")
        finally:
            await browser.close()

    assert form_is_deep
    assert radio_rides_along
    assert page_height[0] <= page_height[1]
    assert chose_codex == "codex"
    assert page_moved == 0


@pytest.mark.browser
async def test_new_chat_meters_follow_the_live_usage_header(tmp_path, monkeypatch):
    """#2045: the New chat tiles used to freeze at the numbers the page render
    gave them while the sticky header moved on with every usage push, so a phone
    left open across a window reset showed one account's meter twice, with two
    wildly different answers. The tiles now copy the header's figures — without
    the control itself being swapped, which would drop the selection and the
    half-typed first message."""
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda _account: True
    )
    app = _app_with_state(tmp_path)
    app.state.config.inject.enabled = True

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(
                viewport={"width": 390, "height": 800}, service_workers="block"
            )
            await page.goto(base + "/chats")
            await page.wait_for_function("window.htmx")
            await page.locator(".new-chat > summary").click()

            tile = page.locator(
                '.new-chat .account-choice-usage[data-account-key="claude_code:test"]'
            )
            five_hour = tile.locator(
                '.account-choice-window[data-window-kind="5h"] b'
            )
            seven_day = tile.locator(
                '.account-choice-window[data-window-kind="7d"] b'
            )
            # The page render already agrees with the header; the drift only
            # starts once usage moves under an open page.
            await five_hour.filter(has_text="42%").wait_for()

            # The operator is mid-compose when the next poll lands.
            await page.locator("#new-chat-message").fill("look at the poller logs")

            app.state.app_state.set_usage(
                UsageSnapshot(
                    account_key="claude_code:test",
                    five_hour_pct=5.0,
                    five_hour_resets_at=None,
                    seven_day_pct=95.0,
                    seven_day_resets_at=None,
                    fetched_at=datetime.now(UTC),
                )
            )

            # The tile follows the header rather than staying at 42%/7%…
            await five_hour.filter(has_text="5%").wait_for()
            assert await seven_day.text_content() == "95%"
            # …colouring included: 95% of a window is the crit band.
            assert "crit" in (await seven_day.get_attribute("class") or "")
            header_windows = await page.locator(
                '#limit-bars .mini-acct[data-account-key="claude_code:test"] .mini-pct'
            ).all_text_contents()
            assert header_windows == ["5%", "95%"]

            # And nothing the operator was doing was disturbed.
            assert await page.locator("#new-chat-message").input_value() == (
                "look at the poller logs"
            )
            assert await page.locator(
                ".new-chat [name=account_key]:checked"
            ).get_attribute("value") == "claude_code:test"
        finally:
            await browser.close()


async def test_account_page_saves_and_applies_new_chat_defaults(tmp_path, monkeypatch):
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda _account: True
    )
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    app.state.app_state.update_session(
        replace(
            app.state.app_state.sessions["claude_code:test:sid1"],
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    starts = []

    async def start_new(account, provider, cwd, message, images=None, **kwargs):
        starts.append({"account": account.key, "cwd": cwd, **kwargs})
        return InjectResult(True)

    app.state.injector.start_new = start_new

    async with _client(app) as client:
        account_page = await client.get("/accounts/claude_code%3Atest")
        assert account_page.status_code == 200
        assert "New chat defaults" in account_page.text
        assert "Usage limits" in account_page.text

        saved = await client.post(
            "/accounts/claude_code%3Atest/new-chat-defaults",
            data={"model": "fable", "effort": "high"},
        )
        assert saved.status_code == 200
        assert "Saved for New chat" in saved.text
        assert 'value="fable" selected' in saved.text
        assert 'value="high" selected' in saved.text

        dashboard = await client.get("/chats")
        assert 'data-default-model="Fable 5.1"' in dashboard.text
        assert 'data-default-effort="High"' in dashboard.text

        created = await client.post(
            "/sessions/new",
            data={
                "account_key": "claude_code:test",
                "cwd": str(tmp_path),
                "message": "Start with saved defaults",
                "model": "",
                "effort": "",
            },
        )
        continued = await client.post(
            "/sessions/claude_code%3Atest%3Asid1/continue",
            data={"account_key": "claude_code:test", "model": "", "effort": ""},
        )

        invalid_model = await client.post(
            "/accounts/claude_code%3Atest/new-chat-defaults",
            data={"model": "unknown", "effort": "high"},
        )
        invalid_effort = await client.post(
            "/accounts/claude_code%3Atest/new-chat-defaults",
            data={"model": "fable", "effort": "impossible"},
        )

    assert created.status_code == 202
    assert continued.status_code == 202
    assert [(call["model"], call["effort"]) for call in starts] == [
        ("fable", "high"),
        ("fable", "high"),
    ]
    assert invalid_model.status_code == 422
    assert invalid_effort.status_code == 422


async def test_account_page_plans_a_usage_reset_and_marks_the_week_bar(tmp_path):
    """#3379: a planned reset is set on the account page or the API, and the week bar says so."""
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=10.0,
            five_hour_resets_at=now + timedelta(hours=2),
            # Far ahead of pace: flagged until a reset is planned.
            seven_day_pct=90.0,
            seven_day_resets_at=now + timedelta(days=6),
            fetched_at=now,
        )
    )
    path = "/accounts/claude_code%3Atest/planned-reset"

    async with _client(app) as client:
        before = await client.get("/accounts/claude_code%3Atest")
        bad = await client.post(path, data={"at": "25:99"})
        saved = await client.post(path, data={"at": "23:59"})
        page = await client.get("/accounts/claude_code%3Atest")
        read = await client.get(f"/api{path}")
        cleared = await client.delete(f"/api{path}")
        refused = await client.put(f"/api{path}", json={"at": "soon"})
        unknown = await client.put(
            "/api/accounts/claude_code%3Anope/planned-reset", json={"at": "12:00"}
        )

    assert "⚠" in before.text and "↺" not in before.text
    assert "HH:MM" in bad.text
    assert "Saved" in saved.text and "Clear" in saved.text
    # Enter in the time field presses the first submit button, which must not be Clear.
    assert saved.text.index(">Save</button>") < saved.text.index(">Clear</button>")
    assert "↺" in page.text and "⚠" not in page.text
    assert read.json()["account"] == "claude_code:test"
    assert datetime.fromisoformat(read.json()["at"]) > now
    assert cleared.json() == {"account": "claude_code:test", "at": None}
    assert refused.status_code == 400
    assert unknown.status_code == 404


async def test_account_index_is_always_reachable_and_lists_configured_accounts(tmp_path):
    app = _app_with_metered_accounts(tmp_path, ["main", "alt", "codex"])

    async with _client(app) as client:
        dashboard = await client.get("/chats")
        settings = await client.get("/settings")
        index = await client.get("/accounts")

    assert dashboard.status_code == 200
    # Reached from Settings and each usage bar since the top bar dropped its icon, along
    # with the New issue link (#3239).
    assert 'class="account-nav-link"' not in dashboard.text
    assert "issues/new?template" not in dashboard.text
    assert '<a href="/accounts">Manage accounts' in settings.text
    assert index.status_code == 200
    assert index.text.count('class="account-card"') == 3
    for label in ("main", "alt", "codex"):
        assert f'href="/accounts/claude_code%3A{label}"' in index.text
    # Each card's usage figures follow the page's stream (#3228), as does an account's page.
    assert 'sse-connect="/events?view=accounts"' in index.text
    assert 'sse-swap="account-usage-claude_code:main"' in index.text
    from test_change_budget import _EventsRequest, _until

    from agentdeck.web import routes_sse

    stream = routes_sse._stream(_EventsRequest(app, view="accounts"))
    # One push with the top bar's meters: theirs, then each card's figures.
    pushed = await _until(stream, "event: usage")
    assert "event: account-usage-claude_code:codex" in pushed
    assert pushed.count('class="account-card-usage"') == 3
    await stream.aclose()
    stream = routes_sse._stream(_EventsRequest(app, view="account", key="claude_code:alt"))
    pushed = await _until(stream, "event: usage")
    assert "event: account-capacity" in pushed and 'id="account-capacity"' in pushed
    await stream.aclose()


@pytest.mark.parametrize("viewport_width", [320, 1100])
@pytest.mark.browser
async def test_account_index_back_loads_the_dashboard(tmp_path, viewport_width):
    app = _app_with_state(tmp_path, with_transcript=True)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": viewport_width, "height": 800}, service_workers="block"
        )
        await page.goto(base + "/accounts")
        await page.locator("a.back[href='/']").wait_for()
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir and viewport_width == 320:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-659-accounts-mobile.png")
            )
        await page.locator("a.back[href='/']").click()
        await page.wait_for_url(base + "/")
        # The deck, not the accounts page: the Work-first front page, which lists
        # no chats at all (#2219).
        await page.locator("#works-needs").wait_for()
        assert await page.locator("#sessions").count() == 0
        assert await page.locator("#accounts-title").count() == 0
        if screenshot_dir and viewport_width == 320:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-659-dashboard-after-back-mobile.png")
            )
        await browser.close()


@pytest.mark.browser
async def test_account_detail_back_returns_to_its_source_page(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    key = "claude_code:test:sid1"

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # Where "back" lands is the subject and every URL here is compared
        # exactly, so this page declines the harness's `chats=1` (#1525).
        page = await browser.new_page(
            viewport={"width": 320, "height": 800},
            service_workers="block",
        )

        # Settings leads to the index (#3239: the top bar no longer carries an icon).
        await page.goto(base + "/settings")
        await page.locator('a[href="/accounts"]').click()
        await page.wait_for_url(base + "/accounts")
        await page.locator(".account-card").click()
        await page.wait_for_url(base + "/accounts/claude_code%3Atest")
        await page.locator("[data-account-back]").click()
        await page.wait_for_url(base + "/accounts")

        # A direct account shortcut from a selected chat returns to that chat,
        # not to the dashboard or account index.
        await page.goto(base + f"/sessions/{key}")
        await page.locator(".usage-mini").click()
        await page.locator(".acct-label a").click()
        await page.wait_for_url(base + "/accounts/claude_code%3Atest")
        await page.locator("[data-account-back]").click()
        await page.wait_for_url(base + f"/sessions/{key}")

        # A direct entry has no usable source and follows the fallback href.
        direct = await browser.new_page(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        await direct.goto(base + "/accounts/claude_code%3Atest")
        await direct.locator("[data-account-back]").click()
        await direct.wait_for_url(base + "/accounts")
        await browser.close()


@pytest.mark.parametrize("viewport_width", [320, 1100])
@pytest.mark.browser
async def test_account_page_fits_default_controls_at_mobile_and_desktop(
    tmp_path, viewport_width
):
    app = _app_with_state(tmp_path)
    _seed_meters(app)
    app.state.auth_watch._reader = _stub_status(broken=True)
    app.state.auth_watch.refresh(app.state.accounts[0])

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": viewport_width, "height": 800},
            service_workers="block",
        )
        await page.goto(base + "/accounts")
        await page.locator(".account-card").wait_for()
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir)
                    / f"issue-619-navigation-{'mobile' if viewport_width == 320 else 'desktop'}.png"
                ),
                full_page=True,
            )

        await page.goto(base + "/accounts/claude_code%3Atest")
        await page.locator("#account-new-chat-defaults").wait_for()
        assert await page.locator("#account-capacity").is_visible()
        assert await page.locator("#account-login-title").is_visible()
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir)
                    / f"issue-619-{'mobile' if viewport_width == 320 else 'desktop'}.png"
                ),
                full_page=True,
            )
        await browser.close()


@pytest.mark.browser
async def test_session_picker_hydration_and_live_update_keep_layout_stable(
    tmp_path, monkeypatch
):
    """The first selected account's setting rows must exist before first paint.

    Issue #507's trace caught both rows appearing only when the deferred picker
    hydrated, moving the submit button and transcript by 143px. Exercise the
    rendered session page with that script deliberately held past first paint,
    then drive the real HTMX/SSE swap path at mobile and desktop widths.
    """
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda _account: True
    )
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    app.state.app_state.update_session(replace(session, cwd=tmp_path))
    async with _client(app) as client:
        response = await client.get(f"/sessions/{key}")

    assert 'id="continue-chat-model-field"' in response.text
    assert 'id="continue-chat-effort-field"' in response.text
    assert 'id="continue-chat-model-field" hidden' not in response.text
    assert 'id="continue-chat-effort-field" hidden' not in response.text

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    picker = (static_dir / "model_picker.js").read_text()
    page_html = re.sub(
        r'<script src="/static/model_picker\.js\?v=[^"]+" defer></script>',
        "",
        response.text,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = []
        for width in (320, 1200):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()
            await page.add_init_script(
                """
                window.__layoutShifts = [];
                new PerformanceObserver(list => {
                  list.getEntries().forEach(entry => {
                    if (!entry.hadRecentInput) window.__layoutShifts.push(entry.value);
                  });
                }).observe({type: 'layout-shift', buffered: true});
                window.__eventSources = [];
                class FakeEventSource extends EventTarget {
                  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
                  constructor(url) {
                    super(); this.url = url; this.readyState = FakeEventSource.OPEN;
                    window.__eventSources.push(this);
                    queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
                  }
                  close() { this.readyState = FakeEventSource.CLOSED; }
                }
                window.EventSource = FakeEventSource;
                """
            )

            async def serve(route):
                path = route.request.url.split("?", 1)[0].removeprefix(
                    "http://agentdeck.test"
                )
                if path == f"/sessions/{key}":
                    await route.fulfill(
                        status=200, content_type="text/html", body=page_html
                    )
                elif (asset := _static_route_body(static_dir, path)) is not None:
                    await route.fulfill(
                        status=200, content_type=asset[0], body=asset[1]
                    )
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto(f"http://agentdeck.test/sessions/{key}")
            submit = page.locator(".continue-chat button[type=submit]")
            await submit.wait_for(state="visible")
            before = await submit.bounding_box()
            await page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => "
                "requestAnimationFrame(resolve)))"
            )

            # Hydrate only after the server-rendered form has painted. This is
            # the delayed-script order from the captured mobile trace.
            await page.add_script_tag(content=picker)
            await page.wait_for_function(
                "document.querySelector('.continue-chat form').dataset.modelPickerReady"
            )
            await page.wait_for_function("window.__eventSources.length === 1")
            await page.evaluate(
                """() => {
                  const source = window.__eventSources[0];
                  ['status', 'assistant-session'].forEach(name => {
                    const target = name === 'status'
                      ? document.getElementById('session-status')
                      : document.getElementById('assistant-session-details');
                    source.dispatchEvent(new MessageEvent(name, {data: target.innerHTML}));
                  });
                }"""
            )
            await page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => "
                "requestAnimationFrame(resolve)))"
            )
            after = await submit.bounding_box()
            result = await page.evaluate(
                """() => ({
                  shifts: window.__layoutShifts,
                  modelHidden: document.getElementById(
                    'continue-chat-model-field').hidden,
                  effortHidden: document.getElementById(
                    'continue-chat-effort-field').hidden,
                })"""
            )
            result["button_before"] = before["y"]
            result["button_after"] = after["y"]
            results.append(result)
            await context.close()
        await browser.close()

    for result in results:
        assert result["modelHidden"] is False
        assert result["effortHidden"] is False
        assert abs(result["button_after"] - result["button_before"]) < 1
        assert max(result["shifts"], default=0) < 0.1
        assert sum(result["shifts"]) < 0.1


@pytest.mark.browser
async def test_dashboard_input_reports_presence_at_desktop_and_mobile_widths(tmp_path):
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    state.last_operator_input_at = datetime.now(UTC) - timedelta(days=1)

    async with _client(app) as client:
        away = (await client.get("/api/presence")).json()
    assert away["status"] == "away"
    assert away["idle_for_s"] >= 23 * 60 * 60
    assert away["idle_after_s"] == 300

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width, input_kind in ((1200, "keyboard"), (320, "pointer")):
            state.last_operator_input_at = datetime.now(UTC) - timedelta(days=1)
            context = await browser.new_context(
                viewport={"width": width, "height": 800},
                has_touch=input_kind == "pointer",
                service_workers="block",
            )
            page = await context.new_page()
            await page.goto(base_url + "/chats")
            search = page.locator("#q")
            await search.evaluate("input => input.focus()")
            before = await page.evaluate(
                """() => ({
                  active: document.activeElement.id,
                  value: document.querySelector('#q').value,
                  scrollX: scrollX,
                  scrollY: scrollY,
                })"""
            )

            async with page.expect_response(
                lambda response: response.url.endswith("/api/presence")
                and response.request.method == "POST"
            ):
                if input_kind == "keyboard":
                    await page.keyboard.press("a")
                else:
                    box = await search.bounding_box()
                    assert box is not None
                    await page.touchscreen.tap(box["x"] + 4, box["y"] + 4)

            present = await page.evaluate(
                "() => fetch('/api/presence').then(response => response.json())"
            )
            after = await page.evaluate(
                """() => ({
                  active: document.activeElement.id,
                  value: document.querySelector('#q').value,
                  scrollX: scrollX,
                  scrollY: scrollY,
                })"""
            )
            assert present["status"] == "present"
            assert present["idle_for_s"] < 5
            assert after["active"] == before["active"] == "q"
            assert after["scrollX"] == before["scrollX"]
            assert after["scrollY"] == before["scrollY"]
            if input_kind == "keyboard":
                assert after["value"] == before["value"] + "a"
            else:
                assert after["value"] == before["value"]
            await context.close()
        await browser.close()


# --- recovering a Claude account's login from the deck (#298) ---------------


def _stub_status(broken: bool):
    """Stand in for `claude auth status` without shelling out."""
    from agentdeck.providers.claude_code.auth import AuthStatus

    def read(_config_dir, **_kwargs):
        return AuthStatus(
            logged_in=not broken,
            email="eero@example.com" if not broken else None,
            organization="Northwind" if not broken else None,
            checked_at=datetime.now(UTC),
        )

    return read


async def test_a_signed_out_account_is_visible_and_offers_a_way_back_in(tmp_path):
    app = _app_with_state(tmp_path)
    watch = app.state.auth_watch
    watch._reader = _stub_status(broken=True)
    watch.refresh(app.state.accounts[0])

    async with _client(app) as client:
        page = await client.get("/chats")
        account_page = await client.get("/accounts/claude_code%3Atest")

    assert page.status_code == 200
    # The collapsed usage line gains a marker, not a row: the sticky header on a
    # phone must not get taller for this.
    assert 'class="mini-auth crit"' in page.text
    assert "is signed out — delegations to it will fail" not in page.text
    assert 'href="/accounts/claude_code%3Atest"' in page.text
    assert "is signed out — delegations to it will fail" in account_page.text
    assert "/accounts/claude_code%3Atest/auth/login" in account_page.text


async def test_a_healthy_account_says_who_it_is_and_nothing_alarming(tmp_path):
    app = _app_with_state(tmp_path)
    watch = app.state.auth_watch
    watch._reader = _stub_status(broken=False)
    watch.refresh(app.state.accounts[0])

    async with _client(app) as client:
        page = await client.get("/chats")
        account_page = await client.get("/accounts/claude_code%3Atest")

    assert "mini-auth" not in page.text
    assert "Signed in as eero@example.com" not in page.text
    assert "Signed in as eero@example.com" in account_page.text
    assert "Reconnect" not in account_page.text


async def test_the_login_panel_walks_from_link_to_code_to_confirmation(tmp_path):
    app = _app_with_state(tmp_path)
    watch = app.state.auth_watch
    watch._reader = _stub_status(broken=True)
    watch.refresh(app.state.accounts[0])

    class FakeFlow:
        """The flow's states, without a subprocess: the routes are what is under test."""

        def __init__(self):
            self.account_key = "claude_code:test"
            self.state = "awaiting_code"
            self.url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x"
            self.detail = "Open the link, approve, then paste the code it gives you."
            self.status = None
            self.codes: list[str] = []

        @property
        def active(self):
            return self.state in {"starting", "awaiting_code", "verifying"}

        def submit_code(self, code):
            from agentdeck.providers.claude_code.auth import AuthStatus

            self.codes.append(code)
            self.state = "done"
            self.status = AuthStatus(True, email="eero@example.com")

        def cancel(self):
            self.state = "failed"

    flow = FakeFlow()
    watch.start_login = lambda account: watch._flows.setdefault(account.key, flow)

    async with _client(app) as client:
        started = await client.post("/accounts/claude_code%3Atest/auth/login")
        assert started.status_code == 200
        # Rendered as a link the operator can open (escaped in the href, as it must be).
        assert "https://claude.com/cai/oauth/authorize?code=true&amp;client_id=x" in started.text
        assert 'name="code"' in started.text

        finished = await client.post(
            "/accounts/claude_code%3Atest/auth/code", data={"code": "pasted-code"}
        )

    assert finished.status_code == 200
    assert flow.codes == ["pasted-code"]
    assert "Signed in as eero@example.com" in finished.text
    # The code is never echoed back into the page, and nothing is re-sent for the
    # operator: an uncertain delivery has to be repeated on purpose.
    assert "pasted-code" not in finished.text
    assert "has to be sent again on purpose" in finished.text


async def test_a_code_with_no_login_in_progress_is_a_conflict_not_a_crash(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/accounts/claude_code%3Atest/auth/code", data={"code": "x"}
        )
    assert response.status_code == 409


async def test_verifying_asks_again_right_now(tmp_path):
    app = _app_with_state(tmp_path)
    watch = app.state.auth_watch
    watch._reader = _stub_status(broken=True)
    watch.refresh(app.state.accounts[0])
    watch._reader = _stub_status(broken=False)

    async with _client(app) as client:
        response = await client.post("/accounts/claude_code%3Atest/auth/verify")

    assert response.status_code == 200
    assert "Signed in as eero@example.com" in response.text


async def test_only_a_claude_account_can_be_signed_in_here(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        unknown = await client.post("/accounts/nope%3Athere/auth/login")
    assert unknown.status_code == 404


async def test_a_turn_that_failed_to_authenticate_is_not_shown_as_an_answer(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            last_role="agent",
            last_text="Failed to authenticate: OAuth session expired and could not be refreshed",
        )
    )

    async with _client(app) as client:
        page = await client.get("/chats")

    card = _session_card(page.text, "claude_code:test:sid1")
    assert "login expired" in card


async def test_the_auth_panel_is_reachable_by_get(tmp_path):
    """The local-file catch-all answers every unmatched GET, so a route registered
    after it is unreachable — a 404 that looks like a missing account (#298)."""
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get("/accounts/claude_code%3Atest/auth")

    assert response.status_code == 200
    assert "acct-auth" in response.text
    assert "local file not found" not in response.text


@pytest.mark.asyncio
async def test_settings_is_reachable_from_the_shared_topbar(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        for path in ("/", "/sessions/claude_code:test:sid1"):
            page = await client.get(path)
            assert 'href="/settings"' in page.text, path
        # The gear marks itself current on its own page, and the account
        # shortcut it sits beside is untouched.
        settings = await client.get("/settings")
        assert 'href="/settings"' in settings.text
        assert 'aria-current="page"' in settings.text
        assert 'href="/accounts"' in settings.text




@pytest.mark.browser
async def test_settings_say_the_phone_is_told_only_about_needs_more_info(tmp_path):
    """The finished-notification choice is gone (#2886); the page says what pushes."""
    app = _app_with_state(tmp_path)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        for width in (320, 1100):
            page = await browser.new_page(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            await page.goto(base + "/settings")
            section = page.locator('section[aria-labelledby="notification-mode-title"]')
            await section.get_by_text(
                "Your phone is notified only when a card moves to Needs More Info."
            ).wait_for()
            assert await page.locator('input[name="finished_mode"]').count() == 0
            box = await section.bounding_box()
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width
            if screenshot_dir:
                await section.screenshot(
                    path=str(Path(screenshot_dir) / f"issue-2886-notifications-{width}.png")
                )
            await page.close()
        await browser.close()


@pytest.mark.browser
async def test_shared_brand_link_opens_the_front_page(tmp_path):
    app = _app_with_state(tmp_path)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1100, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.goto(base + "/settings")
            brand = page.get_by_role("link", name="AgentDeck front page")
            assert await brand.get_attribute("href") == "/"

            muted = await brand.evaluate("link => getComputedStyle(link).color")
            await brand.hover()
            assert await brand.evaluate("link => getComputedStyle(link).color") != muted
            if screenshot_dir:
                await page.screenshot(
                    path=str(
                        Path(screenshot_dir) / f"issue-996-brand-link-{width}.png"
                    )
                )

            await brand.click()
            await page.wait_for_url(base + "/")
            # The Work-first front page (#2219).
            assert await page.locator("#works-needs").count() == 1
            await page.close()
        await browser.close()


@pytest.mark.asyncio
async def test_pollers_is_reachable_from_settings(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        settings = await client.get("/settings")

    assert 'href="/pollers"' in settings.text


def test_service_worker_precaches_every_shell_asset():
    """`sw.js` ASSETS must not drift from what `base.html` actually loads (#792).

    The list is hand-maintained, and nothing used to tie the two together: a
    script added to the shell was silently left out of the precache, and a
    script deleted from disk stayed in the list as a permanent install-time
    404. Every `<script src>` and stylesheet in `base.html` must be listed,
    and everything listed must exist.
    """
    import agentdeck.web as web_pkg

    web_dir = Path(web_pkg.__file__).parent
    base_html = (web_dir / "templates" / "base.html").read_text()
    sw = (web_dir / "static" / "sw.js").read_text()
    block = re.search(r"const ASSETS = \[(.*?)\];", sw, re.S)
    assert block, "sw.js no longer declares an ASSETS list"
    assets = set(re.findall(r"'([^']+)'", block.group(1)))

    loaded = set(re.findall(r'<script src="(/static/[^"?]+)', base_html))
    loaded |= set(re.findall(r'<link rel="stylesheet" href="(/static/[^"?]+)', base_html))
    missing = sorted(loaded - assets)
    assert not missing, f"loaded by base.html but not precached by sw.js: {missing}"

    static_dir = web_dir / "static"
    rotten = sorted(
        asset
        for asset in assets
        if asset.startswith("/static/")
        and not (static_dir / asset.removeprefix("/static/")).is_file()
    )
    assert not rotten, f"precached by sw.js but gone from disk: {rotten}"


def _composer_session_app(tmp_path):
    """A session page that renders the message composer."""
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.config.inject.enabled = True
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.update_session(
        replace(
            state.sessions[key],
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_a_service_worker_claim_waits_for_a_composer_draft(tmp_path):
    """The first predicate in ``workInProgress()``, driven by a real claim (#792).

    ``composingMessage()`` was the only guard with no replacement claim over
    it — the panel/model/filter tests cover the other three — so dropping or
    reordering the draft check would have gone uncaught. The field is blurred
    first, so the draft itself is what holds the reload back rather than the
    caret sitting in it.
    """
    app = _composer_session_app(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 390, 844)
        page = await _settled_worker_page(
            context, f"{base}/sessions/claude_code:test:sid1"
        )

        field = page.locator("#inject-message")
        await field.fill("half a thought")
        await field.evaluate("element => element.blur()")

        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        await page.wait_for_function("() => window.__claims === 1", timeout=20000)
        # Past the handler's own 5s re-check, so the guard holds on the retry too.
        await page.wait_for_timeout(7000)
        assert await page.evaluate("() => window.__preReload") is True
        assert await field.input_value() == "half a thought"

        # Deferred, not dropped: emptying the composer releases the reload.
        await field.fill("")
        await field.evaluate("element => element.blur()")
        await page.wait_for_function(
            "() => window.__preReload === undefined", timeout=20000
        )

        await browser.close()


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_a_service_worker_claim_waits_for_a_reader_scrolled_up(tmp_path):
    """Deliberate transcript scroll is work in progress (#792).

    A reload re-follows the transcript to the bottom, so landing one under
    someone reading history loses their place. Scrolling back to the end is
    what releases the deferred reload.
    """
    import json

    app = _composer_session_app(tmp_path)
    # Enough transcript that the page genuinely scrolls at this viewport.
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    extra = [
        {
            "type": "user",
            "timestamp": f"2026-07-15T08:{i:02d}:00Z",
            "message": {"role": "user", "content": f"filler question number {i}"},
        }
        for i in range(40)
    ]
    with transcript.open("a") as handle:
        for line in extra:
            handle.write(json.dumps(line) + "\n")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 390, 844)
        page = await _settled_worker_page(
            context, f"{base}/sessions/claude_code:test:sid1"
        )

        # The same scroll-root rule as base.html's readingHistory().
        scroll_root = """() => {
            const detail = document.querySelector('.session-detail');
            return detail && getComputedStyle(detail).overflowY === 'auto'
                ? detail : document.documentElement;
        }"""
        await page.wait_for_function(
            f"() => {{ const root = ({scroll_root})();"
            " return root.scrollHeight > root.clientHeight + 200; }"
        )
        await page.evaluate(
            f"() => {{ ({scroll_root})().scrollTo(0, 0); window.scrollTo(0, 0); }}"
        )

        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        await page.wait_for_function("() => window.__claims === 1", timeout=20000)
        # Past the handler's own 5s re-check, so the guard holds on the retry too.
        await page.wait_for_timeout(7000)
        assert await page.evaluate("() => window.__preReload") is True

        # Returning to the end is done reading: the deferred reload lands.
        await page.evaluate(
            f"() => {{ const root = ({scroll_root})();"
            " root.scrollTo(0, root.scrollHeight);"
            " window.scrollTo(0, document.documentElement.scrollHeight); }"
        )
        await page.wait_for_function(
            "() => window.__preReload === undefined", timeout=20000
        )

        await browser.close()


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_an_empty_focused_composer_does_not_hold_the_reload(tmp_path):
    """The composer's post-send caret must not defer the update forever (#792).

    ``clearComposer(..., keepFocus)`` re-focuses the emptied message box after
    every send, so treating that caret as work in progress held a pending
    reload off for the page's whole life. Empty means nothing to lose: the
    reload lands with focus still sitting in the composer.
    """
    app = _composer_session_app(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await _worker_claim_context(browser, 390, 844)
        page = await _settled_worker_page(
            context, f"{base}/sessions/claude_code:test:sid1"
        )

        await page.locator("#inject-message").focus()
        await page.evaluate("() => navigator.serviceWorker.register('/sw.js?deploy=2')")
        await page.wait_for_function(
            "() => window.__preReload === undefined", timeout=20000
        )

        await browser.close()


async def test_a_chat_that_crossed_providers_names_the_provider_it_is_on_now(tmp_path):
    """``main`` on two providers is two accounts; the label alone would lie."""
    from agentdeck.web.render import account_chain_label

    labels = {"claude_code:main": "main", "codex:main": "main", "codex:alt": "alt"}
    # The badge that carries the whole chain resolves the collision.
    assert account_chain_label(("claude_code:main", "codex:main"), labels) == "claude▸codex"
    assert account_chain_label(("claude_code:main", "codex:alt"), labels) == "main▸alt"
    assert account_chain_label(("codex:alt",), labels) == "alt"


@pytest.mark.browser
async def test_a_failed_turn_paints_a_distinct_dot_from_an_idle_chat(tmp_path):
    """#788 (2): ``turn_failed`` reached triage and the API but never the UI.

    ``display_state`` fell through to ``LIVE -> "idle"``, so a chat whose turn
    died painted the same dot as a healthy resting chat and the only signal was
    an Attention card the operator had to open — the gap #778 introduced the
    field to close. Asserted on the *computed* colour, because a class name that
    no stylesheet rule matches would still "pass" a markup check.
    """
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    healthy = "claude_code:test:sid1"
    failed = "claude_code:test:sid2"
    state.update_session(
        replace(state.sessions[healthy], key=failed, session_id="sid2",
                title="Died Mid Turn", turn_failed=True)
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(base + "/chats")
        await page.locator(f'[data-session-key="{failed}"]').wait_for()

        def dot(key: str):
            return page.locator(f'[data-session-key="{key}"] .session-main .dot')

        assert await dot(failed).get_attribute("title") == "failed"
        assert await dot(healthy).get_attribute("title") == "idle"
        colour = "el => getComputedStyle(el).backgroundColor"
        failed_colour = await dot(failed).evaluate(colour)
        assert failed_colour != await dot(healthy).evaluate(colour)
        assert failed_colour == "rgb(229, 83, 75)"  # --crit

        # The detail header agrees with the list, and says so in words.
        await page.goto(f"{base}/sessions/{failed}")
        tag = page.locator(".session-head .status-tag")
        await tag.wait_for()
        # `.status-tag` is uppercased by the stylesheet, and `inner_text` returns
        # the transformed rendering, so read the text the template actually put
        # there. This assertion has been failing on master since #788 landed.
        assert (await tag.text_content()).strip() == "failed"
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.goto(base + "/chats")
            await page.locator(f'[data-session-key="{failed}"]').wait_for()
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-788-failed-turn-dot.png")
            )
        await browser.close()


def test_a_working_chat_reports_thinking_even_if_its_last_turn_failed(tmp_path):
    """``thinking`` outranks ``turn_failed``: a chat that is working *now* is not
    reporting a dead past turn (#788)."""
    app = _app_with_state(tmp_path)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    assert replace(session, turn_failed=True).display_state == "failed"
    assert replace(session, turn_failed=True, thinking=True).display_state == "thinking"
    assert session.display_state == "idle"


def _checkout_with_a_change(tmp_path):
    """A tiny repository whose branch has one committed and one uncommitted edit."""
    import subprocess

    repo = tmp_path / "checkout"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "--initial-branch=master")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "greeting.txt").write_text("hello\nworld\n")
    git("add", "-A")
    git("commit", "-m", "base")
    git("checkout", "-b", "agent/issue-1114")
    (repo / "greeting.txt").write_text("hello\ndeck\n")
    git("add", "-A")
    git("commit", "-m", "committed work")
    (repo / "notes.md").write_text("# still being written\n")
    git("add", "notes.md")
    return repo


def _app_with_a_checkout(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.update_session(replace(state.sessions[key], cwd=_checkout_with_a_change(tmp_path)))
    return app


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_reads_the_worktree_the_pull_requests_branch_is_in(tmp_path, monkeypatch):
    """A chat started in a repository's main checkout made its own worktree for the
    branch it opened a pull request from. The main checkout sat on an old, merged branch
    and the pane said "Nothing has changed here yet." (#2993). Its second pull request is
    in a repository it has no checkout of, found from that repository's clone (#3295)."""
    import subprocess

    from agentdeck import work_start

    app = _app_with_state(tmp_path, with_transcript=True)
    key = "claude_code:test:sid1"
    state = app.state.app_state
    main = _checkout_with_a_change(tmp_path)
    subprocess.run(["git", "-C", str(main), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-m", "notes"], check=True, capture_output=True
    )
    for args in (
        ["checkout", "master"],
        ["merge", "--ff-only", "agent/issue-1114"],  # the old branch, merged
        ["checkout", "agent/issue-1114"],
        ["remote", "add", "origin", "https://github.com/shop/store.git"],
    ):
        subprocess.run(["git", "-C", str(main), *args], check=True, capture_output=True)
    worktree = tmp_path / "klarna"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", "hotfix/3548", str(worktree), "master"],
        check=True,
        capture_output=True,
    )
    (worktree / "klarna.py").write_text("TIMEOUT = 10\n")
    web = tmp_path / "web"
    web.mkdir()
    for args in (
        ["init", "--initial-branch=master"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["remote", "add", "origin", "git@github.com:Shop/Web.git"],
        ["commit", "--allow-empty", "-m", "base"],
        ["worktree", "add", "-b", "hotfix/715-part", str(tmp_path / "web-wt")],
    ):
        subprocess.run(["git", "-C", str(web), *args], check=True, capture_output=True)
    (tmp_path / "web-wt" / "transfer.py").write_text("DAY = 1\n")
    monkeypatch.setattr(work_start, "checkout_candidates", lambda _app: [str(main), str(web)])
    state.update_session(replace(state.sessions[key], cwd=main))
    context = GitContext(
        repository="shop/store",
        branch=None,
        dirty=False,
        pull_requests=(
            PullRequestContext(
                repository="shop/store",
                number=3549,
                title="Klarna timeout",
                url="https://github.com/shop/store/pull/3549",
                status="open",
                head_branch="hotfix/3548",
                base_branch="master",
            ),
            PullRequestContext(
                repository="shop/web",
                number=3567,
                title="Transfer day",
                url="https://github.com/shop/web/pull/3567",
                status="open",
                head_branch="hotfix/715-part",
                base_branch="master",
            ),
        ),
    )
    app.state.assistant._contexts.update({key: context})

    async with _client(app) as client:
        fragment = await client.get(f"/partials/sessions/{key}/diff")

    body = fragment.text
    assert "klarna.py" in body and "hotfix/3548" in body
    assert "transfer.py" in body and "hotfix/715-part" in body
    assert "Nothing has changed here yet." not in body
    assert body.count('class="diff-checkout"') == 2


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_fragment_reports_the_checkout_and_is_not_streamed(tmp_path):
    app = _app_with_a_checkout(tmp_path)

    async with _client(app) as client:
        page = await client.get("/sessions/claude_code:test:sid1")
        fragment = await client.get("/partials/sessions/claude_code:test:sid1/diff")

    assert fragment.status_code == 200
    body = fragment.text
    assert "greeting.txt" in body and "notes.md" in body
    assert "agent/issue-1114" in body and "vs master" in body
    assert '<div class="diff-line diff-add">+deck</div>' in body
    # The pane ships empty and asks for its body only when it is opened: nothing
    # about a diff belongs on the session's live stream.
    assert 'data-diff-url="/partials/sessions/claude_code:test:sid1/diff"' in page.text
    assert "greeting.txt" not in page.text
    assert 'sse-swap="diff"' not in page.text


def _checkout_with_source_tests_and_screenshots(tmp_path):
    """A realistic branch: two source files, a big test file, a doc and a screenshot."""
    import subprocess

    repo = tmp_path / "mixed"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "--initial-branch=master")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "worktree_diff.py").write_text("def read():\n    return 1\n")
    git("add", "-A")
    git("commit", "-m", "base")
    git("checkout", "-b", "agent/issue-1684")
    (repo / "src" / "worktree_diff.py").write_text("def read():\n    return 2\n")
    (repo / "src" / "app.css").write_text(".diff-group { border: 1px solid; }\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_worktree_diff.py").write_text(
        "".join(f"def test_case_{index}():\n    assert True\n" for index in range(40))
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "notes.md").write_text("# what changed\n")
    (repo / "docs" / "screenshots").mkdir()
    (repo / "docs" / "screenshots" / "after.png").write_bytes(b"\x89PNG\r\n\x1a\n binary")
    git("add", "-A")
    git("commit", "-m", "work")
    return repo


def _app_with_a_mixed_checkout(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.update_session(
        replace(state.sessions[key], cwd=_checkout_with_source_tests_and_screenshots(tmp_path))
    )
    return app


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_tests_docs_and_screenshots_are_grouped_below_the_source(tmp_path):
    """The source change is what the pane is for. Before #1684 a big test file and a
    committed screenshot sat in the same flat list and buried it."""
    app = _app_with_a_mixed_checkout(tmp_path)

    async with _client(app) as client:
        fragment = await client.get("/partials/sessions/claude_code:test:sid1/diff")

    body = fragment.text
    # Every changed file is still reachable, but the whole-diff chip counts only the source:
    # the tests, docs and screenshots carry their own counts on their groups (#2331).
    for path in ("src/worktree_diff.py", "tests/test_worktree_diff.py", "docs/notes.md"):
        assert path in body
    chip = re.search(r'<span class="chip diff-total">(.*?)</span></span>', body, re.S)
    assert chip is not None and " ".join(re.sub(r"<[^>]+>", "", chip.group(1)).split()) == (
        "2 files +2 −1"
    )
    # The source files come before the first group, and the groups name themselves.
    assert body.index("src/worktree_diff.py") < body.index("Tests")
    assert body.index("src/app.css") < body.index("Tests")
    assert "Screenshots and other non-code files" in body
    # A group is closed until someone opens it, so it costs one row of height.
    for group in ("tests", "docs", "assets"):
        assert f'class="diff-group diff-group-{group}"' in body
    assert "<details class=\"diff-group diff-group-tests\" open>" not in body
    # …while the source file, small on its own, is still expanded on arrival.
    assert body.count("<details class=\"diff-file\" open>") == 2


@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_arrives_before_the_base_branch_fetch_does(tmp_path):
    """Every git read behind the pane is a local few-millisecond thing except the
    base-branch fetch, which is a network round trip. So the fragment answers from
    the refs already on disk and asks for itself once more; that second request is
    the one that waits for the fetch (#1133)."""
    app = _app_with_a_checkout(tmp_path)
    waited = []

    async def refresh(_directory, _repository, _base, *, await_refresh):
        waited.append(await_refresh)
        return (False, not await_refresh)

    app.state.worktree_diff._refresh_base = refresh

    async with _client(app) as client:
        first = await client.get("/partials/sessions/claude_code:test:sid1/diff")
        settled = await client.get("/partials/sessions/claude_code:test:sid1/diff?refresh=wait")

    # The first answer is a real diff, not a waiting room, and it carries the one
    # follow-up that fetches — triggered on load, replacing this same body.
    assert "greeting.txt" in first.text
    assert 'class="diff-refreshing"' in first.text
    assert 'hx-get="/partials/sessions/claude_code:test:sid1/diff?refresh=wait"' in first.text
    assert 'hx-trigger="load"' in first.text
    assert 'hx-target="closest .session-diff-body"' in first.text
    # The settled answer waited, so it does not ask again — no request loop.
    assert "greeting.txt" in settled.text
    assert 'class="diff-refreshing"' not in settled.text
    assert waited == [False, True]


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_grouped_diff_pane_opens_on_the_source_and_folds_the_rest_away(tmp_path):
    """What a reader sees on a phone: the source change open, and one collapsed row
    each for tests, docs and screenshots that opens on a tap (#1684)."""
    app = _app_with_a_mixed_checkout(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")

        await page.locator("[data-session-diff-open]").click()
        await expect(page.locator("#session-diff")).to_contain_text("src/worktree_diff.py")
        source = page.locator("#session-diff .diff-checkout > .diff-file").first
        tests = page.locator("#session-diff .diff-group-tests")
        # The source body is on screen without a tap; the tests are one row.
        await expect(source.locator(".diff-body")).to_be_visible()
        await expect(tests).to_be_visible()
        await expect(tests).to_contain_text("Tests")
        await expect(tests.locator(".diff-file").first).to_be_hidden()
        await expect(page.locator("#session-diff .diff-group")).to_have_count(3)
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1684-diff-grouped.png")
            )

        # And a tap opens the group in place, so nothing is hidden, only folded.
        await tests.locator("> summary").click()
        await expect(tests.locator(".diff-file").first).to_be_visible()
        await expect(tests).to_contain_text("tests/test_worktree_diff.py")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1684-diff-tests-open.png")
            )
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_pane_says_it_is_reading_the_checkout(tmp_path):
    """Reading a checkout is never instant. Before #1133 the pane only dimmed the
    line inviting the open, which reads as a control that did nothing."""
    app = _app_with_a_checkout(tmp_path)
    original = app.state.worktree_diff.read_many

    async def slow(*args, **kwargs):
        await asyncio.sleep(1.5)
        return await original(*args, **kwargs)

    app.state.worktree_diff.read_many = slow

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")

        await page.locator("[data-session-diff-open]").click()
        loading = page.locator("#session-diff .diff-loading")
        await expect(loading).to_be_visible()
        await expect(loading).to_contain_text("Reading this chat's checkout")
        # A spinner that does not turn is a picture of a spinner.
        assert (
            await page.evaluate(
                "getComputedStyle(document.querySelector('.diff-spinner')).animationName"
            )
            == "diff-spin"
        )
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            # The pane slides in, so let it land rather than catching it mid-flight.
            stage = await page.locator(".session-layout").bounding_box()
            await page.wait_for_function(
                "Math.abs(document.getElementById('session-diff')"
                f".getBoundingClientRect().left - {stage['x']}) <= 1"
            )
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-1133-diff-loading.png"))

        await expect(page.locator("#session-diff")).to_contain_text("greeting.txt")
        await expect(loading).to_have_count(0)
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_is_readable_while_its_base_branch_is_still_being_fetched(tmp_path):
    """The whole point of #1133: the files are on screen before the network call
    finishes, and the pane replaces its own body once the fetch settles."""
    app = _app_with_a_checkout(tmp_path)
    holding = asyncio.Event()

    async def refresh(_directory, _repository, _base, *, await_refresh):
        if not await_refresh:
            return (False, True)
        await holding.wait()
        return (False, False)

    app.state.worktree_diff._refresh_base = refresh

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")

        await page.locator("[data-session-diff-open]").click()
        # Readable diff and an honest note that the base is still being checked —
        # both at once, which is the behaviour the issue asked for.
        await expect(page.locator("#session-diff")).to_contain_text("greeting.txt")
        refreshing = page.locator("#session-diff .diff-refreshing")
        await expect(refreshing).to_be_visible()
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-1133-diff-refreshing.png"))

        holding.set()
        # The follow-up swaps the settled comparison into the same body and takes
        # the note with it — and does not ask a third time.
        await expect(refreshing).to_have_count(0)
        await expect(page.locator("#session-diff")).to_contain_text("greeting.txt")
        await browser.close()


async def test_a_chat_without_a_directory_says_so_instead_of_hiding_the_control(tmp_path):
    """The pane outlives a session navigation, so it is the chat pane that says
    whether there is a checkout to read. With none the control stays put and the
    pane explains itself: hiding it left the gesture silently dead with nothing on
    screen to say why (#1219)."""
    app = _app_with_state(tmp_path, with_transcript=True)

    async with _client(app) as client:
        page = await client.get("/sessions/claude_code:test:sid1")

    assert 'data-diff-url=""' in page.text
    assert 'data-session-diff-open aria-expanded="false"' in page.text
    control = page.text.split("data-session-diff-open", 1)[1].split(">", 1)[0]
    assert "hidden" not in control


@pytest.mark.browser
async def test_a_chat_without_a_checkout_opens_the_pane_and_explains(tmp_path):
    """#1219: the swipe and the control both work, and what they open says why
    there is no diff rather than nothing happening at all."""
    app = _app_with_state(tmp_path, with_transcript=True)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, has_touch=True
        )
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")
        toggle = page.locator("[data-session-diff-open]")
        await expect(toggle).to_be_visible()
        await toggle.click()
        pane = page.locator("#session-diff")
        await expect(pane).to_have_attribute("aria-hidden", "false")
        await expect(pane).to_contain_text("no checkout on this host")
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1219-diff-no-checkout.png")
            )
        await browser.close()


async def _finger_drag(page, points):
    """Drag with a real touch point rather than the mouse.

    Playwright's mouse produces pointer events of type ``mouse``, which is not
    the input this gesture is for: a phone's own edge gestures and the
    touch-only rules in ``session_diff.js`` only exist for fingers. Driven
    through CDP because Playwright's touchscreen can tap but not drag.
    """
    session = await page.context.new_cdp_session(page)
    for index, (x, y) in enumerate(points):
        await session.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart" if index == 0 else "touchMove",
                "touchPoints": [{"x": x, "y": y}],
            },
        )
    await session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_a_chat_opened_in_page_still_has_its_diff_pane(tmp_path):
    """Tapping a card is how chats are opened, and that path replaced only the
    chat pane — so the diff pane and its control were simply not on the page, and
    the gesture had nothing to open. Only a full page load ever had them (#1219)."""
    app = _app_with_a_checkout(tmp_path)
    canonical_path = app.state.app_state.chat_path_for_session(
        "claude_code:test:sid1"
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, has_touch=True
        )
        await page.goto(base + "/chats")
        await page.wait_for_function("window.htmx")
        assert not await page.evaluate("!!document.getElementById('session-diff')")

        await page.locator('a[data-session-key="claude_code:test:sid1"]').first.click()
        await page.wait_for_function(f"location.pathname === {canonical_path!r}")
        pane = page.locator("#session-diff")
        toggle = page.locator("[data-session-diff-open]")
        await expect(toggle).to_be_visible()
        await toggle.click()
        await expect(pane).to_have_attribute("aria-hidden", "false")
        await expect(pane).to_contain_text("greeting.txt")
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await expect(page.locator("#session-diff .diff-file").first).to_be_visible()
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1219-diff-after-in-page-open.png")
            )

        # And a finger opens it here too, not only after a full page load.
        await page.keyboard.press("Escape")
        await expect(pane).to_have_attribute("aria-hidden", "true")
        stage = await page.locator(".session-layout").bounding_box()
        middle = stage["x"] + stage["width"] / 2
        await _finger_drag(
            page, [(middle, 500)] + [(middle - step, 504) for step in (30, 80, 140, 180)]
        )
        await expect(pane).to_have_attribute("aria-hidden", "false")

        # Going back to the list on a desktop takes the control with it: left
        # behind, it would open a chat that is no longer mounted.
        await page.keyboard.press("Escape")
        await resized(page, 1200, 900)
        # The house leaves for the front page (#2266): over the chats view it is a plain
        # link to `/` rather than a close into that view.
        await page.locator("a.brand").click()
        await page.wait_for_function("location.pathname === '/'")
        await expect(toggle).to_have_count(0)
        assert not await page.evaluate("!!document.getElementById('session-diff')")
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_a_finger_opens_the_diff_without_starting_at_the_screen_edge(tmp_path):
    """The strip along the screen edge belongs to the phone, which puts its own
    back/forward gestures there and swallows the touch before the page sees it.
    So a finger opens and closes the pane by dragging anywhere on the layer it is
    dragging, and only a mouse still has to start at the edge (#1219)."""
    app = _app_with_a_checkout(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, has_touch=True
        )
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")
        pane = page.locator("#session-diff")
        stage = await page.locator(".session-layout").bounding_box()
        middle = stage["x"] + stage["width"] / 2

        # Reading the transcript is a vertical swipe and must leave it closed.
        await _finger_drag(
            page, [(middle, 600)] + [(middle - 12, 600 - step) for step in (40, 120, 240)]
        )
        await expect(pane).to_have_attribute("aria-hidden", "true")

        # A mouse keeps the edge rule: it selects text across this same area.
        await page.mouse.move(middle, 500)
        await page.mouse.down()
        for step in (60, 150, 270):
            await page.mouse.move(middle - step, 500)
        await page.mouse.up()
        await expect(pane).to_have_attribute("aria-hidden", "true")

        # A finger dragging leftwards from the middle of the chat opens it — even
        # starting on a button, because a transcript is mostly links and buttons.
        # The drag swallows the tap it would otherwise have been.
        await page.evaluate(
            """() => {
                const button = document.createElement('button');
                button.type = 'button';
                button.id = 'drag-start-button';
                button.textContent = 'tap me';
                button.addEventListener('click', () => { window.tapped = true; });
                document.querySelector('.transcript .ev').appendChild(button);
            }"""
        )
        button = await page.locator("#drag-start-button").bounding_box()
        start = (button["x"] + button["width"] / 2, button["y"] + button["height"] / 2)
        await _finger_drag(
            page,
            [start] + [(start[0] - step, start[1] + 4) for step in (30, 80, 140, 180)],
        )
        await expect(pane).to_have_attribute("aria-hidden", "false")
        assert not await page.evaluate("window.tapped === true")
        await expect(page.locator("#session-diff .diff-file").first).to_be_visible()
        await expect(pane).to_contain_text("greeting.txt")

        # And dragging back rightwards from the middle of the pane closes it,
        # leaving the chat — not the session list — behind it.
        await _finger_drag(
            page, [(middle, 500)] + [(middle + step, 504) for step in (30, 80, 140, 180)]
        )
        await expect(pane).to_have_attribute("aria-hidden", "true")
        await expect(page.locator("[data-session-diff-open]")).to_have_attribute(
            "aria-expanded", "false"
        )
        assert await page.evaluate("location.pathname") == "/sessions/claude_code:test:sid1"
        assert not await page.evaluate(
            "document.body.classList.contains('mobile-list-open')"
        )

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            # Mid-gesture, so the evidence shows the pane arriving under a finger
            # that started nowhere near the screen edge.
            cdp = await page.context.new_cdp_session(page)
            for index, x in enumerate((middle, middle - 40, middle - 120, middle - 200)):
                await cdp.send(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchStart" if index == 0 else "touchMove",
                        "touchPoints": [{"x": x, "y": 500}],
                    },
                )
            assert await page.evaluate(
                "document.body.classList.contains('diff-dragging')"
            )
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1219-diff-drag-midway.png")
            )
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            await expect(pane).to_have_attribute("aria-hidden", "false")
            await expect(page.locator("#session-diff .diff-file").first).to_be_visible()
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1219-diff-open.png")
            )
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_pane_opens_by_swipe_and_button_and_closes_with_back(tmp_path):
    app = _app_with_a_checkout(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, has_touch=True
        )
        await page.goto(base + "/sessions/claude_code:test:sid1")
        await page.wait_for_function("window.htmx")
        pane = page.locator("#session-diff")
        toggle = page.locator("[data-session-diff-open]")
        stage = await page.locator(".session-layout").bounding_box()
        settled = (
            "Math.abs(document.getElementById('session-diff')"
            f".getBoundingClientRect().left - {stage['x']}) <= 1"
        )

        # Closed: the pane is off to the right and out of the accessibility tree.
        assert await pane.evaluate("el => el.getBoundingClientRect().left") >= (
            stage["x"] + stage["width"]
        )
        assert await pane.get_attribute("aria-hidden") == "true"

        await toggle.click()
        await expect(page.locator("#session-diff .diff-file").first).to_be_visible()
        await expect(pane).to_contain_text("greeting.txt")
        await expect(toggle).to_have_attribute("aria-expanded", "true")
        # It slides in, so wait for the transform to settle rather than mid-flight.
        await page.wait_for_function(settled)
        # The pane covers the chat rather than widening the page.
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )

        # System Back closes the diff and leaves the chat where it was.
        await page.go_back()
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        assert await page.evaluate("location.pathname") == "/sessions/claude_code:test:sid1"

        # A drag in from the right edge opens the same pane, and the request is
        # not repeated because the body was already fetched.
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        edge = stage["x"] + stage["width"] - 4
        await page.mouse.move(edge, 500)
        await page.mouse.down()
        for step in (60, 150, 270, 340):
            await page.mouse.move(edge - step, 500)
        await page.mouse.up()
        await expect(pane).to_have_attribute("aria-hidden", "false")
        await page.wait_for_function(settled)
        assert not [url for url in requests if url.endswith("/diff")]

        # Desktop: the same control, the pane taking the transcript's column.
        await resized(page, 1200, 900)
        await page.keyboard.press("Escape")
        await expect(toggle).to_have_attribute("aria-expanded", "false")
        await toggle.click()
        await expect(pane).to_be_visible()
        box = await pane.bounding_box()
        assert box["width"] > 400
        # The open pane and the chat it replaces share one grid cell. Left to
        # auto-placement the chat was pushed onto a second row under the session
        # list, which halved the sidebar and left a stray chat block showing
        # below it (#1128).
        assert (
            len(
                await page.evaluate(
                    "getComputedStyle(document.querySelector('.session-layout'))"
                    ".gridTemplateRows.split(' ')"
                )
            )
            == 1
        )
        await wait_until(
            page,
            "the chat pane settled on top of the stage it replaced",
            "(at) => {"
            " const el = document.querySelector('.session-detail');"
            " if (!el) return false;"
            " const r = el.getBoundingClientRect();"
            " return Math.abs(r.left - at.x) <= 1 && Math.abs(r.top - at.y) <= 1;"
            "}",
            arg={"x": box["x"], "y": box["y"]},
        )
        # The session list still fills the stage rather than being cut in half.
        stage_box = await page.locator(".session-layout").bounding_box()
        sidebar = await page.locator(".session-sidebar").bounding_box()
        assert abs(sidebar["height"] - stage_box["height"]) <= 1
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1114-diff-desktop.png")
            )
            await resized(page, 390, 844)
            await expect(pane).to_be_visible()
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1114-diff-mobile.png")
            )
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_a_wide_desktop_reads_the_diff_beside_the_chat(tmp_path):
    """#1796: with the room for a third column the diff is read next to the
    transcript rather than on top of it. Narrower desktops and a chat that has
    already claimed the columns for an extra pane keep the stacked pane."""
    app = _app_with_a_checkout(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1600, "height": 900}, service_workers="block"
        )
        await _open_chat_from_chats_view(page, base, "claude_code:test:sid1")
        await page.wait_for_function("window.htmx")
        pane = page.locator("#session-diff")
        chat = page.locator(".session-layout > .session-detail")
        toggle = page.locator("[data-session-diff-open]")

        async def boxes():
            return await chat.bounding_box(), await pane.bounding_box()

        await toggle.click()
        await expect(page.locator("#session-diff .diff-file").first).to_be_visible()
        await expect(pane).to_contain_text("greeting.txt")

        # Side by side: the chat is still readable and the diff starts after it.
        assert await chat.evaluate("el => getComputedStyle(el).visibility") == "visible"
        chat_box, pane_box = await boxes()
        assert pane_box["x"] >= chat_box["x"] + chat_box["width"] - 1
        assert abs(chat_box["y"] - pane_box["y"]) <= 1
        assert chat_box["width"] > 400 and pane_box["width"] > 400
        # Still one row, so the chat cannot be pushed under a halved sidebar
        # the way it was in #1128, and the page does not grow a scrollbar.
        assert (
            len(
                await page.evaluate(
                    "getComputedStyle(document.querySelector('.session-layout'))"
                    ".gridTemplateRows.split(' ')"
                )
            )
            == 1
        )
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        sidebar = await page.locator(".session-sidebar").bounding_box()
        stage = await page.locator(".session-layout").bounding_box()
        assert abs(sidebar["height"] - stage["height"]) <= 1

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1796-diff-beside-chat.png")
            )

        # A desktop too narrow for a third column keeps the stacked pane.
        await resized(page, 1200, 900)
        await expect(pane).to_be_visible()
        assert await chat.evaluate("el => getComputedStyle(el).visibility") == "hidden"
        chat_box, pane_box = await boxes()
        assert abs(chat_box["x"] - pane_box["x"]) <= 1

        await resized(page, 1600, 900)
        chat_box, pane_box = await boxes()
        assert pane_box["x"] >= chat_box["x"] + chat_box["width"] - 1

        # Once an extra chat pane is open the columns are spoken for, so the diff
        # goes back to taking the transcript's own column.
        await page.locator("[data-session-extra-add]").click()
        await expect(page.locator(".session-extra-frame")).to_have_count(1)
        await expect(pane).to_be_visible()
        assert await chat.evaluate("el => getComputedStyle(el).visibility") == "hidden"
        chat_box, pane_box = await boxes()
        assert abs(chat_box["x"] - pane_box["x"]) <= 1
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= window.innerWidth"
        )
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1796-diff-with-extra-pane.png")
            )

        # Closing the extra pane hands the third column back to the diff.
        await page.locator("[data-session-extra-close]").click()
        await expect(page.locator(".session-extra-frame")).to_have_count(0)
        assert await chat.evaluate("el => getComputedStyle(el).visibility") == "visible"
        chat_box, pane_box = await boxes()
        assert pane_box["x"] >= chat_box["x"] + chat_box["width"] - 1
        await browser.close()


@pytest.mark.browser
@pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")
async def test_the_diff_pane_follows_the_chat_the_deck_navigates_to(tmp_path):
    """Navigation replaces the chat pane and leaves the diff pane standing, so the
    pane has to re-read whose diff it is showing — otherwise it keeps offering the
    previous chat's checkout."""
    app = _app_with_a_checkout(tmp_path)
    state = app.state.app_state
    transcript = tmp_path / "projects" / "-tmp"
    (transcript / "sid2.jsonl").write_text((transcript / "sid1.jsonl").read_text())
    state.update_session(
        Session(
            key="claude_code:test:sid2",
            account_key="claude_code:test",
            session_id="sid2",
            status=SessionStatus.LIVE,
            title="No checkout here",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    sid2_path = state.chat_path_for_session("claude_code:test:sid2")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 900})
        await _open_chat_from_chats_view(page, base, "claude_code:test:sid1")
        await page.wait_for_function("window.htmx")
        toggle = page.locator("[data-session-diff-open]")
        await toggle.click()
        await expect(page.locator("#session-diff")).to_contain_text("greeting.txt")

        await page.locator('[data-session-key="claude_code:test:sid2"]').first.click()
        await page.wait_for_function(f"location.pathname === {sid2_path!r}")
        # The pane it inherited is closed rather than still showing the other
        # chat's files, and the control stays put but now explains itself (#1219).
        await expect(toggle).to_be_visible()
        await expect(page.locator("#session-diff")).to_have_attribute("aria-hidden", "true")
        await expect(page.locator("#session-diff")).not_to_contain_text("greeting.txt")
        await toggle.click()
        await expect(page.locator("#session-diff")).to_contain_text("no checkout on this host")
        await browser.close()


# --- the current commit's review chip (#1127) ---

_HEAD_SHA = "e" * 40


def _app_with_a_commit(tmp_path, *, dirty=False):
    """A chat whose checkout Deckhand has resolved down to a commit."""
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.update_session(replace(state.sessions[key], cwd=tmp_path))
    # The chip shares its row with the chat's issue and pull request, so the
    # fixture carries those too: the row is what a real chat's row looks like.
    state.add_issue(key, IssueRef("eerovil/agentdeck", 1127))
    state.add_pull_request(key, PullRequestRef("eerovil/agentdeck", 1128))
    app.state.assistant._contexts.update(
        {
            key: GitContext(
                repository="eerovil/agentdeck",
                branch="agent/issue-1127",
                dirty=dirty,
                pull_requests=(
                    PullRequestContext(
                        repository="eerovil/agentdeck",
                        number=1128,
                        title="Show a review chip for the current commit",
                        url="https://github.com/eerovil/agentdeck/pull/1128",
                        status="open",
                        head_branch="agent/issue-1127",
                        base_branch="master",
                    ),
                ),
                head_sha=_HEAD_SHA,
            )
        }
    )
    return app


def _stub_review_launch(app, monkeypatch, *, accepted=True, reason=None):
    """Answer the review launch without starting an agent; keep what was asked."""
    from agentdeck.web import routes_actions

    account = app.state.accounts[0]
    monkeypatch.setattr(
        routes_actions, "delegation_account", lambda request, account_key: account
    )
    calls = []

    async def fake_start_delegation(account, provider, cwd, message, **kwargs):
        calls.append({"cwd": cwd, "message": message, **kwargs})
        if not accepted:
            return InjectResult(False, reason or "no headroom"), None
        # What the real service does before it answers: the delegation is on
        # record as starting, which is how the chip knows a review is under way
        # before the provider has named its chat.
        app.state.injector._remember_delegation(
            DelegationStatus("deleg-1127", "starting", account.key)
        )
        return InjectResult(True), "deleg-1127"

    monkeypatch.setattr(app.state.injector, "start_delegation", fake_start_delegation)
    return calls


async def test_the_commit_chip_offers_a_review_and_pressing_it_starts_one(
    tmp_path, monkeypatch
):
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    calls = _stub_review_launch(app, monkeypatch)

    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")
        bar = _session_action_bar(page.text)
        assert f'hx-post="/sessions/{key}/review-head"' in bar
        assert f"{_HEAD_SHA[:7]} · review &amp; fix?" in " ".join(bar.split())

        pressed = await client.post(f"/sessions/{key}/review-head")

    assert pressed.status_code == 202
    assert f"{_HEAD_SHA[:7]} · reviewing…" in " ".join(pressed.text.split())
    # The review runs in this chat's own directory as a primary coordinator and is told
    # which commit it is about and where to put the verdict. Its review-fix-pr
    # pass can request the narrow approvals needed to commit and push fixes.
    assert len(calls) == 1
    assert calls[0]["cwd"] == tmp_path
    assert "parent_session_id" not in calls[0]
    assert calls[0]["sandbox"] == "workspace-write"
    assert calls[0]["approval_policy"] == "on-request"
    assert calls[0]["session_role"] == "primary"
    assert _HEAD_SHA in calls[0]["message"]
    assert "Run the installed `review-fix-pr` skill exactly once" in calls[0]["message"]
    assert "/api/review-verdicts" in calls[0]["message"]
    launch = app.state.app_state.head_review_launch("eerovil/agentdeck", _HEAD_SHA)
    assert launch is not None
    assert launch.delegation_id == "deleg-1127"


async def test_a_commit_being_reviewed_is_not_reviewed_twice(tmp_path, monkeypatch):
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    calls = _stub_review_launch(app, monkeypatch)
    monkeypatch.setattr(
        app.state.injector,
        "delegation_status",
        lambda delegation_id: DelegationStatus(
            delegation_id, "running", "claude_code:test", session_key="claude_code:test:rev"
        ),
    )

    async with _client(app) as client:
        first = await client.post(f"/sessions/{key}/review-head")
        second = await client.post(f"/sessions/{key}/review-head")

    assert first.status_code == 202
    assert second.status_code == 409
    assert "already running" in second.text
    assert len(calls) == 1
    # The refusal still leaves the row it replaced, so nothing vanishes from the page.
    assert f"{_HEAD_SHA[:7]} · reviewing…" in " ".join(second.text.split())
    assert 'href="/sessions/claude_code:test:rev"' in second.text


async def test_a_commit_that_has_a_verdict_shows_it_and_is_not_reviewed_again(
    tmp_path, monkeypatch
):
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    calls = _stub_review_launch(app, monkeypatch)

    async with _client(app) as client:
        recorded = await client.post(
            "/api/review-verdicts",
            json={
                "repo": "eerovil/agentdeck",
                "sha": _HEAD_SHA,
                "verdict": "changes-needed",
            },
        )
        page = await client.get(f"/sessions/{key}")
        pressed = await client.post(f"/sessions/{key}/review-head")

    assert recorded.status_code == 200
    assert recorded.json()["recorded"] is True
    bar = _session_action_bar(page.text)
    assert f"{_HEAD_SHA[:7]} · changes-needed" in " ".join(bar.split())
    assert "review-head" not in bar
    assert pressed.status_code == 409
    assert "already has a verdict" in pressed.text
    assert calls == []


async def test_a_review_verdict_only_speaks_the_two_words_a_reviewer_reports(tmp_path):
    app = _app_with_a_commit(tmp_path)

    async with _client(app) as client:
        refused = await client.post(
            "/api/review-verdicts",
            json={"repo": "eerovil/agentdeck", "sha": _HEAD_SHA, "verdict": "looks fine"},
        )

    assert refused.status_code == 422
    assert app.state.app_state.review_verdict("eerovil/agentdeck", _HEAD_SHA) is None


async def test_a_refused_review_says_why_in_the_row_that_was_pressed(
    tmp_path, monkeypatch
):
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    _stub_review_launch(app, monkeypatch, accepted=False, reason="message injection is disabled")

    async with _client(app) as client:
        refused = await client.post(f"/sessions/{key}/review-head")

    assert refused.status_code == 422
    assert refused.headers["content-type"].startswith("text/html")
    assert "head-review-refused" in refused.text
    # The chip it was pressed from is still there, still pressable.
    assert "review-head" in refused.text


async def test_a_dirty_checkout_is_not_handed_to_review_fix_pr(tmp_path, monkeypatch):
    app = _app_with_a_commit(tmp_path, dirty=True)
    key = "claude_code:test:sid1"
    calls = _stub_review_launch(app, monkeypatch)

    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")
        refused = await client.post(f"/sessions/{key}/review-head")

    assert refused.status_code == 409
    assert "uncommitted changes" in refused.text
    assert "review-fix-pr" in refused.text
    assert calls == []
    assert "commit first" in _session_action_bar(page.text)
    assert "review-head" not in _session_action_bar(page.text)


async def test_a_chat_with_no_commit_has_no_commit_chip(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    key = "claude_code:test:sid1"

    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")
        pressed = await client.post(f"/sessions/{key}/review-head")

    assert "head-review" not in _session_action_bar(page.text)
    assert pressed.status_code == 422
    assert "no commit to review" in pressed.text


@pytest.mark.browser
async def test_pressing_the_commit_chip_flips_it_to_reviewing(tmp_path, monkeypatch):
    # The real control in the real action bar: the button is legible at the
    # narrowest supported width, and pressing it swaps the row it lives in (#1127).
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    _stub_review_launch(app, monkeypatch)
    started = {"value": False}

    def status(delegation_id):
        if not started["value"]:
            return None
        return DelegationStatus(
            delegation_id, "running", "claude_code:test", session_key="claude_code:test:rev"
        )

    monkeypatch.setattr(app.state.injector, "delegation_status", status)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for viewport_width in (390, 1100):
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": 844},
                service_workers="block",
            )
            # Each width starts from a commit nobody is reviewing: the press made
            # at the previous width would otherwise be refused as a duplicate.
            app.state.app_state.head_review_launches.clear()
            started["value"] = False
            await page.goto(f"{base}/sessions/{key}")
            await page.wait_for_function("window.htmx")
            button = page.locator("#session-related-links button.head-review-start")
            await button.wait_for(state="visible")
            assert "review & fix?" in (await button.inner_text())
            if screenshot_dir:
                await page.locator("#session-action-bar").screenshot(
                    path=str(
                        Path(screenshot_dir)
                        / f"issue-1155-review-fix-chip-{viewport_width}.png"
                    )
                )
            started["value"] = True
            page.once("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
            await button.click()
            running = page.locator("#session-related-links a.head-review-running")
            await running.wait_for(state="visible")
            assert "reviewing" in (await running.inner_text())
            started["value"] = False
            await page.close()
        await browser.close()


@pytest.mark.browser
async def test_the_commit_chip_shows_a_recorded_verdict(tmp_path):
    # The other end of the same control: a commit with a verdict is a plain chip,
    # in the verdict's own colour, with no button to press (#1127).
    app = _app_with_a_commit(tmp_path)
    key = "claude_code:test:sid1"
    app.state.app_state.record_review_verdict(
        PollerReviewVerdict(
            repo="eerovil/agentdeck",
            sha=_HEAD_SHA,
            verdict="pass",
            result="none",
            recorded_at=datetime.now(UTC),
        )
    )

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(f"{base}/sessions/{key}")
        chip = page.locator("#session-related-links .chip.head-review-pass")
        await chip.wait_for(state="visible")
        assert "pass" in (await chip.inner_text())
        await expect(page.locator("button.head-review-start")).to_have_count(0)
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.locator("#session-action-bar").screenshot(
                path=str(Path(screenshot_dir) / "issue-1127-commit-chip-reviewed-390.png")
            )
        await page.close()
        await browser.close()


def _chained_chat_app(tmp_path):
    """One chat whose work moved from account ``test`` to account ``alt``.

    Both legs deliberately start their own numbering at 1, which is the collision
    that made a bare seq unusable as a chat-level message identity (#1265).
    """
    legs = {
        "test": ("sid1", "first question", "an answer here"),
        "alt": ("sid2", "second question", "the newer answer"),
    }
    for label, (session_id, asked, answered) in legs.items():
        proj = tmp_path / label / "projects" / "-tmp"
        proj.mkdir(parents=True)
        lines = [
            {
                "type": "user",
                "timestamp": "2026-07-15T07:41:00Z",
                "message": {"role": "user", "content": asked},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-15T07:42:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": answered}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        (proj / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines)
        )
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            accounts=[
                AccountConfig(
                    provider="claude_code", label=label, config_dir=str(tmp_path / label)
                )
                for label in legs
            ],
        )
    )
    state = app.state.app_state
    for label, (session_id, _, _) in legs.items():
        state.update_session(
            Session(
                key=f"claude_code:{label}:{session_id}",
                account_key=f"claude_code:{label}",
                session_id=session_id,
                status=SessionStatus.LIVE,
                title="Chained chat",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )
    old_key, current_key = "claude_code:test:sid1", "claude_code:alt:sid2"
    app.state.budget_db.ensure_session_chats((old_key,))
    chat = app.state.budget_db.append_chat_leg(old_key, current_key)
    return app, chat, old_key, current_key


async def test_two_leg_chat_is_one_scroll_with_an_inline_switch_event(tmp_path):
    app, chat, old_key, current_key = _chained_chat_app(tmp_path)

    # As above: the cursor on the stream URL is asserted exactly, so this reads
    # the default page rather than one with the sidebar list opened (#1525).
    async with _client(app) as client:
        page = await client.get(f"/chats/{chat.chat_id}")
        through_old_alias = await client.get(f"/sessions/{old_key}")

    assert page.status_code == 200
    for html in (page.text, through_old_alias.text):
        # One continuous transcript: both legs, in order, in the same scroll.
        assert html.index("an answer here") < html.index("the newer answer")
        # The inline account switch between them (#1250 shape A).
        assert 'class="ev leg-switch"' in html
        assert 'id="chat-switch-1"' in html
        # It names both sides, not only where the chat went (#1267): a reader
        # scrolling up meets this row away from the badge that says where it is.
        seam = html[html.index('id="chat-switch-1"') :][:800]
        assert seam.index(">test<") < seam.index("→") < seam.index(">alt<")
        assert html.index("an answer here") < html.index('id="chat-switch-1"')
        assert html.index('id="chat-switch-1"') < html.index("the newer answer")
        # Colliding native seq 1 on both legs, two distinct anchors.
        assert 'id="event-1"' in html
        assert 'id="event-1.1"' in html
        assert f'data-event-session-key="{old_key}"' in html
        assert f'data-event-session-key="{current_key}"' in html
        # Only the current leg is a live tail, and its cursor is chat-level —
        # "<leg>.<seq>" — so a switch under an open page still leaves the stream
        # a cursor that says which leg the reader had reached (#1267).
        assert (
            f'sse-connect="/events/sessions/{current_key}?after_seq=1.2"' in html
        )
        assert f"/events/sessions/{old_key}" not in html


async def test_a_rollover_seam_says_it_was_a_rollover(tmp_path):
    """The seam a context rollover made must not read as an account move (#1667).

    A rollover replaces the provider session, usually on the same account, and
    leaves the chain the same "new leg" a real account move leaves. Only the
    frozen delivery row tells them apart, so the row is what the seam reads.
    """
    app, chat, old_key, current_key = _chained_chat_app(tmp_path)
    # The fixture's own deck has no delivery ledger; this seam is read from one.
    db = Db(tmp_path / "deliveries.sqlite3")
    app.state.db = app.state.app_state.db = db
    db.reserve_context_delivery(
        ContextDelivery(
            action_id="act-rollover",
            attempt=1,
            provider_id="claude_code",
            account_key="claude_code:test",
            worker_key="worker-1",
            delivery_id="del-rollover",
            request_sha256="a" * 64,
            chat_id=chat.chat_id,
            source_session_key=old_key,
            source_session_id="sid1",
            mode="enforce",
            proposed_action="rollover",
            executed_action="rollover",
            reason="threshold",
            target_session_key=current_key,
        )
    )

    async with _client(app) as client:
        page = await client.get(f"/chats/{chat.chat_id}")

    assert page.status_code == 200
    seam = page.text[page.text.index('id="chat-switch-1"') :][:900]
    assert "context rollover" in seam
    # And the row no longer claims something that did not happen.
    assert "continued on another account" not in seam
    assert "the context window had filled up" in seam


async def test_a_seam_that_was_not_a_rollover_keeps_the_words_it_had(tmp_path):
    app, chat, _old_key, _current_key = _chained_chat_app(tmp_path)

    async with _client(app) as client:
        page = await client.get(f"/chats/{chat.chat_id}")

    seam = page.text[page.text.index('id="chat-switch-1"') :][:900]
    assert "context rollover" not in seam
    assert "continued on another account" in seam


async def test_load_earlier_crosses_the_leg_boundary_without_duplicates_or_gaps(
    tmp_path,
):
    app, _chat, old_key, current_key = _chained_chat_app(tmp_path)

    async with _client(app) as client:
        # The first event of the newest leg: this leg has nothing before it, so
        # the step has to fall through into the leg before it.
        crossing = await client.get(
            f"/partials/sessions/{current_key}/transcript?before=1.1"
        )
        # Leg 0 starts at its own event 1, so the chat is now fully on screen.
        exhausted = await client.get(
            f"/partials/sessions/{current_key}/transcript?before=1"
        )

    assert crossing.status_code == 200
    assert "an answer here" in crossing.text
    assert 'id="event-1"' in crossing.text
    # No gap: the switch marker into the newer leg comes with the older window.
    assert 'id="chat-switch-1"' in crossing.text
    # No duplicates: the newer leg's messages are already on the page.
    assert "the newer answer" not in crossing.text
    assert 'id="event-1.1"' not in crossing.text
    # Nothing earlier remains, so the button does not come back.
    assert "load-earlier" not in crossing.text
    assert exhausted.text == ""
    assert old_key  # the crossing is what proves the older leg was reached


def _switching_chat_app(tmp_path):
    """A chat on leg ``test`` whose successor on ``alt`` exists but is not linked.

    The successor's transcript is deliberately absent: the test writes it at the
    moment it appends the leg, which is the shape of every automatic switch —
    the deck learns about the new leg and its first events together.
    """
    for label in ("test", "alt"):
        (tmp_path / label / "projects" / "-tmp").mkdir(parents=True)

    def _line(kind, text):
        if kind == "user":
            return {
                "type": "user",
                "timestamp": "2026-07-15T07:41:00Z",
                "message": {"role": "user", "content": text},
            }
        return {
            "type": "assistant",
            "timestamp": "2026-07-15T07:42:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }

    def _write(label, session_id, lines, *, append=False):
        path = tmp_path / label / "projects" / "-tmp" / f"{session_id}.jsonl"
        body = "".join(
            json.dumps(_line(kind, text)) + "\n" for kind, text in lines
        )
        with path.open("a" if append else "w") as handle:
            handle.write(body)

    _write("test", "sid1", [("user", "asked on test"), ("assistant", "answered on test")])
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
            accounts=[
                AccountConfig(
                    provider="claude_code", label=label, config_dir=str(tmp_path / label)
                )
                for label in ("test", "alt")
            ],
        )
    )
    state = app.state.app_state
    for label, session_id in (("test", "sid1"), ("alt", "sid2")):
        state.update_session(
            Session(
                key=f"claude_code:{label}:{session_id}",
                account_key=f"claude_code:{label}",
                session_id=session_id,
                status=SessionStatus.LIVE,
                title="Switching chat",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )
    old_key, new_key = "claude_code:test:sid1", "claude_code:alt:sid2"
    app.state.budget_db.ensure_session_chats((old_key,))
    chat = app.state.budget_db.chat_for_session(old_key)
    return app, chat, old_key, new_key, _write


@pytest.mark.browser
async def test_an_open_chat_moves_its_live_tail_to_the_new_leg(tmp_path):
    """An automatic switch under an open page moves the tail, not just the card.

    Before #1267 the EventSource stayed bound to whichever leg was current when
    the page was rendered, so the card and every control moved to the successor
    while the visible transcript went on tailing a leg that had stopped
    answering — and only a reload fixed it.
    """
    app, chat, old_key, new_key, write = _switching_chat_app(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        insights=tuple(
            AssistantInsight(key, "waiting", "waiting")
            for key in (old_key, new_key)
        ),
    )

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/chats/{chat.chat_id}")
        await expect(page.locator("#event-1 .ev-text")).to_contain_text(
            "asked on test"
        )
        # A chat page carries no list to mark the leg on (#2603); the transcript is the
        # evidence below.
        # A marker that only survives if the page is never reloaded.
        await page.evaluate("window.__neverReloaded = true")

        # The switch: the successor's first events land and the chat adopts it,
        # exactly as reserve_chat_successor/append_chat_leg do in production.
        write("alt", "sid2", [("user", "asked on alt"), ("assistant", "answered on alt")])
        app.state.budget_db.append_chat_leg(old_key, new_key)
        # What the collector's scan of that write announces (#2865): the open page
        # waits on its chat's topic, not on a tick.
        app.state.app_state.sessions_changed((new_key,))

        # The new leg arrives on the socket that was already open.
        await expect(page.locator("#event-1\\.1 .ev-text")).to_contain_text(
            "asked on alt"
        )
        await expect(page.locator("#event-1\\.2 .ev-text")).to_contain_text(
            "answered on alt"
        )
        # Exactly once, with the seam above it and the old leg still in place.
        await expect(page.locator("#event-1\\.1")).to_have_count(1)
        await expect(page.locator("#event-1\\.2")).to_have_count(1)
        await expect(page.locator(".leg-switch")).to_have_count(1)
        await expect(page.locator("#chat-switch-1")).to_contain_text("test")
        await expect(page.locator("#chat-switch-1")).to_contain_text("alt")
        await expect(page.locator("#event-1")).to_have_count(1)
        await expect(page.locator("#event-2")).to_have_count(1)
        await expect(page.locator("#event-1 .ev-text")).to_contain_text(
            "asked on test"
        )

        # The chat has left the old leg, so what is written there afterwards is
        # not this conversation any more and must not be tailed.
        write("test", "sid1", [("assistant", "a stray write on the old leg")], append=True)
        # The tail keeps working on the leg it moved to, which is also how long
        # enough passes for a stray old-leg append to have shown up if it could.
        write("alt", "sid2", [("user", "still talking on alt")], append=True)
        app.state.app_state.wake_chat_pages((old_key, new_key))
        await expect(page.locator("#event-1\\.3 .ev-text")).to_contain_text(
            "still talking on alt"
        )
        await expect(page.locator("#event-3")).to_have_count(0)
        assert "a stray write on the old leg" not in await page.content()

        assert await page.evaluate("window.__neverReloaded") is True
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir) / "issue-1267-live-leg-migration-390.png"
                ),
                full_page=True,
            )
        await page.close()
        await browser.close()


async def test_a_reconnect_after_a_switch_catches_up_across_the_seam(tmp_path):
    """A watchdog reconnect whose cursor names the leg the chat has left.

    The stream has to replay the seam and the whole new leg — not resume on the
    old leg, and not replay what the reader already has (#1267)."""
    from agentdeck.web.routes_sse import _session_stream

    app, chat, old_key, new_key, write = _switching_chat_app(tmp_path)
    write("alt", "sid2", [("user", "asked on alt"), ("assistant", "answered on alt")])
    app.state.budget_db.append_chat_leg(old_key, new_key)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    # The reader last saw leg 0's event 2, then the socket dropped. It reconnects
    # through the alias it was rendered with, which is the old leg's key.
    gen = _session_stream(FakeRequest(app), old_key, after_seq="2")
    try:
        pushed = ""
        for _ in range(14):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if event.startswith("event: transcript"):
                pushed = event
                break
    finally:
        await gen.aclose()

    assert 'id="chat-switch-1"' in pushed
    assert 'id="event-1.1"' in pushed
    assert 'id="event-1.2"' in pushed
    # Not what the reader already has on screen.
    stripped = pushed.replace('id="event-1.1"', "").replace('id="event-1.2"', "")
    assert 'id="event-1"' not in stripped
    assert 'id="event-2"' not in stripped
    assert "answered on test" not in pushed
    # The cursor handed back names the leg, so the next reconnect resumes here.
    assert "\nid: 1.2\n" in pushed
    assert chat.chat_id


@pytest.mark.browser
async def test_chained_chat_reads_as_one_transcript_on_a_phone(tmp_path):
    """The whole point, on the screen the deck is actually used on."""
    app, chat, old_key, _current_key = _chained_chat_app(tmp_path)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/chats/{chat.chat_id}")
        await expect(page.locator("#event-1 .ev-text")).to_contain_text(
            "first question"
        )
        await expect(page.locator("#event-1\\.1 .ev-text")).to_contain_text(
            "second question"
        )
        await expect(page.locator(".leg-switch")).to_have_count(1)
        # Both accounts, on one row that does not read as a message and does not
        # wrap on a phone (#1267). The leg's cost left with spend (#2864).
        await expect(page.locator("#chat-switch-1")).to_contain_text("test")
        await expect(page.locator("#chat-switch-1")).to_contain_text("alt")
        await expect(page.locator("#chat-switch-1 .leg-switch-spend")).to_have_count(0)
        seam = await page.locator("#chat-switch-1").bounding_box()
        assert seam["width"] <= 390
        assert seam["height"] < 30
        # The old leg's anchor is the old leg's message, not the current leg's
        # message that happens to share native seq 1 (#1254).
        assert await page.evaluate(
            "document.querySelector('#event-1').dataset.eventSessionKey"
        ) == old_key
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(
                    Path(screenshot_dir) / "issue-1265-continuous-transcript-mobile.png"
                ),
                full_page=True,
            )
        await page.close()
        await browser.close()


async def test_a_second_attached_board_reuses_the_first_board_s_render(tmp_path, monkeypatch):
    """#1357: an idle attached board cost a full private re-render of the topbar.

    Every attached client wakes from the same bus publication, and each one used
    to rebuild byte-identical HTML — the topbar meters on every host sample, and
    once the whole chat list, which no stream carries any more (#2603). The renders
    are shared: N clients cost one render.
    """
    from agentdeck.web import routes_sse
    from agentdeck.web.routes_sse import _stream

    app = _app_with_state(tmp_path)

    bar_builds: list[str] = []
    real_bars = routes_sse.render_limit_bars

    def counted_bars(*args, **kwargs):
        bar_builds.append("build")
        return real_bars(*args, **kwargs)

    monkeypatch.setattr(routes_sse, "render_limit_bars", counted_bars)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            self.query_params = {}

        async def is_disconnected(self):
            return False

    state = app.state.app_state

    async def next_usage(gen):
        event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        while not event.startswith("event: usage"):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        return event

    # The three devices this deck is sized for, all on the board.
    boards = [_stream(FakeRequest(app)) for _ in range(3)]
    try:
        for gen in boards:
            await next_usage(gen)
        assert bar_builds == ["build"], "three boards, one topbar render"

        # Three host samples: every board is pushed each time, and each round costs
        # one render between them rather than one apiece.
        rounds = []
        for _ in range(3):
            state.bus.publish("host")
            rounds.append([await next_usage(gen) for gen in boards])
    finally:
        for gen in boards:
            await gen.aclose()

    for pushed in rounds:
        assert pushed[0] == pushed[1] == pushed[2], "every board sees the same bytes"
    # Four renders in total: the prime plus one per announced change. Unshared,
    # three boards over the same four events cost twelve.
    assert len(bar_builds) == 4


async def test_a_publication_still_rebuilds_the_shared_fragment(tmp_path):
    """The shared render is keyed on what the deck announced, so an announcement
    always rebuilds it. Without this the cut in #1357 would be a stale board."""
    from agentdeck.events import EventBus
    from agentdeck.web.shared_fragments import SharedFragments

    bus = EventBus()
    shared = SharedFragments(bus)
    answers = iter(["first", "second", "third"])

    def build():
        return next(answers)

    assert shared.get("f", ("sessions",), (), build) == "first"
    assert shared.get("f", ("sessions",), (), build) == "first"  # nothing announced
    bus.publish("sessions")
    assert shared.get("f", ("sessions",), (), build) == "second"  # announced: rebuilt
    # A second topic in the key invalidates it just as well...
    bus.publish("assistant")
    assert shared.get("f", ("sessions", "assistant"), (), build) == "third"
    # ...and a different per-client key never shares another client's answer.
    answers = iter(["mine"])
    assert shared.get("f", ("sessions",), ("other-chat",), build) == "mine"
    assert shared.hits == 1


async def test_the_shared_fragment_cache_stays_bounded(tmp_path):
    """``history_limit`` rides in on the query string, so a client must not be
    able to grow this cache by asking for a new window on every reconnect — and
    a session list is hundreds of kilobytes of HTML, so a superseded render has
    to be dropped rather than kept beside its replacement."""
    from agentdeck.events import EventBus
    from agentdeck.web.shared_fragments import SharedFragments

    bus = EventBus()
    shared = SharedFragments(bus, maxsize=4)
    for index in range(50):
        shared.get("f", ("sessions",), (index,), lambda value=index: value)
    assert len(shared._entries) == 4

    # A thousand announcements of the same fragment are still one entry.
    for round_index in range(1000):
        bus.publish("sessions")
        shared.get("g", ("sessions",), (), lambda value=round_index: value)
    assert len(shared._entries) == 4
    assert shared.get("g", ("sessions",), (), lambda: "unused") == 999


async def test_browser_error_endpoint_records_a_redacted_report(tmp_path, caplog):
    """An uncaught page failure reaches the server's log (#1514)."""
    app = _app_with_state(tmp_path)
    with caplog.at_level(logging.ERROR):
        async with _client(app) as client:
            response = await client.post(
                "/api/browser-errors",
                json={
                    "kind": "error",
                    "message": "boom sk-ant-api03-abcdefghijklmnop",
                    "source": "/static/sse.js",
                    "line": 7,
                    "column": 2,
                    "stack": "at reconnect (/static/sse.js:7:2)",
                    "page": "/sessions/claude_code:test:sid1",
                },
            )

    assert response.status_code == 204
    assert "browser error on /sessions/claude_code:test:sid1" in caplog.text
    assert "at reconnect" in caplog.text
    assert "sk-ant" not in caplog.text


@pytest.mark.browser
async def test_browser_forwards_uncaught_errors_and_rejections(tmp_path):
    """The real shell script posts a thrown error and a rejected promise (#1514)."""
    app = _app_with_state(tmp_path)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(service_workers="block")
        posted = []
        page.on(
            "request",
            lambda request: posted.append(request.post_data)
            if request.url.endswith("/api/browser-errors")
            else None,
        )
        await page.goto(base + "/chats")
        await page.wait_for_function("window.htmx")

        await page.evaluate("setTimeout(function () { throw new Error('thrown boom'); }, 0)")
        await page.evaluate(
            "setTimeout(function () { Promise.reject(new Error('rejected boom')); }, 0)"
        )
        # One with no message says what was rejected (#3432).
        await page.evaluate("setTimeout(function () { Promise.reject({code: 'E1'}); }, 0)")
        for _ in range(50):
            if len(posted) >= 3:
                break
            await asyncio.sleep(0.05)

        kinds = {json.loads(body)["kind"] for body in posted if body}
        messages = " ".join(json.loads(body)["message"] for body in posted if body)
        assert kinds == {"error", "unhandledrejection"}
        assert "thrown boom" in messages and "rejected boom" in messages
        assert 'Object: {"code":"E1"}' in messages
        assert all(json.loads(body)["page"].startswith("/") for body in posted if body)
        await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("path", ["/", "/sessions/claude_code:test:sid1", "/settings"])
async def test_every_page_has_a_reload_button_that_fits_a_phone(tmp_path, path):
    """Installed from the home screen the deck has no browser controls, so no
    reload and no pull-to-refresh; the topbar carries its own button (#2240). It
    must fit the narrowest phone beside everything else a chat page carries."""
    app = _app_with_state(tmp_path, with_transcript=True)

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800}, has_touch=True)
        await page.goto(base + path)
        await page.wait_for_function("window.htmx")
        button = page.get_by_role("button", name="Reload page")
        await expect(button).to_be_visible()
        await wait_until(
            page,
            "the topbar row still fits a 320px phone with the reload button in it",
            "() => { const row = document.querySelector('.topbar-row');"
            " return row.scrollWidth <= row.clientWidth; }",
        )
        box = await button.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 320
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            name = {"/": "front", "/settings": "settings"}.get(path, "chat")
            await page.screenshot(
                path=str(Path(screenshot_dir) / f"issue-2240-reload-button-{name}-320.png"),
                clip={"x": 0, "y": 0, "width": 320, "height": 140},
            )

        await page.evaluate("window.__beforeReload = true")
        async with page.expect_navigation():
            await button.tap()
        await page.wait_for_function("window.htmx")
        await expect(page.get_by_role("button", name="Reload page")).to_be_enabled()
        assert not await page.evaluate("window.__beforeReload === true")
        await browser.close()


# --- opening a chat is what clears its attention colour (#2599) ---------------


async def test_opening_a_chat_page_clears_its_cards_attention_colour(tmp_path):
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    assistant = app.state.assistant
    key = "claude_code:test:reader"
    # A chat of its own: one a Work owns is listed on its Work, not here.
    state.update_session(
        Session(
            key=key,
            account_key="claude_code:test",
            session_id="reader",
            status=SessionStatus.IDLE,
            title="Read me",
            last_role="agent",
            last_text="Finished.",
            show_when_idle=True,
            last_activity=datetime.now(UTC) - timedelta(minutes=2),
        )
    )
    await assistant.refresh()

    async with _client(app) as c:
        before = _session_card((await c.get("/chats")).text, key)
        assert "dh-finished" in before
        assert (await c.get(f"/sessions/{key}")).status_code == 200
        await assistant.refresh()
        after = _session_card((await c.get("/chats")).text, key)

    assert key in state.session_last_opened
    assert "dh-finished" not in after
    assert "dh-pill" not in after


@pytest.mark.parametrize(
    "moves, working, marked",
    [(False, False, False), (True, False, True), (True, True, False)],
)
async def test_an_open_chats_stream_advances_its_read_marker_when_the_chat_moves(
    tmp_path, monkeypatch, moves, working, marked
):
    """A turn the boss watched finish on the chat page must not light up as unread.

    Only once the chat is at rest: a working chat has no attention to clear, and
    each advance costs a durable write and a whole-deck Deckhand pass.
    """
    from agentdeck.web import routes_sse
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    app.state.assistant.session_context = lambda _session: None
    monkeypatch.setattr(routes_sse, "HEARTBEAT_S", 0.001)
    monkeypatch.setattr(routes_sse, "READ_MARK_INTERVAL_S", 0)
    moved_at = datetime.now(UTC)

    class MoveThenDisconnect:
        def __init__(self, application):
            self.app = application
            self.query_params = {}
            self.checks = 0

        async def is_disconnected(self):
            self.checks += 1
            if moves and self.checks == 2:
                state.update_session(
                    replace(
                        state.sessions[key],
                        last_activity=moved_at,
                        thinking=working,
                        status=SessionStatus.LIVE if working else SessionStatus.IDLE,
                    )
                )
            return self.checks > 4

    [event async for event in _session_stream(MoveThenDisconnect(app), key)]

    if marked:
        assert state.session_last_opened[key] >= moved_at
    else:
        assert key not in state.session_last_opened


async def test_the_done_snooze_and_pin_routes_and_scripts_are_gone(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        for action in ("handle", "unhandle", "snooze", "unsnooze", "pin", "unpin"):
            response = await c.post(
                f"/assistant/{action}",
                data={"session_key": "claude_code:test:sid1"},
                headers={"Origin": "http://t"},
            )
            assert response.status_code in {404, 405}, action
        page = await c.get("/sessions/claude_code:test:sid1")
        worker = await c.get("/sw.js")
    for script in ("deckhand_actions.js", "session_done.js"):
        assert script not in page.text
        assert script not in worker.text
    assert "data-session-done" not in page.text
    assert "deckhand-done-bar" not in page.text


async def test_opening_a_chat_the_live_window_let_go_loads_it_first(tmp_path):
    """#2841: the web process keeps only recent chats. A page for a chat the deck knows
    but has let go marks it opened and scans its account before the route reads it;
    a key the deck has never seen scans nothing."""
    from agentdeck import live_chats

    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    key = "claude_code:test:sid1"
    held = state.sessions[key]
    assert state.chat_for_session(key) is not None
    del state.sessions[key]
    live_chats._failed.clear()
    scanned: list[str] = []

    async def load_sessions(account_key, session_ids):
        scanned.append(account_key)
        assert session_ids == {"sid1"}, "just this chat, not its account"
        if key in state.live_window.opened():
            state.update_session(held)
        return True

    app.state.collector.load_sessions = load_sessions
    async with _client(app) as client:
        page = await client.get(f"/sessions/{key}")
        assert page.status_code == 200
        assert scanned == ["claude_code:test"]
        assert key in state.live_window.opened()
        again = await client.get(f"/sessions/{key}")
        assert again.status_code == 200
        assert scanned == ["claude_code:test"], "held now: no second scan"
        # Opening a chat the deck holds keeps it live too, even once its Work finishes.
        state.live_window._opened.clear()
        assert (await client.get(f"/sessions/{key}")).status_code == 200
        assert key in state.live_window.opened()
        unknown = await client.get("/sessions/claude_code:test:nobody")
        assert unknown.status_code == 404
        assert scanned == ["claude_code:test"]
        # A Work row links a chat it holds no leg of by its chat id on /sessions/
        # (#3240); that address loads the chat too, or the row's link 404s (#3293).
        del state.sessions[key]
        state.live_window._opened.clear()
        chat_id = state.chat_for_session(key).chat_id
        assert (await client.get(f"/sessions/{chat_id}?work=x")).status_code == 200
        assert scanned == ["claude_code:test", "claude_code:test"]


