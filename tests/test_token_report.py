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
            # A success notice's routine line (#4253) is read past.
            "Routine notice: do what it asks, then answer in one or two sentences.\n\n"
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
    # A watch's message (#4466) is a wake-up like any other wake-on.
    assert token_report.trigger("AgentDeck watch: your check for x says the problem is back") == (
        "wake"
    )
    assert token_report.stage_turns([]) == []
    assert (
        token_report.reviewed_work(
            "You are AgentDeck's Claude reviewer. The deck started you to review pull request"
            " A/b#7 at commit `abc` for the Work A/b#6. This directory"
        )
        == "a/b#6"
    )
    fallback = (
        "You are AgentDeck's Claude reviewer. ChatGPT was not started: chat x/chat#9"
        " (https://github.com/x/chat/issues/9), which asks for a review of a/b#5.\n"
    )
    assert token_report.reviewed_work(fallback) == "a/b#5"
    design = (
        "You are AgentDeck's Claude reviewer. The deck started you to review the design of"
        " the Work A/b#4 before any code is written for it: the comment https://x"
    )
    assert token_report.reviewed_work(design) == "a/b#4"
    assert token_report.reviewed_work("Context: Work for a/b#1. for the Work a/b#2.") == ""


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
            "message": {
                "id": "m1",
                "model": "claude-haiku-5-5",
                "usage": usage,
                "content": [{"type": kind, **extra}],
            },
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
    assert calls[0].units == 2 * 0.2 * (  # alt's week is half; Haiku is a fifth of Opus
        token_report.INPUT
        + 1000 * token_report.WRITE
        + 5000 * token_report.READ
        + 10 * token_report.OUTPUT
    )
    assert token_report.read_transcript(path, 2, seen)[0] == []  # a resumed copy counts once
    assert token_report.model_price("claude-sonnet-5-5") == 0.6
    assert token_report.model_price("claude-opus-5-5") == token_report.model_price(None) == 1


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


def test_units_per_finished_work_spread_every_unit_over_the_works_closed_in_the_window(tmp_path):
    path = tmp_path / "deck.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        create table works (work_id, source_kind, source_key, created_at);
        insert into works values
            ('github_issue:A/b#1', 'github_issue', 'A/b#1', '1'),
            ('github_issue:a/b#2', 'github_issue', 'a/b#2', '1'),
            ('github_issue:a/c#3', 'github_issue', 'a/c#3', '1'),
            ('github_issue:Z/d#1', 'github_issue', 'Z/d#1', '1');
        create table work_merged_pulls (work_id, pull, base, merged_at);
        insert into work_merged_pulls values
            ('github_issue:a/c#3', 'a/c#10', 'main', '2026-11-04T08:00:00+00:00'),
            ('github_issue:a/c#3', 'a/c#11', 'main', '2026-11-04T09:00:00+00:00'),
            ('github_issue:A/b#1', 'a/b#12', 'main', '2026-11-04T08:00:00+00:00');
        create table work_metadata (work_id, live_check_at);
        insert into work_metadata values
            ('github_issue:a/c#3', '2026-11-05T08:00:00+00:00'),
            ('github_issue:A/b#1', '2026-11-03T00:00:00+00:00'),
            ('github_issue:a/b#2', null);
        """
    )
    db.commit()
    db.close()
    since = token_report._stamp("2026-11-03T08:00:00+00:00")
    until = token_report._stamp("2026-11-10T08:00:00+00:00")
    asked = []

    def closes(repository, start, end, numbers):  # GitHub's answer: every close, cached or not
        asked.append((repository, numbers))
        if repository == "z/d":
            raise token_report.subprocess.CalledProcessError(1, "gh")
        return {"a/b#1": since + 9, "a/b#9": since + 9, "a/c#3": since + 9}

    finished, unread = token_report.finished_works(str(path), since, until, closes)
    # b#9 closed but no chat worked on it; b#2 did not close; z/d could not be read.
    # Nothing here is in the deck's issue cache or work_signoff: GitHub alone decides.
    assert finished == {"a/b#1": since + 9, "a/c#3": since + 9}
    # one read per repository, names folded, asking only about its Works
    assert asked == [("a/b", {"1", "2"}), ("a/c", {"3"}), ("z/d", {"1"})]
    assert unread == ["z/d"]

    def call(ts, work, output, stage="build"):
        made = token_report.Call(ts, 1, output=output)
        made.work, made.stage = work, stage
        return made

    unit = 1 / token_report.OUTPUT
    chats = {
        ("w", ""): [
            call(since - 40 * 86400, "a/c#3", unit),  # an old turn of a recently changed chat
            call(since - 86400, "a/c#3", unit),
            call(since + 1, "a/c#3", unit),
        ],
        ("o", ""): [call(since + 1, "a/b#9", unit)],
        ("n", ""): [call(since + 1, "", unit), call(until, "", 5 * unit)],
        ("bench", ""): [call(since + 2, "", 2 * unit, "benchmark")],
        # a review of b#1 is not its first turn: b#1 has none, so no speed
        ("r", ""): [call(since + 1, "a/b#1", 0, "review")],
    }
    ends = token_report.work_ends(str(path))
    # the later of the last live check and the last merge (b#1 merged after a failed
    # check); a Work with neither has no end
    assert ends == {
        "a/c#3": token_report._stamp("2026-11-05T08:00:00+00:00"),
        "a/b#1": token_report._stamp("2026-11-04T08:00:00+00:00"),
    }
    lines = token_report.per_finished_work(
        chats, finished, since, until, since - 30 * 86400, ends=ends
    )
    assert lines[0].startswith("2 Works finished, 5.0 units: 2.500 units per finished Work")
    # b#9's first turn is in the window; c#3's was 40 days before it
    assert lines[1] == "  Works started 1, 1.0 a week"
    assert "on them 1.0u" in lines[2] and "not finished in the window 1.0u" in lines[2]
    assert "review benchmark 2.0u, no Work 1.0u" in lines[2]
    assert lines[3].startswith("  as if all on one account: 2.500")
    assert "(0.0u): 2.500" in lines[4]
    # c#3 from its first turn, 40 days before the window, to its live check 2 days in
    assert lines[5] == (
        "  speed, first turn to last live check or merge: median 1008.0 h, p75 1008.0 h,"
        " p90 1008.0 h (1 Works, of which 0 to their close, having neither in time;"
        " 1 with no Claude turn read before it)"
    )
    # with no live check or merge, a Work's close ends it; one ending before its first
    # turn has no speed
    chats[("b", "")] = [call(since + 3600, "a/b#1", 0)]
    closing = token_report.per_finished_work(chats, finished, since, until, ends={})
    assert closing[5].endswith(
        "median 960.0 h, p75 960.0 h, p90 960.0 h (1 Works, of which 1 to their close,"
        " having neither in time; 1 with no Claude turn read before it)"
    )
    # a check after the window, or one from before the Work's first turn (a reopen), is
    # not its end: its close is, as with neither
    for late in (until + 5 * 86400, since - 50 * 86400):
        moved = token_report.per_finished_work(chats, finished, since, until, ends={"a/c#3": late})
        assert "median 960.0 h" in moved[5] and "of which 1 to their close" in moved[5]
    assert "mean 1.000u, median 1.000u" in lines[6] and "with no Claude use 1" in lines[6]
    assert "p90 1.800u" in lines[6]  # within the two costs, 0 and 2, not past the dearest
    hours = token_report._hours([3600.0 * n for n in range(10, 0, -1)])
    assert hours == "median 5.5 h, p75 7.8 h, p90 9.1 h"
    assert token_report._hours([]) == "none"

    short = token_report.per_finished_work(chats, finished, since, until, None, unread)
    assert short[0].endswith("n/a units per finished Work")  # z/d's Works are unknown
    assert "n/a" in short[3] and "n/a" in short[4]
    assert short[5] == "  n/a: closes not read for z/d, so the count is short"
    assert short[6] == "  life cost per finished Work: n/a, and no per-repository figures"
    assert len(short) == 7  # no speed, life cost or repository row from a short finished set

    quiet = token_report.per_finished_work(chats, set(), since, until)
    assert quiet[0].endswith("n/a units per finished Work")
    assert "n/a" in quiet[3] and "n/a" in quiet[4]
    assert "speed, first turn to last live check or merge: none (0 Works" in quiet[5]
    assert len(quiet) == 6  # no life cost line when no Work finished


def test_github_closes_counts_a_close_as_it_stood_at_the_window_end(monkeypatch):
    since = token_report._stamp("2026-11-03T08:00:00+00:00")
    until = token_report._stamp("2026-11-10T08:00:00+00:00")
    events = {  # each issue's close and reopen events, oldest first
        "5": "closed\t2026-11-04T00:00:00Z\nreopened\t2026-11-12T00:00:00Z",  # reopened later
        "6": "closed\t2026-11-04T00:00:00Z\nreopened\t2026-11-05T00:00:00Z",  # in the window
        "7": "closed\t2026-11-05T00:00:00Z\nreopened\t2026-11-11T00:00:00Z\n"
        "closed\t2026-11-20T00:00:00Z",  # closed again in a later week
        "2": "closed\t2026-11-10T08:00:00Z",  # the window's end is outside it
    }
    paths = []

    def run(args, **kwargs):
        path = args[2]
        paths.append(path)
        if "/events" in path:
            out = events[path.split("/")[4]]
        else:
            # GitHub lists only issues updated strictly after ``since``; #1's last update
            # is its close, exactly at the window's start
            after = token_report._stamp(path.split("since=")[1].split("&")[0])
            assert after < since
            out = "\n".join(
                [
                    "1\tclosed\t2026-11-03T08:00:00Z",
                    "2\tclosed\t2026-11-10T08:00:00Z",
                    "4\tclosed\t2026-11-02T08:00:00Z",  # closed before, updated since
                    "5\topen\t\treopened",
                    "6\topen\t\treopened",
                    "7\tclosed\t2026-11-20T00:00:00Z",
                    "8\topen\t",  # never closed, so its events are not read
                    "9\tclosed\t2026-11-04T00:00:00Z",  # not one of the Works asked about
                    "x\tclosed\t2026-11-04T00:00:00Z",
                    "short",
                ]
            )
        return token_report.subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(token_report.subprocess, "run", run)
    works = {"1", "2", "4", "5", "6", "7", "8"}
    # each with when it closed: from the list, or from its last close event in the window
    assert token_report.github_closes("A/B", since, until, works) == {
        "a/b#1": since,
        "a/b#5": token_report._stamp("2026-11-04T00:00:00Z"),
        "a/b#7": token_report._stamp("2026-11-05T00:00:00Z"),
    }
    assert paths[0] == "repos/A/B/issues?state=all&since=2026-11-03T07:59:59Z&per_page=100"
    # only the issues the list cannot settle have their events read
    assert sorted(path.split("/")[4] for path in paths[1:]) == ["2", "5", "6", "7"]
    assert "a/b#9" in token_report.github_closes("A/B", since, until)
    day = 86400.0
    # newest first is read the same as oldest first
    assert token_report.closed_at_end(
        [("reopened", since + 2 * day), ("closed", since + 3 * day), ("closed", since + day)],
        since, until,
    )  # fmt: skip
    # a close and a reopen in the same second leave it open
    assert not token_report.closed_at_end(
        [("reopened", since + day), ("closed", since + day)], since, until
    )
    # closed before the window and reopened in it: not finished in this week
    assert not token_report.closed_at_end(
        [("closed", since - day), ("reopened", since + day)], since, until
    )
    assert not token_report.closed_at_end([], since, until)


def test_a_chat_the_database_ties_to_no_work_takes_the_one_its_first_message_names():
    assert token_report.prompt_work("You are working A/b#12 for the AgentDeck poller.") == "a/b#12"
    assert token_report.prompt_work("Run the kanban-worker skill for a/b#3.") == "a/b#3"
    assert token_report.prompt_work("You are working b#12 for the poller.") == ""  # no owner
    brief = "You are AgentDeck's Claude reviewer. Review pull request a/b#7 for the Work a/b#6."
    assert token_report.prompt_work(brief) == "a/b#6"
    assert token_report.prompt_work("hello") == ""


def test_a_chat_continued_from_another_takes_the_work_the_earlier_one_was_on():
    note = "Continue the work from the earlier part of this AgentDeck chat."
    read = "~/agentdeck/.venv/bin/agentdeck chat-read claude_code:main:{} --outline"
    old, mid, new, gone, loop1, loop2 = (f"{n:08x}-0000-0000-0000-000000000000" for n in range(6))
    works = {old: [(0.0, "a/b#1"), (100.0, "a/b#2")]}
    turns = {
        # an account move from a chat on two Works: the one it was on when it moved
        mid: [
            (
                50.0,
                f"The previous account (alt) reached its usage limit until 10:00.\n\n"
                f"{note}\n\nIssues: x/y#9\n\n{read.format(old)}",
            )
        ],
        # moved again, from the cold-chat bar, a person's words first
        new: [(150.0, f"eteneekö tämä\n\n{note}\n\n{read.format(mid)}")],
        # the earlier chat ties to no Work: the issue its message names
        gone: [
            (
                1.0,
                f"Continue the work from this handoff:\n\n{read.format('dead')}"
                " https://h/sessions/claude_code:main:"
                "ffffffff-0000-0000-0000-000000000000/x\n\nOn A/c#3, then a/c#4.",
            )
        ],
        loop1: [(1.0, f"{note} {read.format(loop2)}")],  # two chats naming each other
        loop2: [(1.0, f"{note} {read.format(loop1)} on a/d#5")],
        "written": [(1.0, "Continue the work from this handoff.\n\n# Handoff: A/e#6 ships")],
        "worker": [(1.0, "You are working a/f#7 for the AgentDeck poller.")],
        "person": [(1.0, "Continue the work from yesterday, please.")],
        "empty": [],
    }
    token_report.infer_works(works, turns)
    assert {sid: token_report.work_at(entries, 0) for sid, entries in works.items()} == {
        old: "a/b#1",
        mid: "a/b#1",
        new: "a/b#1",
        gone: "a/c#3",
        loop2: "a/d#5",
        "written": "a/e#6",
        "worker": "a/f#7",
    }
    assert token_report.continued_from("hello claude_code:main:" + old) == ("", "")


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
