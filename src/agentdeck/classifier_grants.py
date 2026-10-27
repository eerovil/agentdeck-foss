"""One-time approvals for a tool call Claude's classifier blocked (#2271).

When the boss answers a block with "Allow once", the runtime writes one grant
file for that chat and that exact call. Claude then retries the call, and the
deck's ``PreToolUse`` hook finds the file, removes it and answers ``allow``,
which settles the call before the classifier sees it again. Removing the file is
what uses the grant up, so a second identical call goes back to the classifier.

Stdlib only: the hook imports this on every gated tool call, so it must load
fast and must not pull in the rest of the package.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

# A grant the retry never used stops counting after this long. Claude retries
# straight after the hook answers, so anything older is a call the model chose
# not to repeat, and it must not approve some later, unrelated run.
GRANT_MAX_AGE_S = 3600.0

_GRANT_DIR = "classifier-grants"


def fingerprint(tool_name: str, tool_input: Any) -> str:
    """Identify one call. Shell commands compare with runs of whitespace
    squeezed, so re-indenting a retry still matches; any other change does not."""
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        subject: Any = " ".join(tool_input["command"].split())
    else:
        subject = tool_input
    raw = json.dumps([tool_name or "", subject], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _chat_dir(root: Path, account_key: str, worker_key: str) -> Path:
    chat = hashlib.sha256(f"{account_key}\0{worker_key}".encode()).hexdigest()
    return root / _GRANT_DIR / chat


def write_grant(
    root: Path, account_key: str, worker_key: str, tool_name: str, tool_input: Any
) -> Path:
    directory = _chat_dir(root, account_key, worker_key)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / fingerprint(tool_name, tool_input)
    target.write_text(str(time.time()))
    return target


def consume_grant(
    root: Path,
    account_key: str,
    worker_key: str,
    tool_name: str,
    tool_input: Any,
    *,
    now: float | None = None,
) -> bool:
    """Use up this chat's grant for this exact call. True only once per grant."""
    if not account_key or not worker_key:
        return False
    target = _chat_dir(root, account_key, worker_key) / fingerprint(tool_name, tool_input)
    try:
        age = (time.time() if now is None else now) - target.stat().st_mtime
        os.unlink(target)
    except OSError:
        return False
    return age <= GRANT_MAX_AGE_S


def clear_grants(root: Path, account_key: str, worker_key: str) -> None:
    """Drop every grant a chat still holds (its worker ended)."""
    directory = _chat_dir(root, account_key, worker_key)
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            entry.unlink()
        except OSError:
            pass
