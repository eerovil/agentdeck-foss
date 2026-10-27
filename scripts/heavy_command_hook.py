#!/usr/bin/env python3
"""A Claude Code ``PreToolUse`` hook that pushes heavy commands onto the queue.

The queue in ``agentdeck.jobs`` caps the suites it starts, and ``tests/conftest.py``
caps a direct ``pytest`` in this repo. Neither covers the other projects on this
host — a Node and Playwright project never imports a conftest of ours —
so this is the generic half: refuse the Bash call and say what to run instead.

Install it in ``~/.claude/settings.json``::

    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
      {"type": "command", "command": "/home/me/agentdeck/scripts/heavy_command_hook.py"}
    ]}]}}

What "heavy" means, and the refusal text, live in
``src/agentdeck/heavy_shim/classify.py`` and are loaded from there by path — this
hook runs under the system interpreter with no AgentDeck on its path. The other
enforcement half, the PATH shim that covers Codex workers, reads the same file,
so the two providers cannot drift into different notions of a whole suite.

Known gap: hooks are per provider. This covers Claude Code sessions. Cursor
workers still need their own interception, or an ``AGENTS.md`` instruction, and
until then the queue is advisory for them.

It fails open on purpose. A hook that denies while the runtime is down would
leave every project on the host unable to run its tests at all, which is a worse
failure than the pile-up this exists to prevent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_CLASSIFY = (
    Path(__file__).resolve().parent.parent / "src" / "agentdeck" / "heavy_shim" / "classify.py"
)


def _classifier():
    """Load the shared matcher, or ``None`` if this checkout cannot supply it.

    A hook that cannot decide must not decide: an unreadable module leaves every
    command allowed, which is the same fail-open stance as an absent runtime.
    """
    try:
        spec = importlib.util.spec_from_file_location("agentdeck_heavy_classify", _CLASSIFY)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError):
        return None
    return module


classify = _classifier()

# Re-exported so the hook keeps one obvious name for each, and so a caller that
# already had `hook.HEAVY` still finds it.
HEAVY = getattr(classify, "HEAVY", None)
EXEMPT = getattr(classify, "EXEMPT", None)


def runtime_socket() -> str:
    """Where ``providers/runtime_socket_client.runtime_socket_path`` puts it.

    Re-derived rather than imported: a hook runs under the system interpreter
    with no AgentDeck on its path.
    """
    return classify.runtime_socket()


def runtime_is_up() -> bool:
    return classify.runtime_is_up()


def reason(command: str, cwd: str) -> str:
    return classify.queue_advice(command, cwd)


def main() -> int:
    if classify is None:
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str(payload.get("tool_input", {}).get("command", ""))
    if not classify.is_heavy(command):
        return 0
    if os.environ.get("AGENTDECK_HEAVY_SLOT_HELD", "").strip() not in ("", "0"):
        return 0
    if not runtime_is_up():
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason(command, cwd),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
