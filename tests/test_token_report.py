"""The token report's stage rules and transcript reading (#4080)."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from agentdeck.deck_notices import KEEP_WARM_TEXT
from agentdeck.gh_shim.classify import target_owner
from agentdeck.note_text import with_notes
from agentdeck.pre_review_helpers import Outcome, log_usage

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
    # Deck notes in front of a message (#4303) move the stage as when they came alone, and
    # the message behind them is what started the turn (#4687).
    passed = "AgentDeck: the review of eerovil/agentdeck#2 at abc passed clean."
    asks = "AgentDeck: the deck's reviewer asks for changes on eerovil/agentdeck#2"
    assert token_report.stage_turns(
        [
            "Context: Work for eerovil/agentdeck#1.",
            with_notes(["AgentDeck: something."], asks),
            with_notes([passed], "AgentDeck wake-on: check"),
            with_notes([passed], asks),
        ]
    ) == [
        ("build", "start"),
        ("fix", "review_result"),
        ("release", "wake"),
        ("fix", "review_result"),
    ]
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

    def block(kind, request="r1", used=usage, **extra):
        return {
            "type": "assistant",
            "timestamp": "2026-11-04T08:00:01Z",
            "requestId": request,
            "message": {
                "id": "m" + request,
                "model": "claude-haiku-5-5",
                "usage": used,
                "content": [{"type": kind, **extra}],
            },
        }

    def attached(**attachment):  # an attachment row may carry no time
        return {"type": "attachment", "attachment": attachment}

    guide = [
        {"path": "/var/home/me/proj/.worktrees/issue-1/AGENTS.md", "content": "g" * 7000},
        {"path": "/home/me/.claude/CLAUDE.md", "content": "ä\n" * 100},
    ]
    rows = [
        attached(type="instructions", files=guide),
        {"type": "assistant", "timestamp": "2026-11-04T08:00:00Z", "message": {"usage": {}}},
        attached(type="prompt_snapshot", systemPrompt=["x" * 90, "old"]),
        attached(
            type="prompt_snapshot",
            systemPrompt=["x" * 90, "\n" + token_report.SESSION_GUIDE + "." * 499],
        ),
        {
            "type": "user",
            "timestamp": "2026-11-04T08:00:00Z",
            "message": {"content": "Context: Work for a/b#1." + "i" * 3576},
        },
        block("tool_use", name="Bash", id="t1", input={"command": "cd /x && git log"}),
        block("tool_use", name="Read", id="t2", input={}),
        {
            "type": "user",
            "timestamp": "2026-11-04T08:00:03Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 16_001},
                    {
                        "type": "tool_result",
                        "tool_use_id": "t2",
                        "content": [{"type": "text", "text": "y" * 16_000}, {"type": "image"}],
                    },
                    {"type": "tool_result", "tool_use_id": "gone", "content": "z" * 99_999},
                ]
            },
        },
        "not json",
        "3",
        '{"type": "assistant", "timestamp": "2026-11-04T08:00:02Z", "message": "gone"}',
        {
            "type": "user",
            "timestamp": "2026-11-04T08:00:03Z",
            "message": {"content": [{"type": "tool_result", "content": "o" * 9000}]},
        },
        block("text", request="r2", used=usage | {"cache_read_input_tokens": 8000}, text="done"),
    ]
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows))

    seen = set()
    guides = {}
    calls, turns = token_report.read_transcript(path, 2, seen, guides)

    # the guide files as first listed, and the session guide in the newest copy of the prompt
    assert guides == {
        "files": {guide[0]["path"]: 7000, guide[1]["path"]: 200},
        "session": len(token_report.SESSION_GUIDE) + 500,
    }
    # #4714: the first call holds the brief by its characters and the standing start as the
    # rest; the next grew by 3000: the first one's output of 10, and what the tool sent back
    first, second = (dict(zip(token_report.PARTS, call.parts, strict=True)) for call in calls)
    assert round(first[token_report.BRIEF]) == 1000 and sum(first.values()) == 6001
    assert first[token_report.STANDING] > 4990 and not first[token_report.TOOL_OUTPUT]
    assert round(second[token_report.TOOL_OUTPUT]) == 2990 and round(sum(second.values())) == 9001
    assert round(second[token_report.OWN] + second[token_report.THINKING]) == 10
    del calls[1]

    assert len(calls) == 1 and len(turns) == 1
    assert calls[0].tools == ["Bash: git", "Read"]
    # #4754: what each block of the call wrote, sized by its characters
    assert [kind for kind, _ in calls[0].wrote] == [token_report.SHELL, token_report.OTHER_CALLS]
    assert not calls[0].thought
    split = token_report.written_units(calls[0])  # no thinking: all 10 tokens are the two calls
    assert sum(split.values()) == pytest.approx(10 * calls[0].scale * token_report.OUTPUT)
    assert split[token_report.THOUGHT] == 0 and split[token_report.SHELL] > 0
    # #4725: how long each answer was, by its tool; one with no call in this file is left out
    assert calls[0].results == [("Bash: git", 16_001), ("Read", 16_000)]
    stamp = calls[0].ts
    assert token_report.ideas({("chat", ""): calls}, stamp, stamp + 1)[-1] == (
        "tool results 2: over 16000 characters 1 = 50.0%, from the shell 1"
    )
    assert token_report.ideas({}, stamp, stamp + 1)[-1].startswith("tool results 0: over")
    # #4737: what the keep-warm pings cost, on the line under the cold wakes they prevent
    kind, calls[0].kind = calls[0].kind, token_report.trigger(KEEP_WARM_TEXT)
    lines = token_report.ideas({("chat", ""): calls}, stamp, stamp + 1)
    assert calls[0].kind == "keep_warm" and "keep_warm" not in token_report.WOKEN
    assert token_report.trigger("AgentDeck keep-warm: why do I see this?") != "keep_warm"
    ping = next(i for i, line in enumerate(lines) if line.startswith("keep-warm pings 1: "))
    assert lines[ping - 1].startswith("cold wakes ") and "= 100.0%" in lines[ping]
    calls[0].kind = kind
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
    assert "review benchmark 2.0u, no Work 1.0u, pre-review helpers 0.0u" in lines[2]
    # the helpers' runs are in the headline and on a row of their own; one holds its whole
    # run, so its context is not a size
    runs = [token_report.Call(since, 1, read=10**6, output=unit)]
    helped = token_report.per_finished_work(chats, finished, since, until, helpers=runs)
    assert helped[0].startswith("2 Works finished, 6.0 units")
    assert "pre-review helpers 1.0u" in helped[2] and "(0.0u)" in helped[4]
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
    asked = []

    def run(args, **kwargs):
        # what gh prints: an issue's number, then its closes and reopens, oldest first
        asked.append((args, kwargs["env"]))
        out = "\n".join(
            [
                "1\tclosed 2026-11-03T08:00:00Z",
                "2\tclosed 2026-11-10T08:00:00Z",  # the window's end is outside it
                "4\tclosed 2026-11-02T08:00:00Z",  # closed before, updated since
                "5\tclosed 2026-11-04T00:00:00Z\treopened 2026-11-12T00:00:00Z",  # later
                "6\tclosed 2026-11-04T00:00:00Z\treopened 2026-11-05T00:00:00Z",
                "7\tclosed 2026-11-05T00:00:00Z\treopened 2026-11-11T00:00:00Z"
                "\tclosed 2026-11-20T00:00:00Z",  # closed again in a later week
                "8",  # never closed
                "9\tclosed 2026-11-04T00:00:00Z",  # not one of the Works asked about
                "4\tclosed soon",
            ]
        )
        return token_report.subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(token_report.subprocess, "run", run)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    works = {"1", "2", "4", "5", "6", "7", "8"}
    # each with when it closed: its last close in the window
    assert token_report.github_closes("A/B", since, until, works) == {
        "a/b#1": since,
        "a/b#5": token_report._stamp("2026-11-04T00:00:00Z"),
        "a/b#7": token_report._stamp("2026-11-05T00:00:00Z"),
    }
    # one call, however many issues reopened or closed later (#4687), from a second before
    # the window, so #1, last updated by its close at the window's start, is listed
    ((args, env),) = asked
    assert args[:4] == ["gh", "api", "graphql", "--paginate"]
    assert {"owner=A", "name=B", "since=2026-11-03T07:59:59Z"} <= set(args)
    # the gh shim reads whose token speaks from the remote, set past the settings there
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_CONFIG_KEY_1"] == "remote.origin.url"
    assert target_owner(args[1:], remote=env["GIT_CONFIG_VALUE_1"]) == "a"
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


def test_use_without_a_transcript_is_counted_and_set_beside_the_meters(tmp_path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("AGENTDECK_HELPER_USAGE_LOG", str(log))
    unit = 1 / token_report.OUTPUT
    tokens = {"input_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0, "cost_usd": 1.0}
    usage = tokens | {"model": "claude-haiku-5-5,claude-opus-5-5", "output_tokens": unit}
    for account in ("claude_code:main", "claude_code:alt", ""):
        monkeypatch.setattr("time.strftime", lambda *_: "2026-11-18T22:30:00+0200")
        log_usage("a/b#1", "abc", account, [Outcome(1, usage=usage)] * (account != ""))
    monkeypatch.setattr("time.strftime", lambda *_: "2026-11-19T22:30:00+0200")
    lengths = {"reply_chars": 289, "tool_chars": 221, "tool_calls": 2}  # the log's own fields
    log_usage("a/b#2", "abc", "claude_code:main", [Outcome(1, usage=usage | lengths)])
    monkeypatch.setattr("time.strftime", lambda *_: "2026-11-17T22:30:00+0200")
    log_usage("a/b#1", "abc", "", [Outcome(1, usage=usage | {"model": ""})])
    with log.open("a") as rows:
        rows.write('not json\n[]\n{"at": null}\n')
    since = token_report._stamp("2026-11-18T05:40:00+00:00")
    until = token_report._stamp("2026-11-25T05:40:00+00:00")
    helpers, begins = token_report.helper_calls(log, since, until)
    # a run logged before #4754 is not split; a later one by the lengths its line holds
    *old, split = helpers
    assert [c.wrote for c in old] == [None, None] and split.thought
    assert split.wrote == [
        (token_report.TO_PERSON, pytest.approx(100 / token_report.TEXT_FIX)),
        (token_report.OTHER_CALLS, pytest.approx(140 / token_report.TOOL_FIX)),
    ]
    del helpers[2]
    # alt's week is half the size; a run over two models counts as the dearer
    assert [round(c.units, 6) for c in helpers] == [1.0, 2.0]
    assert begins == token_report._stamp("2026-11-17T20:30:00+00:00")
    assert token_report.helper_calls(tmp_path / "none", since, until) == ([], None)

    path = tmp_path / "deck.db"
    db = sqlite3.connect(path)
    db.execute("create table usage_history (account_key, ts, seven_day_pct)")
    db.executemany(
        "insert into usage_history values (?, ?, ?)",
        [
            ("claude_code:main", "2026-11-18T05:30:00+00:00", 90.0),
            ("claude_code:main", "2026-11-18T08:00:00+00:00", 95.0),
            ("claude_code:main", "2026-11-18T08:10:00+00:00", 1.0),  # a new week
            ("claude_code:main", "2026-11-18T19:40:00+00:00", 10.0),
            ("claude_code:main", "2026-11-18T19:45:00+00:00", 9.0),  # rounding, not a week
            ("claude_code:main", "2026-11-18T20:30:00+00:00", 24.0),  # #4488's hours
            ("claude_code:main", "2026-11-18T20:35:00+00:00", None),
            ("claude_code:main", "2026-11-18T18:30:00+00:00", 10.0),
            *(("claude_code:main", f"2026-11-19T{h:02}:00:00+00:00", 24.0) for h in range(12)),
            ("claude_code:main", "2026-11-18T22:00:00+00:00", 24.0),
            ("claude_code:main", "2026-11-25T05:00:00+00:00", 24.0),
            ("claude_code:main", "2026-11-25T05:40:00+00:00", 80.0),  # past the window
            ("claude_code:alt", "2026-11-18T05:00:00+00:00", 10.0),
            ("claude_code:alt", "2026-11-19T05:00:00+00:00", 14.0),
            ("claude_code:alt", "2026-11-25T05:00:00+00:00", 14.0),
            # a new week after a break is seen however high its first reading is
            ("claude_code:break", "2026-11-18T05:00:00+00:00", 95.0),
            ("claude_code:break", "2026-11-25T05:00:00+00:00", 55.0),
            ("claude_code:low", "2026-11-18T05:00:00+00:00", 1.0),
            ("claude_code:low", "2026-11-18T06:00:00+00:00", 0.0),  # rounding, however low
            ("claude_code:low", "2026-11-25T05:00:00+00:00", 1.0),
            ("claude_code:stops", "2026-11-18T05:00:00+00:00", 1.0),
            ("claude_code:stops", "2026-11-24T05:00:00+00:00", 9.0),
            ("claude_code:late", "2026-11-17T05:00:00+00:00", 1.0),
            ("claude_code:late", "2026-11-25T05:00:00+00:00", 9.0),
            ("claude_code:old", "2026-11-18T05:00:00+00:00", 9.0),
            ("codex:codex", "2026-11-18T05:00:00+00:00", 3.0),
        ],
    )
    db.commit()
    db.close()
    assert token_report.meter_rise(str(path), "main", since, until) == 29.0
    for account, rise in {
        "break": 55.0,
        "low": 0.0,
        "stops": None,
        "late": None,
        "old": None,
        "none": None,
    }.items():
        assert token_report.meter_rise(str(path), account, since, until) == rise, account
    assert token_report.meter_rise(str(path), "main", since - 86400, until) is None
    # half an hour with a reading long before it, only before it, or not near its end
    for start, end in (("20:15", "20:45"), ("20:35", "21:05"), ("19:42", "20:12")):
        hours = [token_report._stamp(f"2026-11-18T{at}:00+00:00") for at in (start, end)]
        assert token_report.meter_rise(str(path), "main", *hours) is None, start
    assert token_report.meter_rise(str(path), "main", since - 86400, since - 80000) is None

    def call(at, work="", leg="", stage="build", weight=1):
        made = token_report.Call(token_report._stamp(at), weight, output=unit)
        made.work, made.leg, made.stage = work, leg, stage
        return made

    inside = [
        call("2026-11-18T19:55:00+00:00", "eerovil/agentdeck#4488"),  # the Work's own chat
        call("2026-11-18T20:00:00+00:00", "eerovil/agentdeck#4488", "leg"),
        call("2026-11-18T20:10:00+00:00", stage="one-off test"),
        call("2026-11-18T20:10:00+00:00", weight=2),  # alt's use is in no test's gap
    ]
    lines = token_report.meter_check(str(path), inside, helpers, since, until, 10)
    assert " ".join(lines[0].split()) == (
        "main transcripts 3.0u helpers 1.0u counted 4.0u meter 29.0u gap 25.0u (86.2% of the meter)"
    )
    # alt's percent is already of its own week, as its doubled calls are
    assert "counted    4.0u  meter    4.0u  gap    0.0u (0.0% of the meter)" in lines[1]
    assert "meter   33.0u  gap   25.0u" in lines[2]
    # 14 on the meter in #4488's hours, 4 of them counted
    assert lines[3].startswith("one-off tests 12.0u, left in the totals: 2.0u in transcripts, 10")
    assert "agentdeck#4488" in lines[3]
    assert lines[4] == "per finished Work by the meter 3.300, less the one-off tests 2.100"
    # a window that cuts the test keeps its part of the hours: 14 on the meter, 1 counted
    cut = token_report._stamp("2026-11-18T20:35:00+00:00")
    [*_, part] = token_report.meter_check(str(path), inside[2:3], [], since, cut)
    assert part.startswith("one-off tests 14.0u") and "agentdeck#4488" in part
    early = token_report.meter_check(str(path), [], [], since - 86400, until)
    assert "meter n/a" in early[0] and "meter n/a" in early[2] and len(early) == 4
    # a test with no meter in its hours is not named as if it had used nothing
    with sqlite3.connect(path) as db:
        db.execute("delete from usage_history where ts > '2026-11-18T09'")
    blind = token_report.meter_check(str(path), [], [], since, until)
    assert blind[3].endswith("(the gap in their hours): none") and len(blind) == 4
    assert token_report.model_price("claude-fable-5-1") == 4.0


def test_a_one_off_tests_own_folder_is_no_work(tmp_path, monkeypatch):
    folder = tmp_path / "projects" / f"-home{token_report.TEST_DIR}a"
    folder.mkdir(parents=True)
    at = "2026-11-18T20:00:00.000Z"
    usage = {"input_tokens": 1, "output_tokens": 1}
    reply = {"id": "m", "model": "claude-fable-5-1", "usage": usage, "content": []}
    lines = [
        {"type": "user", "timestamp": at, "message": {"content": "Build a/b#1"}},
        {"type": "assistant", "timestamp": at, "requestId": "r", "message": reply},
    ]
    (folder / "s.jsonl").write_text("\n".join(map(json.dumps, lines)))
    monkeypatch.setattr(token_report, "ACCOUNTS", {"main": (str(tmp_path), 1)})
    monkeypatch.setattr(token_report, "work_of_sessions", lambda _: {"s": [(0.0, "a/b#1")]})
    [[call]] = token_report.load(0.0, float("inf")).values()
    assert (call.stage, call.work, call.price) == ("one-off test", "", 4.0)


def test_the_report_prints_its_own_splits(tmp_path):
    """#4728: the wake kinds, what a call holds and the guide sizes were one-off scripts."""
    stopped = "job: failed, stopped or did not run"
    helpers = "\nExit code: 1\nCommand: agentdeck pre-review-helpers --cwd . --pull a/b#2"
    for outcome, kind in (
        ("finished successfully", "job: pre-review helpers done"),
        ("failed", stopped),  # the command names the helpers whether they ran or not
        ("was cancelled", stopped),
        ("was cut off by a runtime restart after 40 min", stopped),
        ("did not run", stopped),
    ):
        assert token_report.wake_kind("job", f"Your queued job 12 {outcome}.{helpers}") == kind
    passed = "Your queued job 12 finished successfully.\nCommand: pytest -q"
    assert token_report.wake_kind("job", passed) == "job: other passed"
    note = "AgentDeck wake-on: nothing arrived in 30 minutes for the commit status"
    assert token_report.wake_kind("ci_wake", note) == "CI: nothing arrived"
    assert token_report.wake_kind("person", "the review passed clean, was merged") == ""

    def call(turn, work, wake="", tools=(), stage="build", parts=(), **more):
        made = token_report.Call(100.0 + turn, 1, write=30_000, read=10_000, tools=list(tools))
        made.sid, made.turn, made.work, made.wake, made.stage = "s", turn, work, wake, stage
        made.parts = tuple(parts)
        for name, value in more.items():
            setattr(made, name, value)
        return made

    merged = "wake: PR merged"
    calls = [
        call(0, "a/b#1", tools=["Edit"]),  # a person's turn is no wake
        call(1, "a/b#1", merged, gap=7200.0),  # one light turn, its context written cold
        call(2, "a/b#2", merged, tools=["Edit"]),  # an edit is not a light turn
        *[call(3, "a/b#2", stopped) for _ in range(4)],  # nor are four model calls
    ]
    unit = calls[0].units
    header, failed, merge, total = token_report.wake_table(calls, counted=unit * 10)
    assert header.split() == ["kind", "units", "share", "turns", "Works", "u/turn", "light", "cold"]
    assert failed.split()[-7:] == [
        f"{unit * 4:.2f}u",
        "40.0%",
        "1",
        "1",
        f"{unit * 4:.3f}",
        "0%",
        "0%",
    ]
    cold = 30_000 * token_report.WRITE / (unit * 2) * 100
    assert merge.startswith(merged) and merge.split()[-6:] == [
        "20.0%", "2", "2", f"{unit:.3f}", "50%", f"{cold:.0f}%"
    ]  # fmt: skip
    assert total.split()[-6:-2] == ["60.0%", "3", "2", f"{unit * 2:.3f}"]
    assert token_report.wake_table(calls[:1], counted=0.0) == [header]

    # the heaviest Works: units with their helpers' runs, by pull request (#4728)
    db_path = tmp_path / "deck.db"
    db = sqlite3.connect(db_path)
    db.execute("create table session_pull_requests (session_key, repository, number, work_id)")
    db.executemany(
        "insert into session_pull_requests values (?, ?, ?, ?)",
        [
            ("claude_code:main:s", "A/B", 7, "github_issue:A/B#2"),  # the Work it names
            ("claude_code:main:s", "a/b", 8, ""),  # else the one most of the chat's calls were on
            ("claude_code:main:s", "a/b", 9, None),
            ("claude_code:main:gone", "a/b", 10, ""),  # a chat with no call names no Work
            # two chats on one pull request: the Work a registration names wins, in either order
            ("claude_code:main:s", "a/b", 11, ""),
            ("claude_code:main:other", "a/b", 11, "github_issue:a/b#1"),
            ("claude_code:main:other", "a/b", 12, "github_issue:a/b#1"),
            ("claude_code:main:s", "a/b", 12, ""),
        ],
    )
    db.commit()
    pulls = token_report.work_pulls(str(db_path), {("s", ""): calls})
    assert {pulls.pop("a/b#11"), pulls.pop("a/b#12")} == {"a/b#1"}
    assert pulls == {"a/b#7": "a/b#2", "a/b#8": "a/b#2", "a/b#9": "a/b#2"}
    with sqlite3.connect(tmp_path / "bad.db") as bad:  # any other failure is not "no table"
        bad.execute("create table session_pull_requests (session_key)")
    with pytest.raises(sqlite3.OperationalError):
        token_report.work_pulls(str(tmp_path / "bad.db"), {})
    assert token_report.work_pulls(str(tmp_path / "none.db"), {}) == {}
    sqlite3.connect(tmp_path / "old.db").execute("create table works (work_id)")
    assert token_report.work_pulls(str(tmp_path / "old.db"), {}) == {}
    helper = token_report.Call(1.0, 1, write=30_000, read=10_000, pull="a/b#7")
    unread = token_report.Call(1.0, 1, write=30_000, read=10_000, pull="x/y#1")  # no Work's
    head, first, second, foot = token_report.heaviest_works(
        calls, [helper, unread], pulls, counted=unit * 10
    )
    assert head.split() == ["Work", "units", "share", "pulls", "u/pull", "turns", "helpers"]
    assert first.split() == [
        "a/b#2", f"{unit * 6:.2f}u", "60.0%", "3", f"{unit * 2:.2f}", "2", f"{unit:.2f}u"
    ]  # fmt: skip
    assert second.split() == ["a/b#1", f"{unit * 2:.2f}u", "20.0%", "0", "-", "2", "0.00u"]
    assert foot.startswith("these 2 Works") and foot.endswith("0 more Works 0.0u")
    # a reviewer's calls on a Work are its units, as in #4719, but not its turns
    reviewed = [*calls, call(9, "a/b#1", stage="review", sid="reviewer")]
    _, _, with_review, _ = token_report.heaviest_works(reviewed, [], pulls, counted=unit * 10)
    assert with_review.split()[:2] == ["a/b#1", f"{unit * 3:.2f}u"]
    assert with_review.split()[-2] == "2"
    *_, cut = token_report.heaviest_works(calls, [], pulls, counted=unit * 10, limit=1)
    assert f"these 1 Works {unit * 5:.1f}u = 50.0%" in cut
    assert cut.endswith(f"1 more Works {unit * 2:.1f}u")

    # output by what was written (#4754): each column's output, split and summing to it
    def wrote(kind, tokens, output, thought=True, **more):
        return call(0, "a/b#1", wrote=[(kind, tokens)], output=output, thought=thought, **more)

    out = token_report.OUTPUT
    old_helper = token_report.Call(1.0, 1, output=500)
    old_helper.wrote = None
    never_ran = token_report.Call(1.0, 1)  # a did_not_run line: no output, so in neither count
    never_ran.wrote = None
    new_helper = token_report.Call(1.0, 1, output=1000, thought=True)
    new_helper.wrote = [(token_report.TO_PERSON, 300.0), (token_report.OTHER_CALLS, 100.0)]
    head, top, *rest, whole, helped = token_report.output_split(
        [
            wrote(token_report.SHELL, 600.0, 1000),  # 400 left: thinking
            wrote(token_report.CODE, 500.0, 400),  # shown over the output: scaled down to it
            wrote(token_report.POSTED, 50.0, 100, thought=False),  # no thinking: all of it
            wrote(token_report.TO_PERSON, 10.0, 10, leg="agent-1"),
            wrote(token_report.SHELL, 10.0, 10, stage="review"),
            call(0, "", stage="no-work", output=10),  # nothing shown: thinking
        ],
        [old_helper, new_helper, never_ran],
    )
    assert head.split() == ["Work", "chats", "Work", "legs", "reviews", "no", "Work"]
    assert top.startswith(token_report.SHELL) and top.split()[2:4] == [f"{600 * out:.1f}u", "40.0%"]
    by_kind = {line[:44].strip(): line[44:].split() for line in rest}
    assert by_kind[token_report.THOUGHT][1::2] == ["26.7%", "0.0%", "0.0%", "100.0%"]
    assert by_kind[token_report.CODE][1] == "26.7%" and by_kind[token_report.POSTED][1] == "6.7%"
    assert by_kind[token_report.TO_PERSON][3] == "100.0%"
    assert whole.split() == ["all", "output", *[f"{n * out:.1f}u" for n in (1500, 10, 10, 10)]]
    assert helped == (
        f"pre-review helpers, 1 runs: reply {300 * out:.1f}u, tool calls {100 * out:.1f}u,"
        f" thinking {600 * out:.1f}u; 1 runs logged before reply lengths: {500 * out:.1f}u"
    )
    assert len(token_report.output_split([], [])) == len(token_report.WRITTEN) + 2

    # a shell call's heredoc bodies are what they are; the rest of the call is the command
    def kinds(command):
        return {kind for kind, _ in token_report.written("Bash", {"command": command})}

    edit_script = "python3 - <<'EOF'\np.write_text(s)\nEOF\ngit status"
    assert kinds(edit_script) == {token_report.CODE, token_report.SHELL}
    assert kinds("python3 - <<EOF\nprint(1)\nEOF") == {token_report.SCRIPTS, token_report.SHELL}
    assert token_report.POSTED in kinds("gh issue comment 1 --body-file - <<'EOF'\nhello\nEOF")
    assert token_report.POSTED in kinds("agentdeck bookkeep --summary - <<'EOF'\n{}\nEOF")
    assert token_report.POSTED in kinds("cat > /var/tmp/w.Xy/body.md <<'EOF'\ntext\nEOF")
    assert token_report.CODE in kinds("cat > src/a.py <<'EOF'\nx = 1\nEOF")
    assert token_report.CODE in kinds("cat > docs/guide.md <<'EOF'\ntext\nEOF")
    assert token_report.SCRIPTS in kinds("cat > /tmp/probe.py <<'EOF'\nprint(1)\nEOF")
    assert token_report.CODE in kinds("cat > /tmp/fix.py <<'EOF'\np.write_text(s)\nEOF")
    assert kinds("cat <<< 'x'\ngrep -c a <<EOF\nb\nEOF") == {token_report.SHELL}
    # a shift is no heredoc, and neither is an opening that never closes: the lines after
    # it stay the command's own, and a real heredoc later in the call is still found
    assert kinds("python3 -c 'print(1<<3)'\nls -la") == {token_report.SHELL}
    assert kinds("python3 -c 'print(a << b)'\ngit commit -m fix") == {token_report.SHELL}
    assert kinds("python3 - <<'EOF'\nnever closed") == {token_report.SHELL}
    shifted = "echo $((1<<2))\ncat > src/a.py <<'EOF'\nx = 1\nEOF"
    assert kinds(shifted) == {token_report.CODE, token_report.SHELL}
    assert token_report.CODE in kinds("cat > tests/data/fixture.json <<'EOF'\n{}\nEOF")
    assert token_report.POSTED in kinds("cat > /var/tmp/w.Xy/wake.json <<'EOF'\n{}\nEOF")
    whole_file = {"file_path": "/repo/tests/test_a.py", "content": "x"}
    assert token_report.written("Write", whole_file)[0][0] == token_report.CODE
    assert token_report.written("Edit", {})[0][0] == token_report.CODE
    assert token_report.written("Read", {})[0] == (
        token_report.OTHER_CALLS,
        (2 / token_report.JSON_CPT + 20) / token_report.TOOL_FIX,
    )
    whole_call = {"command": edit_script, "description": "d"}
    sized = (len(json.dumps(whole_call)) / token_report.JSON_CPT + 20) / token_report.TOOL_FIX
    assert sum(n for _, n in token_report.written("Bash", whole_call)) == pytest.approx(sized)

    # what a call holds: a Work chat's own calls, averaged, in all stages and stage by stage
    parts = [1000.0] * len(token_report.PARTS)
    held = [
        call(0, "a/b#1", parts=[9000.0, *parts[1:]]),
        call(1, "a/b#1", parts=[3000.0, *parts[1:]], stage="fix"),
        call(2, "a/b#1", parts=parts, leg="agent-1"),  # a subagent leg is not the chat's call
        call(3, "", parts=parts, stage="no-work"),
        call(4, next(iter(token_report.ONE_OFF_TESTS)), parts=parts),
        call(5, "a/b#1"),  # a helper run holds no parts
    ]
    top, standing, *_, whole = token_report.context_split(held)
    assert "all stages (2 calls)" in top and "fix (1 calls)" in top and "release (0 calls)" in top
    assert standing.split()[-8:] == [
        "6.0k",
        "46.2%",
        "9.0k",
        "56.2%",
        "3.0k",
        "30.0%",
        "0.0k",
        "0.0%",
    ]
    assert whole.split()[-8:-6] == ["13.0k", "100.0%"]

    # restarts (#4766): read from a transcript's compact_boundary row onto the next call
    usage = {"cache_creation_input_tokens": 50_000, "cache_read_input_tokens": 1000}
    rows = [
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-23T08:00:00Z",
         "compactMetadata": {"trigger": "auto", "preTokens": 168_000, "postTokens": 11_000}},
        {"type": "user", "isCompactSummary": True, "timestamp": "2026-11-23T08:00:01Z",
         "message": {"content": "s" * 4000}},
        *[{"type": "assistant", "timestamp": "2026-11-23T08:00:02Z", "requestId": f"r{n}",
           "message": {"id": f"m{n}", "usage": usage, "content": []}} for n in (1, 2)],
    ]  # fmt: skip
    path = tmp_path / "restarted.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    first, later = token_report.read_transcript(path, 2, set())[0]
    assert first.after_compact and first.restart == (168_000, round(4000 / token_report.TEXT_CPT))
    assert not later.after_compact and later.restart == ()
    # a resumed chat's copy of that restart, then a new call: the restart is counted once
    new_call = {"type": "assistant", "timestamp": "2026-11-24T08:00:00Z", "requestId": "r3",
                "message": {"id": "m3", "usage": usage, "content": []}}  # fmt: skip
    resumed = tmp_path / "resumed.jsonl"
    resumed.write_text("\n".join(json.dumps(row) for row in [*rows, new_call]))
    seen = set()
    token_report.read_transcript(path, 2, seen)
    (fresh,) = token_report.read_transcript(resumed, 2, seen)[0]
    assert not fresh.after_compact and fresh.restart == ()
    # a boundary row of an unknown shape is a restart of unknown size, not a failed report
    for meta in ("odd", {"preTokens": "many"}, {"preTokens": None}, None):
        odd = tmp_path / "odd.jsonl"
        boundary = rows[0] | {"compactMetadata": meta}
        odd.write_text("\n".join(json.dumps(row) for row in [boundary, *rows[1:]]))
        assert token_report.read_transcript(odd, 2, set())[0][0].restart[0] == 0
    for made in (first, later):
        made.sid, made.work, made.stage = "s", "a/b#1", "build"
    plain = call(0, "a/b#1")  # main, at 100 s; a compaction with no boundary row: no summary
    plain.after_compact = True
    leg = call(0, "a/b#1", leg="agent-1")  # a subagent leg's restart is not the chat's
    leg.after_compact = True
    summary = 2 * (168_000 * token_report.READ + first.restart[1] * token_report.OUTPUT)
    rewrite = 2 * 50_000 * token_report.WRITE
    whole_units = first.units + later.units + plain.units
    head, every, main, alt, epoch, nov23 = token_report.restarts([first, later, plain, leg])
    assert head.split() == ["restarts", "calls", "/100", "summary", "rewrite", "together", "share"]
    plain_rewrite = 30_000 * token_report.WRITE
    together = summary + rewrite + plain_rewrite
    assert every.split() == [
        "all", "2", "3", "66.67", f"{summary:.1f}u", f"{rewrite + plain_rewrite:.1f}u",
        f"{together:.1f}u", f"{together / whole_units * 100:.1f}%",
    ]  # fmt: skip
    assert main.split()[:4] == ["main", "1", "1", "100.00"] and main.split()[4] == "0.0u"
    assert alt.split()[:5] == ["alt", "1", "2", "50.00", f"{summary:.1f}u"]
    assert epoch.split()[:3] == ["1970-01-01", "1", "1"]
    assert nov23.split()[:3] == ["2026-11-23", "1", "2"]
    assert token_report.restarts([])[1].split()[:4] == ["all", "0", "0", "0.00"]

    # the guide text by repository: the chat's own files, worktree and home left out of a name
    chats = {("s", ""): [call(0, "a/b#1")], ("t", ""): [call(0, "a/b#2")], ("u", ""): [call(0, "")]}
    guides = {
        "s": {"files": {"/var/home/me/b/.worktrees/issue-1/AGENTS.md": 7000}, "session": 24_000},
        "t": {"files": {"/home/me/b/.worktrees/issue-2/AGENTS.md": 14_000, "/home/me/x.md": 1}},
        "u": {"files": {"/home/me/elsewhere.md": 5}},  # a chat on no Work
    }
    repo, agents, other = token_report.guide_sizes(chats, guides, 0.0, 1000.0)
    assert (
        repo.split()
        == (
            "a/b chats 2 guide files median 10500 chars = about 3.0k tokens"
            " session guide median 24000 chars"
        ).split()
    )
    assert agents.split() == "~/b/…/AGENTS.md in 2 chats, median 10500 chars".split()
    assert other.split() == "~/x.md in 1 chats, median 1 chars".split()
    assert token_report.guide_sizes(chats, guides, 500.0, 1000.0) == []
