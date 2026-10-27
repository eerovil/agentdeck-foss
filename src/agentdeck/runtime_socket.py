"""Where the persistent runtime's Unix socket lives (#2597).

A leaf module on purpose: the web process, the runtime and the Poller Engine's launcher all
name this one path, and the engine must not pay for importing the providers package to learn it.
"""

from __future__ import annotations

import os
from pathlib import Path


def runtime_socket_path() -> Path:
    """Return the user-local socket shared by the web and runtime services."""
    configured = os.environ.get("AGENTDECK_CODEX_SOCKET")
    if configured:
        return Path(configured).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("~/.cache").expanduser()
    return base / "agentdeck" / "codex-runtime.sock"
