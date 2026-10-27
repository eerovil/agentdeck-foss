"""Small JSON stores that more than one process may write (#3070).

A web hot reload runs two workers side by side for minutes: the new one loads its stores
when it starts, and the old one keeps serving until the new one's first scans finish. A
store that loads its file once and then writes it whole loses whatever the other worker
wrote in that window. These helpers let such a store notice a foreign write and re-read
before it answers, and hold a lock across its read-modify-write so two writers cannot
overwrite each other.

A store that sleeps on its own data (a deadline, a retry) is not asked anything while
it sleeps, so it would not notice the other worker's write. `refresh_shared_files` runs
each such store's ``on_foreign_write`` hook; the web worker calls it when the supervisor
says the old worker has drained, which is when the last foreign write has landed. State kept
in the database but loaded into memory once at start has the same gap, so it registers an
`add_drained_hook` that `refresh_shared_files` runs too (#3244).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import threading
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

#: What identifies one version of a file: every write replaces it with a new inode.
Stamp = tuple[int, int, int] | None


def stamp(path: Path) -> Stamp:
    """``(inode, mtime_ns, size)`` of ``path``, or ``None`` when it does not exist."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


class SharedFile:
    """One JSON file's version as last read or written by this process.

    A store calls `stale` before trusting what it holds, and does each whole
    read-modify-write inside `locked`. The lock is re-entrant within one store, so a
    mutation may call the store's own readers.
    """

    def __init__(
        self,
        path: Path,
        *,
        prefix: str,
        on_foreign_write: Callable[[], object] | None = None,
    ) -> None:
        self.path = path
        self._prefix = prefix
        self._on_foreign_write = on_foreign_write
        self._seen: Stamp = None
        self._loaded = False
        self._depth = 0
        self._thread_lock = threading.RLock()
        if on_foreign_write is not None:
            _WATCHED.add(self)

    def stale(self) -> bool:
        """Whether the file is not the version this process last read or wrote."""
        return not self._loaded or stamp(self.path) != self._seen

    def read(self) -> object:
        """The file's parsed JSON, ``None`` when missing; raises OSError/ValueError."""
        # Stamp first: a write landing between the stat and the read leaves an older stamp,
        # so the next look reads again rather than trusting a version it never saw.
        self._seen = stamp(self.path)
        self._loaded = True
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def write(self, data: object, *, indent: int | None = None) -> None:
        """Replace the file atomically: a crash leaves the old version or the new one."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self._prefix, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=indent)
            os.replace(temp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        self._seen = stamp(self.path)
        self._loaded = True

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the file's lock, shared with every other process writing it."""
        with self._thread_lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


_WATCHED: weakref.WeakSet[SharedFile] = weakref.WeakSet()
_DRAINED_HOOKS: list[Callable[[], object]] = []


def add_drained_hook(hook: Callable[[], object]) -> Callable[[], None]:
    """Run ``hook`` each time the old worker has drained; returns its removal."""
    _DRAINED_HOOKS.append(hook)

    def remove() -> None:
        with contextlib.suppress(ValueError):
            _DRAINED_HOOKS.remove(hook)

    return remove


def refresh_shared_files() -> int:
    """Run the hook of every watched file another process wrote, then every drained hook;
    how many file hooks ran."""
    ran = 0
    for shared in list(_WATCHED):
        # A file this process never read yet is read on first use; nothing to tell.
        if not shared._loaded or not shared.stale():  # noqa: SLF001 - same module
            continue
        try:
            shared._on_foreign_write()  # noqa: SLF001 - same module
            ran += 1
        except Exception:  # noqa: BLE001 - one store must not keep the others stale
            log.exception("shared file %s: refresh failed", shared.path)
    for hook in list(_DRAINED_HOOKS):
        try:
            hook()
        except Exception:  # noqa: BLE001 - one hook must not keep the others stale
            log.exception("drained hook %r failed", hook)
    return ran
