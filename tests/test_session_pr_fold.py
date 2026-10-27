"""#2711: finished pull requests fold behind one chip under the transcript."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import async_playwright
from test_web import _app_with_state, _client, _live_server, _session_action_bar

from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import IssueRef
from agentdeck.poller_ops import BoardCard, BoardView
from agentdeck.web.render import fold_pull_requests

KEY = "claude_code:test:sid1"


def _pull(number, status="merged", draft=False):
    return PullRequestContext(
        repository="eerovil/store",
        number=number,
        title=f"Pull {number}",
        url=f"https://github.com/eerovil/store/pull/{number}",
        status=status,
        draft=draft,
    )


def test_two_or_more_finished_pull_requests_fold_and_open_ones_stay():
    pulls = (_pull(1), _pull(2, "open"), _pull(3), _pull(4, "open", draft=True), _pull(5))

    shown, fold = fold_pull_requests(pulls)

    assert [pull.number for pull in shown] == [2, 4]
    assert fold is not None and [pull.number for pull in fold.pulls] == [1, 3, 5]
    assert fold.label == "3 PRs merged"


def test_one_finished_pull_request_is_not_folded():
    pulls = (_pull(1), _pull(2, "open"))

    assert fold_pull_requests(pulls) == (pulls, None)
    assert fold_pull_requests(()) == ((), None)


def test_the_fold_names_its_status():
    assert fold_pull_requests((_pull(1, "closed"), _pull(2, "closed")))[1].label == "2 PRs closed"
    mixed = fold_pull_requests((_pull(1, "closed"), _pull(2)))[1]
    assert (mixed.label, mixed.status) == ("2 PRs done", "done")


def test_an_unknown_status_is_never_folded():
    # `is_terminal` is an explicit set, so a status GitHub invents later stays a chip.
    pulls = (_pull(1), _pull(2), _pull(3, "queued"))

    shown, fold = fold_pull_requests(pulls)

    assert [pull.number for pull in shown] == [3]
    assert [pull.number for pull in fold.pulls] == [1, 2]


async def test_the_session_page_folds_finished_pull_requests(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.assistant._contexts.update(
        {
            KEY: GitContext(
                "eerovil/store",
                "agent/issue-502",
                False,
                (_pull(3487), _pull(3485), _pull(3410), _pull(3500, "open")),
            )
        }
    )

    async with _client(app) as client:
        response = await client.get(f"/sessions/{KEY}")

    action_bar = " ".join(_session_action_bar(response.text).split())
    assert f'<details class="pr-fold" data-keep-open="related-prs:{KEY}">' in action_bar
    assert "3 PRs merged</summary>" in action_bar
    head, _, folded = action_bar.partition('<details class="pr-fold"')
    assert "PR #3500 · open" in head and "PR #3487" not in head
    for number in (3487, 3485, 3410):
        assert f"PR #{number} · merged" in folded
    assert "<details open" not in action_bar


@pytest.mark.browser
async def test_an_opened_fold_stays_open_through_a_push(tmp_path, monkeypatch):
    # The row is one live fragment, replaced whole whenever anything in it changes; an open
    # fold must not snap shut under the reader, and closed it adds no height to the row.
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state
    state.add_issue(KEY, IssueRef("eerovil/issues", 502))
    state.update_session(
        replace(
            state.sessions[KEY],
            issue_url="https://github.com/eerovil/issues/issues/502",
            issue_status="open",
            issue_status_kind="open",
        )
    )
    context = GitContext(
        "eerovil/store",
        "agent/issue-502",
        False,
        (_pull(3487), _pull(3485), _pull(3410), _pull(3501, "open")),
    )
    app.state.assistant._contexts.update({KEY: context})

    async def resolve(sessions, *, force=False):
        return {session.key: context for session in sessions}

    # The stream re-resolves the chat's git context; keep it the one above, not this host's.
    monkeypatch.setattr(app.state.assistant.context_resolver, "resolve", resolve)
    project = {"status": "In progress"}
    app.state.issues.read_board = lambda: BoardView(
        cards={
            "eerovil/issues#502": BoardCard(
                repository="eerovil/issues", number=502, status=project["status"]
            )
        }
    )
    shots = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            """
            window.__sseEvents = [];
            document.addEventListener('DOMContentLoaded', () => {
              document.body.addEventListener('htmx:sseMessage', (event) => {
                window.__sseEvents.push(event.detail && event.detail.type);
              });
            });
            """
        )
        await page.goto(f"{base}/sessions/{KEY}")
        # The stream's first update re-pushes every fragment, this row included; measured
        # before it lands, the summary can be swapped out between lookup and measurement.
        await page.wait_for_function("() => window.__sseEvents.includes('related-links')")
        fold = page.locator("#session-related-links details.pr-fold")
        summary = fold.locator("summary")
        await summary.wait_for(state="visible")
        assert await summary.inner_text() == "3 PRs merged"
        assert await fold.evaluate("el => el.open") is False
        open_pr = page.locator("#session-related-links a.pr-link.pr-open")
        closed_height = (await summary.bounding_box())["height"]
        assert abs(closed_height - (await open_pr.bounding_box())["height"]) < 1
        # The ChatGPT review chip left chat pages (#2864).
        assert await page.locator("#session-related-links .chat-chip").count() == 0
        bar = page.locator("#session-action-bar")
        if shots:
            await bar.screenshot(path=str(Path(shots) / "issue-2711-folded.png"))

        await summary.click()
        merged = fold.locator("a.pr-link.pr-merged")
        await merged.first.wait_for(state="visible")
        assert await merged.count() == 3
        if shots:
            await bar.screenshot(path=str(Path(shots) / "issue-2711-opened.png"))

        project["status"] = "In review"
        # The board watch announces a card move (#2865); the page does not re-read on a tick.
        app.state.app_state.bus.publish("board")
        issue = page.locator("#session-related-links .chip.issue-link")
        await issue.get_by_text("In review", exact=False).wait_for(state="visible")
        assert await page.locator("#session-related-links details.pr-fold").evaluate(
            "el => el.open"
        )
        await merged.first.wait_for(state="visible")
        await browser.close()
