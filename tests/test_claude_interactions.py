"""Interactive-question mapping: raw can_use_tool control_request -> neutral dict
(runtime, worker._normalize_interaction) -> PendingInteraction (web,
provider._pending_from_dict)."""

from __future__ import annotations

from agentdeck.providers.claude_code.provider import _pending_from_dict
from agentdeck.providers.claude_code.worker import _normalize_interaction


def test_askuserquestion_normalizes_and_deserializes():
    raw = {
        "request_id": "req-1",
        "tool_name": "AskUserQuestion",
        "display_name": "AskUserQuestion",
        "input": {
            "questions": [
                {
                    "question": "Pick a fruit",
                    "header": "Fruit",
                    "multiSelect": True,
                    "options": [
                        {"label": "apple", "description": "a"},
                        {"label": "banana", "description": "b"},
                    ],
                }
            ]
        },
    }
    neutral = _normalize_interaction(raw, "sid-1")
    assert neutral["id"] == "req-1" and neutral["kind"] == "question"
    assert neutral["thread_id"] == "sid-1"

    interaction = _pending_from_dict(neutral)
    assert interaction.id == "req-1" and interaction.kind == "question"
    q = interaction.questions[0]
    assert q.id == "0" and q.prompt == "Pick a fruit" and q.header == "Fruit"
    assert q.multiselect is True and q.allow_other is True
    assert [o.label for o in q.options] == ["apple", "banana"]


def test_permission_gate_normalizes_with_no_session_scope_button():
    raw = {
        "request_id": "g1",
        "tool_name": "Bash",
        "display_name": "Bash",
        "description": "delete a file",
        "input": {"command": "rm x"},
    }
    interaction = _pending_from_dict(_normalize_interaction(raw, "sid-1"))
    assert interaction.kind == "permission"
    assert interaction.title == "Allow Bash?"
    assert interaction.message == "delete a file"
    assert interaction.command == "rm x"
    assert interaction.questions == ()
    # "Allow for session" is deliberately not offered (can't be honored)
    assert "acceptForSession" not in interaction.decisions
    assert "cancel" in interaction.decisions


def test_permission_gate_previews_non_bash_input():
    raw = {
        "request_id": "g2",
        "tool_name": "Write",
        "display_name": "Write",
        "input": {"file_path": "/etc/passwd", "content": "x"},
    }
    interaction = _pending_from_dict(_normalize_interaction(raw, "sid-1"))
    assert interaction.message == "file_path: /etc/passwd"  # not answered blind


def test_normalize_returns_none_without_request_id():
    assert _normalize_interaction({"tool_name": "Bash"}, "sid-1") is None
    assert _normalize_interaction(None, "sid-1") is None
    assert _pending_from_dict(None) is None
