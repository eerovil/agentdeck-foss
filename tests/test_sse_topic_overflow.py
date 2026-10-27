"""Exercise both real SSE generators with synthetic state and render boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agentdeck.events import EventBus
from agentdeck.web import routes_sse as sse
from agentdeck.web.shared_fragments import SharedFragments

pytestmark = pytest.mark.asyncio


class _Request:
    def __init__(self, app_state, cycles, query_params=None):
        self.app = SimpleNamespace(state=app_state)
        self.query_params = query_params or {}
        self.cycles = cycles

    async def is_disconnected(self):
        self.cycles -= 1
        return self.cycles < 0


@pytest.fixture
def streams(monkeypatch):
    # No browser, provider, filesystem scan or background service is started.
    # EventBus, SharedFragments, format_sse and both generators remain real.
    monkeypatch.setattr(sse, "HEARTBEAT_S", 0.001)
    monkeypatch.setattr(sse, "SessionView", SimpleNamespace)
    monkeypatch.setattr(sse, "SESSION_FRAGMENTS", ())
    monkeypatch.setattr(sse, "SESSION_FRAGMENTS_BY_NAME", {
        "interaction": SimpleNamespace(name="interaction", signature=lambda _view: ())
    })
    for name in (
        "session_issue_chips", "session_pull_request_chips",
        "session_head_review_chip",
    ):
        monkeypatch.setattr(sse, name, lambda *args: ())
    monkeypatch.setattr(sse, "resolve_activity_label", lambda **kwargs: None)
    for name in (
        "render_limit_bars", "render_assistant_session",
    ):
        monkeypatch.setattr(sse, name, lambda *args, **kwargs: "<fragment>current</fragment>")
    monkeypatch.setattr(sse, "get_state", lambda request: request.app.state.app_state)
    monkeypatch.setattr(sse, "get_shared_fragments", lambda request: request.app.state.shared)
    monkeypatch.setattr(sse, "get_accounts", lambda request: ())
    monkeypatch.setattr(sse, "get_injector", lambda request: None)
    monkeypatch.setattr(sse, "get_templates", lambda request: request.app.state.templates)

    def make(*, maxsize=64):
        bus = EventBus(maxsize=maxsize)
        shared = SharedFragments(bus)
        session = SimpleNamespace(
            key="codex:test:one", account_key="codex:test", last_progress=None,
            last_activity=None, pull_requests=(), issues=(), status=sse.SessionStatus.IDLE,
            question=None, lifecycle_active=False, capabilities=frozenset(),
        )
        provider = SimpleNamespace(
            transcript_cursor=AsyncMock(return_value=(0, 0)),
            last_event=AsyncMock(return_value=None),
            tail_transcript=AsyncMock(return_value=([], 0, 0)),
            pending_interaction=lambda *args: None,
        )
        presentation = SimpleNamespace(
            display=lambda current: current, active_child_sessions=lambda current: (),
            has_working_subagent=lambda current: False,
        )
        state = SimpleNamespace(
            bus=bus, usage={}, host_stats=None, sessions={session.key: session},
            session_presentation=Mock(return_value=presentation),
            chat_session_keys=lambda key: (key,), chat_reachable_leg_key=lambda key: key,
            chat_identity_key=lambda key: key,
            presentation_deadline=lambda: None,
        )
        app_state = SimpleNamespace(
            app_state=state, shared=shared, auth_watch=None,
            assistant=SimpleNamespace(session_context=lambda value: None, contexts={}),
            scheduled_messages=SimpleNamespace(visible=lambda key: ()),
            templates=SimpleNamespace(get_template=lambda name: SimpleNamespace(
                render=lambda **kwargs: "<held></held>"
            )),
        )
        monkeypatch.setattr(sse, "resolve_session", lambda request, key: (
            object(), session, provider
        ))
        for topic, name in (
            ("jobs", "render_job_queue"), ("deployment", "render_deployment_status"),
        ):
            monkeypatch.setattr(sse, name, lambda *args, _topic=topic: (
                f"<{_topic}>{bus.revision(_topic)[0]}</{_topic}>"
            ))

        def open_stream(mode, *, cycles=1, query_params=None):
            request = _Request(app_state, cycles, query_params)
            return sse._stream(request) if mode == "dashboard" else sse._session_stream(
                request, session.key
            )

        return bus, shared, state, open_stream

    return make


async def _prime(stream, mode="dashboard"):
    # Both streams subscribe before these primes; pause while the response is
    # being sent so overflow is deterministic rather than timing-dependent. The
    # last prime is the chat's own Deckhand section on a chat page, and the topbar
    # on the deck: no stream sends the old panel (#2256), a chat list (#2603) or
    # the done bar (#2599).
    last = "event: assistant-session\n" if mode == "session" else "event: usage\n"
    while True:
        event = await asyncio.wait_for(anext(stream), timeout=1)
        assert not event.startswith(("event: jobs\n", "event: deployment\n"))
        assert not event.startswith("event: assistant\n")
        if event.startswith(last):
            return


async def _rest(stream):
    async def collect():
        return [event async for event in stream]
    return await asyncio.wait_for(collect(), timeout=1)


@pytest.mark.parametrize("mode", ["dashboard", "session"])
@pytest.mark.parametrize("maxsize", [1, 2, 64])
@pytest.mark.parametrize("flood", ["host", "sessions"])
async def test_overflow_recovers_jobs_and_deployment(streams, mode, maxsize, flood):
    bus, _shared, _state, open_stream = streams(maxsize=maxsize)
    stream = open_stream(mode)
    try:
        await _prime(stream, mode)
        bus.publish("jobs")
        bus.publish("deployment")
        for _ in range(100):
            bus.publish(flood)
        events = await _rest(stream)
    finally:
        await stream.aclose()
    for topic in ("jobs", "deployment"):
        assert sum(event.startswith(f"event: {topic}\n") for event in events) == 1
        assert f"<{topic}>1</{topic}>" in "".join(events)
    assert all(not subscribers for subscribers in bus._subs.values())


@pytest.mark.parametrize("mode", ["dashboard", "session"])
async def test_no_historical_or_quiet_topbar_repush(streams, mode):
    bus, _shared, _state, open_stream = streams()
    bus.publish("jobs")
    bus.publish("deployment")
    stream = open_stream(mode)
    try:
        await _prime(stream, mode)
        events = await _rest(stream)
    finally:
        await stream.aclose()
    assert not any(event.startswith(("event: jobs\n", "event: deployment\n")) for event in events)


@pytest.mark.parametrize("mode", ["dashboard", "session"])
async def test_update_during_yield_is_not_acknowledged_early(streams, mode):
    bus, _shared, _state, open_stream = streams(maxsize=1)
    stream = open_stream(mode, cycles=2)
    try:
        await _prime(stream, mode)
        bus.publish("jobs")
        while not (await asyncio.wait_for(anext(stream), timeout=1)).startswith("event: jobs\n"):
            pass
        # The first pass is paused inside yield. This later publication must
        # belong to the next pass, even though its queue entry is overwritten.
        bus.publish("deployment")
        for _ in range(100):
            bus.publish("host")
        events = await _rest(stream)
    finally:
        await stream.aclose()
    assert sum(event.startswith("event: deployment\n") for event in events) == 1
    assert not any(event.startswith("event: jobs\n") for event in events)


async def test_two_clients_recover_independently_with_shared_render_cache(streams):
    bus, shared, _state, open_stream = streams(maxsize=1)
    first, second = open_stream("dashboard"), open_stream("dashboard")
    try:
        await _prime(first)
        await _prime(second)
        bus.publish("jobs")
        bus.publish("deployment")
        for _ in range(100):
            bus.publish("host")
        for events in (await _rest(first), await _rest(second)):
            for topic in ("jobs", "deployment"):
                assert sum(event.startswith(f"event: {topic}\n") for event in events) == 1
        assert shared.hits > 0
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.parametrize("mode", ["dashboard", "session"])
@pytest.mark.parametrize("query", [{}, {"chats": "1", "history_limit": "80"}])
async def test_no_stream_sends_a_chat_list(streams, mode, query):
    """The chats page is rendered once per load (#2603): neither stream carries a list,
    whatever an old address asks for and whatever moves on the deck."""
    bus, _shared, _state, open_stream = streams()
    stream = open_stream(mode, query_params=query)
    try:
        primed = []
        while not primed or not primed[-1].startswith(
            "event: assistant-session\n" if mode == "session" else "event: usage\n"
        ):
            primed.append(await asyncio.wait_for(anext(stream), timeout=1))
        for topic in ("sessions", "assistant", "works"):
            bus.publish(topic)
        events = primed + await _rest(stream)
    finally:
        await stream.aclose()
    assert not any(event.startswith("event: sessions\n") for event in events)
