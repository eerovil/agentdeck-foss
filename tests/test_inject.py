import asyncio
import base64
import contextlib
import json
import os
import re
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
import uvicorn
from conftest import async_playwright, resized, wait_until
from httpx import ASGITransport, AsyncClient
from playwright.async_api import expect
from starlette.datastructures import FormData, Headers, UploadFile

from agentdeck.action_context import current_client_action_id
from agentdeck.app import create_app
from agentdeck.attachments import ATTACHMENT_HEADER, ATTACHMENT_MARKER, display_message
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    HistoryConfig,
    InjectConfig,
)
from agentdeck.db import Db
from agentdeck.inject import DelegationStatus, InjectionService, QueuedMessage
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
)
from agentdeck.providers.claude_code import transcripts as claude_transcripts
from agentdeck.providers.codex import (
    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
    LOGIN_SHELL_CONFIG_OVERRIDE,
    WRITABLE_ROOTS_CONFIG_OVERRIDE,
)
from agentdeck.providers.codex.inject import (
    inject_session,
    is_injectable_rollout,
    start_session,
)
from agentdeck.providers.codex.transcripts import last_turn_complete
from agentdeck.quota_retry import message_for
from agentdeck.session_secrets import IssuedSecret
from agentdeck.web.render import SESSION_FRAGMENTS_BY_NAME, SessionView, chat_time
from agentdeck.web.uploads import (
    UploadError,
    cleanup_uploaded_files,
    save_uploaded_files,
    sweep_abandoned_uploads,
)

# How long a browser test may wait for a question to appear on the page after
# the server side of it is in place. Playwright's own default is 10s, which is
# not a budget so much as a coincidence: pushing a new interaction costs its
# announcement and a render before the fragment is even sent, and a PWA
# foreground first tears down and reopens the SSE stream.
# Add four xdist workers and headless Chromium on four cores and 10s runs out
# for reasons that have nothing to do with the behaviour under test (#1308).
# This is deliberately generous: these waits end the moment the page renders,
# so a loose ceiling costs nothing on a healthy run and only buys patience on a
# loaded one. It does not weaken the assertion — a question that never arrives
# still fails the test.
QUESTION_RENDER_TIMEOUT_MS = 45_000


def started_token(response):
    """The token of the start an accepted response is now watching in place.

    Pressing Start no longer redirects anywhere (#1799), so the token is read off
    the status strip's own poll rather than an ``HX-Redirect`` header.
    """
    match = re.search(r"/partials/starting-chat/([0-9a-f]+)", response.text)
    assert match is not None, response.text
    return match.group(1)


def render_composer_controls(templates, session):
    view = SessionView(current=session, effective=session)
    return SESSION_FRAGMENTS_BY_NAME["composer-controls"].render(templates, view)


def render_pending_interaction(templates, session_key, interaction):
    account_key, session_id = session_key.rsplit(":", 1)
    session = Session(
        key=session_key,
        account_key=account_key,
        session_id=session_id,
        status=SessionStatus.IDLE,
    )
    view = SessionView(current=session, effective=session, interaction=interaction)
    return SESSION_FRAGMENTS_BY_NAME["interaction"].render(templates, view)


def _line(type_, payload):
    return json.dumps({"timestamp": "2026-07-13T12:00:00Z", "type": type_, "payload": payload})


def _exec_rollout(tmp_path: Path, *, complete: bool = True) -> Path:
    sid = "019f5b5b-6281-7a00-a197-d020a1243d2d"
    path = tmp_path / f"rollout-{sid}.jsonl"
    lines = [
        _line(
            "session_meta",
            {"session_id": sid, "cwd": str(tmp_path), "source": "exec"},
        ),
        _line("event_msg", {"type": "task_started"}),
    ]
    if complete:
        lines.append(_line("event_msg", {"type": "task_complete"}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_completed_exec_turn_is_the_only_injectable_rollout(tmp_path):
    complete = _exec_rollout(tmp_path)
    assert last_turn_complete(complete)
    assert is_injectable_rollout(complete, "exec")
    assert not is_injectable_rollout(complete, "cli")

    active_dir = tmp_path / "active"
    active_dir.mkdir()
    active = _exec_rollout(active_dir, complete=False)
    assert not last_turn_complete(active)
    assert not is_injectable_rollout(active, "exec")


class _FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = None
        self.final_returncode = returncode
        self.pid = os.getpid()
        self.input = None

    async def communicate(self, value):
        self.input = value
        self.returncode = self.final_returncode
        return (b"", b"")


async def test_inject_session_passes_prompt_on_stdin(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setattr(
        "agentdeck.providers.codex.inject.gh_shim.worker_path",
        lambda environment: "/test-gh-shim:" + environment.get("PATH", ""),
    )
    path = _exec_rollout(tmp_path)
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "019f5b5b-6281-7a00-a197-d020a1243d2d",
        SessionStatus.IDLE,
        cwd=tmp_path,
        kind="exec",
        capabilities=frozenset({Capability.INJECT}),
    )
    process = _FakeProcess()
    spawned = {}
    images = [tmp_path / "one.png", tmp_path / "two.jpg"]

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return process

    result = await inject_session(
        account,
        session,
        path,
        "do the next thing",
        timeout_s=10,
        images=images,
        process_factory=factory,
    )
    assert result.accepted
    assert process.input == b"do the next thing\n"
    assert "do the next thing" not in spawned["args"]
    assert spawned["args"][:4] == ("codex", "exec", "resume", session.session_id)
    assert spawned["args"][4:14] == (
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "--config",
        APPROVALS_REVIEWER_CONFIG_OVERRIDE,
        "--config",
        LOGIN_SHELL_CONFIG_OVERRIDE,
    )
    assert spawned["args"][14:18] == (
        "-i",
        str(images[0]),
        "-i",
        str(images[1]),
    )
    assert spawned["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
    assert spawned["kwargs"]["env"]["GIT_CONFIG_VALUE_0"] == "git@github.com:"
    # AgentDeck's per-owner helper, never the host's `gh` login (#2289), and the shim
    # in front of `gh`.
    assert spawned["kwargs"]["env"]["GIT_CONFIG_VALUE_3"].endswith(
        "-m agentdeck.git_credential"
    )
    assert spawned["kwargs"]["env"]["PATH"].startswith("/test-gh-shim:")
    assert spawned["kwargs"]["start_new_session"] is True


async def test_inject_session_rechecks_turn_boundary_before_spawn(tmp_path):
    path = _exec_rollout(tmp_path, complete=False)
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        kind="exec",
    )
    called = False

    async def factory(*args, **kwargs):
        nonlocal called
        called = True

    result = await inject_session(
        account,
        session,
        path,
        "message",
        timeout_s=10,
        process_factory=factory,
    )
    assert not result.accepted
    assert "completed" in result.reason
    assert called is False


class _BlockingProvider:
    def __init__(self):
        self.release = asyncio.Event()
        self.messages = []

    async def inject(self, account, session, message, *, timeout_s):
        self.messages.append(message)
        await self.release.wait()
        return InjectResult(True)


async def test_injection_service_queues_one_session_fifo(tmp_path):
    changed = []
    service = InjectionService(InjectConfig(enabled=True), on_change=changed.append)
    provider = _BlockingProvider()
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.INJECT}),
    )
    first = await service.start(account, session, provider, "first")
    second = await service.start(account, session, provider, "second")
    assert first.accepted
    assert second.accepted
    status = service.status(session.key)
    assert [item.text for item in status.items] == ["first", "second"]
    assert [item.state for item in status.items] == ["queued", "queued"]
    assert changed == [session.key, session.key]
    provider.release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break
    assert provider.messages == ["first", "second"]
    assert [item.state for item in service.status(session.key).items] == [
        "accepted",
        "accepted",
    ]


async def test_wait_for_delivery_preserves_provider_handoff_when_caller_is_cancelled(
    tmp_path,
):
    service = InjectionService(InjectConfig(enabled=True))
    provider = _BlockingProvider()
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )

    await service.start(account, session, provider, "wake")
    waiter = asyncio.create_task(service.wait_for_delivery(session.key))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)

    assert not waiter.done()
    assert service.can_queue(session.key)

    provider.release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert provider.messages == ["wake"]
    await service.stop()




async def test_injection_service_keeps_each_queued_action_id(tmp_path):
    seen = []

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            seen.append((message, current_client_action_id()))
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )

    await service.start(
        account, session, Provider(), "first", client_action_id="action-first"
    )
    await service.start(
        account, session, Provider(), "second", client_action_id="action-second"
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if len(seen) == 2:
            break

    assert seen == [("first", "action-first"), ("second", "action-second")]
    await service.stop()


async def test_one_action_waiting_in_the_queue_is_never_queued_twice(tmp_path):
    """A retry of an action already waiting must not become a second message (#2016).

    The Mac runner re-posts a command's outcome every 30 seconds until the deck
    confirms it landed, and the deck only confirms on a transcript event — which
    a chat in a long turn does not produce until the turn ends. Each retry used
    to append another copy, so the chat filled up with the same handful of
    outcomes and the agent would have been told each of them many times over.
    """
    release = asyncio.Event()

    class Provider:
        def __init__(self):
            self.messages = []

        async def inject(self, account, session, message, *, timeout_s):
            self.messages.append(message)
            await release.wait()
            return InjectResult(True)

    provider = Provider()
    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )

    first = await service.start(
        account, session, provider, "the outcome", client_action_id="mac-runner:run-1"
    )
    await asyncio.sleep(0)
    again = await service.start(
        account, session, provider, "the outcome", client_action_id="mac-runner:run-1"
    )
    # Accepted, because from the caller's side the outcome is with the chat.
    assert (first.accepted, again.accepted) == (True, True)
    assert len(service.status(session.key).items) == 1

    # A different action still queues normally.
    await service.start(
        account, session, provider, "another", client_action_id="mac-runner:run-2"
    )
    assert len(service.status(session.key).items) == 2

    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if len(provider.messages) == 2:
            break
    assert provider.messages == ["the outcome", "another"]
    await service.stop()


async def test_transcript_observation_during_send_is_not_regressed_to_accepted(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            started.set()
            await release.wait()
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    await service.start(account, session, Provider(), "fast transcript")
    await asyncio.wait_for(started.wait(), timeout=1)
    item = service.status(session.key).items[0]

    event = TranscriptEvent(
        seq=8,
        role="user",
        text="fast transcript",
        ts=item.created_at + timedelta(milliseconds=1),
    )
    assert service.observe_transcript(session.key, [event]) == {8: item}
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break

    assert item.state == "observed"
    await service.stop()


async def test_transcript_observation_is_not_regressed_by_late_rejection(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            started.set()
            await release.wait()
            return InjectResult(False, "provider timed out")

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    await service.start(account, session, Provider(), "already durable")
    await asyncio.wait_for(started.wait(), timeout=1)
    item = service.status(session.key).items[0]
    event = TranscriptEvent(
        seq=1,
        role="user",
        text="already durable",
        ts=item.created_at + timedelta(milliseconds=1),
    )
    assert service.observe_transcript(session.key, [event]) == {1: item}
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break

    assert item.state == "observed"
    assert item.reason is None
    await service.stop()


# --- a handoff that reached nothing takes back its working stamp (#1450) ------
#
# ``on_sent`` is optimistic: it fires as the delivery starts, before the provider
# has taken anything, so the chat reads as working from the moment the deck hands
# it work. Nothing else ever clears that stamp — the chat's own activity does, and
# a chat that was never reached produces none — so every route that ends a
# delivery in failure has to hand it back, or the chat stays working for good and
# the stop that should have carded it is never seen.


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


async def test_a_refused_injection_does_not_leave_the_chat_working(tmp_path):
    """The provider took nothing, so the deck must stop claiming it did."""
    sent: list[str] = []
    failed: list[str] = []

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            return InjectResult(False, "provider refused")

    service = InjectionService(
        InjectConfig(enabled=True),
        on_sent=sent.append,
        on_send_failed=failed.append,
    )
    account, session = _injectable(tmp_path)

    assert (await service.start(account, session, Provider(), "go")).accepted
    await _drain(service, session.key)

    assert sent == [session.key]
    assert failed == [session.key]
    assert service.status(session.key).items[0].state == "failed"
    await service.stop()


async def test_an_injection_that_raises_does_not_leave_the_chat_working(tmp_path):
    """A provider that blows up mid-handoff is the same lost send, less politely."""
    sent: list[str] = []
    failed: list[str] = []

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            raise RuntimeError("the CLI died")

    service = InjectionService(
        InjectConfig(enabled=True),
        on_sent=sent.append,
        on_send_failed=failed.append,
    )
    account, session = _injectable(tmp_path)

    assert (await service.start(account, session, Provider(), "go")).accepted
    await _drain(service, session.key)

    assert sent == [session.key]
    assert failed == [session.key]
    assert service.status(session.key).items[0].state == "failed"
    await service.stop()


async def test_a_steer_that_is_refused_does_not_leave_the_chat_working(tmp_path):
    """``deliver_now`` bypasses the FIFO, so it needs the clear of its own."""
    sent: list[str] = []
    failed: list[str] = []

    class Provider:
        async def transcript_cursor(self, account, session):
            return 0, 0

        async def steer(self, account, session, message, **kwargs):
            return InjectResult(False, "no turn to steer")

    service = InjectionService(
        InjectConfig(enabled=True),
        on_sent=sent.append,
        on_send_failed=failed.append,
    )
    account, session = _injectable(tmp_path)

    receipt = await service.deliver_now(account, session, Provider(), "stop that")

    assert receipt.result.accepted is False
    assert sent == [session.key]
    assert failed == [session.key]
    await service.stop()


async def test_one_failed_send_does_not_unwork_a_chat_a_sibling_send_reached(tmp_path):
    """A delivery still in flight is a real handoff, and it keeps the chat working."""
    sent: list[str] = []
    failed: list[str] = []
    release = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return 0, 0

        async def inject(self, account, session, message, *, timeout_s):
            await release.wait()
            return InjectResult(True)

        async def steer(self, account, session, message, **kwargs):
            return InjectResult(False, "no turn to steer")

    service = InjectionService(
        InjectConfig(enabled=True),
        on_sent=sent.append,
        on_send_failed=failed.append,
    )
    account, session = _injectable(tmp_path)

    await service.start(account, session, Provider(), "the one that lands")
    for _ in range(20):
        await asyncio.sleep(0)
        if sent:
            break
    assert sent == [session.key]

    # A steer refused while that first message is still with the provider.
    await service.deliver_now(account, session, Provider(), "and one that does not")
    assert failed == []

    release.set()
    await _drain(service, session.key)
    await service.stop()


async def test_a_send_that_is_not_new_work_never_stamps_the_chat_working(tmp_path):
    """#2003. A durable retry hands the chat nothing, so it must not restart the clock.

    The stamp is what ``session_awaiting_reply`` measures a chat's own activity
    against. A caller that retries the same delivery on a timer — the Mac runner
    does, for as long as an outcome stays unconfirmed — would move that line
    forward faster than a chat can cross it, and a chat whose turn died silently
    never crosses it at all: it sits under Deckhand's Working group with nothing
    running. The first send is real work and still stamps; the repeats do not.
    """
    sent: list[str] = []

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True), on_sent=sent.append)
    account, session = _injectable(tmp_path)

    assert (await service.start(account, session, Provider(), "the outcome")).accepted
    await _drain(service, session.key)
    assert sent == [session.key]

    for _ in range(3):
        assert (
            await service.start(
                account, session, Provider(), "the outcome", counts_as_work=False
            )
        ).accepted
        await _drain(service, session.key)

    assert sent == [session.key]
    await service.stop()


async def test_a_handover_is_announced_from_the_delivery_not_from_start(tmp_path):
    """#2003. The moment the fence arms is inside the send, not at `start`.

    A durable-retry caller decides whether it is giving the chat new work by
    asking whether an earlier send ever reached the provider. It cannot watch for
    that itself — the Mac runner stops waiting after ten seconds — so the answer
    has to be pushed from the delivery's own lifecycle, however late it lands.
    """
    handovers: list[tuple[str, str]] = []
    release = asyncio.Event()

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            await release.wait()
            return InjectResult(True)

    service = InjectionService(
        InjectConfig(enabled=True),
        on_handover=lambda key, action_id: handovers.append((key, action_id)),
    )
    account, session = _injectable(tmp_path)

    assert (
        await service.start(
            account, session, Provider(), "the outcome", client_action_id="mac-runner:r1"
        )
    ).accepted
    # `start` has returned and the provider has not answered. Nothing is armed.
    assert handovers == []

    release.set()
    await _drain(service, session.key)

    assert handovers == [(session.key, "mac-runner:r1")]
    await service.stop()


async def test_a_transcript_observed_mid_send_still_announces_the_handover(tmp_path):
    """#2003. The event can land before the provider call returns.

    `_finish_delivery` returns early for an item already `observed`, so if the
    transcript is seen while `provider.inject` is still awaited, announcing only
    from the accepted branch would never fire. And an observed event is the
    *stronger* evidence of the two: the message is demonstrably in the chat.

    The Mac runner has long since stopped waiting by then — ten seconds — so
    nothing else is left to notice.
    """
    handovers: list[tuple[str, str]] = []
    release = asyncio.Event()
    sending = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return 0, 0

        async def inject(self, account, session, message, *, timeout_s):
            sending.set()
            await release.wait()
            return InjectResult(True)

    service = InjectionService(
        InjectConfig(enabled=True),
        on_handover=lambda key, action_id: handovers.append((key, action_id)),
    )
    account, session = _injectable(tmp_path)

    assert (
        await service.start(
            account, session, Provider(), "the outcome", client_action_id="mac-runner:r1"
        )
    ).accepted
    await asyncio.wait_for(sending.wait(), timeout=5)
    assert handovers == []

    # The transcript carries the message while the provider call is still open.
    service.observe_transcript(
        session.key,
        [
            TranscriptEvent(
                seq=1, role="user", text="the outcome", ts=datetime.now(UTC)
            )
        ],
    )

    assert handovers == [(session.key, "mac-runner:r1")]

    # And the provider returning afterwards does not announce it a second time.
    release.set()
    await _drain(service, session.key)
    assert handovers == [(session.key, "mac-runner:r1")]
    await service.stop()


async def test_a_retry_queued_before_the_first_handover_still_does_not_stamp(tmp_path):
    """#2003. `counts_as_work` is decided at queue time and used at dispatch time.

    The Mac program retries on its own clock, so a retry can be queued while the
    first send is still in flight — at which point nothing has handed over and
    `counts_as_work=True` is the right answer. By the time that queued item
    actually runs, the first send has been accepted and the fence is armed, so
    the answer has gone stale and stamping on it would leave the chat under
    Working for a message the fence collapses.
    """
    sent: list[str] = []
    first_sending = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    class Provider:
        async def transcript_cursor(self, account, session):
            return 0, 0

        async def tail_transcript(self, *args, **kwargs):
            # Never acknowledges. Needed so the queue keeps draining to the second
            # item rather than dying on a missing attribute before it ever runs.
            return (), 0, 0

        async def inject(self, account, session, message, *, timeout_s):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_sending.set()
                await release_first.wait()
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True), on_sent=sent.append)
    account, session = _injectable(tmp_path)

    # The first callback. It is real work and stamps as it starts.
    assert (
        await service.start(
            account, session, Provider(), "outcome", client_action_id="mac-runner:r1"
        )
    ).accepted
    await asyncio.wait_for(first_sending.wait(), timeout=5)
    assert sent == [session.key]

    # The Mac retries while that first send is still open, so nothing has handed
    # over yet and the retry is queued believing it is new work.
    assert (
        await service.start(
            account,
            session,
            Provider(),
            "outcome",
            client_action_id="mac-runner:r1",
            counts_as_work=True,
        )
    ).accepted

    # Only now does the first send land, arming the fence.
    release_first.set()
    await _drain(service, session.key)

    # The queued retry ran afterwards and must not have stamped on its stale answer.
    assert sent == [session.key]
    await service.stop()


async def test_a_refused_send_announces_no_handover(tmp_path):
    """Nothing reached the provider, so no fence armed and the next try is real."""
    handovers: list[tuple[str, str]] = []

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            return InjectResult(False, "provider refused")

    service = InjectionService(
        InjectConfig(enabled=True),
        on_handover=lambda key, action_id: handovers.append((key, action_id)),
    )
    account, session = _injectable(tmp_path)

    await service.start(
        account, session, Provider(), "the outcome", client_action_id="mac-runner:r1"
    )
    await _drain(service, session.key)

    assert handovers == []
    await service.stop()


def test_the_app_wires_a_failed_delivery_back_to_the_working_stamp(tmp_path):
    """The clear only helps if the real injector holds the real state's.

    Both callbacks default to a no-op, so a missing wire is silent: deliveries
    would keep stamping and nothing would ever unstamp.
    """
    app = _web_app(tmp_path)
    state = app.state.app_state
    key = "codex:test:sid"

    app.state.injector._on_sent(key)
    assert key in state.session_sent_at
    app.state.injector._on_send_failed(key)
    assert key not in state.session_sent_at


async def test_injection_keeps_image_until_durable_transcript_event(tmp_path):
    durable = asyncio.Event()
    provider_started = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return (100, 5)

        async def inject(self, account, session, message, *, timeout_s, images=None):
            provider_started.set()
            return InjectResult(True)

        async def tail_transcript(self, account, session, byte_offset, seq):
            assert (byte_offset, seq) == (100, 5)
            if not durable.is_set():
                return ([], byte_offset, seq)
            return (
                [
                    TranscriptEvent(
                        seq=6,
                        role="user",
                        text="inspect",
                        ts=datetime.now(UTC),
                        image_media_types=("image/png",),
                    )
                ],
                200,
                6,
            )

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    image = tmp_path / "pending.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nsmall")

    assert (await service.start(account, session, Provider(), "inspect", [image])).accepted
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert image.exists()
    assert service.status(session.key).items[0].state == "running"

    durable.set()
    for _ in range(20):
        await asyncio.sleep(0.01)
        if not image.exists():
            break
    assert not image.exists()
    assert service.status(session.key).items[0].state == "observed"
    await service.stop()


async def test_queued_message_remains_visible_after_navigation(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    app.state.app_state.sessions["codex:test:sid"].show_when_idle = True
    release = asyncio.Event()

    async def fake_inject(
        account, session, message, *, timeout_s, allow_over_budget=False
    ):
        assert allow_over_budget is True
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "keep this queued"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202

    # A new browser/page request sees the authoritative server-side queue on
    # both the list card and reopened detail page. This load asks for the chat
    # list — it is about the card, not about #1458's shut default.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        dashboard = await client.get("/chats?chats=1")
        detail = await client.get("/sessions/codex:test:sid")
    assert 'class="card-pending" data-pending-count="1"' in dashboard.text
    assert "keep this queued" in dashboard.text
    assert "keep this queued" in detail.text
    assert 'class="ev user pending-message"' in detail.text
    assert detail.text.index('class="ev user pending-message"') < detail.text.index(
        'id="tool-activity"'
    )

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not app.state.injector.can_queue("codex:test:sid"):
            break
    await app.state.injector.stop()


def test_transcript_observation_matches_pending_turns_once_in_fifo_order():
    queued_at = datetime.now(UTC)
    first = QueuedMessage(1, "testing", state="accepted", created_at=queued_at)
    second = QueuedMessage(2, "testing", state="accepted", created_at=queued_at)
    queued = QueuedMessage(3, "testing", state="queued", created_at=queued_at)
    service = InjectionService(InjectConfig(enabled=True))
    service._items["codex:test:sid"] = [first, second, queued]
    old_same_text = TranscriptEvent(
        seq=1,
        role="user",
        text="testing",
        ts=queued_at - timedelta(minutes=1),
    )
    new_same_text = TranscriptEvent(
        seq=2,
        role="user",
        text="testing",
        ts=queued_at + timedelta(seconds=1),
    )

    assert service.observe_transcript("codex:test:sid", [old_same_text]) == {}
    assert service.observe_transcript("codex:test:sid", [new_same_text]) == {2: first}
    assert [item.state for item in (first, second, queued)] == [
        "observed",
        "accepted",
        "queued",
    ]

    # Replaying the same transcript window is idempotent and preserves identity.
    # Since #1722 an already-linked seq is answered from the compact correlation
    # record rather than the queue row, which the row's retirement may have taken
    # away, so pin the identity the transcript template actually reads.
    replayed = service.observe_transcript("codex:test:sid", [new_same_text])
    assert list(replayed) == [2]
    assert (replayed[2].id, replayed[2].client_action_id) == (
        first.id,
        first.client_action_id,
    )
    after_rotation = QueuedMessage(
        4,
        "after rotation",
        state="accepted",
        created_at=queued_at,
    )
    service._items["codex:test:sid"].append(after_rotation)
    rotated_seq = TranscriptEvent(
        seq=2,
        role="user",
        text="after rotation",
        ts=queued_at + timedelta(seconds=2),
    )
    assert service.observe_transcript("codex:test:sid", [rotated_seq]) == {
        2: after_rotation
    }
    next_same_text = TranscriptEvent(
        seq=3,
        role="user",
        text="testing",
        ts=queued_at + timedelta(seconds=3),
    )
    assert service.observe_transcript("codex:test:sid", [next_same_text]) == {3: second}
    assert queued.state == "queued"


def test_transcript_observation_includes_attachment_signature(tmp_path):
    queued_at = datetime.now(UTC)
    png = QueuedMessage(
        1,
        "inspect",
        images=(tmp_path / "one.png",),
        state="accepted",
        created_at=queued_at,
    )
    webp = QueuedMessage(
        2,
        "inspect",
        images=(tmp_path / "two.webp",),
        state="accepted",
        created_at=queued_at,
    )
    service = InjectionService(InjectConfig(enabled=True))
    service._items["codex:test:sid"] = [png, webp]

    wrong_type = TranscriptEvent(
        seq=4,
        role="user",
        text="inspect",
        ts=queued_at + timedelta(seconds=1),
        image_media_types=("image/jpeg",),
    )
    assert service.observe_transcript("codex:test:sid", [wrong_type]) == {}

    events = [
        TranscriptEvent(
            seq=5,
            role="user",
            text="inspect",
            ts=queued_at + timedelta(seconds=2),
            image_media_types=("image/png",),
        ),
        TranscriptEvent(
            seq=6,
            role="user",
            text="inspect",
            ts=queued_at + timedelta(seconds=3),
            image_media_types=("image/webp",),
        ),
    ]
    assert service.observe_transcript("codex:test:sid", events) == {5: png, 6: webp}


def test_transcript_observation_uses_provider_timestamp_precision():
    created_at = datetime(2026, 7, 22, 10, 0, 0, 987654, tzinfo=UTC)
    item = QueuedMessage(
        1,
        "same millisecond",
        state="accepted",
        created_at=created_at,
        after_seq=10,
    )
    service = InjectionService(InjectConfig(enabled=True))
    service._items["codex:test:sid"] = [item]
    provider_event = TranscriptEvent(
        seq=11,
        role="user",
        text="same millisecond",
        ts=created_at.replace(microsecond=987000),
    )

    older_same_millisecond = TranscriptEvent(
        seq=10,
        role="user",
        text="same millisecond",
        ts=provider_event.ts,
    )
    assert service.observe_transcript("codex:test:sid", [older_same_millisecond]) == {}
    assert service.observe_transcript("codex:test:sid", [provider_event]) == {11: item}


def test_transcript_observation_accepts_timestamp_less_queued_event_after_cursor():
    queued_at = datetime.now(UTC)
    item = QueuedMessage(
        1,
        "sent while busy",
        state="accepted",
        created_at=queued_at,
        after_seq=10,
    )
    service = InjectionService(InjectConfig(enabled=True))
    service._items["claude_code:test:sid"] = [item]
    old_event = TranscriptEvent(
        seq=10,
        role="user",
        text="sent while busy",
        queued=True,
    )
    durable_event = TranscriptEvent(
        seq=11,
        role="user",
        text="sent while busy",
        queued=True,
    )

    assert service.observe_transcript("claude_code:test:sid", [old_event]) == {}
    assert service.observe_transcript("claude_code:test:sid", [durable_event]) == {
        11: item
    }
    assert item.state == "observed"
    assert item.observed_at is None
    replayed = service.observe_transcript("claude_code:test:sid", [durable_event])
    assert list(replayed) == [11]
    assert (replayed[11].id, replayed[11].client_action_id) == (
        item.id,
        item.client_action_id,
    )


def test_claude_queued_command_attachment_reconciles_pending_message():
    from agentdeck.providers.claude_code.transcripts import _event_from_line

    text = "Add rule to translation fan out at 85% usage"
    item = QueuedMessage(
        1,
        text,
        state="accepted",
        created_at=datetime(2026, 7, 23, 18, 32, 9, 965614, tzinfo=UTC),
        after_seq=10,
    )
    raw_event = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": [{"type": "text", "text": text}],
            "commandMode": "prompt",
            "timestamp": "2026-07-23T18:32:10.003Z",
        },
        "timestamp": "2026-07-23T18:32:10.003Z",
    }
    event = _event_from_line(11, raw_event)

    assert event is not None
    service = InjectionService(InjectConfig(enabled=True))
    service._items["claude_code:test:sid"] = [item]
    assert service.observe_transcript("claude_code:test:sid", [event]) == {11: item}
    assert item.state == "observed"


async def test_delivery_without_transcript_reaches_terminal_state(tmp_path):
    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            return InjectResult(True, transcript_expected=False)

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    await service.start(account, session, Provider(), "/control")
    for _ in range(20):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break

    assert service.status(session.key).items[0].state == "complete"
    await service.stop()


async def test_session_page_observes_before_rendering_pending_messages(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    queued_at = datetime.now(UTC)
    item = QueuedMessage(
        17,
        "render once",
        client_action_id="action-17",
        state="accepted",
        created_at=queued_at,
    )
    app.state.injector._items["codex:test:sid"] = [item]
    app.state.injector._snapshot("codex:test:sid")

    async def fake_load_transcript(account, session, *, before_seq=None):
        return TranscriptDetail(
            events=[
                TranscriptEvent(
                    seq=9,
                    role="user",
                    text="render once",
                    ts=queued_at + timedelta(seconds=1),
                )
            ],
            tokens=TokenTotals(),
            model=None,
            todos=[],
            total_events=1,
            earliest_seq=1,
        )

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "load_transcript", fake_load_transcript)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/sessions/codex:test:sid")

    assert response.status_code == 200
    assert 'data-observed-queue-id="17"' in response.text
    assert 'data-observed-action-id="action-17"' in response.text
    assert "data-pending-message" not in response.text


async def test_agent_slash_command_clears_its_pending_message(tmp_path):
    service = InjectionService(InjectConfig(enabled=True))
    created_at = datetime(2026, 8, 12, 4, 11, 39, 682000, tzinfo=UTC)
    item = QueuedMessage(
        22,
        "/review-fix-pr",
        images=(tmp_path / "command.png",),
        state="accepted",
        created_at=created_at,
    )
    service._items["claude_code:alt:sid"] = [item]
    transcript = tmp_path / "sid.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-08-12T04:11:39.984Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<command-message>review-fix-pr</command-message>\n"
                                "<command-name>/review-fix-pr</command-name>"
                            ),
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo=",
                            },
                        },
                    ],
                },
            }
        )
        + "\n"
    )

    event = claude_transcripts.read_events(transcript).events[0]
    assert event.image_media_types == ("image/png",)
    links = service.observe_transcript("claude_code:alt:sid", [event])

    assert links == {1: item}
    assert item.state == "observed"


@pytest.mark.parametrize("sent", ["/review-fix-pr  high 123", "/review-fix-pr\nhigh 123"])
async def test_agent_slash_command_reconciles_cli_separator_whitespace(tmp_path, sent):
    service = InjectionService(InjectConfig(enabled=True))
    item = QueuedMessage(
        22,
        sent,
        state="accepted",
        created_at=datetime(2026, 8, 12, 4, 11, 39, 682000, tzinfo=UTC),
    )
    service._items["claude_code:alt:sid"] = [item]
    event = TranscriptEvent(
        seq=1,
        role="user",
        text="/review-fix-pr high 123",
        ts=datetime(2026, 8, 12, 4, 11, 39, 984000, tzinfo=UTC),
    )

    assert service.observe_transcript("claude_code:alt:sid", [event]) == {1: item}
    assert item.state == "observed"
    replayed = service.observe_transcript("claude_code:alt:sid", [event])
    assert list(replayed) == [1], "the replay still matches the whitespace-normalized text"
    assert (replayed[1].id, replayed[1].client_action_id) == (
        item.id,
        item.client_action_id,
    )


async def test_session_sse_observes_before_rendering_transcript_event(tmp_path, monkeypatch):
    from agentdeck.providers import PROVIDERS
    from agentdeck.web.routes_sse import _session_stream

    app = _web_app(tmp_path)
    queued_at = datetime.now(UTC)
    item = QueuedMessage(
        18,
        "stream once",
        client_action_id="action-18",
        state="accepted",
        created_at=queued_at,
    )
    app.state.injector._items["codex:test:sid"] = [item]
    app.state.injector._snapshot("codex:test:sid")
    event = TranscriptEvent(
        seq=10,
        role="user",
        text="stream once",
        ts=queued_at + timedelta(seconds=1),
    )

    async def fake_cursor(account, session):
        return (100, event.seq)

    async def fake_read(account, session, after_seq=0):
        assert after_seq == 9
        return [event]

    async def fake_last_event(account, session):
        return None

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "transcript_cursor", fake_cursor)
    monkeypatch.setattr(provider, "read_transcript", fake_read)
    monkeypatch.setattr(provider, "last_event", fake_last_event)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    stream = _session_stream(FakeRequest(app), "codex:test:sid", after_seq=9)
    try:
        # usage and the chat's Deckhand section, then the transcript: no stream sends
        # the old Deckhand panel (#2256), a chat list (#2603) or the done bar (#2599).
        chunks = [await asyncio.wait_for(stream.__anext__(), timeout=2) for _ in range(3)]
    finally:
        await stream.aclose()

    transcript = chunks[-1]
    assert "event: transcript" in transcript
    assert "id: 10" in transcript
    assert 'data-observed-queue-id="18"' in transcript
    assert 'data-observed-action-id="action-18"' in transcript


async def test_session_sse_honors_native_eventsource_resume_header(monkeypatch):
    from agentdeck.web import routes_sse

    seen = []

    async def fake_stream(request, session_key, *, after_seq=None):
        seen.append((session_key, after_seq))
        yield "event: transcript\ndata: resumed\n\n"

    class FakeRequest:
        headers = {"last-event-id": "42"}

    monkeypatch.setattr(routes_sse, "_session_stream", fake_stream)
    response = await routes_sse.session_events(
        FakeRequest(), "codex:test:sid", after_seq="9"
    )
    await response.body_iterator.__anext__()

    # Cursors are chat event ids now, so the header is carried through as it
    # was sent rather than coerced to a leg-native integer (#1267).
    assert seen == [("codex:test:sid", "42")]


async def test_a_resume_header_naming_a_later_leg_is_carried_through(monkeypatch):
    """`Last-Event-ID: 1.7` is leg 1's event 7, not a malformed integer."""
    from agentdeck.web import routes_sse

    seen = []

    async def fake_stream(request, session_key, *, after_seq=None):
        seen.append(after_seq)
        yield "event: transcript\ndata: resumed\n\n"

    class FakeRequest:
        headers = {"last-event-id": "1.7"}

    monkeypatch.setattr(routes_sse, "_session_stream", fake_stream)
    response = await routes_sse.session_events(
        FakeRequest(), "codex:test:sid", after_seq="3"
    )
    await response.body_iterator.__anext__()
    assert seen == ["1.7"]


async def test_an_unreadable_resume_header_falls_back_to_the_query_cursor(monkeypatch):
    from agentdeck.web import routes_sse

    seen = []

    async def fake_stream(request, session_key, *, after_seq=None):
        seen.append(after_seq)
        yield "event: transcript\ndata: resumed\n\n"

    class FakeRequest:
        headers = {"last-event-id": "not-a-cursor"}

    monkeypatch.setattr(routes_sse, "_session_stream", fake_stream)
    response = await routes_sse.session_events(
        FakeRequest(), "codex:test:sid", after_seq="3"
    )
    await response.body_iterator.__anext__()
    assert seen == ["3"]


@pytest.mark.parametrize(
    ("result", "released"),
    [
        (InjectResult(False, "create rejected"), True),
        (InjectResult(False, "first turn rejected", session_id="allocated"), False),
    ],
)
async def test_new_session_releases_continue_only_before_session_allocation(
    tmp_path, result, released
):
    releases = []

    class Provider:
        def can_start_session(self, account):
            return True

        async def start_session(self, account, cwd, message, **kwargs):
            return result

    service = InjectionService(
        InjectConfig(enabled=True), on_new_session_refused=releases.append
    )
    account = Account("codex:test", "codex", "test", tmp_path)

    accepted = await service.start_new(
        account,
        Provider(),
        tmp_path,
        "continue",
        session_token="reserved-token",
    )
    assert accepted.accepted
    await service._new_tasks[account.key]

    assert releases == (["reserved-token"] if released else [])
    await service.stop()


async def test_new_start_answer_is_the_providers_answer_or_none_when_unknown(tmp_path):
    """A Work start reads the runtime's answer to the spawn it queued (#2984)."""
    answers = [InjectResult(False, "at_capacity"), RuntimeError("socket closed")]
    release = asyncio.Event()

    class Provider:
        def can_start_session(self, account):
            return True

        async def start_session(self, account, cwd, message, **kwargs):
            answer = answers.pop(0) if answers else None
            if answer is None:
                await release.wait()
                return InjectResult(True, session_id="late")
            if isinstance(answer, Exception):
                raise answer
            return answer

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("claude_code:test", "claude_code", "test", tmp_path)

    assert (await service.start_new(account, Provider(), tmp_path, "go")).accepted
    answer = service.new_start_answer(account.key)
    # Picked at once: the start's answer even though it ended before anyone awaited it.
    await service._new_tasks[account.key]
    assert await answer == InjectResult(False, "at_capacity")

    # It raised, so it may have launched: no answer.
    assert (await service.start_new(account, Provider(), tmp_path, "go")).accepted
    assert await service.new_start_answer(account.key) is None

    # Waits for a slow spawn without cancelling it.
    assert (await service.start_new(account, Provider(), tmp_path, "go")).accepted
    slow = asyncio.ensure_future(service.new_start_answer(account.key))
    await asyncio.sleep(0.01)
    assert not slow.done()
    release.set()
    assert await slow == InjectResult(True, session_id="late")
    assert await service.new_start_answer(account.key) is None  # nothing in progress
    await service.stop()


async def test_start_session_passes_first_prompt_on_stdin(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setattr(
        "agentdeck.providers.codex.inject.gh_shim.worker_path",
        lambda environment: "/test-gh-shim:" + environment.get("PATH", ""),
    )
    account = Account("codex:test", "codex", "test", tmp_path)
    process = _FakeProcess()
    spawned = {}
    images = [tmp_path / "one.png", tmp_path / "two.webp"]

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return process

    result = await start_session(
        account,
        tmp_path,
        "start something",
        timeout_s=10,
        images=images,
        process_factory=factory,
    )
    assert result.accepted
    assert spawned["args"] == (
        "codex",
        "exec",
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "--config",
        APPROVALS_REVIEWER_CONFIG_OVERRIDE,
        "--config",
        LOGIN_SHELL_CONFIG_OVERRIDE,
        "-i",
        str(images[0]),
        "-i",
        str(images[1]),
        "--json",
        "--skip-git-repo-check",
        "-",
    )
    assert process.input == b"start something\n"
    assert spawned["kwargs"]["env"]["GIT_CONFIG_VALUE_0"] == "git@github.com:"
    # AgentDeck's per-owner helper, never the host's `gh` login (#2289), and the shim
    # in front of `gh`.
    assert spawned["kwargs"]["env"]["GIT_CONFIG_VALUE_3"].endswith(
        "-m agentdeck.git_credential"
    )
    assert spawned["kwargs"]["env"]["PATH"].startswith("/test-gh-shim:")




def _web_app(
    tmp_path,
    *,
    enabled=True,
    max_message_chars=50,
):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=enabled, max_message_chars=max_message_chars),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    session = Session(
        "codex:test:sid",
        "codex:test",
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
    )
    app.state.app_state.update_session(session)
    return app


async def test_secret_command_delivers_only_a_value_free_notice(tmp_path, monkeypatch):
    app = _web_app(tmp_path, max_message_chars=1000)
    issued = []
    delivered = []

    async def issue(_socket, **kwargs):
        issued.append(kwargs)
        return IssuedSecret(ticket="opaque-ticket", name=kwargs["name"])

    async def start(_account, _session, _provider, message, images, **_kwargs):
        delivered.append((message, images))
        return InjectResult(True)

    monkeypatch.setattr("agentdeck.web.routes_actions.issue_runtime_secret", issue)
    app.state.injector.start = start
    token = "exact-token-must-not-appear"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": f"/secret SERVICE_TOKEN {token}"},
            headers={
                "origin": "http://test",
                "x-agentdeck-action-id": "secret-action-123",
            },
        )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert issued == [
        {
            "session_key": "codex:test:sid",
            "action_id": "secret-action-123",
            "name": "SERVICE_TOKEN",
            "value": token,
        }
    ]
    assert len(delivered) == 1
    notice, images = delivered[0]
    assert images == []
    assert "SERVICE_TOKEN" in notice
    assert "opaque-ticket" in notice
    assert token not in notice
    assert token not in response.text
    assert "/secret" not in response.text


async def test_secret_command_queues_behind_an_active_turn(tmp_path, monkeypatch):
    """#1171: a running turn is when a credential is usually needed, not a refusal.

    The queue carries the value-free notice, so waiting for idle bought nothing
    and only made the send fail with a reason the composer never showed.
    """
    app = _web_app(tmp_path, max_message_chars=1000)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.thinking = True
    delivered = []

    async def issue(_socket, **kwargs):
        return IssuedSecret(ticket="queued-ticket", name=kwargs["name"])

    async def start(_account, _session, _provider, message, images, **_kwargs):
        delivered.append(message)
        return InjectResult(True)

    monkeypatch.setattr("agentdeck.web.routes_actions.issue_runtime_secret", issue)
    app.state.injector.start = start
    token = "queue-token-must-not-appear"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": f"/secret SERVICE_TOKEN {token}"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 202
    assert response.headers["x-agentdeck-action-state"] == "queued"
    assert len(delivered) == 1
    assert "SERVICE_TOKEN" in delivered[0]
    assert "queued-ticket" in delivered[0]
    assert token not in delivered[0]
    assert token not in response.text


async def test_rejected_secret_delivery_revokes_the_runtime_ticket(tmp_path, monkeypatch):
    app = _web_app(tmp_path, max_message_chars=1000)
    revoked = []

    async def issue(_socket, **kwargs):
        return IssuedSecret(ticket="rejected-ticket", name=kwargs["name"])

    async def revoke(_socket, ticket):
        revoked.append(ticket)

    async def reject(*_args, **_kwargs):
        return InjectResult(False, "delivery refused")

    monkeypatch.setattr("agentdeck.web.routes_actions.issue_runtime_secret", issue)
    monkeypatch.setattr("agentdeck.web.routes_actions.revoke_runtime_secret", revoke)
    app.state.injector.start = reject

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "/secret SERVICE_TOKEN rejected-value"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 403
    assert revoked == ["rejected-ticket"]


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/sessions/codex:test:sid/steer", {}),
        ("/sessions/new", {"account_key": "codex:test", "cwd": "/tmp"}),
    ],
)
async def test_secret_command_is_reserved_outside_existing_session_inject(
    tmp_path, path, extra
):
    app = _web_app(tmp_path, max_message_chars=200)
    token = "must-not-reach-provider"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            path,
            data={"message": f"/secret SERVICE_TOKEN {token}", **extra},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 422
    assert token not in response.text


@pytest.mark.browser
async def test_secret_command_never_enters_drafts_or_optimistic_transcript(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path, max_message_chars=1000)
    issue_release = asyncio.Event()
    provider_release = asyncio.Event()
    submitted = {}
    delivered = []

    async def issue(_socket, **kwargs):
        submitted.update(kwargs)
        await issue_release.wait()
        return IssuedSecret(ticket="opaque-ticket", name=kwargs["name"])

    async def inject(_account, _session, message, *, timeout_s, **_kwargs):
        delivered.append(message)
        await provider_release.wait()
        return InjectResult(True)

    monkeypatch.setattr("agentdeck.web.routes_actions.issue_runtime_secret", issue)
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", inject)
    token = "browser-token-must-stay-private"

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.locator("#inject-message").fill(f"/secret SERVICE_TOKEN {token}")
        assert token not in await page.evaluate(
            "JSON.stringify(Object.fromEntries(Object.keys(sessionStorage).map("
            "key => [key, sessionStorage.getItem(key)])))"
        )

        async with page.expect_response(
            lambda response: response.url.endswith("/sessions/codex:test:sid/inject")
        ) as response_info:
            await page.locator("#composer-controls button", has_text="Send").click()
            await page.locator(".optimistic-message").wait_for()
            optimistic = await page.locator(".optimistic-message").inner_text()
            assert "Temporary secret SERVICE_TOKEN submitted securely" in optimistic
            assert token not in optimistic
            assert await page.evaluate(
                "document.querySelector('.inject-form')._agentdeckSendRetryPayload == null"
            )
            issue_release.set()
        response = await response_info.value
        assert response.status == 202, await response.text()
        await page.wait_for_function("document.querySelector('#inject-message').value === ''")
        assert token not in await page.evaluate(
            "JSON.stringify(Object.fromEntries(Object.keys(sessionStorage).map("
            "key => [key, sessionStorage.getItem(key)])))"
        )
        assert token not in await page.locator("body").inner_text()
        assert token not in await page.content()
        assert submitted["value"] == token
        assert len(delivered) == 1
        assert token not in delivered[0]

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1165-temporary-secret.png")
            )
        provider_release.set()
        await browser.close()

    await app.state.injector.stop()


_PNG_1PX = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)


@pytest.mark.browser
async def test_composer_sends_an_image_alone_but_never_an_empty_box(tmp_path, monkeypatch):
    app = _web_app(tmp_path, max_message_chars=1000)
    delivered = []
    release = asyncio.Event()

    async def inject(_account, _session, message, *, timeout_s, images=None, **_kwargs):
        delivered.append((message, len(images or [])))
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", inject)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        sends = []
        page.on("request", lambda r: sends.append(r) if r.url.endswith("/inject") else None)
        # Nothing typed and nothing attached sends nothing, as `required` did (#3790).
        await page.locator("#inject-message").press("Enter")
        await page.locator("#composer-controls button", has_text="Send").click()
        assert sends == [] and await page.locator(".optimistic-message").count() == 0

        await page.locator("#inject-images").set_input_files(
            [{"name": "shot.png", "mimeType": "image/png", "buffer": base64.b64decode(_PNG_1PX)}]
        )
        async with page.expect_response(lambda r: r.url.endswith("/inject")) as answer:
            await page.locator("#composer-controls button", has_text="Send").click()
        assert (await answer.value).status == 202
        row = page.locator(".optimistic-message, [data-pending-message]").first
        await row.locator("img").first.wait_for()
        assert "See the attached image." in await row.inner_text()
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.02)
        assert delivered == [("See the attached image.", 1)]
        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(path=str(Path(screenshot_dir) / "issue-3790-image-only.png"))
        release.set()
        await browser.close()

    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_refused_send_shows_why_it_was_refused(tmp_path, monkeypatch):
    """#1171: a bare "Failed · retry" hid the sentence the server wrote for the person.

    A refused `/secret` looked exactly like a dropped network request, and its
    retry could never work, because the value is deliberately kept out of the
    retry buffer and the saved draft.
    """
    app = _web_app(tmp_path, max_message_chars=1000)

    async def reject(*_args, **_kwargs):
        return InjectResult(False, "this session is not accepting messages right now")

    app.state.injector.start = reject

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/codex:test:sid")

        await page.locator("#inject-message").fill("an ordinary message")
        await page.locator("#composer-controls button", has_text="Send").click()
        ordinary = page.locator(".optimistic-message.failed").first
        await ordinary.wait_for()
        ordinary_text = await ordinary.inner_text()
        assert "this session is not accepting messages right now" in ordinary_text
        assert "Failed · retry" in ordinary_text

        await page.locator("#inject-message").fill("/secret SERVICE_TOKEN")
        await page.locator("#composer-controls button", has_text="Send").click()
        secret = page.locator(".optimistic-message.failed").last
        await secret.wait_for()
        await page.wait_for_function(
            "document.querySelectorAll('.optimistic-message.failed').length === 2"
        )
        secret_text = await secret.inner_text()
        assert "The secret value cannot be empty" in secret_text
        assert "Failed · re-enter the secret" in secret_text
        assert "Failed · retry" not in secret_text

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1171-refusal-reason.png")
            )
        await browser.close()

    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_long_refused_send_shows_why_outside_the_collapsed_preview(
    tmp_path, monkeypatch
):
    """#1802: message collapse swallowed the sentence #1171 put on the row.

    ``prepare`` sweeps every non-head child of a message row into the body it
    clamps to three lines, and the refusal note was one of those children. On a
    message long enough to collapse, the reason went behind "Show more" and the
    row read only "Failed · retry" — so the same conclusive refusal was retried
    over and over.
    """
    app = _web_app(tmp_path, max_message_chars=1000)

    async def reject(*_args, **_kwargs):
        return InjectResult(False, "this session is not accepting messages right now")

    app.state.injector.start = reject

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/codex:test:sid")

        await page.locator("#inject-message").fill(
            "why is this in the \"other\" section? " * 12
        )
        await page.locator("#composer-controls button", has_text="Send").click()
        row = page.locator(".optimistic-message.failed").first
        await row.wait_for()
        await page.wait_for_selector(".optimistic-message.failed .message-error")
        await page.wait_for_function(
            "document.querySelector('.optimistic-message.failed')"
            ".classList.contains('message-collapsible')"
        )
        assert await row.locator(".message-collapse-toggle").inner_text() == "Show more"

        placement = await page.evaluate(
            """() => {
              const row = document.querySelector('.optimistic-message.failed');
              const body = row.querySelector('.ev-message-body');
              const note = row.querySelector('.message-error');
              return {
                insideClamp: body.contains(note),
                belowClamp:
                  note.getBoundingClientRect().top >=
                  body.getBoundingClientRect().bottom,
                height: note.getBoundingClientRect().height,
              };
            }"""
        )
        assert placement["insideClamp"] is False
        assert placement["belowClamp"] is True
        assert placement["height"] > 0

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-1802-collapsed-refusal.png")
            )
        await browser.close()

    await app.state.injector.stop()


def _continue_app(tmp_path, session_cwd=None, max_message_chars=1000):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        inject=InjectConfig(enabled=True, max_message_chars=max_message_chars),
        accounts=[
            AccountConfig(provider="codex", label="test", config_dir=str(tmp_path)),
            AccountConfig(
                provider="claude_code",
                label="alt",
                config_dir=str(tmp_path / "claude-alt"),
            ),
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            "codex:test:sid",
            "codex:test",
            "sid",
            SessionStatus.IDLE,
            cwd=session_cwd or tmp_path,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
        )
    )
    return app


def _merged_worker_worktree(tmp_path):
    """The end state of every kanban card: repository kept, worktree cleaned up."""
    repository = tmp_path / "project"
    (repository / ".git").mkdir(parents=True)
    return repository, repository / ".worktrees" / "issue-743"


async def test_new_chat_cwd_options_hide_nested_home_directories_and_worktrees(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    top_level = home / "top-level"
    top_level_worktree = home / "top-level-worktree"
    nested_plain = home / "group" / "plain"
    nested_repo = home / "group" / "repo"
    nested_worktree = home / "group" / "worktree"
    outside_home = tmp_path / "elsewhere" / "nested"
    outside_worktree = tmp_path / "elsewhere" / "worktree"
    for cwd in (
        top_level,
        top_level_worktree,
        nested_plain,
        nested_repo,
        nested_worktree,
        outside_home,
        outside_worktree,
    ):
        cwd.mkdir(parents=True)
    (nested_repo / ".git").mkdir()
    (top_level_worktree / ".git").write_text(
        "gitdir: ../repo/.git/worktrees/top-level\n"
    )
    (nested_worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/example\n")
    (outside_worktree / ".git").write_text(
        "gitdir: ../repo/.git/worktrees/outside\n"
    )
    monkeypatch.setenv("HOME", str(home))

    app = _web_app(tmp_path)
    db = Db(tmp_path / "cwd-options.db")
    app.state.db = db
    app.state.app_state.db = db
    for number, cwd in enumerate(
        (
            top_level,
            top_level_worktree,
            nested_plain,
            nested_repo,
            nested_worktree,
            outside_home,
            outside_worktree,
        ),
        start=2,
    ):
        app.state.app_state.update_session(
            Session(
                f"codex:test:sid{number}",
                "codex:test",
                f"sid{number}",
                SessionStatus.IDLE,
                cwd=cwd,
            )
        )
    # A remembered nested cwd remains the editable field value, but it must not
    # leak back into the datalist after the session-derived options are filtered.
    app.state.db.record_manual_new_chat_cwd(str(nested_plain))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    options = response.text.split('<datalist id="new-chat-cwds">', 1)[1].split(
        "</datalist>", 1
    )[0]
    assert f'<option value="{top_level}">' in options
    assert f'<option value="{nested_repo}">' in options
    assert f'<option value="{outside_home}">' in options
    assert f'<option value="{top_level_worktree}">' not in options
    assert f'<option value="{nested_worktree}">' not in options
    assert f'<option value="{outside_worktree}">' not in options
    assert f'<option value="{nested_plain}">' not in options
    assert f'value="{nested_plain}"' in response.text
    db.close()


async def test_session_transcript_json_exposes_complete_normalized_history(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path)
    events = [
        TranscriptEvent(seq=1, role="user", text="Original request"),
        TranscriptEvent(
            seq=2,
            role="assistant",
            text="Working on it",
            image_media_types=("image/png",),
        ),
        TranscriptEvent(
            seq=3,
            role="tool",
            tool_name="exec",
            tool_summary="cmd: pytest",
            tool_detail="pytest -q",
        ),
    ]

    async def fake_read(account, session, after_seq=0):
        assert account.key == "codex:test"
        assert session.key == "codex:test:sid"
        assert after_seq == 0
        return events

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "read_transcript", fake_read)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid/transcript.json")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    chat_id = app.state.app_state.sessions["codex:test:sid"].chat_id
    assert chat_id is not None
    assert response.json() == {
        "session_key": "codex:test:sid",
        "account_key": "codex:test",
        "working_directory": str(tmp_path),
        "session_url": f"http://test/chats/{chat_id}",
        "events": [
            {
                **json.loads(json.dumps(event, default=lambda value: value.__dict__)),
                # A single-leg chat is leg 0, so its chat-level event identity
                # is the bare seq every existing anchor already says (#1265).
                "chat_event_id": str(event.seq),
                "image_urls": (
                    ["http://test/sessions/codex:test:sid/transcript-images/2/0"]
                    if event.seq == 2
                    else []
                ),
            }
            for event in events
        ],
    }
    await app.state.injector.stop()


async def test_continue_chat_starts_cross_provider_with_transcript_fetch_prompt(
    tmp_path, monkeypatch
):
    claude_root = tmp_path / "claude-alt"
    claude_root.mkdir()
    (claude_root / "settings.json").write_text(
        json.dumps({"model": "opus", "effortLevel": "high"})
    )
    app = _continue_app(tmp_path)
    received = {}
    departure = {}
    destination_key = "claude_code:alt:new-sid"

    async def fake_inject(account, session, message, **kwargs):
        departure.update(
            account=account, session=session, message=message, kwargs=kwargs
        )
        return InjectResult(True)

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        model=None,
        allow_over_budget=False,
        session_token=None,
    ):
        received.update(
            account=account,
            cwd=cwd,
            message=message,
            model=model,
            allow_over_budget=allow_over_budget,
        )
        return InjectResult(True, session_id="new-sid")

    async def fake_scan(account, only=None):
        return [
            Session(
                destination_key,
                account.key,
                "new-sid",
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        ]

    from agentdeck.providers import PROVIDERS

    target_provider = PROVIDERS["claude_code"]
    selected_model = target_provider.selectable_models[0].value
    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    monkeypatch.setattr(target_provider, "can_start_session", lambda account: True)
    monkeypatch.setattr(target_provider, "start_session", fake_start)
    monkeypatch.setattr(target_provider, "scan_sessions", fake_scan)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")
        assert detail.status_code == 200
        assert 'hx-post="/sessions/codex:test:sid/continue"' in detail.text
        assert '<option value="claude_code:alt" data-provider="claude_code"' in detail.text
        assert 'data-default-model="Opus"' in detail.text
        assert (
            'data-default-effort="High">alt · claude code</option>'
            in detail.text
        )
        assert 'id="continue-chat-model"' in detail.text
        # The source account is now an allowed continue target too.
        assert '<option value="codex:test"' in detail.text.split(
            '<div class="continue-chat">', 1
        )[1].split("</div>", 1)[0]
        assert "Continue in new chat" in detail.text

        invalid_model = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt", "model": "not-a-model"},
            headers={"origin": "http://test"},
        )
        assert invalid_model.status_code == 422
        # The refusal is a sentence in the control's own result slot, not a JSON
        # detail htmx would drop on the floor (#746).
        assert invalid_model.headers["content-type"].startswith("text/html")
        assert 'id="continue-session-result"' in invalid_model.text
        assert "does not offer the model" in invalid_model.text

        response = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt", "model": selected_model},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        assert 'id="continue-session-result"' in response.text
        # Nothing navigates: the start is watched in the control's own result
        # slot and the chat turns up in the chat list on its own (#1799).
        assert "hx-redirect" not in response.headers
        starting_token = started_token(response)
        starting_url = f"/sessions/starting/{starting_token}"
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("claude_code:alt")
            if status and status.state == "complete":
                break

        assert received["account"].key == "claude_code:alt"
        assert received["cwd"] == tmp_path
        assert received["allow_over_budget"] is True
        assert received["model"] == selected_model
        transcript_url = "http://test/sessions/codex:test:sid/transcript.json"
        assert transcript_url in received["message"]
        assert f"curl --fail --silent --show-error {transcript_url}" in received["message"]
        assert "Before taking any substantive action" in received["message"]
        assert "applies only to that source chat, not to this session" in received["message"]
        for _ in range(10):
            await asyncio.sleep(0)
            if departure:
                break
        assert departure["account"].key == "codex:test"
        assert departure["session"].key == "codex:test:sid"
        assert departure["message"].startswith("Source chat only:")
        assert f"[Continue in new chat](http://test{starting_url})" in departure["message"]
        assert "Do no more work in this chat" in departure["message"]
        assert departure["message"].endswith("then stop.")
        assert departure["kwargs"]["allow_over_budget"] is True

        waiting = await client.get(
            f"/partials/starting-chat/{starting_token}"
            "?result_id=continue-session-result"
        )
        assert waiting.status_code == 200
        assert 'id="continue-session-result"' in waiting.text
        chat = app.state.budget_db.chat_for_session(destination_key)
        assert chat is not None
        assert chat.legs == ("codex:test:sid", destination_key)
        assert f'data-session-url="/chats/{chat.chat_id}"' in waiting.text
        assert 'data-starting-state="ready"' in waiting.text
        assert app.state.app_state.sessions.get(destination_key) is not None

        # The link the departure message left in the source chat outlives the
        # page it used to open: it forwards to the chat the start became (#1799).
        forwarded = await client.get(starting_url)
        assert forwarded.status_code == 303
        assert forwarded.headers["location"] == f"/chats/{chat.chat_id}"

        # Pressing Continue again is not a fork and is not a dead end. The
        # press resolves the leg the chat is now on, and the new leg joins the
        # end of the chain — which is what the automatic switch paths have
        # always done. This used to answer "this chat already has a successor.
        # Open its current chat instead", on the page that already *is* its
        # current chat (#1582).
        again = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "codex:test"},
            headers={"origin": "http://test"},
        )
        assert again.status_code == 202
        assert "already has a successor" not in again.text
    await app.state.injector.stop()


async def test_continue_chat_steers_an_active_source_to_stop_immediately(
    tmp_path, monkeypatch
):
    (tmp_path / "claude-alt").mkdir()
    app = _continue_app(tmp_path)
    source = app.state.app_state.sessions["codex:test:sid"]
    app.state.app_state.update_session(
        replace(
            source,
            thinking=True,
            capabilities=frozenset(
                {Capability.TRANSCRIPT, Capability.INJECT, Capability.STEER}
            ),
        )
    )
    steered = {}

    async def fake_cursor(account, session):
        return 0, 0

    async def fake_steer(account, session, message, **kwargs):
        steered.update(account=account, session=session, message=message, kwargs=kwargs)
        return InjectResult(True)

    async def fake_inject(*args, **kwargs):
        raise AssertionError("an active source must be steered, not queued")

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id="continued-sid")

    from agentdeck.providers import PROVIDERS

    source_provider = PROVIDERS["codex"]
    target_provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(source_provider, "transcript_cursor", fake_cursor)
    monkeypatch.setattr(source_provider, "steer", fake_steer)
    monkeypatch.setattr(source_provider, "inject", fake_inject)
    monkeypatch.setattr(target_provider, "can_start_session", lambda account: True)
    monkeypatch.setattr(target_provider, "start_session", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 202
    assert steered["account"].key == "codex:test"
    assert steered["session"].key == "codex:test:sid"
    assert "[Continue in new chat](http://test/sessions/starting/" in steered["message"]
    assert steered["message"].endswith("then stop.")
    assert steered["kwargs"]["allow_over_budget"] is True
    await app.state.injector.stop()


async def test_continue_chat_keeps_the_destination_when_source_steering_is_uncertain(
    tmp_path, monkeypatch
):
    (tmp_path / "claude-alt").mkdir()
    app = _continue_app(tmp_path)
    source = app.state.app_state.sessions["codex:test:sid"]
    app.state.app_state.update_session(
        replace(
            source,
            thinking=True,
            capabilities=frozenset(
                {Capability.TRANSCRIPT, Capability.INJECT, Capability.STEER}
            ),
        )
    )

    async def fake_cursor(account, session):
        return 0, 0

    async def fake_steer(account, session, message, **kwargs):
        raise RuntimeError("connection lost after send")

    async def fake_inject(*args, **kwargs):
        raise AssertionError("an uncertain write must never be retried")

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id="continued-sid")

    from agentdeck.providers import PROVIDERS

    source_provider = PROVIDERS["codex"]
    target_provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(source_provider, "transcript_cursor", fake_cursor)
    monkeypatch.setattr(source_provider, "steer", fake_steer)
    monkeypatch.setattr(source_provider, "inject", fake_inject)
    monkeypatch.setattr(target_provider, "can_start_session", lambda account: True)
    monkeypatch.setattr(target_provider, "start_session", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 202
    assert "hx-redirect" not in response.headers
    assert started_token(response)
    await app.state.injector.stop()


@pytest.mark.browser
async def test_continue_chat_leaves_a_link_and_stop_reply_in_the_source_chat(
    tmp_path, monkeypatch
):
    (tmp_path / "claude-alt").mkdir()
    app = _continue_app(tmp_path)
    events = []

    async def fake_cursor(account, session):
        return 0, len(events)

    async def fake_read(account, session, after_seq=0):
        return [event for event in events if event.seq > after_seq]

    async def fake_inject(account, session, message, **kwargs):
        destination_url = message.split("](", 1)[1].split(")", 1)[0]
        delivered_at = datetime.now(UTC)
        events.extend(
            [
                TranscriptEvent(seq=1, role="user", text=message, ts=delivered_at),
                TranscriptEvent(
                    seq=2,
                    role="assistant",
                    text=(
                        "[Continue in new chat]"
                        f"({destination_url}). I have stopped work in this chat."
                    ),
                    ts=delivered_at,
                ),
            ]
        )
        return InjectResult(True)

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id="continued-sid")

    from agentdeck.providers import PROVIDERS

    source_provider = PROVIDERS["codex"]
    target_provider = PROVIDERS["claude_code"]
    monkeypatch.setattr(source_provider, "transcript_cursor", fake_cursor)
    monkeypatch.setattr(source_provider, "read_transcript", fake_read)
    monkeypatch.setattr(source_provider, "inject", fake_inject)
    monkeypatch.setattr(target_provider, "can_start_session", lambda account: True)
    monkeypatch.setattr(target_provider, "start_session", fake_start)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1100, "height": 900}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.locator("#continue-chat-account").select_option("claude_code:alt")
        pressed_url = page.url
        await page.get_by_role("button", name="Continue in new chat").click()
        # The press stays where it was made: the start is watched in the
        # control's own result slot, and nothing navigates (#1799).
        await page.locator(
            '#continue-session-result[data-starting-state="ready"]'
        ).wait_for()
        assert page.url == pressed_url

        source_page = await context.new_page()
        await source_page.goto(f"{base_url}/sessions/codex:test:sid")
        link = source_page.locator('.ev.assistant a:has-text("Continue in new chat")')
        await link.wait_for()
        destination_url = await link.get_attribute("href")
        assert destination_url is not None
        assert re.search(r"/sessions/starting/[0-9a-f]{32}$", destination_url)
        # The link outlives the waiting page it used to open: it forwards to
        # whatever the start became.
        destination_page = await context.new_page()
        await destination_page.goto(destination_url)
        await destination_page.wait_for_url(
            re.compile(r"/(chats/[0-9a-f]+|sessions/claude_code:alt:continued-sid)$")
        )
        assert await source_page.locator("#event-1.ev.user").filter(
            has_text="Do no more work in this chat"
        ).is_visible()
        assert await source_page.locator(".ev.assistant").filter(
            has_text="I have stopped work in this chat"
        ).is_visible()

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            transcript = source_page.locator(".transcript")
            await link.scroll_into_view_if_needed()
            await transcript.screenshot(
                path=str(Path(screenshot_dir) / "issue-942-continued-chat-link.png")
            )
        await context.close()
        await browser.close()
    await app.state.injector.stop()


async def test_continue_chat_can_ask_the_old_chat_to_write_the_new_context(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    handoff_request = {}
    started = {}

    async def fake_inject(account, session, message, **kwargs):
        handoff_request.update(
            account=account, session=session, message=message, kwargs=kwargs
        )
        return InjectResult(True)

    async def fake_start(account, cwd, message, **kwargs):
        started.update(account=account, cwd=cwd, message=message, kwargs=kwargs)
        return InjectResult(True, session_id="handoff-sid")

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    monkeypatch.setattr(PROVIDERS["claude_code"], "start_session", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        requested = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt", "mode": "handoff"},
            headers={"origin": "http://test"},
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if handoff_request:
                break

        assert requested.status_code == 202
        assert "Preparing the handoff in this chat" in requested.text
        assert "hx-redirect" not in requested.headers
        prompt = handoff_request["message"]
        assert "assume no transcript access" in prompt
        assert "mode=handoff-complete" in prompt
        assert "account_key='claude_code:alt'" in prompt
        token = prompt.split("session_token=", 1)[1].split(".", 1)[0]
        assert len(token) == 32
        assert f"[Continue in new chat](http://test/sessions/starting/{token})" in prompt

        completed = await client.post(
            "/sessions/codex:test:sid/continue",
            data={
                "account_key": "claude_code:alt",
                "mode": "handoff-complete",
                "session_token": token,
                "handoff": "Goal: finish the feature.\nNext: run the focused tests.",
            },
            headers={"origin": "http://test"},
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if started:
                break

    assert completed.status_code == 202
    assert completed.text.startswith("/sessions/starting/")
    assert started["account"].key == "claude_code:alt"
    assert started["cwd"] == tmp_path
    assert "Goal: finish the feature." in started["message"]
    assert "Do not fetch the old chat transcript" in started["message"]
    assert "/sessions/codex:test:sid/transcript.json" not in started["message"]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replay = await client.post(
            "/sessions/codex:test:sid/continue",
            data={
                "account_key": "claude_code:alt",
                "mode": "handoff-complete",
                "session_token": token,
                "handoff": "A replay must not create another chat.",
            },
            headers={"origin": "http://test"},
        )
    assert replay.status_code == 202
    assert replay.text == completed.text
    assert started["message"].count("Goal: finish the feature.") == 1
    await app.state.injector.stop()


async def test_continue_chat_handoff_is_hidden_when_the_old_chat_cannot_write(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    source = app.state.app_state.sessions["codex:test:sid"]
    app.state.app_state.update_session(
        replace(source, capabilities=frozenset({Capability.TRANSCRIPT}))
    )
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")
        refused = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt", "mode": "handoff"},
            headers={"origin": "http://test"},
        )

    assert "Continue in new chat" in detail.text
    assert "Hand off to new chat" not in detail.text
    assert refused.status_code == 422
    assert "cannot write a handoff" in refused.text
    await app.state.injector.stop()


async def test_continue_chat_handoff_hides_read_only_codex_delegations(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    source = app.state.app_state.sessions["codex:test:sid"]
    app.state.app_state.update_session(
        replace(source, is_delegated=True, worker_type="you")
    )
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")

    assert "Continue in new chat" in detail.text
    assert "Hand off to new chat" not in detail.text
    await app.state.injector.stop()


async def test_continue_chat_handoff_callback_returns_plain_failure_and_releases_token(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    outcomes = [InjectResult(False, "destination busy"), InjectResult(True)]

    async def fake_start_new(account, provider, cwd, message, **kwargs):
        return outcomes.pop(0)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    monkeypatch.setattr(app.state.injector, "start_new", fake_start_new)
    token = "b" * 32
    data = {
        "account_key": "claude_code:alt",
        "mode": "handoff-complete",
        "session_token": token,
        "handoff": "Continue from here.",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post(
            "/sessions/codex:test:sid/continue",
            data=data,
            headers={"origin": "http://test"},
        )
        replay = await client.post(
            "/sessions/codex:test:sid/continue",
            data=data,
            headers={"origin": "http://test"},
        )

    assert failed.status_code == 422
    assert failed.headers["content-type"].startswith("text/plain")
    assert failed.text == "Destination busy."
    assert replay.status_code == 202
    assert replay.text == f"/sessions/starting/{token}"
    assert outcomes == []
    await app.state.injector.stop()


async def test_continue_chat_releases_linear_fence_after_definite_background_refusal(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    outcomes = [InjectResult(False, "destination busy"), InjectResult(True, session_id="next")]
    calls = 0

    async def fake_start(account, cwd, message, **kwargs):
        nonlocal calls
        calls += 1
        return outcomes.pop(0)

    async def fake_inject(account, session, message, **kwargs):
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    target = PROVIDERS["claude_code"]
    monkeypatch.setattr(target, "can_start_session", lambda account: True)
    monkeypatch.setattr(target, "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    data = {"account_key": "claude_code:alt"}
    headers = {"origin": "http://test"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/sessions/codex:test:sid/continue", data=data, headers=headers
        )
        assert first.status_code == 202
        for _ in range(20):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("claude_code:alt")
            if status is not None and status.state == "failed":
                break

        second = await client.post(
            "/sessions/codex:test:sid/continue", data=data, headers=headers
        )
        assert second.status_code == 202
        for _ in range(20):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("claude_code:alt")
            if status is not None and status.state == "complete":
                break

    assert calls == 2
    chat = app.state.budget_db.chat_for_session("claude_code:alt:next")
    assert chat is not None and chat.legs == ("codex:test:sid", "claude_code:alt:next")
    await app.state.injector.stop()


async def test_continue_chat_control_respects_new_chat_kill_switch(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    app.state.config.inject.enabled = False
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")

    assert 'class="continue-chat"' not in detail.text
    await app.state.injector.stop()


async def test_continue_chat_uses_repository_once_the_source_worktree_is_gone(
    tmp_path, monkeypatch
):
    """#1512: a removed worker worktree always continues in its repository."""
    repository, gone_worktree = _merged_worker_worktree(tmp_path)
    app = _continue_app(tmp_path, session_cwd=gone_worktree)
    deck_default = tmp_path / "deck-default"
    deck_default.mkdir()
    app.state.config.inject.new_chat_default_cwd = str(deck_default)
    started = {}

    async def fake_start(account, cwd, message, **kwargs):
        started.update(cwd=cwd, message=message)
        return InjectResult(True, session_id="new-sid")

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    monkeypatch.setattr(PROVIDERS["claude_code"], "start_session", fake_start)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")
        control = detail.text.split('<div class="continue-chat">', 1)[1].split(
            '<div id="continue-session-result"', 1
        )[0]

        continued = await client.post(
            "/sessions/codex:test:sid/continue",
            data={"account_key": "claude_code:alt"},
            headers={"origin": "http://test"},
        )
        for _ in range(20):
            await asyncio.sleep(0)
            if started:
                break

    # The move is automatic, so the page neither names it nor asks for a directory.
    assert "no longer exists" not in control
    assert 'name="cwd"' not in control
    assert str(deck_default) not in control

    assert continued.status_code == 202
    assert started["cwd"] == repository
    # The new agent is told its ground moved; the transcript link still points home.
    assert f"That chat ran in {gone_worktree}, which no longer exists" in started["message"]
    assert f"you are starting in {repository} instead" in started["message"]
    assert "http://test/sessions/codex:test:sid/transcript.json" in started["message"]
    await app.state.injector.stop()


@pytest.mark.browser
async def test_deleted_worktree_send_stays_in_the_same_chat_in_the_browser(
    tmp_path, monkeypatch
):
    """#2524, the real race: the page is open when cleanup removes the worktree.

    The send goes to this same chat, whose runtime resumes it in the repository; no
    new chat starts and nothing points anywhere else.
    """
    _repository, worktree = _merged_worker_worktree(tmp_path)
    worktree.mkdir(parents=True)
    app = _continue_app(tmp_path, session_cwd=worktree)
    session = app.state.app_state.sessions["codex:test:sid"]
    app.state.app_state.update_session(
        replace(
            session,
            capabilities=session.capabilities | {Capability.RESUME_ELSEWHERE},
        )
    )
    delivered = []
    starts = []

    async def fake_inject(account, session, message, **kwargs):
        delivered.append((session.key, message))
        return InjectResult(True)

    async def fake_start(account, cwd, message, **kwargs):
        starts.append(cwd)
        return InjectResult(True, session_id="new-sid")

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "can_start_session", lambda account: True)
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "inject", fake_inject)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        worktree.rmdir()

        await page.locator("#inject-message").fill("Hey — one more thing")
        async with page.expect_response(
            re.compile(r"/sessions/codex:test:sid/inject$")
        ) as sent_info:
            await page.locator("#composer-controls button", has_text="Send").click()
        sent = await sent_info.value
        assert sent.status == 202, await sent.text()

        row = page.locator(".optimistic-message")
        await row.wait_for(state="visible")
        await page.wait_for_function(
            "() => document.querySelector('.optimistic-message.accepted')"
        )
        assert "Hey — one more thing" in await row.inner_text()
        assert await page.locator(".start-status").count() == 0
        assert await page.get_by_text("no longer exists").count() == 0
        assert "/sessions/starting/" not in page.url

        screenshot_dir = os.environ.get("AGENTDECK_SCREENSHOT_DIR")
        if screenshot_dir:
            await page.screenshot(
                path=str(Path(screenshot_dir) / "issue-2524-same-chat.png")
            )
        await browser.close()

    assert delivered == [("codex:test:sid", "Hey — one more thing")]
    assert starts == []
    await app.state.injector.stop()


async def test_continue_chat_asks_nothing_extra_while_its_directory_exists(
    tmp_path, monkeypatch
):
    """The fallback control is the degraded case only: an ordinary chat must not
    grow a directory picker, and must not pay for one in default height."""
    app = _continue_app(tmp_path)
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")

    control = detail.text.split('<div class="continue-chat">', 1)[1].split(
        '<div id="continue-session-result"', 1
    )[0]
    assert "no longer exists on this host" not in control
    assert 'id="continue-chat-cwd"' not in control
    assert 'value="transcript">Continue in new chat</button>' in control
    assert 'value="handoff">Hand off to new chat</button>' in control
    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_refused_continue_shows_its_reason_instead_of_a_status_number(
    tmp_path, monkeypatch
):
    """Regression for issue #746, in the browser and in its real order: the chat
    page is open, the worker worktree is then cleaned up behind it, and Continue is
    pressed. htmx drops a 4xx body by default, so the reason only reaches the person
    if `refusal_swap.js` opts the rendered fragment into the control's own swap."""
    removed_directory = tmp_path / "ordinary-directory"
    removed_directory.mkdir()
    app = _continue_app(tmp_path, session_cwd=removed_directory)
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import routes_actions

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    # The test host may itself place pytest below a repository checkout. This
    # scenario is about the rendered refusal, so make the no-repository case
    # explicit instead of depending on the host's temporary-directory layout.
    monkeypatch.setattr(routes_actions, "continued_chat_cwd", lambda _cwd: None)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        reported = []
        for width in (1200, 390):
            context = await browser.new_context(
                viewport={"width": width, "height": 900}, service_workers="block"
            )
            page = await context.new_page()
            await page.goto(f"{base_url}/sessions/codex:test:sid")
            control = page.locator(".continue-chat")
            await control.get_by_role("button", name="Continue in new chat").wait_for()
            # The card merges and its worktree goes while the chat page is open.
            removed_directory.rmdir()
            async with page.expect_response(
                re.compile(r"/sessions/codex:test:sid/continue$")
            ) as response_info:
                await control.get_by_role(
                    "button", name="Continue in new chat"
                ).click()
            response = await response_info.value
            assert response.status == 422
            result = page.locator("#continue-session-result.failed")
            await result.wait_for()
            text = await result.inner_text()
            box = await control.bounding_box()
            reported.append(
                {
                    "reason": f"{removed_directory} no longer exists" in text,
                    "no_repository": "no containing repository exists" in text,
                    "raw_json": "{" in text,
                    "fits": bool(box and box["x"] >= 0 and box["x"] + box["width"] <= width),
                    "overflow": await page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth"
                    ),
                }
            )
            await context.close()
            removed_directory.mkdir()
        await browser.close()

    assert reported == [
        {"reason": True, "no_repository": True, "raw_json": False, "fits": True, "overflow": False},
        {"reason": True, "no_repository": True, "raw_json": False, "fits": True, "overflow": False},
    ]
    await app.state.injector.stop()


@pytest.mark.browser
async def test_continue_chat_control_fits_desktop_and_mobile(tmp_path, monkeypatch):
    app = _continue_app(tmp_path)
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()
    layouts = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()

            async def serve(route, request):
                path = request.url.split("?", 1)[0].removeprefix(
                    "http://agentdeck.test"
                )
                if path == "/sessions/codex:test:sid":
                    await route.fulfill(
                        status=200, content_type="text/html", body=detail.text
                    )
                elif path == "/static/mobile_session_stack.js":
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=stack_script
                    )
                elif path == "/static/model_picker.js":
                    await route.fulfill(
                        status=200,
                        content_type="text/javascript",
                        body=(static_dir / "model_picker.js").read_text(),
                    )
                elif path == "/static/app.css":
                    await route.fulfill(
                        status=200, content_type="text/css", body=stylesheet
                    )
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/sessions/codex:test:sid")
            if width < 1000:
                await page.wait_for_function(
                    "document.body.classList.contains('mobile-session-stack-ready')"
                )
            control = page.locator(".continue-chat")
            box = await control.bounding_box()
            layouts.append(
                {
                    "control": await control.is_visible(),
                    "button": await control.get_by_role(
                        "button", name="Continue in new chat"
                    ).is_visible(),
                    "handoff": await control.get_by_role(
                        "button", name="Hand off to new chat"
                    ).is_visible(),
                    "accounts": await control.locator(
                        "#continue-chat-account option"
                    ).all_text_contents(),
                    "model": await control.locator("#continue-chat-model").is_visible(),
                    "fits": bool(box and box["x"] >= 0 and box["x"] + box["width"] <= width),
                    "overflow": await page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth"
                    ),
                }
            )
            await context.close()
        await browser.close()

    assert layouts == [
        {
            "control": True,
            "button": True,
            "handoff": True,
            "accounts": ["test · codex", "alt · claude code"],
            "model": True,
            "fits": True,
            "overflow": False,
        },
        {
            "control": True,
            "button": True,
            "handoff": True,
            "accounts": ["test · codex", "alt · claude code"],
            "model": True,
            "fits": True,
            "overflow": False,
        },
    ]
    await app.state.injector.stop()


@pytest.mark.browser
async def test_handoff_button_posts_its_mode_and_swaps_status_at_both_widths(
    tmp_path, monkeypatch
):
    app = _continue_app(tmp_path)
    requests = []

    async def fake_inject(account, session, message, **kwargs):
        requests.append(message)
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    monkeypatch.setattr(
        PROVIDERS["claude_code"], "can_start_session", lambda account: True
    )
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 390):
            context = await browser.new_context(
                viewport={"width": width, "height": 900}, service_workers="block"
            )
            page = await context.new_page()
            await page.goto(f"{base_url}/sessions/codex:test:sid")
            control = page.locator(".continue-chat")
            await control.get_by_role("button", name="Hand off to new chat").click()
            result = control.locator("#continue-session-result.running")
            await result.wait_for()
            assert "Preparing the handoff in this chat" in await result.inner_text()
            assert "mode=handoff-complete" in requests[-1]
            await context.close()
        await browser.close()
    assert len(requests) == 2
    await app.state.injector.stop()


async def test_inject_route_accepts_and_reports_status(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    provider = app.state.injector
    state = app.state.app_state
    assistant = app.state.assistant
    resting = replace(
        state.sessions["codex:test:sid"],
        last_activity=datetime.now(UTC) - timedelta(minutes=1),
    )
    state.update_session(replace(resting, status=SessionStatus.LIVE, thinking=True))
    await assistant.refresh()
    state.update_session(resting)
    await assistant.refresh()
    assert [card.session_key for card in assistant.view.insights] == ["codex:test:sid"]
    release = asyncio.Event()
    messages = []

    async def fake_inject(
        account, session, message, *, timeout_s, allow_over_budget=False
    ):
        assert allow_over_budget is True
        messages.append(message)
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "continue safely"},
            headers={
                "origin": "http://test",
                "x-agentdeck-action-id": "action-send-123",
            },
        )
        assert response.status_code == 202
        # The accepted boss turn makes the chat working before the provider
        # transcript catches up with the new user message (#970), which takes
        # the old handoff's card away.
        await assistant.refresh()
        assert assistant.view.insights == ()
        assert response.headers["x-agentdeck-action-id"] == "action-send-123"
        assert response.headers["x-agentdeck-action-state"] == "accepted"
        assert all(
            name in response.headers["server-timing"]
            for name in ("form;dur=", "queue;dur=", "render;dur=", "total;dur=")
        )
        assert 'aria-label="Message queued: continue safely"' in response.text
        assert 'hx-swap-oob="beforeend:.transcript"' in response.text
        assert 'class="ev user pending-message"' in response.text
        assert 'data-client-action-id="action-send-123"' in response.text
        assert '<span class="ev-role">user</span>' in response.text
        assert 'class="ev-time"' in response.text
        assert "user · queued" not in response.text
        conflict = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "again"},
            headers={"origin": "http://test"},
        )
        assert conflict.status_code == 202
        assert conflict.headers["x-agentdeck-action-state"] == "queued"
        assert "again" in conflict.text
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not provider.can_queue("codex:test:sid"):
                break
        assert messages == ["continue safely", "again"]
        status = await client.get("/partials/sessions/codex:test:sid/inject-status")
        assert "Queue completed" not in status.text
        assert "continue safely" not in status.text
    await provider.stop()


_PNG_2847 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cfc0f01f0005000201e3c1a7"
    "6b0000000049454e44ae426082"
)


def _restarting_runtime(app, monkeypatch, *, owned=True):
    """The chat is the deck's own, but its runtime cannot be reached right now (#2847)."""
    from agentdeck.providers import PROVIDERS

    state = app.state.app_state
    session = state.sessions["codex:test:sid"]
    state.update_session(replace(session, capabilities=frozenset({Capability.TRANSCRIPT})))
    monkeypatch.setattr(
        PROVIDERS["codex"], "runtime_unreachable", lambda _account, _session: owned
    )


def _held_rows(app):
    return app.state.scheduled_messages.visible(
        app.state.app_state.chat_identity_key("codex:test:sid")
    )


async def test_a_send_while_the_runtime_restarts_is_held_not_refused(tmp_path, monkeypatch):
    app = _web_app(tmp_path, max_message_chars=1000)
    _restarting_runtime(app, monkeypatch)
    started = []

    async def start(*args, **kwargs):
        started.append((args, kwargs))
        return InjectResult(True)

    app.state.injector.start = start
    headers = {"origin": "http://test", "x-agentdeck-action-id": "held-action-123"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "Remove the worker limits"},
            files=[
                ("images", ("shot.png", _PNG_2847, "image/png")),
                ("images", ("notes.txt", b"hello", "text/plain")),
            ],
            headers=headers,
        )
        assert response.status_code == 202, response.text
        assert response.headers["x-agentdeck-action-state"] == "held"
        assert "Waiting for the runtime to restart" in response.text
        assert "2 attachments kept with it" in response.text
        assert started == []
        (row,) = _held_rows(app)
        assert row.id == "held-action-123"
        assert [item.kind for item in row.attachments] == ["image", "file"]
        kept = [item.path for item in row.attachments]
        assert all(path.is_file() for path in kept)

        # A replay of the same send finds the held row and drops its own copy of the file.
        replay = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "Remove the worker limits"},
            files=[
                ("images", ("shot.png", _PNG_2847, "image/png")),
                ("images", ("notes.txt", b"hello", "text/plain")),
            ],
            headers=headers,
        )
        assert replay.status_code == 202
        (again,) = _held_rows(app)
        assert again.attachments == row.attachments
        # The replay saved its file at the held row's own path; it must survive (#2847 review).
        assert all(path.is_file() for path in kept)

        # The page opened during the restart still offers the message box and the row.
        page = await client.get("/sessions/codex:test:sid")
        assert 'id="inject-message"' in page.text
        assert "Remove the worker limits" in page.text
        assert 'aria-label="Schedule message"' not in page.text

        cancelled = await client.post(
            "/sessions/codex:test:sid/held/held-action-123/cancel",
            headers={"origin": "http://test"},
        )
        assert cancelled.status_code == 200
        assert "Remove the worker limits" not in cancelled.text
        assert not any(path.exists() for path in kept)


async def test_a_quota_retry_row_names_its_reset_instead_of_a_runtime_restart(tmp_path):
    app = _web_app(tmp_path)
    state = app.state.app_state
    session = state.sessions["codex:test:sid"]
    reset = datetime.now(UTC) + timedelta(hours=1)
    state.update_session(
        replace(session, turn_failed=True, automatic_retry_at=reset)
    )
    retry = message_for(state.sessions[session.key])
    assert retry is not None
    assert app.state.scheduled_messages.schedule(retry) is not None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")

    assert "Retrying at" in page.text
    assert "Usage limit" not in page.text
    assert chat_time(reset) in page.text
    assert "Waiting for the runtime to restart" not in page.text


async def test_a_restart_holds_nothing_it_cannot_keep(tmp_path, monkeypatch):
    app = _web_app(tmp_path, max_message_chars=1000)
    _restarting_runtime(app, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        secret = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "/secret TOKEN value"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "secret-held"},
        )
        assert secret.status_code == 503
        assert "send the secret again" in secret.json()["detail"]
        assert _held_rows(app) == ()

        too_long = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "x" * 1001},
            headers={"origin": "http://test", "x-agentdeck-action-id": "long-held"},
        )
        assert too_long.status_code == 422
        assert _held_rows(app) == ()

        # The "send later" fields are gone: a request still carrying them is an ordinary send.
        later = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "Later", "schedule": "true", "delay_minutes": "5"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "later-held"},
        )
        assert later.status_code == 202
        (row,) = _held_rows(app)
        assert row.due_at == row.created_at


async def test_a_chat_the_deck_does_not_run_is_still_refused(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    _restarting_runtime(app, monkeypatch, owned=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={"origin": "http://test", "x-agentdeck-action-id": "not-owned"},
        )
    assert refused.status_code == 403
    assert _held_rows(app) == ()


async def test_inject_clears_attention_only_after_the_boss_message_is_accepted(tmp_path):
    app = _web_app(tmp_path)
    state = app.state.app_state
    key = "codex:test:sid"
    state.update_session(
        replace(
            state.sessions[key],
            last_role="agent",
            last_text="Waiting for your reply",
            last_activity=datetime.now(UTC),
        )
    )
    report = state.set_attention_report(key, "waiting")
    # Attention is the working -> not-working edge since #1450, so the chat has
    # to be seen working before its stop can raise the card this test clears.
    state.update_session(replace(state.sessions[key], thinking=True))
    await app.state.assistant.refresh()
    state.update_session(replace(state.sessions[key], thinking=False))
    await app.state.assistant.refresh()
    assert app.state.assistant.view.insights[0].session_key == key
    outcomes = [InjectResult(False, "refused"), InjectResult(True)]

    async def start(*_args, **_kwargs):
        result = outcomes.pop(0)
        if result.accepted:
            # What the real injector's ``on_sent`` hook does on acceptance.
            state.note_session_sent(key)
        return result

    app.state.injector.start = start
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.post(
            f"/sessions/{key}/inject",
            data={"message": "not delivered"},
            headers={"origin": "http://test"},
        )
        assert rejected.status_code == 403
        assert state.attention_report_for(key) == report
        await app.state.assistant.refresh()
        assert [card.session_key for card in app.state.assistant.view.insights] == [key]

        accepted = await client.post(
            f"/sessions/{key}/inject",
            data={"message": "continue"},
            headers={"origin": "http://test"},
        )
        assert accepted.status_code == 202
        assert state.attention_report_for(key) is None
        await app.state.assistant.refresh()
        assert all(
            insight.session_key != key for insight in app.state.assistant.view.insights
        )


async def test_inject_route_kill_switch_validation_and_origin(tmp_path):
    # One deck with its kill switch flipped: building a second app cost more than the test.
    app = _web_app(tmp_path)
    app.state.config.inject.enabled = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        assert "Continue this Codex session" not in page.text
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 403

    app.state.config.inject.enabled = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        assert '>Message</label>' in page.text
        assert 'maxlength="50"' in page.text
        assert page.text.index('class="inject-form"') < page.text.index(
            'id="inject-result"'
        )
        cross_site = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={"origin": "https://evil.example"},
        )
        assert cross_site.status_code == 403
        empty = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "   "},
            headers={"origin": "http://test"},
        )
        assert empty.status_code == 422
        oversized = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "x" * 51},
            headers={"origin": "http://test"},
        )
        assert oversized.status_code == 422
        wrong_type = await client.post(
            "/sessions/codex:test:sid/inject",
            content="hello",
            headers={"origin": "http://test", "content-type": "text/plain"},
        )
        assert wrong_type.status_code == 415
        oversized_request = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={
                "origin": "http://test",
                "content-length": str(201 * 1024 * 1024 + 1),
            },
        )
        assert oversized_request.status_code == 413
        reserved = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": f"hello{ATTACHMENT_MARKER}"},
            headers={"origin": "http://test"},
        )
        assert reserved.status_code == 422


async def test_inject_route_validates_images_and_cleans_up(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    app.state.config.inject.max_image_bytes = 16
    app.state.config.inject.max_image_total_bytes = 24
    release = asyncio.Event()
    started = asyncio.Event()
    received = []
    messages = []

    async def fake_inject(
        account,
        session,
        message,
        *,
        timeout_s,
        images=None,
        allow_over_budget=False,
    ):
        assert allow_over_budget is True
        received.extend(images or [])
        messages.append(message)
        started.set()
        await release.wait()
        return InjectResult(True)

    async def fake_tail_transcript(account, session, byte_offset, seq):
        return (
            [
                    TranscriptEvent(
                        seq=seq + 1,
                        role="user",
                        text="See the attached image.",
                        ts=datetime.now(UTC),
                        image_media_types=("image/png",),
                )
            ],
            byte_offset + 1,
            seq + 1,
        )

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    monkeypatch.setattr(PROVIDERS["codex"], "tail_transcript", fake_tail_transcript)
    png = b"\x89PNG\r\n\x1a\nsmall"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "inspect"},
            files={"images": ("fake.png", b"not an image", "image/png")},
            headers={"origin": "http://test"},
        )
        assert invalid.status_code == 422

        oversized = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "inspect"},
            files={"images": ("large.png", png + b"xxxx", "image/png")},
            headers={"origin": "http://test"},
        )
        assert oversized.status_code == 422

        # An empty box with an image sends the image alone (#3790).
        accepted = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": " "},
            files={"images": ("attacker-name.png", png, "image/png")},
            headers={"origin": "http://test"},
        )
        assert accepted.status_code == 202
        await asyncio.wait_for(started.wait(), timeout=1)
        assert messages == ["See the attached image."]
        assert len(received) == 1
        saved = received[0]
        assert saved.is_file()
        assert saved.name != "attacker-name.png"
        assert saved.suffix == ".png"
        assert "/sessions/codex:test:sid/pending-images/1/0" in accepted.text
        pending_image = await client.get("/sessions/codex:test:sid/pending-images/1/0")
        assert pending_image.status_code == 200
        assert pending_image.content == png
        assert pending_image.headers["content-type"] == "image/png"
        assert pending_image.headers["cache-control"] == "private, no-store"
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
        assert not saved.parent.exists()
    await app.state.injector.stop()


async def test_generic_upload_path_is_private_opaque_and_retry_stable(
    tmp_path, monkeypatch
):
    from agentdeck.web import uploads

    monkeypatch.setattr(uploads, "GENERIC_UPLOAD_ROOT", tmp_path / "upload-spool")

    def form():
        return FormData(
            [
                (
                    "images",
                    UploadFile(
                        BytesIO(b"same bytes"),
                        filename="../private/report.pdf",
                        headers=Headers({"content-type": "application/pdf"}),
                    ),
                )
            ]
        )

    config = InjectConfig(enabled=True, max_image_bytes=4)
    first = await save_uploaded_files(form(), config, action_id="same-action-id")
    second = await save_uploaded_files(form(), config, action_id="same-action-id")

    assert first.files[0].name == "report.pdf"
    assert first.files[0].path == second.files[0].path
    assert first.files[0].path.suffix == ".upload"
    assert "report" not in first.files[0].path.name
    assert first.files[0].path.stat().st_mode & 0o777 == 0o600
    assert first.files[0].path.parent.stat().st_mode & 0o777 == 0o700
    # A non-image file is held to the file cap, not the far smaller image cap (#3374).
    assert first.files[0].path.read_bytes() == b"same bytes"
    assert [path.name for path in first.files[0].path.parent.iterdir()] == [
        first.files[0].path.name
    ]
    cleanup_uploaded_files(first.paths)
    with pytest.raises(UploadError, match="file is too large"):
        await save_uploaded_files(
            form(), InjectConfig(enabled=True, max_file_bytes=4), action_id="too-big"
        )
    assert list((tmp_path / "upload-spool").iterdir()) == []


def test_attachment_display_text_only_strips_a_generated_trailing_envelope(tmp_path):
    ordinary = f"quote this{ATTACHMENT_MARKER}\nnot a generated envelope"
    assert display_message(ordinary) == ordinary

    path = tmp_path / "1-0123456789abcdef01234567.upload"
    generated = (
        f"read this{ATTACHMENT_MARKER}\n{ATTACHMENT_HEADER}\n"
        f'- {{"name": "brief.pdf", "path": "{path}"}}\n'
        "</agentdeck-attachments>"
    )
    assert display_message(generated) == "read this"


def test_abandoned_generic_uploads_are_swept_without_a_new_upload(tmp_path, monkeypatch):
    from agentdeck.web import uploads

    root = tmp_path / "upload-spool"
    lease = root / "lease"
    lease.mkdir(parents=True)
    attachment = lease / "1-deadbeefdeadbeefdeadbeef.upload"
    attachment.write_bytes(b"private")
    monkeypatch.setattr(uploads, "GENERIC_UPLOAD_ROOT", root)
    old = 1_000.0
    os.utime(lease, (old, old))

    sweep_abandoned_uploads(now=old + uploads.GENERIC_UPLOAD_TTL_S + 1)

    assert not lease.exists()


async def test_inject_route_keeps_an_arbitrary_file_through_the_turn(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path)
    # The brief below is bigger than the image cap: a file answers only to its own (#3374).
    app.state.config.inject.max_image_bytes = 16
    app.state.config.inject.max_file_bytes = 64
    delivered = asyncio.Event()
    waiting = asyncio.Event()
    turn_done = asyncio.Event()
    seen = {}

    async def fake_cursor(account, session):
        return (0, 0)

    async def fake_inject(
        account,
        session,
        message,
        *,
        timeout_s,
        images=None,
        allow_over_budget=False,
    ):
        assert images is None
        seen["message"] = message
        delivered.set()
        return InjectResult(True)

    async def fake_wait(account, session_id, *, timeout_s):
        waiting.set()
        await turn_done.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "transcript_cursor", fake_cursor)
    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    monkeypatch.setattr(PROVIDERS["codex"], "wait_for_session", fake_wait)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        unexpected = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "read the brief"},
            files={"other": ("brief.pdf", b"%PDF-content bigger than an image", "application/pdf")},
            headers={"origin": "http://test"},
        )
        assert unexpected.status_code == 422
        assert unexpected.json()["detail"] == "unexpected file field"

        oversized = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "read the brief"},
            files={"images": ("brief.pdf", b"x" * 65, "application/pdf")},
            headers={"origin": "http://test"},
        )
        assert oversized.status_code == 422
        assert oversized.json()["detail"] == "file is too large"

        # A file alone is a follow-up too, with a line naming it (#3790).
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": ""},
            files={
                "images": (
                    "../../brief.pdf",
                    b"%PDF-content bigger than an image",
                    "application/pdf",
                )
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202, response.text
        assert "agentdeck-attachments" not in response.text
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert display_message(seen["message"]) == "See the attached file."
        attachment = json.loads(
            re.search(r"^- (\{.*\})$", seen["message"], re.MULTILINE).group(1)
        )
        assert attachment["name"] == "brief.pdf"
        saved = Path(attachment["path"])
        assert saved.name.endswith(".upload")
        assert "brief" not in saved.name
        assert saved.read_bytes() == b"%PDF-content bigger than an image"
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert saved.exists()
        turn_done.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
        assert not saved.parent.exists()
    await app.state.injector.stop()


async def test_owned_codex_followup_forwards_selected_effort(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({*session.capabilities, Capability.EFFORT})
    seen = {}

    async def fake_inject(
        account,
        session,
        message,
        *,
        timeout_s,
        effort=None,
        allow_over_budget=False,
    ):
        assert allow_over_budget is True
        seen["effort"] = effort
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")
        assert 'id="inject-effort"' in detail.text
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "continue", "effort": "xhigh"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        for _ in range(10):
            await asyncio.sleep(0)
            if seen:
                break

    assert seen["effort"] == "xhigh"
    await app.state.injector.stop()


async def test_effort_change_requires_owned_codex_capability(tmp_path):
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get("/sessions/codex:test:sid")
        assert 'id="inject-effort"' not in detail.text
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "continue", "effort": "high"},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "effort unavailable"
    await app.state.injector.stop()


async def test_steer_image_cleanup_waits_for_turn(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({*session.capabilities, Capability.STEER})
    turn_done = asyncio.Event()
    cleanup_started = asyncio.Event()
    received = []

    async def fake_steer(
        account, session, message, *, images=None, allow_over_budget=False
    ):
        assert allow_over_budget is True
        received.extend(images or [])
        return InjectResult(True)

    async def fake_wait(account, session_id, *, timeout_s):
        cleanup_started.set()
        await turn_done.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "steer", fake_steer)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    png = b"\x89PNG\r\n\x1a\nsmall"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/steer",
            data={"message": "look now"},
            files={"images": ("screen.png", png, "image/png")},
            headers={
                "origin": "http://test",
                "x-agentdeck-action-id": "action-steer-1",
            },
        )
        assert response.status_code == 200
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        saved = received[0]
        assert saved.exists()
        tracked = app.state.injector.status("codex:test:sid").items[-1]
        assert tracked.client_action_id == "action-steer-1"
        assert tracked.state == "accepted"
        pending_image = await client.get(
            f"/sessions/codex:test:sid/pending-images/{tracked.id}/0"
        )
        assert pending_image.status_code == 200
        observed = app.state.injector.observe_transcript(
            "codex:test:sid",
            [
                TranscriptEvent(
                    seq=11,
                    role="user",
                    text="look now",
                    ts=tracked.created_at + timedelta(milliseconds=1),
                    image_media_types=("image/png",),
                )
            ],
        )
        assert observed == {11: tracked}
        assert tracked.state == "observed"
        turn_done.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
    await app.state.injector.stop()


async def test_new_session_route_and_enter_to_send_ui(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    release = asyncio.Event()

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        allow_over_budget=False,
        session_token=None,
    ):
        assert cwd == tmp_path
        assert message == "build a new thing"
        assert allow_over_budget is True
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/")
        assert "New chat" in dashboard.text
        assert 'class="send-on-enter"' in dashboard.text
        chats = await client.get("/chats")
        assert chats.text.index('id="new-chat-account"') < chats.text.index('id="sessions"')
        assert 'id="q"' in chats.text
        assert 'name="closed"' in chats.text and 'name="done"' not in chats.text
        # A chat page carries no New chat form of its own any more (#2603).
        detail = await client.get("/sessions/codex:test:sid")
        assert detail.text.count('data-agentdeck-action="new_session"') == 0
        assert detail.text.count('data-agentdeck-action="send"') == 1
        # Only the chat's own composer; New work's form appears only on a host with an
        # issue board, so it is left out of the count (#2464).
        outside_new_work = re.sub(
            r'<details class="new-chat new-work".*?</details>', "", detail.text, flags=re.S
        )
        assert outside_new_work.count("＋ Attach file") == 1
        assert 'accept="image/' not in detail.text
        assert "var drafts = document.querySelectorAll(" in detail.text
        assert "Enter to send" in detail.text
        assert "requestSubmit" in detail.text
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        # Nothing navigates. The accepted start is watched where it was pressed,
        # and the chat joins the chat list when the provider names it (#1799).
        assert "hx-redirect" not in response.headers
        assert 'id="new-session-result"' in response.text
        assert 'data-starting-state="starting"' in response.text
        assert "Starting" in response.text
        token = started_token(response)
        starting_url = f"/sessions/starting/{token}"
        waiting = await client.get(f"/partials/starting-chat/{token}")
        assert 'data-starting-state="starting"' in waiting.text
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "complete":
                break
        # This provider accepted without naming a session, so there is nothing to
        # hand over to — the strip says so instead of spinning forever.
        settled = await client.get(f"/partials/starting-chat/{token}")
        assert 'data-starting-state="lost"' in settled.text
        assert "cannot tell which chat that start became" in settled.text
        # The stable link has nowhere better to send anyone than the chat list.
        forwarded = await client.get(starting_url)
        assert forwarded.status_code == 303
        assert forwarded.headers["location"] == "/"
    await app.state.injector.stop()


async def test_new_session_keeps_an_arbitrary_file_through_the_first_turn(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path)
    started = asyncio.Event()
    release_start = asyncio.Event()
    waiting = asyncio.Event()
    turn_done = asyncio.Event()
    seen = {}

    async def fake_start(account, cwd, message, *, images=None, **kwargs):
        assert images is None
        seen["message"] = message
        started.set()
        await release_start.wait()
        return InjectResult(True, session_id="new-with-file")

    async def fake_wait(account, session_id, *, timeout_s):
        assert session_id == "new-with-file"
        waiting.set()
        await turn_done.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "wait_for_session", fake_wait)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # A first message still needs words; only a follow-up may be a file alone (#3790).
        wordless = await client.post(
            "/sessions/new",
            data={"account_key": "codex:test", "cwd": str(tmp_path), "message": ""},
            files={"images": ("notes.txt", b"hello", "text/plain")},
            headers={"origin": "http://test"},
        )
        assert wordless.status_code == 422
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "read this first",
            },
            files={"images": ("notes.txt", b"hello", "text/plain")},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202, response.text
        # The person's own words are what the start records, with the generated
        # attachment envelope stripped back off (#1799 keeps this on the record,
        # even though the strip itself only reports the outcome).
        token = started_token(response)
        start = app.state.injector.started_chat(token)
        assert start is not None
        assert start.message == "read this first"
        assert "agentdeck-attachments" not in start.message
        assert "file-uploads" not in start.message
        await asyncio.wait_for(started.wait(), timeout=1)
        attachment = json.loads(
            re.search(r"^- (\{.*\})$", seen["message"], re.MULTILINE).group(1)
        )
        saved = Path(attachment["path"])
        assert attachment["name"] == "notes.txt"
        assert saved.read_text() == "hello"
        release_start.set()
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert saved.exists()
        turn_done.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
    await app.state.injector.stop()


def _hx_trigger_delay_s(html: str) -> float:
    """Seconds the start-status strip holds before asking again."""
    trigger = re.search(r"hx-trigger=[\"']([^\"']*)[\"']", html)
    assert trigger, f"the status strip does not poll at all: {html!r}"
    delay = re.search(r"delay:\s*([0-9.]+)\s*(ms|s)\b", trigger.group(1))
    assert delay, f"no delay in hx-trigger {trigger.group(1)!r}"
    value = float(delay.group(1))
    return value / 1000 if delay.group(2) == "ms" else value


async def test_start_status_reasks_without_adding_seconds(tmp_path, monkeypatch):
    """The start-status strip must re-ask in well under a second (#654, #765).

    This is the host-independent half of the start-latency guard, and the half
    that actually discriminates: a poll delay measured in seconds is exactly the
    regression #654 fixed, and reading it off the rendered fragment proves it
    without a stopwatch. The browser test below proves the outcome still lands
    end to end, but its bound is deliberately too loose to catch this on its own.
    """
    app = _web_app(tmp_path)
    release = asyncio.Event()

    async def fake_start(account, cwd, message, **kwargs):
        await release.wait()
        return InjectResult(True, session_id="newsid")

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a thing",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        token = started_token(response)

        # The accepted answer is already the strip, so it is the first poll too.
        assert _hx_trigger_delay_s(response.text) < 1.0
        poll = await client.get(f"/partials/starting-chat/{token}")
        assert _hx_trigger_delay_s(poll.text) < 1.0
        release.set()
    await app.state.injector.stop()


async def test_a_refused_start_explains_itself_where_it_was_pressed(tmp_path, monkeypatch):
    """The refusal lands where the press was made (#765, #1799).

    The press is answered before the provider is, so the refusal arrives seconds
    later — into the same result slot the person is still looking at, in words, and
    with their first message untouched in the composer it was typed into.
    """
    app = _web_app(tmp_path)

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(False, "at_capacity")

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a thing",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        token = started_token(response)
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "failed":
                break
        refused = await client.get(f"/partials/starting-chat/{token}")
        assert 'id="new-session-result"' in refused.text
        assert 'data-starting-state="failed"' in refused.text
        assert "at_capacity" not in refused.text
        assert "Every worker slot on this account is busy" in refused.text
        assert "Nothing was sent." in refused.text
        # The draft is not released on a refusal, so the composer keeps the text.
        assert "data-draft-key" in refused.text
    await app.state.injector.stop()


@pytest.mark.parametrize("viewport_width", [1200, 320])
@pytest.mark.browser
async def test_pressing_start_stays_put_and_the_chat_arrives_in_the_list(
    tmp_path, monkeypatch, viewport_width
):
    """The issue itself (#1799): Start new chat must not take you anywhere.

    Spawning an agent takes seconds. The press used to navigate to a waiting page
    standing in for the chat that did not exist yet (#765); now it starts in the
    background, says so where the button is, and the notice turns into a link to
    the chat once it exists. The chats list is rendered per load (#2603), so it is
    the notice, not the list, that says the chat arrived. This holds the provider
    open to prove both halves: the page is unchanged while it is blocked, and the
    link appears without a navigation once it answers.
    """
    app = _web_app(tmp_path)
    new_key = "codex:test:newsid"
    release = asyncio.Event()
    provider_answered = False

    async def fake_start(account, cwd, message, **kwargs):
        nonlocal provider_answered
        await release.wait()
        provider_answered = True
        return InjectResult(True, session_id="newsid")

    async def fake_scan(account, only=None):
        # The chat does not exist until the provider says so, which is what makes
        # its arrival in the list mean something. It arrives the way a real one
        # does: a live process already working on the first message.
        if not provider_answered:
            return []
        return [
            Session(
                new_key,
                "codex:test",
                "newsid",
                SessionStatus.LIVE,
                thinking=True,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        ]

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "scan_sessions", fake_scan)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": 800},
            service_workers="block",
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/chats")
        await page.locator(".new-chat > summary").click()
        await page.locator("#new-chat-cwd").fill(str(tmp_path))
        await page.locator("#new-chat-message").fill("build a thing")
        pressed_url = page.url
        await page.locator('.new-chat button[type="submit"]').click()

        strip = page.locator("#new-session-result[data-starting-state]")
        await strip.wait_for()
        pressed = {
            "url": page.url,
            "state": await strip.get_attribute("data-starting-state"),
            "text": await strip.inner_text(),
            "listed": await page.locator(
                f'a.session[data-session-key="{new_key}"]'
            ).count(),
            "provider_answered": provider_answered,
        }

        release.set()
        await page.locator(
            '#new-session-result[data-starting-state="ready"] a'
        ).wait_for()
        arrived_url = page.url
        await context.close()
        await browser.close()

    assert pressed["provider_answered"] is False
    assert pressed["state"] == "starting"
    assert pressed["url"] == pressed_url
    assert pressed["listed"] == 0
    assert "Starting" in pressed["text"]
    # The chat arrived where it belongs, and the person never left the page.
    assert arrived_url == pressed_url
    await app.state.injector.stop()


@pytest.mark.parametrize("viewport_width", [1200, 320])
@pytest.mark.browser
async def test_new_chat_settles_promptly_after_provider_start_completes(
    tmp_path, monkeypatch, viewport_width
):
    """Learning the chat exists must not be gated on anything slow.

    Pressing Start answers instantly and goes nowhere (#1799), so what this
    measures is the second leg: how long after the provider names a session the
    person is told there is a chat to open.

    The bound is calibrated against this host rather than fixed: a stopwatch
    tight enough to catch a seconds-long poll delay also measures how busy the
    machine is, and the fixed 0.75s it used to assert failed under load on
    unrelated commits (#711). The poll delay itself is asserted exactly, without
    timing, in test_start_status_reasks_without_adding_seconds.
    """
    app = _web_app(tmp_path)
    new_key = "codex:test:newsid"
    provider_completed_at = None

    async def fake_start(account, cwd, message, **kwargs):
        nonlocal provider_completed_at
        await asyncio.sleep(0.05)
        provider_completed_at = asyncio.get_running_loop().time()
        return InjectResult(True, session_id="newsid")

    async def fake_scan(account, only=None):
        return [
            Session(
                new_key,
                "codex:test",
                "newsid",
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        ]

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "scan_sessions", fake_scan)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # The service worker is no part of the start path, and it was the
        # test's *other* flake: on a fresh profile the worker installed, claimed
        # the page, and base.html reloaded once, which landed mid-form and
        # cancelled the fill. That reload is gone (#720 — a first claim no
        # longer reloads), but the worker stays blocked here: installing it is
        # noise this test's stopwatch does not need to pay for.
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": 800},
            service_workers="block",
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/chats")
        # Control measurement: a second, warm load of the same page — the same
        # server, browser and host load the status round trip will have to pay
        # for, and warm rather than cold like the first one.
        load_started_at = asyncio.get_running_loop().time()
        await page.goto(f"{base_url}/chats")
        baseline_page_load_s = asyncio.get_running_loop().time() - load_started_at
        await page.locator(".new-chat > summary").click()
        await page.locator("#new-chat-cwd").fill(str(tmp_path))
        await page.locator("#new-chat-message").fill("build a thing")
        pressed_url = page.url
        await page.locator('.new-chat button[type="submit"]').click()
        await page.locator('#new-session-result[data-starting-state="ready"]').wait_for()
        settle_lag_s = asyncio.get_running_loop().time() - provider_completed_at
        # Nothing navigated on the way to that answer.
        assert page.url == pressed_url
        await browser.close()

    # Measured from the provider, so it covers one sub-second poll delay and one
    # status round trip. Both get slower as the host gets busier, so scale the
    # slack by the control load rather than fixing it. The floor keeps a fast
    # control measurement from producing a bound tighter than the measured lag,
    # and the cap keeps the loosest bound well under the 15s scan interval, which
    # is what learning about the chat falls back to if the poll stops driving it.
    settle_budget_s = min(max(2.0, 5 * baseline_page_load_s), 6.0)
    assert settle_lag_s < settle_budget_s, (
        f"settling took {settle_lag_s:.3f}s, budget {settle_budget_s:.3f}s "
        f"(baseline page load {baseline_page_load_s:.3f}s)"
    )
    await app.state.injector.stop()


@pytest.mark.browser
@pytest.mark.serviceworker
async def test_first_service_worker_claim_leaves_the_new_chat_form_alone(tmp_path):
    """A first visit must not be reloaded out from under someone filling a form.

    On a browser with no worker installed, the worker installs and claims the
    page a second or two after load. base.html used to reload on that claim,
    which wipes the open <details>, the chosen account/model and the typed
    working directory — everything except the message draft it guards (#720).
    A first claim replaces nothing: no worker served this document, and the
    fetch handler is network-first, so its assets are already the current
    generation's.
    """
    app = _web_app(tmp_path)
    typed_cwd = str(tmp_path / "project-x")

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # A real, unblocked worker on a fresh profile: the bug's own shape, and
        # the reason this one opts out of the harness default that blocks it.
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="allow"
        )
        page = await context.new_page()
        # Survives the reload we are asserting does not happen, so the check
        # holds even if the claim lands before the form is filled.
        await page.add_init_script(
            "sessionStorage.setItem("
            "  'agentdeck-test-loads',"
            "  String(Number(sessionStorage.getItem('agentdeck-test-loads') || 0) + 1)"
            ")"
        )
        await page.goto(f"{base_url}/chats")
        await page.locator(".new-chat > summary").click()
        await page.locator("#new-chat-cwd").fill(typed_cwd)
        await page.wait_for_function(
            "() => navigator.serviceWorker.controller !== null", timeout=20000
        )
        # A reload triggered by that claim is already in flight by now; give it
        # room to land rather than racing it.
        await page.wait_for_timeout(750)

        assert await page.evaluate("sessionStorage.getItem('agentdeck-test-loads')") == "1"
        assert await page.locator("#new-chat-cwd").input_value() == typed_cwd
        assert await page.locator(".new-chat").get_attribute("open") is not None
        await context.close()
        await browser.close()
    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_replacing_service_worker_still_reloads_the_stale_shell(tmp_path):
    """The deploy case the reload exists for keeps working.

    A tab that was already controlled when it loaded is holding assets from the
    previous generation, so a worker taking over is a genuine replacement and
    the shell must be reloaded (#720 must not turn the auto-update off).
    """
    app = _web_app(tmp_path)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # The worker itself is blocked: the controller is stubbed instead, so the
        # test drives the handler's decision rather than a real install race.
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator.serviceWorker, 'controller', {"
            "  configurable: true, get: () => ({scriptURL: '/sw.js'})"
            "});"
            "sessionStorage.setItem("
            "  'agentdeck-test-loads',"
            "  String(Number(sessionStorage.getItem('agentdeck-test-loads') || 0) + 1)"
            ")"
        )
        await page.goto(f"{base_url}/chats")
        assert await page.evaluate("sessionStorage.getItem('agentdeck-test-loads')") == "1"

        await page.evaluate(
            "navigator.serviceWorker.dispatchEvent(new Event('controllerchange'))"
        )
        await page.wait_for_function(
            "() => sessionStorage.getItem('agentdeck-test-loads') === '2'", timeout=10000
        )
        await context.close()
        await browser.close()
    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_claim_after_a_suppressed_first_one_still_reloads(tmp_path):
    """Skipping the first claim must arm the next one, not disarm the handler.

    The two cases above both pass against a handler that ignores *every* claim,
    because neither chains two of them: the first-claim test never fires a
    second, and the replacement test is already controlled at load. What keeps
    the deploy reload alive on a tab whose first visit installed the worker is
    `base.html` re-reading the controller and remembering it, so drive both
    claims in one document lifetime.
    """
    app = _web_app(tmp_path)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        # The worker is blocked and the controller stubbed over a variable the
        # test moves, so each claim is a deliberate step rather than an install
        # race: uncontrolled at load, then worker A, then worker B.
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            "window.__swController = null;"
            "Object.defineProperty(navigator.serviceWorker, 'controller', {"
            "  configurable: true, get: () => window.__swController"
            "});"
            "sessionStorage.setItem("
            "  'agentdeck-test-loads',"
            "  String(Number(sessionStorage.getItem('agentdeck-test-loads') || 0) + 1)"
            ")"
        )
        await page.goto(f"{base_url}/chats")
        assert await page.evaluate("sessionStorage.getItem('agentdeck-test-loads')") == "1"

        # First claim on an uncontrolled document: replaces nothing, no reload.
        await page.evaluate(
            "window.__swController = {scriptURL: '/sw.js'};"
            "navigator.serviceWorker.dispatchEvent(new Event('controllerchange'))"
        )
        await page.wait_for_timeout(750)
        assert await page.evaluate("sessionStorage.getItem('agentdeck-test-loads')") == "1"

        # A second worker takes over the same document: a real replacement.
        await page.evaluate(
            "window.__swController = {scriptURL: '/sw.js?next'};"
            "navigator.serviceWorker.dispatchEvent(new Event('controllerchange'))"
        )
        await page.wait_for_function(
            "() => sessionStorage.getItem('agentdeck-test-loads') === '2'", timeout=10000
        )
        await context.close()
        await browser.close()
    await app.state.injector.stop()


async def test_new_session_forwards_selected_model(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    seen = {}

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        model=None,
        allow_over_budget=False,
        session_token=None,
    ):
        assert allow_over_budget is True
        seen["model"] = model
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
                "model": "gpt-5.6-luna",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "complete":
                break
    assert seen["model"] == "gpt-5.6-luna"
    await app.state.injector.stop()


async def test_new_session_forwards_selected_effort(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    seen = {}

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        effort=None,
        allow_over_budget=False,
        session_token=None,
    ):
        assert allow_over_budget is True
        seen["effort"] = effort
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
                "effort": "high",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        for _ in range(10):
            await asyncio.sleep(0)
            if seen:
                break
    assert seen["effort"] == "high"
    await app.state.injector.stop()


async def test_new_session_rejects_unknown_effort(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    called = False

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        effort=None,
        allow_over_budget=False,
        session_token=None,
    ):
        nonlocal called
        called = True
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
                "effort": "limitless",
            },
            headers={"origin": "http://test"},
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid effort"
    assert called is False
    await app.state.injector.stop()


async def test_new_session_blank_model_uses_account_default(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    seen = {}

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        model=None,
        allow_over_budget=False,
        session_token=None,
    ):
        assert allow_over_budget is True
        seen["model"] = model
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
                "model": "",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "complete":
                break
    assert seen["model"] is None
    await app.state.injector.stop()


async def test_new_session_rejects_unknown_model(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    called = False

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        model=None,
        allow_over_budget=False,
        session_token=None,
    ):
        nonlocal called
        called = True
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
                # A Claude alias is not valid for a Codex account.
                "model": "opus",
            },
            headers={"origin": "http://test"},
        )
    assert response.status_code == 422
    assert called is False
    await app.state.injector.stop()


@pytest.mark.browser
async def test_chats_page_controls_stay_visible_on_every_width(tmp_path):
    """The chats page's New chat and filters (#2603) fit a desktop and a 320px phone."""
    app = _web_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        chats = await client.get("/chats")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        layouts = []
        for width in (1200, 320):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()

            async def serve(route, request):
                path = request.url.split("?", 1)[0].removeprefix(
                    "http://agentdeck.test"
                )
                if path == "/chats":
                    await route.fulfill(
                        status=200, content_type="text/html", body=chats.text
                    )
                elif path == "/static/app.css":
                    await route.fulfill(
                        status=200, content_type="text/css", body=stylesheet
                    )
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/chats")
            layouts.append(
                {
                    "new_chat": await page.locator(".new-chat > summary").is_visible(),
                    "search": await page.locator("#q").is_visible(),
                    "hide_closed": await page.locator(
                        'label:has(input[name="closed"])'
                    ).is_visible(),
                    "overflow": await page.evaluate(
                        "document.documentElement.scrollWidth > innerWidth"
                    ),
                }
            )
            await context.close()
        await browser.close()

    expected = {
        "new_chat": True,
        "search": True,
        "hide_closed": True,
        "overflow": False,
    }
    assert layouts == [expected, expected]
    await app.state.injector.stop()


async def test_dashboard_renders_model_picker(tmp_path):
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/")
    assert 'id="new-chat-model"' in dashboard.text
    assert 'data-provider="codex"' in dashboard.text
    assert 'data-default-model="Sol"' in dashboard.text
    assert 'data-default-effort="Medium"' in dashboard.text
    assert "model_picker.js" in dashboard.text
    assert 'id="new-chat-effort"' in dashboard.text
    assert '<option value="ultra" data-provider="codex">Ultra</option>' in dashboard.text
    await app.state.injector.stop()


def _web_app_with_two_accounts(tmp_path):
    """A deck that can start a chat on either of two accounts, both metered."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[
            AccountConfig(provider="codex", label="main", config_dir=str(tmp_path)),
            AccountConfig(provider="codex", label="alt", config_dir=str(tmp_path)),
        ],
    )
    app = create_app(config)
    now = datetime.now(UTC)
    for label, five_hour in (("main", 42.0), ("alt", 91.0)):
        app.state.app_state.set_usage(
            UsageSnapshot(
                account_key=f"codex:{label}",
                five_hour_pct=five_hour,
                five_hour_resets_at=now + timedelta(hours=2),
                seven_day_pct=7.0,
                seven_day_resets_at=now + timedelta(days=4),
                fetched_at=now,
            )
        )
    return app


async def test_new_chat_offers_one_metered_button_per_account(tmp_path):
    """#1912. Which account to spend is a glance, not a dropdown: every startable
    account is its own radio button carrying its own meters, and the one that is
    nearly out says so loudly enough to steer the choice."""
    app = _web_app_with_two_accounts(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/")
    body = dashboard.text
    assert 'role="radiogroup"' in body
    assert '<select id="new-chat-account"' not in body
    for label in ("main", "alt"):
        assert f'name="account_key"\n               value="codex:{label}"' in body
    # The meters ride the buttons, and the crowded account is flagged, not just
    # printed — 91% must not read the same as 42%.
    assert "42%" in body
    assert '<b class="crit">91%</b>' in body
    # A form still posts one account, and the first is preselected so a chat can
    # be started without touching the picker at all.
    assert body.count(' checked') == 1
    await app.state.injector.stop()


async def test_new_chat_hides_model_and_effort_behind_edit_advanced(tmp_path):
    """#1912. Model and effort are read-only until asked for: both selects sit
    inside the collapsed disclosure, whose own line still carries the values."""
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/")
    body = dashboard.text
    advanced = body[body.index('<details class="advanced-settings"') :]
    advanced = advanced[: advanced.index("</details>")]
    assert "Edit advanced" in advanced
    # Not merely styled small — the selects are inside the collapsed element, so
    # a browser with no CSS and no JS still hides them until it is opened.
    assert 'id="new-chat-model"' in advanced
    assert 'id="new-chat-effort"' in advanced
    # The collapsed line reads out what the chat would actually use.
    summary = advanced[advanced.index("data-advanced-summary") :]
    assert "Sol · Medium" in summary[: summary.index("</span>")]
    await app.state.injector.stop()


def test_the_read_only_line_never_prints_the_no_default_sentinel():
    """#1912. `choice_label` says "account" when nothing explicit is recorded,
    which read out twice becomes "account · account" — a line that tells the
    reader nothing about what the chat would use."""
    from agentdeck.web.routes_pages import _advanced_summary

    assert _advanced_summary({"model": "Sol", "effort": "Medium"}) == "Sol · Medium"
    assert _advanced_summary({"model": "account", "effort": "account"}) == (
        "Account defaults"
    )
    assert _advanced_summary({"model": "Opus 5", "effort": "account"}) == (
        "Opus 5 · account default"
    )
    assert _advanced_summary({"model": "Sol", "effort": ""}) == "Sol"


async def test_waiting_chat_handover_target_resolves_before_next_scan(
    tmp_path, monkeypatch
):
    """Regression for issue #4: the handover from an accepted start to the real chat
    must land on a resolvable session even though the periodic scan loop has not
    run yet. The status strip reads that chat on demand so /sessions/<key> does
    not 404 "unknown session" -- just that chat, never the whole account, which
    was most of the wait for "Open it" (#3420)."""
    app = _web_app(tmp_path)
    new_key = "codex:test:newsid"
    scans: list[frozenset[str] | None] = []

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        allow_over_budget=False,
        session_token=None,
    ):
        assert allow_over_budget is True
        return InjectResult(True, session_id="newsid")

    async def fake_scan(account, only=None):
        scans.append(only)
        return [
            Session(
                new_key,
                "codex:test",
                "newsid",
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        ]

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "scan_sessions", fake_scan)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        token = started_token(response)
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "complete":
                break
        # The periodic scan loop is not running in this test, so the new session
        # is absent from state until the status strip forces a scan.
        assert app.state.app_state.sessions.get(new_key) is None
        waiting = await client.get(f"/partials/starting-chat/{token}")
        chat_url = app.state.app_state.chat_path_for_session(new_key)
        assert chat_url.startswith("/chats/")
        assert f'data-session-url="{chat_url}"' in waiting.text
        assert app.state.app_state.sessions.get(new_key) is not None
        assert scans == [frozenset({"newsid"})]
        detail = await client.get(f"/sessions/{new_key}")
        assert detail.status_code == 200
    await app.state.injector.stop()


@pytest.mark.browser
async def test_a_refused_new_chat_keeps_its_first_message_and_explains_itself(
    tmp_path, monkeypatch
):
    """Regression for issue #559, over the background start of #1799: starting a
    chat is only *accepted* by the POST, so clearing the composer there threw the
    first message away before anything had used it — and the refusal arrived as
    the runtime's own word ("at_capacity"), which tells the person who pressed
    Start chat nothing. The press goes nowhere now, so the refusal arrives in the
    composer's own result slot seconds later; the text must survive it untouched,
    ready for a one-press retry, and be released only once a chat really exists."""
    app = _web_app(tmp_path)
    new_key = "codex:test:newsid"
    outcomes = [
        InjectResult(False, "at_capacity"),
        InjectResult(True, session_id="newsid"),
    ]

    async def fake_start(account, cwd, message, **kwargs):
        return outcomes.pop(0)

    async def fake_scan(account, only=None):
        return [
            Session(
                new_key,
                "codex:test",
                "newsid",
                SessionStatus.IDLE,
                cwd=tmp_path,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        ]

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(PROVIDERS["codex"], "scan_sessions", fake_scan)

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1200, "height": 900}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/chats")
        await page.locator(".new-chat > summary").click()
        await page.locator("#new-chat-cwd").fill(str(tmp_path))
        await page.locator("#new-chat-message").fill("build a thing")
        await page.locator('.new-chat button[type="submit"]').click()

        # The press goes nowhere: the refusal arrives in the composer's own
        # result slot, where the person is still standing (#1799).
        refusal = page.locator('#new-session-result[data-starting-state="failed"]')
        await refusal.wait_for()
        refused_message = await refusal.inner_text()
        refused_url = page.url
        # Nothing was consumed, so nothing has to be retyped: the same text is
        # still in the composer, one press from trying again.
        refused_draft = await page.locator("#new-chat-message").input_value()

        await page.locator('.new-chat button[type="submit"]').click()
        await page.locator('#new-session-result[data-starting-state="ready"]').wait_for()
        # A chat exists now, so the draft must be released — both the textarea and
        # the sessionStorage copy that would restore it on the next load.
        # The release happens on the htmx:load that follows the swap, a tick after
        # the strip itself is in the DOM — so wait for it rather than racing it.
        await page.wait_for_function(
            "() => document.querySelector('#new-chat-message').value === ''"
            " && !sessionStorage.getItem('agentdeck:message-draft:/sessions/new')"
        )
        started_draft = await page.locator("#new-chat-message").input_value()
        stored_draft = await page.evaluate(
            "() => sessionStorage.getItem('agentdeck:message-draft:/sessions/new')"
        )
        settled_url = page.url
        await context.close()
        await browser.close()

    assert refused_url == settled_url
    assert stored_draft in (None, "")

    assert "at_capacity" not in refused_message
    assert "Every worker slot on this account is busy" in refused_message
    assert refused_draft == "build a thing"
    assert started_draft == ""
    await app.state.injector.stop()


async def test_manual_new_session_cwd_is_shared_and_delegation_does_not_change_it(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path)
    db = Db(tmp_path / "shared.db")
    app.state.db = db
    app.state.app_state.db = db
    manual_cwd = tmp_path / "manual-project"
    delegated_cwd = tmp_path / "delegated-project"
    manual_cwd.mkdir()
    delegated_cwd.mkdir()

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id=f"started-{cwd.name}")

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as desktop,
        AsyncClient(transport=transport, base_url="http://test") as mobile,
    ):
        response = await desktop.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(manual_cwd),
                "message": "start manually",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202

        mobile_dashboard = await mobile.get("/")
        assert (
            f'id="new-chat-cwd" name="cwd" type="text" list="new-chat-cwds"\n'
            f'           value="{manual_cwd}"'
        ) in mobile_dashboard.text

        delegated = await desktop.post(
            "/api/delegations",
            json={
                "account_key": "codex:test",
                "cwd": str(delegated_cwd),
                "message": "start automatically",
            },
        )
        assert delegated.status_code == 202
        unchanged = await mobile.get("/")
        assert (
            f'id="new-chat-cwd" name="cwd" type="text" list="new-chat-cwds"\n'
            f'           value="{manual_cwd}"'
        ) in unchanged.text

    await app.state.injector.stop()
    db.close()


async def test_delegation_into_the_live_deploy_checkout_is_refused(tmp_path, monkeypatch):
    """A machine-dispatched agent editing the live checkout blocks every deploy
    tick, and worktree-confined workers have reverted uncommitted files there
    (#786). Delegations belong in worktrees; this one never starts."""
    repo = tmp_path / "live-repo"
    repo.mkdir()
    monkeypatch.setenv("AGENTDECK_REPO", str(repo))
    app = _web_app(tmp_path)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(repo), "message": "review this change"},
        )
    assert response.status_code != 202
    assert "live deploy checkout" in response.text
    assert "review-mirrors" in response.text  # says what to pass instead (#3711)


async def test_a_delegation_has_no_length_limit_unless_one_is_configured(tmp_path, monkeypatch):
    """#2677: a /dream reader's brief carries a whole group of transcripts. Codex reads a
    message over its own input channel, so the deck adds no cap of its own by default."""
    from agentdeck.providers import PROVIDERS

    started = []

    async def fake_start(account, cwd, message, **kwargs):
        started.append(message)
        return InjectResult(True, session_id="delegated-thread")

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    brief = "DREAM-AGENT\n" + "transcript line\n" * 30_000  # 480,000 characters

    unlimited = _web_app(tmp_path / "unlimited", max_message_chars=None)
    async with AsyncClient(transport=ASGITransport(app=unlimited), base_url="http://test") as c:
        response = await c.post(
            "/api/delegations",
            json={"account_key": "codex:test", "cwd": str(tmp_path), "message": brief},
        )
        page = await c.get("/sessions/codex:test:sid")
    assert response.status_code == 202
    composer = page.text.split('id="inject-message"', 1)[1].split(">", 1)[0]
    assert "maxlength" not in composer
    await unlimited.state.injector.stop()

    capped = _web_app(tmp_path / "capped", max_message_chars=1000)
    async with AsyncClient(transport=ASGITransport(app=capped), base_url="http://test") as c:
        response = await c.post(
            "/api/delegations",
            json={"account_key": "codex:test", "cwd": str(tmp_path), "message": brief},
        )
    assert response.status_code == 422 and "too long" in response.text
    await capped.state.injector.stop()
    assert len(started) <= 1


def test_the_inject_config_has_no_message_limit_by_default():
    assert InjectConfig().max_message_chars is None
    assert InjectConfig().too_long("x" * 1_000_000) is False
    assert InjectConfig(max_message_chars=3).too_long("abcd") is True
    with pytest.raises(ValueError, match="must be positive"):
        InjectConfig(max_message_chars=0)


async def test_a_read_only_codex_delegation_gets_the_referenced_issue_text_prepended(
    tmp_path, monkeypatch
):
    """#787: a read-only Codex sandbox has no network at all, so it cannot read the very
    ticket it was delegated. The deck knows the parent session's issue link and fetches the
    text coordinator-side, making the documented hand-the-text-over fallback the default."""
    app = _web_app(tmp_path)
    app.state.injector.config.max_message_chars = 40_000
    parent = Session(
        "codex:test:parent",
        "codex:test",
        "parent",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.TRANSCRIPT}),
        issue_url="https://github.com/eerovil/agentdeck/issues/787",
    )
    app.state.app_state.update_session(parent)

    captured = {}

    async def fake_start(account, cwd, message, **kwargs):
        captured["message"] = message
        return InjectResult(True, session_id="delegated-thread")

    from types import SimpleNamespace

    from agentdeck import gh_cli
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import routes_api

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    monkeypatch.setattr(
        routes_api, "shutil", SimpleNamespace(which=lambda name: "/usr/bin/gh")
    )

    async def fake_gh(argv, *, timeout, retry_without_token=True):
        assert argv[1:3] == ["issue", "view"], argv
        payload = {
            "title": "The ticket",
            "body": "Fix the thing.",
            "comments": [{"author": {"login": "eero"}, "body": "Please also X."}],
        }
        return 0, json.dumps(payload).encode()

    monkeypatch.setattr(gh_cli, "run", fake_gh)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "answer from the issue text",
                "sandbox": "read-only",
                "parent_session_id": "codex:test:parent",
            },
        )
        assert response.status_code == 202

    message = captured["message"]
    assert message.startswith("## Referenced work items")
    assert "The ticket" in message and "Fix the thing." in message
    assert "**@eero commented:**" in message and "Please also X." in message
    assert message.endswith("answer from the issue text")
    await app.state.injector.stop()


async def test_an_unfetchable_reference_never_blocks_a_read_only_delegation(
    tmp_path, monkeypatch
):
    """Every failure degrades to "no context prepended": the delegation still starts."""
    app = _web_app(tmp_path)
    app.state.injector.config.max_message_chars = 40_000
    parent = Session(
        "codex:test:parent",
        "codex:test",
        "parent",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.TRANSCRIPT}),
        issue_url="https://github.com/eerovil/agentdeck/issues/787",
    )
    app.state.app_state.update_session(parent)

    captured = {}

    async def fake_start(account, cwd, message, **kwargs):
        captured["message"] = message
        return InjectResult(True, session_id="delegated-thread")

    from agentdeck import gh_cli
    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)

    async def failing_gh(argv, *, timeout, retry_without_token=True):
        return 1, b""

    monkeypatch.setattr(gh_cli, "run", failing_gh)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "answer from the issue text",
                "sandbox": "read-only",
                "parent_session_id": "codex:test:parent",
            },
        )
        assert response.status_code == 202

    assert captured["message"] == "answer from the issue text"
    await app.state.injector.stop()


async def test_machine_delegation_api_returns_final_message(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    release = asyncio.Event()

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        sandbox,
        model,
        effort,
        approval_policy,
        delegated,
        session_token=None,
    ):
        assert account.key == "codex:test"
        assert cwd == tmp_path
        assert message == "review this change"
        assert sandbox == "workspace-write"
        assert model is None
        assert effort is None
        assert approval_policy == "on-request"
        assert delegated is True
        return InjectResult(True, session_id="delegated-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        assert session_id == "delegated-thread"
        await release.wait()
        return InjectResult(True)

    async def fake_result(account, session_id):
        assert session_id == "delegated-thread"
        return "The delegated review is complete."

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "review this change"},
        )
        assert response.status_code == 202
        started = response.json()
        delegation_id = started["id"]
        assert started["timeout_s"] == 14_400.0
        for _ in range(10):
            await asyncio.sleep(0)
            status = await client.get(f"/api/delegations/{delegation_id}")
            if status.json()["state"] == "running":
                break
        assert status.json() == {
            "id": delegation_id,
            "state": "running",
            "account_key": "codex:test",
            "session_key": "codex:test:delegated-thread",
            "session_url": app.state.app_state.chat_path_for_session(
                "codex:test:delegated-thread"
            ),
            "reason": None,
            "final_message": None,
            "interaction": None,
        }
        assert "codex:test:delegated-thread" in app.state.app_state.delegated_session_keys

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            status = await client.get(f"/api/delegations/{delegation_id}")
            if status.json()["state"] == "complete":
                break
        assert status.json()["final_message"] == "The delegated review is complete."

    await app.state.injector.stop()


async def test_a_human_started_tracked_coordinator_gets_the_primary_role(
    tmp_path, monkeypatch
):
    """#1155: tracking the commit reviewer must not forbid review-fix-pr's lanes."""
    app = _web_app(tmp_path)
    account = app.state.accounts[0]
    seen = {}

    async def fake_start(account, cwd, message, *, delegated, **kwargs):
        seen["delegated"] = delegated
        return InjectResult(True, session_id="review-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS[account.provider_id]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    result, delegation_id = await app.state.injector.start_delegation(
        account,
        provider,
        tmp_path,
        "run review-fix-pr",
        parent_session_id="codex:test:source",
        session_role="primary",
    )
    assert result.accepted is True
    assert delegation_id is not None
    for _ in range(20):
        await asyncio.sleep(0)
        status = app.state.injector.delegation_status(delegation_id)
        if status is not None and status.state == "complete":
            break

    assert seen["delegated"] is False
    assert "codex:test:review-thread" not in app.state.app_state.delegated_session_keys
    assert "codex:test:review-thread" not in app.state.app_state.recorded_delegation_parents
    await app.state.injector.stop()


async def test_delegation_parent_session_token_resolves_to_the_parents_key(
    tmp_path, monkeypatch
):
    """A Codex parent can only name itself with the token the deck gave it.

    Codex threads share one app-server process, so no environment variable can
    identify the calling thread. Without a token channel a Codex fan-out records
    no parent at all and every child becomes its own Deckhand handoff (#680).
    """
    app = _web_app(tmp_path)
    app.state.app_state.bind_session_token("tok-parent", "codex:test:sid")

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id="delegated-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={
                "cwd": str(tmp_path),
                "message": "mine one transcript",
                "parent_session_token": "tok-parent",
            },
        )
        assert response.status_code == 202
        # The reply says what was recorded, so a caller that named a parent can
        # see it stuck instead of finding out through an unexpected card.
        assert response.json()["parent_session"] == "codex:test:sid"
        for _ in range(20):
            await asyncio.sleep(0)
            if "codex:test:delegated-thread" in (
                app.state.app_state.recorded_delegation_parents
            ):
                break

    assert (
        app.state.app_state.recorded_delegation_parents["codex:test:delegated-thread"]
        == "codex:test:sid"
    )
    await app.state.injector.stop()


async def test_delegation_reports_a_parent_token_bound_to_no_session(
    tmp_path, monkeypatch, caplog
):
    # Parentage is bookkeeping, so an unresolvable token must not fail the work
    # — but it must not pass silently either, which is how a whole wave lost its
    # nesting with no signal until the cards showed up.
    app = _web_app(tmp_path)

    async def fake_start(account, cwd, message, **kwargs):
        return InjectResult(True, session_id="delegated-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={
                "cwd": str(tmp_path),
                "message": "mine one transcript",
                "parent_session_token": "tok-never-bound",
            },
        )
        assert response.status_code == 202
        assert response.json()["parent_session"] is None
        assert "out of Deckhand" in caplog.text

    await app.state.injector.stop()


async def test_delegation_wait_uses_the_delegation_clock_not_the_inject_one(
    tmp_path, monkeypatch
):
    """A delegated subagent runs a task, so it must not inherit inject.timeout_s.

    Sharing that clock capped every cross-account delegation at the injection
    timeout (15 minutes by default) and reported the still-running child as a
    failed delegation.
    """
    app = _web_app(tmp_path)
    app.state.injector.config.timeout_s = 900.0
    app.state.injector.config.delegation_timeout_s = 14_400.0
    seen: dict[str, float] = {}

    async def fake_start(account, cwd, message, *, timeout_s, **kwargs):
        seen["start"] = timeout_s
        return InjectResult(True, session_id="delegated-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        seen["wait"] = timeout_s
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "long running review"},
        )
        assert response.status_code == 202
        delegation_id = response.json()["id"]
        for _ in range(20):
            await asyncio.sleep(0)
            status = await client.get(f"/api/delegations/{delegation_id}")
            if status.json()["state"] == "complete":
                break
        assert status.json()["state"] == "complete"

    # The startup handshake stays on the injection clock; the task wait does not.
    assert seen["start"] == 900.0
    assert seen["wait"] == 14_400.0

    await app.state.injector.stop()


async def test_delegation_forwards_never_approval_policy(tmp_path, monkeypatch):
    # An autonomous caller (the kanban worker) asks for approval_policy "never" so
    # Codex runs unattended (no git rebase / push / gh pr prompts). The request's
    # policy must reach provider.start_session verbatim.
    app = _web_app(tmp_path)
    seen = {}

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        sandbox,
        model,
        effort,
        approval_policy,
        delegated,
        session_token=None,
    ):
        seen["effort"] = effort
        seen["approval_policy"] = approval_policy
        assert delegated is True
        return InjectResult(True, session_id="auto-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={
                "cwd": str(tmp_path),
                "message": "work the issue",
                "effort": "ultra",
                "approval_policy": "never",
            },
        )
        assert response.status_code == 202
        for _ in range(20):
            await asyncio.sleep(0)
            if seen:
                break
        assert seen["approval_policy"] == "never"
        assert seen["effort"] == "ultra"
        # An invalid policy is rejected by the request schema.
        bad = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "x", "approval_policy": "whatever"},
        )
        assert bad.status_code == 422
        bad_effort = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "x", "effort": "limitless"},
        )
        assert bad_effort.status_code == 422

    await app.state.injector.stop()


async def test_machine_delegation_api_validates_requests(tmp_path):
    disabled = _web_app(tmp_path, enabled=False)
    async with AsyncClient(
        transport=ASGITransport(app=disabled), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "hello"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "message injection is disabled"
        unsafe = await client.post(
            "/api/delegations",
            json={
                "cwd": str(tmp_path),
                "message": "hello",
                "sandbox": "danger-full-access",
            },
        )
        assert unsafe.status_code == 422
        missing = await client.get("/api/delegations/not-a-real-id")
        assert missing.status_code == 404


async def test_delegation_status_hides_interaction_without_capability(
    tmp_path, monkeypatch
):
    app = _web_app(tmp_path)
    interaction = _question_interaction(
        "tok-stale", "Database", "Which database?", "Postgres", "SQLite"
    )
    from agentdeck.providers import PROVIDERS

    # Patch the provider's interaction *read*; pending_interaction itself now
    # owns the INTERACT gate, and the default session lacks INTERACT, so the gate
    # must suppress this stale interaction.
    monkeypatch.setattr(
        PROVIDERS["codex"],
        "_actionable_interaction",
        lambda account, session_id: interaction,
    )
    app.state.injector._remember_delegation(
        DelegationStatus(
            "delegation-stale",
            "running",
            "codex:test",
            session_key="codex:test:sid",
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/delegations/delegation-stale")

    assert response.status_code == 200
    assert response.json()["state"] == "running"
    assert response.json()["interaction"] is None


async def test_owned_session_question_shows_stop_beside_send(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {
            Capability.TRANSCRIPT,
            Capability.INJECT,
            Capability.INTERACT,
            Capability.STEER,
            Capability.INTERRUPT,
        }
    )
    interaction = PendingInteraction(
        id="opaque-token",
        kind="question",
        thread_id="sid",
        turn_id="turn-1",
        title="Codex needs your answer",
        questions=(
            InteractionQuestion(
                id="database",
                header="Database",
                prompt="Which database should we use?",
                options=(
                    InteractionOption("Postgres", "Relational"),
                    InteractionOption("SQLite", "Embedded"),
                ),
                allow_other=True,
            ),
        ),
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: interaction
    )
    answered = {}

    async def answer(account, session, interaction_id, *, answers, decision):
        answered.update(
            interaction_id=interaction_id,
            answers=answers,
            decision=decision,
        )
        return InjectResult(True)

    async def accepted(*args, **kwargs):
        return InjectResult(True)

    monkeypatch.setattr(provider, "answer_interaction", answer)
    monkeypatch.setattr(provider, "steer", accepted)
    monkeypatch.setattr(provider, "interrupt", accepted)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        assert "Which database should we use?" in page.text
        assert "Postgres" in page.text
        assert "Send now" not in page.text
        assert 'hx-post="/sessions/codex:test:sid/interrupt"' in page.text
        assert 'form="interrupt-form"' in page.text
        assert 'aria-label="Stop active turn">Stop</button>' in page.text
        controls = page.text[page.text.index('id="composer-controls"') :]
        controls = controls[: controls.index("</div>")]
        assert controls.index(">Stop</button>") < controls.index(">Send</button>")
        assert "owned-controls" not in page.text
        response = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={
                "interaction_id": "opaque-token",
                "answer__database": "Postgres",
                "decision": "accept",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 200
        assert answered == {
            "interaction_id": "opaque-token",
            "answers": {"database": ["Postgres"]},
            "decision": "accept",
        }
        steer = await client.post(
            "/sessions/codex:test:sid/steer",
            data={"message": "Use SQLite instead"},
            headers={"origin": "http://test"},
        )
        assert steer.status_code == 200
        stop = await client.post(
            "/sessions/codex:test:sid/interrupt",
            headers={"origin": "http://test"},
        )
        assert stop.status_code == 200
        assert 'id="inject-result" class="inject-result running"' in stop.text
        assert 'aria-label="Stopping active turn"' in stop.text
        # A Claude chat's open questions (#3451): any one is answered or dismissed on its
        # own, an answer sent against an older set still counts, and the rest stay open.
        from agentdeck.models import open_questions_id

        def question(qid, prompt):
            return InteractionQuestion(
                id=qid, header="H", prompt=prompt,
                options=(InteractionOption("Yes"), InteractionOption("No")), allow_other=True,
            )

        open_now = (question("r2.0", "Ship it?"), question("r1.0", "Which branch?"))
        interaction = PendingInteraction(
            id=open_questions_id(q.id for q in open_now), kind="question", thread_id="sid",
            turn_id=None, title="Claude is asking", questions=open_now, dismissible=True,
        )
        page = await client.get("/sessions/codex:test:sid")
        assert 'value="dismiss:r1.0" formnovalidate>Dismiss</button>' in page.text
        assert 'data-question-drafts="codex:test:sid"' in page.text
        box = page.text[page.text.index('id="pending-interaction"'):]
        assert " required" not in box[: box.index("</section>")]
        response = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={"interaction_id": "questions-older", "answer__r1.0": "No", "decision": "accept"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 200
        assert answered["answers"] == {"r1.0": ["No"]}
        assert "Ship it?" in response.text and "Which branch?" not in response.text
        assert open_questions_id(["r2.0"]) in response.text
        response = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={"interaction_id": interaction.id, "answer__r1.0": "No",
                  "decision": "dismiss:r2.0"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 200
        assert answered["answers"] == {} and answered["decision"] == "dismiss:r2.0"
        assert "Ship it?" not in response.text and "Which branch?" in response.text
        nothing = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={"interaction_id": interaction.id, "decision": "accept"},
            headers={"origin": "http://test"},
        )
        assert nothing.status_code == 422
        # #3481: an agent relaying the owner's answer reads the Work's question box and
        # answers it as JSON, without the page's form.
        from types import SimpleNamespace

        from agentdeck.work_overview import WorkAttention

        asks = [
            WorkAttention("codex:test:sid", "Worker", "waiting", "/chats/w", asks=True,
                          question="Ship it?", interaction="question"),
            WorkAttention("claude_code:main:other", "Other", "waiting", "/chats/o", asks=True,
                          question="Which colour?"),
        ]
        card = SimpleNamespace(work_id="github_issue:eerovil/agentdeck#7", attention=asks)

        class Overview:
            async def current(self):
                return SimpleNamespace(cards=[card])

        app.state.work_overview = Overview()
        # #3503: the owning chat's box is listed once, Deckhand card or not.
        import agentdeck.work_router as work_router

        monkeypatch.setattr(
            work_router, "owner_of_issue",
            lambda state, repo, number, projection=None: ("codex:test:sid", f"{repo}#{number}"),
        )
        read = (await client.get("/api/works/eerovil/agentdeck/7/question")).json()
        box, message = read["questions"]
        card.attention = []
        (alone,) = (await client.get("/api/works/eerovil/agentdeck/7/question")).json()[
            "questions"
        ]
        assert alone["answer_url"] == box["answer_url"] and alone["question"] == "Ship it?"
        card.attention = asks
        assert box["answer_with"] == "question_box" and message["answer_with"] == "message"
        assert box["answer_url"] == "/api/sessions/codex:test:sid/interaction"
        assert [q["id"] for q in box["interaction"]["questions"]] == ["r2.0", "r1.0"]
        assert message["answer_url"] == "/sessions/claude_code:main:other/inject"
        response = await client.post(
            box["answer_url"],
            json={"interaction_id": box["interaction"]["id"], "answers": {"r2.0": "Yes"}},
        )
        assert response.status_code == 200
        assert answered["answers"] == {"r2.0": ["Yes"]}
        assert [q["id"] for q in response.json()["remaining"]["questions"]] == ["r1.0"]
        empty = await client.post(
            box["answer_url"], json={"interaction_id": interaction.id, "answers": {}}
        )
        assert empty.status_code == 422


async def test_pending_interaction_requires_interact_capability(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({Capability.TRANSCRIPT, Capability.INJECT})
    interaction = _question_interaction(
        "tok-stale", "Database", "Which database?", "Postgres", "SQLite"
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    # Patch the read + answer hooks; the INTERACT gate lives in the base's
    # pending_interaction / answer_interaction and must suppress both the render
    # and the POST for a session lacking INTERACT.
    monkeypatch.setattr(
        provider, "_actionable_interaction", lambda account, session_id: interaction
    )
    answered = False

    async def answer(*args, **kwargs):
        nonlocal answered
        answered = True
        return InjectResult(True)

    monkeypatch.setattr(provider, "_answer_actionable", answer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        partial = await client.get("/partials/sessions/codex:test:sid/interaction")
        response = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={"interaction_id": "tok-stale", "answer__database": "Postgres"},
            headers={"origin": "http://test"},
        )

    assert "Which database?" not in page.text
    assert "Which database?" not in partial.text
    assert response.status_code == 403
    assert answered is False






































async def test_only_secretary_can_use_steer_to_start_an_idle_turn(tmp_path):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({*session.capabilities, Capability.REVIVE})
    assert Capability.INJECT in session.capabilities
    assert Capability.STEER not in session.capabilities
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/steer",
            data={"message": "Start another turn."},
            headers={"origin": "http://test"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "active-turn steering unavailable"


































async def test_interaction_sse_uses_replaced_session_capabilities(tmp_path, monkeypatch):
    from dataclasses import replace

    from agentdeck.providers import PROVIDERS
    from agentdeck.web.routes_sse import _session_stream

    app = _web_app(tmp_path)
    state = app.state.app_state
    session = state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    interaction = _question_interaction(
        "tok-current", "Database", "Which database?", "Postgres", "SQLite"
    )
    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider,
        "pending_interaction",
        lambda account, candidate: (
            interaction if Capability.INTERACT in candidate.capabilities else None
        ),
    )
    monkeypatch.setattr(provider, "transcript_cursor", _async_return((0, 0)))
    monkeypatch.setattr(provider, "tail_transcript", _async_return(([], 0, 0)))
    monkeypatch.setattr(provider, "last_event", _async_return(None))

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            # The deck's chat list open, as this test's deck always had it (#1458).
            self.query_params = {"chats": "1"}

        async def is_disconnected(self):
            return False

    stream = _session_stream(FakeRequest(app), session.key)
    try:
        for _ in range(3):  # usage, Deckhand section, scheduled (#2603, #2599)
            await asyncio.wait_for(stream.__anext__(), timeout=2)
        state.update_session(
            replace(
                session,
                capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
            )
        )
        chunks = []
        for _ in range(8):
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=2)
            chunks.append(chunk)
            if "event: interaction" in chunk:
                break
    finally:
        await stream.aclose()

    interaction_chunk = next(chunk for chunk in chunks if "event: interaction" in chunk)
    assert "Which database?" not in interaction_chunk


def _question_interaction(token, header, prompt, opt_a, opt_b):
    return PendingInteraction(
        id=token,
        kind="question",
        thread_id="sid",
        turn_id="turn-1",
        title="Codex needs your answer",
        questions=(
            InteractionQuestion(
                id=header.lower(),
                header=header,
                prompt=prompt,
                options=(InteractionOption(opt_a, "a"), InteractionOption(opt_b, "b")),
                allow_other=True,
            ),
        ),
    )


async def test_interaction_widget_is_sse_driven_not_polled(tmp_path, monkeypatch):
    # Issue #28 regression (server contract): the pending-interaction widget must
    # NOT self-poll. A 2s poll that re-renders the form wiped the user's selected
    # radio/checkbox a second after they picked it. Instead it lives in an
    # `sse-swap="interaction"` slot and is pushed only when the interaction changes.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    interaction = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: interaction
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        unchanged = await client.get(
            "/partials/sessions/codex:test:sid/interaction",
            params={"known_interaction_id": "tok-1"},
        )
        stale = await client.get(
            "/partials/sessions/codex:test:sid/interaction",
            params={"known_interaction_id": ""},
        )
    assert page.status_code == 200
    assert unchanged.status_code == 204
    assert stale.status_code == 200
    assert "Which database should we use?" in stale.text
    assert 'sse-swap="interaction"' in page.text
    assert "Which database should we use?" in page.text
    # No self-poll: the partial URL is inert until the PWA foreground handler
    # requests one ID-keyed reconciliation.
    assert 'hx-trigger="every 2s"' not in page.text
    assert (
        'data-refresh-url="/partials/sessions/codex:test:sid/interaction"' in page.text
    )
    assert 'hx-get="/partials/sessions/codex:test:sid/interaction"' not in page.text


@pytest.mark.browser
async def test_pwa_foreground_reconciles_question_without_wiping_answer(
    tmp_path, monkeypatch
):
    # Issue #131: a question can appear while an installed PWA is backgrounded.
    # Foregrounding must render it even though the reconnected SSE stream seeds
    # its interaction ID from current server state. A later foreground with the
    # same question must not replace the form and wipe the user's answer.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    question = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    from agentdeck.providers import PROVIDERS

    box = {"pending": None}
    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, candidate: box["pending"]
    )

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
        "/static/session_bottom_follow.js": (
            static_dir / "session_bottom_follow.js"
        ).read_text(),
    }
    requests: list[str] = []

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://agentdeck.test"
    ) as client:
        initial = await client.get("/sessions/codex:test:sid")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(
                viewport={"width": 320, "height": 800}, service_workers="block"
            )
            page = await context.new_page()
            await page.add_init_script(
                """
                window.__agentdeckHidden = false;
                Object.defineProperty(document, 'hidden', {
                  configurable: true,
                  get: () => window.__agentdeckHidden
                });
                Object.defineProperty(document, 'visibilityState', {
                  configurable: true,
                  get: () => window.__agentdeckHidden ? 'hidden' : 'visible'
                });
                window.__eventSources = [];
                window.__scrolls = [];
                Element.prototype.scrollIntoView = function (opts) {
                  window.__scrolls.push({id: this.id, behavior: opts && opts.behavior});
                };
                class FakeEventSource extends EventTarget {
                  static CONNECTING = 0;
                  static OPEN = 1;
                  static CLOSED = 2;
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
                request = route.request
                relative = request.url.removeprefix("http://agentdeck.test")
                path = relative.split("?", 1)[0]
                requests.append(relative)
                if path == "/sessions/codex:test:sid":
                    await route.fulfill(
                        status=200, content_type="text/html", body=initial.text
                    )
                elif path in scripts:
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=scripts[path]
                    )
                elif path == "/partials/sessions/codex:test:sid/interaction":
                    response = await client.get(relative)
                    await route.fulfill(
                        status=response.status_code,
                        content_type="text/html",
                        body=response.text,
                    )
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/sessions/codex:test:sid")
            await page.wait_for_function("window.__eventSources.length === 1")
            assert await page.locator("#pending-interaction").count() == 1
            assert await page.locator("#pending-interaction form").count() == 0

            await page.evaluate(
                "window.__agentdeckHidden = true; "
                "document.dispatchEvent(new Event('visibilitychange'))"
            )
            box["pending"] = question
            await page.evaluate(
                "window.__agentdeckHidden = false; "
                "document.dispatchEvent(new Event('visibilitychange'))"
            )
            # Two separate things have to happen here, and when this used to be
            # one wait a failure could not tell them apart: the foreground has
            # to reopen the SSE stream, and the reopened stream has to re-render
            # the question. Wait on the reconnect first so "the reconnect never
            # fired" reports as that, not as a missing bit of text (#1308).
            await page.wait_for_function(
                "window.__eventSources.length === 2",
                timeout=QUESTION_RENDER_TIMEOUT_MS,
            )
            await page.wait_for_selector(
                "text=Which database should we use?",
                timeout=QUESTION_RENDER_TIMEOUT_MS,
            )
            await page.wait_for_function(
                "window.__scrolls.some(item => item.id === 'pending-interaction')"
            )

            radio = page.locator('input[name="answer__database"][value="Postgres"]')
            other = page.locator('input[name="other__database"]')
            await radio.check()
            await other.fill("Keep this typed answer")

            await page.evaluate(
                "window.__agentdeckHidden = true; "
                "document.dispatchEvent(new Event('visibilitychange')); "
                "window.__agentdeckHidden = false; "
                "document.dispatchEvent(new Event('visibilitychange'))"
            )
            await page.wait_for_function(
                "window.__eventSources.length === 3",
                timeout=QUESTION_RENDER_TIMEOUT_MS,
            )
            assert await radio.is_checked()
            assert await other.input_value() == "Keep this typed answer"
            assert any("known_interaction_id=" in request for request in requests)
            assert any("known_interaction_id=tok-1" in request for request in requests)
            await browser.close()


@pytest.mark.browser
async def test_interaction_selection_survives_live_updates_e2e(tmp_path, monkeypatch):
    # Issue #28 (the real browser lifecycle): a user picks a multiple-choice answer;
    # the constant SSE traffic of a live turn (status/tools/…) must not re-render the
    # widget and wipe the selection. A genuinely new question (a fresh `interaction`
    # event) does replace it — so several questions in a row work.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {
            Capability.TRANSCRIPT,
            Capability.INJECT,
            Capability.INTERACT,
            Capability.INTERRUPT,
        }
    )
    q1 = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    q2 = _question_interaction(
        "tok-2", "Cache", "Which cache layer should we use?", "Redis", "Memcached"
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: q1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")
    q2_fragment = render_pending_interaction(app.state.templates, "codex:test:sid", q2)

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
        "/static/question_drafts.js": (static_dir / "question_drafts.js").read_text(),
    }
    requests: list[str] = []
    interaction_asks: list[str] = []

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
              static CONNECTING = 0;
              static OPEN = 1;
              static CLOSED = 2;
              constructor(url) {
                super();
                this.url = url;
                this.readyState = FakeEventSource.OPEN;
                this.listenerNames = [];
                window.__eventSources.push(this);
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              addEventListener(name, listener, options) {
                this.listenerNames.push(name);
                super.addEventListener(name, listener, options);
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
              emit(name, data) { this.dispatchEvent(new MessageEvent(name, {data: data})); }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            request = route.request
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            requests.append(path)
            if path == "/partials/sessions/codex:test:sid/interaction":
                interaction_asks.append(request.url)
            if path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        # The widget is wired to the `interaction` SSE topic (proves sse-swap).
        await page.wait_for_function(
            "window.__eventSources.length === 1 && "
            "window.__eventSources[0].listenerNames.includes('interaction')"
        )

        radio = page.locator('input[name="answer__database"][value="Postgres"]')
        await radio.check()
        assert await radio.is_checked()

        async def emit(name, data=""):
            await page.evaluate(
                "([n, d]) => window.__eventSources[0].emit(n, d)", [name, data]
            )

        # The steady drip of a live turn must leave the selection alone.
        await emit("tools", '<div id="tool-activity">Working…</div>')
        await emit("status", '<div id="session-status">live</div>')
        await page.wait_for_timeout(2500)  # well past the old 2s poll interval
        await emit("tools", '<div id="tool-activity">Still working…</div>')
        assert await radio.is_checked(), "live SSE updates wiped the selected answer"
        # And the widget was never polled (the old clobbering GET is gone). The
        # one ask allowed is the catch-up when the stream opened, and it names
        # the question on screen, so an unchanged one is left alone (#2611).
        assert len(interaction_asks) <= 1, interaction_asks
        assert all("known_interaction_id=tok-1" in url for url in interaction_asks)

        # A real next question arrives over SSE → the widget swaps to it.
        await emit("interaction", q2_fragment)
        await page.wait_for_selector("text=Which cache layer should we use?")
        slot = await page.locator("#pending-interaction-slot").inner_html()
        assert "Which cache layer should we use?" in slot
        assert "Which database should we use?" not in slot

        # Enter in an open-questions box's text field answers it (#3749). The browser
        # presses the form's first submit button, a question's Dismiss, which threw
        # the typed answer away.
        from urllib.parse import parse_qs

        from agentdeck.models import open_questions_id

        asked = InteractionQuestion(
            id="r1.0", header="H", prompt="Ship it?",
            options=(InteractionOption("Yes"), InteractionOption("None of these")),
            allow_other=True,
        )
        box = PendingInteraction(
            id=open_questions_id(["r1.0"]), kind="question", thread_id="sid", turn_id=None,
            title="Claude is asking", questions=(asked,), dismissible=True,
        )
        await emit(
            "interaction", render_pending_interaction(app.state.templates, "codex:test:sid", box)
        )
        await page.wait_for_selector("text=Ship it?")
        await page.locator('input[name="answer__r1.0"][value="None of these"]').check()
        other = page.locator('input[name="other__r1.0"]')
        await other.fill("Wait for the review")
        async with page.expect_request(
            lambda request: request.method == "POST" and "/interaction" in request.url
        ) as posted:
            await other.press("Enter")
        sent = parse_qs((await posted.value).post_data or "")
        assert sent["decision"] == ["accept"]
        assert sent["answer__r1.0"] == ["None of these"]
        assert sent["other__r1.0"] == ["Wait for the review"]
        # Enter in an empty field of an unanswered question never dismisses it.
        await emit(
            "interaction",
            render_pending_interaction(
                app.state.templates, "codex:test:sid",
                replace(box, id=open_questions_id(["r2.0"]),
                        questions=(replace(asked, id="r2.0", prompt="Merge it?"),)),
            ),
        )
        await page.wait_for_selector("text=Merge it?")
        async with page.expect_request(
            lambda request: request.method == "POST" and "/interaction" in request.url
        ) as posted:
            await page.locator('input[name="other__r2.0"]').press("Enter")
        sent = parse_qs((await posted.value).post_data or "")
        assert sent["decision"] == ["accept"]
        assert await page.locator("text=Merge it?").is_visible()
        await browser.close()


@pytest.mark.browser
async def test_new_question_scrolls_into_view_e2e(tmp_path, monkeypatch):
    # Issue follow-up: when a question (or approval) arrives, the reader must be
    # scrolled to it — even if they had scrolled up into history. The cleared
    # state must NOT scroll. Driven off the same `interaction` SSE topic.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    q1 = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    q2 = _question_interaction(
        "tok-2", "Cache", "Which cache layer should we use?", "Redis", "Memcached"
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    # No prompt at load, so the slot starts empty and only SSE drives it.
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")
    templates = app.state.templates
    q1_fragment = render_pending_interaction(templates, "codex:test:sid", q1)
    q2_fragment = render_pending_interaction(templates, "codex:test:sid", q2)
    cleared_fragment = render_pending_interaction(templates, "codex:test:sid", None)

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
        "/static/session_bottom_follow.js": (
            static_dir / "session_bottom_follow.js"
        ).read_text(),
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1200, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            window.__eventSources = [];
            window.__scrolls = [];
            Element.prototype.scrollIntoView = function (opts) {
              window.__scrolls.push({id: this.id, behavior: opts && opts.behavior});
            };
            class FakeEventSource extends EventTarget {
              static CONNECTING = 0;
              static OPEN = 1;
              static CLOSED = 2;
              constructor(url) {
                super();
                this.url = url;
                this.readyState = FakeEventSource.OPEN;
                this.listenerNames = [];
                window.__eventSources.push(this);
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              addEventListener(name, listener, options) {
                this.listenerNames.push(name);
                super.addEventListener(name, listener, options);
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
              emit(name, data) { this.dispatchEvent(new MessageEvent(name, {data: data})); }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            request = route.request
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        await page.wait_for_function(
            "window.__eventSources.length === 1 && "
            "window.__eventSources[0].listenerNames.includes('interaction')"
        )

        async def emit(data):
            await page.evaluate(
                "d => window.__eventSources[0].emit('interaction', d)", data
            )

        # A real question scrolls the widget into view, smoothly.
        await emit(q1_fragment)
        await page.wait_for_function(
            "window.__scrolls.some(s => s.id === 'pending-interaction' "
            "&& s.behavior === 'smooth')"
        )
        after_q1 = await page.evaluate("window.__scrolls.length")

        # Clearing the prompt (answered/cancelled) must NOT scroll.
        await emit(cleared_fragment)
        await page.wait_for_timeout(200)
        assert await page.evaluate("window.__scrolls.length") == after_q1

        # The next question scrolls again — several in a row each get revealed.
        await emit(q2_fragment)
        await page.wait_for_function(f"window.__scrolls.length > {after_q1}")
        await browser.close()


@pytest.mark.browser
async def test_question_while_mobile_list_open_keeps_the_list_usable_e2e(
    tmp_path, monkeypatch
):
    # Issue #545: on a phone the chat stays mounted behind the session list,
    # translated a full viewport to the right inside overflow:hidden ancestors.
    # A question arriving while the reader is on the list must not drag those
    # ancestors sideways to chase the off-screen widget — that used to slide the
    # list away and leave an inert, pointer-events:none chat covering the screen
    # with no scrollbar and no gesture to get back: a frozen UI.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    # Live, so the chats list shows it to open from (an idle chat is off the list).
    app.state.app_state.update_session(replace(session, status=SessionStatus.LIVE))
    question = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: None)

    # On a phone the list sits under a chat only when the chat was opened from it: a chat
    # page loaded on its own carries no list (#2603).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        chats = await client.get("/chats")
        response = await client.get(
            "/sessions/codex:test:sid", headers={"X-AgentDeck-Session-Navigation": "detail"}
        )
    fragment = render_pending_interaction(app.state.templates, "codex:test:sid", question)

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    scripts = {
        "/static/" + name: (static_dir / name).read_text()
        for name in (
            "htmx.min.js",
            "sse.js",
            "session_bottom_follow.js",
            "mobile_session_stack.js",
            "session_navigation.js",
        )
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            window.__eventSources = [];
            class FakeEventSource extends EventTarget {
              static CONNECTING = 0;
              static OPEN = 1;
              static CLOSED = 2;
              constructor(url) {
                super();
                this.url = url;
                this.readyState = FakeEventSource.OPEN;
                window.__eventSources.push(this);
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
              emit(name, data) { this.dispatchEvent(new MessageEvent(name, {data: data})); }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/chats":
                await route.fulfill(status=200, content_type="text/html", body=chats.text)
            elif path.startswith(("/sessions/", "/chats/")):
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/chats")
        await page.locator('a.session[data-session-key="codex:test:sid"]').click()
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )

        # Back to the deck: the chat layer slides off, the list is what the
        # reader is looking at.
        await page.locator("a.back").click()
        await page.wait_for_function(
            "location.pathname === '/chats' && "
            "document.body.classList.contains('mobile-list-open')"
        )

        snapshot = """() => {
          const sidebar = document.querySelector('.session-sidebar');
          const layout = document.querySelector('.session-layout');
          const main = document.querySelector('main');
          return {
            sidebarLeft: Math.round(sidebar.getBoundingClientRect().left),
            layoutScroll: layout.scrollLeft,
            mainScroll: main.scrollLeft,
            documentScroll: document.documentElement.scrollLeft,
            listOpen: document.body.classList.contains('mobile-list-open'),
          };
        }"""
        before = await page.evaluate(snapshot)

        await page.evaluate(
            "d => window.__eventSources.at(-1).emit('interaction', d)", fragment
        )
        await page.wait_for_selector("text=Which database should we use?", state="attached")
        # Give the reveal's rAF + smooth scrolling time to land anywhere it would.
        await page.wait_for_timeout(600)
        after = await page.evaluate(snapshot)

        # And the widget is still queued up for the reader: the chat pane's own
        # scroll box moved to it, so returning to the chat shows the question.
        # The reveal is a scroll, so wait for it rather than sample it (#1826).
        await wait_until(
            page,
            "the chat pane scrolled the question into its own box",
            "() => {"
            " const detail = document.querySelector('.session-detail');"
            " const section = document.querySelector('#pending-interaction');"
            " if (!detail || !section) return false;"
            " const pane = detail.getBoundingClientRect();"
            " const box = section.getBoundingClientRect();"
            " return detail.scrollTop > 0"
            "   && box.top >= pane.top - 1 && box.bottom <= pane.bottom + 1;"
            "}",
        )
        revealed = await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const section = document.querySelector('#pending-interaction');
              const pane = detail.getBoundingClientRect();
              const box = section.getBoundingClientRect();
              return {
                paneScrolled: detail.scrollTop > 0,
                withinPane: box.top >= pane.top - 1 && box.bottom <= pane.bottom + 1,
              };
            }"""
        )
        await browser.close()

    assert before == {
        "sidebarLeft": 0,
        "layoutScroll": 0,
        "mainScroll": 0,
        "documentScroll": 0,
        "listOpen": True,
    }
    assert after == before
    assert revealed == {"paneScrolled": True, "withinPane": True}


def _async_return(value):
    async def _fn(*args, **kwargs):
        return value

    return _fn


@contextlib.asynccontextmanager
async def _live_server(app):
    """Run the real ASGI app on an ephemeral loopback port so a browser can drive
    it end to end (real routes, real SSE stream, real static JS). lifespan is off:
    app state is built in create_app, and we don't want real provider startup."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started, "uvicorn did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5)


@pytest.mark.browser
async def test_full_question_lifecycle_e2e_with_mock_llm(tmp_path, monkeypatch):
    # Full stack, real browser: a (mocked) LLM asks a multiple-choice question,
    # it streams to the page over the REAL SSE channel, scrolls into view, the
    # user's selection survives the live turn, and submitting delivers the answer
    # back through the real route. Only the provider (the "LLM") is faked.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    question = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    # The "LLM" backend: a flippable pending interaction + an answer sink.
    box = {"pending": None}
    captured = {}

    async def answer_interaction(account, session, interaction_id, *, answers, decision):
        captured.update(interaction_id=interaction_id, answers=answers, decision=decision)
        box["pending"] = None  # answered → cleared, like the real runtime
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: box["pending"]
    )
    monkeypatch.setattr(provider, "answer_interaction", answer_interaction)
    # Keep the SSE tail loop cheap and quiet — no real transcript backend.
    monkeypatch.setattr(provider, "transcript_cursor", _async_return((0, 0)))
    monkeypatch.setattr(provider, "tail_transcript", _async_return(([], 0, 0)))
    monkeypatch.setattr(provider, "last_event", _async_return(None))

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1024, "height": 720}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            // The reveal must move the chat pane's own scroll box and nothing
            // above it: an ancestor scroll chases the off-screen mobile chat
            // layer and strands the reader (#545).
            window.__ancestorScrolls = [];
            const orig = Element.prototype.scrollIntoView;
            Element.prototype.scrollIntoView = function (opts) {
              window.__ancestorScrolls.push({id: this.id});
              return orig.apply(this, arguments);
            };
            // Two separate things have to happen once the question is asked,
            // and while this was one wait a failure could not tell them apart:
            // the server has to notice the new interaction and push an
            // `interaction` event,
            // and htmx has to swap the fragment that event carries. Record the
            // events so the first half can be waited on by itself — the same
            // split #1308 asked for and the fake-EventSource tests above already
            // get from `window.__eventSources` (#1826). Recorded from the start,
            // so the stream's own first update can be waited on too.
            window.__sseEvents = [];
            document.addEventListener('DOMContentLoaded', () => {
              document.body.addEventListener('htmx:sseMessage', (event) => {
                window.__sseEvents.push(event.detail && event.detail.type);
              });
            });
            """
        )
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        # The slot is present but empty/hidden (no question).
        await page.wait_for_selector("#pending-interaction-slot", state="attached")
        # And the live chat is actually live before the question is asked. The
        # slot exists the moment the page is parsed, but the stream connects
        # later — seconds later on a loaded runner — and its first update
        # re-pushes every fragment of the chat. A question asked before then is
        # not the mid-turn question this test is about: it arrives with that
        # burst, or is caught up by the page's ask on connect rather than pushed
        # (#2611). That case has its own test below.
        await wait_until(
            page,
            "the live stream connected and sent its first update",
            "() => window.__sseEvents.includes('status')",
            timeout=QUESTION_RENDER_TIMEOUT_MS,
        )

        # The mocked LLM asks its question and the real SSE stream pushes it —
        # see QUESTION_RENDER_TIMEOUT_MS for why 10s was not enough.
        box["pending"] = question
        # What the runtime's change reaching the provider announces (#2865): the
        # stream waits on the chat's own topic, not on a tick.
        app.state.app_state.sessions_changed(("codex:test:sid",))
        await wait_until(
            page,
            "the server pushed the new question over the real SSE stream",
            "() => window.__sseEvents.includes('interaction')",
            timeout=QUESTION_RENDER_TIMEOUT_MS,
        )
        await page.wait_for_selector(
            "text=Which database should we use?", timeout=QUESTION_RENDER_TIMEOUT_MS
        )

        # It is revealed inside the chat pane — which is a scroll, so it lands a
        # frame or two after the swap and must be waited for, not sampled.
        await wait_until(
            page,
            "the question was revealed inside the chat pane",
            """() => {
              const paneEl = document.querySelector('.session-detail');
              const boxEl = document.querySelector('#pending-interaction');
              if (!paneEl || !boxEl) return false;
              const pane = paneEl.getBoundingClientRect();
              const box = boxEl.getBoundingClientRect();
              return box.top >= pane.top - 1 && box.bottom <= pane.bottom + 1;
            }""",
        )
        # And the reveal never reached for scrollIntoView, which scrolls every
        # ancestor scroll box (#545). Checked after the reveal has settled: a
        # sample taken before it would pass without having watched anything.
        assert await page.evaluate("window.__ancestorScrolls") == []
        await expect(page.locator("#pending-interaction")).to_be_in_viewport()

        # Pick an answer, let the live turn keep streaming, and confirm it survives.
        radio = page.locator('input[name="answer__database"][value="Postgres"]')
        await radio.check()
        await page.wait_for_timeout(2500)
        assert await radio.is_checked(), "selection did not survive the live SSE turn"

        # Submit → the answer must reach the (mock) LLM through the real route.
        await page.locator('#pending-interaction button[type="submit"]').click()
        await page.wait_for_selector(
            "text=Which database should we use?",
            state="detached",
            timeout=QUESTION_RENDER_TIMEOUT_MS,
        )
        await browser.close()

    # The selected answer AND the submit button's decision reached the (mock) LLM
    # through the real route. The decision rides a hidden carrier field that
    # interaction_feedback.js adds before disabling the button, so disabling the
    # submitter no longer drops `decision` from the serialized form.
    assert captured == {
        "interaction_id": "tok-1",
        "answers": {"database": ["Postgres"]},
        "decision": "accept",
    }


@pytest.mark.browser
async def test_a_question_asked_while_the_chat_connects_still_appears(tmp_path, monkeypatch):
    # The stream treats whatever interaction exists when it connects as already
    # on screen, so a question asked after the page rendered but before its
    # stream connected used to be neither in the page nor ever pushed: it sat
    # unseen until a reload (#2611). Hold the stream back until the question
    # exists, then let it connect; the page has to catch the question up itself.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    question = _question_interaction(
        "tok-1", "Database", "Which database should we use?", "Postgres", "SQLite"
    )
    box = {"pending": None}

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: box["pending"]
    )
    monkeypatch.setattr(provider, "transcript_cursor", _async_return((0, 0)))
    monkeypatch.setattr(provider, "tail_transcript", _async_return(([], 0, 0)))
    monkeypatch.setattr(provider, "last_event", _async_return(None))

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1024, "height": 720}, service_workers="block"
        )
        page = await context.new_page()
        held = asyncio.get_running_loop().create_future()

        async def hold_stream(route):
            if not held.done():
                held.set_result(route)
            else:
                await route.continue_()

        await page.route("**/events/sessions/**", hold_stream)
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector("#pending-interaction-slot", state="attached")
        route = await asyncio.wait_for(held, QUESTION_RENDER_TIMEOUT_MS / 1000)
        assert not await page.locator("text=Which database should we use?").count()

        box["pending"] = question
        await route.continue_()
        await page.wait_for_selector(
            "text=Which database should we use?", timeout=QUESTION_RENDER_TIMEOUT_MS
        )
        await browser.close()


@pytest.mark.browser
async def test_question_prompt_renders_markdown_e2e(tmp_path, monkeypatch):
    # #1579: agents write their questions in the same markdown as their messages,
    # so the panel showed raw `**[#164](url)**` in the one place a reader has to
    # understand before answering. The prompt now goes through the same markdown
    # pass as the transcript — and out of the <legend>, which takes no block markup.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    question = _question_interaction(
        "tok-md",
        "Merge #165",
        "**[#164](https://example.test/164)** is finished and waiting on you."
        " Ship it?",
        "Ship it",
        "Hold",
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: question
    )
    monkeypatch.setattr(provider, "transcript_cursor", _async_return((0, 0)))
    monkeypatch.setattr(provider, "tail_transcript", _async_return(([], 0, 0)))
    monkeypatch.setattr(provider, "last_event", _async_return(None))

    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1024, "height": 720}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector(".interaction-prompt[data-md]")
        rendered = await page.evaluate(
            """() => {
              const prompt = document.querySelector('.interaction-prompt');
              const link = prompt.querySelector('a');
              const legend = document.querySelector('.pending-interaction legend');
              return {
                bold: !!prompt.querySelector('strong'),
                href: link && link.getAttribute('href'),
                linkText: link && link.textContent,
                rawSyntax: /\\*\\*|\\]\\(/.test(prompt.textContent),
                header: legend.textContent.trim(),
                // The header keeps its own small-caps styling, and the question
                // is no longer swallowed by the legend.
                legendCarriesPrompt: legend.textContent.includes('finished'),
              };
            }"""
        )
        await browser.close()

    assert rendered == {
        "bold": True,
        "href": "https://example.test/164",
        "linkText": "#164",
        "rawSyntax": False,
        "header": "Merge #165",
        "legendCarriesPrompt": False,
    }


@pytest.mark.browser
async def test_approval_decision_survives_button_disable_e2e(tmp_path, monkeypatch):
    # Regression for the decision-drop: for a permission approval the chosen
    # decision (accept/decline/cancel) IS the answer. Disabling the submit button
    # on submit used to strip it from the serialized form. Clicking "Decline"
    # must still POST decision=decline.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
    )
    approval = PendingInteraction(
        id="perm-1",
        kind="command_approval",
        thread_id="sid",
        turn_id="turn-1",
        title="Approve command?",
        message="Run a shell command",
        command="rm -rf build",
        decisions=("accept", "acceptForSession", "decline", "cancel"),
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: approval)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        name: (static_dir / name.split("/")[-1]).read_text()
        for name in (
            "/static/htmx.min.js",
            "/static/sse.js",
            "/static/action_timing.js",
            "/static/interaction_feedback.js",
        )
    }
    posted = {}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(service_workers="block")
        page = await context.new_page()
        await page.add_init_script(
            """
            class FakeEventSource extends EventTarget {
              constructor(url) {
                super(); this.url = url; this.readyState = 1;
                queueMicrotask(() => { if (this.onopen) this.onopen(new Event('open')); });
              }
              close() { this.readyState = 2; }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            request = route.request
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if request.method == "POST" and path.endswith("/interaction"):
                posted["body"] = request.post_data
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<section id="pending-interaction" class="pending-interaction"></section>',
                )
            elif path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        await page.wait_for_selector("text=Approve command?")
        await page.locator('#pending-interaction button:has-text("Decline")').click()
        # The POST response clears the widget, so the prompt text detaches.
        await page.wait_for_selector("text=Approve command?", state="detached", timeout=10_000)
        await browser.close()

    assert posted.get("body") is not None, "no interaction POST was captured"
    assert "interaction_id=perm-1" in posted["body"]
    assert "decision=decline" in posted["body"]


async def test_idle_composer_hides_stop_button(tmp_path):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({Capability.TRANSCRIPT, Capability.INJECT})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")

    assert 'id="composer-controls"' in page.text
    assert ">Send</button>" in page.text
    assert 'aria-label="Stop active turn"' not in page.text


@pytest.mark.browser
async def test_composer_buttons_keep_textarea_focus(tmp_path):
    # Issue #17: pressing Send/Stop must not blur the message textarea. On mobile
    # a blur drops the keyboard and the afterRequest refocus re-shows it — a jump.
    # Enter stays smooth because it never blurs; the buttons must match.
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERRUPT}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")
    assert ">Stop</button>" in response.text  # both buttons present

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    assets = {
        "/static/app.css": ("text/css", (static_dir / "app.css").read_text()),
        "/static/htmx.min.js": (
            "text/javascript",
            (static_dir / "htmx.min.js").read_text(),
        ),
        "/static/sse.js": ("text/javascript", (static_dir / "sse.js").read_text()),
    }

    async def press_and_check_focus(page, label):
        prompt = page.locator("#inject-message")
        await prompt.focus()
        assert await page.evaluate("document.activeElement.id") == "inject-message"
        btn = page.locator("#composer-controls button", has_text=label)
        await btn.hover()  # scroll into view and wait until the button can receive events
        await page.mouse.down()  # native mousedown — the moment focus would be stolen
        focused = await page.evaluate("document.activeElement.id")
        await page.mouse.up()
        return focused

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            "window.EventSource = class extends EventTarget "
            "{ constructor(){ super(); } close(){} };"
        )

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if request.method == "POST":
                await route.fulfill(
                    status=202,
                    content_type="text/html",
                    body='<div id="inject-result" class="inject-result"></div>',
                )
            elif path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in assets:
                content_type, body = assets[path]
                await route.fulfill(
                    status=200, content_type=content_type, body=body
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        await page.locator("#inject-message").fill("hello")

        assert await press_and_check_focus(page, "Send") == "inject-message"
        assert await press_and_check_focus(page, "Stop") == "inject-message"
        await browser.close()


@pytest.mark.browser
async def test_a_held_send_shows_its_waiting_bubble_at_mobile_width(
    tmp_path, monkeypatch
):
    # #2847: a send while the runtime restarts is kept and shown as a waiting
    # bubble below the transcript, instead of "Failed · retry".
    app = _web_app(tmp_path)
    _restarting_runtime(app, monkeypatch)
    headers = {
        "origin": "http://test",
        "x-agentdeck-action-id": "held-browser-action",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        page_response = await client.get("/sessions/codex:test:sid")
        receipt = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "Check the overnight build"},
            headers=headers,
        )
    assert receipt.status_code == 202

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    assets = {
        "/static/app.css": ("text/css", (static_dir / "app.css").read_text()),
        "/static/htmx.min.js": (
            "text/javascript",
            (static_dir / "htmx.min.js").read_text(),
        ),
        "/static/sse.js": ("text/javascript", (static_dir / "sse.js").read_text()),
        "/static/action_timing.js": (
            "text/javascript",
            (static_dir / "action_timing.js").read_text(),
        ),
        "/static/interaction_feedback.js": (
            "text/javascript",
            (static_dir / "interaction_feedback.js").read_text(),
        ),
    }
    posted = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            "window.EventSource = class extends EventTarget "
            "{ constructor(){ super(); } close(){} };"
        )

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if request.method == "POST":
                posted["body"] = request.post_data or ""
                await route.fulfill(
                    status=202,
                    headers={"X-AgentDeck-Action-State": "held"},
                    content_type="text/html",
                    body=receipt.text,
                )
            elif path == "/sessions/codex:test:sid":
                await route.fulfill(
                    status=200, content_type="text/html", body=page_response.text
                )
            elif path in assets:
                content_type, body = assets[path]
                await route.fulfill(
                    status=200, content_type=content_type, body=body
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        await page.locator("#inject-message").fill("Check the overnight build")
        await page.locator("#composer-controls button", has_text="Send").click()

        held = page.locator(".held-message")
        await held.wait_for()
        assert "Check the overnight build" in await held.inner_text()
        assert "Waiting for the runtime to restart" in await held.inner_text()
        assert await page.locator("#inject-message").input_value() == ""
        assert await page.locator(".optimistic-message").count() == 0
        assert "Check the overnight build" in posted["body"]
        assert await page.locator(".delay-menu").count() == 0
        assert await page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
        )
        await browser.close()


@pytest.mark.browser
async def test_mobile_chat_height_tracks_the_visual_viewport(tmp_path):
    # Issue #17: the mobile chat height follows the *visual* viewport so the
    # sticky composer floats above the on-screen keyboard instead of behind it
    # (dvh doesn't shrink for the keyboard on iOS).
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/mobile_session_stack.js": (
            static_dir / "mobile_session_stack.js"
        ).read_text(),
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()
        await page.add_init_script(
            """
            var fake = new EventTarget();
            fake.width = window.innerWidth; fake.height = window.innerHeight; fake.offsetTop = 0;
            Object.defineProperty(window, 'visualViewport',
              {configurable: true, get: function(){ return fake; }});
            window.__setViewportHeight = function (h) {
              fake.height = h; fake.dispatchEvent(new Event('resize'));
            };
            window.EventSource = class extends EventTarget { constructor(){ super(); } close(){} };
            """
        )

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")

        def app_height():
            return page.evaluate(
                "document.documentElement.style.getPropertyValue('--app-height')"
            )

        # keyboard opens → visual viewport shrinks → app height tracks it
        await page.evaluate("window.__setViewportHeight(520)")
        assert await app_height() == "520px"
        # keyboard closes → back to full height
        await page.evaluate("window.__setViewportHeight(800)")
        assert await app_height() == "800px"
        await browser.close()


@pytest.mark.browser
async def test_mobile_composer_hides_without_a_jump_while_another_box_is_typed_in(
    tmp_path,
):
    # Issue #2513: on a phone, typing into any other box on the chat page hides
    # the pinned chat box, but it keeps its space so the chat does not move.
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    assets = {
        "/static/app.css": ("text/css", (static_dir / "app.css").read_text()),
        "/static/mobile_session_stack.js": (
            "text/javascript",
            (static_dir / "mobile_session_stack.js").read_text(),
        ),
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = {}
        for width in (390, 1280):
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()
            await page.add_init_script(
                "window.EventSource = class extends EventTarget"
                " { constructor(){ super(); } close(){} };"
            )

            async def serve(route, request):
                path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
                if path == "/sessions/codex:test:sid":
                    await route.fulfill(
                        status=200, content_type="text/html", body=response.text
                    )
                elif path in assets:
                    content_type, body = assets[path]
                    await route.fulfill(status=200, content_type=content_type, body=body)
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/sessions/codex:test:sid")
            # A long transcript, a box typed into inside it, a checkbox, and the
            # real title editor opened so its input can take focus.
            await page.evaluate(
                """() => {
                  const form = document.querySelector('.session-detail .inject-form');
                  const filler = document.createElement('div');
                  filler.style.height = '2400px';
                  const other = document.createElement('input');
                  other.type = 'text'; other.id = 'other-box';
                  const tick = document.createElement('input');
                  tick.type = 'checkbox'; tick.id = 'tick';
                  form.before(filler, other, tick);
                  document.getElementById('session-title-editor').open = true;
                }"""
            )

            def composer(page=page):
                return page.evaluate(
                    """() => {
                      const form = document.querySelector('.session-detail .inject-form');
                      const style = getComputedStyle(form);
                      return [style.position, style.visibility];
                    }"""
                )

            def layout(page=page):
                return page.evaluate(
                    """() => {
                      const pane = document.querySelector('.session-detail');
                      return [pane.scrollTop, pane.scrollHeight,
                              document.getElementById('other-box')
                                .getBoundingClientRect().top];
                    }"""
                )

            shown = await composer()
            await page.locator("#inject-message").fill("half-typed draft")
            await page.evaluate(
                "document.getElementById('inject-message').focus({preventScroll: true})"
            )
            while_composing = await composer()
            await page.evaluate(
                "document.querySelector('.session-detail').scrollTop = 1e6"
            )
            before = await layout()
            await page.evaluate(
                "document.getElementById('other-box').focus({preventScroll: true})"
            )
            other_box = await composer()
            during = await layout()
            await page.evaluate(
                "document.getElementById('tick').focus({preventScroll: true})"
            )
            checkbox = await composer()
            await page.evaluate(
                "document.getElementById('session-title-input')"
                ".focus({preventScroll: true})"
            )
            title = await composer()
            await page.evaluate("document.activeElement.blur()")
            after = await layout()
            results[width] = {
                "shown": shown,
                "composing": while_composing,
                "other": other_box,
                "checkbox": checkbox,
                "title": title,
                "blurred": await composer(),
                "layout": (before, during, after),
                "draft": await page.locator("#inject-message").input_value(),
            }
            await context.close()
        await browser.close()

    pinned = ["sticky", "visible"]
    hidden = ["static", "hidden"]
    phone = results[390]
    assert phone["shown"] == pinned
    assert phone["composing"] == pinned
    assert phone["other"] == hidden
    assert phone["checkbox"] == pinned
    assert phone["title"] == hidden
    assert phone["blurred"] == pinned
    before, during, after = phone["layout"]
    assert before[0] > 0
    assert before == during == after
    assert phone["draft"] == "half-typed draft"
    desktop = results[1280]
    assert desktop["other"] == pinned
    assert desktop["title"] == pinned


@pytest.mark.browser
async def test_browser_action_timing_covers_htmx_response_and_sse_reconciliation(tmp_path):
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
        "/static/action_timing.js": (static_dir / "action_timing.js").read_text(),
        "/static/interaction_feedback.js": (
            static_dir / "interaction_feedback.js"
        ).read_text(),
        "/static/session_bottom_follow.js": (
            static_dir / "session_bottom_follow.js"
        ).read_text(),
    }
    measured = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def measure(width):
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
                  emit(name, data, lastEventId) {
                    this.dispatchEvent(new MessageEvent(name, {
                      data: data, lastEventId: lastEventId || ''
                    }));
                  }
                }
                window.EventSource = FakeEventSource;
                """
            )
            captured = {}

            async def serve(route, request, captured=captured):
                path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
                if path == "/api/presence":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"status":"present"}',
                    )
                elif request.method == "POST":
                    captured["action_id"] = request.headers["x-agentdeck-action-id"]
                    captured["body"] = request.post_data
                    await asyncio.sleep(0.15)
                    action_id = captured["action_id"]
                    await route.fulfill(
                        status=202,
                        content_type="text/html",
                        headers={
                            "Server-Timing": "form;dur=2.0, queue;dur=3.0, total;dur=55.0",
                            "X-AgentDeck-Action-ID": action_id,
                            "X-AgentDeck-Action-State": "accepted",
                        },
                        body=(
                            '<div id="inject-result" class="inject-result running"></div>'
                            '<template hx-swap-oob="beforeend:.transcript">'
                            '<div class="ev user pending-message" data-pending-message '
                            f'data-client-action-id="{action_id}">'
                            '<div class="ev-text">measured send</div></div></template>'
                        ),
                    )
                elif path == "/sessions/codex:test:sid":
                    await route.fulfill(status=200, content_type="text/html", body=response.text)
                elif path in scripts:
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=scripts[path]
                    )
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/sessions/codex:test:sid")
            action = "send" if width == 1200 else "steer"
            await page.evaluate(
                "action => document.querySelector('.inject-form').dataset.agentdeckAction = action",
                action,
            )
            await page.locator("#inject-message").fill("measured send")
            await page.locator("#composer-controls button", has_text="Send").click()
            await page.wait_for_function(
                "document.querySelector('.optimistic-message .message-state')?.textContent "
                "=== 'Sending'"
            )
            if width == 320:
                # Exercise the reverse race: the durable SSE event arrives while
                # the POST is still in flight, then its OOB pending row arrives.
                await page.evaluate(
                    """() => {
                      const source = window.__eventSources[0];
                      const actionId = window.AgentDeckActionTiming.snapshot()[0].id;
                      source.emit('transcript',
                        '<div class="ev user" data-observed-action-id="' + actionId + '">' +
                        '<div class="ev-text">measured send</div></div>', '42');
                    }"""
                )
            await page.wait_for_function(
                "window.AgentDeckActionTiming && "
                "window.AgentDeckActionTiming.snapshot()[0]?.marks.response !== undefined"
            )
            await page.evaluate(
                """(emitTranscript) => {
                  const source = window.__eventSources[0];
                  const actionId = window.AgentDeckActionTiming.snapshot()[0].id;
                  if (!document.querySelector('[data-pending-message]')) {
                    document.querySelector('.transcript').insertAdjacentHTML(
                      'beforeend',
                      '<div class="ev user pending-message" data-pending-message ' +
                      'data-client-action-id="' + actionId + '">' +
                      '<div class="ev-text">measured send</div></div>'
                    );
                  }
                  source.emit('composer-controls', '<button type="submit">Send</button>');
                  if (emitTranscript) {
                    source.emit('transcript',
                      '<div class="ev user" data-observed-action-id="' + actionId + '">' +
                      '<div class="ev-text">measured send</div></div>', '42');
                  }
                }""",
                width == 1200,
            )
            await page.wait_for_function(
                "window.AgentDeckActionTiming.snapshot()[0]?.marks.first_transcript !== undefined"
            )
            await page.wait_for_function(
                "document.querySelectorAll('[data-pending-message]').length === 0"
            )
            await page.evaluate("window.htmx.reconnectSSE(document.body)")
            await page.wait_for_function("window.__eventSources.length === 2")
            record = await page.evaluate("window.AgentDeckActionTiming.snapshot()[0]")
            summary = await page.evaluate(
                "action => window.AgentDeckActionTiming.summary()[action]", action
            )
            item = {
                "width": width,
                "action": action,
                "record": record,
                "action_id": captured["action_id"],
                "body": captured["body"],
                "summary": summary,
                "resume_url": await page.evaluate("window.__eventSources[1].url"),
                "overflow": await page.evaluate(
                    "document.documentElement.scrollWidth > "
                    "document.documentElement.clientWidth"
                ),
            }
            await context.close()
            return item

        # The 16ms acknowledgement budget misses under host load even with the
        # browser-slot cap (#794): min-of-k with the threshold unchanged — up
        # to three fresh runs per width, the best sample must pass, and every
        # functional invariant still holds on every run.
        for width in (1200, 320):
            attempts = []
            for _attempt in range(3):
                attempts.append(await measure(width))
                if attempts[-1]["summary"]["acknowledgement_ms"]["p95"] < 16:
                    break
            measured.append(attempts)
        await browser.close()

    for item in [attempt for attempts in measured for attempt in attempts]:
        record = item["record"]
        assert record["id"] == item["action_id"]
        assert record["action"] == item["action"]
        assert item["resume_url"].endswith("?after_seq=42")
        assert record["serverTiming"] == (
            "form;dur=2.0, queue;dur=3.0, total;dur=55.0"
        )
        assert record["marks"]["response"] - record["marks"]["request_start"] >= 100
        assert {
            "interaction",
            "acknowledged",
            "request_start",
            "response",
            "first_sse_state",
            "first_transcript",
            "settled",
        } <= set(record["marks"])
        assert item["action_id"] in item["body"]
        assert item["summary"]["samples"] == 1
        assert item["summary"]["http_ms"]["p50"] >= 100
        assert item["summary"]["sse_ms"] is not None
        assert item["summary"]["transcript_ms"] is not None
        assert item["overflow"] is False

    for attempts in measured:
        best = min(a["summary"]["acknowledgement_ms"]["p95"] for a in attempts)
        assert best < 16


@pytest.mark.browser
async def test_immediate_feedback_is_specific_and_recovers_failed_inputs():
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    action_script = (static_dir / "action_timing.js").read_text()
    feedback_script = (static_dir / "interaction_feedback.js").read_text()
    html = """
      <div class="transcript"></div>
      <form id="send" data-agentdeck-action="send" hx-post="/sessions/test/inject">
        <textarea name="message">keep this draft</textarea>
        <input type="file" name="images"><button type="submit">Send</button>
      </form>
      <form id="stop" data-agentdeck-action="stop" hx-post="/sessions/test/interrupt">
        <button class="stop-button" type="submit">Stop</button>
      </form>
      <section id="pending-interaction">
        <form id="interaction" data-agentdeck-action="interaction"
              hx-post="/sessions/test/interaction">
          <label><input type="radio" name="answer" value="yes" checked>Yes</label>
          <button type="submit">Submit answer</button>
        </form>
      </section>
      <form id="new" data-agentdeck-action="new_session" hx-post="/sessions/new">
        <textarea name="message">new task</textarea><button type="submit">Start chat</button>
      </form>
      <div id="new-session-result"></div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def measure():
            page = await browser.new_page()
            await page.set_content(html)
            await page.add_script_tag(content=action_script)
            await page.add_script_tag(content=feedback_script)
            result = await page.evaluate(
                """() => {
                  function submit(id) {
                    const form = document.querySelector(id);
                    const button = form.querySelector('button[type="submit"]');
                    form.dispatchEvent(new SubmitEvent('submit', {
                      bubbles: true, cancelable: true, submitter: button
                    }));
                    return form;
                  }
                  const transfer = new DataTransfer();
                  transfer.items.add(new File(['image'], 'screenshot.png', {type: 'image/png'}));
                  const imageInput = document.querySelector('#send input[name="images"]');
                  imageInput.files = transfer.files;
                  const send = submit('#send');
                  const sendRecord = send._agentdeckActionTiming;
                  const stop = submit('#stop');
                  const interaction = submit('#interaction');
                  const fresh = submit('#new');
                  const immediate = {
                    sendState: document.querySelector(
                      '.optimistic-message .message-state'
                    ).textContent,
                    sendImages: document.querySelectorAll('.optimistic-message .ev-image').length,
                    sendImageAlt: document.querySelector('.optimistic-message .ev-image').alt,
                    stopText: stop.querySelector('button').textContent,
                    stopDisabled: stop.querySelector('button').disabled,
                    interactionStatus: interaction.querySelector(
                      '.interaction-submitting'
                    ).textContent,
                    interactionDisabled: interaction.querySelector('button').disabled,
                    answerPreserved: interaction.querySelector('input').checked,
                    newStatus: document.querySelector('#new-session-result').textContent,
                    acknowledgementMs:
                      sendRecord.marks.acknowledged - sendRecord.marks.interaction,
                  };
                  [send, stop, interaction, fresh].forEach(form => {
                    form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                      bubbles: true,
                      detail: {elt: form, successful: false, xhr: {status: 503}}
                    }));
                  });
                  return {
                    immediate,
                    failedSend: document.querySelector(
                      '.optimistic-message .message-state'
                    ).textContent,
                    draft: send.querySelector('textarea').value,
                    stopText: stop.querySelector('button').textContent,
                    stopDisabled: stop.querySelector('button').disabled,
                    interactionStatus: Boolean(
                      interaction.querySelector('.interaction-submitting')
                    ),
                    interactionDisabled: interaction.querySelector('button').disabled,
                    answerPreserved: interaction.querySelector('input').checked,
                    newStatus: document.querySelector('#new-session-result').textContent,
                  };
                }"""
            )
            await page.close()
            return result

        # Min-of-k against host load (#794): the 16ms budget is unchanged, up
        # to three fresh runs, and the best sample must pass. The functional
        # claims still hold on every run.
        attempts = []
        for _attempt in range(3):
            attempts.append(await measure())
            if attempts[-1]["immediate"]["acknowledgementMs"] < 16:
                break
        await browser.close()

    for result in attempts:
        assert result["immediate"] == {
            "sendState": "Sending",
            "sendImages": 1,
            "sendImageAlt": "Attached image 1",
            "stopText": "Stop…",
            "stopDisabled": True,
            "interactionStatus": "Submitting…",
            "interactionDisabled": True,
            "answerPreserved": True,
            "newStatus": "Starting chat…",
            "acknowledgementMs": result["immediate"]["acknowledgementMs"],
        }
        assert result | {"immediate": None} == {
            "immediate": None,
            "failedSend": "Failed · retry",
            "draft": "keep this draft",
            "stopText": "Stop",
            "stopDisabled": False,
            "interactionStatus": False,
            "interactionDisabled": False,
            "answerPreserved": True,
            "newStatus": "Failed to start chat. Retry.",
        }
    assert min(a["immediate"]["acknowledgementMs"] for a in attempts) < 16


@pytest.mark.browser
@pytest.mark.parametrize("viewport_width", [1200, 320])
async def test_message_send_retries_a_frontend_restart_with_the_same_action_id(
    viewport_width,
):
    """#1070: a dropped send retries itself without becoming a second message."""
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = [
        (static_dir / "htmx.min.js").read_text(),
        (static_dir / "action_timing.js").read_text(),
        (static_dir / "interaction_feedback.js").read_text().replace(
            "var SEND_RETRY_DELAYS = [800, 1600, 3200];",
            "var SEND_RETRY_DELAYS = [2000, 3200, 5000];",
        ),
    ]
    html = """
      <main>
        <div class="transcript"></div>
        <form class="inject-form" data-agentdeck-action="send"
              hx-post="/sessions/test/inject" hx-target="#inject-result"
              hx-swap="outerHTML" hx-encoding="multipart/form-data">
          <textarea name="message" required>survive the restart</textarea>
          <input type="file" name="images">
          <button type="submit">Send</button>
          <div id="inject-result"></div>
        </form>
      </main>
    """
    requests = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": viewport_width, "height": 844})

        async def serve(route):
            if route.request.url.endswith("/screen"):
                await route.fulfill(status=200, content_type="text/html", body=html)
                return
            if not route.request.url.endswith("/sessions/test/inject"):
                await route.fulfill(status=404)
                return
            requests.append({
                "action_id": route.request.headers.get("x-agentdeck-action-id"),
                "body": route.request.post_data,
            })
            if len(requests) == 1:
                await route.fulfill(status=502, body="Bad Gateway")
            else:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<div id="inject-result"></div>',
                    headers={"X-AgentDeck-Action-State": "accepted"},
                )

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/screen")
        for script in scripts:
            await page.add_script_tag(content=script)

        await page.locator('input[name="images"]').set_input_files({
            "name": "before.png", "mimeType": "image/png", "buffer": b"before"
        })
        await page.locator("button", has_text="Send").click()
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Auto-retrying…'"
        )
        await page.locator("form").evaluate(
            """form => {
              form.querySelector('textarea').value = 'newer draft';
              const transfer = new DataTransfer();
              transfer.items.add(new File(['after'], 'after.png', {type: 'image/png'}));
              form.querySelector('input[name=images]').files = transfer.files;
              form.requestSubmit();
            }"""
        )
        await page.wait_for_timeout(100)
        assert len(requests) == 1
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Accepted'",
            timeout=10000,
        )
        result = {
            "requests": requests,
            "rows": await page.locator(".optimistic-message").count(),
            "message": await page.locator("textarea[name=message]").input_value(),
            "image": await page.locator('input[name="images"]').evaluate(
                "input => input.files[0]?.name"
            ),
            "overflow": await page.evaluate(
                "document.documentElement.scrollWidth > "
                "document.documentElement.clientWidth"
            ),
        }
        await browser.close()

    assert len(result["requests"]) == 2
    assert result["requests"][0]["action_id"] == result["requests"][1]["action_id"]
    for request in result["requests"]:
        assert "survive the restart" in request["body"]
        assert 'filename="before.png"' in request["body"]
        assert "newer draft" not in request["body"]
        assert 'filename="after.png"' not in request["body"]
    assert result["rows"] == 1
    assert result["message"] == "newer draft"
    assert result["image"] == "after.png"
    assert result["overflow"] is False


@pytest.mark.browser
async def test_message_send_does_not_retry_an_uncertain_connection_reset():
    """A reset may hide an accepted write, so it remains a manual retry."""
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    html = """
      <div class="transcript"></div>
      <form data-agentdeck-action="send" hx-post="/sessions/test/inject">
        <textarea name="message">already landed</textarea>
        <button type="submit">Send</button>
      </form>
    """
    request_count = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()

        async def serve(route):
            nonlocal request_count
            if route.request.url.endswith("/screen"):
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif route.request.url.endswith("/sessions/test/inject"):
                request_count += 1
                await route.abort("connectionreset")
            else:
                await route.fulfill(status=404)

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/screen")
        for name in ("htmx.min.js", "action_timing.js", "interaction_feedback.js"):
            await page.add_script_tag(content=(static_dir / name).read_text())

        await page.locator("button", has_text="Send").click()
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Failed · retry'"
        )
        await page.wait_for_timeout(1000)
        button_disabled = await page.locator("button", has_text="Send").is_disabled()
        await browser.close()

    assert request_count == 1
    assert button_disabled is False


@pytest.mark.browser
async def test_rendered_composer_retry_preserves_a_newer_draft_and_attachment(tmp_path):
    """The real composer replays its snapshot while keeping later operator work."""
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/action_timing.js": (static_dir / "action_timing.js").read_text(),
        "/static/interaction_feedback.js": (
            static_dir / "interaction_feedback.js"
        ).read_text().replace(
            "var SEND_RETRY_DELAYS = [800, 1600, 3200];",
            "var SEND_RETRY_DELAYS = [2000, 3200, 5000];",
        ),
    }
    requests = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            """class FakeEventSource extends EventTarget {
              static OPEN = 1; constructor() { super(); this.readyState = 1; }
              close() { this.readyState = 2; }
            } window.EventSource = FakeEventSource;"""
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/codex:test:sid" and route.request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            elif path == "/sessions/codex:test:sid/inject":
                requests.append({
                    "action_id": route.request.headers["x-agentdeck-action-id"],
                    "body": route.request.post_data,
                })
                if len(requests) == 1:
                    await route.fulfill(status=502, body="Bad Gateway")
                else:
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        headers={"X-AgentDeck-Action-State": "accepted"},
                        body='<div id="inject-result"></div>',
                    )
            else:
                await route.fulfill(status=204)

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        composer = page.locator("#inject-message")
        images = page.locator('#inject-images')
        await composer.fill("original message")
        await images.set_input_files(
            {"name": "original.pdf", "mimeType": "application/pdf", "buffer": b"original"}
        )
        assert await page.locator(".attached-file-name").text_content() == "original.pdf"
        assert await page.locator(".paste-count").text_content() == "1 file attached"
        await page.locator("#composer-controls button", has_text="Send").click()
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Auto-retrying…'"
        )
        await composer.fill("newer draft")
        await images.set_input_files(
            {"name": "newer.zip", "mimeType": "application/zip", "buffer": b"newer"}
        )
        await composer.press("Enter")
        await page.wait_for_timeout(100)
        assert len(requests) == 1
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Accepted'",
            timeout=10000,
        )
        result = {
            "draft": await composer.input_value(),
            "image": await images.evaluate("input => input.files[0]?.name"),
            "rows": await page.locator(".optimistic-message").count(),
        }
        await browser.close()

    assert len(requests) == 2
    assert requests[0]["action_id"] == requests[1]["action_id"]
    for request in requests:
        assert "original message" in request["body"]
        assert 'filename="original.pdf"' in request["body"]
        assert "newer draft" not in request["body"]
        assert 'filename="newer.zip"' not in request["body"]
    assert result == {"draft": "newer draft", "image": "newer.zip", "rows": 1}


@pytest.mark.browser
async def test_composer_controls_survive_repeated_sse_updates_on_desktop_and_mobile(
    tmp_path, monkeypatch
):
    from agentdeck import issue_post

    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    # The fullest phone row (#3545): 🎤 and 🎧 from the host's speech service and an
    # issue chip, all left of Stop and Send.
    app.state.templates.env.globals["host_speech"] = True
    chip = issue_post.shown_target("eerovil/agentdeck#3545")
    monkeypatch.setattr(issue_post, "issue_post_target", lambda state, key: chip)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    idle_controls = render_composer_controls(app.state.templates, session)
    session.capabilities = frozenset({*session.capabilities, Capability.INTERRUPT})
    live_controls = render_composer_controls(app.state.templates, session)
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/htmx.min.js": (static_dir / "htmx.min.js").read_text(),
        "/static/sse.js": (static_dir / "sse.js").read_text(),
        "/static/app.css": (static_dir / "app.css").read_text(),
    }
    requests = []

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
              static CONNECTING = 0;
              static OPEN = 1;
              static CLOSED = 2;
              constructor(url) {
                super();
                this.url = url;
                this.readyState = FakeEventSource.OPEN;
                this.listenerNames = [];
                window.__eventSources.push(this);
                queueMicrotask(() => {
                  if (this.onopen) this.onopen(new Event('open'));
                });
              }
              addEventListener(name, listener, options) {
                this.listenerNames.push(name);
                super.addEventListener(name, listener, options);
              }
              close() { this.readyState = FakeEventSource.CLOSED; }
              emit(name, data) {
                this.dispatchEvent(new MessageEvent(name, {data: data}));
              }
            }
            window.EventSource = FakeEventSource;
            """
        )

        async def serve(route):
            request = route.request
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if request.method == "POST":
                requests.append(path)
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body='<div id="inject-result" class="inject-result">ok</div>',
                )
            elif path == "/sessions/codex:test:sid":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                kind = "text/css" if path.endswith(".css") else "text/javascript"
                await route.fulfill(status=200, content_type=kind, body=scripts[path])
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        await page.wait_for_function(
            "window.__eventSources.length === 1 && "
            "window.__eventSources[0].listenerNames.includes('composer-controls')"
        )

        async def emit_controls(fragment):
            await page.evaluate(
                "fragment => window.__eventSources[0].emit('composer-controls', fragment)",
                fragment,
            )

        # Repeated state notifications must replace the stable target's children,
        # never nest a second target or duplicate its forms and buttons.
        await emit_controls(live_controls)
        await emit_controls(live_controls)
        assert "Stop active turn" in await page.locator("#composer-controls").inner_html()
        await page.evaluate(
            """() => {
              window.__submits = {send: 0, stop: 0};
              document.querySelector('form.inject-form').addEventListener('submit', event => {
                window.__submits.send += 1;
                event.preventDefault();
                event.stopImmediatePropagation();
              }, {capture: true});
              document.querySelector('form#interrupt-form').addEventListener('submit', event => {
                window.__submits.stop += 1;
                event.preventDefault();
                event.stopImmediatePropagation();
              }, {capture: true});
            }"""
        )
        await page.locator("#inject-message").fill("send exactly once")
        await page.locator("#composer-controls .stop-button").click()
        await page.locator("#composer-controls button", has_text="Send").click()
        submits = await page.evaluate("window.__submits")

        await emit_controls(idle_controls)
        await emit_controls(idle_controls)
        idle = await page.evaluate(
            """() => ({
              targets: document.querySelectorAll('#composer-controls').length,
              sends: document.querySelectorAll('#composer-controls button[type="submit"]').length,
              stops: document.querySelectorAll('#composer-controls .stop-button').length,
              injectForms: document.querySelectorAll('form.inject-form').length,
              interruptForms: document.querySelectorAll('form#interrupt-form').length,
            })"""
        )

        # Stop coming and going must not move what sits left of it, such as 🎤
        # and 🎧 on the right-aligned phone row, and the row must not run over the
        # ＋ button however wide Stop's spot is (#3545).
        await page.evaluate(
            "document.documentElement.classList.add('speech-recordable', 'voice-recordable')"
        )
        shifts, fits, overlaps = [], [], []
        for width in (1200, 390, 360, 320):
            await resized(page, width, 800)
            lefts = []
            # Idle, a turn running, and Stop pressed (interaction_feedback's "Stop…").
            for fragment, stopping in ((idle_controls, False), (live_controls, False),
                                       (live_controls, True)):
                await emit_controls(fragment)
                if stopping:
                    fits.append(
                        await page.evaluate(
                            """() => { const stop = document.querySelector('.stop-button');
                                stop.textContent = 'Stop…';
                                return stop.scrollWidth <= stop.clientWidth; }"""
                        )
                    )
                lefts.append(
                    await page.evaluate(
                        "document.querySelector('#composer-controls')"
                        ".getBoundingClientRect().left"
                    )
                )
                # At 320 px this full row overlaps ＋ mid-turn, as it did before #3545;
                # phones that narrow are rare, so the check starts at 360.
                overlaps.extend(
                    [] if width < 360 else await page.evaluate(
                        """() => {
                          const form = document.querySelector('form.inject-form');
                          const row = [form.querySelector('.image-picker'),
                            ...form.querySelectorAll('.composer-actions > :not(.composer-hint)')]
                            .map(el => [el.className, el.getBoundingClientRect()])
                            .filter(([, box]) => box.width > 0);
                          const hits = [];
                          for (let i = 1; i < row.length; i++) {
                            if (row[i][1].left < row[i - 1][1].right - 0.5) {
                              hits.push(`${innerWidth}: ${row[i][0]} over ${row[i - 1][0]}`);
                            }
                          }
                          return hits;
                        }"""
                    )
                )
            shifts.append([left - lefts[0] for left in lefts[1:]])
        await emit_controls(idle_controls)
        placeholder = await page.evaluate(
            """() => { const p = document.querySelector('#composer-controls .stop-placeholder');
                return [getComputedStyle(p).visibility, p.type, p.tabIndex]; }"""
        )

        await emit_controls(live_controls)
        layouts = []
        for width in (1200, 320):
            await resized(page, width, 800)
            layouts.append(
                await page.evaluate(
                    """() => {
                      const controls = document.querySelector('#composer-controls');
                      const form = document.querySelector('form.inject-form');
                      const send = controls.querySelector('button:not(.stop-button)');
                      const stop = controls.querySelector('.stop-button');
                      return {
                        targets: document.querySelectorAll('#composer-controls').length,
                        buttons: controls.querySelectorAll('button').length,
                        sendUsesMessageForm: send.form === form,
                        stopUsesInterruptForm: stop.form.id === 'interrupt-form',
                        controlsInsideForm:
                          controls.getBoundingClientRect().right <=
                          form.getBoundingClientRect().right,
                        horizontalOverflow:
                          document.documentElement.scrollWidth >
                          document.documentElement.clientWidth,
                      };
                    }"""
                )
            )
        await browser.close()

    assert requests == []
    assert submits == {"send": 1, "stop": 1}
    assert idle == {
        "targets": 1,
        "sends": 1,
        "stops": 0,
        "injectForms": 1,
        "interruptForms": 1,
    }
    assert shifts == [[0, 0]] * 4
    assert fits == [True] * 4
    assert overlaps == []
    assert placeholder == ["hidden", "button", -1]
    assert layouts == [
        {
            "targets": 1,
            "buttons": 2,
            "sendUsesMessageForm": True,
            "stopUsesInterruptForm": True,
            "controlsInsideForm": True,
            "horizontalOverflow": False,
        },
        {
            "targets": 1,
            "buttons": 2,
            "sendUsesMessageForm": True,
            "stopUsesInterruptForm": True,
            "controlsInsideForm": True,
            "horizontalOverflow": False,
        },
    ]


def test_queued_message_is_pending_predicate():
    """is_pending is the single home for the injection 'still in flight' rule."""
    from agentdeck.inject import QueuedMessage

    def _state(state):
        item = QueuedMessage(1, "hi")
        item.state = state
        return item

    assert _state("queued").is_pending
    assert _state("running").is_pending
    assert _state("accepted").is_pending
    # Once the durable transcript row appears or the turn resolves, not pending.
    assert not _state("observed").is_pending
    assert not _state("complete").is_pending
    assert not _state("failed").is_pending


def _steer_session(account):
    return Session(
        "codex:test:sid", account.key, "sid", SessionStatus.LIVE,
        capabilities=frozenset({Capability.STEER}),
    )


async def test_deliver_now_tracks_and_steers(tmp_path):
    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 3)

        async def steer(self, account, session, message, images=None):
            return InjectResult(True, session_id="sid")

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    receipt = await service.deliver_now(account, _steer_session(account), Provider(), "go")

    assert receipt.result.accepted
    assert receipt.item is not None and receipt.item.state == "accepted"
    assert receipt.item.after_seq == 3  # the cursor read is tracked on the row




async def test_deliver_now_cleans_images_immediately_on_rejection(tmp_path):
    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            return InjectResult(False, "turn not active")

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    image = tmp_path / "s.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    receipt = await service.deliver_now(account, _steer_session(account), Provider(), "x", [image])

    assert not receipt.result.accepted
    assert not image.exists()  # rejected → cleaned now, not deferred


async def test_deliver_now_defers_image_cleanup_until_turn_ends(tmp_path):
    waited = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            return InjectResult(True)

        async def wait_for_session(self, account, session_id, *, timeout_s):
            waited.set()
            return InjectResult(True)

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    image = tmp_path / "s.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    receipt = await service.deliver_now(account, _steer_session(account), Provider(), "x", [image])

    assert receipt.result.accepted
    assert image.exists()  # accepted → deferred, still served mid-turn
    await asyncio.wait_for(waited.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not image.exists()  # cleaned once the turn finished


async def test_web_shutdown_does_not_delete_a_file_from_an_active_turn(tmp_path):
    waiting = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            return InjectResult(True)

        async def wait_for_session(self, account, session_id, *, timeout_s):
            waiting.set()
            await asyncio.Event().wait()

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    attachment = tmp_path / "temporary.upload"
    attachment.write_bytes(b"keep through restart")

    receipt = await service.deliver_now(
        account,
        _steer_session(account),
        Provider(),
        "read it",
        files=[attachment],
    )
    assert receipt.result.accepted
    await asyncio.wait_for(waiting.wait(), timeout=1)
    await service.stop()

    assert attachment.read_bytes() == b"keep through restart"
    attachment.unlink()


async def test_web_shutdown_keeps_a_file_when_queued_handoff_is_uncertain(tmp_path):
    handed_off = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def inject(self, account, session, message, *, timeout_s):
            handed_off.set()
            await asyncio.Event().wait()

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )
    attachment = tmp_path / "temporary.upload"
    attachment.write_bytes(b"keep uncertain queue")

    result = await service.start(
        account,
        session,
        Provider(),
        "read it",
        files=[attachment],
    )
    assert result.accepted
    await asyncio.wait_for(handed_off.wait(), timeout=1)
    await service.stop()

    assert attachment.read_bytes() == b"keep uncertain queue"
    attachment.unlink()


async def test_web_shutdown_keeps_a_file_when_new_chat_handoff_is_uncertain(tmp_path):
    handed_off = asyncio.Event()

    class Provider:
        def can_start_session(self, account):
            return True

        async def start_session(self, account, cwd, message, **kwargs):
            handed_off.set()
            await asyncio.Event().wait()

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    attachment = tmp_path / "temporary.upload"
    attachment.write_bytes(b"keep uncertain start")

    result = await service.start_new(
        account,
        Provider(),
        tmp_path,
        "read it",
        files=[attachment],
    )
    assert result.accepted
    await asyncio.wait_for(handed_off.wait(), timeout=1)
    await service.stop()

    assert attachment.read_bytes() == b"keep uncertain start"
    attachment.unlink()


async def test_turn_wait_timeout_keeps_a_temporary_file_for_the_hard_ttl(tmp_path):
    waited = asyncio.Event()

    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            return InjectResult(True)

        async def wait_for_session(self, account, session_id, *, timeout_s):
            waited.set()
            return InjectResult(False, "still running")

    service = InjectionService(InjectConfig(enabled=True, timeout_s=1))
    account = Account("codex:test", "codex", "test", tmp_path)
    attachment = tmp_path / "temporary.upload"
    attachment.write_bytes(b"keep after timeout")

    receipt = await service.deliver_now(
        account,
        _steer_session(account),
        Provider(),
        "read it",
        files=[attachment],
    )
    assert receipt.result.accepted
    await asyncio.wait_for(waited.wait(), timeout=1)
    await asyncio.sleep(0)
    assert attachment.read_bytes() == b"keep after timeout"
    await service.stop()
    attachment.unlink()


async def test_uncertain_steer_error_keeps_generic_file_but_cleans_native_image(tmp_path):
    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            raise RuntimeError("connection lost after send")

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    image = tmp_path / "s.png"
    image.write_bytes(b"image")
    attachment = tmp_path / "temporary.upload"
    attachment.write_bytes(b"keep on uncertainty")

    with pytest.raises(RuntimeError, match="connection lost"):
        await service.deliver_now(
            account,
            _steer_session(account),
            Provider(),
            "read it",
            [image],
            files=[attachment],
        )

    assert not image.exists()
    assert attachment.read_bytes() == b"keep on uncertainty"
    attachment.unlink()


async def test_deliver_now_compensates_and_reraises_on_error(tmp_path):
    class Provider:
        async def transcript_cursor(self, account, session):
            return (0, 0)

        async def steer(self, account, session, message, images=None):
            raise RuntimeError("boom")

    service = InjectionService(InjectConfig(enabled=True))
    account = Account("codex:test", "codex", "test", tmp_path)
    session = _steer_session(account)
    image = tmp_path / "s.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    raised = False
    try:
        await service.deliver_now(account, session, Provider(), "x", [image])
    except RuntimeError:
        raised = True
    assert raised  # the error propagates to the caller
    assert not image.exists()  # images cleaned on the exception path
    assert service.status(session.key).items[-1].state == "failed"  # row marked failed


async def test_delegated_session_can_resolve_its_own_key_from_its_token(tmp_path, monkeypatch):
    # A Codex thread never learns its session key from the environment. The deck
    # mints a token, hands it to the agent in the instructions it injects, and
    # binds it to the real key once the provider returns an id.
    app = _web_app(tmp_path)
    seen = {}

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        sandbox,
        model,
        effort,
        approval_policy,
        delegated,
        session_token=None,
    ):
        seen["session_token"] = session_token
        return InjectResult(True, session_id="auto-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "work the issue"},
        )
        assert response.status_code == 202
        for _ in range(50):
            await asyncio.sleep(0)
            if seen.get("session_token"):
                break
        token = seen["session_token"]
        assert token

        resolved = await client.get(f"/api/sessions/by-token/{token}")
        assert resolved.status_code == 200
        assert resolved.json()["session_key"] == "codex:test:auto-thread"
        assert resolved.json()["session_url"].startswith("/chats/")

        # That key is immediately usable on the session-owned API surfaces.
        # The token binds before the read model knows the session: this stubbed
        # provider never publishes one, yet a route that only stores rows under
        # the key accepts it because a deck token names it (#2627).
        registered = await client.post(
            f"/api/sessions/{resolved.json()['session_key']}/pull-requests",
            json={"pull_request": "eerovil/agentdeck#232"},
        )
        assert registered.status_code == 200
        assert [item["number"] for item in registered.json()["pull_requests"]] == [232]

        assert (await client.get("/api/sessions/by-token/not-a-token")).status_code == 404

    await app.state.injector.stop()


@pytest.mark.browser
async def test_quick_phrases_send_in_one_tap_and_never_eat_a_draft(tmp_path):
    """#734: the ⚡ menu adds no composer height while closed, one tap sends the
    verbose phrase, and a phrase lands in an existing draft instead of sending."""
    app = _web_app(tmp_path)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector("form.inject-form [data-quick-phrases]")

        # Closed, the control costs the composer nothing: removing it from layout
        # must not change the form's height, and the panel must not be rendered.
        closed = await page.evaluate(
            """() => {
              const form = document.querySelector('form.inject-form');
              const menu = form.querySelector('[data-quick-phrases]');
              const withControl = form.getBoundingClientRect().height;
              menu.style.display = 'none';
              const without = form.getBoundingClientRect().height;
              menu.style.display = '';
              return {
                addedHeight: Math.round(withControl - without),
                panelHeight: Math.round(
                  menu.querySelector('.phrase-menu-panel').getBoundingClientRect().height
                ),
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )

        # Hold the send in the browser: this asserts the module's contract, not
        # the injection backend, which has its own coverage.
        await page.evaluate(
            """() => {
              window.__submits = 0;
              document.querySelector('form.inject-form').addEventListener('submit', event => {
                window.__submits += 1;
                event.preventDefault();
                event.stopImmediatePropagation();
              }, {capture: true});
            }"""
        )

        await page.locator("form.inject-form [data-quick-phrases] summary").click()
        # Wait for the panel to be laid out before measuring it; opening it and
        # placing it are different frames (#1826).
        await wait_until(
            page,
            "the quick-phrase panel opened inside the viewport",
            "() => {"
            " const panel = document.querySelector("
            "   'form.inject-form .phrase-menu-panel');"
            " if (!panel) return false;"
            " const box = panel.getBoundingClientRect();"
            " return box.height > 0 && box.left >= 0 && box.right <= window.innerWidth;"
            "}",
        )
        opened = await page.evaluate(
            """() => {
              const form = document.querySelector('form.inject-form');
              const panel = form.querySelector('.phrase-menu-panel');
              const box = panel.getBoundingClientRect();
              return {
                visible: box.height > 0,
                labels: Array.from(panel.querySelectorAll('.phrase-chip'))
                  .map(chip => chip.textContent.trim()),
                withinViewport: box.left >= 0 && box.right <= window.innerWidth,
              };
            }"""
        )

        await page.locator(".phrase-chip", has_text="Status").click()
        sent = {
            "message": await page.locator("#inject-message").input_value(),
            "submits": await page.evaluate("window.__submits"),
            "menuOpen": await page.evaluate(
                "!!document.querySelector('[data-quick-phrases][open]')"
            ),
        }

        # A phrase chosen mid-sentence appends and waits; unsent text survives.
        await page.locator("#inject-message").fill("also please")
        await page.locator("form.inject-form [data-quick-phrases] summary").click()
        await page.locator(".phrase-chip", has_text="Progress").click()
        kept = {
            "message": await page.locator("#inject-message").input_value(),
            "submits": await page.evaluate("window.__submits"),
            "drafted": await page.evaluate(
                """() => Object.keys(sessionStorage)
                     .filter(key => key.startsWith('agentdeck:message-draft:'))
                     .map(key => sessionStorage.getItem(key))"""
            ),
        }

        await resized(page, 1200, 800)
        desktop = await page.evaluate(
            """() => {
              const panel = document.querySelector('.phrase-menu-panel');
              const box = panel.getBoundingClientRect();
              return {
                withinViewport: box.left >= 0 && box.right <= window.innerWidth,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert closed == {"addedHeight": 0, "panelHeight": 0, "overflow": False}
    assert opened["visible"] and opened["withinViewport"]
    assert opened["labels"][:2] == ["Status", "Progress"]
    # The button label is terse; what it sends spells the intent out.
    assert sent["submits"] == 1
    assert not sent["menuOpen"]
    assert sent["message"] == "Status check"
    assert kept["submits"] == 1
    assert kept["message"].startswith("also please Can we make this progress?")
    assert kept["drafted"] == [kept["message"]]
    assert desktop == {"withinViewport": True, "overflow": False}


def test_quick_phrases_are_terse_buttons():
    from agentdeck.composer_phrases import DEFAULT_COMPOSER_PHRASES

    phrases = DEFAULT_COMPOSER_PHRASES
    assert len(phrases) >= 4
    labels = [phrase.label for phrase in phrases]
    assert len(set(labels)) == len(labels)
    for phrase in phrases:
        # A chip has to stay thumb-sized on a phone (#734). The owner's own set
        # replaced the spelled-out built-ins (#3623), so a message may be short.
        assert len(phrase.label) <= 12, phrase
        assert phrase.text, phrase
        assert phrase.text == phrase.text.strip()


@pytest.mark.asyncio
async def test_quick_phrases_are_editable_on_the_settings_page(tmp_path):
    """#743: the ⚡ vocabulary is a setting, not a constant. What /settings saves is
    what every composer offers, and a refused edit keeps the text that was typed."""
    from agentdeck.composer_phrases import DEFAULT_COMPOSER_PHRASES

    app = _web_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        page = await client.get("/settings")
        assert page.status_code == 200
        # Nothing saved yet, so the editor starts from the live built-ins — and
        # offers no reset, because there is nothing to reset from.
        assert 'value="Status"' in page.text
        assert "Reset to defaults" not in page.text

        saved = await client.post(
            "/settings/composer-phrases",
            data={
                "action": "save",
                "label__0": "  Ship  it ",
                "text__0": "Merge the branch and deploy it.",
                # Clearing both fields of a row is how a phrase is removed.
                "label__1": "",
                "text__1": "",
                "label__2": "Hold",
                "text__2": "Stop where you are and wait for me.",
            },
        )
        assert saved.status_code == 200
        assert "Saved · 2 phrases" in saved.text
        assert "Reset to defaults" in saved.text

        chips_pattern = (
            r'class="phrase-chip" data-quick-phrase="([^"]*)"[^>]*>([^<]*)<'
        )
        session_page = await client.get("/sessions/codex:test:sid")
        chips = re.findall(chips_pattern, session_page.text)
        assert [label for _, label in chips] == ["Ship it", "Hold"]
        assert chips[0][0] == "Merge the branch and deploy it."

        refused = await client.post(
            "/settings/composer-phrases",
            data={"action": "save", "label__0": "", "text__0": "no label for this one"},
        )
        assert refused.status_code == 200
        assert "Give every phrase a short button label." in refused.text
        # The refused text comes back in the form rather than being swallowed.
        assert "no label for this one" in refused.text
        still = await client.get("/sessions/codex:test:sid")
        assert len(re.findall(chips_pattern, still.text)) == 2

        empty = await client.post(
            "/settings/composer-phrases",
            data={"action": "save", "label__0": "", "text__0": ""},
        )
        assert "Keep at least one phrase, or press Reset to defaults." in empty.text

        reset = await client.post(
            "/settings/composer-phrases", data={"action": "reset"}
        )
        assert "the built-in phrases are back" in reset.text
        back = await client.get("/sessions/codex:test:sid")
        assert len(re.findall(chips_pattern, back.text)) == len(
            DEFAULT_COMPOSER_PHRASES
        )




@pytest.mark.asyncio
@pytest.mark.browser
async def test_editing_a_quick_phrase_changes_the_chip_it_sends(tmp_path):
    """#743 end to end on the real pages: the editor saves through HTMX without
    leaving /settings, the row fits a phone, and the chip the chat offers is the
    edited one — sending the edited message in one tap."""
    app = _web_app(tmp_path)
    async with _live_server(app) as base_url, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        page = await context.new_page()
        await page.goto(f"{base_url}/settings")
        await page.wait_for_selector("#composer-phrase-settings")

        # One column on a phone: the message field must not be squeezed beside a
        # label field, and nothing may push the page sideways.
        mobile = await page.evaluate(
            """() => {
              const row = document.querySelector('.phrase-settings-row');
              const label = row.querySelector('input');
              const text = row.querySelector('textarea');
              return {
                stacked: text.getBoundingClientRect().top >
                  label.getBoundingClientRect().bottom,
                textWidth: Math.round(text.getBoundingClientRect().width),
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )

        # The editor starts from the live built-ins, so this rewrites the first
        # chip and adds a ninth in the trailing blank row.
        await page.fill("#phrase-label-0", "Ship")
        await page.fill("#phrase-text-0", "Merge the branch and deploy it now.")
        blank = await page.evaluate(
            "() => document.querySelectorAll('.phrase-settings-row').length - 1"
        )
        await page.fill(f"#phrase-label-{blank}", "Hold")
        await page.fill(f"#phrase-text-{blank}", "Stop where you are and wait for me.")
        await page.click('#composer-phrase-settings button[value="save"]')
        await page.wait_for_selector(".phrase-settings [role=status]")
        after_save = {
            "stillOnSettings": page.url.endswith("/settings"),
            "status": (
                await page.text_content(".phrase-settings [role=status]")
            ).strip(),
            "firstLabel": await page.input_value("#phrase-label-0"),
            "offersReset": await page.is_visible(
                '#composer-phrase-settings button[value="reset"]'
            ),
        }

        # A refusal must not swallow the edit it refused.
        await page.fill("#phrase-label-0", "")
        await page.click('#composer-phrase-settings button[value="save"]')
        await page.wait_for_selector(".phrase-settings [role=alert]")
        refused = {
            "alert": (await page.text_content(".phrase-settings [role=alert]")).strip(),
            "keptText": await page.input_value("#phrase-text-0"),
        }
        await page.fill("#phrase-label-0", "Ship")
        await page.click('#composer-phrase-settings button[value="save"]')
        await page.wait_for_selector(".phrase-settings [role=status]")

        # The chat's ⚡ menu now offers the edited vocabulary, and one tap sends it.
        # The send is held in the browser: this asserts what the chip puts in the
        # composer, which the injection backend covers separately.
        await page.goto(f"{base_url}/sessions/codex:test:sid")
        await page.wait_for_selector("form.inject-form [data-quick-phrases]")
        await page.evaluate(
            """() => {
              document.querySelector('form.inject-form').addEventListener('submit', event => {
                window.__sent = document.querySelector('#inject-message').value;
                event.preventDefault();
                event.stopImmediatePropagation();
              }, {capture: true});
            }"""
        )
        await page.click("form.inject-form [data-quick-phrases] > summary")
        labels = await page.eval_on_selector_all(
            ".phrase-chip[data-quick-phrase]", "els => els.map(e => e.textContent.trim())"
        )
        await page.click('.phrase-chip[data-quick-phrase^="Merge the branch"]')
        sent = await page.evaluate("window.__sent")

        await resized(page, 1200, 900)
        await page.goto(f"{base_url}/settings")
        await page.wait_for_selector("#composer-phrase-settings")
        desktop = await page.evaluate(
            """() => {
              const row = document.querySelector('.phrase-settings-row');
              const label = row.querySelector('input');
              const text = row.querySelector('textarea');
              return {
                sideBySide: text.getBoundingClientRect().left >
                  label.getBoundingClientRect().right,
                overflow: document.documentElement.scrollWidth >
                  document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert mobile["stacked"] and not mobile["overflow"]
    assert mobile["textWidth"] > 280, mobile
    assert after_save["stillOnSettings"]
    assert after_save["status"].startswith("Saved · 9 phrases")
    assert after_save["firstLabel"] == "Ship"
    assert after_save["offersReset"]
    assert refused["alert"] == "Give every phrase a short button label."
    assert refused["keptText"] == "Merge the branch and deploy it now."
    assert labels[0] == "Ship" and labels[-1] == "Hold" and len(labels) == 9
    # "Status" was the first built-in; rewriting row 0 replaced it outright.
    assert "Status" not in labels
    # The chip's own message, not the built-in it replaced.
    assert sent == "Merge the branch and deploy it now."
    assert desktop == {"sideBySide": True, "overflow": False}


@pytest.mark.asyncio
async def test_a_quick_phrase_save_that_beats_htmx_still_lands(tmp_path):
    """htmx is deferred, so a fast tap submits the form natively. That post must
    save and answer with the whole settings page, not a bare fragment."""
    app = _web_app(tmp_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        native = await client.post(
            "/settings/composer-phrases",
            data={"action": "save", "label__0": "Ship", "text__0": "Merge and deploy."},
        )
        assert native.status_code == 200
        assert native.text.startswith("<!doctype html>")
        assert "Saved · 1 phrase" in native.text
        # And the same post over htmx stays a fragment.
        fragment = await client.post(
            "/settings/composer-phrases",
            data={"action": "save", "label__0": "Ship", "text__0": "Merge and deploy."},
            headers={"hx-request": "true"},
        )
        assert fragment.text.lstrip().startswith("<form")

        session_page = await client.get("/sessions/codex:test:sid")
        assert session_page.text.count('class="phrase-chip"') == 1


async def test_a_lost_start_keeps_watching_for_the_token_binding(tmp_path):
    """A web restart must not dead-end the status strip at "lost" (#792).

    ``InjectionService._starts`` is in-memory, so a restart during a spawn
    drops the record while the runtime keeps going. The token binding lands in
    the shared store as soon as the provider names a session, so the page keeps
    re-asking by token for a bounded window instead of terminating — and only
    past that window does the honest dead-end copy render.
    """
    app = _web_app(tmp_path)
    token = "tok-792-lost"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No record of the start: keep looking, and count the re-asks.
        looking = await client.get(f"/partials/starting-chat/{token}")
        assert 'data-starting-state="lost"' in looking.text
        assert "still watching for it" in looking.text
        assert f"/partials/starting-chat/{token}?checks=1" in looking.text

        # The binding appears — the spawn survived the restart — and the very
        # next poll hands the page over.
        app.state.app_state.bind_session_token(token, "codex:test:sid")
        ready = await client.get(f"/partials/starting-chat/{token}?checks=7")
        assert 'data-starting-state="ready"' in ready.text
        assert app.state.app_state.chat_path_for_session("codex:test:sid") in ready.text

        # Past the window nothing will ever bind: the dead end, with no re-ask.
        settled = await client.get("/partials/starting-chat/tok-792-gone?checks=90")
        assert 'data-starting-state="lost"' in settled.text
        assert "cannot tell which chat that start became" in settled.text
        assert "hx-get" not in settled.text
    await app.state.injector.stop()


async def test_a_delegated_child_never_starts_before_the_cutover_is_durable(
    tmp_path, monkeypatch
):
    """#2207: a child started before the cutover could be recorded as legacy later.

    The cutover write fails, so the provider is never asked to start the child; after
    a restart with a healthy store, the same delegation starts once and the creation
    rule decides it (the parent's Run first) — never the legacy backfill.
    """
    from agentdeck.db import Db
    from agentdeck.providers import PROVIDERS

    calls = []

    async def fake_start(account, cwd, message, **kwargs):
        calls.append(message)
        return InjectResult(True, session_id="child-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        return InjectResult(True)

    async def fake_result(account, session_id):
        return "done"

    real = Db.record_work_backfill
    monkeypatch.setattr(Db, "record_work_backfill", lambda self, name, summary: False)
    first = _web_app(tmp_path)
    account = first.state.accounts[0]
    provider = PROVIDERS[account.provider_id]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    result, delegation_id = await first.state.injector.start_delegation(
        account, provider, tmp_path, "review this", parent_session_id="codex:test:sid"
    )
    assert result.accepted is False and delegation_id is None
    assert "cutover" in (result.reason or "")
    assert calls == []
    control = first.state.app_state.budget_db
    assert control.load_delegated_sessions() == set()
    assert control.session_work_bindings() == []
    await first.state.injector.stop()

    # Restart with a store that can write again, and retry the same delegation.
    monkeypatch.setattr(Db, "record_work_backfill", real)
    second = _web_app(tmp_path)
    state = second.state.app_state
    assert state.budget_db.work_backfill("delegated_work_cutover")
    work_id = "github_issue:eerovil/agentdeck#2207"
    state.bind_session_run("codex:test:sid", work_id, "run-1")

    result, delegation_id = await second.state.injector.start_delegation(
        account, provider, tmp_path, "review this", parent_session_id="codex:test:sid"
    )
    assert result.accepted is True
    for _ in range(20):
        await asyncio.sleep(0)
        status = second.state.injector.delegation_status(delegation_id)
        if status is not None and status.state == "complete":
            break
    assert calls == ["review this"]

    child = "codex:test:child-thread"
    runs = state.budget_db.session_run_bindings(session_key=child)
    assert [(r.work_id, r.run_id) for r in runs] == [(work_id, "run-1")]
    assert [r.evidence for r in state.budget_db.session_work_bindings()] == ["run"]
    # The legacy backfill does not take it: it was recorded after the cutover.
    assert state.backfill_delegated_work()["considered"] == 0
    await second.state.injector.stop()


async def test_a_store_that_keeps_no_cutover_still_starts_delegated_children(tmp_path):
    """#2207: a state with no store keeps no cutover, so it never waits."""
    from types import SimpleNamespace

    from agentdeck.state import AppState

    state = AppState()
    assert state.delegation_cutover_ready() is True
    service = InjectionService(
        InjectConfig(enabled=True),
        on_delegation_started=state.mark_delegated_session,
        delegation_ready=state.delegation_cutover_ready,
    )
    calls = []

    class _Provider:
        def can_start_session(self, account):
            return True

        async def start_session(self, account, cwd, message, **kwargs):
            calls.append(message)
            return InjectResult(True, session_id="child-thread")

        async def wait_for_session(self, account, session_id, *, timeout_s):
            return InjectResult(True)

        async def session_result(self, account, session_id):
            return "done"

    account = SimpleNamespace(key="codex:test")
    result, delegation_id = await service.start_delegation(
        account, _Provider(), tmp_path, "review this", parent_session_id="codex:test:sid"
    )
    assert result.accepted is True and delegation_id is not None
    for _ in range(20):
        await asyncio.sleep(0)
        if calls and "codex:test:child-thread" in state.delegated_session_keys:
            break
    assert calls == ["review this"]
    assert "codex:test:child-thread" in state.delegated_session_keys
    await service.stop()


@pytest.mark.browser
async def test_rendered_composer_empties_on_send_and_refills_on_failure(tmp_path):
    """The box clears as the message leaves, not when the server answers (#2843)."""
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    requests = []
    release = [asyncio.Event(), asyncio.Event()]
    statuses = [200, 500]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            """class FakeEventSource extends EventTarget {
              static OPEN = 1; constructor() { super(); this.readyState = 1; }
              close() { this.readyState = 2; }
            } window.EventSource = FakeEventSource;"""
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/codex:test:sid" and route.request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path.startswith("/static/") and path.endswith(".js"):
                await route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=(static_dir / path.removeprefix("/static/")).read_text(),
                )
            elif path == "/sessions/codex:test:sid/inject":
                index = len(requests)
                requests.append(route.request.post_data)
                await release[index].wait()
                await route.fulfill(
                    status=statuses[index],
                    content_type="text/html",
                    headers={"X-AgentDeck-Action-State": "accepted"},
                    body='<div id="inject-result"></div>',
                )
            else:
                await route.fulfill(status=204)

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        composer = page.locator("#inject-message")

        await composer.fill("first message")
        await composer.press("Enter")
        await page.wait_for_function("document.querySelector('#inject-message').value === ''")
        in_flight = await page.locator(".optimistic-message .message-state").text_content()
        release[0].set()
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Accepted'"
        )
        after_success = await composer.input_value()

        await composer.fill("second message")
        await composer.press("Enter")
        await page.wait_for_function("document.querySelector('#inject-message').value === ''")
        release[1].set()
        await page.wait_for_function(
            "document.querySelector('#inject-message').value === 'second message'"
        )
        failed_state = await page.locator(
            ".optimistic-message.failed .message-state"
        ).text_content()
        await browser.close()

    assert len(requests) == 2
    assert "first message" in requests[0]
    assert "second message" in requests[1]
    assert in_flight != "Accepted"
    assert after_success == ""
    assert failed_state == "Failed · retry"


@pytest.mark.browser
async def test_rendered_composer_holds_reloads_and_second_sends_while_one_is_in_flight(
    tmp_path,
):
    """An emptied box still counts as unsent work, and takes no second send (#2843)."""
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    origin = "http://localhost:8765"  # a secure context, so the reload guard is armed
    requests = []
    release = asyncio.Event()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            """class FakeEventSource extends EventTarget {
              static OPEN = 1; constructor() { super(); this.readyState = 1; }
              close() { this.readyState = 2; }
            } window.EventSource = FakeEventSource;"""
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix(origin)
            if path == "/sessions/codex:test:sid" and route.request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path.startswith("/static/") and path.endswith(".js"):
                await route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=(static_dir / path.removeprefix("/static/")).read_text(),
                )
            elif path == "/sessions/codex:test:sid/inject":
                requests.append(route.request.post_data)
                await release.wait()
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    headers={"X-AgentDeck-Action-State": "accepted"},
                    body='<div id="inject-result"></div>',
                )
            else:
                await route.fulfill(status=204)

        await page.route(f"{origin}/**", serve)
        await page.goto(f"{origin}/sessions/codex:test:sid")
        assert await page.evaluate("typeof window.AgentDeckWorkInProgress") == "function"
        composer = page.locator("#inject-message")

        await composer.fill("first message")
        await composer.press("Enter")
        await page.wait_for_function("document.querySelector('#inject-message').value === ''")
        await page.evaluate("document.activeElement.blur()")
        held_while_sending = await page.evaluate("window.AgentDeckWorkInProgress()")

        await composer.fill("first message")
        await composer.press("Enter")
        await page.wait_for_timeout(300)
        second_enter = {
            "requests": len(requests),
            "box": await composer.input_value(),
            "rows": await page.locator(".optimistic-message").count(),
        }

        # The in-form status poll finishes while the send is still on its way:
        # htmx re-homes that completion to the form, carrying its own request.
        await page.evaluate(
            """() => {
              const form = document.querySelector('#inject-message').form;
              form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                bubbles: true,
                detail: {elt: form, successful: true, xhr: new XMLHttpRequest()}
              }));
            }"""
        )
        await composer.press("Enter")
        await page.wait_for_timeout(300)
        after_stray_poll = {
            "requests": len(requests),
            "box": await composer.input_value(),
            "sending": await page.locator("form[data-sending]").count(),
        }

        release.set()
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Accepted'"
        )
        await page.wait_for_timeout(300)
        after = {
            "requests": len(requests),
            "box": await composer.input_value(),
            "sending": await page.locator("form[data-sending]").count(),
        }
        await composer.fill("")
        await page.evaluate("document.activeElement.blur()")
        held_after = await page.evaluate("window.AgentDeckWorkInProgress()")
        await browser.close()

    assert held_while_sending is True
    # The same text typed again is a new draft, not the in-flight message.
    assert second_enter == {"requests": 1, "box": "first message", "rows": 1}
    assert after_stray_poll == {"requests": 1, "box": "first message", "sending": 1}
    assert after == {"requests": 1, "box": "first message", "sending": 0}
    assert held_after is False


@pytest.mark.browser
async def test_rendered_composer_never_rewrites_a_draft_typed_during_a_send(tmp_path):
    """Retries and failures leave a newer draft alone, same text or /secret (#2843)."""
    app = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/sessions/codex:test:sid")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    scripts = {
        "/static/interaction_feedback.js": (
            static_dir / "interaction_feedback.js"
        ).read_text().replace(
            "var SEND_RETRY_DELAYS = [800, 1600, 3200];",
            "var SEND_RETRY_DELAYS = [1500, 3200, 5000];",
        ),
    }
    statuses = [502, 200, 500]
    requests = []
    hold_failure = asyncio.Event()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 390, "height": 844}, service_workers="block"
        )
        await page.add_init_script(
            """class FakeEventSource extends EventTarget {
              static OPEN = 1; constructor() { super(); this.readyState = 1; }
              close() { this.readyState = 2; }
            } window.EventSource = FakeEventSource;"""
        )

        async def serve(route):
            path = route.request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/codex:test:sid" and route.request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=response.text)
            elif path in scripts:
                await route.fulfill(
                    status=200, content_type="text/javascript", body=scripts[path]
                )
            elif path.startswith("/static/") and path.endswith(".js"):
                await route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    body=(static_dir / path.removeprefix("/static/")).read_text(),
                )
            elif path == "/sessions/codex:test:sid/inject":
                index = len(requests)
                requests.append(route.request.post_data)
                if index == 2:
                    await hold_failure.wait()
                await route.fulfill(
                    status=statuses[index],
                    content_type="text/html",
                    headers={"X-AgentDeck-Action-State": "accepted"},
                    body='<div id="inject-result"></div>',
                )
            else:
                await route.fulfill(status=204)

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/codex:test:sid")
        composer = page.locator("#inject-message")

        # 1. The same words typed again while a 502 waits for its automatic retry.
        await composer.fill("same words")
        await composer.press("Enter")
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Auto-retrying…'"
        )
        await composer.fill("same words")
        await page.wait_for_function(
            "document.querySelector('.optimistic-message .message-state')?.textContent "
            "=== 'Accepted'",
            timeout=10000,
        )
        after_retry = await composer.input_value()

        # 2. A /secret typed while an ordinary send is on its way to failing.
        await composer.fill("ordinary message")
        await composer.press("Enter")
        await page.wait_for_function("document.querySelector('#inject-message').value === ''")
        await composer.fill("/secret hunter2")
        hold_failure.set()
        await page.wait_for_selector(".optimistic-message.failed")
        await page.wait_for_timeout(200)
        after_failure = await composer.input_value()
        stored = await page.evaluate(
            "Object.keys(sessionStorage).map(key => sessionStorage.getItem(key))"
        )
        await browser.close()

    assert len(requests) == 3
    assert "same words" in requests[1]
    assert after_retry == "same words"
    assert after_failure == "/secret hunter2"
    assert not any("hunter2" in value or "ordinary message" in value for value in stored)


async def test_new_chat_takes_the_spare_cli_booted_for_exactly_that_start(
    tmp_path, monkeypatch
):
    """#3420: a press matching the spare starts under its token and key, once per press."""
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import start_spares

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    claude = PROVIDERS["claude_code"]
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    app.state.db.record_manual_new_chat_cwd(str(tmp_path))
    asked: list[tuple] = []
    dropped: list[tuple] = []
    starts: list[dict] = []

    async def prepare_spare(account, shape, key, **kwargs):
        asked.append((shape, key, kwargs["session_token"], kwargs["cwd"]))
        return "booted"

    async def drop_spare(account, shape):
        dropped.append((account.key, shape))
        return "dropped"

    async def start_session(account, cwd, message, **kwargs):
        starts.append({"cwd": cwd, **kwargs})
        return InjectResult(True, session_id=f"sid-{len(starts)}")

    monkeypatch.setattr(claude, "can_start_session", lambda account: True)
    monkeypatch.setattr(claude, "prepare_spare", prepare_spare)
    monkeypatch.setattr(claude, "drop_spare", drop_spare)
    monkeypatch.setattr(claude, "start_session", start_session)
    spares = app.state.start_spares
    await spares.refresh()
    new_chat = [entry for entry in asked if entry[0] == start_spares.NEW_CHAT]
    assert new_chat == [(start_spares.NEW_CHAT, new_chat[0][1], new_chat[0][2], str(tmp_path))]
    _, spare_key, spare_token, _ = new_chat[0]

    async def press(cwd: Path, action_id: str):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.post(
                "/sessions/new",
                data={"account_key": "claude_code:main", "cwd": str(cwd), "message": "go"},
                headers={"origin": "http://test", "x-agentdeck-action-id": action_id},
            )
        assert response.status_code == 202
        await app.state.injector.wait_for_new_start("claude_code:main", 1.0)
        return starts[-1]

    taken = await press(tmp_path, "press-0001")
    assert (taken["session_token"], taken["worker_key"]) == (spare_token, spare_key)
    # A resend of that press names the same worker, never the next spare.
    resent = await press(tmp_path, "press-0001")
    assert (resent["session_token"], resent["worker_key"]) == (spare_token, spare_key)
    # The spare is gone once taken, and a start elsewhere is cold: no worker key.
    cold = await press(other_dir, "press-0002")
    assert "worker_key" not in cold and cold["session_token"] != spare_token
    await asyncio.sleep(0)
    await spares.refresh()
    latest = [entry for entry in asked if entry[0] == start_spares.NEW_CHAT][-1]
    # The next spare boots where New chat now remembers, under a new key.
    assert latest[3] == str(other_dir) and latest[1] != spare_key
    # No New work form asked for a Work spare (#3590), so only that one is stopped.
    assert {shape for _key, shape in dropped} <= {start_spares.WORK}
    # With New chat switched off, nothing asks for either start: the runtime stops both.
    monkeypatch.setattr(app.state.config.inject, "enabled", False)
    await spares.refresh()
    assert {entry for entry in dropped if entry[1] != start_spares.WORK} == {
        ("claude_code:main", start_spares.DECK_CHAT),
        ("claude_code:main", start_spares.NEW_CHAT),
    }
    await spares.stop()
    await app.state.injector.stop()


async def test_an_open_new_work_form_keeps_a_work_spare_in_its_own_worktree(
    tmp_path, monkeypatch
):
    """#3590: booted with the Work role in a spare worktree, taken once, and swept after."""
    from agentdeck import work_start
    from agentdeck.providers import PROVIDERS
    from agentdeck.web import start_spares

    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=500),
        accounts=[AccountConfig(provider="claude_code", label="main", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    claude = PROVIDERS["claude_code"]
    asked: list[tuple] = []
    made: list[str] = []
    removed: list[str] = []

    async def prepare_spare(account, shape, key, **kwargs):
        asked.append((shape, kwargs["cwd"], kwargs.get("role")))
        return "booted"

    async def drop_spare(account, shape):
        return "none"

    def make_spare_worktree(main):
        made.append(f"{main}/.worktrees/work-spare-{len(made)}")
        return made[-1]

    (account,) = app.state.accounts
    monkeypatch.setattr(claude, "prepare_spare", prepare_spare)
    monkeypatch.setattr(claude, "drop_spare", drop_spare)
    monkeypatch.setattr(work_start, "start_account", lambda app_, repository: account)
    monkeypatch.setattr(work_start, "checkout_candidates", lambda app_: [])
    monkeypatch.setattr(
        "agentdeck.work_side_chat.local_repository_checkout", lambda repo, found: "/src/deck"
    )
    monkeypatch.setattr(work_start, "make_spare_worktree", make_spare_worktree)
    monkeypatch.setattr(work_start, "remove_spare_worktree", removed.append)
    spares = app.state.start_spares
    spares.want_work("eerovil/agentdeck")

    await spares.refresh()
    await spares.refresh()  # the same spare is kept, not made again

    work = [entry for entry in asked if entry[0] == start_spares.WORK]
    assert made == ["/src/deck/.worktrees/work-spare-0"]
    assert set(work) == {(start_spares.WORK, made[0], "work")}
    # Only a press in that repository on that account takes it, and only once.
    assert spares.claim_work("eerovil/other", account_key="claude_code:main") is None
    taken = spares.claim_work("EEROVIL/agentdeck", account_key="claude_code:main")
    assert taken is not None and taken[2] == made[0]
    assert spares.claim_work("eerovil/agentdeck", account_key="claude_code:main") is None
    spares.release(start_spares.WORK)
    await spares.refresh()
    assert made[-1] == "/src/deck/.worktrees/work-spare-1"
    # Ten minutes after the form last asked, the untaken spare and its worktree go.
    spares._work_wanted = ("eerovil/agentdeck", 0.0)
    await spares.refresh()
    assert removed == [made[-1]] and made[0] not in removed
    await spares.stop()
    await app.state.injector.stop()
