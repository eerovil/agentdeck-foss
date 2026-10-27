"""The runtime's side of a held Claude worker (#3248).

``holder.py`` keeps a Claude CLI's pipes outside the runtime. This module is what the
runtime talks to instead of a child process: :class:`HeldProcess` has the parts of
``asyncio.subprocess.Process`` the worker host uses (``pid``, ``returncode``, ``stdin``,
``stdout``, ``wait``), so the host's read loop, writes and teardown work unchanged.

What a restart needs to find a held worker again lives in :class:`HolderStore`, a file
beside the worker records rather than inside them: an older runtime that does not know
these fields must still load its records (a record it cannot parse is dropped), so a
rollback costs at worst one cut-off chat, never a lost one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ... import pid_watch

log = logging.getLogger(__name__)

HOLDER_PATH = Path(__file__).with_name("holder.py")
# The holder protocol this runtime speaks. A holder answering with another one is not
# reconnected to: it is stopped, and its chat is revived as before the holder existed.
PROTOCOL = 1
HELLO_TIMEOUT_S = 15.0
# How often the read position is saved while events stream in; every event that changes
# what a restart must know (a result, a question, the session start) saves at once.
OFFSET_SAVE_INTERVAL_S = 5.0


@dataclass
class HolderInfo:
    """Where one worker's holder is, and what the next runtime must know about it."""

    socket: str
    log: str
    exit: str
    pid: int
    pid_start: int | None = None
    # Bytes of the log this runtime has read and acted on.
    offset: int = 0
    scope_unit: str = ""
    started_at: float = field(default_factory=time.time)
    # The open question or permission prompt the CLI is waiting on, as ``_LiveWorker`` has it.
    pending_interaction: dict[str, Any] | None = None
    background_agents: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: object) -> HolderInfo | None:
        if not isinstance(raw, dict):
            return None
        try:
            pending = raw.get("pending_interaction")
            agents = raw.get("background_agents") or {}
            return cls(
                socket=str(raw["socket"]),
                log=str(raw["log"]),
                exit=str(raw["exit"]),
                pid=int(raw["pid"]),
                pid_start=None if raw.get("pid_start") is None else int(raw["pid_start"]),
                offset=int(raw.get("offset", 0) or 0),
                scope_unit=str(raw.get("scope_unit", "")),
                started_at=float(raw.get("started_at", 0.0) or 0.0),
                pending_interaction=dict(pending) if isinstance(pending, dict) else None,
                background_agents={str(k): float(v) for k, v in dict(agents).items()},
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def ledger(self) -> str:
        """The delivery ids the holder accepted for the CLI, one per line (#3248)."""
        return f"{self.exit}.writes"

    def written(self) -> set[str] | None:
        """The ids in the ledger; empty when the holder never accepted one, and None when
        the ledger exists but cannot be read, which proves nothing either way."""
        try:
            return set(Path(self.ledger).read_text().splitlines())
        except FileNotFoundError:
            return set()
        except (OSError, UnicodeDecodeError):
            log.warning("could not read the write ledger %s", self.ledger, exc_info=True)
            return None

    def alive(self) -> bool:
        """Whether this holder is still running: the pid exists with the same start time."""
        return self.pid_start is not None and process_start(self.pid) == self.pid_start

    def remove_files(self) -> None:
        for path in (self.log, self.exit, f"{self.exit}.tmp", self.ledger, self.socket):
            with contextlib.suppress(OSError):
                os.unlink(path)


class HolderStore:
    """``<label>.holders.json``: worker key -> :class:`HolderInfo`."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, HolderInfo]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        holders = raw.get("holders") if isinstance(raw, dict) else None
        if not isinstance(holders, dict):
            return {}
        loaded = {key: HolderInfo.from_dict(value) for key, value in holders.items()}
        return {key: info for key, info in loaded.items() if info is not None}

    def save(self, holders: dict[str, HolderInfo]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"holders": {key: asdict(info) for key, info in holders.items()}})
            )
            tmp.replace(self.path)
        except OSError:
            # Losing this costs the next restart a cut-off chat, as before #3248.
            log.exception("could not save the worker holders to %s", self.path)


def process_start(pid: int) -> int | None:
    """The kernel's start time for ``pid`` (clock ticks since boot), or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # The command name is in parentheses and may hold spaces; the fields after it do not.
        fields = stat.rsplit(")", 1)[1].split()
        # A zombie has exited; only its parent has not collected it yet.
        return None if fields[0] == "Z" else int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def read_exit(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def kill_holder(info: HolderInfo) -> None:
    """Stop a holder and its CLI, which share one process group led by the holder."""
    if not info.alive():
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(info.pid, signal.SIGKILL)


class _HeldStdin:
    """The CLI's stdin, one ``stdin`` op per line through the holder."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._closed = False

    def write(self, payload: bytes, *, delivery_id: str | None = None) -> None:
        if self._closed or self._writer.is_closing():
            raise BrokenPipeError("the worker holder connection is closed")
        for line in payload.splitlines():
            op: dict[str, str] = {"op": "stdin", "data": line.decode()}
            if delivery_id:
                op["id"] = delivery_id
            self._writer.write(json.dumps(op, separators=(",", ":")).encode() + b"\n")

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        """End the CLI's input: the runtime is stopping this chat on purpose."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._writer.write(b'{"op":"close_stdin"}\n')


class HeldProcess:
    """A held Claude CLI, as the worker host sees a process."""

    def __init__(
        self,
        info: HolderInfo,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.info = info
        self.pid = info.pid
        self.returncode: int | None = None
        self.stdout = reader
        self.stdin = _HeldStdin(writer)
        self._writer = writer
        self._exited = asyncio.Event()
        self._watch = asyncio.create_task(self._watch_exit(), name=f"holder-{info.pid}")

    @classmethod
    async def attach(cls, info: HolderInfo, *, limit: int) -> HeldProcess | None:
        """Connect to a running holder from ``info.offset``; None if it does not answer."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(info.socket, limit=limit), HELLO_TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            log.warning("worker holder %s did not accept a connection: %s", info.pid, exc)
            return None
        try:
            writer.write(json.dumps({"op": "attach", "offset": info.offset}).encode() + b"\n")
            await writer.drain()
            hello = json.loads(await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_S))
        except (OSError, TimeoutError, ValueError) as exc:
            log.warning("worker holder %s did not say hello: %s", info.pid, exc)
            writer.close()
            return None
        if (
            not isinstance(hello, dict)
            or hello.get("holder") != PROTOCOL
            or hello.get("pid") != info.pid
        ):
            log.warning("worker holder %s answered %r; not reconnecting", info.pid, hello)
            writer.close()
            return None
        return cls(info, reader, writer)

    async def _watch_exit(self) -> None:
        gone = asyncio.Event()
        exits = pid_watch.ExitWatches(lambda _pid: gone.set())
        exits.watch([self.pid])
        try:
            if self.pid in exits.unwatched:
                while self.info.alive():
                    await asyncio.sleep(1.0)
            else:
                await gone.wait()
        finally:
            exits.close()
        code = read_exit(self.info.exit)
        # A holder killed outright leaves no exit file; that is what SIGKILL looks like.
        self.returncode = -signal.SIGKILL if code is None else code
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def detach(self) -> None:
        """Let go of the holder without stopping it: the next runtime reconnects."""
        self._watch.cancel()
        self._writer.close()


async def spawn_holder(
    process_factory: Any,
    scope_prefix: list[str],
    python: str,
    info: HolderInfo,
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    limit: int,
) -> HeldProcess:
    """Start a holder running ``command`` and connect to it.

    Raises ``OSError`` when the holder did not come up; it is stopped first, so nothing
    is left running that the runtime does not know about.
    """
    process = await process_factory(
        *scope_prefix,
        python,
        "-I",
        str(HOLDER_PATH),
        "--socket",
        info.socket,
        "--log",
        info.log,
        "--exit",
        info.exit,
        "--",
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        # The holder leads a process group of its own with the CLI in it, so one signal
        # to the group stops both, exactly as it stopped the CLI alone before.
        start_new_session=True,
    )

    def stop() -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)

    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), HELLO_TIMEOUT_S)
        hello = json.loads(line)
        if not isinstance(hello, dict) or hello.get("holder") != PROTOCOL:
            raise ValueError(f"unexpected holder hello {line!r}")
    except (OSError, TimeoutError, ValueError) as exc:
        stop()
        raise OSError(f"the worker holder did not start: {exc}") from exc
    info.pid = process.pid
    info.pid_start = process_start(process.pid)
    held = await HeldProcess.attach(info, limit=limit)
    if held is None:
        stop()
        raise OSError("the worker holder did not accept a connection")
    return held
