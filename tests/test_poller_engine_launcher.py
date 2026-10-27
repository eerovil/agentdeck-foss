"""The engine launcher re-runs itself under the checkout's venv (#2597).

Every caller runs ``scripts/poller_engine.py`` directly through its ``python3`` shebang — the
wake, the canary and the card-control scripts already written on disk — so the launcher is the
one place that can put all of them on the venv the deck uses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "poller_engine.py"


def _checkout(tmp_path: Path) -> Path:
    """A throwaway checkout: the real launcher over a stub engine that reports how it ran."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(LAUNCHER, repo / "scripts" / "poller_engine.py")
    engine = repo / "src" / "agentdeck" / "poller_engine"
    engine.mkdir(parents=True)
    (repo / "src" / "agentdeck" / "__init__.py").write_text("")
    (engine / "__init__.py").write_text("")
    (engine / "__main__.py").write_text(
        "import os, sys\n"
        "def main(argv):\n"
        "    print('engine', sys.executable, os.environ.get('AGENTDECK_ENGINE_REEXEC'), *argv)\n"
        "    return 0\n"
    )
    (repo / "src" / "agentdeck" / "runtime_socket.py").write_text(
        "def runtime_socket_path():\n    return '/rt.sock'\n"
    )
    return repo


def _fake_venv(repo: Path, log: Path) -> Path:
    """A venv whose ``python`` records its arguments and then runs the real interpreter."""
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\necho "venv $*" >> {log}\nexec {sys.executable} "$@"\n')
    python.chmod(0o755)
    return python


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "AGENTDECK_ENGINE_REEXEC"}
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "poller_engine.py"), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=env,
    )


def test_the_launcher_re_runs_once_under_the_checkouts_venv(tmp_path):
    repo = _checkout(tmp_path)
    log = tmp_path / "venv.log"
    _fake_venv(repo, log)

    done = _run(repo, "cycle", "--manifest", "m")

    launched = log.read_text().splitlines()
    assert launched == [f"venv {repo / 'scripts' / 'poller_engine.py'} cycle --manifest m"]
    # The engine got the caller's arguments, and the guard did not leak to what it runs.
    assert done.stdout.split()[2:] == ["None", "cycle", "--manifest", "m"]


def test_a_checkout_with_no_venv_runs_where_it_was_started(tmp_path):
    repo = _checkout(tmp_path)

    done = _run(repo, "inspect")

    assert done.stdout.split() == ["engine", sys.executable, "None", "inspect"]
