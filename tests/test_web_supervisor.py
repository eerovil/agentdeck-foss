from __future__ import annotations

import asyncio
import itertools
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from uvicorn.logging import AccessFormatter

from agentdeck import web_idle, web_supervisor, web_worker


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]



def _wait_for(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_systemd_listener_claims_exactly_one_descriptor(monkeypatch) -> None:
    claimed: list[int] = []

    class Listener:
        def getsockopt(self, _level: int, option: int) -> int:
            if option == socket.SO_TYPE:
                return socket.SOCK_STREAM
            if option == socket.SO_ACCEPTCONN:
                return 1
            raise AssertionError(option)

    sentinel = Listener()
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setenv("LISTEN_FDNAMES", "web")
    monkeypatch.setattr(
        web_supervisor.socket,
        "socket",
        lambda *, fileno: claimed.append(fileno) or sentinel,
    )

    assert web_supervisor._take_systemd_listener() is sentinel
    assert claimed == [3]
    assert "LISTEN_PID" not in os.environ
    assert "LISTEN_FDS" not in os.environ
    assert "LISTEN_FDNAMES" not in os.environ


def test_systemd_listener_rejects_an_ambiguous_descriptor_set(monkeypatch) -> None:
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "2")

    with pytest.raises(RuntimeError, match="expected one"):
        web_supervisor._take_systemd_listener()


def test_web_worker_installs_the_same_bearer_redaction_as_the_cli() -> None:
    previous_factory = web_worker.logging.getLogRecordFactory()
    stream = StringIO()
    handler = web_worker.logging.StreamHandler(stream)
    logger = web_worker.logging.getLogger("agentdeck.redaction-probe")
    before_handlers = tuple(logger.handlers)
    before_propagate = logger.propagate
    logger.handlers[:] = [handler]
    logger.propagate = False
    try:
        web_worker._install_redaction_filter()
        logger.warning("Bearer %s", "secret-token")
        assert stream.getvalue().strip() == "Bearer [redacted]"
    finally:
        web_worker.logging.setLogRecordFactory(previous_factory)
        logger.handlers[:] = before_handlers
        logger.propagate = before_propagate


def test_redaction_preserves_uvicorns_structured_access_record() -> None:
    previous_factory = web_worker.logging.getLogRecordFactory()
    stream = StringIO()
    handler = web_worker.logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    logger = web_worker.logging.getLogger("uvicorn.access")
    before_handlers = tuple(logger.handlers)
    before_level = logger.level
    before_propagate = logger.propagate
    logger.handlers[:] = [handler]
    logger.setLevel(web_worker.logging.INFO)
    logger.propagate = False
    try:
        web_worker._install_redaction_filter()
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1",
            "GET",
            "/" + "sk-" + "ant-secret",
            "1.1",
            200,
        )
        assert "[redacted]" in stream.getvalue()
        assert "secret" not in stream.getvalue()
    finally:
        web_worker.logging.setLogRecordFactory(previous_factory)
        logger.handlers[:] = before_handlers
        logger.setLevel(before_level)
        logger.propagate = before_propagate


def test_worker_readiness_requires_every_initial_account_scan() -> None:
    readiness = {"finished": False}
    app = SimpleNamespace(
        state=SimpleNamespace(
            collector=SimpleNamespace(
                initial_scans_finished=lambda: readiness["finished"],
            ),
        )
    )

    assert not web_worker._initial_scans_finished(app)
    readiness["finished"] = True
    assert web_worker._initial_scans_finished(app)


@pytest.mark.parametrize(
    ("failing_commands", "old_stopped"),
    [
        ({b"A"}, False),
        ({b"Q", b"A"}, False),
        ({b"A", b"R"}, True),
        ({b"Q", b"A", b"R"}, True),
    ],
)
def test_failed_handoff_resumes_or_retires_the_old_worker(
    tmp_path: Path, monkeypatch, failing_commands: set[bytes], old_stopped: bool
) -> None:
    class Worker:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self) -> None:
            raise AssertionError("worker should terminate cleanly")

    old = Worker(301)
    replacement = Worker(302)
    commands: list[tuple[int, bytes]] = []

    def send(worker, command: bytes) -> None:
        commands.append((worker.pid, command))
        if command in failing_commands:
            raise RuntimeError(f"failed {command!r}")

    with socket.socket() as listener:
        supervisor = web_supervisor.WebSupervisor(
            listener, pid_file=tmp_path / "worker.pid"
        )
        supervisor.worker = old  # type: ignore[assignment]
        monkeypatch.setattr(supervisor, "_spawn_ready_worker", lambda: replacement)
        monkeypatch.setattr(supervisor, "_send_worker_command", send)

        with pytest.raises(RuntimeError):
            supervisor._replace_worker()

    assert replacement.terminated
    assert old.terminated is old_stopped
    assert supervisor.worker is (None if old_stopped else old)
    assert commands == [(301, b"Q"), (302, b"A"), (301, b"R")]
    assert (tmp_path / "reload.failed").read_text() == "failed b'A'\n"


def test_an_old_worker_too_slow_to_stop_accepting_is_replaced_anyway(
    tmp_path: Path, monkeypatch
) -> None:
    class Worker:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    old = Worker(401)
    replacement = Worker(402)
    commands: list[tuple[int, bytes]] = []

    def send(worker, command: bytes) -> None:
        commands.append((worker.pid, command))
        if command == b"Q":
            raise RuntimeError("web worker 401 did not acknowledge b'Q'")

    with socket.socket() as listener:
        supervisor = web_supervisor.WebSupervisor(listener, pid_file=tmp_path / "worker.pid")
        (tmp_path / "reload.failed").write_text("an earlier failure\n")
        supervisor.worker = old  # type: ignore[assignment]
        monkeypatch.setattr(supervisor, "_spawn_ready_worker", lambda: replacement)
        monkeypatch.setattr(supervisor, "_send_worker_command", send)

        supervisor._replace_worker()

    assert commands == [(401, b"Q"), (402, b"A")]
    assert supervisor.worker is replacement
    assert (tmp_path / "worker.pid").read_text() == "402\n"
    assert not (tmp_path / "reload.failed").exists()
    _wait_for(lambda: old.terminated)
    assert not replacement.terminated


def test_the_old_worker_gets_longer_to_stop_accepting_than_other_commands(
    tmp_path: Path,
) -> None:
    class Worker:
        pid = 304

    command_read, command_write = os.pipe()
    acknowledgement_read, acknowledgement_write = os.pipe()
    try:
        with socket.socket() as listener:
            supervisor = web_supervisor.WebSupervisor(
                listener,
                pid_file=tmp_path / "worker.pid",
                command_timeout_s=0.01,
                quiesce_timeout_s=0.5,
            )
            supervisor._worker_controls[Worker.pid] = web_supervisor._WorkerControl(
                command_write=command_write,
                acknowledgement_read=acknowledgement_read,
            )
            # The busy worker answers after the ordinary command budget.
            timer = threading.Timer(0.2, os.write, (acknowledgement_write, b"Q"))
            timer.start()
            supervisor._send_worker_command(Worker(), b"Q")  # type: ignore[arg-type]
            timer.join()

            with pytest.raises(RuntimeError, match="did not acknowledge"):
                supervisor._send_worker_command(Worker(), b"R")  # type: ignore[arg-type]
    finally:
        os.close(command_read)
        os.close(command_write)
        os.close(acknowledgement_read)
        os.close(acknowledgement_write)


def test_worker_command_timeout_is_independent_of_slow_startup_budget(
    tmp_path: Path,
) -> None:
    class Worker:
        pid = 303

    command_read, command_write = os.pipe()
    acknowledgement_read, acknowledgement_write = os.pipe()
    try:
        with socket.socket() as listener:
            supervisor = web_supervisor.WebSupervisor(
                listener,
                pid_file=tmp_path / "worker.pid",
                start_timeout_s=30,
                command_timeout_s=0.01,
                quiesce_timeout_s=0.01,
            )
            supervisor._worker_controls[Worker.pid] = web_supervisor._WorkerControl(
                command_write=command_write,
                acknowledgement_read=acknowledgement_read,
            )

            started = time.monotonic()
            with pytest.raises(RuntimeError, match="did not acknowledge"):
                supervisor._send_worker_command(Worker(), b"Q")  # type: ignore[arg-type]

            assert time.monotonic() - started < 0.5
    finally:
        os.close(command_read)
        os.close(command_write)
        os.close(acknowledgement_read)
        os.close(acknowledgement_write)


def test_replacement_does_not_wait_for_the_old_worker_to_finish_draining(
    tmp_path: Path, monkeypatch
) -> None:
    release = threading.Event()

    class Worker:
        def __init__(self, pid: int, *, slow: bool = False) -> None:
            self.pid = pid
            self.slow = slow
            self.terminated = False
            self.killed = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            if self.slow:
                release.wait(5)
            return 0

        def kill(self) -> None:
            self.killed = True

        def poll(self):
            return None

    old = Worker(101, slow=True)
    replacement = Worker(202)
    with socket.socket() as listener:
        supervisor = web_supervisor.WebSupervisor(listener, pid_file=tmp_path / "worker.pid")
        supervisor.worker = old  # type: ignore[assignment]
        monkeypatch.setattr(supervisor, "_spawn_ready_worker", lambda: replacement)
        commands: list[tuple[int, bytes]] = []
        monkeypatch.setattr(
            supervisor,
            "_send_worker_command",
            lambda worker, command: commands.append((worker.pid, command)),
        )

        started = time.monotonic()
        supervisor._replace_worker()

        assert time.monotonic() - started < 0.5
        assert supervisor.worker is replacement
        assert commands == [(101, b"Q"), (202, b"A")]
        assert old.terminated
        assert not old.killed
        release.set()


class _ThreadWorker:
    """`subprocess.Popen` for `agentdeck.web_worker`, as a thread of this process.

    The real worker module, `main()` and all: its arguments, the uvicorn server on
    the inherited listener, the readiness and command pipes, the graceful drain and
    the idle exit code. Only the app is small — the deck's own takes seconds to
    import and start in a fresh interpreter, twice per reload. The descriptors the
    supervisor passes are duplicated, as a fork would, so a worker closes its own
    copies and never the supervisor's.
    """

    _pids = itertools.count(10_000_000)

    def __init__(self, command: list[str], *, pass_fds=(), **_options) -> None:
        assert command[1:3] == ["-m", "agentdeck.web_worker"], command
        own = {fd: os.dup(fd) for fd in pass_fds}
        argv = [
            str(own[int(arg)]) if arg.isdigit() and int(arg) in own else arg for arg in command[3:]
        ]
        self.args = command
        self.pid = next(self._pids)
        self.returncode: int | None = None
        self._thread = threading.Thread(target=self._run, args=(argv,), daemon=True)
        self._thread.start()

    def _run(self, argv: list[str]) -> None:
        try:
            web_worker.main(argv)
            code = 0
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
        except BaseException:  # noqa: BLE001 - a crashed worker is an exit, as in a process
            code = 1
        self.returncode = code

    def _server(self):
        return getattr(self._thread, "server", None)

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else self.returncode

    def terminate(self) -> None:
        if (server := self._server()) is not None:
            server.should_exit = True

    def kill(self) -> None:
        if (server := self._server()) is not None:
            server.force_exit = server.should_exit = True

    def wait(self, timeout: float | None = None) -> int | None:
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode


def _small_deck(runtime_socket: Path):
    """What the supervisor needs of an app: `/healthz`, an SSE stream that never ends
    by itself, finished initial scans, and a runtime client for the idle probe."""

    async def healthz(_request):
        return PlainTextResponse("ok")

    async def events(_request):
        async def stream():
            yield b": connected\n\n"
            while True:
                await asyncio.sleep(3600)

        return StreamingResponse(stream(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/healthz", healthz), Route("/events", events)])
    app.state.collector = SimpleNamespace(initial_scans_finished=lambda: True)
    # What the worker tells the wake-on service about serving (#3584).
    accepting: list[bool] = []
    app.state.wake_on = SimpleNamespace(accepting=accepting, set_accepting=accepting.append)
    # Nothing listens there, so the probe finds no runtime holding work.
    app.state.runtime_http = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=str(runtime_socket)), base_url="http://runtime"
    )
    return app


@pytest.fixture
def in_process_workers(tmp_path: Path, monkeypatch):
    """Spawn the supervisor's workers as `_ThreadWorker`s serving `_small_deck`."""

    class _RecordedServer(web_worker._ReadyServer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            threading.current_thread().server = self

    app = _small_deck(tmp_path / "runtime.sock")
    monkeypatch.setattr(web_supervisor.subprocess, "Popen", _ThreadWorker)
    monkeypatch.setattr(web_supervisor, "_LOOP_POLL_S", 0.01)
    monkeypatch.setattr(web_worker, "_ReadyServer", _RecordedServer)
    monkeypatch.setattr(web_worker, "_GRACEFUL_SHUTDOWN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(web_worker, "initialize_error_reporting", lambda _name: None)
    monkeypatch.setattr(web_worker, "check_at_start", lambda _name: None)
    monkeypatch.setattr(web_worker, "config_path", lambda: None)
    monkeypatch.setattr(web_worker, "load_config", lambda _path: None)
    monkeypatch.setattr(web_worker, "create_app", lambda _config: app)
    record_factory = logging.getLogRecordFactory()
    yield app
    logging.setLogRecordFactory(record_factory)


def _supervise(listener: socket.socket, pid_file: Path, drive, *, give_up_s: float = 30.0):
    """Run the supervisor here, as its process would, while `drive` runs beside it.

    `run()` takes the signal handlers, so it needs the main thread. `drive` gets the
    supervisor and does what the clients and systemd would; anything it raises is
    re-raised here once the supervisor has stopped.
    """
    supervisor = web_supervisor.WebSupervisor(listener, pid_file=pid_file)
    failure: list[BaseException] = []

    def driver() -> None:
        try:
            drive(supervisor)
        except BaseException as error:  # noqa: BLE001 - handed back to the test
            failure.append(error)
            supervisor.request_stop(signal.SIGTERM, None)

    handlers = {
        number: signal.getsignal(number)
        for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
    }
    thread = threading.Thread(target=driver, daemon=True)
    deadline = threading.Timer(give_up_s, supervisor.request_stop, args=(signal.SIGTERM, None))
    deadline.start()
    thread.start()
    try:
        supervisor.run()
    finally:
        deadline.cancel()
        for number, handler in handlers.items():
            signal.signal(number, handler)
    thread.join(5)
    if failure:
        raise failure[0]
    return supervisor


def _listener() -> socket.socket:
    return socket.create_server(("127.0.0.1", 0), backlog=2048, reuse_port=False)


def test_reload_serves_requests_until_the_replacement_is_ready(tmp_path: Path) -> None:
    port = _unused_port()
    config = tmp_path / "config.toml"
    database = tmp_path / "agentdeck.db"
    pid_file = tmp_path / "worker.pid"
    config.write_text(
        f"""
[server]
bind = "127.0.0.1"
port = {port}

[history]
db_path = "{database}"
"""
    )
    env = {
        **os.environ,
        "AGENTDECK_CONFIG": str(config),
        "AGENTDECK_WEB_WORKER_PID": str(pid_file),
        # Isolate the worker from the host's live runtime: otherwise it polls real
        # jobs and Works while the test measures reload and shutdown (#2704).
        "AGENTDECK_CODEX_SOCKET": str(tmp_path / "runtime.sock"),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "agentdeck.web_supervisor"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    failures: list[str] = []
    successes = 0
    stop_requests = threading.Event()

    def healthy() -> bool:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200
        except httpx.HTTPError:
            return False

    def request_loop() -> None:
        nonlocal successes
        while not stop_requests.is_set():
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
                if response.status_code != 200:
                    failures.append(f"HTTP {response.status_code}")
                else:
                    successes += 1
            except httpx.HTTPError as exc:
                failures.append(type(exc).__name__)

    try:
        _wait_for(lambda: pid_file.exists() and healthy())
        first_worker = pid_file.read_text().strip()
        sse = socket.create_connection(("127.0.0.1", port), timeout=3)
        sse.sendall(
            b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
        )
        sse.settimeout(3)
        assert b"200 OK" in sse.recv(4096)
        requester = threading.Thread(target=request_loop, daemon=True)
        requester.start()

        supervisor.send_signal(signal.SIGHUP)
        _wait_for(lambda: pid_file.read_text().strip() != first_worker)
        second_worker = pid_file.read_text().strip()
        successes_at_handoff = successes
        _wait_for(lambda: successes >= successes_at_handoff + 20)
        _wait_for(lambda: not Path(f"/proc/{first_worker}").exists(), timeout=10)

        assert second_worker != first_worker
        assert failures == []
    finally:
        stop_requests.set()
        if "requester" in locals():
            requester.join(timeout=5)
            assert not requester.is_alive()
        if "sse" in locals():
            sse.close()
        supervisor.terminate()
        # The worker alone may take its five-second graceful window plus app
        # shutdown, and far longer on a loaded host; ten seconds here used to
        # SIGKILL a supervisor that was still stopping cleanly (#2704).
        try:
            supervisor.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(supervisor.pid, signal.SIGKILL)
            supervisor.wait()
        if supervisor.returncode != 0:
            stderr = (supervisor.stderr.read() if supervisor.stderr else b"").decode()
            raise AssertionError(f"supervisor exited {supervisor.returncode}:\n{stderr}")


class _FakeSchedule:
    """A clock that only moves when the watchdog sleeps, with a step budget."""

    def __init__(self, steps: int) -> None:
        self.now = 0.0
        self.remaining = steps

    async def sleep(self, seconds: float) -> None:
        if self.remaining <= 0:
            raise _ScheduleExhausted
        self.remaining -= 1
        self.now += seconds


class _ScheduleExhausted(Exception):
    pass


def _watchdog(schedule: _FakeSchedule, samples, *, busy=False, timeout_s: float = 60.0):
    stopped: list[bool] = []
    activity = iter(samples)
    watchdog = web_idle.IdleWatchdog(
        timeout_s=timeout_s,
        web_activity=lambda: next(activity),
        runtime_busy=_returning(busy),
        on_idle=lambda: stopped.append(True),
        clock=lambda: schedule.now,
        sleep=schedule.sleep,
    )
    return watchdog, stopped


def _returning(value: bool):
    async def probe() -> bool:
        return value

    return probe


async def test_a_quiet_worker_stops_once_the_idle_window_passes() -> None:
    schedule = _FakeSchedule(steps=8)
    quiet = [web_idle.WebActivity(total_requests=7, open_connections=0)] * 9
    watchdog, stopped = _watchdog(schedule, quiet)

    await watchdog.run()

    assert stopped == [True]
    # Four fifteen-second polls to cross a sixty-second window, not one.
    assert schedule.now == pytest.approx(60.0)


async def test_an_open_connection_keeps_the_worker_alive() -> None:
    schedule = _FakeSchedule(steps=8)
    watched = [web_idle.WebActivity(total_requests=7, open_connections=1)] * 9
    watchdog, stopped = _watchdog(schedule, watched)

    with pytest.raises(_ScheduleExhausted):
        await watchdog.run()

    assert stopped == []


async def test_a_served_request_restarts_the_idle_window() -> None:
    schedule = _FakeSchedule(steps=8)
    samples = [
        web_idle.WebActivity(total_requests=7, open_connections=0),
        web_idle.WebActivity(total_requests=7, open_connections=0),
        web_idle.WebActivity(total_requests=8, open_connections=0),
        *[web_idle.WebActivity(total_requests=8, open_connections=0)] * 6,
    ]
    watchdog, stopped = _watchdog(schedule, samples)

    await watchdog.run()

    assert stopped == [True]
    # The request at 30s reset the window, so the stop lands at 30 + 60.
    assert schedule.now == pytest.approx(90.0)


async def test_the_worker_stays_up_while_the_runtime_still_holds_work() -> None:
    schedule = _FakeSchedule(steps=8)
    quiet = [web_idle.WebActivity(total_requests=1, open_connections=0)] * 9
    watchdog, stopped = _watchdog(schedule, quiet, busy=True)

    with pytest.raises(_ScheduleExhausted):
        await watchdog.run()

    assert stopped == []


@pytest.mark.parametrize(
    ("payload", "busy"),
    [
        ({"active": True, "live_worker_processes": 0, "unfinished_jobs": 0}, True),
        ({"active": False, "live_worker_processes": 1, "unfinished_jobs": 0}, True),
        ({"active": False, "live_worker_processes": 0, "unfinished_jobs": 1}, True),
        ({"active": False, "live_worker_processes": 0, "unfinished_jobs": 0}, False),
        ({"active": False, "active_turns": {"codex": 0}}, False),
    ],
)
async def test_runtime_activity_decides_whether_stopping_would_lose_work(
    payload: dict, busy: bool
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        base_url="http://runtime",
    ) as client:
        assert await web_idle.runtime_is_busy(client) is busy


async def test_an_unreachable_runtime_does_not_hold_the_worker_open() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no runtime", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(refuse), base_url="http://runtime"
    ) as client:
        assert await web_idle.runtime_is_busy(client) is False

    assert await web_idle.runtime_is_busy(None) is False


@pytest.mark.parametrize(
    ("value", "timeout"),
    [("900", 900.0), ("", 0.0), ("0", 0.0), ("-5", 0.0), ("nonsense", 0.0)],
)
def test_the_idle_watchdog_is_off_unless_the_unit_asks_for_it(
    value: str, timeout: float
) -> None:
    assert web_idle.idle_timeout_s({"AGENTDECK_WEB_IDLE_TIMEOUT_S": value}) == timeout
    assert web_idle.idle_timeout_s({}) == 0.0


def test_the_supervisor_stands_down_instead_of_replacing_an_idle_worker(
    tmp_path: Path, monkeypatch
) -> None:
    class Worker:
        pid = 4242
        returncode = web_idle.IDLE_EXIT_CODE

        def poll(self):
            return web_idle.IDLE_EXIT_CODE

        def terminate(self) -> None:
            raise AssertionError("an idle worker has already exited")

    with socket.socket() as listener:
        supervisor = web_supervisor.WebSupervisor(
            listener, pid_file=tmp_path / "worker.pid"
        )
        replacements: list[int] = []

        def spawn():
            replacements.append(1)
            return Worker()

        monkeypatch.setattr(supervisor, "_spawn_ready_worker", spawn)
        monkeypatch.setattr(supervisor, "_send_worker_command", lambda *_: None)
        monkeypatch.setattr(supervisor, "_record_worker", lambda _worker: None)

        handlers = {
            number: signal.getsignal(number)
            for number in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        }
        try:
            supervisor.run()
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)

    # One spawn at startup and no replacement after the idle exit.
    assert replacements == [1]
    assert supervisor.worker is None


def test_an_unobserved_worker_stops_itself_and_the_socket_can_start_it_again(
    tmp_path: Path, in_process_workers, monkeypatch
) -> None:
    idle_s = 0.05
    monkeypatch.setenv("AGENTDECK_WEB_IDLE_TIMEOUT_S", str(idle_s))
    listener = _listener()
    port = listener.getsockname()[1]
    pid_file = tmp_path / "worker.pid"
    workers: list[_ThreadWorker] = []

    # Attach before any worker exists: the connection waits in the listener's queue
    # and is the first thing the worker accepts. Attaching after a health check
    # raced the idle window, which a loaded host let lapse in between (#3256).
    held = socket.create_connection(("127.0.0.1", port), timeout=15)
    held.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")

    def drive(supervisor) -> None:
        try:
            assert b"200 OK" in held.recv(4096)
            _wait_for(pid_file.exists)
            workers.append(supervisor.worker)

            # A watched deck is never idle, however long nothing else asks for it.
            time.sleep(3 * idle_s)
            assert workers[0].poll() is None
        finally:
            held.close()

    started = time.monotonic()
    supervisor = _supervise(listener, pid_file, drive, give_up_s=20.0)

    # The supervisor stood down by itself, on the worker's idle exit, with nobody
    # asking it to stop — well inside the deadline that would have.
    assert time.monotonic() - started < 20.0
    assert workers[0].returncode == web_idle.IDLE_EXIT_CODE
    assert supervisor.worker is None
    assert not pid_file.exists()
    # Its app counted as serving only once the supervisor activated it (#3584).
    assert in_process_workers.state.wake_on.accepting == [False, True]


async def test_a_request_arriving_during_the_runtime_probe_cancels_the_stop() -> None:
    """The runtime probe is a round trip; the deck can be woken inside it."""
    schedule = _FakeSchedule(steps=6)
    served = {"requests": 4, "connections": 0}
    probes: list[int] = []
    stopped: list[bool] = []

    async def probe() -> bool:
        probes.append(1)
        # A browser attaches while the watchdog is waiting on the runtime.
        served["requests"] += 1
        served["connections"] = 1
        return False

    watchdog = web_idle.IdleWatchdog(
        timeout_s=60.0,
        web_activity=lambda: web_idle.WebActivity(
            total_requests=served["requests"],
            open_connections=served["connections"],
        ),
        runtime_busy=probe,
        on_idle=lambda: stopped.append(True),
        clock=lambda: schedule.now,
        sleep=schedule.sleep,
    )

    with pytest.raises(_ScheduleExhausted):
        await watchdog.run()

    assert probes == [1]
    assert stopped == []


def test_the_serving_worker_is_told_once_its_predecessor_has_drained(
    tmp_path: Path, monkeypatch
) -> None:
    """#3070: only then are the old worker's last store writes on disk."""

    class Worker:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            pass

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

    old, replacement = Worker(101), Worker(202)
    command_read, command_write = os.pipe()
    acknowledgement_read, acknowledgement_write = os.pipe()
    try:
        with socket.socket() as listener:
            supervisor = web_supervisor.WebSupervisor(listener, pid_file=tmp_path / "worker.pid")
            supervisor.worker = old  # type: ignore[assignment]
            supervisor._worker_controls[replacement.pid] = web_supervisor._WorkerControl(
                command_write=command_write, acknowledgement_read=acknowledgement_read
            )
            monkeypatch.setattr(supervisor, "_spawn_ready_worker", lambda: replacement)
            monkeypatch.setattr(supervisor, "_send_worker_command", lambda worker, command: None)

            supervisor._replace_worker()

            os.set_blocking(command_read, True)
            assert os.read(command_read, 1) == b"D"
            supervisor._close_worker_control(replacement)  # type: ignore[arg-type]
    finally:
        os.close(command_read)
        os.close(acknowledgement_write)
