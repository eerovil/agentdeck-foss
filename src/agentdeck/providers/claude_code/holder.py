"""Hold one Claude CLI's pipes so the chat outlives the runtime that started it (#3248).

A deck-owned Claude chat is a ``claude -p --input-format stream-json`` process the runtime
talks to over stdin and stdout. Those pipes used to end in the runtime, so every runtime
restart closed them and the CLI exited mid-turn. This program sits between the two: it
starts the CLI as its own child, keeps both pipes, and serves them on a Unix socket. A
restarting runtime drops its connection and the next one reconnects, while the CLI never
notices.

It writes everything the CLI prints to an append-only log, and a client attaches from a byte
offset, so a runtime that went down mid-turn reads exactly what it missed. When the CLI
exits, its exit code goes to ``--exit`` and the holder exits too; the log and the exit file
stay until the runtime has read them.

Protocol, one JSON object per line from the client:

- ``{"op": "attach", "offset": N}`` -- first. The holder answers with one line
  ``{"holder": PROTOCOL, "pid": ..., "child": ...}``, then the log from byte ``N`` and
  every later byte as it arrives. A new attach replaces any earlier client.
- ``{"op": "stdin", "data": "<one line>", "id": "<delivery id>"}`` -- written to the CLI's
  stdin. Only whole lines reach the CLI: a client that dies half-way through one leaves
  nothing behind. An ``id`` is appended to ``<exit>.writes`` and synced *before* the line
  can reach the CLI, so a runtime that lost its connection mid-write reads there whether
  the delivery got through: listed means it may have, missing means it provably did not.
  A line under an id already listed is dropped, so the same delivery never runs twice.
- ``{"op": "close_stdin"}`` -- the runtime is ending the chat; the CLI sees end of input.

A dropped connection only detaches. Standard library only and never imported by the
runtime: it is started with ``python3 -I`` from the checkout, so a running holder keeps the
code it started with and a change here needs no runtime restart.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time

PROTOCOL = 1
CHUNK = 65536
# Output waiting for a slow client. Past the high mark the holder stops reading the CLI,
# so the CLI waits on its own pipe exactly as it did when the runtime held that pipe;
# it reads again once the backlog is under the low mark (#3248).
BACKLOG_HIGH = 8 * 1024 * 1024
BACKLOG_LOW = 1024 * 1024
# How often an idle holder checks that its socket file is still there.
SOCKET_CHECK_S = 5.0
# How long an exiting holder keeps sending to its client before it leaves anyway.
FINAL_FLUSH_S = 5.0


class Holder:
    def __init__(self, sock_path: str, log_path: str, exit_path: str, argv: list[str]) -> None:
        self.sock_path = sock_path
        self.log_path = log_path
        self.exit_path = exit_path
        self.selector = selectors.DefaultSelector()
        self.log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.listener = self._listen()
        self.child = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        assert self.child.stdin is not None and self.child.stdout is not None
        self.child_in = self.child.stdin.fileno()
        self.child_out = self.child.stdout.fileno()
        os.set_blocking(self.child_in, False)
        os.set_blocking(self.child_out, False)
        self.to_child = bytearray()
        self.close_child_in = False
        self.client: socket.socket | None = None
        self.from_client = bytearray()
        self.to_client = bytearray()
        self.attached = False
        # Where in the log the attached client's catch-up has reached; None once it is
        # live. The past is read from the log a little at a time, never all at once.
        self.replay_at: int | None = None
        self.reading_child = True
        # Delivery ids already handed to the CLI. A second line under one of them is
        # dropped: a runtime that retries a delivery it was unsure of must not run it twice.
        try:
            with open(f"{exit_path}.writes") as ledger:
                self.accepted = set(ledger.read().splitlines())
        except FileNotFoundError:
            self.accepted = set()
        self.selector.register(self.listener, selectors.EVENT_READ, "accept")
        self.selector.register(self.child_out, selectors.EVENT_READ, "child")

    # --- the socket --------------------------------------------------------

    def _listen(self) -> socket.socket:
        os.makedirs(os.path.dirname(self.sock_path), mode=0o700, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        listener.bind(self.sock_path)
        os.chmod(self.sock_path, 0o600)
        listener.listen(4)
        listener.setblocking(False)
        self.sock_inode = os.stat(self.sock_path).st_ino
        return listener

    def _ensure_socket(self) -> None:
        """Bind again if the socket file was removed: it is the only way back in (#3248).

        A runtime directory can be emptied underneath a running holder -- systemd removes a
        service's ``RuntimeDirectory`` when it stops -- and a holder nobody can reach is a
        chat the next runtime has to cut.
        """
        try:
            if os.stat(self.sock_path).st_ino == self.sock_inode:
                return
        except FileNotFoundError:
            pass
        except OSError:
            return
        try:
            listener = self._listen()
        except OSError:
            return
        self.selector.unregister(self.listener)
        self.listener.close()
        self.listener = listener
        self.selector.register(self.listener, selectors.EVENT_READ, "accept")

    # --- the client --------------------------------------------------------

    def _drop_client(self) -> None:
        if self.client is None:
            return
        try:
            self.selector.unregister(self.client)
        except (KeyError, ValueError):
            pass
        self.client.close()
        self.client = None
        self.from_client.clear()
        self.to_client.clear()
        self.attached = False
        self.replay_at = None
        # Nobody to fall behind any more: the log keeps everything for the next one.
        self._pace_child()

    def _accept(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except BlockingIOError:
            return
        # The newest runtime wins: an older connection is a runtime on its way out.
        self._drop_client()
        conn.setblocking(False)
        self.client = conn
        self.selector.register(conn, selectors.EVENT_READ, "client")

    def _replay(self) -> None:
        """Top the client's buffer up from the log until it has caught up, then go live."""
        if self.replay_at is None:
            return
        with open(self.log_path, "rb") as log:
            log.seek(self.replay_at)
            while len(self.to_client) < BACKLOG_LOW:
                chunk = log.read(CHUNK)
                if not chunk:
                    # Everything the CLI printed so far is queued; what follows is live.
                    self.replay_at = None
                    return
                self.to_client += chunk
                self.replay_at += len(chunk)

    def _pace_child(self) -> None:
        """Stop reading the CLI while the client is far behind, and start again later."""
        backlog = len(self.to_client) if self.client is not None else 0
        if self.reading_child and backlog > BACKLOG_HIGH:
            self.selector.unregister(self.child_out)
            self.reading_child = False
        elif not self.reading_child and backlog < BACKLOG_LOW:
            self.selector.register(self.child_out, selectors.EVENT_READ, "child")
            self.reading_child = True

    def _client_events(self) -> None:
        self._replay()
        self._pace_child()
        if self.client is None:
            return
        want = selectors.EVENT_READ
        if self.to_client:
            want |= selectors.EVENT_WRITE
        self.selector.modify(self.client, want, "client")

    def _read_client(self) -> None:
        assert self.client is not None
        try:
            data = self.client.recv(CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            # Detached. Whatever half line it left was never meant for the CLI.
            self._drop_client()
            return
        self.from_client += data
        while (end := self.from_client.find(b"\n")) >= 0:
            line = bytes(self.from_client[:end])
            del self.from_client[: end + 1]
            self._op(line)
            if self.client is None:
                return

    def _op(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        op = message.get("op")
        if op == "attach" and not self.attached:
            offset = message.get("offset")
            offset = offset if isinstance(offset, int) and offset >= 0 else 0
            hello = {"holder": PROTOCOL, "pid": os.getpid(), "child": self.child.pid}
            self.to_client += json.dumps(hello).encode() + b"\n"
            self.replay_at = offset
            self.attached = True
            self._replay()
        elif op == "stdin" and isinstance(message.get("data"), str):
            data = message["data"].encode()
            if not data.endswith(b"\n"):
                data += b"\n"
            if not self.close_child_in:
                delivery_id = message.get("id")
                if isinstance(delivery_id, str) and delivery_id and "\n" not in delivery_id:
                    if delivery_id in self.accepted:
                        data = b""
                    else:
                        self._record_write(delivery_id)
                        self.accepted.add(delivery_id)
                if data:
                    self.to_child += data
                    self._child_in_events()
        elif op == "close_stdin":
            self.close_child_in = True
            self._child_in_events()
        if self.client is not None:
            self._client_events()

    def _record_write(self, delivery_id: str) -> None:
        # Recorded first: a holder that dies between the two leaves a delivery that reads
        # as possibly sent, never one that reads as unsent and goes out twice.
        fd = os.open(f"{self.exit_path}.writes", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, delivery_id.encode() + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_client(self) -> None:
        assert self.client is not None
        try:
            sent = self.client.send(self.to_client)
        except BlockingIOError:
            return
        except OSError:
            self._drop_client()
            return
        del self.to_client[:sent]
        self._client_events()

    # --- the CLI -------------------------------------------------------------

    def _child_in_events(self) -> None:
        if self.child_in < 0:
            return
        if not self.to_child and self.close_child_in:
            self._close_child_in()
            return
        registered = self.child_in in self.selector.get_map()
        if self.to_child and not registered:
            self.selector.register(self.child_in, selectors.EVENT_WRITE, "child_in")
        elif not self.to_child and registered:
            self.selector.unregister(self.child_in)

    def _close_child_in(self) -> None:
        if self.child_in < 0:
            return
        if self.child_in in self.selector.get_map():
            self.selector.unregister(self.child_in)
        assert self.child.stdin is not None
        try:
            self.child.stdin.close()
        except OSError:
            pass
        self.child_in = -1

    def _write_child(self) -> None:
        try:
            written = os.write(self.child_in, self.to_child)
        except BlockingIOError:
            return
        except OSError:
            # The CLI stopped reading; it is exiting, and its exit is what ends this.
            self.to_child.clear()
            self._close_child_in()
            return
        del self.to_child[:written]
        self._child_in_events()

    def _read_child(self) -> bool:
        """Copy what the CLI printed to the log and the client; False at its end."""
        try:
            data = os.read(self.child_out, CHUNK)
        except BlockingIOError:
            return True
        if not data:
            return False
        view = memoryview(data)
        while view:
            view = view[os.write(self.log_fd, view) :]
        # A client still catching up reads this from the log when it gets there.
        if self.client is not None and self.attached and self.replay_at is None:
            self.to_client += data
            self._client_events()
        return True

    # --- the loop ------------------------------------------------------------

    def run(self) -> int:
        while True:
            self._ensure_socket()
            for key, events in self.selector.select(SOCKET_CHECK_S):
                what = key.data
                if what == "accept":
                    self._accept()
                elif what == "child":
                    if not self._read_child():
                        return self._finish()
                elif what == "child_in":
                    self._write_child()
                elif what == "client" and self.client is not None:
                    if events & selectors.EVENT_READ:
                        self._read_client()
                    if self.client is not None and events & selectors.EVENT_WRITE:
                        self._write_client()

    def _flush_until(self, data: bytes, deadline: float) -> None:
        """Send ``data`` to the client, or raise ``TimeoutError`` once ``deadline`` passes."""
        assert self.client is not None
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the client stopped reading")
            self.client.settimeout(left)
            view = view[self.client.send(view) :]

    def _finish(self) -> int:
        code = self.child.wait()
        os.fsync(self.log_fd)
        os.close(self.log_fd)
        tmp = f"{self.exit_path}.tmp"
        with open(tmp, "w") as handle:
            handle.write(f"{code}\n")
        os.replace(tmp, self.exit_path)
        # Hand the attached client the rest, then end its stream: EOF is how it learns.
        # Bounded: a stalled client must not keep the holder alive. Whatever it misses is
        # in the log, which the runtime reads up from its own position once it sees EOF.
        if self.client is not None and self.attached:
            deadline = time.monotonic() + FINAL_FLUSH_S
            try:
                self._flush_until(bytes(self.to_client), deadline)
                if self.replay_at is not None:
                    with open(self.log_path, "rb") as log:
                        log.seek(self.replay_at)
                        while chunk := log.read(CHUNK):
                            self._flush_until(chunk, deadline)
            except OSError:
                pass
        self._drop_client()
        self.listener.close()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="holder")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--exit", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command to hold")
    holder = Holder(args.socket, args.log, args.exit, command)
    # The runtime stops a chat with a signal to the whole process group; the CLI gets it
    # too, and the holder stays to pass on its last words and its exit code. Only after
    # the CLI has started: an ignored signal is inherited across exec, and the CLI must
    # still die of it.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Tell the runtime where to reach it, then let go of the pipe the runtime holds.
    sys.stdout.write(
        json.dumps({"holder": PROTOCOL, "pid": os.getpid(), "child": holder.child.pid}) + "\n"
    )
    sys.stdout.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    return holder.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
