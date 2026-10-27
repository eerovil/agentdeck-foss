#!/usr/bin/env python3
"""Answer a Codex ``PermissionRequest`` from a prior approval instead of a guardian.

Codex runs this before it routes an escalation to whoever reviews approvals. Two
events matter:

``PermissionRequest``
    Emit ``decision.behavior = "allow"`` when this exact capability was approved
    before in this worker, repository, worktree and policy. Emit *no* decision
    otherwise, which is not a denial — it hands the request on to the reviewer
    that would have seen it anyway, so the guard is unchanged on every miss.

``PostToolUse``
    The tool ran, so whoever reviewed it allowed it. That promotes the request
    this hook deferred into a reusable grant. A denied command never runs, so it
    never reaches here and never becomes a grant.

Any failure at all prints nothing and exits 0. A silent hook costs a guardian
call; a crashing or confused one could cost an approval nobody made.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.approval_cache import (  # noqa: E402
    CapabilityLedger,
    capability_key,
    file_change_key,
    resolve_scope,
)

_SHELL_TOOLS = frozenset({"Bash", "bash", "shell", "shell_command", "unified_exec"})
_PATCH_TOOLS = frozenset({"apply_patch", "ApplyPatch", "Edit", "Write"})


def _allow(message: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow", "message": message},
                }
            },
            separators=(",", ":"),
        )
    )


def _key_for(payload: dict, scope) -> object:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    if tool_name in _SHELL_TOOLS:
        command = tool_input.get("command")
        if isinstance(command, list):
            # Codex may hand an argv list; only the plain string form is one
            # command we can normalize without guessing how it would be joined.
            command = " ".join(str(part) for part in command) if len(command) == 1 else None
        if not isinstance(command, str):
            return None
        return capability_key(command, cwd=payload.get("cwd") or scope.worktree, scope=scope)
    if tool_name in _PATCH_TOOLS:
        return file_change_key(tool_input.get("path") or payload.get("cwd"), scope=scope)
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    ledger = CapabilityLedger.from_env()
    if ledger is None:
        return 0
    event = payload.get("hook_event_name")
    request_id = payload.get("tool_use_id") or payload.get("turn_id")
    if not isinstance(request_id, str) or not request_id:
        return 0

    if event == "PostToolUse":
        ledger.observe_execution(request_id)
        return 0
    if event != "PermissionRequest":
        return 0

    worker = payload.get("session_id")
    scope = resolve_scope(worker if isinstance(worker, str) else "", payload.get("cwd") or "")
    if scope is None:
        return 0
    key = _key_for(payload, scope)
    hit = ledger.consult(key, request_id=request_id)  # type: ignore[arg-type]
    if hit is not None:
        _allow(hit.audit_line())
    return 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # Never let a cache defect become an approval outcome.
        raise SystemExit(0) from None
