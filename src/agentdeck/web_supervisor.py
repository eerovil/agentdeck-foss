"""Keep the web listener alive while replaceable Uvicorn workers overlap."""

from __future__ import annotations

import logging
import os
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import config_path, load_config
from .error_reporting import initialize_error_reporting
from .local_bin import ensure_local_bin_on_path
from .web_idle import IDLE_EXIT_CODE

_LOG = logging.getLogger(__name__)
_SYSTEMD_FD_START = 3
_DEFAULT_START_TIMEOUT_S = 300.0
_DEFAULT_COMMAND_TIMEOUT_S = 5.0
# A busy old worker can take several seconds to stop accepting (#2979: ~10 s on a
# loaded host). Give it longer than an ordinary command, then hand over anyway.
_DEFAULT_QUIESCE_TIMEOUT_S = 30.0
_DEFAULT_STOP_TIMEOUT_S = 5.0
# How often the supervisor looks for a reload, a stop or an exited worker.
_LOOP_POLL_S = 0.25


@dataclass
class _WorkerControl:
    command_write: int
    acknowledgement_read: int


def _take_systemd_listener() -> socket.socket | None:
    """Claim the one socket passed by systemd, following ``sd_listen_fds``."""

    try:
        listen_pid = int(os.environ.get("LISTEN_PID", "0"))
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError as exc:
        raise RuntimeError("invalid systemd socket activation environment") from exc
    if listen_pid != os.getpid():
        return None
    if listen_fds != 1:
        raise RuntimeError(f"expected one systemd web listener, received {listen_fds}")
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    listener = socket.socket(fileno=_SYSTEMD_FD_START)
    if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise RuntimeError("systemd web descriptor is not a stream socket")
    if not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
        raise RuntimeError("systemd web descriptor is not a listening socket")
    return listener


def _open_listener() -> socket.socket:
    inherited = _take_systemd_listener()
    if inherited is not None:
        return inherited
    config = load_config(config_path())
    return socket.create_server(
        (config.server.bind, config.server.port),
        backlog=2048,
        reuse_port=False,
    )


def _pid_file() -> Path:
    configured = os.environ.get("AGENTDECK_WEB_WORKER_PID", "").strip()
    if configured:
        return Path(configured).expanduser()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    if not runtime.is_dir():
        # macOS has no per-user runtime folder; the runtime's socket falls back the same way.
        runtime = Path("~/.cache").expanduser()
    return runtime / "agentdeck-web" / "worker.pid"


def reload_failure_file(pid_file: Path) -> Path:
    """Where the supervisor says its last replacement failed, beside the pid file.

    The deploy removes it before a reload and fails at once when it appears,
    instead of waiting out its whole timeout for a pid that will not change.
    """

    return pid_file.with_name("reload.failed")


class WebSupervisor:
    def __init__(
        self,
        listener: socket.socket,
        *,
        pid_file: Path,
        start_timeout_s: float = _DEFAULT_START_TIMEOUT_S,
        command_timeout_s: float = _DEFAULT_COMMAND_TIMEOUT_S,
        quiesce_timeout_s: float = _DEFAULT_QUIESCE_TIMEOUT_S,
        stop_timeout_s: float = _DEFAULT_STOP_TIMEOUT_S,
    ) -> None:
        self.listener = listener
        self.pid_file = pid_file
        self.start_timeout_s = start_timeout_s
        self.command_timeout_s = command_timeout_s
        self.quiesce_timeout_s = quiesce_timeout_s
        self.stop_timeout_s = stop_timeout_s
        self.worker: subprocess.Popen[bytes] | None = None
        self._worker_controls: dict[int, _WorkerControl] = {}
        # Guards the control pipes: a drain thread writes to the current worker's while
        # the main thread may be closing it.
        self._controls_lock = threading.Lock()
        self._reload = threading.Event()
        self._stop = threading.Event()

    def request_reload(self, _signum: int, _frame: object) -> None:
        self._reload.set()

    def request_stop(self, _signum: int, _frame: object) -> None:
        self._stop.set()

    def _spawn_ready_worker(self) -> subprocess.Popen[bytes]:
        ready_read, ready_write = os.pipe()
        command_read, command_write = os.pipe()
        acknowledgement_read, acknowledgement_write = os.pipe()
        command = [
            sys.executable,
            "-m",
            "agentdeck.web_worker",
            "--listener-fd",
            str(self.listener.fileno()),
            "--ready-fd",
            str(ready_write),
            "--command-fd",
            str(command_read),
            "--acknowledgement-fd",
            str(acknowledgement_write),
        ]
        worker = subprocess.Popen(
            command,
            pass_fds=(
                self.listener.fileno(),
                ready_write,
                command_read,
                acknowledgement_write,
            ),
        )
        os.close(ready_write)
        os.close(command_read)
        os.close(acknowledgement_write)
        self._worker_controls[worker.pid] = _WorkerControl(
            command_write=command_write,
            acknowledgement_read=acknowledgement_read,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(ready_read, selectors.EVENT_READ)
                deadline = time.monotonic() + self.start_timeout_s
                while time.monotonic() < deadline:
                    if worker.poll() is not None:
                        raise RuntimeError(
                            f"web worker {worker.pid} exited before becoming ready"
                        )
                    if selector.select(timeout=0.1):
                        if os.read(ready_read, 1) == b"1":
                            return worker
                        raise RuntimeError(
                            f"web worker {worker.pid} closed its readiness pipe"
                        )
                    if self._stop.is_set():
                        raise RuntimeError("web supervisor stopped during worker startup")
            raise RuntimeError(
                f"web worker {worker.pid} was not ready after {self.start_timeout_s:g}s"
            )
        except Exception:
            self._stop_failed_worker(worker)
            raise
        finally:
            os.close(ready_read)

    def _record_worker(self, worker: subprocess.Popen[bytes]) -> None:
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pid_file.with_suffix(".tmp")
        temporary.write_text(f"{worker.pid}\n")
        temporary.replace(self.pid_file)
        reload_failure_file(self.pid_file).unlink(missing_ok=True)

    def _record_failure(self, error: BaseException) -> None:
        try:
            self.pid_file.parent.mkdir(parents=True, exist_ok=True)
            reload_failure_file(self.pid_file).write_text(f"{error}\n")
        except OSError:
            _LOG.exception("could not record the failed web worker replacement")

    def _stop_failed_worker(self, worker: subprocess.Popen[bytes]) -> None:
        if worker.poll() is not None:
            self._close_worker_control(worker)
            return
        worker.terminate()
        try:
            worker.wait(timeout=self.stop_timeout_s)
        except subprocess.TimeoutExpired:
            _LOG.warning("web worker %d did not drain; killing it", worker.pid)
            worker.kill()
            worker.wait()
        self._close_worker_control(worker)

    def _close_worker_control(self, worker: subprocess.Popen[bytes]) -> None:
        with self._controls_lock:
            control = self._worker_controls.pop(worker.pid, None)
            if control is None:
                return
            os.close(control.command_write)
            os.close(control.acknowledgement_read)

    def _tell_drained(self) -> None:
        """Tell the serving worker its predecessor has exited (#3070).

        Until then the old worker could still write the stores both share; the new one
        re-reads them on this. Not acknowledged: a worker too busy to read it now reads
        it later, and one that is gone needs nothing.
        """
        with self._controls_lock:
            worker = self.worker
            control = None if worker is None else self._worker_controls.get(worker.pid)
            if control is None:
                return
            try:
                os.write(control.command_write, b"D")
            except OSError:
                _LOG.warning("could not tell web worker %d its predecessor drained", worker.pid)

    def _send_worker_command(
        self, worker: subprocess.Popen[bytes], command: bytes
    ) -> None:
        control = self._worker_controls[worker.pid]
        os.write(control.command_write, command)
        with selectors.DefaultSelector() as selector:
            selector.register(control.acknowledgement_read, selectors.EVENT_READ)
            timeout = self.quiesce_timeout_s if command == b"Q" else self.command_timeout_s
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not selector.select(timeout=min(0.1, deadline - time.monotonic())):
                    continue
                acknowledgement = os.read(control.acknowledgement_read, 1)
                if acknowledgement == command:
                    return
                if not acknowledgement:
                    break
                _LOG.warning(
                    "web worker %d returned stale acknowledgement %r while waiting for %r",
                    worker.pid,
                    acknowledgement,
                    command,
                )
        raise RuntimeError(
            f"web worker {worker.pid} did not acknowledge {command!r}"
        )

    def _drain_worker(self, worker: subprocess.Popen[bytes]) -> None:
        worker.terminate()
        worker.wait()
        self._close_worker_control(worker)
        _LOG.info("web worker %d drained", worker.pid)
        self._tell_drained()

    def _replace_worker(self) -> None:
        try:
            self._hand_over()
        except Exception as error:
            self._record_failure(error)
            raise

    def _hand_over(self) -> None:
        old = self.worker
        replacement = self._spawn_ready_worker()
        old_may_be_quiesced = False
        try:
            if old is not None:
                old_may_be_quiesced = True
                try:
                    self._send_worker_command(old, b"Q")
                except Exception:
                    # A slow old worker is the one most worth replacing (#2979).
                    # Both accepting on the shared listener for a moment is
                    # harmless; the old one drains once the replacement serves.
                    _LOG.warning(
                        "web worker %d did not stop accepting; handing over anyway",
                        old.pid,
                    )
            self._send_worker_command(replacement, b"A")
        except Exception:
            resume_failed = False
            try:
                if old is not None and old_may_be_quiesced:
                    self._send_worker_command(old, b"R")
            except Exception:
                resume_failed = True
                _LOG.exception("could not resume old web worker %d", old.pid)
            finally:
                self._stop_failed_worker(replacement)
            if old is not None and resume_failed:
                self._stop_failed_worker(old)
                self.worker = None
            raise
        self.worker = replacement
        self._record_worker(replacement)
        _LOG.info("web worker %d is ready", replacement.pid)
        if old is not None:
            threading.Thread(
                target=self._drain_worker,
                args=(old,),
                name=f"web-worker-drain-{old.pid}",
                daemon=True,
            ).start()

    def run(self) -> None:
        signal.signal(signal.SIGHUP, self.request_reload)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self._replace_worker()
        try:
            while not self._stop.wait(_LOOP_POLL_S):
                if self._reload.is_set():
                    self._reload.clear()
                    try:
                        self._replace_worker()
                    except Exception:
                        _LOG.exception("replacement web worker did not become ready")
                elif self.worker is not None and self.worker.poll() is not None:
                    if self.worker.returncode == IDLE_EXIT_CODE:
                        # The worker served nothing for its whole idle window and
                        # the runtime held nothing. Exit cleanly so systemd leaves
                        # us stopped; the socket starts us on the next request.
                        _LOG.info(
                            "web worker %d stopped for idleness; standing down",
                            self.worker.pid,
                        )
                        self._close_worker_control(self.worker)
                        self.worker = None
                        self._stop.set()
                        continue
                    _LOG.error("web worker %d exited; replacing it", self.worker.pid)
                    self._replace_worker()
                elif self.worker is None:
                    try:
                        self._replace_worker()
                    except Exception:
                        _LOG.exception("web worker recovery failed")
        finally:
            if self.worker is not None:
                self.worker.terminate()
                self.worker.wait()
                self._close_worker_control(self.worker)
            self.pid_file.unlink(missing_ok=True)
            self.listener.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    initialize_error_reporting("web-supervisor")
    # The web worker inherits this PATH, and every `gh` call it makes needs it.
    ensure_local_bin_on_path()
    listener = _open_listener()
    WebSupervisor(listener, pid_file=_pid_file()).run()


if __name__ == "__main__":
    main()
