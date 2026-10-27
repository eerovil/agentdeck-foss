import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchfiles import Change

from agentdeck.models import Account, Session, SessionStatus
from agentdeck.providers.claude_code import provider as provider_mod
from agentdeck.providers.claude_code import transcripts
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.state import AppState


def _write(path, lines):
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_trailing_question():
    tq = transcripts.trailing_question
    # Picks the question sentence even when a statement follows.
    assert tq("Sounds good. Should I push both commits? Let me know.") == (
        "Should I push both commits?")
    # Last question wins when there are several.
    assert tq("Do you want A? Or would B be better?") == "Or would B be better?"
    # Newlines are flattened.
    assert tq("Here is the plan.\n\nWant me to proceed with it?") == (
        "Want me to proceed with it?")
    # A bulleted / colon block (no full stops) must not swallow the whole thing
    # into one giant "sentence" — the trailing line is the question.
    recap = "Recap of what shipped:\n- kanban titles\n- colour stripes\n\nWhat's next?"
    assert tq(recap) == "What's next?"
    # No question → None.
    assert tq("All done, pushed to master.") is None
    assert tq("") is None
    assert tq(None) is None
    # Guards: a URL query or a ternary must not register as a question.
    assert tq("Fetching /static/app.css?v=abc now.") is None
    assert tq("The value is a ? b : c in that branch.") is None
    # A markdown-wrapped question (bold/italic/quoted) still registers — the
    # trailing "**", "_", or quote is peeled before judging and off the result.
    assert tq("My question still stands: **how can I help?**") == (
        "My question still stands: **how can I help?")
    assert tq("So, _what should we do next?_") == "So, _what should we do next?"
    assert tq('Confirm: "is this the final answer?"') == 'Confirm: "is this the final answer?'
    # Wrappers must not turn a non-question into one, nor rescue a short fragment.
    assert tq("All shipped and verified.**") is None
    assert tq("ok?**") is None
    # A markdown table cell is documentation the agent wrote about its own work,
    # not a question for you — the sentence split cuts the row after the cell's
    # "?", which used to leave a "question" on a finished chat (#352).
    table = (
        "The markdown enhancer listened to two signals:\n\n"
        "| Signal | Fires on client-side open? |\n"
        "| --- | --- |\n"
        "| `window` `load` | no — the document never navigates |\n\n"
        "Full suite: 1531 passed."
    )
    assert tq(table) is None
    # A row that omits the leading pipe is still a row. A piped fragment is read
    # strictly: missing one odd question only drops back to the session's own
    # attention report, while a false one hijacks every card that follows.
    assert tq("Signal | Fires on client-side open? | no") is None
    assert tq("Checked with `rg question | wc -l`? nothing.") is None
    # A real question after a table still registers.
    assert tq(table + "\n\nShould I merge this now?") == "Should I merge this now?"


def test_recent_conversation_reads_bounded_tail_and_filters_tools(tmp_path):
    path = tmp_path / "chat.jsonl"
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": "old objective"},
        },
        {"type": "progress", "payload": "x" * 2_000},
        {
            "type": "user",
            "message": {"role": "user", "content": "new objective"},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "working on it"},
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "private output"}],
            },
        },
    ]
    _write(path, lines)

    events = transcripts.recent_conversation(path, limit=4, tail=700)

    assert [(event.role, event.text) for event in events] == [
        ("user", "new objective"),
        ("assistant", "working on it"),
    ]


ASK_QUESTION = {
    "type": "assistant",
    "timestamp": "2026-07-03T08:00:02Z",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [
            {
                "type": "tool_use",
                "name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Which database should we use?",
                            "header": "DB",
                            "options": [{"label": "Postgres", "description": "..."}],
                        }
                    ]
                },
            },
        ],
    },
}
ASK_ANSWER = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "tool_result", "content": 'Your questions have been answered: "Postgres".'}
        ],
    },
}


def test_ask_user_question_extracted_as_question(tmp_path):
    """The multiple-choice tool carries its prompt in tool_use input, not a text
    block — it must still surface as the event's question."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASK_QUESTION])
    read = transcripts.read_events(p)
    ev = read.events[-1]
    assert ev.tool_name == "AskUserQuestion"
    assert ev.question == "Which database should we use?"
    # last_event (busy/waiting probe) sees the unanswered question too.
    assert transcripts.last_event(p).question == "Which database should we use?"


def test_ask_user_question_multiple_joined(tmp_path):
    p = tmp_path / "t.jsonl"
    two = json.loads(json.dumps(ASK_QUESTION))
    two["message"]["content"][0]["input"]["questions"].append(
        {"question": "And which cache?", "header": "Cache", "options": []}
    )
    _write(p, [two])
    ev = transcripts.read_events(p).events[-1]
    assert ev.question == "Which database should we use? And which cache?"


def test_answered_question_clears_pending(tmp_path):
    """Once answered, the trailing event is the tool_result — no pending question."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASK_QUESTION, ASK_ANSWER])
    assert transcripts.last_event(p).question is None


ASSISTANT = {
    "type": "assistant",
    "timestamp": "2026-07-03T08:00:01Z",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [
            {"type": "text", "text": "Doing the thing"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
        },
    },
}
USER = {"type": "user", "message": {"role": "user", "content": "hello there"}}
TOOL_RESULT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "tool_result", "content": "file listing"}]},
}


def test_claude_user_image_is_kept_with_text_and_image_only_turn_is_visible(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transcripts, "_MAX_IMAGE_DATA_CHARS", 20)
    p = tmp_path / "t.jsonl"
    media = {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}
    _write(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": media},
                        {"type": "text", "text": "What is this?"},
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "image", "source": media}],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {**media, "data": "a" * 21},
                        },
                        {"type": "text", "text": "Oversized image"},
                    ],
                },
            },
        ],
    )

    first, second, oversized = transcripts.read_events(p).events

    assert first.text == "What is this?"
    assert first.image_media_types == ("image/png",)
    assert second.text is None
    assert second.image_media_types == first.image_media_types
    assert oversized.text == "Oversized image"
    assert oversized.image_media_types == ()
    assert transcripts.transcript_image(p, 1, 0) == ("image/png", b"\x89PNG\r\n\x1a\n")


# The bookkeeping a completed /compact leaves in the transcript.
COMPACT_SUMMARY = {
    "type": "user",
    "isCompactSummary": True,
    "isVisibleInTranscriptOnly": True,
    "message": {"role": "user", "content": "This session is being continued from a previous…"},
}
COMPACT_CMD = {
    "type": "user",
    "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
}
_STDOUT = "<local-command-stdout>Compacted</local-command-stdout>"
COMPACT_STDOUT = {"type": "user", "message": {"role": "user", "content": _STDOUT}}
TASK_NOTIFICATION = {
    "type": "user",
    "message": {
        "role": "user",
        "content": """<task-notification>
<task-id>a5c85ab59b8b7af55</task-id>
<tool-use-id>toolu_01FC1FQ9Kc5PowdGkVwvTbxu</tool-use-id>
<output-file>/tmp/claude/tasks/a5c85ab59b8b7af55.output</output-file>
<status>completed</status>
<summary>Agent \"Translate battle_message 192-281\" finished</summary>
<note>This task may notify more than once.</note>
<result>89 lines changed out of the 90-line range.</result>
</task-notification>""",
    },
}


def _enqueue(text):
    return {"type": "queue-operation", "operation": "enqueue", "content": text}


def test_read_events_parses_roles(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, TOOL_RESULT])
    read = transcripts.read_events(p)
    assert [e.role for e in read.events] == ["user", "assistant", "tool"]
    a = read.events[1]
    assert a.text == "Doing the thing"
    assert a.tool_name == "Bash"
    assert "ls -la" in a.tool_summary
    assert a.model == "claude-opus-4-8"


def test_claude_tool_calls_carry_expandable_full_input(tmp_path):
    """Claude tool calls expand like Codex ones: the whole command, not the peek."""
    command = "cd /var/home/me/agentdeck && " + " && ".join(
        f"rg -n 'pattern {index}' src tests" for index in range(12)
    )
    assert len(command) > transcripts._MAX_TOOL_SUMMARY
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": command,
                                "description": "Search for the pattern",
                                "run_in_background": False,
                            },
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "TodoWrite", "input": {}}],
                },
            },
        ],
    )

    events = transcripts.read_events(p).events
    bash, empty_input = events[0], events[1]
    # The peek stays short; the expandable detail holds the untruncated command.
    assert len(bash.tool_summary) <= transcripts._MAX_TOOL_SUMMARY
    assert command not in bash.tool_summary
    assert bash.tool_summary == "Search for the pattern"
    assert bash.tool_detail.startswith(f"Command\n{command}")
    # Secondary fields follow, labelled, so the expansion opens on the point.
    assert "Description\nSearch for the pattern" in bash.tool_detail
    assert "Run in background\nfalse" in bash.tool_detail
    # A tool call with no input has nothing to expand.
    assert empty_input.tool_detail is None


def _tool_use_line(name: str, input_: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": name, "input": input_}],
        },
    }


def test_tool_peek_prefers_the_call_description(tmp_path):
    """Issue #581: collapsed rows read as intent, not the same truncated `cd …`."""
    p = tmp_path / "t.jsonl"
    long_description = "Read CI status once " * 20
    shell = "cd /home/me/agentdeck && gh pr checks 580"
    _write(
        p,
        [
            _tool_use_line("Bash", {"command": shell, "description": "Read CI status once"}),
            _tool_use_line("Bash", {"command": "ls -la"}),
            _tool_use_line(
                "Bash", {"command": "ls -la", "description": "  \n Check the lease \n more "}
            ),
            _tool_use_line("Bash", {"command": "ls -la", "description": "   "}),
            _tool_use_line("Bash", {"command": "ls -la", "description": long_description}),
            _tool_use_line(
                "Agent", {"prompt": "a very long subagent brief", "description": "Map the peek"}
            ),
        ],
    )

    described, plain, multiline, blank, long_, agent = transcripts.read_events(p).events
    # The description replaces the command outright — no `description: ` prefix,
    # because the peek is prose, not a field dump.
    assert described.tool_summary == "Read CI status once"
    # Without one, the command still identifies the row.
    assert plain.tool_summary == "command: ls -la"
    assert multiline.tool_summary == "Check the lease"
    assert blank.tool_summary == "command: ls -la"
    assert long_.tool_summary == long_description.strip()[: transcripts._MAX_TOOL_SUMMARY]
    assert agent.tool_summary == "Map the peek"
    # The full command stays one expansion away.
    assert described.tool_detail.startswith(f"Command\n{shell}")


def test_task_notification_that_opens_a_turn_is_a_one_line_notice(tmp_path):
    """Issue #3377: hidden, it left no boundary between the reply to the person and the
    automatic reply after it. It is a notice, never the person's prompt."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, TASK_NOTIFICATION])

    events = transcripts.read_events(p).events
    meta = transcripts.transcript_meta(p)

    notice = events[-1]
    assert [event.role for event in events] == ["user", "assistant", "system"]
    assert notice.notice == 'Agent "Translate battle_message 192-281" finished'
    assert notice.text == "89 lines changed out of the 90-line range."
    assert notice.turn_continues is True  # the automatic turn is open
    assert transcripts.last_event(p).notice == notice.notice
    assert meta.last_prompt == "hello there"


def test_task_notification_after_empty_text_block_is_a_notice(tmp_path):
    p = tmp_path / "t.jsonl"
    notification = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": ""},
                {
                    "type": "text",
                    "text": TASK_NOTIFICATION["message"]["content"],
                }
            ],
        },
    }
    _write(p, [USER, notification])

    events = transcripts.read_events(p).events
    meta = transcripts.transcript_meta(p)

    assert [(event.role, event.notice) for event in events] == [
        ("user", None),
        ("system", 'Agent "Translate battle_message 192-281" finished'),
    ]
    assert meta.last_prompt == "hello there"


def test_truncated_task_notification_is_a_notice_without_detail(tmp_path):
    p = tmp_path / "t.jsonl"
    truncated = {
        "type": "user",
        "message": {
            "role": "user",
            "content": "<task-notification>\n<status>completed</status>",
        },
    }
    _write(p, [USER, truncated])

    events = transcripts.read_events(p).events
    meta = transcripts.transcript_meta(p)

    assert [(event.role, event.text, event.notice) for event in events] == [
        ("user", "hello there", None),
        ("system", None, "Background task finished"),
    ]
    assert meta.last_prompt == "hello there"


def test_enqueued_task_notification_is_hidden_as_protocol_noise(tmp_path):
    """Issue #336: the CLI queues its own task notifications through the same
    queue-operation the user's typed-while-busy message uses. The processed
    ``user`` line was already hidden, so rendering the enqueue put the raw
    protocol block in the chat as *your* message — and on the card as your
    latest prompt."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _enqueue(TASK_NOTIFICATION["message"]["content"]), ASSISTANT])

    events = transcripts.read_events(p).events
    meta = transcripts.transcript_meta(p)

    assert [(event.role, event.queued) for event in events] == [
        ("user", False),
        ("assistant", False),
    ]
    assert meta.last_prompt == "hello there"


def test_queued_command_attachment_task_notification_is_hidden(tmp_path):
    """The same notification is also delivered into an open turn as a
    ``queued_command`` attachment, which rendered a second user bubble."""
    p = tmp_path / "t.jsonl"
    attachment = {
        "type": "attachment",
        "attachment": {
            "type": "queued_command",
            "prompt": TASK_NOTIFICATION["message"]["content"],
        },
    }
    _write(p, [USER, attachment, ASSISTANT])

    events = transcripts.read_events(p).events

    assert [(event.role, event.queued) for event in events] == [
        ("user", False),
        ("assistant", False),
    ]


def test_enqueued_slash_command_echo_is_hidden(tmp_path):
    """Local-command bookkeeping is noise in the queued envelope too."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _enqueue(_STDOUT), ASSISTANT])

    events = transcripts.read_events(p).events
    meta = transcripts.transcript_meta(p)

    assert [event.role for event in events] == ["user", "assistant"]
    assert meta.last_prompt == "hello there"


def test_agent_slash_command_is_normalized_for_delivery_reconciliation(tmp_path):
    """#965: a skill command is durable user input, despite Claude's tagged echo.

    Hiding that line left the dashboard's optimistic message waiting forever,
    even while Claude was visibly doing the requested review.
    """
    p = tmp_path / "t.jsonl"
    command = {
        "type": "user",
        "timestamp": "2026-08-12T04:11:39.984Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "<command-message>review-fix-pr</command-message>\n"
                        "<command-name>/review-fix-pr</command-name>\n"
                        "<command-args>high 123</command-args>"
                    ),
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "iVBORw0KGgo=",
                    },
                },
            ],
        },
    }
    _write(p, [command])

    event = transcripts.read_events(p).events[0]
    latest = transcripts.last_event(p)
    meta = transcripts.transcript_meta(p)

    assert (event.role, event.text, event.queued) == (
        "user",
        "/review-fix-pr high 123",
        False,
    )
    assert event.image_media_types == ("image/png",)
    assert (latest.role, latest.text) == ("user", "/review-fix-pr high 123")
    assert (meta.last_prompt, meta.last_role) == ("/review-fix-pr high 123", "user")


def test_command_markup_in_user_prose_is_not_interpreted_as_protocol(tmp_path):
    p = tmp_path / "t.jsonl"
    prose = (
        "Claude wrote <command-message>review-fix-pr</command-message> and "
        "<command-name>/review-fix-pr</command-name> in the transcript."
    )
    _write(p, [{"type": "user", "message": {"role": "user", "content": prose}}])

    event = transcripts.read_events(p).events[0]

    assert event.text == prose


def test_incomplete_command_envelope_at_start_remains_user_prose(tmp_path):
    p = tmp_path / "t.jsonl"
    prose = (
        "<command-message>not-actually-run</command-message>\n"
        "<command-name>/not-actually-run</command-name>\n"
        "This is a pasted example, not a CLI envelope."
    )
    _write(p, [{"type": "user", "message": {"role": "user", "content": prose}}])

    assert transcripts.read_events(p).events[0].text == prose


@pytest.mark.parametrize(
    ("message", "name"),
    [
        ("not-a-command", "not-a-command"),
        ("", "/"),
        ("double", "//double"),
        ("/double", "//double"),
        ("two words", "/two words"),
        ("<tag>", "/<tag>"),
    ],
)
def test_malformed_command_names_remain_user_prose(tmp_path, message, name):
    p = tmp_path / "t.jsonl"
    prose = (
        f"<command-message>{message}</command-message>\n"
        f"<command-name>{name}</command-name>"
    )
    _write(p, [{"type": "user", "message": {"role": "user", "content": prose}}])

    assert transcripts.read_events(p).events[0].text == prose


def test_local_slash_command_without_command_message_remains_hidden(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, COMPACT_CMD])

    assert [(event.role, event.text) for event in transcripts.read_events(p).events] == [
        ("user", "hello there")
    ]


def test_claude_stop_reason_preserves_quiet_tool_use_turn(tmp_path):
    """Issue #94: assistant prose can be followed by a quiet tool-use gap.

    Claude declares that continuation with ``stop_reason=tool_use`` even when
    the renderable line contains only text, so the card must not mistake that
    prose for a terminal reply after the short write-recency window expires.
    """
    p = tmp_path / "t.jsonl"
    progress_at = datetime.now(UTC) - timedelta(seconds=60)
    continuing = {
        "type": "assistant",
        "timestamp": progress_at.isoformat(),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "I found the file."}],
            "stop_reason": "tool_use",
        },
    }
    _write(p, [USER, continuing])

    event = transcripts.last_event(p)

    assert event.turn_continues is True
    assert ClaudeCodeProvider()._activity(
        True, p, progress_at
    ) == "Working"


def test_claude_terminal_stop_reason_closes_turn_immediately(tmp_path):
    p = tmp_path / "t.jsonl"
    terminal = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Done."}],
            "stop_reason": "end_turn",
        },
    }
    _write(p, [USER, terminal])

    event = transcripts.last_event(p)

    assert event.turn_continues is False
    assert ClaudeCodeProvider()._activity(True, p, datetime.now(UTC)) is None

    terminal["message"]["stop_reason"] = "future_reason"
    _write(p, [USER, terminal])
    assert transcripts.last_event(p).turn_continues is None


def _assistant_msg(text):
    return {
        "type": "assistant",
        "timestamp": "2026-07-03T08:00:01Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_long_assistant_output_renders_in_full(tmp_path):
    # Issue #38: a full agent response must render, not be cut at the old
    # 4000-char cap (which is now only for short list previews).
    p = tmp_path / "t.jsonl"
    long_text = "x" * 50_000
    _write(p, [_assistant_msg(long_text)])
    ev = transcripts.read_events(p).events[-1]
    assert ev.text == long_text  # not truncated


def test_rendered_text_is_still_bounded(tmp_path):
    # ...but a pathological multi-MB message is still capped, to bound payloads.
    p = tmp_path / "t.jsonl"
    _write(p, [_assistant_msg("y" * 300_000)])
    ev = transcripts.read_events(p).events[-1]
    assert len(ev.text) == transcripts._MAX_RENDERED_TEXT


def test_queued_message_rendered_as_user(tmp_path):
    """A message typed while busy (queue-operation/enqueue) that never became a
    real user turn still shows, flagged as queued."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _enqueue("sent while you were working"), ASSISTANT])
    read = transcripts.read_events(p)
    queued = [e for e in read.events if e.queued]
    assert len(queued) == 1
    assert queued[0].role == "user"
    assert queued[0].text == "sent while you were working"


def test_queued_duplicate_deduped(tmp_path):
    """If the enqueued message was later processed as a real user turn, only the
    real turn is kept (no double render); non-enqueue ops are ignored."""
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            _enqueue("do the thing"),
            {"type": "queue-operation", "operation": "dequeue"},
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        ],
    )
    read = transcripts.read_events(p)
    texts = [(e.text, e.queued) for e in read.events]
    assert texts == [("do the thing", False)]  # the real turn, once


def test_read_events_skips_malformed_and_meta(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(USER) + "\n" + "{ broken\n" + json.dumps({"type": "mode", "mode": "x"}) + "\n"
    )
    read = transcripts.read_events(p)
    assert read.skipped == 1
    assert [e.role for e in read.events] == ["user"]  # mode line yields nothing


def test_read_events_incremental_and_partial_trailing(tmp_path):
    p = tmp_path / "t.jsonl"
    # two complete lines + a partial (no trailing newline)
    p.write_text(json.dumps(USER) + "\n" + json.dumps(ASSISTANT) + "\n" + '{"type":"user","mess')
    read1 = transcripts.read_events(p)
    assert len(read1.events) == 2
    # the partial line must not have been consumed
    # complete the file and resume from the cursor
    with p.open("a") as f:
        f.write('age":{"role":"user","content":"more"}}\n')
    read2 = transcripts.read_events(p, byte_offset=read1.byte_offset, seq=read1.seq)
    assert [e.text for e in read2.events] == ["more"]
    assert read2.events[0].seq == 3  # seq continued past the first two lines


async def test_large_transcript_cursor_and_detail_run_in_thread(tmp_path, monkeypatch):
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    path = project / "large.jsonl"
    lines = [
        {
            "type": "user",
            "message": {"role": "user", "content": f"message {index} " + "x" * 1024},
        }
        for index in range(1200)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + '{"partial":')

    direct_cursor = transcripts.transcript_cursor(path, chunk_size=4096)
    assert direct_cursor == (path.stat().st_size - len('{"partial":'), 1200)

    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    session = Session("claude_code:test:large", account.key, "large", SessionStatus.IDLE)
    direct_detail = provider._load_transcript_file(root, "large", path, None)
    calls: list[str] = []
    original_to_thread = provider_mod.asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(provider_mod.asyncio, "to_thread", tracking_to_thread)
    assert await provider.transcript_cursor(account, session) == direct_cursor
    direct_events = [event for event in transcripts.read_events(path).events if event.seq > 1198]
    assert await provider.read_transcript(account, session, after_seq=1198) == direct_events
    detail = await provider.load_transcript(account, session)
    assert detail == direct_detail
    assert len(detail.events) == 400
    assert detail.total_events == 1200
    assert calls == ["transcript_cursor", "_read_transcript_file", "_load_transcript_file"]


async def test_last_activity_survives_a_post_hoc_transcript_rewrite(tmp_path):
    """Issue #335: Claude Code rewrites a finished transcript's bookkeeping lines
    hours after the last turn. That write carries no timestamp and is not chat
    activity, so it must not relabel a night-old card "1m ago", refloat it to the
    top of the deck, or move the marker a dismissal is compared against."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    path = project / "33333333-3333-3333-3333-333333333333.jsonl"
    last_turn = datetime.now(UTC) - timedelta(hours=6)
    _write(
        path,
        [
            USER,
            {
                "type": "assistant",
                "timestamp": last_turn.isoformat().replace("+00:00", "Z"),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Shipped it."}],
                    "stop_reason": "end_turn",
                },
            },
        ],
    )
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    before = (await provider.scan_sessions(account))[0]
    assert before.last_activity == last_turn

    # The rewrite: a timestamp-less index line appended now, mtime moved to now.
    with path.open("a") as handle:
        handle.write(json.dumps({"type": "last-prompt", "leafUuid": "x"}) + "\n")

    after = (await provider.scan_sessions(account))[0]
    assert after.last_activity == last_turn  # not "just now"
    assert after.last_activity == before.last_activity  # a dismissal still holds


async def test_known_claude_transcript_change_skips_the_account_inventory(
    tmp_path, monkeypatch
):
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "34343434-3434-3434-3434-343434343434"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Incremental reply."}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )
    monkeypatch.setattr(
        provider_mod,
        "_scan_inventory",
        lambda _root: pytest.fail("incremental scan walked the account inventory"),
    )

    changed = await provider.scan_changed_sessions(
        account, frozenset({(Change.modified, path)}), state
    )

    assert changed is not None
    assert [session.session_id for session in changed] == [sid]
    assert changed[0].last_text == "Incremental reply."


async def test_an_append_that_did_not_move_the_mtime_is_still_read(tmp_path):
    """Not every filesystem stamps a file per write (#1368).

    Some advance mtime on a clock tick instead, so a reply appended a moment
    after the last one carries the timestamp the previous parse already saw. A
    cache keyed on mtime alone then answers with the earlier text, and the deck
    shows a chat's previous message as its latest — which is exactly what a
    dev-cluster pod's overlay filesystem did to this suite. Pinned here with
    `os.utime` because the workstation's own filesystem is too fine to show it.
    """
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "35353535-3535-3535-3535-353535353535"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    before = path.stat()

    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Same-tick reply."}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    changed = await provider.scan_changed_sessions(
        account, frozenset({(Change.modified, path)}), state
    )

    assert changed is not None
    assert changed[0].last_text == "Same-tick reply."


def _append_reply(path, text):
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )


async def test_a_coalesced_claude_batch_still_refreshes_incrementally(
    tmp_path, monkeypatch
):
    """Issue #1487: the scan loop coalesces everything that arrives during its
    interval, so a real batch is several chats writing plus the CLI's own
    ``history.jsonl``. Bailing on the first entry this path could not attribute
    meant the incremental path essentially never ran."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    first_sid = "36363636-3636-3636-3636-363636363636"
    second_sid = "37373737-3737-3737-3737-373737373737"
    first = project / f"{first_sid}.jsonl"
    second = project / f"{second_sid}.jsonl"
    _write(first, [USER, ASSISTANT])
    _write(second, [USER, ASSISTANT])
    history = root / "history.jsonl"
    history.write_text("{}\n")
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    _append_reply(first, "First reply.")
    _append_reply(second, "Second reply.")
    monkeypatch.setattr(
        provider_mod,
        "_scan_inventory",
        lambda _root: pytest.fail("a coalesced batch walked the account inventory"),
    )

    changed = await provider.scan_changed_sessions(
        account,
        frozenset(
            {
                (Change.modified, first),
                (Change.modified, second),
                (Change.modified, history),
            }
        ),
        state,
    )

    assert changed is not None
    assert {session.session_id for session in changed} == {first_sid, second_sid}
    assert {session.last_text for session in changed} == {
        "First reply.",
        "Second reply.",
    }


async def test_a_claude_batch_of_only_source_noise_merges_nothing(tmp_path, monkeypatch):
    """A ``history.jsonl`` write cannot add, remove or re-rank a session, so it
    must not cost a full scan either (#1487). The slow reconciliation still runs
    on its own cadence."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    _write(project / "38383838-3838-3838-3838-383838383838.jsonl", [USER, ASSISTANT])
    history = root / "history.jsonl"
    history.write_text("{}\n")
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    monkeypatch.setattr(
        provider_mod,
        "_scan_inventory",
        lambda _root: pytest.fail("source noise walked the account inventory"),
    )

    assert (
        await provider.scan_changed_sessions(
            account, frozenset({(Change.modified, history)}), state
        )
        == []
    )


async def test_a_claude_registry_membership_change_requires_a_full_scan(tmp_path):
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    _write(project / "known.jsonl", [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    registry_entry = root / "sessions" / "new-session.json"
    registry_entry.parent.mkdir()
    registry_entry.write_text("{}")

    assert (
        await provider.scan_changed_sessions(
            account, frozenset({(Change.added, registry_entry)}), state
        )
        is None
    )


async def test_a_full_claude_scan_parses_no_transcript_on_the_event_loop(
    tmp_path, monkeypatch
):
    """Issue #1487: ``py-spy`` put 86% of the web worker's event-loop time in the
    scan, and the hottest self-time frame in the process was JSON decoding. The
    inventory and spend already ran in a thread; every other per-session read did
    not. Nothing here asserts speed — only that the loop thread does no parsing."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    subagents = project / "39393939-3939-3939-3939-393939393939" / "subagents"
    subagents.mkdir(parents=True)
    _write(project / "39393939-3939-3939-3939-393939393939.jsonl", [USER, ASSISTANT])
    for index in range(2):
        _write(
            subagents / f"agent-over-cap-{index}.jsonl",
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "message": {"role": "user", "content": f"Subagent {index}"},
                },
                ASSISTANT,
            ],
        )
    # One root plus two subagents exceeds every per-transcript cache's cap.
    # Prewarming without carrying the values into the build would evict the
    # root before the event-loop pass and parse it again there.
    monkeypatch.setattr(provider_mod, "SCAN_CACHE_ENTRIES", 2)
    loop_thread = threading.current_thread()
    offenders: list[str] = []

    def watched(name, real):
        def wrapper(path, *args, **kwargs):
            if threading.current_thread() is loop_thread:
                offenders.append(name)
            return real(path, *args, **kwargs)

        return wrapper

    for name in ("transcript_meta", "last_event", "last_context_tokens", "task_ledger"):
        monkeypatch.setattr(transcripts, name, watched(name, getattr(transcripts, name)))
    monkeypatch.setattr(
        provider_mod,
        "_subagent_task",
        watched("_subagent_task", provider_mod._subagent_task),
    )

    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    workers = MagicMock()
    workers.available = False
    workers.owned_session_ids.return_value = {
        "39393939-3939-3939-3939-393939393939"
    }
    workers.owns.return_value = True
    workers.live.return_value = False
    workers.cwd_for.return_value = None
    workers.turn_active.return_value = False
    workers.effective_activity.return_value = False
    workers.turn_cut_off.return_value = False
    workers.turn_cut_off_at.return_value = 0.0
    workers.turn_started_at.return_value = None
    workers.revival_due.return_value = False
    provider._workers[account.key] = workers

    sessions = await provider.scan_sessions(account)

    assert len(sessions) == 3
    assert offenders == []


async def test_a_replacement_worker_reuses_the_full_claude_scan_parse(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    subagents = project / "39393939-3939-3939-3939-393939393939" / "subagents"
    subagents.mkdir(parents=True)
    _write(project / "39393939-3939-3939-3939-393939393939.jsonl", [USER, ASSISTANT])
    _write(
        subagents / "agent-shared-cache.jsonl",
        [
            {
                "type": "user",
                "isSidechain": True,
                "message": {"role": "user", "content": "Shared cache task"},
            },
            ASSISTANT,
        ],
    )

    calls: dict[str, int] = {}

    def counted(name, real):
        def wrapper(path, *args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return real(path, *args, **kwargs)

        return wrapper

    for name in (
        "transcript_meta",
        "last_event",
        "last_context_tokens",
        "task_ledger",
    ):
        monkeypatch.setattr(
            transcripts, name, counted(name, getattr(transcripts, name))
        )
    monkeypatch.setattr(
        provider_mod,
        "_subagent_task",
        counted("_subagent_task", provider_mod._subagent_task),
    )

    account = Account("claude_code:test", "claude_code", "test", root)
    first = await ClaudeCodeProvider().scan_sessions(account)
    assert set(calls) == {
        "transcript_meta",
        "last_event",
        "last_context_tokens",
        "task_ledger",
        "_subagent_task",
    }
    calls.clear()

    second = await ClaudeCodeProvider().scan_sessions(account)

    assert calls == {}, f"a replacement worker re-parsed Claude history: {calls}"
    assert [session.key for session in second] == [session.key for session in first]


async def test_unknown_claude_transcript_requires_a_full_scan(tmp_path):
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    known = project / "known.jsonl"
    _write(known, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    state = AppState()
    state.replace_account_sessions(account.key, await provider.scan_sessions(account))
    unknown = project / "unknown.jsonl"
    _write(unknown, [USER, ASSISTANT])

    assert (
        await provider.scan_changed_sessions(
            account, frozenset({(Change.added, unknown)}), state
        )
        is None
    )


async def test_transcript_path_is_resolved_without_re_walking_projects(
    tmp_path, monkeypatch
):
    """Issue #737: the liveness sweep resolves every live session's transcript
    every few seconds, on the event loop, and the subagent pattern has to list
    each project directory to answer. On a real account that is tens of thousands
    of dirents per sweep, which is what makes opening a session wait."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "44444444-4444-4444-4444-444444444444"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    (session,) = await provider.scan_sessions(account)

    # The composer prices the next message from the normalized Session. Claude's
    # transcript model used to stay trapped in detail parsing, leaving this empty.
    assert session.model == "claude-opus-4-8"

    walks = 0
    real_glob = Path.glob

    def counting_glob(self, pattern, *args, **kwargs):
        nonlocal walks
        walks += 1
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting_glob)
    assert provider._transcript_path(account, session) == path
    assert provider._transcript_path(account, session) == path
    assert walks == 0  # the scan's own listing already answered this

    # A transcript that disappears must not stay cached.
    path.unlink()
    assert provider._transcript_path(account, session) is None
    assert walks > 0


async def test_the_sweep_does_not_re_walk_projects_for_a_missing_transcript(
    tmp_path, monkeypatch
):
    """Issue #1325: #737 fixed the *hit*, but a live session whose transcript
    cannot be found still paid two full project-tree walks on every sweep, for
    ever, because a failed resolve was never remembered. On this host that was
    13.9 ms of walking per missing session per sweep."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "55555555-5555-5555-5555-555555555555"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    (session,) = await provider.scan_sessions(account)

    path.unlink()  # the transcript goes away; the session is still swept
    walks = 0
    real_glob = Path.glob

    def counting_glob(self, pattern, *args, **kwargs):
        nonlocal walks
        walks += 1
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting_glob)

    assert provider._transcript_path(account, session, tolerate_stale_miss=True) is None
    first = walks
    assert first > 0  # the first sweep still has to look

    for _ in range(5):
        assert (
            provider._transcript_path(account, session, tolerate_stale_miss=True)
            is None
        )
    assert walks == first  # every later sweep is free


async def test_a_user_facing_read_never_reuses_a_remembered_miss(tmp_path):
    """The sweep tolerates a stale miss; opening a chat must not. A transcript
    that appears right after a failed sweep resolve has to be visible at once,
    not up to MISSING_TRANSCRIPT_TTL_S later."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "77777777-7777-7777-7777-777777777777"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    (session,) = await provider.scan_sessions(account)

    path.unlink()
    assert provider._transcript_path(account, session, tolerate_stale_miss=True) is None

    _write(path, [USER, ASSISTANT])  # it comes back
    assert provider._transcript_path(account, session) == path


async def test_a_scan_clears_a_remembered_miss(tmp_path):
    """The authoritative scan re-seeds the path cache from the listing it already
    built, so the sweep must stop trusting its remembered miss straight away."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sid = "88888888-8888-8888-8888-888888888888"
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    (session,) = await provider.scan_sessions(account)

    path.unlink()
    assert provider._transcript_path(account, session, tolerate_stale_miss=True) is None
    assert provider._missing_paths

    _write(path, [USER, ASSISTANT])
    (session,) = await provider.scan_sessions(account)
    assert not provider._missing_paths
    assert provider._transcript_path(account, session, tolerate_stale_miss=True) == path


async def test_the_path_cache_does_not_cross_config_roots(tmp_path):
    """The provider is a process-wide singleton, so the cache has to be keyed by
    the directory a session id resolves against — not by an account label two
    roots could share."""
    sid = "66666666-6666-6666-6666-666666666666"
    provider = ClaudeCodeProvider()
    project = tmp_path / "one" / "projects" / "-tmp"
    project.mkdir(parents=True)
    path = project / f"{sid}.jsonl"
    _write(path, [USER, ASSISTANT])
    (tmp_path / "two" / "projects").mkdir(parents=True)

    one = Account("claude_code:test", "claude_code", "test", tmp_path / "one")
    (session,) = await provider.scan_sessions(one)
    assert provider._transcript_path(one, session) == path

    # The other root has no such transcript, and must not inherit this one's.
    two = Account("claude_code:test", "claude_code", "test", tmp_path / "two")
    assert provider._transcript_path(two, session) is None


async def test_a_relocated_transcript_is_repicked_by_the_next_scan(tmp_path):
    """The same session id appears under a second project slug when its cwd
    moves; the newest file wins, and the cached path must follow it."""
    root = tmp_path / "cfg"
    old_project = root / "projects" / "-tmp-old"
    old_project.mkdir(parents=True)
    sid = "55555555-5555-5555-5555-555555555555"
    old = old_project / f"{sid}.jsonl"
    _write(old, [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    (session,) = await provider.scan_sessions(account)
    assert provider._transcript_path(account, session) == old

    new_project = root / "projects" / "-tmp-new"
    new_project.mkdir(parents=True)
    new = new_project / f"{sid}.jsonl"
    _write(new, [USER, ASSISTANT])
    os.utime(old, (0, 0))

    (session,) = await provider.scan_sessions(account)
    assert provider._transcript_path(account, session) == new


def test_token_totals(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [ASSISTANT, ASSISTANT])
    read = transcripts.read_events(p)
    tot = transcripts.token_totals(read.events)
    assert tot.input_tokens == 200
    assert tot.output_tokens == 40
    assert tot.cache_read_tokens == 10
    assert tot.total == 256


def test_transcript_meta_keeps_the_latest_claude_model(tmp_path):
    p = tmp_path / "t.jsonl"
    newer = {
        **ASSISTANT,
        "message": {**ASSISTANT["message"], "model": "claude-sonnet-5"},
    }
    _write(p, [ASSISTANT, newer])

    assert transcripts.transcript_meta(p).model == "claude-sonnet-5"


def test_last_context_tokens_uses_latest_usage_block(tmp_path):
    # context = input + cache_read + cache_creation of the *most recent* usage
    # block — not summed over the transcript. A later, bigger turn wins, and a
    # trailing tool-result line (no usage) doesn't reset it.
    bigger = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": "more"}],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_input_tokens": 40000,
                "cache_creation_input_tokens": 200,
            },
        },
    }
    p = tmp_path / "t.jsonl"
    _write(p, [ASSISTANT, bigger, TOOL_RESULT])
    assert transcripts.last_context_tokens(p) == 41200  # 1000 + 40000 + 200


def test_last_context_tokens_none_without_usage(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, TOOL_RESULT])
    assert transcripts.last_context_tokens(p) is None
    assert transcripts.last_context_tokens(tmp_path / "missing.jsonl") is None


def test_last_context_tokens_reset_by_compaction(tmp_path):
    # After /compact the last usage block predates the compaction and no longer
    # reflects the (now much smaller) context — report unknown, not the old size.
    p = tmp_path / "t.jsonl"
    _write(p, [ASSISTANT, COMPACT_SUMMARY, COMPACT_CMD, COMPACT_STDOUT])
    assert transcripts.last_context_tokens(p) is None


def test_last_context_tokens_after_post_compact_turn(tmp_path):
    # A real turn after the compaction produces a fresh usage block → shown again.
    post = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "back"}],
                    "usage": {"input_tokens": 300, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0}},
    }
    p = tmp_path / "t.jsonl"
    _write(p, [ASSISTANT, COMPACT_SUMMARY, post])
    assert transcripts.last_context_tokens(p) == 300


def test_last_event_idle_after_compact(tmp_path):
    # A tool_result left open, then /compact: the agent is idle afterwards, so the
    # open-turn probe must NOT read the tool_result as an in-progress turn.
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, TOOL_RESULT, COMPACT_SUMMARY, COMPACT_CMD, COMPACT_STDOUT])
    assert transcripts.last_event(p) is None


def test_read_events_skips_compact_and_command_noise(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, COMPACT_SUMMARY, COMPACT_CMD, COMPACT_STDOUT])
    read = transcripts.read_events(p)
    # only the real user + assistant render; compaction/command noise is dropped
    assert [e.role for e in read.events] == ["user", "assistant"]


def test_transcript_meta_resets_stale_prompt_after_compact(tmp_path):
    stale = {"type": "last-prompt", "lastPrompt": "the original prompt"}
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, stale, COMPACT_SUMMARY, COMPACT_CMD, COMPACT_STDOUT, stale])
    meta = transcripts.transcript_meta(p)
    assert meta.last_prompt is None  # stale pre-compact prompt (and bookkeeping) dropped
    assert meta.last_text is None  # stale pre-compact reply dropped
    assert meta.last_role is None
    assert meta.first_prompt == "hello there"  # retained for title / kanban resolution


def test_transcript_meta_keeps_real_prompt_after_compact(tmp_path):
    fresh = {"type": "user", "message": {"role": "user", "content": "fresh question"}}
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, COMPACT_SUMMARY, COMPACT_CMD, fresh])
    meta = transcripts.transcript_meta(p)
    assert meta.last_prompt == "fresh question"  # a genuine post-compact prompt is current
    assert meta.last_role == "user"


def test_last_model(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT])
    read = transcripts.read_events(p)
    assert transcripts.last_model(read.events) == "claude-opus-4-8"


def test_load_todos(tmp_path):
    cfg = tmp_path / "cfg"
    tdir = cfg / "tasks" / "sid-1"
    tdir.mkdir(parents=True)
    (tdir / "0.json").write_text(json.dumps({"subject": "do X", "status": "in_progress"}))
    (tdir / "1.json").write_text(json.dumps([{"subject": "do Y", "status": "completed"}]))
    todos = transcripts.load_todos(cfg, "sid-1")
    assert {t["subject"] for t in todos} == {"do X", "do Y"}


def test_load_todos_missing(tmp_path):
    assert transcripts.load_todos(tmp_path, "nope") == []


def test_transcript_meta_prefers_ai_title(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {"type": "ai-title", "aiTitle": "Fix the login bug", "sessionId": "s"},
            {"type": "last-prompt", "lastPrompt": "now add a test", "sessionId": "s"},
            USER,
        ],
    )
    meta = transcripts.transcript_meta(p)
    assert meta.title == "Fix the login bug"
    # the USER line is newer than the last-prompt bookkeeping, so it wins
    assert meta.last_prompt == "hello there"
    assert meta.first_prompt == "hello there"


def test_last_prompt_tracks_user_line_not_just_bookkeeping(tmp_path):
    """The card's last_prompt must follow the real user line, which is written
    before the lagging `last-prompt` bookkeeping — else the list shows a stale
    prompt while the agent is already working on the new one."""
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {"type": "last-prompt", "lastPrompt": "old question", "sessionId": "s"},
            {"type": "user", "message": {"role": "user", "content": "the NEW question"}},
            # agent has started; the new `last-prompt` line hasn't been written yet
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
            },
        ],
    )
    assert transcripts.transcript_meta(p).last_prompt == "the NEW question"


def test_transcript_meta_first_user_fallback(tmp_path):
    p = tmp_path / "t.jsonl"
    meta_line = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "META"}}
    _write(p, [meta_line, USER])
    meta = transcripts.transcript_meta(p)
    assert meta.title is None  # no ai-title
    assert meta.first_prompt == "hello there"  # meta line skipped


def test_transcript_meta_extracts_cwd(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {
                "type": "user",
                "cwd": "/var/home/me/trailhead",
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    assert transcripts.transcript_meta(p).cwd == "/var/home/me/trailhead"


def test_transcript_meta_missing_file(tmp_path):
    assert transcripts.transcript_meta(tmp_path / "nope.jsonl") == transcripts.TranscriptMeta()


def test_transcript_meta_extracts_last_agent_text(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            USER,
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "older"}]},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "newest reply"}],
                },
            },
        ],
    )
    assert transcripts.transcript_meta(p).last_text == "newest reply"


def test_last_role_reflects_most_recent_message(tmp_path):
    """last_role tells the card which line is newest. A user turn after the
    agent's reply (agent working, no new text yet) must read as 'user'."""
    agent = {
        "type": "assistant",
        "timestamp": "2026-08-13T09:01:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "here you go"}]},
    }
    # agent replied, then the user sent a new message (agent not yet answered)
    newq = {
        "type": "user",
        "timestamp": "2026-08-13T09:02:00Z",
        "message": {"role": "user", "content": "and now this"},
    }
    p = tmp_path / "u.jsonl"
    _write(p, [USER, agent, newq])
    meta = transcripts.transcript_meta(p)
    assert meta.last_role == "user"
    assert meta.last_message_at == datetime(2026, 8, 13, 9, 2, tzinfo=UTC)
    # agent replied last
    p2 = tmp_path / "a.jsonl"
    _write(p2, [USER, agent])
    meta = transcripts.transcript_meta(p2)
    assert meta.last_role == "agent"
    assert meta.last_message_at == datetime(2026, 8, 13, 9, 1, tzinfo=UTC)


def test_askuserquestion_answer_renders_as_user_reply():
    from agentdeck.providers.claude_code.transcripts import _askuserquestion_answer

    # the tool_result the CLI writes once you answer via the widget
    assert (
        _askuserquestion_answer(
            'Your questions have been answered: "Pick a fruit"="banana". '
            "You can now continue with these answers in mind."
        )
        == "Pick a fruit: banana"
    )
    # multiple questions
    assert (
        _askuserquestion_answer('Your questions have been answered: "A"="1", "B"="2".')
        == "A: 1; B: 2"
    )
    # Claude CLI 2.1.220 wording, including its current instruction suffix.
    assert (
        _askuserquestion_answer(
            'The user answered: "What next?"="Build it, File issue with agent label". '
            "Read the answers carefully — they may request clarification, changes, or "
            "that you not proceed — and follow what they actually say."
        )
        == "What next?: Build it, File issue with agent label"
    )
    assert _askuserquestion_answer("The user answered: ordinary tool output") is None
    # freeform responded / did-not-answer variants
    assert _askuserquestion_answer("The user responded: use option two") == "use option two"
    assert _askuserquestion_answer("The user did not answer the questions.") is None
    assert _askuserquestion_answer("random tool output") is None


def test_answer_event_parsed_from_transcript(tmp_path):
    from agentdeck.providers.claude_code.transcripts import read_events

    line = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": 'Your questions have been answered: "Pick a fruit"="banana".',
                }
            ],
        },
    }
    path = tmp_path / "t.jsonl"
    path.write_text(__import__("json").dumps(line) + "\n")
    events = read_events(path).events
    assert len(events) == 1
    ev = events[0]
    assert ev.role == "user" and ev.answer == "Pick a fruit: banana" and ev.text is None


def test_transcript_meta_finds_a_prompt_buried_under_a_huge_turn(tmp_path):
    # The card showed an older message than the chat held (#470): one agent turn
    # is bigger than the head+tail windows, so the newest prompt fell in the
    # unread middle and the head's opening prompt was rendered as "your latest".
    newest = {"type": "user", "message": {"role": "user", "content": "make sure it gets deployed"}}
    filler = [
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "x" * 4000}]}}
        for _ in range(30)
    ]
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, *filler, newest, *filler])
    assert p.stat().st_size > 65536 + 32768  # the windows genuinely cannot cover it

    meta = transcripts.transcript_meta(p)
    assert meta.last_prompt == "make sure it gets deployed"
    assert meta.first_prompt == "hello there"  # the head still owns title/first prompt


def test_transcript_meta_finds_a_reply_buried_under_huge_tool_results(tmp_path):
    # The same window blindness on the *reply* side (#558): a turn that ends in
    # image reads fills the whole tail with base64 tool results, so the newest
    # assistant text fell in the unread middle and the card resurfaced the head's
    # opening reply ("I'll start by reading the issue.") as the agent's latest.
    reply = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "all tests pass"}]},
    }
    tool_use = {
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t", "name": "Read", "input": {}}]},
    }
    big_result = {
        "type": "user",
        "message": {"role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t",
                                 "content": "z" * 8000}]},
    }
    turn = [tool_use, big_result] * 10  # the base64-sized tool output around the reply
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, *turn, reply, *turn[:10]])
    blob, head, tail = p.read_bytes(), 65536, 32768
    assert head < blob.index(b"all tests pass") < len(blob) - tail  # in the unread middle
    assert blob.index(b"Doing the thing") < head  # the stale reply the head does see

    meta = transcripts.transcript_meta(p, head=head, tail=tail)
    assert meta.last_text == "all tests pass"  # not the head's opening reply
    assert meta.last_role == "agent"
    assert meta.last_prompt == "hello there"  # the buried prompt still resolves too


def test_transcript_meta_orders_a_buried_pair_by_the_reverse_scan(tmp_path):
    # Prompt and reply are both past the windows and the prompt is the newer of
    # the two (the agent is working but has not answered yet). One reverse scan
    # decides both, so last_role must say "user" — the order cannot come from two
    # independent scans.
    reply = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}]},
    }
    newest = {"type": "user", "message": {"role": "user", "content": "and now this"}}
    tool_call = {
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                 "input": {"cmd": "x" * 4000}}]},
    }
    filler = [tool_call] * 30
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, *filler, reply, newest, *filler])
    assert p.stat().st_size > 65536 + 32768

    meta = transcripts.transcript_meta(p)
    assert (meta.last_prompt, meta.last_text, meta.last_role) == ("and now this", "on it", "user")


def test_transcript_meta_reads_a_queued_message_past_the_tail_window(tmp_path):
    queued = {"type": "queue-operation", "operation": "enqueue", "content": "typed while busy"}
    filler = [
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "y" * 4000}]}}
        for _ in range(30)
    ]
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, *filler, queued, *filler])
    assert transcripts.transcript_meta(p).last_prompt == "typed while busy"


def test_transcript_meta_drops_a_prompt_compacted_away_past_the_tail_window(tmp_path):
    # The compaction rule must survive the same distance: a boundary newer than
    # every prompt drops it, even when only the reverse scan can see the pair.
    filler = [
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "z" * 4000}]}}
        for _ in range(30)
    ]
    stale = {"type": "last-prompt", "lastPrompt": "the original prompt"}
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, *filler, COMPACT_SUMMARY, COMPACT_CMD, *filler, stale])
    assert transcripts.transcript_meta(p).last_prompt is None


def _notification(task_id: str) -> str:
    return (
        f"<task-notification>\n<task-id>{task_id}</task-id>\n"
        "<tool-use-id>toolu_01</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )


def test_collected_task_ids_reads_delivery_not_arrival(tmp_path):
    """A queued notification is the CLI telling the chat, not the chat knowing.

    Only the record that hands it over — a real user turn, or the ``remove`` for
    one consumed mid-turn — means the chat has the result. Between the two the
    spawned agent is still outstanding, which is exactly the gap #617 read as a
    finished handoff.
    """
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            # Delivered as its own turn.
            {"type": "queue-operation", "operation": "enqueue",
             "content": _notification("adelivered")},
            {"type": "user", "message": {"role": "user",
                                         "content": _notification("adelivered")}},
            # Consumed while a turn was already running: enqueue, mirror, remove.
            {"type": "queue-operation", "operation": "enqueue",
             "content": _notification("aconsumed")},
            {"type": "attachment", "attachment": {"type": "queued_command",
                                                  "prompt": _notification("aconsumed")}},
            {"type": "queue-operation", "operation": "remove",
             "content": _notification("aconsumed")},
            # Still queued: announced, never taken.
            {"type": "queue-operation", "operation": "enqueue",
             "content": _notification("apending")},
            # Unknown shapes are skipped, never fatal.
            {"type": "queue-operation", "operation": "remove", "content": None},
            {"type": "user", "message": "not a dict"},
        ],
    )

    assert transcripts.collected_task_ids(p) == frozenset({"adelivered", "aconsumed"})


def test_collected_task_ids_reads_a_block_shaped_notification(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(
        p,
        [
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": _notification("ablock")},
            ]}},
        ],
    )
    assert transcripts.collected_task_ids(p) == frozenset({"ablock"})


def _subagent_tree(tmp_path, *, sub_last_write, parent_lines):
    """A parent chat resting on agent prose with one spawned agent under it."""
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    subagents = project / "parent-uuid" / "subagents"
    subagents.mkdir(parents=True)
    _write(project / "parent-uuid.jsonl", parent_lines)
    sub = subagents / "agent-atask1.jsonl"
    _write(
        sub,
        [
            {"type": "user", "isSidechain": True,
             "message": {"role": "user", "content": "Draft the batch"}},
            # The shape a spawned agent leaves while it generates a large tool
            # call, and the shape it leaves when it is done: identical.
            {"type": "assistant",
             "timestamp": sub_last_write.isoformat().replace("+00:00", "Z"),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "Writing the file now."}]}},
        ],
    )
    return root


def _parent_prose(when):
    return [
        USER,
        {"type": "assistant",
         "timestamp": when.isoformat().replace("+00:00", "Z"),
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "Wave launched — I'll report back."}],
                     "stop_reason": "end_turn"}},
    ]


async def test_subagent_works_until_its_parent_collects_it(tmp_path):
    """Issue #617: a spawned agent quiet mid-generation is not a finished one.

    Its transcript cannot tell the two apart — no terminal stop reason either way
    — so recency alone dropped a working agent after 25s, un-rolled the parent's
    activity, and handed the operator a chat in the middle of a wave.
    """
    now = datetime.now(UTC)
    quiet = now - timedelta(minutes=2)
    root = _subagent_tree(tmp_path, sub_last_write=quiet, parent_lines=_parent_prose(quiet))
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    scanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    subagent = scanned["agent-atask1"]
    assert subagent.thinking is True
    assert subagent.parent_session_key == "claude_code:test:parent-uuid"
    assert scanned["parent-uuid"].thinking is False  # the parent's own turn is over

    # The parent takes delivery: the wave is over for this agent, at once — no
    # ten-minute stall window standing between a real handoff and the operator.
    _write(
        root / "projects" / "-tmp" / "parent-uuid.jsonl",
        [
            *_parent_prose(quiet),
            {"type": "user", "message": {"role": "user", "content": _notification("atask1")}},
        ],
    )
    rescanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    assert rescanned["agent-atask1"].thinking is False


def _agent_result(agent_id: str, *, status: str = "completed") -> dict:
    """The tool_result line a *synchronous* Agent/Task subagent reports back on.

    Shape read off a live 2026-09-08 transcript (#1708): the report is an
    ordinary tool_result and the structured ``toolUseResult`` carries the
    subagent's bare id — its transcript is ``agent-<id>.jsonl`` — plus how it
    ended. No ``<task-notification>`` is written anywhere for this path.
    """
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "content": [{"type": "text", "text": "Here is what I found."}]}
            ],
        },
        "toolUseResult": {
            "agentId": agent_id,
            "agentType": "Explore",
            "status": status,
            "totalDurationMs": 42000,
        },
    }


def test_task_ledger_collects_a_synchronous_agent_from_its_finished_result(tmp_path):
    """#1708: the one collection record the ledger did not read.

    A synchronous subagent never notifies, so before this the parent looked as
    though it had taken delivery of nothing and every finished agent stayed
    "working" until its own stall window expired.
    """
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _agent_result("atask1")])
    assert transcripts.task_ledger(p).collected == frozenset({"atask1"})

    # A result that is not a finished report proves nothing. A backgrounded agent
    # reports back on the notification path instead, and its launch must not be
    # mistaken for its report.
    _write(p, [USER, _agent_result("atask1", status="running")])
    assert transcripts.task_ledger(p).collected == frozenset()


async def test_a_synchronous_subagent_stops_when_its_result_lands(tmp_path):
    """#1708: a finished Explore agent must not hold the parent's indicators.

    The live case: a chat had run 41 subagents and written no notification at
    all, so each one kept a nested "working" row — and the activity badge,
    THINKING header and "1 WORKING" section rolled up from it — for the ten
    minutes its own stall window took to expire, with the parent's finished
    answer already on screen.
    """
    now = datetime.now(UTC)
    quiet = now - timedelta(minutes=2)
    root = _subagent_tree(tmp_path, sub_last_write=quiet, parent_lines=_parent_prose(quiet))
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    scanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    assert scanned["agent-atask1"].thinking is True  # no result yet: still running

    _write(
        root / "projects" / "-tmp" / "parent-uuid.jsonl",
        [*_parent_prose(quiet), _agent_result("atask1")],
    )
    rescanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    finished = rescanned["agent-atask1"]
    assert finished.thinking is False
    assert finished.activity is None
    assert finished.stalled is False


async def test_uncollected_subagent_still_stops_at_the_stall_window(tmp_path):
    """A parent killed mid-wave never collects: the backstop is the stall window."""
    now = datetime.now(UTC)
    dead = now - timedelta(minutes=30)
    root = _subagent_tree(tmp_path, sub_last_write=dead, parent_lines=_parent_prose(dead))
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    scanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    assert scanned["agent-atask1"].thinking is False


def _background_launch(task_id: str, *, prose: str | None = None) -> dict:
    """The tool_result line the CLI writes when a Bash command goes background.

    Two spellings reach it — an explicit ``run_in_background`` and a foreground
    command that outran its timeout — and both stamp the id on ``toolUseResult``.
    """
    text = prose or (
        f"Command running in background with ID: {task_id}. Output is being "
        f"written to: /tmp/tasks/{task_id}.output. You will be notified when it "
        "completes."
    )
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": text}
            ],
        },
        "toolUseResult": {
            "stdout": "",
            "stderr": "",
            "interrupted": False,
            "backgroundTaskId": task_id,
        },
    }


def test_task_ledger_holds_a_backgrounded_command_until_its_notification_lands(tmp_path):
    """Issue #683: the launch is structured, the collection is the notification."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, _background_launch("bwait1")])
    assert transcripts.task_ledger(p).outstanding == frozenset({"bwait1"})

    # Announced but not yet handed over — the chat is about to wake up on it.
    _write(
        p,
        [
            USER,
            _background_launch("bwait1"),
            {"type": "queue-operation", "operation": "enqueue",
             "content": _notification("bwait1")},
        ],
    )
    assert transcripts.task_ledger(p).outstanding == frozenset({"bwait1"})

    # Delivered: collected, and no longer outstanding.
    _write(
        p,
        [
            USER,
            _background_launch("bwait1"),
            {"type": "queue-operation", "operation": "enqueue",
             "content": _notification("bwait1")},
            {"type": "user", "message": {"role": "user", "content": _notification("bwait1")}},
        ],
    )
    ledger = transcripts.task_ledger(p)
    assert ledger.collected == frozenset({"bwait1"})
    assert ledger.outstanding == frozenset()


def test_task_ledger_reads_the_structured_id_not_the_prose(tmp_path):
    """A chat that *quotes* a launch line has not launched anything.

    Reading the "Command running in background with ID: …" prose out of arbitrary
    tool output would let one chat's grep of another chat's transcript invent an
    outstanding task and park a finished chat on "Working" for the whole stall
    window. Only the CLI's own ``toolUseResult`` stamp counts.
    """
    p = tmp_path / "t.jsonl"
    quoted = _background_launch("bquoted")
    quoted.pop("toolUseResult")
    _write(p, [USER, quoted])
    assert transcripts.task_ledger(p).outstanding == frozenset()

    # A command that outran its timeout is a real launch in the other spelling.
    _write(
        p,
        [
            USER,
            _background_launch(
                "btimeout",
                prose=(
                    "Command did not complete within its 600s timeout and was "
                    "moved to the background (ID: btimeout)."
                ),
            ),
        ],
    )
    assert transcripts.task_ledger(p).outstanding == frozenset({"btimeout"})


def _waiting_chat(tmp_path, *, when, lines):
    """A live CLI chat whose own turn has ended, with ``lines`` before the prose."""
    root = tmp_path / "cfg"
    (root / "sessions").mkdir(parents=True)
    (root / "projects" / "-tmp").mkdir(parents=True)
    (root / "sessions" / "4242.json").write_text(
        json.dumps(
            {"pid": 4242, "sessionId": "chat-uuid", "cwd": "/tmp",
             "procStart": "19", "kind": "interactive"}
        )
    )
    _write(root / "projects" / "-tmp" / "chat-uuid.jsonl", [*lines, *_parent_prose(when)])
    return root




async def test_an_abandoned_background_command_stops_at_the_stall_window(tmp_path, monkeypatch):
    """A command whose notification never comes is bounded, like an uncollected
    subagent: the chat rests at the stall window instead of working forever."""
    monkeypatch.setattr(provider_mod.registry_mod, "is_alive", lambda entry: True)
    now = datetime.now(UTC)
    dead = now - timedelta(minutes=30)
    root = _waiting_chat(tmp_path, when=dead, lines=[_background_launch("borphan")])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)

    scanned = {s.session_id: s for s in await provider.scan_sessions(account)}
    assert scanned["chat-uuid"].thinking is False


# Claude Code gives up on a failing request by writing its own apology into the
# transcript in the exact shape of a model reply, with `isApiErrorMessage` the
# only thing telling the two apart (#1463).
API_ERROR_REPLY = {
    "type": "assistant",
    "timestamp": "2026-09-03T14:17:11.220Z",
    "isApiErrorMessage": True,
    "apiErrorStatus": 529,
    "message": {
        "role": "assistant",
        "model": "<synthetic>",
        "stop_reason": "stop_sequence",
        "content": [
            {"type": "text", "text": "API Error: 529 Overloaded. This is a server-side issue."}
        ],
    },
}
# The retry lines the CLI writes while it is failing. It buffers them and flushes
# them all at once when the turn ends, so they are history, never live progress.
API_RETRY_LINE = {
    "type": "system",
    "subtype": "api_error",
    "level": "error",
    "source": "request_retry",
    "timestamp": "2026-09-03T14:13:58.025Z",
    "retryAttempt": 1,
    "maxRetries": 10,
    "error": {"status": 529, "formatted": "529 Overloaded"},
}


FINAL_REPLY = {
    "type": "assistant",
    "timestamp": "2026-09-03T14:17:11.220Z",
    "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "All done."}],
    },
}


def test_a_claude_api_error_reply_is_marked_as_a_failure_not_an_answer(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, API_ERROR_REPLY])
    user, error = transcripts.read_events(p).events

    assert user.failed is False
    # It still renders — the operator wants to read what went wrong — but it no
    # longer passes for the model's own reply.
    assert error.role == "assistant"
    assert error.text.startswith("API Error: 529 Overloaded")
    assert error.failed is True


def test_a_real_claude_reply_is_never_marked_failed(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT])
    assert [e.failed for e in transcripts.read_events(p).events] == [False, False]


def test_the_buffered_api_retry_lines_stay_out_of_the_transcript(tmp_path):
    """They arrive stamped minutes *before* the failure they precede, so treating
    them as events would move the turn's newest event backwards in time."""
    p = tmp_path / "t.jsonl"
    _write(p, [USER, API_ERROR_REPLY, API_RETRY_LINE, API_RETRY_LINE])
    events = transcripts.read_events(p).events

    assert [e.role for e in events] == ["user", "assistant"]
    assert transcripts.last_event(p).failed is True


async def _api_error_chat(tmp_path, monkeypatch, *, lines):
    """One live CLI chat whose transcript is exactly ``lines``."""
    monkeypatch.setattr(provider_mod.registry_mod, "is_alive", lambda entry: True)
    root = _waiting_chat(tmp_path, when=datetime.now(UTC), lines=[])
    _write(root / "projects" / "-tmp" / "chat-uuid.jsonl", lines)
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    (session,) = await provider.scan_sessions(account)
    return session


async def test_a_claude_turn_that_died_on_an_api_error_reads_as_failed(
    tmp_path, monkeypatch
):
    """The report behind #1463: the CLI burned three minutes of retries and then
    gave up, and the chat sat in the deck looking like a healthy resting one. A
    failure no timer can catch needs the provider to say so, the same rule Cursor
    got in #778."""
    session = await _api_error_chat(
        tmp_path, monkeypatch, lines=[USER, API_RETRY_LINE, API_ERROR_REPLY]
    )

    assert session.status == SessionStatus.LIVE
    assert session.turn_failed is True
    assert session.thinking is False
    # Not "idle": the process is alive and resting, but the work it was given
    # never happened and nothing is going to move it.
    assert session.display_state == "failed"


async def test_a_claude_chat_that_answered_is_not_failed(tmp_path, monkeypatch):
    session = await _api_error_chat(tmp_path, monkeypatch, lines=[USER, FINAL_REPLY])
    assert session.turn_failed is False
    assert session.display_state == "idle"


async def test_a_new_prompt_after_an_api_error_clears_the_failure(tmp_path, monkeypatch):
    """Asking again is the operator taking the failure off the board. The chat is
    working on the retry, so it must not keep shouting about the previous turn."""
    session = await _api_error_chat(
        tmp_path,
        monkeypatch,
        lines=[
            USER,
            API_ERROR_REPLY,
            {
                "type": "user",
                "timestamp": datetime.now(UTC).isoformat(),
                "message": {"role": "user", "content": "try again"},
            },
        ],
    )

    assert session.turn_failed is False
    assert session.thinking is True


def _said(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


IMAGE_ONLY = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [{"type": "image", "source": {"type": "base64", "data": "aaaa"}}],
    },
}
QUEUED_ANSWER = {
    "type": "attachment",
    "attachment": {"type": "queued_command", "prompt": "yes, re-render it"},
}
JOB_REPORT = _said("Your queued job 0f37c6ce89bd finished successfully.\nExit code: 0")


@pytest.mark.parametrize(
    ("lines", "background"),
    [
        # #2760: a render watcher woke the chat after it asked a question; nobody answered.
        ([USER, ASSISTANT, TASK_NOTIFICATION, ASSISTANT, TOOL_RESULT], True),
        ([USER, ASSISTANT, JOB_REPORT, ASSISTANT], True),
        ([TASK_NOTIFICATION, ASSISTANT, USER, ASSISTANT, TOOL_RESULT], False),
        ([TASK_NOTIFICATION, ASSISTANT, IMAGE_ONLY], False),
        ([TASK_NOTIFICATION, ASSISTANT, COMPACT_SUMMARY], False),
        ([USER, ASSISTANT], False),
        ([], False),
        # Someone answered while the woken turn still ran: queued, or delivered into it.
        ([USER, ASSISTANT, TASK_NOTIFICATION, ASSISTANT, _enqueue("yes, re-render")], False),
        ([USER, ASSISTANT, TASK_NOTIFICATION, ASSISTANT, QUEUED_ANSWER], False),
        # A second notification delivered into the woken turn changes nothing.
        ([USER, ASSISTANT, TASK_NOTIFICATION, _enqueue(TASK_NOTIFICATION["message"]["content"])],
         True),
    ],
)
def test_a_turn_a_background_notification_opened_is_told_from_a_message(
    tmp_path, lines, background
):
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    assert transcripts.turn_opened_by_notification(p) is background


def test_the_job_report_prefix_is_what_the_queue_sends():
    from agentdeck.jobs import DONE, Job, job_message

    job = Job("abc123", ["pytest", "-q"], "/tmp", "/nonexistent.log", 0.0, state=DONE)
    assert job_message(job).startswith(transcripts.JOB_MESSAGE_PREFIX)


def test_only_a_working_chat_carries_its_turns_opener(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [USER, ASSISTANT, TASK_NOTIFICATION, ASSISTANT])
    provider = ClaudeCodeProvider()
    working = Session("claude_code:a:s", "claude_code:a", "s", SessionStatus.LIVE, thinking=True)
    resting = Session("claude_code:a:r", "claude_code:a", "r", SessionStatus.LIVE)

    provider._note_turn_opener(working, p)
    provider._note_turn_opener(resting, p)
    assert (working.background_turn, resting.background_turn) == (True, False)

    provider._note_turn_opener(working, None)
    assert working.background_turn is False


async def test_the_live_window_stops_listing_old_owned_claude_workers(tmp_path, monkeypatch):
    """#2841: an owned worker whose transcript is older than the window is listed only
    while it runs, is in a Work still going, or was just opened; an older chat someone
    opened is listed even when this deck does not own it."""
    import time

    from agentdeck import live_window

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    sids = {
        name: f"{str(index) * 8}-1111-1111-1111-111111111111"
        for index, name in enumerate(
            ("new", "old_owned", "running", "pinned", "opened", "recent_owned"), start=1
        )
    }
    ten_days = time.time() - 10 * 86400
    for name, sid in sids.items():
        path = project / f"{sid}.jsonl"
        _write(path, [USER, ASSISTANT])
        if name != "new":
            stamp = time.time() - 86400 if name == "recent_owned" else ten_days
            os.utime(path, (stamp, stamp))
    monkeypatch.setattr(provider_mod, "MAX_IDLE_SESSIONS", 1)
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    owned = {sids[name] for name in ("old_owned", "running", "pinned", "recent_owned")}
    workers = MagicMock()
    workers.available = False
    workers.owned_session_ids.return_value = owned
    workers.owns.side_effect = lambda sid: sid in owned
    workers.live.side_effect = lambda sid: sid == sids["running"]
    workers.cwd_for.return_value = None
    workers.turn_active.return_value = False
    workers.effective_activity.return_value = False
    workers.turn_cut_off.return_value = False
    workers.turn_cut_off_at.return_value = 0.0
    workers.turn_started_at.return_value = None
    workers.revival_due.return_value = False
    provider._workers[account.key] = workers
    window = live_window.LiveWindow(7)
    window.set_works(pinned={f"{account.key}:{sids['pinned']}"}, finished=())
    window.request([f"{account.key}:{sids['opened']}"])

    token = live_window.CURRENT.set(window)
    try:
        sessions = await provider.scan_sessions(account)
    finally:
        live_window.CURRENT.reset(token)

    assert {s.session_id for s in sessions} == {
        sids[name] for name in ("new", "running", "pinned", "opened", "recent_owned")
    }


async def test_a_targeted_claude_scan_reads_only_the_named_chat(tmp_path, monkeypatch):
    """#2841 review: opening one old chat reads its transcript by name, not the account."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    root = tmp_path / "cfg"
    project = root / "projects" / "-tmp"
    project.mkdir(parents=True)
    wanted = "11111111-1111-1111-1111-111111111111"
    other = "22222222-2222-2222-2222-222222222222"
    for sid in (wanted, other):
        _write(project / f"{sid}.jsonl", [USER, ASSISTANT])
    provider = ClaudeCodeProvider()
    account = Account("claude_code:test", "claude_code", "test", root)
    await provider.scan_sessions(account)
    billable = list(provider._billable_paths[account.key])

    def no_inventory(*_args):
        raise AssertionError("a one-chat load reads nothing account-wide")

    monkeypatch.setattr(provider_mod, "_scan_inventory", no_inventory)
    monkeypatch.setattr(provider_mod.registry_mod, "read_registry", no_inventory)
    monkeypatch.setattr(provider_mod.history_mod.HistoryIndex, "read", no_inventory)
    sessions = await provider.scan_sessions(account, only=frozenset({wanted}))

    assert [s.session_id for s in sessions] == [wanted]
    assert provider._billable_paths[account.key] == billable
