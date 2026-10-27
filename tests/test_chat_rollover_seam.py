"""A rollover reads as a rollover at the leg boundary it made (#1667).

Replacing the provider session is the only context maintenance a Claude chat has,
and it leaves the same trace in the chain as a move to another account: a new leg.
The seam between them was labelled "this chat continued on another account", which
for a rollover is not what happened — the account never changed, the session did.

The frozen `context_deliveries` row is the only durable record that tells the two
apart, so the seam reads its `reason` and says so.
"""

from __future__ import annotations

from agentdeck.chat_transcript import LegSwitch
from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db
from agentdeck.state import AppState


def _row(**overrides) -> ContextDelivery:
    base = dict(
        action_id="act-1",
        attempt=1,
        provider_id="claude_code",
        account_key="claude_code:main",
        worker_key="worker-1",
        delivery_id="del-1",
        request_sha256="a" * 64,
        chat_id="chat-1",
        source_session_key="claude_code:main:sid1",
        source_session_id="sid1",
        mode="enforce",
        proposed_action="rollover",
        executed_action="rollover",
        reason="threshold",
        target_session_key="claude_code:main:sid2",
    )
    base.update(overrides)
    return ContextDelivery(**base)


def test_only_an_executed_rollover_with_a_successor_names_a_seam(tmp_path):
    db = Db(tmp_path / "deck.sqlite3")
    state = AppState(db=db)
    for row in (
        _row(),
        # A compact is maintenance too, but it makes no leg, so it names no seam.
        _row(action_id="act-2", delivery_id="del-2", executed_action="compact",
             reason="threshold", target_session_key="claude_code:main:sid3"),
        # A rollover that never reached a successor has no boundary to label.
        _row(action_id="act-3", delivery_id="del-3", target_session_key=None),
        # Another chat's rollover must not leak into this one.
        _row(action_id="act-4", delivery_id="del-4", chat_id="chat-2",
             target_session_key="claude_code:main:sid9"),
    ):
        assert db.reserve_context_delivery(row) is not None

    # No durable chat identity here, so the chat id is the session key itself.
    assert state.chat_rollover_reasons("chat-1") == {
        "claude_code:main:sid2": "threshold"
    }
    assert state.chat_rollover_reasons("chat-2") == {
        "claude_code:main:sid9": "threshold"
    }


def test_a_deck_with_no_database_simply_has_no_reasons():
    # The seam is a decoration on a page that must still render.
    assert AppState().chat_rollover_reasons("claude_code:main:sid1") == {}


def _switch(reason: str | None) -> LegSwitch:
    return LegSwitch(
        to_index=1,
        from_account_label="main",
        from_provider_id="claude_code",
        to_account_label="main",
        to_provider_id="claude_code",
        reason=reason,
    )


def test_a_seam_with_no_reason_is_still_an_ordinary_account_switch():
    switch = _switch(None)
    assert switch.is_rollover is False


def test_each_recorded_reason_is_said_in_words_a_reader_has_not_had_to_learn():
    for reason, expected in (
        ("threshold", "the context window had filled up"),
        ("new_generation", "new work arrived on a full context window"),
        ("compact_failed", "compacting the context failed"),
        ("compact_insufficient", "compacting the context did not free enough"),
    ):
        words = _switch(reason).rollover_words
        assert expected in words
        assert "context rollover" in words


def test_a_reason_this_deck_has_not_learned_still_says_a_rollover_happened():
    # `context_policy` can grow a reason without this table growing with it.
    words = _switch("some_future_reason").rollover_words
    assert words == "context rollover — the chat continued in a fresh session"


def test_the_deck_chats_fresh_start_says_so_rather_than_rollover():
    # The deck chat starts a clean leg after an idle hour (#3403); nothing filled up.
    switch = _switch("idle_reset")
    assert switch.is_rollover and switch.rollover_label == "fresh start"
    assert "no message for an hour" in switch.rollover_words
    assert "context rollover" not in switch.rollover_words
    assert _switch("threshold").rollover_label == "context rollover"
