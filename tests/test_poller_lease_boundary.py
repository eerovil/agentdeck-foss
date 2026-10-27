"""The Poller lease fences Poller lifecycle, not the worker's GitHub access (#2152).

Nothing ever enforced "every worker comment goes through worker-control": the gh shim never
blocks a write and intake acts only on command comments. The rule lived in the brief alone, and
what it taught a superseded worker was that it could not even say what it had learned. These
tests pin both halves of the boundary — ordinary GitHub collaboration stays ordinary, lifecycle
stays generation-bound — and that the fix did not grow a second control plane in its place.
"""

from __future__ import annotations

import re
from pathlib import Path

from poller_engine_fakes import FakePort, store_for

from agentdeck.poller_adapter.runner import Workspace, compose_message
from agentdeck.poller_adapter.worker import _compact_poller_brief
from agentdeck.poller_engine import intake
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl

_KEY = "store#5"
_SRC = Path(__file__).resolve().parents[1] / "src" / "agentdeck"


def _briefs() -> tuple[str, str]:
    directive = {
        "ref": "agentdeck#2152",
        "repo": "agentdeck",
        "number": 2152,
        "action": "new",
        "generation": "g1",
        "messages": [{"payload": "Implement the requested change."}],
    }
    message = compose_message(
        directive,
        Workspace(path=Path("/repo/.worktrees/issue-2152"), branch="agent/issue-2152"),
        owner="eerovil",
    )
    compact = _compact_poller_brief(
        directive,
        message,
        cwd="/repo/.worktrees/issue-2152",
        worktrees=["/repo/.worktrees/issue-2152"],
    )
    return message, compact


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_no_brief_routes_every_comment_through_worker_control():
    for text in map(_flat, _briefs()):
        assert "Every worker-originated comment" not in text
        assert "Never use `gh issue comment`" not in text
        assert "instead of direct issue comments" not in text
        assert "stale/disabled means stop." not in text


def test_the_brief_lets_a_current_run_comment_and_file_issues_with_normal_gh():
    full, compact = map(_flat, _briefs())

    assert "Ordinary GitHub collaboration does not need the lease" in full
    assert "post an ordinary issue comment, or open a follow-up issue" in full
    assert "a follow-up issue does not become part of this card" in full
    assert "Ordinary issue comments, follow-up issues and GitHub reads use normal `gh`" in (
        compact
    )


def test_the_brief_keeps_lifecycle_generation_bound_and_distinguishes_it_from_github_access():
    full, compact = map(_flat, _briefs())

    assert "controls Poller lifecycle, not your GitHub access" in full
    assert "Lifecycle operations go only through the Poller Instance interface" in full
    assert "neither a board column nor a `status:*` label" in full
    assert "The Poller lease fences lifecycle only" in compact
    assert "status is never edited directly" in compact
    # The terminal commands themselves are still the generation-bound ones.
    assert "worker-control finish agentdeck 2152 g1 in_review --body-file" in compact


def test_a_stale_run_is_told_to_stop_mutating_but_may_still_report():
    full, compact = map(_flat, _briefs())

    # Stop: code, workspace, pushes, lifecycle.
    assert "make no further code or workspace change" in full
    assert "push nothing that could race whoever holds the card now" in full
    assert "run no further lifecycle operation" in full
    assert "means stop code and workspace changes, pushes and lifecycle operations" in compact
    # Still allowed: one documentary comment, a follow-up issue.
    assert "It does not revoke your GitHub credentials" in full
    assert "you may still post one ordinary comment saying what your turn reached" in full
    assert "or open a follow-up issue" in full
    assert "you may still post one ordinary comment saying what your turn reached" in compact


def test_the_brief_says_an_answer_in_the_chat_resumes_the_same_lease():
    full, compact = map(_flat, _briefs())

    for text in (full, compact):
        assert "a person answers in this chat, the deck posts the answer on the issue" in text
        assert "back to In progress for you" in text


def test_the_brief_warns_that_an_ordinary_comment_must_not_be_a_board_command():
    for text in map(_flat, _briefs()):
        assert "`/claude`" in text
        assert re.search(r"[Nn]ever start (a comment or issue you write|one) with a board command",
                         text)


def _store(tmp_path, **extra):
    return store_for(
        tmp_path, {"issues": {_KEY: {"generation": "gen-1", "status": "In progress", **extra}}}
    )


def _port():
    return FakePort(snapshots={_KEY: CardSnapshot(status="In progress")})


def test_a_stale_run_still_cannot_finish(tmp_path):
    port = _port()
    result = WorkerControl(_store(tmp_path), port).finish("store", 5, "old", "in_review", "Done")

    assert not result.ok
    assert result.reason.startswith("stale:")
    assert port.applied == []


def test_a_stale_run_still_cannot_submit_a_review_verdict(tmp_path):
    port = _port()
    result = WorkerControl(_store(tmp_path, mode="merge-review"), port).review(
        "store", 5, "old", "pass", "Looks good"
    )

    assert not result.ok
    assert port.applied == []


def test_no_work_action_control_plane_was_added():
    """The superseded design proposed these; #2152 forbids building them."""
    forbidden = re.compile(
        r"perform_work_action|work_run_authority|work_action_ledger|action_ledger"
    )
    hits = [
        str(path.relative_to(_SRC))
        for path in _SRC.rglob("*.py")
        if forbidden.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_claude_command_parsing_is_unchanged_by_ordinary_comments():
    """An ordinary status comment is not a command; a `/claude` one still is, and still goes
    through intake's unchanged authorization."""
    assert intake.parse_control("Found a separate bug, filed eerovil/agentdeck#1.") is None
    command = intake.parse_control("/claude continue from the report above")
    assert command is not None
    assert command.kind is intake.CommandKind.CLAUDE
