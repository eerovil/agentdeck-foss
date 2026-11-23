"""The 🔊 reply a Claude worker is writing, built from its partial-message lines (#4543)."""

from __future__ import annotations

from agentdeck.providers.claude_code.drafts import DraftBuffer, whole_sentences


def _stream(inner: dict, **extra) -> dict:
    return {"type": "stream_event", "event": inner, **extra}


def _start(message_id: str = "msg_1") -> dict:
    return _stream({"type": "message_start", "message": {"id": message_id}})


def _block(index: int = 0, kind: str = "text") -> dict:
    return _stream({"type": "content_block_start", "index": index, "content_block": {"type": kind}})


def _piece(text: str, index: int = 0) -> dict:
    delta = {"type": "text_delta", "text": text}
    return _stream({"type": "content_block_delta", "index": index, "delta": delta})


def _stop(index: int = 0) -> dict:
    return _stream({"type": "content_block_stop", "index": index})


def _end(reason: str) -> dict:
    return _stream({"type": "message_delta", "delta": {"stop_reason": reason}})


def _feed(buffer: DraftBuffer, *events: dict) -> list[bool]:
    return [buffer.take(event) for event in events]


def test_whole_sentences_cut_at_the_last_finished_one():
    assert whole_sentences("") == ""
    assert whole_sentences("On it") == ""
    assert whole_sentences("On it.") == ""
    assert whole_sentences("On it. Check") == "On it."
    assert whole_sentences("Done! Now? Next") == "Done! Now?"
    assert whole_sentences("A line\nmore") == "A line\n"


def test_the_turns_first_marked_block_streams_its_whole_sentences():
    buffer = DraftBuffer()
    _feed(buffer, _start(), _block())
    # Nothing to read before the block says whether it is marked.
    assert buffer.view() is None
    # Marked, but nothing whole to read yet.
    _feed(buffer, _piece("🔊 On it, checking"))
    assert buffer.view() is None
    _feed(buffer, _piece(" the logs. Then the"))
    assert buffer.view() == {
        "message_id": "msg_1",
        "block": 0,
        "text": "🔊 On it, checking the logs.",
        "complete": False,
        "stop_reason": "",
        "voice": "first",
    }
    _feed(buffer, _piece(" docs."), _stop())
    view = buffer.view()
    assert view["text"] == "🔊 On it, checking the logs. Then the docs."
    assert view["complete"] is True
    # The end reason comes after the block, and the draft is still there for it.
    _feed(buffer, _end("tool_use"), _stream({"type": "message_stop"}))
    assert buffer.view()["stop_reason"] == "tool_use"


def test_later_blocks_and_a_new_turn():
    buffer = DraftBuffer()
    _feed(buffer, _start("m1"), _block(), _piece("🔊 First note."), _stop(), _end("tool_use"))
    assert buffer.view()["voice"] == "first"
    # The next message's start replaces the draft; its marked block is a later one.
    _feed(buffer, _start("m2"), _block(1), _piece("🔊 Done. ", 1))
    assert buffer.view()["voice"] == "later"
    assert buffer.view()["message_id"] == "m2"
    # The finished line ends the block; the end reason still finds the draft after it.
    _feed(
        buffer,
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text"}]}},
        _end("end_turn"),
        _stream({"type": "message_stop"}),
    )
    view = buffer.view()
    assert view["complete"] is True and view["stop_reason"] == "end_turn"
    assert view["text"] == "🔊 Done. "
    # The turn's result starts the count again.
    _feed(buffer, {"type": "result"}, _start("m3"), _block(), _piece("🔊 Again. "))
    assert buffer.view()["voice"] == "first"


def test_an_unmarked_block_is_not_shown_and_does_not_count():
    buffer = DraftBuffer()
    _feed(buffer, _start(), _block(), _piece("Let me look. "))
    assert buffer.view() is None
    _feed(buffer, _start("m2"), _block(), _piece("🔊 Found it. "))
    assert buffer.view()["voice"] == "first"


def test_thinking_helpers_and_pieces_without_a_start_make_no_draft():
    # A runtime that restarted reads the replay from mid-message: no start, no draft.
    buffer = DraftBuffer()
    assert _feed(buffer, _piece("🔊 Half a reply. "), _stop()) == [False, False]
    assert buffer.view() is None

    buffer = DraftBuffer()
    _feed(buffer, _start(), _block(0, "thinking"), _piece("🔊 thinking. ", 0))
    assert buffer.view() is None

    buffer = DraftBuffer()
    helper = {"parent_tool_use_id": "toolu_1"}
    assert _feed(buffer, _start() | helper, _block() | helper, _piece("🔊 Helper. ") | helper) == [
        False,
        False,
        False,
    ]
    assert buffer.view() is None


def test_a_process_that_did_not_see_the_turn_start_marks_it_unknown():
    buffer = DraftBuffer(turn_seen=False)
    _feed(buffer, _start(), _block(), _piece("🔊 Mid-turn. "))
    assert buffer.view()["voice"] == "unknown"
    _feed(buffer, {"type": "result"}, _start("m2"), _block(), _piece("🔊 Next turn. "))
    assert buffer.view()["voice"] == "first"
