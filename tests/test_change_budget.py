"""Work per change has a budget, and this counts it (ADR 0007, #2865).

Nothing here measures time. Each test drives one change through the real state and the
real Work overview and asserts how many times the expensive steps ran
(``agentdeck.work_counter``), so a busy host cannot make it flake and a change that starts
rebuilding the whole deck again fails here first.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_work_ui import OTHER, WORK, _add, _app, _board, _overview

from agentdeck import work_counter
from agentdeck.models import Session, SessionStatus
from agentdeck.state import AppState, chat_topic

pytestmark = pytest.mark.asyncio


def _chat(key: str, **fields) -> Session:
    account, _, session_id = key.rpartition(":")
    return Session(
        key=key,
        account_key=account,
        session_id=session_id,
        status=SessionStatus.LIVE,
        last_activity=datetime.now(UTC),
        **fields,
    )


async def test_one_chat_change_rebuilds_that_chat_once_and_wakes_no_other_page():
    state = AppState()
    keys = [f"codex:test:chat{index}" for index in range(20)]
    for key in keys:
        state.update_session(_chat(key, title=key))
    state.session_presentation()
    changed = keys[3]
    with (
        state.bus.subscribe(chat_topic(changed)) as mine,
        state.bus.subscribe(*(chat_topic(key) for key in keys if key != changed)) as others,
        work_counter.counting() as spent,
    ):
        state.update_session(replace(state.sessions[changed], title="Renamed"))
        presentation = state.session_presentation()
        assert state.session_presentation() is presentation
    assert spent[work_counter.CHAT_DISPLAY] == 1
    assert spent[work_counter.SESSION_PRESENTATION] == 1
    assert mine.get_nowait() is not None
    assert others.get_nowait() is None
    assert presentation.display(state.sessions[changed]).title == "Renamed"


async def test_a_child_change_also_wakes_the_parent_it_shows_on():
    state = AppState()
    state.update_session(_chat("codex:test:parent"))
    state.update_session(_chat("codex:test:child", parent_session_key="codex:test:parent"))
    state.session_presentation()
    with state.bus.subscribe(chat_topic("codex:test:parent")) as parent:
        state.update_session(replace(state.sessions["codex:test:child"], thinking=True))
    assert parent.get_nowait() is not None


async def test_one_chat_change_builds_only_its_own_works_cards(tmp_path, monkeypatch):
    import agentdeck.web.work_view as work_view

    # The once-a-minute full check is the net under silent inputs, not part of this budget.
    monkeypatch.setattr(work_view, "FULL_CHECK_S", 1e9)
    app = _app(tmp_path, board=_board())
    mine = _add(app, "claude_code:test:mine", WORK)
    _add(app, "claude_code:test:theirs", OTHER)
    first = await _overview(app)
    assert {card.work_id for card in first.cards} >= {WORK.work_id, OTHER.work_id}

    with work_counter.counting() as idle:
        await _overview(app)
    # Nothing moved: every card is the one already built, and no Work's inputs are even read.
    assert idle[work_counter.WORK_CARD] == 0
    assert idle[work_counter.WORK_CARD_CHECK] == 0

    state = app.state.app_state
    state.update_session(replace(state.sessions[mine], last_text="something new"))
    with work_counter.counting() as spent:
        overview = await _overview(app)
    # The light card and the hydrated one of the one Work the chat is in; none of OTHER's,
    # whose inputs are not read at all (#3228).
    assert 1 <= spent[work_counter.WORK_CARD] <= 2
    assert 1 <= spent[work_counter.WORK_CARD_CHECK] <= 2
    # Any other topic a card reads moves every Work's gate: all are checked, none rebuilt.
    state.bus.publish("jobs")
    with work_counter.counting() as unscoped:
        await _overview(app)
    assert unscoped[work_counter.WORK_CARD_CHECK] >= 2 and unscoped[work_counter.WORK_CARD] == 0
    assert next(card for card in overview.cards if card.work_id == OTHER.work_id) is next(
        card for card in first.cards if card.work_id == OTHER.work_id
    )


async def test_two_tabs_on_one_chat_share_one_read_and_one_render(tmp_path, monkeypatch):
    """Each update is built once and sent to every open tab (#2865): two pages on the same
    chat, one append, one transcript read and one render of each changed fragment."""
    import asyncio

    from test_web import _app_with_state

    from agentdeck.models import TranscriptEvent
    from agentdeck.providers import PROVIDERS
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    key = "claude_code:test:sid1"
    provider = PROVIDERS["claude_code"]
    appended: list[TranscriptEvent] = []
    reads = {"n": 0}

    async def cursor(account, session):
        return 0, 0

    async def tail(account, session, offset, seq):
        reads["n"] += 1
        events = [event for event in appended if event.seq > seq]
        if not events:
            return [], offset, seq
        return events, offset + len(events), events[-1].seq

    async def last_event(account, session):
        return None

    monkeypatch.setattr(provider, "transcript_cursor", cursor)
    monkeypatch.setattr(provider, "tail_transcript", tail)
    monkeypatch.setattr(provider, "last_event", last_event)
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: None)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            self.query_params = {}

        async def is_disconnected(self):
            return False

    def topic(event):
        return event.split("event: ", 1)[1].split("\n", 1)[0]

    async def until(gen, name):
        for _ in range(20):
            event = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            if topic(event) == name:
                return event
        pytest.fail(f"no {name} event")

    first = _session_stream(FakeRequest(app), key)
    second = _session_stream(FakeRequest(app), key)
    try:
        # Both tabs primed: each has pushed its Needs you list, the last prime.
        await until(first, "needs_you")
        await until(second, "needs_you")
        appended.append(TranscriptEvent(seq=1, role="assistant", text="hello from the tail"))
        state = app.state.app_state
        with work_counter.counting() as spent:
            state.update_session(replace(state.sessions[key], title="Renamed once"))
            got = [await until(first, "transcript"), await until(second, "transcript")]
            statuses = [await until(first, "status"), await until(second, "status")]
        assert all("hello from the tail" in event for event in got)
        assert got[0] == got[1]
        assert all("Renamed once" in event for event in statuses)
        assert spent[work_counter.CHAT_TAIL] == 1
        # The status fragment moved; built once between the two tabs.
        assert spent[work_counter.CHAT_FRAGMENT] == 1
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_tab_closing_mid_read_does_not_cancel_the_other_tabs_read():
    import asyncio

    from agentdeck.web.chat_renders import ChatRenders

    renders = ChatRenders()
    gate = asyncio.Event()
    calls = {"n": 0}

    async def read():
        calls["n"] += 1
        await gate.wait()
        return "events"

    closing = asyncio.create_task(renders.tail(("leg", 0, 0, 0, (1,)), read))
    staying = asyncio.create_task(renders.tail(("leg", 0, 0, 0, (1,)), read))
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.wait_for(staying, 2.0) == "events"
    assert calls["n"] == 1
    # Kept for the next tab at the same cursor and revision.
    assert await renders.tail(("leg", 0, 0, 0, (1,)), read) == "events"
    assert calls["n"] == 1


async def test_two_tabs_on_one_chat_share_one_replay_at_an_account_switch(tmp_path):
    """An account switch moves every open tab on the chat at once (#2865). The leg being
    left is drained and the new leg replayed once between them, not once per tab."""
    from test_web import _switching_chat_app

    from agentdeck.web.routes_sse import _session_stream

    app, _chat, old_key, new_key, write = _switching_chat_app(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application
            self.query_params = {}

        async def is_disconnected(self):
            return False

    def topic(event):
        return event.split("event: ", 1)[1].split("\n", 1)[0]

    async def until(gen, name):
        for _ in range(30):
            event = await gen.__anext__()
            if topic(event) == name:
                return event
        pytest.fail(f"no {name} event")

    first = _session_stream(FakeRequest(app), old_key)
    second = _session_stream(FakeRequest(app), old_key)
    try:
        await until(first, "needs_you")
        await until(second, "needs_you")
        write("alt", "sid2", [("user", "asked on alt"), ("assistant", "answered on alt")])
        app.state.budget_db.append_chat_leg(old_key, new_key)
        with work_counter.counting() as spent:
            app.state.app_state.sessions_changed((new_key,))
            got = [await until(first, "transcript"), await until(second, "transcript")]
        assert all("asked on alt" in event and "leg-switch" in event for event in got)
        assert got[0] == got[1]
        assert spent[work_counter.CHAT_CATCH_UP] == 1
        # The drain of the old leg, shared too.
        assert spent[work_counter.CHAT_TAIL] == 1
    finally:
        await first.aclose()
        await second.aclose()


class _EventsRequest:
    """``/events?view=…`` for ``routes_sse._stream``."""

    def __init__(self, app, **query):
        import httpx

        self.app = app
        self.query_params = query
        self.headers = {}
        self.cookies = {}
        self.url = httpx.URL("http://deck/events", params=query)

    async def is_disconnected(self):
        return False


async def _until(stream, prefix: str) -> str:
    import asyncio

    chunks = []
    while not any(chunk.startswith(prefix) for chunk in chunks):
        chunks.append(await asyncio.wait_for(anext(stream), 5))
    return "".join(chunks)


async def test_one_work_change_renders_its_row_once_for_every_tab(tmp_path):
    """#3228: a pass that changed one Work of many renders that one row, once for every tab,
    and each tab is sent only that row: the others go as placeholders it already holds."""
    import agentdeck.web.routes_sse as sse

    app = _app(tmp_path, board=_board())
    # Both working, so both are listed rows.
    _add(app, "claude_code:test:mine", WORK, thinking=True)
    _add(app, "claude_code:test:theirs", OTHER, thinking=True)
    await app.state.work_overview.refresh()
    tabs = [sse._stream(_EventsRequest(app, view="works")) for _ in range(2)]
    # A page that shows no Works neither receives them nor keeps the overview rebuilding.
    settings = sse._stream(_EventsRequest(app))
    for tab in tabs:
        primed = await _until(tab, "event: works-")
        for work in (WORK, OTHER):
            assert f'data-work-id="{work.work_id}"' in primed
        assert "data-work-same" not in primed  # a new connection gets every row
    await _until(settings, "event: usage")
    assert app.state.app_state.bus.subscribers("works") == 2

    with work_counter.counting() as spent:
        _add(app, "claude_code:test:mine", WORK, thinking=True, title="Renamed")
        await app.state.work_overview.refresh()
        pushed = [await _until(tab, "event: works-") for tab in tabs]
    assert spent[work_counter.WORK_ROW] == 1
    for chunk in pushed:
        # The changed row in full, the other as its placeholder.
        assert '<article class="fr fr-' in chunk and f'data-work-id="{WORK.work_id}"' in chunk
        assert f'data-work-same data-work-id="{WORK.work_id}"' not in chunk
        assert f'data-work-same data-work-id="{OTHER.work_id}"' in chunk
    # A tab that reconnects names the rows it shows (``?sig=``): only what moved comes whole.
    from agentdeck.web.work_view import row_sig

    overview = app.state.work_overview.overview
    held = {row.card.work_id: row.revision for row in overview.front.active}
    held.update({row.card.work_id: row.revision for group in overview.front.groups
                 for row in group.rows})
    sig = row_sig(OTHER.work_id, held[OTHER.work_id])
    back = sse._stream(_EventsRequest(app, view="works", sig=sig))
    primed = await _until(back, "event: works-")
    assert f'data-work-same data-work-id="{OTHER.work_id}"' in primed
    assert f'data-work-same data-work-id="{WORK.work_id}"' not in primed
    for stream in (*tabs, settings, back):
        await stream.aclose()


async def test_a_pass_asks_the_router_once_per_repository(tmp_path):
    """#3228: whether the deck runs a repository's Works re-reads every poller manifest's
    stat, and a pass asked it once or twice per Work. It is asked once per repository."""
    from types import SimpleNamespace

    app = _app(tmp_path, board=_board())
    _add(app, "claude_code:test:mine", WORK)
    _add(app, "claude_code:test:theirs", OTHER)
    asked: list[str] = []
    app.state.work_router = SimpleNamespace(switched=lambda repository: asked.append(repository))
    await _overview(app)
    assert asked == [WORK.repository]  # both Works are in one repository
