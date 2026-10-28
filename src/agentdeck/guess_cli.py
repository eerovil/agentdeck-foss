"""The one Haiku call behind the deck's guesses (#3367), kept apart from ``guess``.

It imports only the account model, so the test suite can stand in for it in every
test without making every test depend on the providers ``guess`` reads chats through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil

from . import claude_env
from .models import Account

log = logging.getLogger(__name__)

GUESS_MODEL = "haiku"
GUESS_TIMEOUT_S = 20.0
MAX_GUESS_CHARS = 300
NO_GUESS = "NONE"
_SYSTEM_PROMPT = "Answer exactly as instructed. Reply with the answer only, on one line."
_SPACE_RE = re.compile(r"\s+")


def guess_command(binary: str) -> list[str]:
    """The measured call: Haiku with nothing loaded but the prompt on stdin."""
    return [
        binary,
        "-p",
        "--model",
        GUESS_MODEL,
        "--tools",
        "",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt",
        _SYSTEM_PROMPT,
        "--output-format",
        "text",
    ]


def _guess_env(account: Account) -> dict[str, str]:
    env = dict(os.environ)
    # The web process may run inside an owned chat; a guess is nobody's session.
    for name in list(env):
        if name.startswith("AGENTDECK_"):
            env.pop(name)
    claude_env.point_at(env, account.root)  # macOS keeps ~/.claude unset
    return env


async def run_claude(account: Account, prompt: str) -> str | None:
    """Ask Haiku on ``account``; the reply's first line, or None on any failure."""
    binary = shutil.which("claude")
    if binary is None:
        log.info("guess skipped: claude is not on PATH")
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *guess_command(binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
            env=_guess_env(account),
        )
    except OSError as exc:
        log.info("guess skipped: %s", exc)
        return None
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode()), timeout=GUESS_TIMEOUT_S
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        log.info("guess timed out on %s", account.key)
        return None
    if proc.returncode != 0:
        log.info(
            "guess failed on %s: exit %s %s",
            account.key,
            proc.returncode,
            err.decode(errors="replace")[:200],
        )
        return None
    return clean_reply(out.decode(errors="replace"))


def clean_reply(reply: str) -> str | None:
    """The first non-empty line, unquoted and capped; None for no guess."""
    line = next((line for line in reply.splitlines() if line.strip()), "")
    line = _SPACE_RE.sub(" ", line).strip().strip("\"'“”‘’`")
    if not line or line.upper().rstrip(".") == NO_GUESS:
        return None
    return line[:MAX_GUESS_CHARS]
