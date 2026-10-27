"""Slice 2 of #176: command intake grammar."""

from __future__ import annotations

import pytest

from agentdeck.poller_engine.intake import (
    DEFAULT_BOT_MARKER,
    CommandKind,
    parse_control,
)


@pytest.mark.parametrize(
    ("body", "kind", "instruction"),
    [
        ("/claude fix the bug", CommandKind.CLAUDE, "fix the bug"),
        ("  /claude do it  ", CommandKind.CLAUDE, "do it"),
        ("/CLAUDE Fix It", CommandKind.CLAUDE, "Fix It"),
        ("/claude stop", CommandKind.STOP, ""),
        ("/claude STOP", CommandKind.STOP, ""),
        ("/merge", CommandKind.MERGE, ""),
        ("/merge review", CommandKind.MERGE_REVIEW, ""),
        ("/merge   review", CommandKind.MERGE_REVIEW, ""),
    ],
)
def test_valid_commands(body, kind, instruction):
    cmd = parse_control(body)
    assert cmd is not None
    assert cmd.kind is kind
    assert cmd.instruction == instruction
    assert cmd.is_valid


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ("/claude", CommandKind.INVALID_CLAUDE),
        ("/claude   ", CommandKind.INVALID_CLAUDE),
        ("/claude stop now please", CommandKind.INVALID_STOP),
        ("/merge please", CommandKind.INVALID_MERGE),
        ("/merge reviewx", CommandKind.INVALID_MERGE),
    ],
)
def test_invalid_near_misses_are_explicit(body, kind):
    cmd = parse_control(body)
    assert cmd is not None
    assert cmd.kind is kind
    assert not cmd.is_valid


@pytest.mark.parametrize(
    "body",
    [
        "",
        None,
        "just a normal comment",
        "please run /claude fix later",  # not at the start
        "```\n/claude fix\n```",  # inside a code fence, not first non-space
        "the /merge command",
    ],
)
def test_non_commands_return_none(body):
    assert parse_control(body) is None


def test_bot_marked_comment_never_triggers():
    body = f"/claude fix it\n\n{DEFAULT_BOT_MARKER}"
    assert parse_control(body) is None


def test_custom_bot_marker_is_honored():
    body = "/merge <!-- other-bot -->"
    # /merge with trailing text is an invalid-merge, not a real merge...
    assert parse_control(body, bot_marker="").kind is CommandKind.INVALID_MERGE
    # ...but with the marker configured, the whole comment is ignored.
    assert parse_control(body, bot_marker="<!-- other-bot -->") is None


# --- named release targets (#1445) ---

def test_a_bare_merge_names_no_target():
    command = parse_control("/merge")
    assert command.kind is CommandKind.MERGE
    assert command.targets == ()


def test_a_merge_can_name_the_pull_request_to_release():
    command = parse_control("/merge store#3024")
    assert command.kind is CommandKind.MERGE
    assert command.targets == ("store#3024",)


def test_a_merge_review_can_name_targets_too():
    command = parse_control("/merge review store#3024 docker#77")
    assert command.kind is CommandKind.MERGE_REVIEW
    assert command.targets == ("store#3024", "docker#77")


def test_a_fully_qualified_ref_keeps_only_the_repository():
    """The engine keys a pull request as `repo#number`; a card's targets share its org."""
    assert parse_control("/merge Scandi/store#3024").targets == ("store#3024",)


def test_a_repeated_ref_is_named_once():
    assert parse_control("/merge store#7 store#7").targets == ("store#7",)


def test_a_malformed_ref_invalidates_the_whole_command():
    """A typo'd ref must not widen back to releasing everything discovery found."""
    for text in ("/merge store3024", "/merge store#3024 nonsense", "/merge #7"):
        assert parse_control(text).kind is CommandKind.INVALID_MERGE


def test_merge_review_is_still_a_review_not_a_target_named_review():
    """`review` must keep beating the ref grammar, or `/merge review` becomes a bad target."""
    assert parse_control("/merge review").kind is CommandKind.MERGE_REVIEW
