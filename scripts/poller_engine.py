#!/usr/bin/env python3
"""Run the Poller Engine from this checkout (#2153).

The engine deploys with AgentDeck as ordinary software: the wake script, the deploy's
activation and an operator's diagnosis all run the engine through this file, so the engine that
answers is always the one in the same checkout. There is no separately built or promoted
artifact.

    scripts/poller_engine.py inspect --manifest poller.toml --overlay overlay.toml
    scripts/poller_engine.py cycle --manifest … --overlay … --binding binding.json

The engine runs under this checkout's venv (#2597): the launcher re-runs itself there, so every
caller — the wake, the deploy, an operator — gets the same interpreter
the deck uses without naming it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"
REEXEC_ENV = "AGENTDECK_ENGINE_REEXEC"


def venv_python() -> Path | None:
    """The checkout venv's interpreter to re-run under, or ``None`` to stay put.

    A checkout with no venv (a scratch worktree, a test run) keeps the interpreter it was
    started with; so does a launcher already running in the venv, or one that has re-run once.
    """
    python = VENV / "bin" / "python"
    if not python.exists() or os.environ.get(REEXEC_ENV):
        return None
    if Path(sys.prefix).resolve() == VENV.resolve():
        return None
    return python


if __name__ == "__main__" and (python := venv_python()) is not None:
    os.environ[REEXEC_ENV] = "1"
    os.execv(python, [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
# The guard is for this launcher alone; nothing the engine runs should inherit it.
os.environ.pop(REEXEC_ENV, None)

# First on the path, ahead of any installed copy: the engine must be this checkout's.
sys.path.insert(0, str(REPO / "src"))

from agentdeck.poller_engine.__main__ import main  # noqa: E402
from agentdeck.runtime_socket import runtime_socket_path  # noqa: E402


def with_host_defaults(argv: list[str]) -> list[str]:
    """The reaper must respect the runtime's Work workspace claims (#2149)."""
    if argv[:1] == ["reap-worktrees"] and not any(
        arg == "--runtime-socket" or arg.startswith("--runtime-socket=") for arg in argv
    ):
        return [*argv, "--runtime-socket", str(runtime_socket_path())]
    return argv


if __name__ == "__main__":
    raise SystemExit(main(with_host_defaults(sys.argv[1:])))
