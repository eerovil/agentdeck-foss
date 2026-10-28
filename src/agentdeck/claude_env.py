"""Which Claude Code login a process the deck starts uses (agentdeck-foss#1).

The deck points each Claude process at its account's folder with ``CLAUDE_CONFIG_DIR``. On
Linux that is where the login lives (``.credentials.json``), so naming the default ``~/.claude``
changes nothing. On macOS the login lives in the Keychain, and Claude Code reads a *different*
Keychain item whenever ``CLAUDE_CONFIG_DIR`` is set, even to ``~/.claude``: a person logged in
with plain ``claude`` reads as logged out to every process the deck starts. So on macOS the
default folder is left unset, and the process finds the person's own login.
"""

from __future__ import annotations

import sys
from collections.abc import MutableMapping
from pathlib import Path


def uses_default_login(root: str | Path) -> bool:
    """True where naming ``root`` would hide the person's own login: macOS's ``~/.claude``."""
    if sys.platform != "darwin":
        return False
    return Path(root).expanduser().resolve() == (Path.home() / ".claude").resolve()


def point_at(env: MutableMapping[str, str], root: str | Path) -> None:
    """Set ``env`` up so Claude Code uses the account at ``root``."""
    if uses_default_login(root):
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(root)
