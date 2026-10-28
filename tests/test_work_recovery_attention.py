"""Needs you means a person, never a chat AgentDeck is still recovering by itself (#2235).

A stopped chat whose provider retry is still pending is ``recovering`` (#2928): it is not a
chat for a person to look at. Only a question in the chat's text is answered by a message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agentdeck.models import Session, SessionStatus
from agentdeck.quota_retry import message_for
from agentdeck.web.work_view import _attention
from agentdeck.work import WorkRef
from agentdeck.work_overview import WorkAttention, build_work_card
from agentdeck.work_story import CHAT_STOPPED, NEEDS_YOU, front_row, work_status

WORK = WorkRef("eerovil/agentdeck", 5)


def _work(*, attention=()):
    return build_work_card(WORK, issue=None, title="Work", attention=attention)


def test_a_chat_that_stopped_needs_a_look():
    manual = WorkAttention("claude_code:main:manual", "Manual", "stalled", "/chats/m")
    work = _work(attention=(manual,))

    assert work_status(work).kind == CHAT_STOPPED
    assert front_row(work, current=True).section == NEEDS_YOU


def test_a_side_chat_waiting_for_its_quota_reset_is_not_needs_you():
    item = WorkAttention(
        "claude_code:main:side", "Side chat", "waiting", "/chats/side",
        recovering=True, cause="turn_failed",
    )
    work = _work(attention=(item,))

    assert work_status(work).kind != CHAT_STOPPED
    assert front_row(work, current=True).section != NEEDS_YOU


def test_a_pending_provider_retry_marks_the_chat_as_recovering():
    key = "claude_code:main:side"
    session = Session(
        key=key,
        account_key="claude_code:main",
        session_id="side",
        status=SessionStatus.LIVE,
        turn_failed=True,
        automatic_retry_at=datetime(2026, 10, 12, 17, 50, tzinfo=UTC),
    )
    retry = message_for(session)
    assert retry is not None
    state = SimpleNamespace(sessions={key: session})
    app = SimpleNamespace(state=SimpleNamespace(app_state=state, assistant=None))
    insight = SimpleNamespace(session_key=key, kind="waiting", cause="turn_failed")

    (item,) = _attention(app, (insight,), None, {}, {retry.id})

    assert item.recovering is True


def test_a_quota_retry_that_is_no_longer_pending_needs_attention():
    key = "claude_code:main:side"
    session = Session(
        key=key,
        account_key="claude_code:main",
        session_id="side",
        status=SessionStatus.LIVE,
        turn_failed=True,
        automatic_retry_at=datetime(2026, 10, 12, 17, 50, tzinfo=UTC),
    )
    retry = message_for(session)
    assert retry is not None
    state = SimpleNamespace(sessions={key: session})
    app = SimpleNamespace(state=SimpleNamespace(app_state=state, assistant=None))
    insight = SimpleNamespace(session_key=key, kind="waiting", cause="turn_failed")

    (item,) = _attention(app, (insight,), None, {}, frozenset())

    assert item.recovering is False


@pytest.mark.parametrize(
    ("interaction", "action"), [("", "Answer"), ("question", "Open"), ("mcp_form", "Open")]
)
def test_the_front_page_answers_only_what_a_message_answers(interaction, action):
    ask = WorkAttention(
        "claude_code:main:worker", "Worker", "waiting", "/chats/w",
        asks=True, question="Which branch?", interaction=interaction,
    )
    row = front_row(_work(attention=(ask,)), current=True)

    assert (row.section, row.action) == (NEEDS_YOU, action)
