"""A send that is accepted and then never lands must stop looking in-flight.

Provider acceptance only means the message was handed over. A CLI can take it,
write nothing to its transcript, and go back to idle — a deleted working
directory does it, and so does a prompt the CLI drops. Nothing errors, so the
optimistic row used to sit on "Sent; waiting for transcript" forever.

Two defences, because they cover different ground: refuse the send when the
directory the chat would be woken in is gone (the cause we have actually seen),
and stop claiming a message is in flight once the agent has had a minute to
acknowledge it and has not (every other cause, including ones not yet seen).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck import inject as inject_module
from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.db import Db
from agentdeck.inject import (
    ACKNOWLEDGEMENT_GRACE_S,
    InjectionService,
    QueuedMessage,
)
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    Session,
    SessionStatus,
    TranscriptEvent,
)

pytestmark = pytest.mark.anyio


def _accepted(age_s: float) -> QueuedMessage:
    return QueuedMessage(
        1,
        "hello",
        state="accepted",
        accepted_at=datetime.now(UTC) - timedelta(seconds=age_s),
    )


def test_silence_only_counts_against_a_message_once_the_grace_has_passed():
    assert _accepted(ACKNOWLEDGEMENT_GRACE_S + 1).is_unacknowledged is True
    assert _accepted(ACKNOWLEDGEMENT_GRACE_S - 1).is_unacknowledged is False
    # Still in the queue or mid-handoff: nothing has been claimed yet.
    assert QueuedMessage(1, "hello").is_unacknowledged is False
    assert QueuedMessage(1, "hello", state="running").is_unacknowledged is False


def test_an_unacknowledged_message_stays_pending_so_a_late_transcript_can_claim_it():
    """Derived, not a state transition: a slow worker resolves itself."""
    item = _accepted(ACKNOWLEDGEMENT_GRACE_S + 1)
    assert item.is_pending is True
    assert item.state == "accepted"
    item.state = "observed"
    assert item.is_unacknowledged is False


def _app(tmp_path, *, cwd, thinking=False, capabilities=frozenset()):
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "history.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        accounts=[
            AccountConfig(
                provider="claude_code", label="test", config_dir=str(tmp_path / "claude")
            )
        ],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            "claude_code:test:sid",
            "claude_code:test",
            "sid",
            SessionStatus.IDLE,
            cwd=cwd,
            thinking=thinking,
            capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT})
            | capabilities,
        )
    )
    return app


async def _send(app, text="hello"):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/sessions/claude_code:test:sid/inject",
            data={"message": text},
            headers={"origin": "http://test"},
        )


async def test_a_send_into_a_deleted_working_directory_is_refused_not_swallowed(
    tmp_path,
):
    """The worktree case: the chat cannot be woken there, so say so now."""
    app = _app(tmp_path, cwd=tmp_path / "worktree-that-was-removed")
    sent = []

    async def start(account, session, provider, message, images=None, **kwargs):
        sent.append(message)
        return InjectResult(True)

    app.state.injector.start = start
    refused = await _send(app)

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "no longer exists" in detail
    assert "worktree-that-was-removed" in detail
    # Nothing queued, so there is no optimistic row left waiting on a transcript
    # event that was never going to arrive.
    assert sent == []


async def test_a_deleted_worktree_chat_its_runtime_can_move_sends_to_the_same_chat(
    tmp_path,
):
    """#2524: the runtime resumes the chat in its repository, so the send goes through."""
    repository = tmp_path / "project"
    (repository / ".git").mkdir(parents=True)
    app = _app(
        tmp_path,
        cwd=repository / ".worktrees" / "issue-7",
        capabilities=frozenset({Capability.RESUME_ELSEWHERE}),
    )
    sent = []

    async def start(account, session, provider, message, images=None, **kwargs):
        sent.append((session.key, message))
        return InjectResult(True)

    app.state.injector.start = start
    response = await _send(app)

    assert response.status_code == 202, response.text
    assert sent == [("claude_code:test:sid", "hello")]


async def test_a_movable_chat_with_no_repository_above_it_is_still_refused(
    tmp_path, monkeypatch
):
    from agentdeck import chat_cwd

    # The test host may place pytest below a repository checkout; make the
    # no-repository case explicit.
    monkeypatch.setattr(chat_cwd, "continued_chat_cwd", lambda _cwd: None)
    app = _app(
        tmp_path,
        cwd=tmp_path / "worktree-that-was-removed",
        capabilities=frozenset({Capability.RESUME_ELSEWHERE}),
    )
    sent = []

    async def start(account, session, provider, message, images=None, **kwargs):
        sent.append(message)
        return InjectResult(True)

    app.state.injector.start = start
    refused = await _send(app)

    assert refused.status_code == 409
    assert "Continue in new chat" in refused.json()["detail"]
    assert sent == []


async def test_a_chat_the_deck_does_not_run_says_why_once_its_directory_is_gone(
    tmp_path,
):
    """No INJECT and no directory: the reason, not a bare "unavailable" (#2524)."""
    app = _app(tmp_path, cwd=tmp_path / "gone")
    session = app.state.app_state.sessions["claude_code:test:sid"]
    app.state.app_state.update_session(
        replace(session, capabilities=frozenset({Capability.TRANSCRIPT}))
    )
    refused = await _send(app)

    assert refused.status_code == 409
    assert "no longer exists" in refused.json()["detail"]


async def test_a_chat_whose_directory_is_there_still_sends(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)

    async def start(account, session, provider, message, images=None, **kwargs):
        return InjectResult(True)

    app.state.injector.start = start
    assert (await _send(app)).status_code == 202


async def test_a_running_turn_is_not_refused_over_its_directory(tmp_path):
    """Its process is already up; a stale idea of the cwd must not cost a message."""
    app = _app(tmp_path, cwd=tmp_path / "gone", thinking=True)

    async def start(account, session, provider, message, images=None, **kwargs):
        return InjectResult(True)

    app.state.injector.start = start
    assert (await _send(app)).status_code == 202


async def test_the_row_stops_claiming_a_message_is_in_flight(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    key = "claude_code:test:sid"
    # The real handoff, so the accepted stamp is the one production sets.
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))
    assert item.state == "accepted"
    assert item.accepted_at is not None

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        item.accepted_at = datetime.now(UTC)
        injector._snapshot("claude_code:test:sid")
        fresh = await client.get(
            "/sessions/claude_code:test:sid"
        )

        item.accepted_at = datetime.now(UTC) - timedelta(
            seconds=ACKNOWLEDGEMENT_GRACE_S + 1
        )
        injector._snapshot("claude_code:test:sid")
        stale = await client.get(
            "/sessions/claude_code:test:sid"
        )

    assert "Sent; waiting for transcript" in fresh.text
    assert "never acknowledged" not in fresh.text
    assert "never acknowledged" in stale.text
    assert "Sent; waiting for transcript" not in stale.text
    # Still shown: the person needs to see the message that did not land.
    assert "did you get this" in stale.text


async def test_a_chat_in_a_turn_is_not_accused_of_ignoring_the_message(tmp_path):
    """Mid-turn silence is not evidence, so the row must not call it that (#2016).

    The boss's screenshot was a column of Mac-runner outcomes in a chat that was
    thinking, every one of them stamped "the agent never acknowledged it". The
    agent had not ignored them; it had not reached them yet. The delivery loop
    already refuses to resend into a busy chat for the same reason.
    """
    app = _app(tmp_path, cwd=tmp_path, thinking=True)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(seconds=ACKNOWLEDGEMENT_GRACE_S + 1)
    injector._snapshot(key)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        busy = await client.get(f"/sessions/{key}")

    assert "never acknowledged" not in busy.text
    assert "Sent; waiting for this turn to reach it" in busy.text
    assert "did you get this" in busy.text


class _Transcript:
    """A provider whose transcript is exactly what the test says it wrote."""

    def __init__(self, *, events=(), accept=True):
        self.messages: list[str] = []
        self.events = list(events)
        self.accept = accept

    async def transcript_cursor(self, account, session):
        return 0, 0

    async def inject(self, account, session, message, *, timeout_s, **kwargs):
        self.messages.append(message)
        return InjectResult(self.accept)

    async def tail_transcript(self, account, session, byte_offset, seq):
        events, self.events = self.events, []
        return events, byte_offset, seq


def _impatient(monkeypatch):
    """Run the acknowledgement watch at test speed, not at a minute a message."""
    monkeypatch.setattr(inject_module, "ACKNOWLEDGEMENT_GRACE_S", 0.0)
    monkeypatch.setattr(inject_module, "ACKNOWLEDGEMENT_POLL_S", 0.0)


async def _deliver(tmp_path, provider, *, busy=False, text="hello"):
    service = InjectionService(
        InjectConfig(enabled=True), session_busy=lambda _key: busy
    )
    account = Account("claude_code:test", "claude_code", "test", tmp_path)
    session = Session(
        "claude_code:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
    )
    assert (await service.start(account, session, provider, text)).accepted
    await asyncio.wait_for(service._tasks[session.key], 5)
    return service.status(session.key).items[0]


def _user_event(text):
    """The CLI's own echo of a message the deck is about to send.

    Stamped a second ahead, because the echo is written *after* the send and the
    matcher in ``inject`` will not accept an event older than the item. Stamping
    it with ``now()`` only passed when this line and the item's ``created_at``
    landed in the same millisecond — true on an idle host, false on a loaded
    one, where the test failed as an unacknowledged send.
    """
    return TranscriptEvent(
        seq=1, role="user", text=text, ts=datetime.now(UTC) + timedelta(seconds=1)
    )


async def test_a_send_that_left_no_trace_at_all_is_made_once_more(
    tmp_path, monkeypatch
):
    """The self-healing case: nothing was written, so nothing can be duplicated."""
    _impatient(monkeypatch)
    provider = _Transcript()

    item = await _deliver(tmp_path, provider)

    assert provider.messages == ["hello", "hello"]
    # And it stops there — a message unacknowledged twice is a person's call.
    assert item.attempts == 2
    assert item.state == "accepted"


async def test_a_transcript_that_moved_is_never_resent(tmp_path, monkeypatch):
    """The agent wrote *something*, so what became of the message is uncertain."""
    _impatient(monkeypatch)
    provider = _Transcript(
        events=[TranscriptEvent(seq=1, role="assistant", text="on it")]
    )

    item = await _deliver(tmp_path, provider)

    assert provider.messages == ["hello"]
    assert item.attempts == 1
    assert item.is_unacknowledged is True


async def test_a_session_with_a_turn_in_flight_is_not_resent_into(
    tmp_path, monkeypatch
):
    """A CLI slow to start has written nothing either; a resend arrives twice."""
    _impatient(monkeypatch)
    provider = _Transcript()

    item = await _deliver(tmp_path, provider, busy=True)

    assert provider.messages == ["hello"]
    assert item.attempts == 1


async def test_an_acknowledged_send_is_left_alone(tmp_path, monkeypatch):
    _impatient(monkeypatch)
    provider = _Transcript(events=[_user_event("hello")])

    item = await _deliver(tmp_path, provider)

    assert provider.messages == ["hello"]
    assert item.state == "observed"
    assert item.is_unacknowledged is False


async def test_the_row_says_when_the_deck_is_having_another_go(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    item.attempts = 1
    item.state = "queued"
    injector._snapshot(key)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resending = await client.get(f"/sessions/{key}")

        item.attempts = 2
        item.state = "accepted"
        item.accepted_at = datetime.now(UTC) - timedelta(
            seconds=ACKNOWLEDGEMENT_GRACE_S + 1
        )
        injector._snapshot(key)
        twice = await client.get(f"/sessions/{key}")

    assert "Sending again; the first send left no trace" in resending.text
    assert "Queued behind active turn" not in resending.text
    assert "Sent twice; the agent never acknowledged it" in twice.text


def _dead_row(*, busy=False, key="claude_code:test:sid", client_action_id=None):
    """A service holding one row the agent took and never wrote down."""
    service = InjectionService(
        InjectConfig(enabled=True), session_busy=lambda _key: busy
    )
    item = service._begin_delivery(
        key, "did you get this", client_action_id=client_action_id
    )
    service._finish_delivery(key, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    service._snapshot(key)
    assert item.is_unacknowledged is True
    return service, item, key


def test_a_dead_row_can_be_dismissed():
    """The escape hatch #1451 left to the person: the row goes, nothing else does."""
    service, item, key = _dead_row()

    assert service.discard(key, item.id) is True
    assert service.status(key).items == ()
    # The resend watcher holds its own reference and reads the state to know to
    # stop; a row that is no longer `accepted` is no longer its business.
    assert item.state == "discarded"
    assert item.is_pending is False


def test_dismissing_a_row_twice_changes_nothing_the_second_time():
    service, item, key = _dead_row()

    assert service.discard(key, item.id) is True
    assert service.discard(key, item.id) is False


def test_a_row_still_in_flight_cannot_be_dismissed():
    """Queued or mid-handoff is the delivery loop's, not the person's."""
    service, item, key = _dead_row()
    item.state = "queued"
    service._snapshot(key)

    assert service.discard(key, item.id) is False
    assert service.status(key).items[0].is_pending is True


def test_a_row_inside_the_grace_period_cannot_be_dismissed():
    """Nothing is wrong yet, so there is nothing to clear."""
    service, item, key = _dead_row()
    item.accepted_at = datetime.now(UTC)
    service._snapshot(key)

    assert service.discard(key, item.id) is False


def test_a_row_in_a_chat_mid_turn_cannot_be_dismissed():
    """The turn may still reach it, and the caption says so (#2016)."""
    service, item, key = _dead_row(busy=True)

    assert service.discard(key, item.id) is False
    assert service.status(key).items[0].is_pending is True


async def test_a_retry_behind_a_dismissed_action_does_not_rebuild_the_row(tmp_path):
    """The dismissal has to outlive the row, or the next retry undoes it (#2031).

    The Mac runner re-posts a command's outcome every 30 seconds until the deck
    confirms it landed, and a message the agent never wrote down is never
    confirmed. Until now the only thing holding that retry off was the row itself,
    so dismissing the row handed the retry an empty queue and it built the same
    row again — new id, same text, within half a minute.
    """
    action = "mac-runner:run-1"
    service, item, key = _dead_row(client_action_id=action)

    class Provider:
        async def inject(self, account, session, message, *, timeout_s):
            return InjectResult(True)

    account = Account("claude_code:test", "claude_code", "test", tmp_path)
    session = Session(
        key,
        account.key,
        "sid",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )

    assert service.discard(key, item.id) is True

    retry = await service.start(
        account, session, Provider(), "did you get this", client_action_id=action
    )

    # Accepted, because from the Mac's side the outcome is already with the chat.
    assert retry.accepted is True
    assert service.status(key).items == ()

    # A different outcome is not caught up in it.
    await service.start(
        account, session, Provider(), "another", client_action_id="mac-runner:run-2"
    )
    assert len(service.status(key).items) == 1
    await service.stop()


async def test_a_dismissal_outlives_the_process_its_memory_bound_and_a_rollover(
    tmp_path,
):
    """The decision has to last as long as the caller keeps retrying (#2031).

    A stuck outcome is re-posted every 30 seconds for as long as it goes
    unconfirmed, which is forever. So a dismissal is up against three things at
    once, and this crosses all three: the queue's two-hundred-entry memory bound,
    a web process that restarts on every frontend deploy, and a chat that rolls
    over to another leg — which matters because a Mac callback arriving after a
    rollover is deliberately retargeted to whichever leg is working then, so the
    retry that has to be stopped arrives against a key the row never had.
    """
    db = Db(tmp_path / "control.db")
    action = "mac-runner:run-1"
    shown = "claude_code:test:sid"
    moved_on = "claude_code:test:sid2"

    class Provider:
        def __init__(self):
            self.messages = []

        async def inject(self, account, session, message, *, timeout_s):
            self.messages.append(message)
            return InjectResult(True)

    account = Account("claude_code:test", "claude_code", "test", tmp_path)
    working_leg = Session(
        moved_on,
        account.key,
        "sid2",
        SessionStatus.IDLE,
        capabilities=frozenset({Capability.INJECT}),
    )

    dismissing = InjectionService(
        InjectConfig(enabled=True),
        on_dismissed=db.record_dismissed_delivery,
        was_dismissed=db.delivery_was_dismissed,
    )
    item = dismissing._begin_delivery(
        shown, "did you get this", client_action_id=action
    )
    dismissing._finish_delivery(shown, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    dismissing._snapshot(shown)
    assert dismissing.discard(shown, item.id) is True

    # Two hundred later dismissals push this one out of the in-memory map.
    for index in range(InjectionService._MAX_DISCARDED_ACTIONS):
        dismissing._remember_discarded_action(f"mac-runner:later-{index}", shown)
    assert action not in dismissing._discarded_actions

    # And the web process restarts, which empties the queue entirely.
    restarted = InjectionService(
        InjectConfig(enabled=True),
        on_dismissed=db.record_dismissed_delivery,
        was_dismissed=db.delivery_was_dismissed,
    )
    provider = Provider()
    # The retry arrives against the leg the chat has moved its work to, not the
    # one the dismissed row was drawn on.
    retry = await restarted.start(
        account, working_leg, provider, "did you get this", client_action_id=action
    )

    assert retry.accepted is True
    assert restarted.status(moved_on) is None
    assert restarted.status(shown) is None
    assert provider.messages == []

    # The record is the action's, not the leg's — and it still claims nothing
    # about the message having reached an agent.
    assert db.delivery_was_dismissed(action) is True
    assert db.mac_runner_callback_state("run-1") is None
    await restarted.stop()


async def test_the_dismiss_button_is_offered_only_on_a_dead_row(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        item.accepted_at = datetime.now(UTC)
        injector._snapshot(key)
        fresh = await client.get(f"/sessions/{key}")

        item.accepted_at = datetime.now(UTC) - timedelta(
            seconds=ACKNOWLEDGEMENT_GRACE_S + 1
        )
        injector._snapshot(key)
        dead = await client.get(f"/sessions/{key}")

    assert "data-pending-dismiss" not in fresh.text
    assert "data-pending-dismiss" in dead.text
    assert f"/sessions/{key}/pending/{item.id}/dismiss" in dead.text


async def test_dismissing_a_dead_row_removes_it_from_the_chat(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    injector._snapshot(key)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        dismissed = await client.post(
            f"/sessions/{key}/pending/{item.id}/dismiss",
            headers={"origin": "http://test"},
        )
        after = await client.get(f"/sessions/{key}")

    # An empty answer swaps the row out of the transcript and nothing else.
    assert dismissed.status_code == 200
    assert dismissed.text == ""
    assert "did you get this" not in after.text
    assert "never acknowledged" not in after.text


async def test_a_refused_dismissal_answers_with_the_row_and_its_reason(tmp_path):
    """A press that cannot take says why rather than doing nothing visible."""
    app = _app(tmp_path, cwd=tmp_path, thinking=True)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    injector._snapshot(key)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            f"/sessions/{key}/pending/{item.id}/dismiss",
            headers={"origin": "http://test"},
        )

    assert refused.status_code == 409
    assert refused.headers["content-type"].startswith("text/html")
    assert "Sent; waiting for this turn to reach it" in refused.text
    assert "data-pending-dismiss" not in refused.text
    assert injector.status(key).items[0].is_pending is True


async def test_a_row_that_is_already_gone_answers_as_removed(tmp_path):
    """Two browsers, one row: the second press must still clear the second view."""
    app = _app(tmp_path, cwd=tmp_path)
    key = "claude_code:test:sid"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        gone = await client.post(
            f"/sessions/{key}/pending/4242/dismiss",
            headers={"origin": "http://test"},
        )

    assert gone.status_code == 200
    assert gone.text == ""


async def test_a_dismissal_acts_on_the_leg_the_row_was_drawn_from(tmp_path):
    """A chat that has rolled over must still lose the row it is showing (#2031).

    The page names a leg in the URL and draws its pending rows from that leg's
    queue. Resolving the dismissal to the chat's *working* leg instead looked at a
    queue that had never held the row, found nothing, and answered "removed" — so
    the row went away in the browser and was back on the next render.
    """
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    shown = "claude_code:test:sid"
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
    # The chat has moved its work to the newer leg; the reader is still on the
    # older one, which is where the stuck row lives.
    app.state.app_state.chat_reachable_leg_key = (
        lambda _key: "claude_code:test:sid2"
    )
    item = injector._begin_delivery(shown, "did you get this")
    injector._finish_delivery(shown, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    injector._snapshot(shown)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        removed = await client.post(
            f"/sessions/{shown}/pending/{item.id}/dismiss",
            headers={"origin": "http://test"},
        )

    assert removed.status_code == 200
    assert removed.text == ""
    assert injector.status(shown).items == ()


async def test_a_cross_site_dismissal_is_refused(tmp_path):
    app = _app(tmp_path, cwd=tmp_path)
    injector = app.state.injector
    key = "claude_code:test:sid"
    item = injector._begin_delivery(key, "did you get this")
    injector._finish_delivery(key, item, InjectResult(True))
    item.accepted_at = datetime.now(UTC) - timedelta(
        seconds=ACKNOWLEDGEMENT_GRACE_S + 1
    )
    injector._snapshot(key)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            f"/sessions/{key}/pending/{item.id}/dismiss",
            headers={"sec-fetch-site": "cross-site"},
        )

    assert refused.status_code == 403
    assert injector.status(key).items[0].is_pending is True
