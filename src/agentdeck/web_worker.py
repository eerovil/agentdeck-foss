"""One replaceable Uvicorn worker for the socket-owning web supervisor."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import socket
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI

from .app import create_app
from .config import config_path, load_config
from .error_reporting import initialize_error_reporting
from .github_accounts import check_at_start
from .shared_file import refresh_shared_files
from .web_idle import (
    IDLE_EXIT_CODE,
    IdleWatchdog,
    WebActivity,
    idle_timeout_s,
    runtime_is_busy,
)

_GRACEFUL_SHUTDOWN_TIMEOUT_S = 5.0


def _install_redaction_filter() -> None:
    pattern = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+|sk-ant-[A-Za-z0-9._\-]+")
    token = re.compile(r"sk-ant-[A-Za-z0-9._\-]+")
    previous = logging.getLogRecordFactory()

    def redacting_factory(*args, **kwargs) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        if isinstance(record.msg, str):
            # Uvicorn's AccessFormatter consumes a five-field tuple directly;
            # flattening it into msg would make every access log fail. Redact
            # string fields in place and preserve that formatter contract.
            if record.name == "uvicorn.access" and isinstance(record.args, tuple):
                record.msg = pattern.sub(r"\g<1>[redacted]", record.msg)
                record.args = tuple(
                    token.sub("[redacted]", value) if isinstance(value, str) else value
                    for value in record.args
                )
                return record
            try:
                rendered = record.getMessage()
            except (TypeError, ValueError):
                rendered = record.msg
            redacted = pattern.sub(r"\g<1>[redacted]", rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
        return record

    logging.setLogRecordFactory(redacting_factory)


def _initial_scans_finished(app: FastAPI) -> bool:
    return app.state.collector.initial_scans_finished()


def _pause_serving(server: asyncio.Server) -> None:
    """Unregister accepts without closing the inherited listener."""

    if not server.is_serving():
        return
    loop = asyncio.get_running_loop()
    for listener in server.sockets or ():
        loop.remove_reader(listener.fileno())
    # asyncio has start_serving() but no matching public pause operation.
    # Keep its internal flag aligned so start_serving() can register accepts again.
    server._serving = False  # noqa: SLF001


def _set_accepting(app: object, accepting: bool) -> None:
    """Tell the app's wake-on service whether this worker serves the deck (#3584).

    A replacement worker starts its app, wake-on included, before the supervisor hands it
    the listener; a ``deck_deploy`` wait must not wake while the old worker still answers.
    """
    wake_on = getattr(getattr(app, "state", None), "wake_on", None)
    if wake_on is not None:
        wake_on.set_accepting(accepting)


class _ReadyServer(uvicorn.Server):
    """Tell the supervisor only after the app lifespan has started."""

    def __init__(
        self,
        config: uvicorn.Config,
        ready_fd: int,
        command_fd: int,
        acknowledgement_fd: int,
        ready_check: Callable[[], bool],
        idle_timeout_s: float = 0.0,
    ) -> None:
        super().__init__(config)
        self._ready_fd = ready_fd
        self._command_fd = command_fd
        self._acknowledgement_fd = acknowledgement_fd
        self._ready_check = ready_check
        self._idle_timeout_s = idle_timeout_s
        self._control_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self.idle_exit = False

    def _web_activity(self) -> WebActivity:
        state = self.server_state
        return WebActivity(
            total_requests=state.total_requests,
            open_connections=len(state.connections),
        )

    def _stop_for_idleness(self) -> None:
        self.idle_exit = True
        self.should_exit = True

    def _start_idle_watchdog(self) -> None:
        if self._idle_timeout_s <= 0:
            return
        app_state = getattr(self.config.app, "state", None)
        watchdog = IdleWatchdog(
            timeout_s=self._idle_timeout_s,
            web_activity=self._web_activity,
            runtime_busy=lambda: runtime_is_busy(
                getattr(app_state, "runtime_http", None)
            ),
            on_idle=self._stop_for_idleness,
        )
        self._idle_task = asyncio.create_task(watchdog.run(), name="web-worker-idle")

    async def _read_command(self) -> bytes:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[bytes] = loop.create_future()

        def read() -> None:
            if not ready.done():
                ready.set_result(os.read(self._command_fd, 1))

        loop.add_reader(self._command_fd, read)
        try:
            return await ready
        finally:
            loop.remove_reader(self._command_fd)

    async def _control_loop(self) -> None:
        while command := await self._read_command():
            if command == b"Q":
                for server in self.servers:
                    _pause_serving(server)
                # Let an accept callback already queued before remove_reader()
                # register its protocol before acknowledging the quiesce.
                await asyncio.sleep(0)
            elif command == b"R":
                await asyncio.gather(
                    *(server.start_serving() for server in self.servers)
                )
            elif command == b"D":
                # The worker this one replaced has exited, so its last writes are on
                # disk: re-read every shared store it may have written (#3070). Not
                # acknowledged; the supervisor does not wait for it.
                refresh_shared_files()
                continue
            else:
                continue
            os.write(self._acknowledgement_fd, command)

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        assert sockets is not None and len(sockets) == 1
        await self.lifespan.startup()
        if self.lifespan.should_exit:
            self.should_exit = True
            return
        # Lifespan startup schedules the first provider scans rather than
        # awaiting them. Keep the listener out of this event loop until every
        # account has completed one authoritative scan.
        while not self._ready_check():
            await asyncio.sleep(0.05)
        os.write(self._ready_fd, b"1")
        os.close(self._ready_fd)
        self._ready_fd = -1

        if await self._read_command() != b"A":
            raise RuntimeError("first web worker command must activate it")

        config = self.config

        def create_protocol(
            _loop: asyncio.AbstractEventLoop | None = None,
        ) -> asyncio.Protocol:
            return config.http_protocol_class(
                config=config,
                server_state=self.server_state,
                app_state=self.lifespan.state,
                _loop=_loop,
            )

        server = await asyncio.get_running_loop().create_server(
            create_protocol,
            sock=sockets[0],
            ssl=config.ssl,
            backlog=config.backlog,
        )
        self.servers = [server]
        self.started = True
        _set_accepting(config.app, True)
        os.write(self._acknowledgement_fd, b"A")
        self._control_task = asyncio.create_task(
            self._control_loop(), name="web-worker-control"
        )
        self._start_idle_watchdog()

    async def serve_listener(self, listener: socket.socket) -> None:
        try:
            await self.serve(sockets=[listener])
        finally:
            if self._idle_task is not None:
                self._idle_task.cancel()
                await asyncio.gather(self._idle_task, return_exceptions=True)
            if self._ready_fd >= 0:
                os.close(self._ready_fd)
            os.close(self._command_fd)
            os.close(self._acknowledgement_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one supervised AgentDeck web worker.")
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--ready-fd", type=int, required=True)
    parser.add_argument("--command-fd", type=int, required=True)
    parser.add_argument("--acknowledgement-fd", type=int, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_redaction_filter()
    initialize_error_reporting("web-worker")
    check_at_start("web-worker")
    config = load_config(config_path())
    app = create_app(config)
    # Not serving until the supervisor's activation command (``A``) says so.
    _set_accepting(app, False)
    listener = socket.socket(fileno=args.listener_fd)
    server = _ReadyServer(
        uvicorn.Config(
            app,
            log_level="info",
            # Replacement activation is already complete before the old worker
            # drains. Give ordinary in-flight requests a bounded five-second
            # window instead of cancelling them after only two seconds;
            # long-lived SSE connections are still bounded.
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        ),
        args.ready_fd,
        args.command_fd,
        args.acknowledgement_fd,
        lambda: _initial_scans_finished(app),
        idle_timeout_s=idle_timeout_s(),
    )
    asyncio.run(server.serve_listener(listener))
    if server.idle_exit:
        # Tell the supervisor this was a deliberate stop, not a crash to replace.
        raise SystemExit(IDLE_EXIT_CODE)


if __name__ == "__main__":
    main()
