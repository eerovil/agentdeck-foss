"""The deck's services on macOS, as launchd agents (#3623).

On Linux the deck runs as systemd user units (``systemd/``). macOS has no systemd, so
``agentdeck-init`` writes the same three jobs as property lists in ``~/Library/LaunchAgents``:
the web supervisor and the persistent runtime, kept alive, and one poller instance woken every
two minutes like ``agentdeck-poller@.timer``. What systemd gave the units for free is spelled out
here: the environment, because launchd's ``PATH`` holds only the system folders, so Homebrew's
and ``~/.local/bin``'s tools would be missing.

Agents run while the person is logged in, which on a Mac is the normal case; there is no
lingering to switch on.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable
from pathlib import Path

LABEL_PREFIX = "com.agentdeck"
WEB = f"{LABEL_PREFIX}.web"
RUNTIME = f"{LABEL_PREFIX}.runtime"
AGENTS_DIR = Path("~/Library/LaunchAgents")
# The poller's own timer waits two minutes after a wake ends; launchd's interval is the
# nearest thing, and a wake that runs long is simply not started twice.
POLLER_INTERVAL_S = 120


def poller_label(instance_id: str) -> str:
    return f"{LABEL_PREFIX}.poller.{instance_id}"


def environment(home: Path) -> dict[str, str]:
    """What every job needs that launchd does not give it."""
    path = ":".join(
        [
            str(home / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    )
    return {
        "PATH": path,
        "HOME": str(home),
        "AGENTDECK_CONFIG": str(home / ".config" / "agentdeck" / "config.toml"),
    }


def web_job(repo: Path, home: Path) -> dict:
    return _service(
        WEB,
        [str(repo / ".venv" / "bin" / "python"), "-m", "agentdeck.web_supervisor"],
        repo,
        home,
        "web",
    )


def runtime_job(repo: Path, home: Path) -> dict:
    return _service(
        RUNTIME, [str(repo / ".venv" / "bin" / "agentdeck"), "codex-runtime"], repo, home, "runtime"
    )


def poller_job(repo: Path, home: Path, root: Path, instance_id: str) -> dict:
    instance = root / "instances" / instance_id
    log = str(instance / "cycle.log")
    return {
        "Label": poller_label(instance_id),
        "ProgramArguments": [
            str(repo / ".venv" / "bin" / "python"),
            str(repo / "scripts" / "poller_wake.py"),
            "--root",
            str(root),
            "--instance",
            instance_id,
        ],
        "WorkingDirectory": str(instance),
        "EnvironmentVariables": environment(home),
        "StartInterval": POLLER_INTERVAL_S,
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _service(label: str, program: list[str], repo: Path, home: Path, name: str) -> dict:
    logs = home / "Library" / "Logs" / "agentdeck"
    return {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": environment(home),
        "RunAtLoad": True,
        # Restarted when it exits, as `Restart=on-failure` does, with launchd's own pause.
        "KeepAlive": True,
        "StandardOutPath": str(logs / f"{name}.log"),
        "StandardErrorPath": str(logs / f"{name}.log"),
    }


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def install(jobs: list[dict], home: Path, *, run: Runner = _run) -> list[str]:
    """Write each job's property list and (re)load it; returns the failures, if any.

    A job already loaded is unloaded first, so a rerun picks up a changed file.
    """
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (home / "Library" / "Logs" / "agentdeck").mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    failures = []
    for job in jobs:
        plist = agents / f"{job['Label']}.plist"
        plist.write_bytes(plistlib.dumps(job))
        run(["launchctl", "bootout", f"{domain}/{job['Label']}"])  # not loaded is fine
        loaded = run(["launchctl", "bootstrap", domain, str(plist)])
        if loaded.returncode != 0:
            failures.append(f"launchctl bootstrap {plist}: {loaded.stderr.strip()}")
    return failures
