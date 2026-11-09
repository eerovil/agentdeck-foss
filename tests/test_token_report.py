"""The token report's stage rules and transcript reading (#4080)."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "token_report", Path(__file__).parent.parent / "scripts" / "token_report.py"
)
token_report = importlib.util.module_from_spec(spec)
sys.modules["token_report"] = token_report
spec.loader.exec_module(token_report)


def test_a_chat_moves_through_its_stages_on_the_decks_own_messages():
    stages = token_report.stage_turns(
        [
            "Context: Work for eerovil/agentdeck#1.",
            "I approve the spec. Remove possible spec-only label and start implementing.",
            "Your queued job 12 finished successfully",
            "AgentDeck: before eerovil/agentdeck#2 at abc goes to review",
            "AgentDeck: the deck's reviewer asks for changes on eerovil/agentdeck#2",
            "AgentDeck: eerovil/agentdeck#2 was behind its base",
            "AgentDeck: the review of eerovil/agentdeck#2 at abc passed clean.",
            "AgentDeck: eerovil/agentdeck#2 merged. Check the change live.",
        ]
    )
    assert [stage for stage, _ in stages] == [
        "spec",
        "build",
        "build",
        "pre-review",
        "fix",
        "upkeep",
        "release",
        "after-merge",
    ]
    assert token_report.stage_turns(["You are AgentDeck's Claude reviewer."])[0][0] == "review"
    assert token_report.stage_turns([]) == []


def test_a_transcript_counts_one_call_per_request_and_each_tool_it_asks_for(tmp_path):
    usage = {
        "input_tokens": 1,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 5000,
        "output_tokens": 10,
    }

    def block(kind, **extra):
        return {
            "type": "assistant",
            "timestamp": "2026-11-04T08:00:01Z",
            "requestId": "r1",
            "message": {"id": "m1", "usage": usage, "content": [{"type": kind, **extra}]},
        }

    rows = [
        {
            "type": "user",
            "timestamp": "2026-11-04T08:00:00Z",
            "message": {"content": "Context: Work for a/b#1."},
        },
        block("tool_use", name="Bash", input={"command": "cd /x && git log"}),
        block("tool_use", name="Read", input={}),
        "not json",
        "3",
        '{"type": "assistant", "timestamp": "2026-11-04T08:00:02Z", "message": "gone"}',
    ]
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows))

    seen = set()
    calls, turns = token_report.read_transcript(path, 2, seen)

    assert len(calls) == 1 and len(turns) == 1
    assert calls[0].tools == ["Bash: git", "Read"]
    assert calls[0].context == 6001
    assert calls[0].units == 2 * (
        token_report.INPUT
        + 1000 * token_report.WRITE
        + 5000 * token_report.READ
        + 10 * token_report.OUTPUT
    )
    assert token_report.read_transcript(path, 2, seen)[0] == []  # a resumed copy counts once


def test_a_chat_belongs_to_a_work_only_on_evidence_the_deck_accepts(tmp_path):
    path = tmp_path / "deck.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        create table session_run_bindings (session_key, work_id, bound_at);
        create table session_work_bindings (session_key, work_id, evidence, bound_at);
        create table session_issues (session_key, repository, number, added_at);
        insert into session_run_bindings values
            ('claude_code:main:run-only', 'github_issue:A/b#1', '1'),
            ('claude_code:main:both', 'github_issue:a/b#2', '2'),
            ('claude_code:main:two-runs', 'github_issue:a/b#11', '2026-11-01T00:00:00+00:00'),
            ('claude_code:main:two-runs', 'github_issue:a/b#12', '2026-11-02T00:00:00+00:00');
        insert into session_work_bindings values
            ('claude_code:main:both', 'github_issue:a/b#3', 'side_chat', '1'),
            ('claude_code:main:delegated', 'github_issue:a/b#4', 'delegated', '1'),
            ('claude_code:main:candidates', 'github_issue:a/b#5', 'ambiguous', '1'),
            ('claude_code:main:candidates', 'github_issue:a/b#6', 'ambiguous', '2'),
            ('claude_code:main:unbound', 'github_issue:a/b#7', 'none', '1'),
            ('claude_code:main:issue', 'github_issue:a/b#8', 'ambiguous', '1');
        insert into session_issues values
            ('claude_code:main:issue', 'a/b', 9, '1'),
            ('claude_code:main:issue', 'a/b', 10, '2');
        """
    )
    db.commit()
    db.close()
    works = token_report.work_of_sessions(str(path))
    assert {sid: token_report.work_at(entries, 0) for sid, entries in works.items()} == {
        "run-only": "a/b#1",
        "both": "a/b#2",
        "two-runs": "a/b#11",
        "delegated": "a/b#4",
        "issue": "a/b#10",
    }
    later = token_report._stamp("2026-11-02T00:00:00+00:00")
    assert token_report.work_at(works["two-runs"], later - 1) == "a/b#11"
    assert token_report.work_at(works["two-runs"], later) == "a/b#12"
    assert token_report.work_at([], later) == ""


def test_compacting_pays_only_once_enough_calls_follow_it():
    calls = [token_report.Call(float(n), 1, read=150_000 + n * 100) for n in range(400)]
    chats = {("chat", ""): calls}
    assert token_report.replay_window(chats, 0, 1000, 10**9) == 0
    assert token_report.replay_window(chats, 0, 1000, 170_000) > 0
    assert token_report.replay_window(chats, 0, 210, 170_000) < 0


def test_a_real_compaction_ends_the_simulated_one():
    before = [token_report.Call(float(n), 1, read=150_000 + n * 1000) for n in range(17)]
    after = [token_report.Call(100.0 + n, 1, read=62_000, after_compact=n == 0) for n in range(100)]
    chats = {("chat", ""): before + after}
    only_before = token_report.replay_window({("chat", ""): before}, 0, 1000, 120_000)
    assert token_report.replay_window(chats, 0, 1000, 120_000) == only_before
