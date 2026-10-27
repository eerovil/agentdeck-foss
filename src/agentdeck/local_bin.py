"""Put ``~/.local/bin`` on a service process's PATH (#3151).

The systemd user manager boots with a bare PATH, before any login shell has run,
and ``gh`` and ``codex`` live in ``~/.local/bin``. Every long-running service then
failed each ``gh`` call, and the Codex runtime could not start its app-server,
until someone restarted it by hand. A fixed ``Environment=PATH=`` in the unit
would fix that but drop whatever else the host put on the manager's PATH, which
the agents these services start rely on, so each entry point adds just this one
directory to the PATH it was given.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def ensure_local_bin_on_path(environ: MutableMapping[str, str] = os.environ) -> None:
    local_bin = str(Path.home() / ".local" / "bin")
    entries = [entry for entry in environ.get("PATH", "").split(os.pathsep) if entry]
    present = {os.path.realpath(entry) for entry in entries}
    if os.path.realpath(local_bin) in present:
        return
    environ["PATH"] = os.pathsep.join([local_bin, *(entries or os.defpath.split(os.pathsep))])
