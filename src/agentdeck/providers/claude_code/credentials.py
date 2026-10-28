"""Fresh reads of the account's Claude Code login: $CLAUDE_CONFIG_DIR/.credentials.json, or on
macOS the Keychain item Claude Code keeps it in.

Tokens rotate: read fresh on every use, hold in a local variable only,
never store in AppState, never log. The file is mode 600; agentdeck runs
as the same user.

macOS has no ``.credentials.json``: Claude Code writes the same JSON to the login Keychain, under
the service ``Claude Code-credentials`` for the default ``~/.claude`` (agentdeck-foss#1). The
deck's agents use that login there (``claude_env``), so the usage bars read it too. Another
account folder's item is named after a hash this module does not know, so it stays unread.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ... import claude_env

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_S = 5.0

Keychain = Callable[[str], str | None]


def _keychain(service: str) -> str | None:
    """The password stored under ``service`` in the login Keychain, or None."""
    security = shutil.which("security")
    if security is None:
        return None
    try:
        done = subprocess.run(
            [security, "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def read_access_token(config_dir: Path, *, keychain: Keychain = _keychain) -> str | None:
    """Return the current OAuth access token, or None if unavailable."""
    try:
        raw = (config_dir / ".credentials.json").read_text()
    except OSError:
        raw = keychain(KEYCHAIN_SERVICE) if claude_env.uses_default_login(config_dir) else None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None
