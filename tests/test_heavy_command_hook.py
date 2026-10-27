"""The generic enforcement half of #1287: the Claude PreToolUse hook.

`tests/conftest.py` caps a direct `pytest` in this repo, but the other projects
on this host run Node and Playwright and never load it. This hook is what makes
the cap reach them, so its matching is worth pinning: a false negative is the
pile-up back again, and a false positive stops someone testing at all.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "heavy_command_hook.py"


def _module():
    spec = importlib.util.spec_from_file_location("heavy_command_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _module()


@pytest.fixture
def runtime_socket(tmp_path):
    path = tmp_path / "codex-runtime.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    yield path
    listener.close()


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "uv run pytest -q -m 'not browser'",
        ".venv/bin/pytest tests/test_jobs.py",
        "npm test",
        "npm run test",
        "pnpm test -- --reporter dot",
        "npx playwright test",
        "npx vitest run",
        "cargo test",
        "go test ./...",
        "make test",
        "cd /home/me/menu-app && npm test",
        "python3 -m pytest -q",
        "uv run python -m pytest -q",
        "/usr/bin/python3 -m pytest -q",
    ],
)
def test_a_whole_suite_is_recognised(command):
    assert hook.classify.is_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "rg pytest_helpers.py",
        "cat tests/test_jobs.py",
        "git commit -m 'add a test'",
        "ls node_modules",
        "echo 'x; pytest -q'",
        "printf '%s' 'agentdeck job status; pytest -q'",
        "echo 'x | npm test'",
    ],
)
def test_ordinary_commands_are_left_alone(command):
    assert not hook.classify.is_heavy(command)


@pytest.mark.parametrize(
    "command",
    [
        "agentdeck job submit --cwd . -- pytest -q",
        "AGENTDECK_HEAVY_SLOT_HELD=1 pytest -k one_thing",
    ],
)
def test_the_queue_and_the_escape_hatch_are_exempt(command):
    assert hook.EXEMPT.search(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo AGENTDECK_HEAVY_SLOT_HELD; pytest -q",
        "AGENTDECK_HEAVY_SLOT_HELD=0 pytest -q",
        "AGENTDECK_HEAVY_SLOT_HELD=1 echo ok; pytest -q",
        "agentdeck job status; pytest -q",
    ],
)
def test_text_and_a_disabled_hatch_do_not_exempt_a_suite(command, runtime_socket):
    output = _run(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        env={"AGENTDECK_CODEX_SOCKET": str(runtime_socket), "PATH": "/usr/bin:/bin"},
    )

    assert json.loads(output)["hookSpecificOutput"]["permissionDecision"] == "deny"


def _run(payload: dict, env: dict | None = None) -> str:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_a_heavy_bash_call_is_denied_with_the_command_to_run_instead(runtime_socket):
    output = _run(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "cwd": "/home/me/hass"},
        env={"AGENTDECK_CODEX_SOCKET": str(runtime_socket), "PATH": "/usr/bin:/bin"},
    )
    decision = json.loads(output)["hookSpecificOutput"]

    assert decision["permissionDecision"] == "deny"
    assert (
        "agentdeck job submit --cwd /home/me/hass -- pytest -q"
        in (decision["permissionDecisionReason"])
    )


def test_it_fails_open_when_the_runtime_is_not_running(tmp_path):
    """Denying while the queue is down would stop every project testing at all."""
    output = _run(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "cwd": "/home/me/hass"},
        env={"AGENTDECK_CODEX_SOCKET": str(tmp_path / "absent.sock"), "PATH": "/usr/bin:/bin"},
    )

    assert output == ""


def test_it_fails_open_when_only_a_stale_runtime_socket_remains(tmp_path):
    stale = tmp_path / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(stale))
    listener.close()
    output = _run(
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
        env={"AGENTDECK_CODEX_SOCKET": str(stale), "PATH": "/usr/bin:/bin"},
    )

    assert output == ""


def test_a_non_bash_tool_is_never_touched(runtime_socket):
    output = _run(
        {"tool_name": "Read", "tool_input": {"file_path": "pytest.ini"}},
        env={"AGENTDECK_CODEX_SOCKET": str(runtime_socket), "PATH": "/usr/bin:/bin"},
    )

    assert output == ""
