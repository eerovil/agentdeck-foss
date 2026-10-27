"""Shared fixtures for the keyed-worker adapters.

`ClaudeWorkerHost` and `CodexWorkerHost` implement the same `KeyedWorkerHost`
seam over completely different machinery — a `claude` child process spoken to
over stdin, and an app-server client. These fakes stand in for each, and
`HARNESSES` wraps them behind one small vocabulary (write count, a hook that
fires at the moment of the child write, reload-from-disk) so the shared
contract suite can exercise every adapter without knowing which it is driving.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from agentdeck.models import Account, InjectResult
from agentdeck.providers.claude_code.worker import ClaudeWorkerHost
from agentdeck.providers.codex.worker import CodexWorkerHost
from agentdeck.providers.delivery import KeyedWorkerHost

# --- Claude: a fake `claude` child process ---------------------------------


class FakeStdin:
    def __init__(self):
        self.lines: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    def __init__(self):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def emit(self, event: dict) -> None:
        self.queue.put_nowait(json.dumps(event).encode() + b"\n")

    async def readline(self) -> bytes:
        return await self.queue.get()


class FakeProcess:
    def __init__(self):
        self.returncode: int | None = None
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.queue.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def claude_host(tmp_path: Path, **kwargs) -> tuple[ClaudeWorkerHost, list]:
    spawned: list[dict] = []

    async def factory(*args, **fkwargs):
        process = FakeProcess()
        spawned.append({"args": args, "kwargs": fkwargs, "process": process})
        session_id = f"sid-auto-{len(spawned)}"
        asyncio.get_running_loop().call_soon(
            process.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": session_id},
        )
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
        **kwargs,
    )
    return host, spawned


# --- Codex: a fake app-server client ---------------------------------------


def codex_account(tmp_path: Path) -> Account:
    return Account("codex:codex", "codex", "codex", tmp_path / "account")


def codex_client() -> MagicMock:
    client = MagicMock()
    client.owns.return_value = False
    client.active_turn.return_value = None
    client.interaction.return_value = None
    client.last_result.return_value = None
    client.create_thread = AsyncMock(return_value=InjectResult(True, session_id="thread-1"))
    client.start_turn = AsyncMock(return_value=InjectResult(True))
    client.queue_turn = AsyncMock(return_value=InjectResult(True))
    client.interrupt = AsyncMock(return_value=InjectResult(True))
    client.wait_for_thread = AsyncMock(return_value=InjectResult(False, "turn interrupted"))
    client.compact = AsyncMock(return_value=InjectResult(True))
    client.set_command_artifacts = AsyncMock(return_value=InjectResult(True))
    return client


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# --- the provider-neutral harness the contract suite drives ----------------


@dataclass
class KeyedWorkerHarness:
    """One adapter behind the vocabulary the shared contract suite needs."""

    provider: str
    host: KeyedWorkerHost
    cwd: str
    #: Messages this host has actually written to its child, in order.
    writes: list[str]
    #: Callables invoked immediately before each write is recorded.
    write_hooks: list[Callable[[], None]] = field(default_factory=list)
    #: Rebuild the host from its persisted state, as a runtime restart would.
    _reload: Callable[[], KeyedWorkerHost] = None  # type: ignore[assignment]

    def record(self, key: str) -> Any:
        """The durable worker record for ``key`` (both adapters name it the same)."""
        return self.host._records[key]

    def receipts(self, key: str) -> dict[str, dict]:
        return self.record(key).deliveries

    def persisted_receipts(self, key: str) -> dict[str, dict]:
        """The receipts as currently written to disk, not as held in memory."""
        state = json.loads(self.host._state_path.read_text())
        for worker in state["workers"]:
            if worker["key"] == key:
                return worker["deliveries"]
        return {}

    def reload(self) -> KeyedWorkerHost:
        self.host = self._reload()
        return self.host

    async def stop(self) -> None:
        """Shut the host down as a runtime stop would. Codex owns no children."""
        stop = getattr(self.host, "stop", None)
        if stop is not None:
            await stop()


def claude_harness(tmp_path: Path) -> KeyedWorkerHarness:
    writes: list[str] = []
    hooks: list[Callable[[], None]] = []

    class HookedStdin(FakeStdin):
        def write(self, data: bytes) -> None:
            for hook in hooks:
                hook()
            payload = json.loads(data)
            content = payload.get("message", {}).get("content") or [{}]
            writes.append(content[0].get("text", ""))
            self.lines.append(payload)

    def make_host() -> ClaudeWorkerHost:
        spawned: list[FakeProcess] = []

        async def factory(*args, **kwargs):
            process = FakeProcess()
            process.stdin = HookedStdin()
            spawned.append(process)
            session_id = f"sid-auto-{len(spawned)}"
            asyncio.get_running_loop().call_soon(
                process.stdout.emit,
                {"type": "system", "subtype": "init", "session_id": session_id},
            )
            return process

        return ClaudeWorkerHost(
            Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
            state_dir=tmp_path / "state",
            process_factory=factory,
        )

    return KeyedWorkerHarness(
        provider="claude_code",
        host=make_host(),
        cwd=str(tmp_path),
        writes=writes,
        write_hooks=hooks,
        _reload=make_host,
    )


def codex_harness(tmp_path: Path) -> KeyedWorkerHarness:
    writes: list[str] = []
    hooks: list[Callable[[], None]] = []
    client = codex_client()

    def written(_session_id: str, message: str, **_kwargs) -> InjectResult:
        for hook in hooks:
            hook()
        writes.append(message)
        return InjectResult(True)

    client.start_turn = AsyncMock(side_effect=written)
    client.queue_turn = AsyncMock(side_effect=written)
    client.steer = AsyncMock(side_effect=written)

    def make_host() -> CodexWorkerHost:
        return CodexWorkerHost(
            codex_account(tmp_path), client, state_dir=tmp_path / "state"
        )

    return KeyedWorkerHarness(
        provider="codex",
        host=make_host(),
        cwd=str(tmp_path),
        writes=writes,
        write_hooks=hooks,
        _reload=make_host,
    )


#: (id, builder) for every adapter the keyed-worker contract must hold for.
HARNESSES = [
    ("claude_code", claude_harness),
    ("codex", codex_harness),
]
