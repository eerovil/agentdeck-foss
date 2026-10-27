"""Tests for the shared incremental Transcript Reader.

The reader is exercised through its own interface with a stub ``LineParser`` so
the machinery (resume, truncation reset, partial-line deferral, seq/skip
accounting, crash containment, dedup, and the two hooks) is verified once,
independent of any provider's wire format.
"""

import json

from agentdeck.models import TranscriptEvent
from agentdeck.providers.transcript_reader import (
    AGENT_TEXT,
    PROMPT,
    RESET,
    LineParser,
    TranscriptReader,
    newest_messages_backwards,
    parse_ts,
    read_line,
    transcript_cursor,
)


class StubParser(LineParser):
    """Maps a control dict straight onto a TranscriptEvent so tests drive shape."""

    def event_from_line(self, seq, obj):
        if obj.get("kind") == "boom":
            raise ValueError("stub parser blew up")
        if obj.get("kind") == "skip":
            return None  # a recognized-but-unrenderable line: skipped without counting
        return TranscriptEvent(
            seq=seq,
            role=obj.get("role", "system"),
            text=obj.get("text"),
            tool_name=obj.get("tool_name"),
            question=obj.get("question"),
            queued=obj.get("queued", False),
        )

    def is_turn_boundary(self, obj):
        return bool(obj.get("boundary"))

    def is_probe_event(self, event):
        return bool(event.text or event.tool_name or event.question)


def _write(path, objs):
    path.write_bytes("".join(json.dumps(o) + "\n" for o in objs).encode())


def _append(path, objs):
    with path.open("ab") as handle:
        handle.write("".join(json.dumps(o) + "\n" for o in objs).encode())


def _reader():
    return TranscriptReader(StubParser())


def test_incremental_resume_matches_single_read(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"role": "user", "text": f"m{i}"} for i in range(3)])
    reader = _reader()
    r1 = reader.read_events(path)
    _append(path, [{"role": "assistant", "text": f"a{i}"} for i in range(2)])
    r2 = reader.read_events(path, byte_offset=r1.byte_offset, seq=r1.seq)
    single = reader.read_events(path)

    assert [e.text for e in r1.events + r2.events] == [e.text for e in single.events]
    assert r2.byte_offset == single.byte_offset == path.stat().st_size
    assert r2.seq == single.seq == 5


def test_offset_past_eof_resets_to_start(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"role": "user", "text": "only"}])
    r = _reader().read_events(path, byte_offset=10_000, seq=99)

    assert [e.text for e in r.events] == ["only"]
    assert r.seq == 1
    assert r.byte_offset == path.stat().st_size


def test_partial_trailing_line_is_deferred_then_consumed(tmp_path):
    path = tmp_path / "t.jsonl"
    complete = json.dumps({"role": "user", "text": "done"}) + "\n"
    partial = json.dumps({"role": "user", "text": "half"})  # no trailing newline
    path.write_bytes((complete + partial).encode())
    reader = _reader()

    r = reader.read_events(path)
    assert [e.text for e in r.events] == ["done"]
    assert r.byte_offset == len(complete.encode())  # stops before the partial line

    path.open("ab").write(b"\n")  # the writer finishes the line
    r2 = reader.read_events(path, byte_offset=r.byte_offset, seq=r.seq)
    assert [e.text for e in r2.events] == ["half"]
    assert r2.seq == 2


def test_seq_counts_every_line_skipped_counts_only_malformed(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"role": "user", "text": "a"}),
        "",  # blank: advances seq, not skipped
        "{not json",  # malformed JSON: skipped
        json.dumps([1, 2, 3]),  # valid JSON, not a dict: skipped
        json.dumps({"kind": "skip"}),  # parser returns None: NOT skipped
        json.dumps({"role": "assistant", "text": "b"}),
    ]
    path.write_bytes(("\n".join(lines) + "\n").encode())
    r = _reader().read_events(path)

    assert [e.text for e in r.events] == ["a", "b"]
    assert r.seq == 6
    assert r.skipped == 2


def test_parser_exception_is_contained_as_a_skip(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [
            {"role": "user", "text": "a"},
            {"kind": "boom"},  # parser raises here
            {"role": "assistant", "text": "b"},
        ],
    )
    r = _reader().read_events(path)

    assert [e.text for e in r.events] == ["a", "b"]  # scan survives the raiser
    assert r.skipped == 1
    assert r.seq == 3


def test_queued_duplicate_dropped_unprocessed_kept(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [
            {"role": "user", "text": "hello", "queued": True},  # later processed
            {"role": "user", "text": "hello"},  # its real processed turn
            {"role": "user", "text": "pending", "queued": True},  # never processed
        ],
    )
    r = _reader().read_events(path)
    seen = {(e.text, e.queued) for e in r.events}

    assert ("hello", True) not in seen  # the queued duplicate is gone
    assert ("hello", False) in seen  # the real turn stays
    assert ("pending", True) in seen  # an unprocessed queued message stays


def test_last_event_returns_latest_probe(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a1"}])
    assert _reader().last_event(path).text == "a1"


def test_last_event_resets_on_turn_boundary(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [
            {"role": "assistant", "tool_name": "Bash"},  # probe hit
            {"boundary": True, "role": "system"},  # boundary clears it
            {"role": "system"},  # non-probe (no text/tool/question)
        ],
    )
    assert _reader().last_event(path) is None


def test_recent_conversation_filters_and_limits(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [
            {"role": "user", "text": "u1"},
            {"role": "assistant", "text": "a1"},
            {"role": "tool", "text": "ignored"},  # not user/assistant
            {"role": "assistant"},  # no text
            {"role": "user", "text": "u2"},
        ],
    )
    got = [e.text for e in _reader().recent_conversation(path, limit=2)]
    assert got == ["a1", "u2"]


def test_transcript_cursor_counts_only_terminated_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    complete = "".join(json.dumps({"n": i}) + "\n" for i in range(3))
    partial = json.dumps({"n": 3})  # unterminated tail
    path.write_bytes((complete + partial).encode())

    offset, seq = transcript_cursor(path)
    assert seq == 3
    assert offset == len(complete.encode())


def test_read_line_fetches_exact_line_and_guards(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"n": 1}, {"n": 2}, {"n": 3}])
    assert read_line(path, 2) == {"n": 2}
    assert read_line(path, 0) is None  # 1-based; seq < 1 is invalid
    assert read_line(path, 99) is None  # past EOF
    path.write_bytes(b"[1, 2, 3]\n")
    assert read_line(path, 1) is None  # non-dict line


def test_parse_ts():
    assert parse_ts("2026-07-22T10:00:00Z") is not None
    assert parse_ts("not a timestamp") is None
    assert parse_ts(None) is None


def test_missing_file_reads_empty(tmp_path):
    reader = _reader()
    missing = tmp_path / "nope.jsonl"
    r = reader.read_events(missing)
    assert r.events == [] and r.skipped == 0
    assert reader.last_event(missing) is None
    assert reader.recent_conversation(missing) == []
    assert transcript_cursor(missing) == (0, 0)


def _classify(obj):
    """Stub classifier: ``kind`` names the verdict, ``text`` carries the message."""
    kind = obj.get("kind")
    if kind == "boom":
        raise ValueError("classifier blew up")
    if kind in (PROMPT, AGENT_TEXT):
        return (kind, obj.get("text"))
    if kind == RESET:
        return (RESET, None)
    return None


def test_newest_messages_finds_both_sides_across_chunks(tmp_path):
    # Both messages sit far from EOF, behind many lines and several chunk reads:
    # a fixed tail window is what misses them, walking back is what finds them.
    noise = [{"kind": "noise", "filler": "x" * 200} for _ in range(200)]
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [{"kind": PROMPT, "text": "old ask"}, {"kind": AGENT_TEXT, "text": "old reply"}]
        + noise
        + [{"kind": PROMPT, "text": "new ask"}, {"kind": AGENT_TEXT, "text": "new reply"}]
        + noise,
    )
    found = newest_messages_backwards(path, _classify, chunk=512)
    assert (found.prompt, found.text) == ("new ask", "new reply")  # newest of each, not oldest
    assert found.role == "agent"  # the reply is the one the scan met first
    assert found.prompt_decided and found.text_decided


def test_newest_messages_role_is_whoever_the_scan_meets_first(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": AGENT_TEXT, "text": "here you go"}, {"kind": PROMPT, "text": "and now"}])
    found = newest_messages_backwards(path, _classify)
    assert (found.role, found.prompt, found.text) == ("user", "and now", "here you go")


def test_newest_messages_never_reads_silence_as_an_answer(tmp_path):
    # Nothing decisive anywhere: both slots stay undecided so the caller keeps its
    # own head/tail values instead of having them cleared.
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": "noise"}, {"kind": "noise"}])
    found = newest_messages_backwards(path, _classify)
    assert (found.prompt_decided, found.text_decided, found.role) == (False, False, None)


def test_newest_messages_skips_a_kind_that_carries_no_text(tmp_path):
    # A tool-only assistant line is an AGENT_TEXT line with nothing to show; the
    # scan must keep walking rather than settle the slot on it.
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [
            {"kind": AGENT_TEXT, "text": "the real reply"},
            {"kind": AGENT_TEXT},
            {"kind": AGENT_TEXT},
        ],
    )
    assert newest_messages_backwards(path, _classify).text == "the real reply"


def test_newest_messages_reset_decides_both_slots_empty(tmp_path):
    # A compaction boundary discards everything older, so the pre-reset turn must
    # not resurface even though it is the only turn in the file.
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": PROMPT, "text": "before"}, {"kind": AGENT_TEXT, "text": "reply"}])
    _append(path, [{"kind": RESET}])
    found = newest_messages_backwards(path, _classify)
    assert (found.prompt, found.text, found.role) == (None, None, None)
    assert found.prompt_decided and found.text_decided
    # A real turn after the reset is current again, and the reset stops the walk
    # before the discarded turn behind it.
    _append(path, [{"kind": AGENT_TEXT, "text": "after"}])
    found = newest_messages_backwards(path, _classify)
    assert (found.prompt, found.text, found.role) == (None, "after", "agent")


def test_newest_messages_reports_undecided_instead_of_a_false_none(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [{"kind": PROMPT, "text": "buried"}]
        + [{"kind": "noise", "filler": "x" * 500} for _ in range(50)],
    )
    # Budget too small to reach the prompt: undecided means "unknown", not
    # "absent", so the caller keeps its head/tail value.
    found = newest_messages_backwards(path, _classify, limit=1024, chunk=256)
    assert (found.prompt, found.prompt_decided, found.text_decided) == (None, False, False)
    # A budget that reaches the start of the file finds it. The absent reply stays
    # undecided — silence is never read as an answer, so the caller keeps its own.
    found = newest_messages_backwards(path, _classify, chunk=256)
    assert (found.prompt, found.prompt_decided) == ("buried", True)
    assert (found.text, found.text_decided) == (None, False)


def test_newest_messages_keeps_a_found_slot_decided_when_the_budget_runs_out(tmp_path):
    # The reply is near EOF and the prompt far behind it: the budget stops the
    # walk, which must not un-decide the reply it already found.
    path = tmp_path / "t.jsonl"
    _write(
        path,
        [{"kind": PROMPT, "text": "buried"}]
        + [{"kind": "noise", "filler": "x" * 500} for _ in range(50)]
        + [{"kind": AGENT_TEXT, "text": "fresh reply"}],
    )
    found = newest_messages_backwards(path, _classify, limit=2048, chunk=256)
    assert (found.text, found.text_decided) == ("fresh reply", True)
    assert (found.prompt, found.prompt_decided) == (None, False)


def test_newest_messages_ignores_a_half_written_final_line(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": AGENT_TEXT, "text": "complete"}])
    with path.open("ab") as handle:
        handle.write(b'{"kind": "agent_text", "text": "half')  # still being appended
    assert newest_messages_backwards(path, _classify).text == "complete"


def test_newest_messages_contains_bad_lines_and_a_raising_classifier(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": AGENT_TEXT, "text": "survivor"}, {"kind": "boom"}])
    path.write_bytes(path.read_bytes() + b"not json\n[1, 2]\n")
    assert newest_messages_backwards(path, _classify).text == "survivor"

    # An unreadable file decides nothing: the caller must keep its own values.
    missing = newest_messages_backwards(tmp_path / "nope.jsonl", _classify)
    assert (missing.text, missing.text_decided, missing.prompt_decided) == (None, False, False)


def test_newest_messages_prefilter_skips_the_json_parse(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [{"kind": AGENT_TEXT, "text": "keep"}, {"kind": AGENT_TEXT, "text": "hidden"}])
    seen = []

    def classify(obj):
        seen.append(obj)
        return _classify(obj)

    found = newest_messages_backwards(path, classify, prefilter=lambda raw: b"keep" in raw)
    assert found.text == "keep" and len(seen) == 1  # the filtered line never reached it
