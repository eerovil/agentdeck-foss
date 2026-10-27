#!/usr/bin/env python3
"""Bring a Claude classifier block to the boss, and honour their "Allow once".

Installed for owned Claude workers through the deck's ``--settings`` file
(#2271). Two events:

``PermissionDenied``
    Auto mode's classifier blocked a tool call. Report it to the persistent
    runtime, which shows it on the chat as a pending prompt, then wait for the
    boss's answer for as long as it takes. Claude waits on this hook, so the
    chat is paused meanwhile. "Allow once" answers ``retry: true``; the runtime
    has already written a one-time grant for this exact call.

``PreToolUse``
    If the chat holds a grant for this exact call, use it up and answer
    ``allow``, which settles the call before the classifier sees it again.
    Otherwise say nothing, so nothing else changes.

Any failure prints nothing and exits 0: a broken hook leaves the block in place
and never approves anything. The runtime is reached over its own Unix socket,
not the web process, so a web deploy does not interrupt a wait; a runtime that
is briefly unreachable is retried until it answers.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentdeck.classifier_grants import consume_grant  # noqa: E402
from agentdeck.command_artifacts import (  # noqa: E402
    ACCOUNT_KEY_ENV,
    CONTEXT_DIR_ENV,
    SESSION_TOKEN_ENV,
    WORKER_KEY_ENV,
)
from agentdeck.heavy_shim.classify import runtime_socket  # noqa: E402

# One long poll. The runtime answers sooner when the boss decides.
WAIT_S = 25
# Pause before asking again after the runtime could not be reached.
RETRY_S = 5.0
ALLOW_REASON = "The boss allowed this once after Claude's safety check blocked it."


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _call(method: str, path: str, body: dict | None = None, *, timeout: float = 10.0) -> dict:
    connection = _UnixConnection(runtime_socket(), timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"content-type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        if response.status >= 500:
            raise OSError(f"runtime answered {response.status}")
        parsed = json.loads(data or b"{}")
        return parsed if isinstance(parsed, dict) else {}
    finally:
        connection.close()


def _reason(payload: dict) -> str:
    for field in ("reason", "denial_reason", "permission_decision_reason", "message"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _on_denied(payload: dict) -> None:
    account_key = os.environ.get(ACCOUNT_KEY_ENV, "")
    worker_key = os.environ.get(WORKER_KEY_ENV, "")
    if not account_key or not worker_key:
        return
    tool_input = payload.get("tool_input")
    report = {
        "account_key": account_key,
        "key": worker_key,
        "session_token": os.environ.get(SESSION_TOKEN_ENV, ""),
        "tool_name": str(payload.get("tool_name") or ""),
        "tool_input": tool_input if isinstance(tool_input, dict) else {},
        "reason": _reason(payload),
    }
    block_id = ""
    while not block_id:
        try:
            answer = _call("POST", "/claude/blocks", report)
        except (OSError, ValueError, http.client.HTTPException):
            time.sleep(RETRY_S)
            continue
        block_id = str(answer.get("id") or "")
        if not block_id:
            # Refused: not a live owned worker, or the report did not match it.
            return
    while True:
        try:
            answer = _call(
                "GET",
                f"/claude/blocks/{block_id}?account_key={quote(account_key, safe='')}"
                f"&key={quote(worker_key, safe='')}&wait={WAIT_S}",
                timeout=WAIT_S + 15,
            )
        except (OSError, ValueError, http.client.HTTPException):
            time.sleep(RETRY_S)
            continue
        state = answer.get("state")
        if state == "pending":
            continue
        if state == "allowed":
            print(
                json.dumps(
                    {"hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}},
                    separators=(",", ":"),
                )
            )
        return


def _on_pre_tool_use(payload: dict) -> None:
    root = os.environ.get(CONTEXT_DIR_ENV, "")
    if not root:
        return
    if not consume_grant(
        Path(root),
        os.environ.get(ACCOUNT_KEY_ENV, ""),
        os.environ.get(WORKER_KEY_ENV, ""),
        str(payload.get("tool_name") or ""),
        payload.get("tool_input"),
    ):
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": ALLOW_REASON,
                }
            },
            separators=(",", ":"),
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event == "PermissionDenied":
            _on_denied(payload)
        elif event == "PreToolUse":
            _on_pre_tool_use(payload)
    except Exception:  # noqa: BLE001 -- a broken hook must never approve anything
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
