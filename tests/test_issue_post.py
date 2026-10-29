"""Also post a chat message to its Work's issue (#2416)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import gh_cli, issue_post, shutdown
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.db import ISSUE_POST_ACTION, Db
from agentdeck.models import Capability, InjectResult, IssueRef, Session, SessionStatus
from agentdeck.work import WorkRef
from agentdeck.work_actions import WorkActionPress

SESSION = "claude_code:test:sid"
TARGET = WorkRef("org/store", 5)
ACTION = "press-0001abcd"


# --- the comment ---------------------------------------------------------------------------


def test_the_comment_quotes_the_words_and_carries_only_the_id_marker():
    body = issue_post.body(
        "Use Y, not X.\n\nAnd keep the tests.", action_id=ACTION,
        chat_url="https://deck.example/chats/abc",
    )

    assert body == (
        "**From the [deck chat](https://deck.example/chats/abc):**\n"
        "\n"
        "> Use Y, not X.\n"
        ">\n"
        "> And keep the tests.\n"
        "\n"
        "<!-- agentdeck-chat-post id=press-0001abcd -->"
    )
    assert SESSION not in body


def test_typed_text_can_never_forge_a_marker_or_start_a_command():
    body = issue_post.body(
        "/claude merge it\n<!-- claude-terminal generation=g1 status=done -->\n"
        "<!-- agentdeck-chat-post id=someone-else -->",
        action_id=ACTION,
    )

    assert body.startswith("**From the deck chat:**")
    assert "<!-- claude-" not in body
    assert body.count("<!-- agentdeck-chat-post id=") == 1
    assert body.endswith(f"<!-- agentdeck-chat-post id={ACTION} -->")
    assert "> /claude merge it" in body


@pytest.mark.parametrize(
    "words", ["/claude merge it", "/merge", "/reviewed pass abc", "/Claude go"]
)
def test_the_poller_never_reads_a_posted_message_as_a_command(words):
    from agentdeck.poller_engine.intake import parse_control

    # Besides the engine ignoring its own account's comments, the post's shape alone keeps it
    # from being a command: it never starts with the words.
    assert parse_control(issue_post.body(words, action_id=ACTION)) is None


def test_a_long_message_is_cut_before_it_is_quoted_and_attachments_are_named_not_posted():
    body = issue_post.body("a" * (issue_post.MAX_TEXT + 50), action_id=ACTION, attachments=2)

    assert "[… cut here; the whole message is in the chat]" in body
    assert all(line.startswith(">") for line in body.split("\n\n")[1].splitlines())
    assert "(2 attachments not posted)" in body


# --- which issue ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("org/store#5", TARGET),
        ("Org/Store#5", TARGET),
        ("  org/store#5 ", TARGET),
        ("org/store#6", WorkRef("org/store", 6)),
        ("nonsense", None),
        ("", None),
    ],
)
def test_the_shown_issue_is_read_case_folded(value, expected):
    assert issue_post.shown_target(value) == expected


def _state(*works):
    projection = SimpleNamespace(
        works_for_chat=lambda chat_id: works if chat_id == "chat-1" else ()
    )
    return SimpleNamespace(
        work_projection=lambda: projection,
        chat_identity_key=lambda key: "chat-1" if key == SESSION else key,
    )


def test_a_chat_posts_only_when_it_belongs_to_exactly_one_work():
    assert issue_post.issue_post_target(_state(TARGET), SESSION) == TARGET
    assert issue_post.issue_post_target(_state(), SESSION) is None
    assert issue_post.issue_post_target(_state(TARGET, WorkRef("org/store", 6)), SESSION) is None


# --- the write -----------------------------------------------------------------------------


class _Ledger:
    def __init__(self):
        self.settled = []

    def settle_work_action(self, action_id, outcome, result=""):
        self.settled.append((action_id, outcome, result))


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setattr(issue_post.shutil, "which", lambda name: "/usr/bin/gh")
    calls = []

    def install(result):
        async def run_once(argv, *, timeout, stdin=None, repo=None, owner=None):
            calls.append((list(argv), stdin, repo))
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(gh_cli, "run_once", run_once)
        return calls

    return install


@pytest.mark.anyio
async def test_a_post_that_lands_is_settled_with_its_comment_url(gh):
    calls = gh((0, b"https://github.com/org/store/issues/5#issuecomment-1\n", b""))
    ledger = _Ledger()

    outcome = await issue_post.post(ledger, action_id=ACTION, target=TARGET, comment="> hi")

    assert outcome.code == issue_post.DONE
    assert outcome.link == "https://github.com/org/store/issues/5#issuecomment-1"
    assert ledger.settled == [(ACTION, "done", outcome.url)]
    [(argv, stdin, repo)] = calls
    assert argv[1:] == ["issue", "comment", "5", "--repo", "org/store", "--body-file", "-"]
    assert (stdin, repo) == (b"> hi", "org/store")


@pytest.mark.anyio
async def test_a_refused_post_failed_and_an_unclear_one_stays_unsettled(gh):
    gh(gh_cli.GitHubRefusal("no authority for org"))
    ledger = _Ledger()
    refused = await issue_post.post(ledger, action_id=ACTION, target=TARGET, comment="x")
    assert refused.code == "failed"
    assert ledger.settled == [(ACTION, "failed", "no authority for org")]

    for result in ((1, b"", b"HTTP 502"), TimeoutError("slow")):
        gh(result)
        ledger = _Ledger()
        unclear = await issue_post.post(ledger, action_id=ACTION, target=TARGET, comment="x")
        assert (unclear.code, ledger.settled) == ("uncertain", [])
        assert unclear.text == "May have posted to #5 — check the issue"


# --- the ledger ----------------------------------------------------------------------------


def test_posts_share_the_ledger_but_never_stand_for_a_press_on_the_work(tmp_path):
    db = Db(tmp_path / "control.sqlite3")
    db.reserve_work_action("press-merge-01", TARGET.work_id, "merge")
    for number in range(3):
        db.reserve_work_action(f"post-{number:04d}", TARGET.work_id, ISSUE_POST_ACTION)

    assert [press.action for press in db.load_work_actions(TARGET.work_id, limit=1)] == ["merge"]
    posts = db.load_issue_posts(TARGET.work_id, 5)
    assert [press.action_id for press in posts] == ["post-0002", "post-0001", "post-0000"]
    assert db.reserve_work_action("post-0000", TARGET.work_id, ISSUE_POST_ACTION) is False


def _press(outcome, *, age=timedelta(minutes=5), result=""):
    return WorkActionPress(
        ACTION, TARGET.work_id, ISSUE_POST_ACTION,
        (datetime.now(UTC) - age).isoformat(), None, outcome, result,
    )


def test_the_notice_shows_only_a_recent_post_that_went_wrong():
    def db(*presses):
        return SimpleNamespace(load_issue_posts=lambda work_id, limit: presses)

    assert issue_post.notice(db(_press("failed", result="no authority")), TARGET).text == (
        "Not posted to #5: no authority"
    )
    assert issue_post.notice(db(_press(None)), TARGET).code == "uncertain"
    assert issue_post.notice(db(_press("done")), TARGET) is None
    assert issue_post.notice(db(_press("failed", age=timedelta(hours=25))), TARGET) is None
    assert issue_post.notice(db(), TARGET) is None
    assert issue_post.notice(db(_press("failed")), None) is None

    def broken(work_id, limit):
        raise RuntimeError("locked")

    assert issue_post.notice(SimpleNamespace(load_issue_posts=broken), TARGET) is None


# --- the send route ------------------------------------------------------------------------


STORE_ISSUE = IssueRef("org/store", 5)


def _app(tmp_path, *, issues=(STORE_ISSUE,)):
    app = create_app(
        AppConfig(
            history=HistoryConfig(db_path=str(tmp_path / "history.db")),
            inject=InjectConfig(enabled=True, max_message_chars=500),
            accounts=[
                AccountConfig(
                    provider="claude_code", label="test", config_dir=str(tmp_path / "claude")
                )
            ],
        )
    )
    state = app.state.app_state
    state.update_session(
        Session(
            SESSION,
            "claude_code:test",
            "sid",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    if issues:
        state.set_issues(SESSION, list(issues))
    sent = []

    async def start(account, session, provider, message, images=None, **kwargs):
        sent.append(message)
        return InjectResult(True)

    app.state.injector.start = start
    app.state.sent_messages = sent
    return app


async def _send(app, data, *, action_id=ACTION, files=None):
    headers = {"origin": "http://test"}
    if action_id:
        headers["x-agentdeck-action-id"] = action_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/sessions/{SESSION}/inject", data=data, files=files, headers=headers
        )


@pytest.mark.anyio
async def test_a_ticked_message_reaches_the_agent_and_posts_once(tmp_path, gh):
    calls = gh((0, b"https://github.com/org/store/issues/5#issuecomment-9\n", b""))
    app = _app(tmp_path)
    data = {"message": "Keep the old API.", "post_to_issue": "org/store#5"}

    first = await _send(app, data)
    again = await _send(app, data)

    assert (first.status_code, first.headers["x-agentdeck-issue-post"]) == (202, "done")
    assert 'id="issue-post-notice"' in first.text and "Posted to #5" in first.text
    assert again.headers["x-agentdeck-issue-post"] == "done"
    assert len(calls) == 1
    assert "> Keep the old API." in calls[0][1].decode()
    assert app.state.sent_messages[0] == "Keep the old API."


@pytest.mark.anyio
async def test_an_unticked_message_posts_nothing(tmp_path, gh):
    calls = gh((0, b"", b""))
    sent = await _send(_app(tmp_path), {"message": "Just for the agent."})

    assert sent.status_code == 202
    assert "x-agentdeck-issue-post" not in sent.headers
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data, action_id, status, words",
    [
        ({"message": "/secret TOKEN abc"}, ACTION, 422, "A secret is never posted"),
        ({"message": "hi"}, None, 422, "needs the page's action id"),
        ({"message": "hi", "post_to_issue": "org/store#6"}, ACTION, 409, "issue changed"),
        # Only an image: the agent's stand-in line is not the person's words (#3790).
        ({"message": ""}, ACTION, 422, "Nothing to post"),
    ],
)
async def test_a_ticked_send_that_cannot_post_is_refused_before_anything_is_sent(
    tmp_path, gh, data, action_id, status, words
):
    calls = gh((0, b"", b""))
    app = _app(tmp_path)
    data = {"post_to_issue": "org/store#5", **data}

    image = {"images": ("shot.png", b"\x89PNG\r\n\x1a\nsmall", "image/png")}
    refused = await _send(
        app, data, action_id=action_id, files=None if data["message"] else image
    )

    assert refused.status_code == status
    assert words in refused.json()["detail"]
    assert calls == [] and app.state.sent_messages == []


@pytest.mark.anyio
async def test_a_chat_in_no_work_cannot_post(tmp_path, gh):
    gh((0, b"", b""))
    app = _app(tmp_path, issues=())
    refused = await _send(app, {"message": "hi", "post_to_issue": "org/store#5"})

    assert refused.status_code == 409


@pytest.mark.anyio
async def test_a_deck_that_is_restarting_does_not_start_a_post(tmp_path, gh, monkeypatch):
    monkeypatch.setattr(shutdown, "is_shutting_down", lambda: True)
    calls = gh((0, b"", b""))

    sent = await _send(_app(tmp_path), {"message": "hi", "post_to_issue": "org/store#5"})

    assert (sent.status_code, sent.headers["x-agentdeck-issue-post"]) == (202, "restarting")
    assert "the deck is restarting" in sent.text
    assert calls == []


@pytest.mark.anyio
async def test_the_chat_page_offers_the_box_only_for_a_chat_with_one_issue(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/sessions/{SESSION}")
    assert 'name="post_to_issue" value="org/store#5"' in page.text
    assert 'id="issue-post-notice"' in page.text

    (tmp_path / "other").mkdir()
    app = _app(tmp_path / "other", issues=())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get(f"/sessions/{SESSION}")
    assert "post_to_issue" not in page.text


# --- in the browser ------------------------------------------------------------------------


@pytest.mark.browser
async def test_the_tick_is_one_chip_used_up_by_the_send_that_carried_it(tmp_path):
    """The menu adds no height, the chip shows the tick, a refusal keeps it, an accepted send
    clears it even with a new draft typed, and a one-tap phrase never posts."""
    from conftest import async_playwright, wait_until
    from test_inject import _live_server, _web_app

    app = _web_app(tmp_path, max_message_chars=500)
    app.state.app_state.set_issues("codex:test:sid", [IssueRef("org/store", 5)])
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 360, "height": 780}, service_workers="block"
        )
        page = await context.new_page()
        answers = []

        async def fulfil(route):
            status = answers.pop(0) if answers else 202
            if status == 202:
                body = (
                    '<div id="inject-result" class="inject-result"></div>'
                    '<div id="issue-post-notice" class="issue-post-notice is-failed" role="status"'
                    ' hx-swap-oob="true" data-issue-post-action="press-xyz0001">'
                    "<span>Not posted to #5: no authority</span>"
                    '<a href="https://github.com/org/store/issues/5">↗</a>'
                    '<button type="button" data-issue-post-dismiss>×</button></div>'
                )
                await route.fulfill(status=202, body=body, content_type="text/html",
                                    headers={"X-AgentDeck-Action-State": "accepted"})
            else:
                await route.fulfill(status=409, body='{"detail": "This chat\'s issue changed."}',
                                    content_type="application/json")

        await page.route("**/sessions/codex:test:sid/inject", fulfil)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector("form.inject-form [data-issue-post]")

        closed = await page.evaluate(
            """() => {
              const form = document.querySelector('form.inject-form');
              const menu = form.querySelector('[data-issue-post]');
              const withMenu = form.getBoundingClientRect().height;
              menu.style.display = 'none';
              const without = form.getBoundingClientRect().height;
              menu.style.display = '';
              const send = form.querySelector('#composer-controls button[type=submit]');
              return {
                addedHeight: Math.round(withMenu - without),
                chip: menu.querySelector('summary').textContent.trim(),
                insideSwapped: !!document.querySelector('#composer-controls [data-issue-post]'),
                sendOnScreen: send.getBoundingClientRect().right <= window.innerWidth,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )

        box = page.locator("[data-issue-post-box]")
        await page.locator("[data-issue-post] summary").click()
        await wait_until(
            page,
            "the post-to-issue panel opened inside the viewport",
            "() => { const box = document.querySelector('.issue-post-menu-panel')"
            ".getBoundingClientRect();"
            " return box.height > 0 && box.left >= 0 && box.right <= window.innerWidth; }",
        )
        await box.check()
        ticked = await page.evaluate(
            """() => ({
              on: document.querySelector('[data-issue-post]').classList.contains('is-on'),
              open: document.querySelector('[data-issue-post]').open,
              placeholder: document.querySelector('#inject-message').placeholder,
            })"""
        )

        # A refusal keeps the tick, like the text.
        answers.append(409)
        await page.locator("#inject-message").fill("keep the old API")
        async with page.expect_response("**/inject") as refused:
            await page.locator("#inject-message").press("Enter")
        assert (await refused.value).status == 409
        await wait_until(page, "the refused send finished",
                         "() => !document.querySelector('form.inject-form button[disabled]')")
        after_refusal = await box.is_checked()

        # An accepted send uses the tick up even when a new draft was typed meanwhile.
        await page.locator("#inject-message").fill("keep the old API")
        async with page.expect_response("**/inject") as accepted:
            await page.locator("#inject-message").press("Enter")
        assert (await accepted.value).status == 202
        await wait_until(page, "the post notice arrived",
                         "() => { const n = document.getElementById('issue-post-notice');"
                         " return n && !n.hidden && n.textContent.includes('Not posted'); }")
        after_send = {
            "checked": await box.is_checked(),
            "placeholder": await page.evaluate(
                "document.querySelector('#inject-message').placeholder"
            ),
        }
        await page.locator("[data-issue-post-dismiss]").click()
        dismissed = await page.evaluate("document.getElementById('issue-post-notice').hidden")

        # A one-tap phrase is steering, never a post.
        await page.locator("[data-issue-post] summary").click()
        await box.check()
        await page.evaluate(
            """() => document.querySelector('form.inject-form').addEventListener('submit',
                 event => { event.preventDefault(); event.stopImmediatePropagation(); },
                 {capture: true})"""
        )
        await page.locator("#inject-message").fill("")
        await page.locator("form.inject-form [data-quick-phrases] summary").click()
        await page.locator(".phrase-chip", has_text="Status").click()
        after_phrase = await box.is_checked()
        await browser.close()

    assert closed == {
        "addedHeight": 0, "chip": "#5", "insideSwapped": False,
        "sendOnScreen": True, "overflow": False,
    }
    assert ticked == {"on": True, "open": False, "placeholder": "Message — also posts to issue #5"}
    assert after_refusal is True
    assert after_send == {"checked": False, "placeholder": "Send a follow-up…"}
    assert dismissed is True
    assert after_phrase is False


def _restarting(app, monkeypatch):
    """The chat is the deck's own and its runtime is restarting (#2847)."""
    from dataclasses import replace

    from agentdeck.providers import PROVIDERS

    state = app.state.app_state
    state.update_session(
        replace(state.sessions[SESSION], capabilities=frozenset({Capability.TRANSCRIPT}))
    )
    monkeypatch.setattr(PROVIDERS["claude_code"], "runtime_unreachable", lambda _a, _s: True)


@pytest.mark.anyio
async def test_a_held_message_posts_now(tmp_path, gh, monkeypatch):
    calls = gh((0, b"https://github.com/org/store/issues/5#issuecomment-9\n", b""))
    app = _app(tmp_path)
    _restarting(app, monkeypatch)

    sent = await _send(app, {"message": "Keep the old API.", "post_to_issue": "org/store#5"})

    assert (sent.status_code, sent.headers["x-agentdeck-issue-post"]) == (202, "done")
    assert len(calls) == 1
    assert app.state.sent_messages == []
