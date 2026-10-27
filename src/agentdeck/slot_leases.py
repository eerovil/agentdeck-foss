"""Host-wide heavy slots, leased over HTTP to clients the deck did not spawn.

The heavy queue of #1287 has two enforcement points and both work the same way:
they **classify a command string**. ``scripts/heavy_command_hook.py`` reads a
Claude Code ``Bash`` call before the shell runs it, and ``agentdeck.heavy_shim``
shadows a Codex worker's PATH. Between them they cover the agents the deck
spawns, because the deck controls the shell those agents reach.

A client the deck did not spawn has neither. The case this was written for is a
systemd **user service** on this host that runs optical music recognition over
sheet music — minutes of CPU a page, several pages a song, on the same four
cores as everything else. It has no shim on its PATH and no ``PreToolUse`` hook,
and it cannot hand the command to ``agentdeck job`` either: it needs the output
file back and reports its own progress while the work runs, so giving the
command to another process is the wrong shape.

The tempting fix is to teach the classifier about that service's tools. This is
the other one: the client asks for a slot, does the work itself, and gives the
slot back. The deck says only *it is your turn*. Nothing here learns what ran,
so nothing here has to be updated when the next heavy thing arrives.

The slot is the same ``flock``ed file ``jobs.py`` and the pytest backstop take
(``host_slots.heavy_slot_paths``), so a lease and a queued suite compete for one
pool. That is the point — they compete for the same cores.

**What a lease has to add over a bare flock is an expiry.** The kernel drops a
lock when its holder dies, so a killed run cannot strand a slot; an HTTP client
that vanishes leaves nothing behind to notice, and the host has only one heavy
slot to strand. So a lease lives for ``ttl_s``, must be renewed to survive, and
the reaper takes back one nobody is renewing.

A lease is deliberately **not durable**. A runtime restart closes every fd it
held, and the next heartbeat answers "unknown lease" rather than pretending the
slot is still held — the same honesty ``jobs.load`` shows when it turns a job
left ``running`` into ``interrupted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .host_slots import MemoryGate, heavy_slot_paths, try_take_slot

log = logging.getLogger(__name__)

# Long enough that a caller heartbeating on a sane interval never trips it,
# short enough that a crashed client does not hold the host's only heavy slot
# for long. A caller doing minutes of work renews; it does not ask for minutes.
DEFAULT_TTL_S = 120.0
# A ceiling on what a caller may ask for, so one client cannot opt out of the
# reaper by requesting a day.
MAX_TTL_S = 900.0
# How long an acquire may block. The caller is on the other end of an HTTP
# request, so waiting forever is not available: past this it is told plainly
# that nothing was free and retries.
MAX_WAIT_S = 60.0
_ACQUIRE_POLL_S = 0.25
_REAP_POLL_S = 1.0
_MAX_LABEL_CHARS = 200


class LeaseError(ValueError):
    """A request the pool cannot accept."""


@dataclass(frozen=True)
class Lease:
    """One held heavy slot. The fd itself is kept by the pool, not here."""

    id: str
    label: str
    acquired_at: float
    expires_at: float
    ttl_s: float

    def to_dict(self, now: float) -> dict[str, Any]:
        """The wire form, in durations rather than timestamps.

        The pool's clock is ``time.monotonic`` — the right clock for an expiry,
        and a meaningless number to a caller in another process. So a caller is
        told how long it has left, never when in some epoch that is.
        """
        return {
            "lease": self.id,
            "label": self.label,
            "ttl_s": self.ttl_s,
            "expires_in_s": max(0.0, self.expires_at - now),
            "held_for_s": max(0.0, now - self.acquired_at),
        }


class SlotLeases:
    """The host's heavy slots, held on behalf of HTTP callers."""

    def __init__(
        self,
        *,
        slot_paths: list[Path] | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        max_ttl_s: float = MAX_TTL_S,
        max_wait_s: float = MAX_WAIT_S,
        acquire_poll_s: float = _ACQUIRE_POLL_S,
        reap_poll_s: float = _REAP_POLL_S,
        clock: Callable[[], float] = time.monotonic,
        memory_gate: MemoryGate | None = None,
    ) -> None:
        self._slot_paths = slot_paths
        # A lease competes with a queued suite for memory as well as cores (#3614), so it
        # waits, as a local job does, while the host is short of it, through the gate the
        # job queue uses; the last acquire that gave up for that reason says so.
        self._memory = memory_gate or MemoryGate()
        self.short_of_memory = False
        self._ttl_s = ttl_s
        self._max_ttl_s = max_ttl_s
        self._max_wait_s = max_wait_s
        self._acquire_poll_s = acquire_poll_s
        self._reap_poll_s = reap_poll_s
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._fds: dict[str, int] = {}
        self._reaper: asyncio.Task | None = None
        self.on_change: Callable[[], None] = lambda: None

    def _slots(self) -> list[Path]:
        return heavy_slot_paths() if self._slot_paths is None else self._slot_paths

    def slot_count(self) -> int:
        """How many slots this pool is competing for, configured size included.

        Read off the paths rather than from ``heavy_slot_count()`` so it cannot
        disagree with the pool actually being locked when the runtime was given
        an explicit size.
        """
        return len(self._slots())

    # ---- requests ------------------------------------------------------

    def _requested_ttl(self, requested: float | None) -> float:
        if requested is None:
            return self._ttl_s
        try:
            value = float(requested)
        except (TypeError, ValueError):
            raise LeaseError("ttl_s must be a number") from None
        if value <= 0:
            raise LeaseError("ttl_s must be positive")
        return min(value, self._max_ttl_s)

    async def acquire(
        self,
        *,
        label: str = "",
        ttl_s: float | None = None,
        wait_s: float = 0.0,
    ) -> Lease | None:
        """Take a slot, waiting up to ``wait_s`` for one. None if none came free.

        None is an ordinary answer, not an error: the caller backs off and asks
        again. Holding the request open indefinitely would turn a busy host into
        a pile of stuck sockets.
        """
        ttl = self._requested_ttl(ttl_s)
        try:
            wait = float(wait_s or 0.0)
        except (TypeError, ValueError):
            raise LeaseError("wait_s must be a number") from None
        deadline = self._clock() + max(0.0, min(wait, self._max_wait_s))
        while True:
            # Reap first: the slot a dead client is sitting on is exactly the
            # one this caller is waiting for.
            self.reap()
            memory_ok = self._memory.ok()
            fd = try_take_slot(self._slots()) if memory_ok else None
            self.short_of_memory = not memory_ok
            if fd is not None:
                self._memory.admit()
                now = self._clock()
                lease = Lease(
                    id=uuid.uuid4().hex[:12],
                    label=str(label or "")[:_MAX_LABEL_CHARS],
                    acquired_at=now,
                    expires_at=now + ttl,
                    ttl_s=ttl,
                )
                self._leases[lease.id] = lease
                self._fds[lease.id] = fd
                log.info("slot lease %s taken (%s)", lease.id, lease.label or "unlabelled")
                return lease
            if self._clock() >= deadline:
                return None
            await asyncio.sleep(self._acquire_poll_s)

    def heartbeat(self, lease_id: str) -> Lease | None:
        """Push the expiry out by a full ttl. None if it is not held any more."""
        self.reap()
        lease = self._leases.get(lease_id)
        if lease is None:
            return None
        renewed = replace(lease, expires_at=self._clock() + lease.ttl_s)
        self._leases[lease_id] = renewed
        return renewed

    def release(self, lease_id: str) -> bool:
        """Give a slot back. False if it was already gone.

        The drop happens before the reap, so releasing a lease that has just
        expired still reports as a release rather than as an unknown id — the
        caller did the right thing and should not be told otherwise.
        """
        dropped = self._drop(lease_id, "released")
        self.reap()
        return dropped

    # ---- state ---------------------------------------------------------

    def get(self, lease_id: str) -> Lease | None:
        return self._leases.get(lease_id)

    def list(self) -> list[Lease]:
        return sorted(self._leases.values(), key=lambda lease: lease.acquired_at)

    def held_slot_inodes(self) -> dict[int, tuple[str, float]]:
        """Slot inode -> ``(label, acquired-at)`` for every lease held (#1391).

        The acquired-at is converted out of this pool's monotonic clock into
        wall time, because the only consumer is a panel that renders an age
        against ``time.time()``. A lease whose fd has gone is skipped rather
        than guessed at.
        """
        offset = time.time() - self._clock()
        held: dict[int, tuple[str, float]] = {}
        for lease_id, fd in self._fds.items():
            lease = self._leases.get(lease_id)
            if lease is None:
                continue
            try:
                inode = os.fstat(fd).st_ino
            except OSError:  # pragma: no cover - the pool owns this fd
                continue
            held[inode] = (lease.label or "unlabelled", lease.acquired_at + offset)
        return held

    def payload(self, lease: Lease) -> dict[str, Any]:
        return lease.to_dict(self._clock())

    def payloads(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [lease.to_dict(now) for lease in self.list()]

    # ---- reaping -------------------------------------------------------

    def reap(self) -> list[str]:
        """Take back every lease nobody is renewing. Returns what was taken."""
        now = self._clock()
        expired = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at <= now
        ]
        for lease_id in expired:
            lease = self._leases.get(lease_id)
            log.warning(
                "slot lease %s (%s) expired without a heartbeat; taking the slot back",
                lease_id,
                (lease.label if lease else "") or "unlabelled",
            )
            self._drop(lease_id, "expired")
        return expired

    def _drop(self, lease_id: str, why: str) -> bool:
        lease = self._leases.pop(lease_id, None)
        fd = self._fds.pop(lease_id, None)
        if fd is not None:
            # flock is released by the close; closing is the whole teardown.
            with contextlib.suppress(OSError):
                os.close(fd)
        if lease is not None:
            log.info("slot lease %s %s", lease_id, why)
            # The reaper drops a lease with no request in flight, so the change feed
            # has to be told here rather than after a control request (#2865).
            self.on_change()
        return lease is not None

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="slot-lease-reaper")

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        for lease_id in list(self._leases):
            self._drop(lease_id, "dropped: the runtime is stopping")

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._reap_poll_s)
            try:
                self.reap()
            except Exception:  # pragma: no cover - the reaper must not die
                log.exception("slot leases: reaping failed")
