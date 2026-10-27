"""Where a chat runs once the directory it ran in is gone (#2332, #2524).

A chat's recorded working directory is usually an issue worktree, removed once its work
merged. The chat itself is not finished with it: a deck-owned Claude or Codex chat resumes
the same conversation in the repository that held the worktree, with everything it knew
(#2524). The runtime makes that move itself when it starts the next turn, so every way a
message reaches the chat gets it; the web side only reads whether the runtime can
(``Capability.RESUME_ELSEWHERE``). A chat that cannot move — no repository above its
directory, or a provider whose runtime does not resume elsewhere — is the one a person
cannot answer in place. Every surface that asks that question reads the rule from here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Capability


def cwd_exists(cwd: Path) -> bool:
    """Whether a chat's recorded working directory is still present on this host."""
    try:
        return cwd.is_dir()
    except OSError:
        return False


def continued_chat_cwd(cwd: Path) -> Path | None:
    """Repository where a chat continues once its worktree is gone.

    A merged worker worktree is the normal end state of every kanban card, so the
    recorded cwd of a finished worker chat routinely disappears (#746). The repository
    the work belongs to still exists, and it is the only correct place to continue the
    work, so there is no directory choice to ask the person to make (#1512).
    """
    try:
        parents = tuple(cwd.parents)
    except (OSError, RuntimeError):
        return None
    for parent in parents:
        try:
            is_repository = (parent / ".git").is_dir()
        except OSError:
            continue
        if is_repository:
            return parent
    return None


def cwd_gone(session: Any) -> bool:
    """The chat has no turn in flight and its recorded working directory no longer exists.

    A running process does not need its directory looked up again, and a chat with no
    recorded directory has nothing to be missing.
    """
    cwd = getattr(session, "cwd", None)
    return cwd is not None and not getattr(session, "thinking", False) and not cwd_exists(
        Path(cwd)
    )


def cannot_resume(session: Any) -> bool:
    """The chat's directory is gone and its next turn has nowhere to run.

    False for a chat whose runtime resumes it in the repository instead (#2524).
    """
    if not cwd_gone(session):
        return False
    capabilities = getattr(session, "capabilities", frozenset())
    return (
        Capability.RESUME_ELSEWHERE not in capabilities
        or continued_chat_cwd(Path(session.cwd)) is None
    )
