from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

from agentdeck import github_identity
from agentdeck.models import Account, InjectResult, PendingInteraction
from agentdeck.providers.codex import (
    APPROVALS_REVIEWER_CONFIG_OVERRIDE,
    LOGIN_SHELL_CONFIG_OVERRIDE,
    WRITABLE_ROOTS_CONFIG_OVERRIDE,
)
from agentdeck.providers.codex.appserver import (
    AppServerError,
    CodexAppServer,
    _PendingRequest,
    _turn_input,
)
from agentdeck.providers.codex.diagnostics import redact_diagnostic
from agentdeck.providers.instructions import (
    agentdeck_session_instructions,
    relocation_notice,
    strip_session_guide,
    wrap_session_guide,
)


def _server(tmp_path: Path) -> CodexAppServer:
    return CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
    )


def test_turn_input_builds_text_and_local_images(tmp_path):
    images = [tmp_path / "one.png", tmp_path / "two.webp"]
    assert _turn_input("Inspect these", images) == [
        {"type": "text", "text": "Inspect these"},
        {"type": "localImage", "path": str(images[0])},
        {"type": "localImage", "path": str(images[1])},
    ]


@pytest.mark.parametrize(
    "credential",
    [
        "Bearer secret-token",
        "Basic dXNlcjpwYXNz",
        "Authorization: Basic dXNlcjpwYXNz",
        "ghp_abcdefghijklmnopqrstuvwxyz",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
        "password=hunter2",
    ],
)
def test_diagnostics_redact_common_credentials(credential):
    assert credential not in redact_diagnostic(f"failure: {credential}")


async def test_app_server_enables_live_web_search(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.setenv("AGENTDECK_HEAVY_SHIM_DIR", str(tmp_path / "heavy-shim"))
    monkeypatch.setattr(
        "agentdeck.providers.codex.appserver.gh_shim.real_gh",
        lambda _environment: "/usr/bin/gh",
    )

    class IdleStdout:
        async def readline(self):
            await asyncio.Future()

        async def read(self, _size):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = Stdin()
            self.stdout = IdleStdout()
            self.stderr = IdleStdout()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    spawned = {}

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return Process()

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        process_factory=factory,
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server._request = AsyncMock(return_value={})
    server._notify = AsyncMock()
    server._recover_owned = AsyncMock()

    await server.start()
    assert spawned["args"] == (
        "codex",
        "app-server",
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "--config",
        APPROVALS_REVIEWER_CONFIG_OVERRIDE,
        "--config",
        LOGIN_SHELL_CONFIG_OVERRIDE,
        "--stdio",
    )
    assert spawned["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
    assert spawned["kwargs"]["env"]["AGENTDECK_GH_CONSUMER"] == "codex:test"
    # Codex has no PreToolUse hook, so the heavy-command queue reaches its
    # workers only because this directory shadows `pytest` and friends (#1346).
    path = spawned["kwargs"]["env"]["PATH"].split(os.pathsep)
    assert str(tmp_path / "heavy-shim") in path
    pythonpath = spawned["kwargs"]["env"]["PYTHONPATH"].split(os.pathsep)
    assert str(tmp_path / "heavy-shim" / "python") in pythonpath
    git_env = spawned["kwargs"]["env"]
    # Every GitHub transport goes through AgentDeck's helper, never the host login (#2289).
    assert [
        (git_env[f"GIT_CONFIG_KEY_{i}"], git_env[f"GIT_CONFIG_VALUE_{i}"])
        for i in range(int(git_env["GIT_CONFIG_COUNT"]))
    ] == github_identity.git_config_entries()
    assert not any(
        "gh auth git-credential" in value for value in git_env.values()
    )
    assert spawned["kwargs"]["stderr"] == asyncio.subprocess.PIPE
    stderr_task = server._stderr_task
    assert stderr_task is not None and not stderr_task.done()
    # No base_url configured on this server → no self-discovery env leaks in.
    assert "AGENTDECK_URL" not in spawned["kwargs"]["env"]
    await server.stop()
    assert server._stderr_task is None
    assert stderr_task.done()


async def test_persistent_app_server_connects_through_a_replaceable_proxy(tmp_path):
    class IdleStream:
        async def readline(self):
            await asyncio.Future()

        async def read(self, _size):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        returncode = None
        stdin = Stdin()
        stdout = IdleStream()
        stderr = IdleStream()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    spawned = {}

    async def factory(*args, **_kwargs):
        spawned["args"] = args
        return Process()

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        process_factory=factory,
    )
    socket = tmp_path / "persistent.sock"
    server._ensure_persistent_backend = AsyncMock(return_value=socket)
    server._request = AsyncMock(return_value={})
    server._notify = AsyncMock()
    server._recover_owned = AsyncMock()

    await server.start()

    assert spawned["args"] == (
        "codex",
        "app-server",
        "proxy",
        "--sock",
        str(socket),
    )
    server._ensure_persistent_backend.assert_awaited_once()
    await server.stop()


async def test_persistent_backend_runs_as_a_separate_redacted_user_service(
    tmp_path, monkeypatch
):
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.abacus]
url = "https://abacus.example/mcp/"
bearer_token_env_var = "ABACUS_MCP_TOKEN"
""".lstrip()
    )
    socket = tmp_path / "agentdeck-app-server-test" / "control.sock"
    socket.parent.mkdir()
    socket.touch()
    server = CodexAppServer(Account("codex:test", "codex", "test", tmp_path))
    monkeypatch.setattr(
        server,
        "_backend_identity",
        lambda: ("agentdeck-codex-app-server-test.service", socket),
    )
    monkeypatch.setattr(
        "agentdeck.providers.codex.appserver.shutil.which",
        lambda _command, *, path: "/opt/codex/bin/codex",
    )

    calls = []
    call_kwargs = []

    class Process:
        def __init__(self, returncode):
            self.returncode = returncode

        async def wait(self):
            return self.returncode

    async def create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        call_kwargs.append(kwargs)
        return Process(1 if args[0] == "systemctl" else 0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    env = {
        "AGENTDECK_GH_CONSUMER": "codex:test",
        "AGENTDECK_URL": "http://127.0.0.1:8420",
        "PATH": "/opt/codex/bin:/usr/bin",
        "PYTHONPATH": "/opt/agentdeck/heavy-shim/python",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
        "ABACUS_MCP_TOKEN": "token-must-not-reach-command-line",
        "UNRELATED_SECRET": "not-forwarded",
    }

    assert await server._ensure_persistent_backend(env) == socket
    assert calls[0] == (
        "systemctl",
        "--user",
        "is-active",
        "--quiet",
        "agentdeck-codex-app-server-test.service",
    )
    command = calls[1]
    assert command[:3] == ("systemd-run", "--user", "--collect")
    assert "--property=Restart=on-failure" in command
    assert "--property=StandardOutput=null" in command
    assert "--property=StandardError=null" in command
    assert "--property=LimitNOFILE=65536" in command
    assert "--property=RuntimeDirectory=agentdeck-app-server-test" in command
    assert "--setenv=CODEX_HOME=" + str(tmp_path) in command
    assert "--setenv=AGENTDECK_GH_CONSUMER=codex:test" in command
    assert "--setenv=PYTHONPATH=/opt/agentdeck/heavy-shim/python" in command
    assert "--setenv=GIT_CONFIG_VALUE_0=!gh auth git-credential" in command
    assert "--setenv=ABACUS_MCP_TOKEN" in command
    assert not any("token-must-not-reach-command-line" in arg for arg in command)
    assert not any("UNRELATED_SECRET" in arg for arg in command)
    assert call_kwargs[1]["env"] is env
    assert command[-14:] == (
        "/opt/codex/bin/codex",
        "app-server",
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "--config",
        APPROVALS_REVIEWER_CONFIG_OVERRIDE,
        "--config",
        LOGIN_SHELL_CONFIG_OVERRIDE,
        "--listen",
        "unix://" + str(socket),
    )


async def test_active_persistent_backend_detects_missing_mcp_credentials(
    tmp_path, monkeypatch
):
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.abacus]
url = "https://abacus.example/mcp/"
bearer_token_env_var = "ABACUS_MCP_TOKEN"
""".lstrip()
    )
    socket = tmp_path / "control.sock"
    socket.touch()
    server = CodexAppServer(Account("codex:test", "codex", "test", tmp_path))
    monkeypatch.setattr(server, "_backend_identity", lambda: ("backend.service", socket))

    class Process:
        returncode = 0

        async def wait(self):
            return self.returncode

        async def communicate(self):
            return b"CODEX_HOME=/tmp PATH=/usr/bin\n", b""

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=Process())
    )

    assert await server._ensure_persistent_backend(
        {"ABACUS_MCP_TOKEN": "secret"}
    ) == socket
    assert server._backend_env_stale is True

    Process.communicate = AsyncMock(
        return_value=(b'ABACUS_MCP_TOKEN="secret"\n', b"")
    )
    assert await server._ensure_persistent_backend(
        {"ABACUS_MCP_TOKEN": "secret"}
    ) == socket
    assert server._backend_env_stale is False


async def test_active_persistent_backend_detects_stale_worker_interception(
    tmp_path, monkeypatch
):
    socket = tmp_path / "control.sock"
    socket.touch()
    server = CodexAppServer(Account("codex:test", "codex", "test", tmp_path))
    monkeypatch.setattr(server, "_backend_identity", lambda: ("backend.service", socket))

    class Process:
        returncode = 0

        async def wait(self):
            return self.returncode

        async def communicate(self):
            return b"PATH=/old/bin PYTHONPATH=/old/python\n", b""

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=Process())
    )

    assert (
        await server._ensure_persistent_backend(
            {"PATH": "/shim:/usr/bin", "PYTHONPATH": "/shim/python"}
        )
        == socket
    )
    assert server._backend_env_stale is True


async def test_stale_persistent_backend_refreshes_only_after_threads_are_idle(
    tmp_path, monkeypatch
):
    server = _server(tmp_path)
    server._persistent_backend = True
    server._backend_env_stale = False
    server._persistent_backend_env_stale = AsyncMock(return_value=True)
    server._owned.add("thread-existing")
    server._threads["thread-existing"] = {
        "id": "thread-existing",
        "status": {"type": "active"},
    }
    server.stop = AsyncMock()
    server.start = AsyncMock()
    subprocess_exec = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)

    assert await server._refresh_persistent_backend_if_idle() is False
    server._persistent_backend_env_stale.assert_awaited_once()
    server.stop.assert_not_awaited()
    subprocess_exec.assert_not_awaited()

    server._threads["thread-existing"]["status"] = {"type": "idle"}

    class Process:
        async def wait(self):
            return 0

    subprocess_exec.return_value = Process()
    assert await server._refresh_persistent_backend_if_idle() is True
    server.stop.assert_awaited_once()
    subprocess_exec.assert_awaited_once_with(
        "systemctl", "--user", "stop", server._backend_identity()[0]
    )
    server.start.assert_awaited_once()


async def test_existing_turn_waits_for_stale_backend_refresh(tmp_path):
    server = _server(tmp_path)
    server._persistent_backend = True
    server.start = AsyncMock()
    server._refresh_persistent_backend_if_idle = AsyncMock(return_value=False)
    server._owned.add("thread-existing")
    server._loaded.add("thread-existing")
    server._request = AsyncMock()

    result = await server.start_turn("thread-existing", "continue", wait=False)

    assert result.accepted is False
    assert result.reason == (
        "Codex app-server environment changed while an existing thread is active; "
        "retry after it becomes idle"
    )
    server._request.assert_not_awaited()


async def test_backend_refresh_cannot_race_an_accepting_turn(tmp_path, monkeypatch):
    server = _server(tmp_path)
    server._persistent_backend = True
    server._backend_env_stale = False
    server._owned.add("thread-existing")
    server._loaded.add("thread-existing")
    server._threads["thread-existing"] = {
        "id": "thread-existing",
        "status": {"type": "idle"},
    }
    server.start = AsyncMock()
    turn_request_started = asyncio.Event()
    accept_turn = asyncio.Event()

    async def request(method, params, **kwargs):
        if method == "turn/start":
            turn_request_started.set()
            await accept_turn.wait()
            return {"turn": {"id": "turn-new"}}
        raise AssertionError(method)

    server._request = request
    subprocess_exec = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", subprocess_exec)

    turn = asyncio.create_task(
        server.start_turn("thread-existing", "continue", wait=False)
    )
    await turn_request_started.wait()
    # Drift discovered while a turn is crossing the backend boundary must wait
    # for that acceptance, then refuse to refresh over the active turn.
    server._backend_env_stale = True
    create = asyncio.create_task(server.create_thread(tmp_path))
    await asyncio.sleep(0)
    accept_turn.set()

    assert (await turn).accepted is True
    rejected = await create
    assert rejected.accepted is False
    assert "existing thread is active" in rejected.reason
    subprocess_exec.assert_not_awaited()


def test_persistent_backend_identity_is_isolated_by_runtime_scope(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    live = CodexAppServer(account, backend_scope="/run/user/1000/agentdeck/runtime.sock")
    staging = CodexAppServer(
        account, backend_scope="/run/user/1000/agentdeck-staging/runtime.sock"
    )

    live_unit, live_socket = live._backend_identity()
    staging_unit, staging_socket = staging._backend_identity()

    assert live_unit != staging_unit
    assert live_socket != staging_socket


def test_a_codex_worker_pushes_through_the_agentdeck_helper_never_the_host_login(tmp_path):
    """#2289: the Codex spawn used `gh auth git-credential` — the host's human login."""
    ambient_helper_called = tmp_path / "ambient-helper-called"
    ambient_helper = tmp_path / "ambient-helper"
    ambient_helper.write_text(
        "#!/bin/sh\n"
        f"touch {ambient_helper_called}\n"
        "printf 'username=ambient\\npassword=wrong-token\\n'\n"
    )
    ambient_helper.chmod(0o700)
    (tmp_path / ".gitconfig").write_text(
        "[credential \"https://github.com\"]\n"
        f"\thelper = !{ambient_helper}\n"
    )
    repository = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:eerovil/repo.git"],
        cwd=repository,
        check=True,
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("AGENTDECK_", "PYTHONPATH"))
    }
    environment.update(
        HOME=str(tmp_path),
        PATH="/usr/bin:/bin",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        GH_TOKEN="inherited-human-token",
        GITHUB_TOKEN="inherited-human-token",
    )
    github_identity.configure_github_git(environment)

    assert "GH_TOKEN" not in environment and "GITHUB_TOKEN" not in environment
    remote = subprocess.run(
        ["git", "remote", "get-url", "--push", "origin"],
        cwd=repository, env=environment, check=True, capture_output=True, text=True,
    )
    credential = subprocess.run(
        ["git", "credential", "fill"],
        env=environment,
        input="protocol=https\nhost=github.com\npath=eerovil/repo.git\n\n",
        check=True, capture_output=True, text=True, timeout=30,
    )

    assert remote.stdout.strip() == "https://github.com/eerovil/repo.git"
    assert "username=eerovil-agent" in credential.stdout
    assert "password=test-agent-token" in credential.stdout
    assert not ambient_helper_called.exists()


async def test_app_server_injects_agentdeck_url(tmp_path):
    class IdleStdout:
        async def readline(self):
            await asyncio.Future()

        async def read(self, _size):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = Stdin()
            self.stdout = IdleStdout()
            self.stderr = IdleStdout()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    spawned = {}

    async def factory(*args, **kwargs):
        spawned["kwargs"] = kwargs
        return Process()

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        base_url="http://127.0.0.1:8756",
        process_factory=factory,
        persistent_backend=False,
    )
    server._request = AsyncMock(return_value={})
    server._notify = AsyncMock()
    server._recover_owned = AsyncMock()

    await server.start()
    # One app-server process hosts many threads, so only the base is injected.
    assert spawned["kwargs"]["env"]["AGENTDECK_URL"] == "http://127.0.0.1:8756"
    assert "AGENTDECK_SESSION_KEY" not in spawned["kwargs"]["env"]
    await server.stop()


async def test_app_server_does_not_inherit_another_sessions_identity(
    tmp_path, monkeypatch
):
    """Started from inside an owned session, the ambient AGENTDECK_* name *that*
    session — every thread this server hosts would otherwise claim its key."""

    class IdleStdout:
        async def readline(self):
            await asyncio.Future()

        async def read(self, _size):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = Stdin()
            self.stdout = IdleStdout()
            self.stderr = IdleStdout()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    spawned = {}

    async def factory(*args, **kwargs):
        spawned["kwargs"] = kwargs
        return Process()

    monkeypatch.setenv("AGENTDECK_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "codex:other:thread-xyz")

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        base_url="http://127.0.0.1:8756",
        process_factory=factory,
        persistent_backend=False,
    )
    server._request = AsyncMock(return_value={})
    server._notify = AsyncMock()
    server._recover_owned = AsyncMock()

    await server.start()
    env = spawned["kwargs"]["env"]
    assert env["AGENTDECK_URL"] == "http://127.0.0.1:8756"  # ours, not the ambient one
    assert "AGENTDECK_SESSION_KEY" not in env
    await server.stop()


async def test_app_server_logs_bounded_stderr_with_account_context(tmp_path, caplog):
    class Stderr:
        def __init__(self):
            self.chunks = [
                b"startup failed: Bear",
                b"er secret-token\n",
                b"x" * (8 * 1024),
                b"x" * (8 * 1024),
                (b"x" * 4_000) + b"\nlate create failure\n",
                b"",
            ]
            self.read_sizes = []

        async def read(self, size):
            self.read_sizes.append(size)
            return self.chunks.pop(0)

    class Process:
        stderr = Stderr()

    server = _server(tmp_path)
    server._process = Process()

    with caplog.at_level(logging.WARNING):
        await server._stderr_loop()

    records = [record for record in caplog.records if "app-server stderr" in record.message]
    assert records
    assert all(size <= 8 * 1024 for size in Process.stderr.read_sizes)
    assert all(len(record.message) < 9_000 for record in records)
    assert "codex:test" in caplog.text
    assert "startup failed" in caplog.text
    assert "Bearer [redacted]" in caplog.text
    assert "secret-token" not in caplog.text
    assert "omitted oversized stderr line" in caplog.text
    assert "late create failure" in caplog.text


async def test_cancelled_app_server_start_stops_the_process_and_readers(tmp_path):
    class IdleStream:
        async def readline(self):
            await asyncio.Future()

        async def read(self, _size):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = Stdin()
            self.stdout = IdleStream()
            self.stderr = IdleStream()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    process = Process()
    initialize_started = asyncio.Event()

    async def factory(*_args, **_kwargs):
        return process

    async def request(*_args, **_kwargs):
        initialize_started.set()
        await asyncio.Future()

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        process_factory=factory,
        persistent_backend=False,
    )
    server._request = request
    start_task = asyncio.create_task(server.start())
    await initialize_started.wait()
    stdout_task = server._reader_task
    stderr_task = server._stderr_task

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert process.returncode == 0
    assert server._process is None
    assert server._reader_task is None
    assert server._stderr_task is None
    assert stdout_task is not None and stdout_task.done()
    assert stderr_task is not None and stderr_task.done()


async def test_request_user_input_round_trip(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._server_request(
        41,
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "questions": [
                {
                    "id": "database",
                    "header": "Database",
                    "question": "Which database should we use?",
                    "isOther": True,
                    "options": [
                        {"label": "Postgres", "description": "Relational"},
                        {"label": "SQLite", "description": "Embedded"},
                    ],
                }
            ],
        },
    )

    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.kind == "question"
    assert interaction.questions[0].prompt == "Which database should we use?"
    assert [option.label for option in interaction.questions[0].options] == [
        "Postgres",
        "SQLite",
    ]

    writes = []

    async def write(message):
        writes.append(message)

    server._write = write
    result = await server.answer(
        "thread-1",
        interaction.id,
        answers={"database": ["Postgres"]},
    )
    assert result.accepted
    assert writes == [
        {"id": 41, "result": {"answers": {"database": {"answers": ["Postgres"]}}}}
    ]
    assert server.interaction("thread-1") is None


async def test_recover_owned_includes_vscode_persisted_agentdeck_threads(tmp_path):
    ours = tmp_path / "sessions" / "2026" / "07" / "14" / "ours.jsonl"
    theirs = tmp_path / "sessions" / "2026" / "07" / "14" / "theirs.jsonl"
    ours.parent.mkdir(parents=True)
    ours.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "agentdeck"}}
        )
        + "\n"
    )
    theirs.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "codex-tui"}}
        )
        + "\n"
    )
    server = _server(tmp_path)

    async def request(method, params):
        if method == "thread/resume":
            server._server_request(
                "pending-1",
                "mcpServer/elicitation/request",
                {
                    "threadId": params["threadId"],
                    "turnId": "turn-1",
                    "serverName": "codex_apps",
                    "mode": "form",
                    "message": "Choose an account",
                },
            )
            return {"thread": {"id": params["threadId"], "status": {"type": "active"}}}
        return {
            "data": [
                {
                    "id": "ours",
                    "source": "vscode",
                    "threadSource": None,
                    "path": str(ours),
                    "status": {"type": "active"},
                },
                {
                    "id": "theirs",
                    "source": "vscode",
                    "threadSource": None,
                    "path": str(theirs),
                },
            ]
        }

    server._request = AsyncMock(side_effect=request)

    await server._recover_owned()

    assert server._request.await_args_list == [
        call(
            "thread/list", {"sourceKinds": ["appServer", "vscode"], "limit": 200}
        ),
        call(
            "thread/resume",
            {"threadId": "ours", "approvalsReviewer": "auto_review"},
        ),
    ]
    assert server.owns("ours")
    assert "ours" in server._loaded
    assert server.active_turn("ours") == "turn-1"
    assert server.interaction("ours") is not None
    assert server.interaction("ours").message == "Choose an account"
    assert not server.owns("theirs")


async def test_recover_owned_follows_next_cursor_past_the_first_page(tmp_path):
    """A deck-owned chat older than the newest page is owned again (#2650)."""
    rollouts = tmp_path / "sessions" / "2026" / "10" / "05"
    rollouts.mkdir(parents=True)
    for name, originator in (("new", "agentdeck"), ("old", "agentdeck"), ("cli", "codex-tui")):
        (rollouts / f"{name}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"originator": originator}})
            + "\n"
        )
    pages = {
        None: {"data": [{"id": "new", "path": str(rollouts / "new.jsonl")}], "nextCursor": "c1"},
        "c1": {"data": [{"id": "cli", "path": str(rollouts / "cli.jsonl")}], "nextCursor": "c2"},
        "c2": {"data": [{"id": "old", "path": str(rollouts / "old.jsonl")}], "nextCursor": None},
    }
    server = _server(tmp_path)
    server._request = AsyncMock(side_effect=lambda method, params: pages[params.get("cursor")])

    await server._recover_owned()

    assert [c.args[1].get("cursor") for c in server._request.await_args_list] == [None, "c1", "c2"]
    assert server.owned_threads() == {"new", "old"}


async def test_recover_owned_stops_on_a_repeated_cursor(tmp_path):
    server = _server(tmp_path)
    server._request = AsyncMock(return_value={"data": [], "nextCursor": "same"})

    await server._recover_owned()

    assert server._request.await_count == 2


def test_adopt_owned_recovers_a_persisted_thread_outside_the_list_page(tmp_path):
    thread_id = "019fa1b9-older-thread"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "14"
        / f"rollout-2026-07-14T10-00-00-{thread_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"originator": "agentdeck"}})
        + "\n"
    )
    server = _server(tmp_path)

    server.adopt_owned({thread_id})

    assert server.owns(thread_id)
    assert thread_id in server.owned_threads()


async def test_recover_owned_rejects_transcript_outside_account(tmp_path):
    outside = tmp_path.parent / "outside-agentdeck.jsonl"
    outside.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "agentdeck"}}
        )
        + "\n"
    )
    server = _server(tmp_path)
    server._request = AsyncMock(
        return_value={"data": [{"id": "outside", "path": str(outside)}]}
    )

    await server._recover_owned()

    assert not server.owns("outside")


async def test_recover_owned_rejects_subagent_rollout(tmp_path):
    helper = tmp_path / "sessions" / "2026" / "07" / "14" / "helper.jsonl"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "originator": "agentdeck",
                    "thread_source": "subagent",
                    "source": {"subagent": {"other": "guardian"}},
                },
            }
        )
        + "\n"
    )
    server = _server(tmp_path)
    server._request = AsyncMock(
        return_value={"data": [{"id": "helper", "path": str(helper)}]}
    )

    await server._recover_owned()

    assert not server.owns("helper")


async def test_command_approval_and_permission_response(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    writes = []

    async def write(message):
        writes.append(message)

    server._write = write
    server._server_request(
        "approval-1",
        "item/commandExecution/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "startedAtMs": 1,
            "command": "uv run pytest -q",
            "cwd": str(tmp_path),
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.command == "uv run pytest -q"
    result = await server.answer(
        "thread-1", interaction.id, answers={}, decision="acceptForSession"
    )
    assert result.accepted
    assert writes[-1] == {
        "id": "approval-1",
        "result": {"decision": "acceptForSession"},
    }

    requested = {"network": {"enabled": True}}
    server._server_request(
        42,
        "item/permissions/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-2",
            "startedAtMs": 2,
            "cwd": str(tmp_path),
            "permissions": requested,
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    result = await server.answer(
        "thread-1", interaction.id, answers={}, decision="accept"
    )
    assert result.accepted
    assert writes[-1] == {
        "id": 42,
        "result": {"permissions": requested, "scope": "turn"},
    }


async def test_permission_prompt_override_updates_thread_and_replays_on_resume(tmp_path):
    allowed = {"thread-1"}
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        permission_prompts_allowed=lambda thread_id: thread_id in allowed,
    )
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._request = AsyncMock(return_value={"approvalPolicy": "never"})

    result = await server.set_permission_prompts("thread-1", True)

    assert result.accepted
    assert server._request.await_args_list == [
        call(
            "thread/resume",
            {
                "threadId": "thread-1",
                "approvalsReviewer": "user",
                "approvalPolicy": "on-request",
            },
        ),
        call(
            "thread/settings/update",
            {
                "threadId": "thread-1",
                "approvalsReviewer": "user",
                "approvalPolicy": "on-request",
            },
        ),
    ]
    assert server.approval_policy("thread-1") == "on-request"

    server._request.reset_mock()
    result = await server.set_permission_prompts(
        "thread-1", False, previous_policy="never"
    )

    assert result.accepted
    server._request.assert_awaited_once_with(
        "thread/settings/update",
        {
            "threadId": "thread-1",
            "approvalsReviewer": "auto_review",
            "approvalPolicy": "never",
        },
    )
    assert server.approval_policy("thread-1") == "never"


async def test_permission_prompt_override_loads_idle_policy_before_enabling(tmp_path):
    granular_policy = {"reject": {"rules": [{"type": "network"}]}}
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._request = AsyncMock(return_value={"approvalPolicy": granular_policy})

    policy = await server.current_approval_policy("thread-1")

    assert policy == granular_policy
    server._request.assert_awaited_once_with(
        "thread/resume",
        {"threadId": "thread-1", "approvalsReviewer": "auto_review"},
    )
    assert "thread-1" in server._loaded


async def test_app_server_disconnect_forgets_connection_loaded_threads(tmp_path):
    class EmptyStdout:
        async def readline(self):
            return b""

    class Process:
        stdout = EmptyStdout()

    server = _server(tmp_path)
    process = Process()
    server._process = process
    server._loaded.add("idle-thread")

    await server._read_loop()

    assert server._process is None
    assert not server._loaded




async def test_kanban_thread_start_receives_kanban_role_block(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        return {"thread": {"id": "thread-kanban", "status": {"type": "idle"}}}

    server._request = request

    result = await server.create_thread(tmp_path, role="kanban")

    assert result.accepted and result.session_id == "thread-kanban"
    assert calls[0][1]["developerInstructions"] == agentdeck_session_instructions(provider="codex",
        role="kanban"
    )
    # No subagent cap of our own: Codex's default applies (#2482), so a kanban
    # thread with nothing else to configure sends no config at all.
    assert "config" not in calls[0][1]


async def test_enforced_kanban_thread_receives_hooks_cap_and_private_identity(tmp_path):
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server.start = AsyncMock()
    server._artifact_hooks_trusted = True
    server._request = AsyncMock(
        return_value={"thread": {"id": "thread-kanban", "status": {"type": "idle"}}}
    )

    result = await server.create_thread(
        tmp_path,
        role="kanban",
        worker_key="repo#1484",
        artifact_root=tmp_path / "artifacts",
        artifact_policy={"mode": "enforce", "visible_tool_result_chars": 1600},
    )

    assert result.accepted
    config = server._request.await_args.args[1]["config"]
    assert "hooks" not in config
    assert "tool_output_token_limit" not in config
    assert config["shell_environment_policy"]["set"]["AGENTDECK_WORKER_KEY"] == "repo#1484"


async def test_a_kanban_thread_gets_the_approval_cache_without_command_capture(tmp_path):
    """#2154: the approval cache (#1738) has its own switch — being a kanban worker, like on
    Claude — instead of riding on the retired phase setting that turned capture on. The thread
    carries no capture policy, so the capture hook stays a no-op."""
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server.start = AsyncMock()
    server._ensure_command_artifact_hooks = AsyncMock(return_value=InjectResult(True))
    server._request = AsyncMock(
        return_value={"thread": {"id": "thread-kanban", "status": {"type": "idle"}}}
    )

    result = await server.create_thread(tmp_path, role="kanban", worker_key="repo#2154")

    assert result.accepted
    server._ensure_command_artifact_hooks.assert_awaited_once()
    environment = server._request.await_args.args[1]["config"]["shell_environment_policy"]
    assert "AGENTDECK_COMMAND_ARTIFACT_MODE" not in str(environment)


async def test_an_unavailable_approval_cache_never_refuses_a_thread_without_capture(tmp_path):
    """Capture still requires its hooks; the cache alone is a convenience, so a busy account
    that cannot install them yet starts the thread without them."""
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server.start = AsyncMock()
    busy = InjectResult(False, "generated Codex capture hooks require all threads to be idle")
    server._ensure_command_artifact_hooks = AsyncMock(return_value=busy)
    server._request = AsyncMock(
        return_value={"thread": {"id": "thread-kanban", "status": {"type": "idle"}}}
    )

    plain = await server.create_thread(tmp_path, role="kanban", worker_key="repo#2154")
    capturing = await server.create_thread(
        tmp_path,
        role="kanban",
        worker_key="repo#2154",
        artifact_root=tmp_path / "artifacts",
        artifact_policy={"mode": "enforce"},
    )

    assert plain.accepted
    assert not capturing.accepted and capturing.reason == busy.reason


async def test_a_primary_thread_installs_no_worker_hooks(tmp_path):
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server.start = AsyncMock()
    server._ensure_command_artifact_hooks = AsyncMock(return_value=InjectResult(True))
    server._request = AsyncMock(
        return_value={"thread": {"id": "thread-1", "status": {"type": "idle"}}}
    )

    assert (await server.create_thread(tmp_path)).accepted
    server._ensure_command_artifact_hooks.assert_not_awaited()


async def test_existing_codex_thread_can_receive_artifact_hooks_before_next_turn(tmp_path):
    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        persistent_backend=False,
        artifact_root=tmp_path / "artifacts",
    )
    server.start = AsyncMock()
    server._owned.add("thread-1")
    capture = str(Path(__file__).parents[1] / "scripts" / "command_output_hook.py")
    approvals = str(Path(__file__).parents[1] / "scripts" / "approval_cache_hook.py")
    hook_entries = [
        {
            "key": f"hook-{index}",
            "currentHash": f"sha256:{index}",
            "command": command,
            "source": "sessionFlags",
            "trustStatus": "untrusted",
            "eventName": event,
            "handlerType": "command",
            "matcher": "Bash",
            "enabled": True,
        }
        for index, (event, command) in enumerate(
            (
                ("preToolUse", capture),
                ("postToolUse", capture),
                ("postToolUse", approvals),
                ("permissionRequest", approvals),
            )
        )
    ]
    trusted_entries = [dict(hook, trustStatus="trusted") for hook in hook_entries]
    server._request = AsyncMock(
        side_effect=[
            {"data": [{"hooks": hook_entries}]},
            {"data": [{"hooks": trusted_entries}]},
        ]
    )

    result = await server.set_command_artifacts(
        "thread-1",
        worker_key="repo#1484",
        session_token="opaque-session-token",
        artifact_root=tmp_path / "artifacts",
        artifact_policy={"mode": "observe"},
    )

    assert result.accepted
    calls = server._request.await_args_list
    assert [entry.args[0] for entry in calls] == [
        "hooks/list",
        "hooks/list",
    ]
    assert server._artifact_hook_states == {
        "hook-0": "sha256:0",
        "hook-1": "sha256:1",
        "hook-2": "sha256:2",
        "hook-3": "sha256:3",
    }


async def test_disabling_artifacts_does_not_activate_codex_hooks(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")

    result = await server.set_command_artifacts(
        "thread-1",
        worker_key="repo#1484",
        session_token="opaque-session-token",
        artifact_root=tmp_path / "artifacts",
        artifact_policy={"mode": "off"},
    )

    assert result.accepted
    server.start.assert_not_awaited()
    assert server._artifact_hooks_requested is False


async def test_new_thread_receives_current_mcp_servers(tmp_path):
    """A long-lived app-server must not freeze the MCP config it started with."""
    (tmp_path / "config.toml").write_text(
        """
[mcp_servers.sentry]
command = "podman"
args = ["exec", "-i", "sentry_mcp", "sentry-mcp"]
""".lstrip()
    )
    server = _server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        return {"thread": {"id": "thread-mcp", "status": {"type": "idle"}}}

    server._request = request

    result = await server.create_thread(tmp_path, role="kanban")

    assert result.accepted and result.session_id == "thread-mcp"
    assert calls[0][1]["config"] == {
        "mcp_servers": {
            "sentry": {
                "command": "podman",
                "args": ["exec", "-i", "sentry_mcp", "sentry-mcp"],
            }
        },
    }


async def test_new_thread_rejects_unreadable_current_codex_config(tmp_path):
    (tmp_path / "config.toml").write_text("[mcp_servers.sentry\n")
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._request = AsyncMock()

    with pytest.raises(AppServerError, match="could not read current Codex config"):
        await server.create_thread(tmp_path)

    server._request.assert_not_awaited()





def _thread_in_deleted_worktree(tmp_path: Path, *, loaded: bool):
    """An owned, idle thread whose issue worktree a merge removed (#2524)."""
    repository = tmp_path / "project"
    (repository / ".git").mkdir(parents=True)
    worktree = repository / ".worktrees" / "issue-1"
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._threads["thread-1"] = {
        "id": "thread-1",
        "cwd": str(worktree),
        "status": {"type": "idle"},
    }
    if loaded:
        server._loaded.add("thread-1")
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        return {}

    server._request = request
    return server, calls, repository, worktree


async def test_a_thread_whose_worktree_is_gone_resumes_in_its_repository(tmp_path):
    server, calls, repository, worktree = _thread_in_deleted_worktree(
        tmp_path, loaded=False
    )

    result = await server.start_turn("thread-1", "one more thing", wait=False)

    assert result.accepted
    assert calls[0][0] == "thread/resume"
    assert calls[0][1]["cwd"] == str(repository)
    method, params = calls[1]
    assert method == "turn/start"
    assert params["cwd"] == str(repository)
    text = params["input"][0]["text"]
    # Told once, in the block the transcript reader strips: the turn still reads as
    # the person's own message.
    assert text == wrap_session_guide(
        relocation_notice(str(worktree), str(repository)), "one more thing"
    )
    assert strip_session_guide(text) == "one more thing"
    # A guide refresh in front of it is stripped as well.
    assert strip_session_guide(wrap_session_guide("guide", text)) == "one more thing"

    server._active_turn.pop("thread-1")
    calls.clear()
    await server.start_turn("thread-1", "and another", wait=False)
    assert calls[0][1]["input"][0]["text"] == "and another"
    assert "cwd" not in calls[0][1]


async def test_a_loaded_thread_whose_worktree_is_gone_moves_on_its_next_turn(tmp_path):
    server, calls, repository, _worktree = _thread_in_deleted_worktree(
        tmp_path, loaded=True
    )

    await server.start_turn("thread-1", "one more thing", wait=False)

    assert [method for method, _params in calls] == ["turn/start"]
    assert calls[0][1]["cwd"] == str(repository)


async def test_a_thread_whose_directory_is_there_is_not_moved(tmp_path):
    server, calls, _repository, worktree = _thread_in_deleted_worktree(
        tmp_path, loaded=True
    )
    worktree.mkdir(parents=True)

    await server.start_turn("thread-1", "one more thing", wait=False)

    assert "cwd" not in calls[0][1]
    assert calls[0][1]["input"][0]["text"] == "one more thing"


async def test_owned_thread_start_steer_and_interrupt(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        return {}

    server._request = request
    image = tmp_path / "screen.png"
    result = await server.start_thread(
        tmp_path,
        "Build it",
        images=[image],
        sandbox="workspace-write",
        model="gpt-test",
        effort="high",
        approval_policy="on-request",
        delegated=True,
    )
    assert result.accepted
    assert result.session_id == "thread-1"
    assert calls[0] == (
        "thread/start",
        {
            "cwd": str(tmp_path),
            "developerInstructions": agentdeck_session_instructions(
                provider="codex", role="delegated"
            ),
            "ephemeral": False,
            "threadSource": "agentdeck",
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "approvalPolicy": "on-request",
        },
    )
    assert calls[0][1]["developerInstructions"] == agentdeck_session_instructions(provider="codex",
        role="delegated"
    )
    assert server.active_turn("thread-1") == "turn-1"
    assert calls[1] == (
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [
                {"type": "text", "text": "Build it"},
                {"type": "localImage", "path": str(image)},
            ],
            "effort": "high",
        },
    )
    result = await server.steer("thread-1", "Use SQLite instead", images=[image])
    assert result.accepted
    assert calls[-1] == (
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-1",
            "input": [
                {"type": "text", "text": "Use SQLite instead"},
                {"type": "localImage", "path": str(image)},
            ],
        },
    )

    result = await server.interrupt("thread-1")
    assert result.accepted
    assert calls[-1] == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )


async def test_thread_start_treats_an_invalid_response_as_ambiguous(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()

    async def request(method, params, **kwargs):
        if method == "thread/start":
            return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
        return {"turn": {}}

    server._request = request

    with pytest.raises(AppServerError, match="response was ambiguous"):
        await server.start_thread(tmp_path, "Build it")


def test_turn_completion_exposes_a_result_for_keyed_worker_observation(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._active_turn["thread-1"] = "turn-1"

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "completed"},
        },
    )

    finished = server.last_result("thread-1")
    assert finished is not None
    assert finished[0] > 0
    assert finished[1] == "completed"
    assert finished[2] == "", "nothing refused a turn that completed"


def _completed(server, turn: dict) -> tuple:
    server._owned.add("thread-1")
    server._active_turn["thread-1"] = "turn-1"
    server._notification("turn/completed", {"threadId": "thread-1", "turn": turn})
    result = server.last_result("thread-1")
    assert result is not None
    return result


def test_a_turn_the_account_refused_names_the_refusal(tmp_path):
    """#1519, against the payload codex 0.150.0 actually sends: a `failed` turn carries a
    `TurnError` whose `codexErrorInfo` names the cause, and exactly one cause means the
    account could not run a turn at all."""
    refused = _completed(_server(tmp_path), {
        "id": "turn-1",
        "status": "failed",
        "error": {
            "message": (
                "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
                "to purchase more credits or try again at Sep 11th, 2026 7:48 AM."
            ),
            "codexErrorInfo": "usageLimitExceeded",
            "additionalDetails": None,
        },
    })

    assert refused[1] == "failed"
    assert refused[2].startswith("usage_limit_exceeded: You've hit your usage limit.")


async def test_waiting_on_a_refused_turn_names_the_usage_limit(tmp_path):
    """#3210: the upgrade check read only "status failed" and blamed a Codex release for an
    account that was out of usage."""
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._active_turn["thread-1"] = "turn-1"
    server._completed_turns["turn-1"] = {
        "id": "turn-1",
        "status": "failed",
        "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"},
    }

    result = await server.wait_for_thread("thread-1")

    assert not result.accepted
    assert result.reason == "Codex turn ended with status failed: usage_limit_exceeded: limit"


def test_the_rollouts_snake_case_spelling_of_the_same_cause_is_read_too(tmp_path):
    """Two wires carry this turn: the app-server's camelCase JSON-RPC and the snake_case
    rollout the same turn writes to disk. Neither spelling may be the one that is missed."""
    refused = _completed(_server(tmp_path), {
        "id": "turn-1",
        "status": "failed",
        "error": {"message": "limit", "codex_error_info": "usage_limit_exceeded"},
    })

    assert refused[2] == "usage_limit_exceeded: limit"


@pytest.mark.parametrize("turn", [
    # Every other cause is a turn that ran and went wrong, not an account that turned it
    # away — including the two that sound closest.
    {"id": "turn-1", "status": "failed",
     "error": {"message": "too long", "codexErrorInfo": "contextWindowExceeded"}},
    {"id": "turn-1", "status": "failed",
     "error": {"message": "spent", "codexErrorInfo": "sessionBudgetExceeded"}},
    {"id": "turn-1", "status": "failed", "error": {"message": "boom"}},
    {"id": "turn-1", "status": "failed"},
    {"id": "turn-1", "status": "interrupted"},
    # A shape a later Codex invents reads as unknown, never as a refusal.
    {"id": "turn-1", "status": "failed", "error": {"message": "?", "codexErrorInfo": 7}},
    {"id": "turn-1", "status": "failed", "error": "usageLimitExceeded"},
])
def test_only_a_usage_limit_is_reported_as_a_refusal(tmp_path, turn):
    assert _completed(_server(tmp_path), turn)[2] == ""


async def test_wait_for_owned_thread_completion(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._active_turn["thread-1"] = "turn-1"
    server._completed_turns["turn-1"] = {"id": "turn-1", "status": "completed"}

    result = await server.wait_for_thread("thread-1")

    assert result.accepted


async def test_idle_only_queue_refuses_inside_the_thread_operation_lock(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    server._active_turn["thread-1"] = "turn-old"
    server._request = AsyncMock()

    result = await server.queue_turn("thread-1", "continue", require_idle=True)

    assert not result.accepted
    assert result.reason == "active_turn"
    server._request.assert_not_awaited()


async def test_queued_turn_is_acknowledged_when_followup_starts(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    server._active_turn["thread-1"] = "turn-old"
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        assert method == "turn/start"
        assert params["threadId"] == "thread-1"
        return {"turn": {"id": "turn-new", "status": "inProgress"}}

    server._request = request
    queued = asyncio.create_task(
        server.queue_turn("thread-1", "Next message", effort="xhigh")
    )
    await asyncio.sleep(0)
    assert not queued.done()

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-old", "status": "completed"},
        },
    )
    result = await asyncio.wait_for(queued, timeout=1)

    assert result.accepted
    assert calls[-1] == (
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "Next message"}],
            "effort": "xhigh",
        },
    )
    assert server.active_turn("thread-1") == "turn-new"
    # No turn/completed notification for turn-new was needed: acceptance of
    # turn/start is the point where the message has finished sending.


async def test_compact_waits_for_current_turn_and_compaction_completion(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    server._active_turn["thread-1"] = "turn-old"
    requested = asyncio.Event()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        requested.set()
        return {}

    server._request = request
    compacting = asyncio.create_task(server.compact("thread-1"))
    await asyncio.sleep(0)
    assert calls == []

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-old", "status": "completed"},
        },
    )
    await requested.wait()
    assert calls == [("thread/compact/start", {"threadId": "thread-1"})]
    assert not compacting.done()

    server._notification(
        "turn/started",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "inProgress"},
        },
    )
    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "completed"},
        },
    )

    result = await asyncio.wait_for(compacting, timeout=1)
    assert result.accepted
    assert server.active_turn("thread-1") is None


async def test_maintained_queue_compacts_before_starting_batch(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    compact_requested = asyncio.Event()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "thread/compact/start":
            compact_requested.set()
            return {}
        if method == "turn/start":
            return {"turn": {"id": "turn-batch", "status": "inProgress"}}
        raise AssertionError(method)

    server._request = request
    queued = asyncio.create_task(
        server.queue_turn(
            "thread-1",
            "Attention batch",
            maintain_context=True,
            after_maintenance=lambda status, reason: calls.append(
                (f"maintenance/{status}", {"reason": reason})
            ),
            before_start=lambda: calls.append(("receipt/prepare", {})),
        )
    )
    await compact_requested.wait()
    assert [method for method, _params in calls] == ["thread/compact/start"]

    server._notification(
        "turn/started",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "inProgress"},
        },
    )
    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "completed"},
        },
    )

    result = await asyncio.wait_for(queued, timeout=1)

    assert result.accepted
    assert [method for method, _params in calls] == [
        "thread/compact/start",
        "maintenance/succeeded",
        "receipt/prepare",
        "turn/start",
    ]
    assert calls[-1][1]["input"] == [
        {"type": "text", "text": "Attention batch"}
    ]


async def test_queued_followup_does_not_retarget_concurrent_steer(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "turn/start":
            return {"turn": {"id": "turn-b", "status": "inProgress"}}
        raise AssertionError(method)

    server._request = request
    server._notification(
        "turn/started",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-a", "status": "inProgress"},
        },
    )
    queued = asyncio.create_task(server.queue_turn("thread-1", "queued batch"))
    await asyncio.sleep(0)
    steered = asyncio.create_task(server.steer("thread-1", "amend turn A"))
    await asyncio.sleep(0)

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-a", "status": "completed"},
        },
    )

    assert (await queued).accepted
    steer_result = await steered
    assert not steer_result.accepted
    assert calls == [
        (
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "queued batch"}],
            },
        )
    ]
def test_unsafe_mcp_url_is_not_exposed(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._server_request(
        43,
        "mcpServer/elicitation/request",
        {
            "threadId": "thread-1",
            "serverName": "example",
            "mode": "url",
            "message": "Open this",
            "url": "javascript:alert(1)",
            "elicitationId": "e-1",
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.url is None


class _RunningProcess:
    returncode = None


def _idle_server(tmp_path: Path, *thread_ids: str, since: float = 0.0) -> CodexAppServer:
    server = _server(tmp_path)
    server._process = _RunningProcess()
    server._request = AsyncMock(return_value={"status": "unsubscribed"})
    for thread_id in thread_ids:
        server._owned.add(thread_id)
        server._loaded.add(thread_id)
        server._threads[thread_id] = {"id": thread_id, "status": {"type": "idle"}}
        server._idle_since[thread_id] = since
    return server


async def test_idle_thread_is_unsubscribed_and_resumed_on_its_next_turn(tmp_path):
    server = _idle_server(tmp_path, "thread-idle")

    assert await server.unload_idle(idle_for=600, now=601) == ["thread-idle"]
    server._request.assert_awaited_once_with(
        "thread/unsubscribe", {"threadId": "thread-idle"}
    )
    assert "thread-idle" not in server._loaded
    assert "thread-idle" not in server._idle_since
    # Unloaded once: a second sweep has nothing left to ask.
    assert await server.unload_idle(idle_for=600, now=1_000) == []

    server.start = AsyncMock()
    server._request = AsyncMock(
        side_effect=[{"thread": {"id": "thread-idle"}}, {"turn": {"id": "turn-1"}}]
    )
    assert (await server.start_turn("thread-idle", "continue", wait=False)).accepted
    assert [c.args[0] for c in server._request.await_args_list] == [
        "thread/resume",
        "turn/start",
    ]


async def test_busy_waiting_recent_and_foreign_threads_stay_loaded(tmp_path):
    server = _idle_server(
        tmp_path, "thread-active", "thread-waiting", "thread-locked", "thread-recent"
    )
    server._active_turn["thread-active"] = "turn-1"
    server._interactions["token"] = _PendingRequest(
        1,
        "item/tool/requestUserInput",
        {},
        PendingInteraction(
            id="token", kind="question", thread_id="thread-waiting", turn_id=None, title="?"
        ),
    )
    server._idle_since["thread-recent"] = 500.0
    server._loaded.add("thread-foreign")
    server._idle_since["thread-foreign"] = 0.0
    lock = server._thread_operation_lock("thread-locked")
    await lock.acquire()

    assert await server.unload_idle(idle_for=600, now=601) == []
    server._request.assert_not_awaited()
    assert server._loaded == {
        "thread-active",
        "thread-waiting",
        "thread-locked",
        "thread-recent",
        "thread-foreign",
    }
    lock.release()


async def test_unload_error_keeps_that_thread_and_unloads_the_rest(tmp_path):
    server = _idle_server(tmp_path, "thread-bad", "thread-good")

    async def request(method, params):
        if params["threadId"] == "thread-bad":
            raise AppServerError("boom")
        return {"status": "unsubscribed"}

    server._request = AsyncMock(side_effect=request)

    assert await server.unload_idle(idle_for=600, now=601) == ["thread-good"]
    assert server._loaded == {"thread-bad"}


async def test_idle_clock_starts_at_turn_end_or_resume_never_at_thread_start(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    # A thread with no turn yet has no rollout, so it could not be resumed.
    assert "thread-1" not in server._idle_since

    server._notification("turn/started", {"threadId": "thread-1", "turn": {"id": "t1"}})
    assert "thread-1" not in server._idle_since
    server._notification(
        "turn/completed", {"threadId": "thread-1", "turn": {"id": "t1", "status": "completed"}}
    )
    assert "thread-1" in server._idle_since
    server._notification("thread/closed", {"threadId": "thread-1"})
    assert "thread-1" not in server._idle_since and "thread-1" not in server._loaded

    server._request = AsyncMock(return_value={})
    await server._ensure_loaded("thread-1")
    assert "thread-1" in server._idle_since


async def test_persistent_backend_below_the_file_limit_is_stale(tmp_path, monkeypatch):
    socket = tmp_path / "control.sock"
    socket.touch()
    server = CodexAppServer(Account("codex:test", "codex", "test", tmp_path))
    monkeypatch.setattr(server, "_backend_identity", lambda: ("backend.service", socket))
    shown = []

    class Process:
        returncode = 0
        limit = b"1024\n"
        started = f"{{ argv[]=codex app-server --config {LOGIN_SHELL_CONFIG_OVERRIDE} }}".encode()

        def __init__(self, args=()):
            self.args = args

        async def wait(self):
            return self.returncode

        async def communicate(self):
            if "--property=ExecStart" in self.args:
                return Process.started, b""
            return Process.limit, b""

    async def create_subprocess_exec(*args, **kwargs):
        shown.append(args)
        return Process(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    assert await server._ensure_persistent_backend({}) == socket
    assert server._backend_env_stale is True
    assert (
        "systemctl", "--user", "show", "--property=LimitNOFILESoft", "--value",
        "backend.service",
    ) in shown

    for limit in (b"65536\n", b"infinity\n"):
        Process.limit = limit
        assert await server._ensure_persistent_backend({}) == socket
        assert server._backend_env_stale is False

    # A backend started before #3350 still runs its commands in a login shell, whose
    # profile hides the gh shim, so it is refreshed once idle too; an unreadable
    # answer keeps it.
    Process.started = b"{ argv[]=codex app-server --listen unix:///x }"
    assert await server._ensure_persistent_backend({}) == socket
    assert server._backend_env_stale is True
    Process.started = b""
    assert await server._ensure_persistent_backend({}) == socket
    assert server._backend_env_stale is False
