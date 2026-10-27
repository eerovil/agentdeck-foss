"""The heavy-command shim: the queue's enforcement half for Codex workers.

Claude Code sessions are pushed onto the queue of #1287 by a ``PreToolUse``
hook.  Codex CLI has no such hook, so a deck-spawned Codex worker met the queue
only as an ``AGENTS.md`` request it could forget (#1346).  This package is the
same guarantee by the other available means: shadowing executables placed first
on the worker's PATH, exactly as ``agentdeck.gh_shim`` does for ``gh``.

Both halves share one notion of "heavy" — see ``classify`` — so a suite is
never refused under one provider and started under the other.
"""

from __future__ import annotations

from .classify import is_heavy
from .install import (
    ensure_python_guard,
    ensure_shim,
    real_program,
    shim_dir,
    worker_path,
    worker_pythonpath,
)

__all__ = [
    "ensure_python_guard",
    "ensure_shim",
    "is_heavy",
    "real_program",
    "shim_dir",
    "worker_path",
    "worker_pythonpath",
]
