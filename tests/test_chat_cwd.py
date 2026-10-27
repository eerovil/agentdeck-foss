"""Whether a chat whose directory is gone can still be answered (#2332, #2524)."""

from __future__ import annotations

from pathlib import Path

from agentdeck.chat_cwd import cannot_resume, continued_chat_cwd
from agentdeck.models import Capability, Session, SessionStatus


def _session(cwd: Path, *capabilities: Capability, thinking: bool = False) -> Session:
    return Session(
        "claude_code:main:sid",
        "claude_code:main",
        "sid",
        SessionStatus.IDLE,
        cwd=cwd,
        thinking=thinking,
        capabilities=frozenset({Capability.TRANSCRIPT, *capabilities}),
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "project"
    (repository / ".git").mkdir(parents=True)
    return repository


def test_a_gone_worktree_continues_in_the_repository_that_held_it(tmp_path):
    repository = _repository(tmp_path)
    assert continued_chat_cwd(repository / ".worktrees" / "issue-1") == repository


def test_a_chat_its_runtime_can_move_is_answerable(tmp_path):
    gone = _repository(tmp_path) / ".worktrees" / "issue-1"
    assert not cannot_resume(_session(gone, Capability.RESUME_ELSEWHERE))


def test_a_chat_its_runtime_cannot_move_is_stranded(tmp_path):
    gone = _repository(tmp_path) / ".worktrees" / "issue-1"
    assert cannot_resume(_session(gone))


def test_a_movable_chat_with_no_repository_above_it_is_stranded(tmp_path, monkeypatch):
    from agentdeck import chat_cwd

    monkeypatch.setattr(chat_cwd, "continued_chat_cwd", lambda _cwd: None)
    assert cannot_resume(_session(tmp_path / "gone", Capability.RESUME_ELSEWHERE))


def test_a_present_directory_or_a_running_turn_is_never_stranded(tmp_path):
    assert not cannot_resume(_session(tmp_path))
    assert not cannot_resume(_session(tmp_path / "gone", thinking=True))
