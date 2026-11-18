"""The deck's services on macOS, as launchd agents (#3623).

On Linux the deck runs as systemd user units (``systemd/``). macOS has no systemd, so
``agentdeck-init`` writes the same jobs as property lists in ``~/Library/LaunchAgents``: the
web supervisor, the persistent runtime and the Work services (the Work router, the merge
release and the missed-start backstop, #4016), kept alive, and one poller instance woken every
two minutes like ``agentdeck-poller@.timer``. ``agentdeck-launchd`` adds a deck job
that is missing on a Mac set up before it existed, leaving the running ones alone (#4454).

What systemd gave the units for free is spelled out
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
SERVICES = f"{LABEL_PREFIX}.services"
# ``launchctl print`` exits with this for a job launchd does not know ("Could not find service").
LAUNCHCTL_NOT_FOUND = 113
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


def services_job(repo: Path, home: Path) -> dict:
    """``agentdeck services``, as ``systemd/agentdeck-services.service`` runs it (#4454).

    Its socket is ``~/.cache/agentdeck-services/services.sock``: launchd sets no
    ``XDG_RUNTIME_DIR``, so the web, run the same way, looks in the same place.
    """
    job = _service(
        SERVICES, [str(repo / ".venv" / "bin" / "agentdeck"), "services"], repo, home, "services"
    )
    # The unit's guard against a git or ssh password prompt nobody sees (#4340).
    job["EnvironmentVariables"].update(GIT_TERMINAL_PROMPT="0", SSH_ASKPASS_REQUIRE="never")
    # systemd's 90 s stop wait, not launchd's 20 s: a merge takes up to 30 s (#2963), and one
    # killed half-way is an uncertain write that is never sent again (#4003).
    job["ExitTimeOut"] = 90
    return job


def deck_jobs(repo: Path, home: Path) -> list[dict]:
    """The deck's own jobs, every project's poller apart, in the order they start."""
    return [runtime_job(repo, home), services_job(repo, home), web_job(repo, home)]


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


def install_missing(
    jobs: list[dict], home: Path, *, run: Runner = _run
) -> tuple[list[str], list[str]]:
    """Install only the jobs launchd is not running; returns (installed labels, failures).

    :func:`install` unloads a loaded job first, which would stop the runtime and every chat
    it holds; a repair must never do that to a job that is already up.
    """
    domain = f"gui/{os.getuid()}"
    missing, failures = [], []
    for job in jobs:
        printed = run(["launchctl", "print", f"{domain}/{job['Label']}"])
        if printed.returncode == LAUNCHCTL_NOT_FOUND:
            missing.append(job)
        elif printed.returncode != 0:
            # Unknown is not missing: installing would unload a job that may be running.
            failures.append(f"launchctl print {job['Label']}: {printed.stderr.strip()}")
    if missing:
        failures += install(missing, home, run=run)
    return [job["Label"] for job in missing], failures


def main(argv: list[str] | None = None) -> int:
    """``agentdeck-launchd``: add the deck's launchd jobs a Mac is missing.

    A console script of its own, never an ``agentdeck`` subcommand: this module names the
    poller's wake script, which the deck's own entry point must not reach.
    """
    import argparse
    import sys

    argparse.ArgumentParser(
        prog="agentdeck-launchd",
        description=(
            "On macOS, write and load any of the deck's launchd jobs (web, runtime, Work"
            " services) that launchd is not running. Running jobs are left alone."
        ),
    ).parse_args(sys.argv[1:] if argv is None else argv)
    if sys.platform != "darwin":
        print(
            "agentdeck-launchd: only on macOS; on Linux run"
            " `systemctl --user enable --now agentdeck-services`",
            file=sys.stderr,
        )
        return 2
    # The checkout this runs from, as ``setup_init.REPO`` says; not imported from there, which
    # would put the poller engine behind ``agentdeck``'s own entry point.
    repo = Path(__file__).resolve().parent.parent.parent
    home = Path.home()
    installed, failures = install_missing(deck_jobs(repo, home), home)
    for failure in failures:
        print(f"FAILED: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        "installed: " + ", ".join(installed) if installed else "every deck job is already running"
    )
    return 0
