"""Messages the deck sent into a chat render as one-line markers (#3200)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

from agentdeck import deck_notices
from agentdeck.db import Db
from agentdeck.models import TranscriptEvent
from agentdeck.scheduled_messages import ScheduledMessage
from agentdeck.web.render import register_filters, render_transcript_events

TS = datetime(2026, 10, 16, 14, 23, tzinfo=UTC)
WAKE = (
    "AgentDeck wake-on: commit status continuous-integration/drone/pr on"
    " northwind/abacus@33b286bcbbd6 finished: success (https://ci.example/6733)."
    " You asked to be woken for CI. Webhooks are a hint, so confirm it with one read."
)


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        (WAKE, "wake-on", "CI passed · drone/pr"),
        (
            "AgentDeck wake-on: pull request northwind/abacus#2120 was merged as"
            " 529d1c4df6d8. You asked to be woken for it.",
            "wake-on",
            "Merged abacus#2120",
        ),
        (
            "AgentDeck wake-on: deployment to prod on northwind/abacus@144ba46ba125"
            " finished: success. You asked to be woken.",
            "wake-on",
            "Deployed to prod",
        ),
        (
            "AgentDeck wake-on: the web worker serving this deck now runs 1234567890ab, which"
            " includes commit abcdef1, and bundled skills are installed at 1234567890ab.",
            "wake-on",
            "Deck now runs 12345678",
        ),
        (
            "AgentDeck: the review of eerovil/x#7 at 56b13fc14b26 (review chat"
            " eerovil/review-chats#5792) passed clean. The deck records the pass.",
            "work-release",
            "Review passed · review-chats#5792",
        ),
        (
            "AgentDeck: store/a#1, store/a#2 merged. Check that it works live now.",
            "work-release",
            "Merged a#1, a#2",
        ),
        (
            "AgentDeck event: eerovil commented on issue eerovil/agentdeck#3186:\n\nhello",
            "work-event",
            "Eerovil commented on agentdeck#3186",
        ),
        (
            "AgentDeck: your last turn on o/r#1 ended without a next step.",
            "work-ending",
            "Turn ended without a next step",
        ),
        (
            "AgentDeck: something new happened to o/r#1. Do a thing.",
            "work-release",
            "Something new happened to o/r#1",
        ),
    ],
)
def test_each_deck_message_gets_a_one_line_label(text, kind, expected):
    assert deck_notices.label(text, kind) == expected


def test_deck_messages_render_as_markers_and_a_persons_own_stay_messages(tmp_path):
    store = Db(tmp_path / "agentdeck.db")
    now = datetime.now(UTC)
    store.record_scheduled_message(
        ScheduledMessage(id="wake-on:abc", chat_id="c", text=WAKE, due_at=TS, created_at=TS)
    )
    store.claim_scheduled_message("wake-on:abc")
    store.settle_scheduled_message("wake-on:abc", "delivered")
    # A person's held send is in the same table, but it is theirs.
    store.record_scheduled_message(
        ScheduledMessage(
            id="action-1",
            chat_id="c",
            text="Continue",
            due_at=TS,
            created_at=TS,
            hold_reason="runtime",
        )
    )
    store.claim_scheduled_message("action-1")
    store.settle_scheduled_message("action-1", "delivered")
    # Without the usual prefix the record alone decides, and only this chat's record.
    for record, chat, text in (
        ("work-release:y", "c", "Looks done"),
        ("work-release:z", "x", "Ship it"),
    ):
        store.record_scheduled_message(
            ScheduledMessage(id=record, chat_id=chat, text=text, due_at=TS, created_at=TS)
        )
        store.claim_scheduled_message(record)
        store.settle_scheduled_message(record, "delivered")
    brief = (
        "Context: Work for eerovil/agentdeck#3200.\n\nThis chat is the Work for this issue."
        " Read the issue, then do what it asks."
    )
    side_chat = "Context: Work for eerovil/agentdeck#3200.\n\nplease look at the CSS"
    events = [
        TranscriptEvent(seq=1, role="user", text=brief, ts=TS),
        TranscriptEvent(seq=2, role="user", text=WAKE, ts=now),
        TranscriptEvent(seq=3, role="user", text="Continue", ts=TS),
        TranscriptEvent(seq=4, role="user", text=side_chat, ts=TS),
        TranscriptEvent(seq=5, role="user", text="Pick one", answer="A", ts=TS),
        TranscriptEvent(seq=6, role="assistant", text="Done.", ts=TS),
        # Records are stamped with the real clock when they are handed over.
        TranscriptEvent(seq=7, role="user", text="Looks done", ts=now),
        TranscriptEvent(seq=8, role="user", text="Ship it", ts=now),
        # The person repeating the deck's words later is still the person.
        TranscriptEvent(seq=9, role="user", text="Looks done", ts=now + timedelta(hours=7)),
    ]
    templates = Jinja2Templates(
        directory=str(Path(__file__).parents[1] / "src/agentdeck/web/templates")
    )
    register_filters(templates)
    html = render_transcript_events(templates, events, session_key="k", store=store, chat_id="c")

    rows = html.split('id="event-')[1:]
    assert 'class="ev deck-notice"' in rows[0] and "Work started" in rows[0]
    assert 'class="ev deck-notice"' in rows[1] and "CI passed · drone/pr" in rows[1]
    # The full text is still there behind the tap.
    assert "Webhooks are a hint" in rows[1]
    for row in rows[2:5]:
        assert "deck-notice" not in row and 'class="ev user' in row
    assert 'class="ev assistant' in rows[5]
    assert 'class="ev deck-notice"' in rows[6] and "Looks done" in rows[6]
    assert "deck-notice" not in rows[7]  # the deck sent this text to another chat
    assert "deck-notice" not in rows[8]  # not the turn the record wrote
    # No store, no records: the text prefix alone still marks the deck's message.
    plain = render_transcript_events(templates, events[1:3], session_key="k")
    assert plain.count("deck-notice-label") == 1
