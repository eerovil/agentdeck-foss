"""Fresh reads of $CLAUDE_CONFIG_DIR/.credentials.json.

Tokens rotate: read fresh on every use, hold in a local variable only,
never store in AppState, never log. The file is mode 600; agentdeck runs
as the same user.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_access_token(config_dir: Path) -> str | None:
    """Return the current OAuth access token, or None if unavailable."""
    path = config_dir / ".credentials.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None
