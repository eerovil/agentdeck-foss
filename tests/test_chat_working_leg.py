"""The leg a chat is on is the one that is running, not the one added last (#1501).

Chat identity assumed succession is permanent: a chat leaves one account for
another and never comes back, so the leg appended last is the leg that is
working. It is not. The poller picks an account by headroom and by affinity and
hands a card back to an account it used hours earlier, and the process it left
behind was never killed. Live example, chat ``67c511dc…``: leg 0 on codex was
still committing and running the suite at 16:50, leg 1 on claude had done
nothing since 11:19, and every control on the chat page addressed leg 1.

So a chat whose recorded order and activity order disagree is the case under
test here — the deck's answer to "which leg is this chat on" has to be the same
one for the poller, the web layer and the transcript, only one leg may claim to
be live, and a control write from the chat URL has to reach the leg that is
actually working.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.chat_identity import LegStanding, working_leg
from agentdeck.chat_transcript import load_chat_transcript
from agentdeck.config import (
    AccountConfig,
    AppConfig,
    HistoryConfig,
    InjectConfig,
    RestartNudgeConfig,
)
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
)

pytestmark = pytest.mark.anyio

# Leg 0 was appended first and is the one still working; leg 1 was appended
# after it and has been quiet for hours. This is the live shape from #1501.
WORKING_KEY = "claude_code:main:leg-one"
QUIET_KEY = "claude_code:alt:leg-two"

NOW = datetime(2026, 9, 4, 17, 0, tzinfo=UTC)
BUSY = NOW - timedelta(minutes=10)
STALE = NOW - timedelta(hours=6)
# The successor answered after the leg it was moved from last spoke (#1914).
MOVED = NOW - timedelta(minutes=5)


# --- the rule itself ------------------------------------------------------


def test_the_working_leg_is_the_live_one_however_late_it_was_appended():
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY),
        LegStanding(QUIET_KEY, live=False, activity=STALE),
    )
    assert working_leg(standings).key == WORKING_KEY


def test_two_live_legs_are_decided_by_activity_not_by_position():
    # Both provider processes really are alive; only one is the conversation.
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY),
        LegStanding(QUIET_KEY, live=True, activity=STALE),
    )
    assert working_leg(standings).key == WORKING_KEY


def test_a_chat_at_rest_still_falls_back_to_the_end_of_the_chain():
    # Nothing live is the ordinary case for every finished chat, and the answer
    # there is the rule every caller followed before this issue.
    standings = (
        LegStanding(WORKING_KEY, live=False, activity=BUSY),
        LegStanding(QUIET_KEY, live=False, activity=STALE),
    )
    assert working_leg(standings).key == QUIET_KEY
    assert working_leg(()) is None


def test_a_failed_live_predecessor_cannot_reclaim_a_finished_successor():
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY, failed=True),
        LegStanding(QUIET_KEY, live=False, activity=STALE),
    )
    assert working_leg(standings).key == QUIET_KEY


def test_a_failed_unstarted_successor_does_not_hide_an_earlier_working_leg():
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY),
        LegStanding(QUIET_KEY, live=True, activity=None, failed=True),
    )
    assert working_leg(standings).key == WORKING_KEY


def test_a_live_leg_that_has_never_been_seen_working_still_beats_a_dead_one():
    # A leg just started has no activity stamp yet. It is where the chat is.
    standings = (
        LegStanding(WORKING_KEY, live=False, activity=BUSY),
        LegStanding(QUIET_KEY, live=True, activity=None),
    )
    assert working_leg(standings).key == QUIET_KEY


def test_the_leg_a_chat_moved_from_cannot_reclaim_it_by_staying_live():
    # #1914. "Live" is not one thing: a deck-owned Claude worker is live while
    # its process is up, a Codex thread only while a turn runs. So the leg a
    # chat has just moved to goes idle the moment it answers, and filtering to
    # the live legs handed the chat back to the one the deck had already told
    # to stop working. The successor spoke last; it is where the chat is.
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY),
        LegStanding(QUIET_KEY, live=False, activity=MOVED),
    )
    assert working_leg(standings).key == QUIET_KEY


def test_a_leg_nothing_has_happened_on_yet_is_taken_as_the_successor():
    # The link lands before the scan that installs the session, so the chain's
    # own end is trusted rather than the predecessor that is still resolvable.
    standings = (
        LegStanding(WORKING_KEY, live=True, activity=BUSY),
        LegStanding(QUIET_KEY, live=False, activity=None),
    )
    assert working_leg(standings).key == QUIET_KEY


# --- the deck ------------------------------------------------------------


def _chat_app(tmp_path, *, quiet_status=SessionStatus.IDLE, quiet_activity=STALE):
    """A two-leg chat whose recorded order and activity order disagree."""
    config = AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        inject=InjectConfig(enabled=True, max_message_chars=200),
        restart_nudge=RestartNudgeConfig(file=str(tmp_path / "restart-nudge.json")),
        accounts=[
            AccountConfig(
                provider="claude_code", label=label, config_dir=str(tmp_path / label)
            )
            for label in ("main", "alt")
        ],
    )
    app = create_app(config)
    state = app.state.app_state
    app.state.budget_db.ensure_session_chats((WORKING_KEY,))
    for key, label, status, activity in (
        (WORKING_KEY, "main", SessionStatus.LIVE, BUSY),
        (QUIET_KEY, "alt", quiet_status, quiet_activity),
    ):
        state.update_session(
            Session(
                key=key,
                account_key=f"claude_code:{label}",
                session_id=key.rsplit(":", 1)[-1],
                status=status,
                title="Chained chat",
                cwd=tmp_path,
                last_activity=activity,
                capabilities=frozenset(
                    {Capability.TRANSCRIPT, Capability.INJECT, Capability.INTERACT}
                ),
            )
        )
    chat = app.state.budget_db.append_chat_leg(WORKING_KEY, QUIET_KEY)
    # Re-admit both legs now the chain exists, the way a scan would.
    for key in chat.legs:
        state.update_session(state.sessions[key])
    return app, chat


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_deck_reports_the_working_leg_as_the_chats_current_one(tmp_path):
    app, chat = _chat_app(tmp_path)
    state = app.state.app_state

    # One answer, whichever way it is asked — this is what the poller reads
    # through the published chat URL and what every control write resolves.
    assert state.chat_current_session_key(QUIET_KEY) == WORKING_KEY
    assert state.current_session_for_chat(chat).key == WORKING_KEY
    assert (
        state.current_leg_for_published_url(f"http://deck/chats/{chat.chat_id}")
        == WORKING_KEY
    )
    row = state.session_presentation().top_level[0]
    assert row.chat_account_keys == ("claude_code:main", "claude_code:alt")
    async with _client(app) as client:
        session_list = await client.get("/chats")
    assert "main▸alt" in session_list.text


async def test_a_reply_from_the_chat_url_reaches_the_leg_that_is_working(tmp_path):
    """The headline case: the control must not write into the idle leg."""
    app, chat = _chat_app(tmp_path)
    delivered = []

    async def start(account, session, provider, message, images=None, **kwargs):
        delivered.append((account.key, session.key, message))
        return InjectResult(True)

    app.state.injector.start = start

    async with _client(app) as client:
        # Addressed through the leg the page used to treat as current.
        accepted = await client.post(
            f"/sessions/{QUIET_KEY}/inject",
            data={"message": "how is the suite going?"},
            headers={"origin": "http://test"},
        )

    assert accepted.status_code == 202
    assert delivered == [("claude_code:main", WORKING_KEY, "how is the suite going?")]


async def test_the_next_message_after_an_account_change_reaches_the_new_leg(tmp_path):
    """#1914, end to end: the chat moved, answered there, and went quiet.

    The leg it left is a deck-owned worker whose process is still up, so it is
    still "live" and its last words are the deck's own "do no more work in this
    chat". The next thing the person types has to reach the leg that answered.
    """
    app, _chat = _chat_app(tmp_path, quiet_activity=MOVED)
    state = app.state.app_state
    # Its own scan reported the worker process up; the chat has moved on, so
    # the deck no longer shows it as the leg that is working.
    assert state.sessions[WORKING_KEY].status is SessionStatus.IDLE
    assert state.chat_reachable_leg_key(WORKING_KEY) == QUIET_KEY
    delivered = []

    async def start(account, session, provider, message, images=None, **kwargs):
        delivered.append((account.key, session.key, message))
        return InjectResult(True)

    app.state.injector.start = start

    async with _client(app) as client:
        # Typed into the page of the leg the chat was moved away from.
        accepted = await client.post(
            f"/sessions/{WORKING_KEY}/inject",
            data={"message": "look at the last issue"},
            headers={"origin": "http://test"},
        )

    assert accepted.status_code == 202
    assert delivered == [("claude_code:alt", QUIET_KEY, "look at the last issue")]


async def test_one_chat_never_reports_two_live_legs(tmp_path):
    # Both processes exist. Only the one the chat is on may say so, or no view
    # can answer "which leg is working" from status alone.
    app, _chat = _chat_app(tmp_path, quiet_status=SessionStatus.LIVE)
    state = app.state.app_state

    assert state.sessions[WORKING_KEY].status is SessionStatus.LIVE
    assert state.sessions[QUIET_KEY].status is SessionStatus.IDLE

    async with _client(app) as client:
        payload = (await client.get("/api/sessions")).json()["sessions"]
    by_key = {row["key"]: row["status"] for row in payload}
    assert by_key[WORKING_KEY] == "live"
    assert by_key[QUIET_KEY] == "idle"


async def test_a_single_leg_chat_keeps_the_status_its_own_scan_reported(tmp_path):
    # The rule is about choosing between legs; a chat with one leg has nothing
    # to choose and must not be demoted by it.
    app, _chat = _chat_app(tmp_path)
    state = app.state.app_state
    solo = "claude_code:main:solo"
    state.update_session(
        Session(
            key=solo,
            account_key="claude_code:main",
            session_id="solo",
            status=SessionStatus.LIVE,
            cwd=tmp_path,
            last_activity=STALE,
        )
    )
    assert state.sessions[solo].status is SessionStatus.LIVE


async def test_a_chat_with_no_reachable_leg_refuses_a_control_write_clearly(tmp_path):
    app, _chat = _chat_app(tmp_path)
    state = app.state.app_state
    # No leg of the chat can be reached. Refusing is the honest answer, and it
    # must not read as "that session does not exist": the chat is real and the
    # URL is right, the deck just cannot say where it is running.
    state.sessions.pop(WORKING_KEY)
    state.sessions.pop(QUIET_KEY)

    async with _client(app) as client:
        refused = await client.post(
            f"/sessions/{WORKING_KEY}/inject",
            data={"message": "anyone there?"},
            headers={"origin": "http://test"},
        )

    assert refused.status_code == 404
    assert "no leg the deck can reach" in refused.json()["detail"]


# --- the transcript -------------------------------------------------------


def _detail(text: str) -> TranscriptDetail:
    return TranscriptDetail(
        events=[TranscriptEvent(seq=1, role="assistant", text=text)],
        tokens=TokenTotals(),
        model=None,
        todos=[],
        total_events=1,
        earliest_seq=1,
    )


class _Provider:
    async def load_transcript(self, account, session, **kwargs):
        return _detail(f"from {session.key}")


async def test_the_transcript_reads_up_to_the_working_leg_not_past_it(tmp_path):
    """The quiet leg is history above the working one, and keeps its position.

    Leg indices are what pins, anchors and "load earlier" cursors were minted
    from, so reading order may move a leg on screen but must never renumber it.
    """
    app, _chat = _chat_app(tmp_path)
    state = app.state.app_state
    accounts = {
        f"claude_code:{label}": Account(
            key=f"claude_code:{label}",
            provider_id="claude_code",
            label=label,
            root=tmp_path / label,
        )
        for label in ("main", "alt")
    }

    transcript = await load_chat_transcript(
        chat_keys=state.chat_session_keys(QUIET_KEY),
        current_session_key=state.chat_current_session_key(QUIET_KEY),
        sessions=state.sessions,
        accounts_by_key=accounts,
        providers={"claude_code": _Provider()},
        account_labels={key: key.split(":")[-1] for key in accounts},
        window=50,
    )

    assert [leg.session_key for leg in transcript.legs] == [QUIET_KEY, WORKING_KEY]
    assert transcript.current.session_key == WORKING_KEY
    # Appended positions, untouched by the reordering above.
    assert [leg.index for leg in transcript.legs] == [1, 0]
    # The seam names the move the reader actually made.
    assert transcript.legs[0].switch is not None
    assert transcript.legs[0].switch.to_index == 0
    assert transcript.legs[1].switch is None
