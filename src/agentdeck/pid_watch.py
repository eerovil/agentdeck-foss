"""Be told when a process exits, instead of asking whether it is still there (#2865).

A process exit is not a filesystem event, which is why the liveness sweep and the
delegation takeover used to look every few seconds. Linux hands out a *pidfd* for a
process: it becomes readable when the process exits, so the event loop can wait on it
like a socket. A pidfd names the process, not the number, so a pid the kernel recycles
afterwards cannot be mistaken for the one being watched.

:class:`ExitWatches` keeps one pidfd per process of interest and calls back once per
exit. Where ``pidfd_open`` is missing (an old kernel, another platform) or refused, the
pid is reported as *unwatched*, and the caller keeps its old cadence for it. A Python
built without ``os.pidfd_open`` on Linux — uv's standalone CPython 3.12 is one — still
gets a pidfd from the system call itself (#3056).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import logging
import os
import sys
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)

# The same number on every Linux architecture but alpha, since 5.3.
_SYS_PIDFD_OPEN = 434
_NATIVE_PIDFD_OPEN = getattr(os, "pidfd_open", None)


def _pidfd_open(pid: int) -> int:
    """``os.pidfd_open``, or the system call itself where Python left it out.

    Raises like ``os.pidfd_open``: ``ProcessLookupError`` for a process already gone,
    ``OSError`` for a refusal or an old kernel, ``AttributeError`` off Linux.
    """
    if _NATIVE_PIDFD_OPEN is not None:
        return _NATIVE_PIDFD_OPEN(pid)
    if not sys.platform.startswith("linux"):
        raise AttributeError("pidfd_open is Linux-only")
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(_SYS_PIDFD_OPEN, ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        code = ctypes.get_errno() or errno.ENOSYS
        raise OSError(code, os.strerror(code))  # ESRCH comes back a ProcessLookupError
    return fd


class ExitWatches:
    """One pidfd per watched pid; ``on_exit(pid)`` once when that process ends."""

    def __init__(self, on_exit: Callable[[int], None]) -> None:
        self._on_exit = on_exit
        self._fds: dict[int, int] = {}
        self.unwatched: frozenset[int] = frozenset()

    @property
    def pids(self) -> frozenset[int]:
        return frozenset(self._fds)

    def watch(self, pids: Iterable[int]) -> None:
        """Watch exactly these pids: start the new ones, forget the rest."""
        wanted = {pid for pid in pids if isinstance(pid, int) and pid > 0}
        for pid in set(self._fds) - wanted:
            self._forget(pid)
        loop = asyncio.get_running_loop()
        unwatched: set[int] = set()
        for pid in wanted - set(self._fds):
            try:
                fd = _pidfd_open(pid)
            except ProcessLookupError:
                # Gone before it could be watched: that is an exit too.
                loop.call_soon(self._on_exit, pid)
                continue
            except (AttributeError, OSError) as exc:
                log.debug("cannot watch process %s for its exit: %s", pid, exc)
                unwatched.add(pid)
                continue
            self._fds[pid] = fd
            loop.add_reader(fd, self._exited, pid)
        self.unwatched = frozenset(unwatched)

    def _exited(self, pid: int) -> None:
        self._forget(pid)
        self._on_exit(pid)

    def _forget(self, pid: int) -> None:
        fd = self._fds.pop(pid, None)
        if fd is None:
            return
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    def close(self) -> None:
        for pid in list(self._fds):
            self._forget(pid)
        self.unwatched = frozenset()
