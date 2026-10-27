#!/usr/bin/env python3
"""Claude/Codex hook and capture launcher for poller worker shell results."""

from __future__ import annotations

import json
import os
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.command_artifacts import (  # noqa: E402
    ACCOUNT_KEY_ENV,
    CONTEXT_DIR_ENV,
    POLICY_MODE_ENV,
    ArtifactStore,
    execute_prepared,
    load_context,
    no_archive,
    safe_to_rewrite,
)
from agentdeck.guarded_commands import reaches_github  # noqa: E402
from agentdeck.heavy_shim.classify import is_heavy, queue_advice, runtime_is_up  # noqa: E402


def _response(payload: dict, event: str, *, updated_input: dict | None = None) -> dict:
    specific: dict[str, object] = {"hookEventName": payload.get("hook_event_name") or event}
    if updated_input is not None:
        specific.update(permissionDecision="allow", updatedInput=updated_input)
    return {"hookSpecificOutput": specific}


def _deny(reason: str) -> None:
    specific = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
    print(json.dumps({"hookSpecificOutput": specific}, separators=(",", ":")))


def _tool_bytes(response: object) -> tuple[bytes, bytes, int | None, int | None, bool]:
    """Normalize the two providers' shell result shapes without inventing status."""
    if isinstance(response, str):
        return response.encode(), b"", None, None, False
    if not isinstance(response, dict):
        return json.dumps(response, sort_keys=True).encode(), b"", None, None, False
    stdout = response.get("stdout", response.get("output", ""))
    stderr = response.get("stderr", "")
    if isinstance(stdout, (dict, list)):
        stdout = json.dumps(stdout, sort_keys=True)
    if isinstance(stderr, (dict, list)):
        stderr = json.dumps(stderr, sort_keys=True)
    exit_code = response.get("exit_code", response.get("exitCode"))
    term_signal = response.get("signal")
    return (
        str(stdout or "").encode(),
        str(stderr or "").encode(),
        int(exit_code) if isinstance(exit_code, int) else None,
        int(term_signal) if isinstance(term_signal, int) else None,
        bool(response.get("timed_out", response.get("timeout", False))),
    )


def _main_hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    root_value = os.environ.get(CONTEXT_DIR_ENV, "")
    account_key = os.environ.get(ACCOUNT_KEY_ENV, "")
    session_id = str(payload.get("session_id") or "")
    if not root_value or not account_key or not session_id:
        return 0
    root = Path(root_value)
    event = str(payload.get("hook_event_name") or "")
    context = load_context(root, account_key, session_id)
    if context is None:
        if os.environ.get(POLICY_MODE_ENV) == "enforce" and event == "PreToolUse":
            _deny("AgentDeck command artifact policy is unavailable; retry after it recovers.")
        return 0
    policy = context.get("policy") if isinstance(context.get("policy"), dict) else {}
    mode = str(policy.get("mode") or "off")
    if mode == "off":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = str(tool_input.get("command") or "")
    try:
        store = ArtifactStore(root)
    except OSError:
        if mode == "enforce" and event == "PreToolUse":
            _deny("AgentDeck command artifact storage is unavailable; retry after it recovers.")
        return 0
    covered, reason = safe_to_rewrite(str(payload.get("tool_name") or ""), tool_input)
    if event == "PreToolUse":
        # Rewriting a suite into the capture launcher must not bypass #1346's
        # queue policy. Both provider seams still ask the one shared parser.
        if (
            is_heavy(command)
            and os.environ.get("AGENTDECK_HEAVY_SLOT_HELD", "").strip() in ("", "0")
            and runtime_is_up()
        ):
            _deny(queue_advice(command, str(payload.get("cwd") or os.getcwd())))
            return 0
        if mode == "enforce" and "turn_id" not in payload and reaches_github(command):
            # The rewrite below answers ``allow``, and an ``allow`` skips Claude's
            # classifier. A command that can reach GitHub must be reviewed, so it
            # runs as written and goes uncaptured (#2271). Codex, whose payload
            # carries ``turn_id``, has its own reviewer and is left as it was.
            store.observe_uncovered(context, command, "classifier_review")
            return 0
        if not covered:
            store.observe_uncovered(context, command, reason)
            if mode == "enforce":
                _deny(
                    "AgentDeck cannot bound this interactive, streaming, or background "
                    "shell shape; rerun it as one non-interactive foreground command."
                )
            return 0
        if mode != "enforce":
            return 0
        try:
            run_id = store.prepare(context, tool_input)
        except OSError:
            _deny("AgentDeck could not prepare protected command capture; retry the command.")
            return 0
        updated = dict(tool_input)
        updated["command"] = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
            f"--run {shlex.quote(run_id)}"
        )
        # The launcher owns the original deadline. Keeping the provider's outer
        # timer would race artifact finalization after the child is terminated.
        updated.pop("timeout", None)
        updated.pop("timeout_ms", None)
        print(json.dumps(_response(payload, event, updated_input=updated), separators=(",", ":")))
        return 0
    if event != "PostToolUse" or mode != "observe":
        return 0
    if "turn_id" in payload:
        # Codex's compatibility hook receives the provider's already-truncated,
        # combined display result. It is useful coverage evidence, but it is not
        # an auditable copy of the two original streams.
        store.observe_uncovered(context, command, "provider_post_result_incomplete")
        return 0
    stdout, stderr, exit_code, term_signal, timed_out = _tool_bytes(payload.get("tool_response"))
    configured = tuple(str(item) for item in policy.get("no_archive_commands") or ())
    now = datetime.now(UTC)
    try:
        result = store.capture(
            context=context,
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            term_signal=term_signal,
            timed_out=timed_out,
            started_at=now,
            finished_at=now,
            archive=not no_archive(command, configured),
        )
    except OSError:
        return 0
    # Observe mode records the proposal privately and emits no provider-visible
    # hook response at all.
    del result
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--run":
        root = os.environ.get(CONTEXT_DIR_ENV, "")
        if not root:
            return 127
        return execute_prepared(Path(root), sys.argv[2])
    return _main_hook()


if __name__ == "__main__":
    raise SystemExit(main())
