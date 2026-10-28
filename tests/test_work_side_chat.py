"""A side chat started from a Work's page (#2388).

It runs in the Work's checkout, is linked to the Work once it exists, and is never a Run:
no Run trace row, no ``session_run_bindings`` row, so the Poller never takes it for the
card's worker. The press's action id is also the chat's session token, so a replay never
starts a second chat.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck.models import (
    Capability,
    InjectResult,
    IssueRef,
    PullRequestRef,
    Session,
    SessionStatus,
)
from agentdeck.poller_ops import BoardScope, BoardView
from agentdeck.work import WorkBinding, WorkChatFacts, WorkEvidence, WorkRef, build_work_projection
from agentdeck.work_overview import work_href
from agentdeck.work_side_chat import (
    context_line,
    first_message,
    local_repository_checkout,
    prefilled_account,
    work_checkout,
    work_pull_requests,
)
from agentdeck.work_workspace import (
    CwdObservation,
    PathObservation,
    RepositoryRole,
    WorkPullRequest,
    WorkRepositoryContext,
    WorktreeObservation,
    WorkWorkspace,
)

WORK = WorkRef("eerovil/agentdeck", 2388)
NOW = datetime.now(UTC)
ACCOUNT = "claude_code:test"


def _repo(path, remote: str = "https://github.com/eerovil/agentdeck.git"):
    path.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "master"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([*git, "remote", "add", "origin", remote], check=True)
    return path


# --- the pure pieces -------------------------------------------------------------------------


def test_a_side_chat_binding_is_direct_membership_with_no_run():
    projection = build_work_projection(
        [
            WorkChatFacts(
                chat_id="c:side",
                bindings=(WorkBinding("c:side", WORK.work_id, WorkEvidence.SIDE_CHAT),),
            )
        ]
    )
    work = projection.get(WORK)
    assert work is not None and work.chat_ids == ("c:side",)
    (membership,) = work.memberships
    assert membership.direct
    assert [p.evidence for p in membership.provenance] == [WorkEvidence.SIDE_CHAT]


def test_the_context_line_names_the_work_and_its_pull_requests_only():
    issue = IssueRef("eerovil/agentdeck", 123)
    assert context_line(issue, ()) == "Context: Work for eerovil/agentdeck#123."
    pulls = (PullRequestRef("eerovil/agentdeck", 456), PullRequestRef("eerovil/agentdeck", 457))
    assert (
        context_line(issue, pulls) == "Context: Work for eerovil/agentdeck#123 (PR #456, PR #457)."
    )
    other = (PullRequestRef("EeroVil/agent-evidence", 9),)
    assert context_line(issue, other) == (
        "Context: Work for eerovil/agentdeck#123 (PR EeroVil/agent-evidence#9)."
    )
    assert first_message(issue, (), "  look at it \n") == (
        "Context: Work for eerovil/agentdeck#123.\n\nlook at it"
    )


def _seen(path: str, repository: str = "eerovil/agentdeck", exists: bool = True) -> PathObservation:
    return PathObservation(
        path, exists=exists, toplevel=path if exists else "", repository=repository
    )


def _workspace(*repos: WorkRepositoryContext, primary: str = "") -> WorkWorkspace:
    return WorkWorkspace(work_id=WORK.work_id, repositories=repos, primary=primary)


def test_the_work_checkout_is_a_code_worktree_first_and_never_an_artifact_or_a_gone_path():
    artifacts = WorkRepositoryContext(
        "eerovil/agent-evidence",
        roles=(RepositoryRole.ARTIFACT,),
        worktrees=(WorktreeObservation("/a", True, _seen("/a"), ()),),
    )
    code = WorkRepositoryContext(
        "eerovil/agentdeck",
        roles=(RepositoryRole.SOURCE,),
        worktrees=(
            WorktreeObservation("/gone", True, _seen("/gone", exists=False), ()),
            WorktreeObservation("/wt", True, _seen("/wt"), ()),
        ),
        cwds=(CwdObservation("k", "c", "/chat", _seen("/chat"), None),),
    )
    assert work_checkout(_workspace(artifacts, code), WORK) == "/wt"
    only_cwd = WorkRepositoryContext("eerovil/agentdeck", roles=(), cwds=code.cwds)
    assert work_checkout(_workspace(artifacts, only_cwd), WORK) == "/chat"
    assert work_checkout(_workspace(artifacts), WORK) == ""
    assert work_checkout(None, WORK) == ""


def test_the_workspace_pull_requests_are_listed_once_each():
    pull = WorkPullRequest("eerovil/agentdeck", 5, "https://github.com/eerovil/agentdeck/pull/5")
    again = WorkPullRequest("EeroVil/AgentDeck", 5, "https://github.com/EeroVil/AgentDeck/pull/5")
    repo = WorkRepositoryContext("eerovil/agentdeck", roles=(), pull_requests=(pull, again))
    assert work_pull_requests(_workspace(repo)) == (PullRequestRef("eerovil/agentdeck", 5),)
    assert work_pull_requests(None) == ()


def test_a_local_checkout_is_a_main_checkout_of_the_works_repository(tmp_path):
    other = _repo(tmp_path / "other", "https://github.com/eerovil/trailhead.git")
    ours = _repo(tmp_path / "agentdeck")
    (ours / "src").mkdir()
    candidates = [str(other), "", str(ours / "src"), str(tmp_path / "missing"), str(ours)]
    assert local_repository_checkout(WORK, candidates) == str(ours.resolve())
    assert local_repository_checkout(WORK, [str(other)]) == ""


class _Chat:
    def __init__(self, key: str, minutes_ago: int | None):
        self.session_key = key
        self.last_activity = NOW - timedelta(minutes=minutes_ago) if minutes_ago else None


class _Leg:
    def __init__(self, account_key: str):
        self.account_key = account_key


def test_the_account_is_the_latest_chats_when_it_can_start_one():
    sessions = {"a:x:1": _Leg("a:x"), "b:y:2": _Leg("b:y")}
    chats = [_Chat("a:x:1", 30), _Chat("b:y:2", 5)]
    assert prefilled_account(chats, sessions, ["a:x", "b:y"]) == "b:y"
    # The latest chat's account cannot start one: the deck's default, not an older chat's.
    assert prefilled_account(chats, sessions, ["c:z", "a:x"]) == "c:z"
    assert prefilled_account([], sessions, ["a:x"]) == "a:x"
    assert prefilled_account(chats, sessions, []) == ""


# --- linking once the chat exists ------------------------------------------------------------


def _app(tmp_path, monkeypatch, *, cwd=None, starts=None):
    from test_web import _app_with_state

    from agentdeck.providers import PROVIDERS

    app = _app_with_state(tmp_path)
    board = BoardView(scopes=(BoardScope("agentdeck", org="eerovil"),))
    app.state.issues.read_board = lambda: board
    app.state.issues.cached_issues = lambda refs: {}
    app.state.config.inject.enabled = True
    state = app.state.app_state
    state.update_session(
        Session(
            key=f"{ACCOUNT}:worker",
            account_key=ACCOUNT,
            session_id="worker",
            status=SessionStatus.IDLE,
            last_activity=NOW,
            title="Worker chat",
            cwd=cwd,
            capabilities=frozenset({Capability.INJECT}),
        )
    )
    state.set_issues(f"{ACCOUNT}:worker", (IssueRef(WORK.repository, WORK.number),))
    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: True)
    if starts is not None:
        count = iter(range(1, 1000))

        async def start_session(account, cwd, message, *, timeout_s, session_token=None, **kw):
            starts.append((str(cwd), message, session_token))
            return InjectResult(True, session_id=f"side{next(count)}")

        monkeypatch.setattr(PROVIDERS["claude_code"], "start_session", start_session)
    return app


def _scanned(app, session_id: str, cwd=None) -> str:
    key = f"{ACCOUNT}:{session_id}"
    app.state.app_state.update_session(
        Session(
            key=key,
            account_key=ACCOUNT,
            session_id=session_id,
            status=SessionStatus.IDLE,
            last_activity=NOW,
            title="Side chat",
            cwd=cwd,
            capabilities=frozenset({Capability.INJECT}),
        )
    )
    return key


def test_a_pending_side_chat_is_linked_once_it_exists_and_then_forgotten(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    state, control = app.state.app_state, app.state.control_db
    pull = "https://github.com/eerovil/agentdeck/pull/2400"
    assert control.record_work_side_chat("press-0001", WORK.work_id, [pull])

    # Not named yet: nothing happens and the row stays.
    assert state.finalize_work_side_chats() == 0
    assert [row.action_id for row in control.pending_work_side_chats()] == ["press-0001"]

    key = _scanned(app, "side1")
    state.bind_session_token("press-0001", key)
    assert state.finalize_work_side_chats() == 1
    assert control.pending_work_side_chats() == []
    assert state.issues_for(key) == (IssueRef("eerovil/agentdeck", 2388),)
    assert state.pull_requests_for(key) == (PullRequestRef("eerovil/agentdeck", 2400),)
    evidence = {(row.session_key, row.evidence) for row in control.session_work_bindings()}
    assert (key, "side_chat") in evidence
    # Never a Run: no binding a Run reader could see.
    assert control.session_run_bindings(session_key=key) == []
    assert control.latest_session_run(key) is None
    work = state.work_projection().get(WORK)
    assert state.chat_identity_key(key) in work.chat_ids
    # Linking again is a no-op.
    assert state.finalize_work_side_chats() == 0


def test_a_crash_before_the_delete_links_again_without_duplicates(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    state, control = app.state.app_state, app.state.control_db
    key = _scanned(app, "side1")
    state.bind_session_token("press-0002", key)
    control.record_work_side_chat("press-0002", WORK.work_id, [])
    monkeypatch.setattr(control, "delete_work_side_chat", lambda action_id: None)
    assert state.finalize_work_side_chats() == 1
    assert state.finalize_work_side_chats() == 1
    assert state.issues_for(key) == (IssueRef("eerovil/agentdeck", 2388),)
    assert [row.evidence for row in control.session_work_bindings() if row.session_key == key] == [
        "side_chat"
    ]


def test_a_start_nothing_ever_named_is_dropped_after_a_day(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    state, control = app.state.app_state, app.state.control_db
    control.record_work_side_chat("press-0003", WORK.work_id, [])
    assert state.finalize_work_side_chats(now=NOW + timedelta(hours=23)) == 0
    assert len(control.pending_work_side_chats()) == 1
    assert state.finalize_work_side_chats(now=NOW + timedelta(hours=25)) == 0
    assert control.pending_work_side_chats() == []


# --- the route -------------------------------------------------------------------------------


async def _settle(app):
    for _ in range(50):
        await asyncio.sleep(0)
    tasks = [task for task in app.state.injector._new_tasks.values() if not task.done()]
    if tasks:
        await asyncio.gather(*tasks)


async def _post(app, data):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(work_href(WORK.work_id) + "/new-chat", data=data)
        await _settle(app)
        return response


def _press(action_id: str, message: str = "Find why the flaky test flakes") -> dict:
    return {"message": message, "account_key": ACCOUNT, "client_action_id": action_id}


@pytest.mark.asyncio
async def test_new_chat_starts_once_in_the_works_checkout_and_joins_the_work(tmp_path, monkeypatch):
    checkout = _repo(tmp_path / "checkout")
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=checkout, starts=starts)
    state = app.state.app_state

    page = await asyncio.to_thread(
        lambda: (
            __import__("fastapi.testclient").testclient.TestClient(app).get(work_href(WORK.work_id))
        )
    )
    assert "data-work-new-chat-open" in page.text
    assert f'data-work-new-chat-folder="{checkout.resolve()}"' in page.text

    first = await _post(app, _press("side-chat-0001"))
    assert first.status_code == 202, first.text
    assert first.headers["X-AgentDeck-New-Chat"] == "started"
    assert 'id="work-new-chat-result"' in first.text
    # A double press, and a retry with different text, are the same press.
    again = await _post(app, _press("side-chat-0001", "something else"))
    assert again.status_code == 202 and again.headers["X-AgentDeck-New-Chat"] == "started"
    assert len(starts) == 1
    cwd, message, token = starts[0]
    assert cwd == str(checkout.resolve())
    assert token == "side-chat-0001"
    assert message == (
        "Context: Work for eerovil/agentdeck#2388.\n\nFind why the flaky test flakes"
    )

    # The next scan names it; the collector links it to the Work, never to a Run.
    key = _scanned(app, "side1", cwd=checkout)
    assert state.session_key_for_token("side-chat-0001") == key
    assert state.finalize_work_side_chats() == 1
    assert state.issues_for(key) == (IssueRef("eerovil/agentdeck", 2388),)
    assert app.state.control_db.session_run_bindings(session_key=key) == []
    html = await asyncio.to_thread(
        lambda: (
            __import__("fastapi.testclient")
            .testclient.TestClient(app)
            .get(work_href(WORK.work_id))
            .text
        )
    )
    chats = html.split('id="work-chats"', 1)[1].split("</details>", 1)[0]
    assert "Side chat" in chats
    inspector = html.split('id="work-inspector"', 1)[1]
    assert "side chat" in inspector
    assert 'id="work-runs"' not in html  # the Runs section went with the Poller's Runs

    # A new deliberate press is a new chat.
    assert (await _post(app, _press("side-chat-0002"))).status_code == 202
    assert len(starts) == 2
    await app.state.injector.stop()


@pytest.mark.asyncio
async def test_with_no_checkout_anywhere_nothing_is_started(tmp_path, monkeypatch):
    starts: list = []
    app = _app(tmp_path, monkeypatch, starts=starts)
    response = await _post(app, _press("side-chat-0003"))
    assert response.status_code == 409
    assert "No checkout of eerovil/agentdeck on this host" in response.text
    assert starts == [] and app.state.control_db.pending_work_side_chats() == []
    # Nothing was reserved, so the same press may go again once a checkout exists.
    assert app.state.control_db.work_action("side-chat-0003") is None


@pytest.mark.asyncio
async def test_a_local_checkout_of_the_repository_is_the_fallback(tmp_path, monkeypatch):
    local = _repo(tmp_path / "agentdeck")
    starts: list = []
    app = _app(tmp_path, monkeypatch, starts=starts)
    app.state.config.inject.new_chat_default_cwd = str(local)
    assert (await _post(app, _press("side-chat-0004"))).status_code == 202
    assert [cwd for cwd, _, _ in starts] == [str(local.resolve())]
    await app.state.injector.stop()


@pytest.mark.asyncio
async def test_an_undeliverable_first_message_is_refused_before_anything(tmp_path, monkeypatch):
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=_repo(tmp_path / "checkout"), starts=starts)
    response = await _post(app, _press("side-chat-0005", "Stuck?"))
    assert response.status_code == 422 and "Not started:" in response.text
    assert starts == [] and app.state.control_db.work_action("side-chat-0005") is None


@pytest.mark.asyncio
async def test_a_refused_start_is_final_for_its_press_and_a_new_press_may_try(
    tmp_path, monkeypatch
):
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=_repo(tmp_path / "checkout"), starts=starts)
    injector = app.state.injector
    real = injector.start_new
    calls: list[str] = []

    async def busy(*args, **kwargs):
        calls.append(kwargs["session_token"])
        return InjectResult(False, "a new session is already starting for this account")

    monkeypatch.setattr(injector, "start_new", busy)
    refused = await _post(app, _press("side-chat-0006"))
    assert refused.status_code == 409 and "already starting" in refused.text
    assert app.state.control_db.pending_work_side_chats() == []
    replay = await _post(app, _press("side-chat-0006"))
    assert replay.status_code == 409 and "did not start a chat" in replay.text
    assert calls == ["side-chat-0006"]

    monkeypatch.setattr(injector, "start_new", real)
    assert (await _post(app, _press("side-chat-0007"))).status_code == 202
    assert [token for _, _, token in starts] == ["side-chat-0007"]
    await injector.stop()


@pytest.mark.asyncio
async def test_a_press_without_a_usable_id_or_account_starts_nothing(tmp_path, monkeypatch):
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=_repo(tmp_path / "checkout"), starts=starts)
    assert (await _post(app, _press("short"))).status_code == 422
    bad = _press("side-chat-0008")
    bad["account_key"] = "codex:nobody"
    assert (await _post(app, bad)).status_code == 422
    assert starts == []


def test_no_account_able_to_start_a_chat_shows_no_control(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agentdeck.providers import PROVIDERS

    app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(PROVIDERS["claude_code"], "can_start_session", lambda account: False)
    html = TestClient(app).get(work_href(WORK.work_id)).text
    assert "data-work-new-chat-open" not in html and "data-work-new-chat" not in html


# --- in the browser --------------------------------------------------------------------------


@pytest.mark.browser
@pytest.mark.asyncio
async def test_new_chat_on_a_phone_adds_no_height_keeps_its_text_and_starts_once(
    tmp_path, monkeypatch
):
    from conftest import async_playwright
    from test_web import _live_server

    checkout = _repo(tmp_path / "checkout")
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=checkout, starts=starts)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + work_href(WORK.work_id))
        opener = page.locator("[data-work-new-chat-open]")
        await opener.wait_for()
        # Shut, the control costs the page no height at all, on a phone or a narrow one: the
        # same page with the control taken out entirely is exactly as tall.
        for width in (390, 320):
            await page.set_viewport_size({"width": width, "height": 844})
            heights = await page.evaluate(
                """() => {
                    const form = document.querySelector('#work-message');
                    const button = form.querySelector('[data-work-new-chat-open]');
                    const withIt = form.getBoundingClientRect().height;
                    const mark = button.nextSibling;
                    button.remove();
                    const without = form.getBoundingClientRect().height;
                    form.insertBefore(button, mark);
                    return [withIt, without];
                }"""
            )
            assert heights[0] == heights[1], (width, heights)
        await page.set_viewport_size({"width": 390, "height": 844})
        assert await page.locator("#work-new-chat").is_hidden()

        await opener.click()
        form = page.locator("#work-new-chat")
        assert await form.is_visible()
        await form.locator("textarea").fill("Check the flaky browser test")
        action_id = await form.locator('input[name="client_action_id"]').input_value()
        assert len(action_id) >= 8

        # A live update of the Work leaves the open box and its text alone.
        app.state.app_state.update_session(
            Session(
                key=f"{ACCOUNT}:third",
                account_key=ACCOUNT,
                session_id="third",
                status=SessionStatus.IDLE,
                last_activity=NOW,
                title="A third chat",
                capabilities=frozenset({Capability.INJECT}),
            )
        )
        app.state.app_state.set_issues(
            f"{ACCOUNT}:third", (IssueRef(WORK.repository, WORK.number),)
        )
        # The Work changing must not wipe the form; the page has no stream since #2864.
        app.state.app_state.bus.publish("sessions")
        assert await form.is_visible()
        assert await form.locator("textarea").input_value() == "Check the flaky browser test"

        # A double press is one start.
        await form.locator(".wd-new-chat-start").dblclick()
        await page.locator("#work-new-chat-slot [data-starting-state]").wait_for(timeout=15000)
        await page.wait_for_function("() => document.querySelector('#work-new-chat').hidden")
        await _settle(app)
        assert [token for _, _, token in starts] == [action_id]

        # Once a scan names it and the collector links it, it is listed on the Work.
        key = _scanned(app, "side1", cwd=checkout)
        app.state.app_state.finalize_work_side_chats()
        app.state.app_state.bus.publish("sessions")
        await page.locator("#work-new-chat-slot a", has_text="Open it").wait_for(timeout=15000)
        # A chat exists, so its first message leaves the box.
        await page.wait_for_function(
            "() => document.querySelector('#work-new-chat textarea').value === ''"
        )
        # The inspector is not a live section since #2864: the next load lists it.
        await page.reload()
        await page.locator(f'#work-inspector [data-wd-leg="{key}"]').wait_for(
            state="attached", timeout=15000
        )
        await browser.close()
    await app.state.injector.stop()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_a_start_refused_after_it_was_accepted_gives_the_text_back(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server

    from agentdeck.providers import PROVIDERS

    app = _app(tmp_path, monkeypatch, cwd=_repo(tmp_path / "checkout"))
    tokens: list = []

    async def refuse(account, cwd, message, *, timeout_s, session_token=None, **kw):
        tokens.append(session_token)
        return InjectResult(False, "the account is over its limit")

    monkeypatch.setattr(PROVIDERS["claude_code"], "start_session", refuse)
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.goto(base + work_href(WORK.work_id))
        await page.locator("[data-work-new-chat-open]").click()
        form = page.locator("#work-new-chat")
        await form.locator("textarea").fill("A long first message")
        first_id = await form.locator('input[name="client_action_id"]').input_value()
        await form.locator(".wd-new-chat-start").click()
        await page.locator('#work-new-chat-slot [data-starting-state="failed"]').wait_for(
            timeout=15000
        )
        await form.wait_for(state="visible")
        assert await form.locator("textarea").input_value() == "A long first message"
        # Trying again is a new press.
        assert await form.locator('input[name="client_action_id"]').input_value() != first_id
        assert tokens == [first_id]
        await browser.close()
    await app.state.injector.stop()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_with_no_chat_to_message_new_chat_still_adds_no_height(tmp_path, monkeypatch):
    from conftest import async_playwright
    from test_web import _live_server

    app = _app(tmp_path, monkeypatch, cwd=_repo(tmp_path / "checkout"))
    worker = app.state.app_state.sessions[f"{ACCOUNT}:worker"]
    worker.capabilities = frozenset()
    async with _live_server(app) as base, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 320, "height": 640}, service_workers="block"
        )
        await page.goto(base + work_href(WORK.work_id))
        await page.locator("#work-message [data-wd-no-target]").wait_for()
        heights = await page.evaluate(
            """() => {
                const form = document.querySelector('#work-message');
                const button = form.querySelector('[data-work-new-chat-open]');
                const withIt = form.getBoundingClientRect().height;
                button.remove();
                return [withIt, form.getBoundingClientRect().height];
            }"""
        )
        assert heights[0] == heights[1], heights
        await browser.close()


@pytest.mark.asyncio
async def test_an_account_whose_runtime_is_restarting_stays_offered_and_refuses_until_back(
    tmp_path, monkeypatch
):
    """A page loaded while the runtime restarts still offers Claude (#2975).

    The form is rendered once and never swapped, so dropping the account then dropped it for
    good. It stays, and Start says to try again without spending the press.
    """
    from fastapi.testclient import TestClient

    from agentdeck.providers import PROVIDERS

    checkout = _repo(tmp_path / "checkout")
    starts: list = []
    app = _app(tmp_path, monkeypatch, cwd=checkout, starts=starts)
    provider = PROVIDERS["claude_code"]
    monkeypatch.setitem(provider._workers, ACCOUNT, SimpleNamespace(unreachable=True))
    monkeypatch.setattr(provider, "can_start_session", lambda account: False)

    html = TestClient(app).get(work_href(WORK.work_id)).text
    assert f'name="account_key" value="{ACCOUNT}"' in html

    refused = await _post(app, _press("side-chat-2975"))
    assert refused.status_code == 409
    assert "try again in a minute" in refused.text
    assert starts == []

    # Back again: the same press starts the chat, since the refusal never recorded it.
    monkeypatch.setattr(provider, "can_start_session", lambda account: True)
    started = await _post(app, _press("side-chat-2975"))
    assert started.status_code == 202, started.text
    assert len(starts) == 1


def test_the_front_page_form_keeps_an_account_whose_runtime_is_restarting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agentdeck.providers import PROVIDERS

    checkout = _repo(tmp_path / "checkout")
    app = _app(tmp_path, monkeypatch)
    provider = PROVIDERS["claude_code"]
    monkeypatch.setitem(provider._workers, ACCOUNT, SimpleNamespace(unreachable=True))
    monkeypatch.setattr(provider, "can_start_session", lambda account: False)
    client = TestClient(app)

    form = client.get("/").text.split('class="new-chat"', 1)[1].split("</details>", 1)[0]
    assert f'value="{ACCOUNT}"' in form

    response = client.post(
        "/sessions/new",
        data={"account_key": ACCOUNT, "cwd": str(checkout), "message": "hello"},
        headers={"origin": "http://testserver"},
    )
    assert response.status_code == 422
    assert "try again in a minute" in response.text
