"""A send that failed after it was queued can be sent again (#2662).

A chat message was lost when the web process posted it on a runtime connection
the runtime had just closed: the send failed with "Server disconnected without
sending a response", the composer had already been cleared, and the text went
with the row. Two defences: the runtime client posts a fenced send once more
when its connection broke before any answer, and a send that still fails stays
in the chat as a "Not sent" row with a button that queues it again under its own
action id, so the runtime's receipts keep it from ever landing twice.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.action_context import current_client_action_id
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.inject import InjectionService, unsent_messages
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    Session,
    SessionStatus,
)
from agentdeck.providers import PROVIDERS
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
from agentdeck.providers.codex.runtime_client import CodexRuntimeClient
from agentdeck.providers.delivery import DeliveryReceipts

KEY = "claude_code:test:sid"


# --- the runtime client posts a fenced send once more --------------------------


def _flaky(responses):
    """A transport that answers each POST with the next scripted outcome."""
    posted: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"workers": {}})
        posted.append(json.loads(request.content))
        outcome = responses[len(posted) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.MockTransport(handler), posted


def _account(tmp_path: Path, provider="claude_code") -> Account:
    return Account(f"{provider}:main", provider, "main", tmp_path)


def _dropped() -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError("Server disconnected without sending a response.")


@pytest.mark.parametrize(
    "lost",
    [
        _dropped(),
        httpx.ReadError("connection reset"),
        httpx.ConnectError("socket gone"),
    ],
)
async def test_a_fenced_send_whose_connection_broke_is_posted_once_more(tmp_path, lost):
    transport, posted = _flaky(
        [lost, httpx.Response(200, json={"accepted": True, "session_id": "s-1"})]
    )
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)

    result = await client.deliver("key-1", "hello", delivery_id="act-1")

    assert result.accepted
    assert len(posted) == 2
    # The same request, byte for byte, so the runtime's fence recognises it.
    assert posted[0] == posted[1]
    assert posted[1]["delivery_id"] == "act-1"
    await client.stop()


async def test_a_send_without_a_fence_id_is_never_posted_twice(tmp_path):
    transport, posted = _flaky([_dropped(), httpx.Response(200, json={"accepted": True})])
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)

    result = await client.deliver("key-1", "hello")

    assert not result.accepted
    assert result.reason == (
        "claude worker runtime unavailable: Server disconnected without sending a response."
    )
    assert len(posted) == 1
    await client.stop()


@pytest.mark.parametrize(
    "outcome",
    [httpx.ReadTimeout("still running"), httpx.Response(500, text="boom")],
)
async def test_a_timeout_or_an_error_status_is_not_retried(tmp_path, outcome):
    """Either may mean the runtime has the request, so it stays one attempt."""
    transport, posted = _flaky([outcome, httpx.Response(200, json={"accepted": True})])
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)

    result = await client.deliver("key-1", "hello", delivery_id="act-1")

    assert not result.accepted
    assert len(posted) == 1
    await client.stop()


async def test_a_second_broken_connection_fails_the_send(tmp_path):
    transport, posted = _flaky([_dropped(), _dropped()])
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)

    result = await client.deliver("key-1", "hello", delivery_id="act-1")

    assert not result.accepted
    assert "Server disconnected" in result.reason
    assert len(posted) == 2
    await client.stop()


async def test_a_fenced_send_waits_for_a_restarting_runtime_to_listen(tmp_path):
    """A deploy restart leaves the socket missing for about a minute (#3567)."""
    gone = httpx.ConnectError("[Errno 2] No such file or directory")
    ok = httpx.Response(200, json={"accepted": True, "session_id": "s-1"})
    transport, posted = _flaky([gone, gone, gone, ok])
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)
    client._RESTART_POLL_S = 0

    result = await client.deliver("key-1", "hello", delivery_id="act-1")

    assert result.accepted
    assert len(posted) == 4
    await client.stop()

    transport, posted = _flaky([gone] * 50)
    client = ClaudeWorkerClient(_account(tmp_path), transport=transport)
    client._RESTART_POLL_S = 0.01
    client.RUNTIME_RESTART_WAIT_S = 0.03

    result = await client.deliver("key-1", "hello", delivery_id="act-1")

    assert not result.accepted
    assert "No such file or directory" in result.reason
    assert 2 < len(posted) < 50
    await client.stop()


async def test_an_unfenced_action_is_not_retried(tmp_path):
    """Interrupt carries the action id but the runtime does not fence it."""
    transport, posted = _flaky([_dropped(), httpx.Response(200, json={"accepted": True})])
    client = CodexRuntimeClient(_account(tmp_path, "codex"), transport=transport)

    from agentdeck.action_context import client_action_context

    with client_action_context("act-9"):
        result = await client.interrupt("thread-1")

    assert not result.accepted
    assert len(posted) == 1
    await client.stop()


async def test_a_codex_send_is_fenced_on_its_action_id(tmp_path):
    from agentdeck.action_context import client_action_context

    transport, posted = _flaky([_dropped(), httpx.Response(200, json={"accepted": True})])
    client = CodexRuntimeClient(_account(tmp_path, "codex"), transport=transport)

    with client_action_context("act-7"):
        result = await client.queue_turn("thread-1", "hello")

    assert result.accepted
    assert [body["client_action_id"] for body in posted] == ["act-7", "act-7"]
    await client.stop()


def test_idle_connections_are_dropped_before_the_runtime_closes_them(tmp_path):
    client = ClaudeWorkerClient(_account(tmp_path), transport=None)
    pool = client._http._transport._pool
    # uvicorn's default keep-alive is 5 seconds; the pool must give up first.
    assert pool._keepalive_expiry == 2.0


def test_a_resent_id_whose_write_began_is_never_written_again():
    """The safety the retry leans on: a started write replays as uncertain."""
    store: dict = {}
    receipts = DeliveryReceipts(store)
    receipts.prepare("act-1", "fp", session_id="sid")
    receipts.begin_write("act-1", "fp", session_id="sid")

    replay = receipts.lookup("act-1", "fp", live_session_id="sid")

    assert replay is not None
    assert replay.action == "uncertain"
    assert replay.accepted is True


# --- a failed send stays in the chat and can be sent again --------------------


def _injectable(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    return account, session


async def _drain(service, session_key):
    for _ in range(50):
        await asyncio.sleep(0)
        if not service.can_queue(session_key):
            return


class _Provider:
    """Fails the first ``failures`` sends, then takes everything."""

    def __init__(self, failures=1):
        self.failures = failures
        self.sent: list[tuple[str, str | None]] = []

    async def inject(self, account, session, message, *, timeout_s, **_kwargs):
        if self.failures:
            self.failures -= 1
            return InjectResult(
                False,
                "claude worker runtime unavailable: "
                "Server disconnected without sending a response.",
            )
        self.sent.append((message, current_client_action_id()))
        return InjectResult(True, transcript_expected=False)


async def test_a_failed_message_is_sent_again_under_its_own_action_id(tmp_path):
    service = InjectionService(InjectConfig(enabled=True))
    account, session = _injectable(tmp_path)
    provider = _Provider()

    await service.start(account, session, provider, "go on", client_action_id="act-1")
    await _drain(service, session.key)
    [failed] = unsent_messages(service.status(session.key))
    assert failed.text == "go on"

    resent = await service.resend(account, session, provider, failed.id)
    await _drain(service, session.key)

    assert resent.accepted
    assert provider.sent == [("go on", "act-1")]
    assert unsent_messages(service.status(session.key)) == []
    # Pressing again changes nothing: the message is no longer waiting.
    again = await service.resend(account, session, provider, failed.id)
    assert not again.accepted
    assert again.reason == "this message is no longer waiting to be sent again"
    await _drain(service, session.key)
    assert provider.sent == [("go on", "act-1")]
    await service.stop()


async def test_messages_that_failed_behind_a_failure_can_be_sent_again(tmp_path):
    release = asyncio.Event()

    class Provider(_Provider):
        async def inject(self, account, session, message, *, timeout_s, **kwargs):
            await release.wait()
            return await super().inject(account, session, message, timeout_s=timeout_s, **kwargs)

    service = InjectionService(InjectConfig(enabled=True))
    account, session = _injectable(tmp_path)
    provider = Provider()
    await service.start(account, session, provider, "first", client_action_id="a-1")
    await service.start(account, session, provider, "second", client_action_id="a-2")
    release.set()
    await _drain(service, session.key)

    unsent = unsent_messages(service.status(session.key))
    assert [item.text for item in unsent] == ["first", "second"]
    assert unsent[1].reason == "not sent because the previous turn failed"

    for item in unsent:
        assert (await service.resend(account, session, provider, item.id)).accepted
    await _drain(service, session.key)

    assert provider.sent == [("first", "a-1"), ("second", "a-2")]
    await service.stop()


async def test_a_failure_that_a_later_send_got_past_is_history(tmp_path):
    service = InjectionService(InjectConfig(enabled=True))
    account, session = _injectable(tmp_path)
    provider = _Provider()
    await service.start(account, session, provider, "lost")
    await _drain(service, session.key)
    await service.start(account, session, provider, "landed")
    await _drain(service, session.key)

    assert unsent_messages(service.status(session.key)) == []


async def test_a_message_whose_uploads_are_gone_cannot_be_sent_again(tmp_path):
    service = InjectionService(InjectConfig(enabled=True))
    account, session = _injectable(tmp_path)
    provider = _Provider()
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    await service.start(account, session, provider, "look", [image])
    await _drain(service, session.key)
    [failed] = unsent_messages(service.status(session.key))

    refused = await service.resend(account, session, provider, failed.id)

    assert not refused.accepted
    assert "attachments were not kept" in refused.reason
    assert provider.sent == []
    await service.stop()


# --- the chat page ---------------------------------------------------------------


def _app(tmp_path):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path / "claude"))
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            KEY,
            "claude_code:test",
            "sid",
            SessionStatus.IDLE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


async def test_a_failed_send_is_a_not_sent_row_that_sends_again(tmp_path, monkeypatch):
    app = _app(tmp_path)
    provider = _Provider()
    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", provider.inject)
    injector = app.state.injector

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        sent = await client.post(
            f"/sessions/{KEY}/inject",
            data={"message": "I approve the spec"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "act-2662"},
        )
        assert sent.status_code == 202
        await _drain(injector, KEY)
        [failed] = unsent_messages(injector.status(KEY))

        status = await client.get(f"/partials/sessions/{KEY}/inject-status")
        page = await client.get(f"/sessions/{KEY}")

        # The live failure redraws the row the send left in the transcript…
        assert 'class="inject-result failed has-detail"' in status.text
        assert f'id="pending-message-{failed.id}"' in status.text
        assert 'hx-swap-oob="outerHTML:.pending-message[data-client-action-id=' in status.text
        assert "Not sent" in status.text
        # …and a reload still shows it, text and button, rather than losing it.
        for body in (status.text, page.text):
            assert "I approve the spec" in body
            assert "Server disconnected without sending a response." in body
            assert f"/sessions/{KEY}/pending/{failed.id}/resend" in body
            assert "Send again" in body

        resent = await client.post(
            f"/sessions/{KEY}/pending/{failed.id}/resend",
            headers={"origin": "http://test"},
        )
        assert resent.status_code == 200
        # The composer slot polls again and the row is queued in place.
        assert "inject-status" in resent.text
        assert f'id="pending-message-{failed.id}"' in resent.text
        assert "Not sent" not in resent.text
        await _drain(injector, KEY)

        after = await client.get(f"/sessions/{KEY}")

    assert provider.sent == [("I approve the spec", "act-2662")]
    assert "Send again" not in after.text


async def test_a_refused_resend_says_why_in_place(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/sessions/{KEY}/pending/999/resend", headers={"origin": "http://test"}
        )

    assert refused.status_code == 200
    assert "This message is no longer waiting to be sent again." in refused.text


async def test_a_cross_site_resend_is_refused(tmp_path):
    app = _app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/sessions/{KEY}/pending/1/resend", headers={"origin": "http://evil"}
        )

    assert refused.status_code == 403


@pytest.mark.browser
async def test_a_failed_send_comes_back_and_sends_again_in_the_browser(tmp_path, monkeypatch):
    """The real page: the poll turns the sent row into "Not sent", the button
    queues it again, and what is being typed in the composer meanwhile stays."""
    import os
    import re

    from conftest import async_playwright
    from test_inject import _live_server

    app = _app(tmp_path)
    provider = _Provider()
    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", provider.inject)
    screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/{KEY}")

        await page.locator("#inject-message").fill("I approve the spec")
        async with page.expect_response(re.compile(rf"/sessions/{KEY}/inject$")) as sent:
            await page.locator("#composer-controls button", has_text="Send").click()
        assert (await sent.value).status == 202

        unsent = page.locator(".pending-message.unsent-message")
        await unsent.wait_for(state="visible", timeout=15_000)
        assert "I approve the spec" in await unsent.inner_text()
        assert "Not sent" in await unsent.inner_text()
        assert "Server disconnected" in await unsent.inner_text()
        resend = unsent.get_by_role("button", name="Send again")
        await resend.wait_for(state="visible")
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "unsent-row.png"))

        await page.locator("#inject-message").fill("half-typed next message")
        async with page.expect_response(re.compile(r"/resend$")) as again:
            await resend.click()
        assert (await again.value).status == 200

        await page.wait_for_function(
            "() => !document.querySelector('.pending-message.unsent-message')"
        )
        assert await page.locator("#inject-message").input_value() == ("half-typed next message")
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "resent-row.png"))
        await browser.close()

    assert provider.sent == [("I approve the spec", provider.sent[0][1])]
    assert provider.sent[0][1]
    await app.state.injector.stop()


def _rolled_over(tmp_path):
    """A chat whose work moved to ``sid2`` while the reader is still on ``sid``."""
    app = _app(tmp_path)
    app.state.app_state.update_session(
        Session(
            "claude_code:test:sid2",
            "claude_code:test",
            "sid2",
            SessionStatus.LIVE,
            cwd=tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    app.state.app_state.chat_reachable_leg_key = lambda _key: "claude_code:test:sid2"
    return app


async def _failed_on_old_leg(app, provider):
    injector = app.state.injector
    account = next(a for a in app.state.accounts if a.key == "claude_code:test")
    session = app.state.app_state.sessions[KEY]
    await injector.start(account, session, provider, "did you get this", client_action_id="act-old")
    await _drain(injector, KEY)
    [failed] = unsent_messages(injector.status(KEY))
    return failed


async def test_a_row_from_a_rolled_over_leg_is_resent_to_the_current_leg(tmp_path, monkeypatch):
    """The row lives on the leg the URL names; the chat now works on the next one."""
    app = _rolled_over(tmp_path)
    provider = _Provider()
    delivered_to: list[str] = []
    original = provider.inject

    async def inject(account, session, message, **kwargs):
        delivered_to.append(session.key)
        return await original(account, session, message, **kwargs)

    async def spent(account, session, delivery_id):
        assert (session.key, delivery_id) == (KEY, "act-old")
        return False

    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", inject)
    monkeypatch.setattr(PROVIDERS["claude_code"], "delivery_spent", spent, raising=False)
    failed = await _failed_on_old_leg(app, PROVIDERS["claude_code"])
    injector = app.state.injector

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resent = await client.post(
            f"/sessions/{KEY}/pending/{failed.id}/resend",
            headers={"origin": "http://test"},
        )
        await _drain(injector, "claude_code:test:sid2")

    assert resent.status_code == 200
    assert "no longer waiting" not in resent.text
    assert delivered_to == [KEY, "claude_code:test:sid2"]
    assert provider.sent == [("did you get this", "act-old")]
    # The row left the old leg's queue rather than showing "Not sent" there too.
    assert unsent_messages(injector.status(KEY)) == []


async def test_a_row_the_old_leg_may_have_delivered_is_not_resent_to_the_next(
    tmp_path, monkeypatch
):
    """The next leg's fence cannot see the old leg's receipt, so ask first."""
    app = _rolled_over(tmp_path)
    provider = _Provider(failures=1)

    async def spent(account, session, delivery_id):
        return True

    monkeypatch.setattr(PROVIDERS["claude_code"], "inject", provider.inject)
    monkeypatch.setattr(PROVIDERS["claude_code"], "delivery_spent", spent, raising=False)
    failed = await _failed_on_old_leg(app, PROVIDERS["claude_code"])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/sessions/{KEY}/pending/{failed.id}/resend",
            headers={"origin": "http://test"},
        )

    assert refused.status_code == 200
    assert "It may already have reached the earlier part of this chat." in refused.text
    assert provider.sent == []
    assert [item.id for item in unsent_messages(app.state.injector.status(KEY))] == [failed.id]
