"""Chat-level transcript event identity across a chat's legs (#1265)."""

import pytest

from agentdeck.chat_transcript import chat_event_id, parse_chat_event_id


def test_first_leg_keeps_the_bare_seq_every_old_link_already_says():
    # Every chat that existed before chaining is leg 0, so its anchors, pins and
    # "load earlier" cursors must render exactly as they always did.
    assert chat_event_id(0, 1) == "1"
    assert chat_event_id(0, 4127) == "4127"
    assert parse_chat_event_id("4127") == (0, 4127)


def test_colliding_native_seqs_get_different_chat_identities():
    # Both legs of a chained chat routinely contain native event 1.
    assert chat_event_id(0, 1) != chat_event_id(1, 1)
    assert parse_chat_event_id(chat_event_id(0, 1)) == (0, 1)
    assert parse_chat_event_id(chat_event_id(1, 1)) == (1, 1)
    assert parse_chat_event_id(chat_event_id(2, 1)) == (2, 1)


def test_identity_carries_the_leg_and_the_native_seq_a_provider_read_needs():
    for leg_index in range(4):
        for seq in (0, 1, 2, 99, 100000):
            assert parse_chat_event_id(chat_event_id(leg_index, seq)) == (
                leg_index,
                seq,
            )


def test_appending_a_leg_cannot_renumber_an_existing_identity():
    # Leg positions are assigned on append and never move, so an identity minted
    # while the chat had one leg still means the same event after two more.
    before = [chat_event_id(0, seq) for seq in range(1, 6)]
    after_two_more_legs = [chat_event_id(0, seq) for seq in range(1, 6)]
    assert before == after_two_more_legs
    assert chat_event_id(1, 3) not in before
    assert chat_event_id(2, 3) not in before


@pytest.mark.parametrize(
    "value", [None, "", "   ", "abc", "1.2.3", "-1.4", "1.-4", "x.1", "1.x"]
)
def test_an_unreadable_cursor_reads_as_nothing_rather_than_raising(value):
    # These arrive from URLs and from hand-editable id attributes.
    assert parse_chat_event_id(value) is None


def test_a_negative_leg_index_is_a_programming_error_not_an_identity():
    with pytest.raises(ValueError):
        chat_event_id(-1, 1)
