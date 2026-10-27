"""Agent-triggered runtime restart from inside an agent turn (issue #20).

The persistent runtime unit uses ``KillMode=control-group``. An agent worker and
the bash it spawns both live in that cgroup, so a plain ``systemctl restart`` of
the runtime tears the agent down mid-turn — its own restart command is killed
before it returns. :func:`trigger_detached_restart` runs the restart from a
*transient* systemd scope (its own cgroup) via ``systemd-run``, so the runtime's
cgroup teardown does not kill the restart job.

Continuing afterwards is a separate concern and lives in
``agentdeck.restart_nudge``: an agent registers its session key there before
triggering, and the fresh runtime nudges every registered chat on boot.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _unit_from_cgroup(text: str) -> str | None:
    """Return the deepest ``*.service`` in a cgroup-v2 membership line, if any."""
    services = re.findall(r"[A-Za-z0-9@:._-]+\.service", text)
    return services[-1] if services else None


def detect_runtime_unit() -> str | None:
    """Best-effort: the systemd unit this process runs under (cgroup-v2 leaf)."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    return _unit_from_cgroup(text)


def looks_like_runtime_unit(service: str) -> bool:
    name = service.lower()
    return "agentdeck" in name and "codex" in name


def trigger_detached_restart(service: str) -> None:
    """Restart ``service`` from a transient scope that outlives its cgroup.

    ``systemd-run --user --collect`` starts a throwaway unit in its own cgroup;
    it runs ``systemctl --user restart <service>`` and is reaped on completion,
    so tearing down the runtime cgroup does not kill the restart job.
    """
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--collect",
            "--quiet",
            "systemctl",
            "--user",
            "restart",
            service,
        ],
        check=True,
    )
