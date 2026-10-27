from __future__ import annotations

import json

from agentdeck.providers.codex import transcripts


def _tool_call(input_: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-07-15T07:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "input": input_,
            },
        }
    )


def test_wrapped_file_edit_uses_edit_files_label(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _tool_call(
            'const patch = "*** Begin Patch\\n*** Update File: AGENTS.md\\n'
            '@@\\n-old\\n+new\\n*** End Patch"; '
            "text(await tools.apply_patch(patch));"
        )
        + "\n"
    )

    (event,) = transcripts.read_events(path).events
    assert event.tool_name == "exec"
    assert event.tool_display_name == "Edit files"
    assert event.tool_summary == "path: AGENTS.md"
    assert "*** Update File: AGENTS.md" in event.tool_detail


def test_elevated_shell_call_shows_approval_reason_and_command(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        _tool_call(
            'const r = await tools.exec_command({cmd:"git add AGENTS.md",'
            '"sandbox_permissions":"require_escalated",'
            '"justification":"Allow staging the requested guide?"}); text(r.output)'
        )
        + "\n"
    )

    (event,) = transcripts.read_events(path).events
    assert event.tool_name == "exec"
    assert event.tool_display_name == "Approval"
    assert event.tool_summary == "reason: Allow staging the requested guide?"
    assert event.tool_detail == (
        "Reason\nAllow staging the requested guide?\n\nCommand\ngit add AGENTS.md"
    )


def test_tool_call_description_wins_the_collapsed_peek(tmp_path):
    """Issue #581: a described call peeks as its intent, like the Claude side."""
    path = tmp_path / "rollout.jsonl"
    with_description = {"cmd": "rg -n pattern src", "description": "Locate the peek builder"}
    path.write_text(
        _tool_call(json.dumps(with_description))
        + "\n"
        + _tool_call(json.dumps({"cmd": "rg -n pattern src"}))
        + "\n"
    )

    described, plain = transcripts.read_events(path).events
    assert described.tool_summary == "Locate the peek builder"
    assert plain.tool_summary == "cmd: rg -n pattern src"
