"""Own the web process's delivery shutdown choreography (#1889, #1911).

The web process deliberately gives ordinary HTTP/SSE connections only a short graceful-stop
window, while keyed worker deliveries may already have crossed an at-most-once ledger fence.
Those deliveries need a different lifecycle: stop admitting new writes as soon as uvicorn sees
the signal, let already-started provider writes settle, and only then close the runtime HTTP
channel they are using.

That temporal protocol used to be spread across the uvicorn adapter, the delivery route, and the
FastAPI lifespan.  ``DeliveryShutdownCoordinator`` owns it now.  The outer layers only report a
stop, protect a delivery, or hand over the runtime-channel closer; they do not choose the order.
The persistent runtime remains a separate process, per ADR-0001 — this coordinator governs only
the restartable web process's client channel to it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

log = logging.getLogger(__name__)

# Long enough for a provider write and its confirmation to land, short enough to stay well
# inside systemd's stop timeout for the web unit. A delivery with no answer by now is not
# going to produce one, and waiting further only delays the deploy.
DELIVERY_DRAIN_TIMEOUT_S = 30.0

ShutdownPhase = Literal["running", "stopping", "draining", "closed"]


class DeliveryShutdownCoordinator:
    """Single owner of the delivery side of web-process shutdown.

    ``signal_shutdown`` closes admission immediately. ``protect`` keeps already-started writes
    alive if their request task is cancelled. ``drain_then_close`` then waits for those writes
    before closing the runtime HTTP channel.  Keeping the phase and in-flight register together
    makes the ordering an invariant of this object instead of a convention shared by callers.
    """

    def __init__(self) -> None:
        self._phase: ShutdownPhase = "running"
        self._in_flight: dict[asyncio.Task, str] = {}
        self._os_shutdown_signalled = False

    @property
    def phase(self) -> ShutdownPhase:
        return self._phase

    def _begin_shutdown(self) -> None:
        if self._phase == "running":
            self._phase = "stopping"

    def signal_shutdown(self) -> None:
        """Record that uvicorn received a process stop signal.

        A real OS-triggered stop is terminal for this process. Explicit lifespan teardown in
        tests can reuse the process afterwards, so that case uses the same ordering without
        setting this terminal bit.
        """
        self._os_shutdown_signalled = True
        self._begin_shutdown()

    def is_shutting_down(self) -> bool:
        """True once a stop has begun, so a new write must be refused rather than started."""
        return self._phase != "running"

    def reset(self) -> None:
        """Forget process-local shutdown state. For tests and re-created apps in-process."""
        self._phase = "running"
        self._os_shutdown_signalled = False
        self._in_flight.clear()

    async def protect(self, delivery_id: str, work: Awaitable[Any]) -> Any:
        """Run one delivery where a cancelled caller cannot leave its ledger row unproven.

        Admission is intentionally decided by the route before it builds ledger state. Once work
        reaches this method it owns a provider write and therefore runs as its own task. The task
        is awaited through a shield, stays registered until it settles, and is what shutdown waits
        for before the runtime channel can close.
        """
        task = asyncio.ensure_future(work)
        self._in_flight[task] = delivery_id
        task.add_done_callback(lambda finished: self._in_flight.pop(finished, None))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                log.error(
                    "shutdown cancelled the caller of context delivery %s; the provider write "
                    "continues and its ledger row will be settled before this process exits",
                    delivery_id,
                )
            raise

    async def drain(self, timeout: float = DELIVERY_DRAIN_TIMEOUT_S) -> None:
        """Wait for protected deliveries and name any that never finish."""
        self._begin_shutdown()
        if self._phase != "closed":
            self._phase = "draining"

        pending = set(self._in_flight)
        if not pending:
            return
        log.info(
            "shutdown: waiting up to %.0fs for %d in-flight context deliver(y/ies)",
            timeout,
            len(pending),
        )
        _done, still_running = await asyncio.wait(pending, timeout=timeout)
        for task in still_running:
            log.error(
                "context delivery %s did not finish before shutdown; its ledger row may be left "
                "uncertain with the provider write unproven. Resolve it by hand with "
                "`agentdeck resolve-context-delivery`",
                self._in_flight.get(task, "unknown"),
            )
            task.cancel()

    async def drain_then_close(
        self,
        close_runtime: Callable[[], Awaitable[Any]],
        *,
        timeout: float = DELIVERY_DRAIN_TIMEOUT_S,
    ) -> None:
        """Finish the web side of delivery shutdown in the only safe order.

        Lifespan teardown can happen without uvicorn's OS-signal path (tests and explicit app
        shutdown do this), so this method also closes admission before draining. The runtime
        closer is invoked only after every protected write has either settled or hit the bounded
        drain timeout.

        A true uvicorn signal leaves the process terminally closed. An explicit lifespan teardown
        has no such process-level signal, so after its runtime client is closed the module-level
        coordinator returns to ``running`` and a later app instance in the same process can admit
        deliveries again.
        """
        self._begin_shutdown()
        await self.drain(timeout=timeout)
        await close_runtime()
        self._phase = "closed" if self._os_shutdown_signalled else "running"


_coordinator = DeliveryShutdownCoordinator()


def signal_shutdown() -> None:
    """Compatibility seam for uvicorn's signal adapter."""
    _coordinator.signal_shutdown()


def is_shutting_down() -> bool:
    """Compatibility seam for delivery admission."""
    return _coordinator.is_shutting_down()


def reset() -> None:
    """Reset process-local coordinator state."""
    _coordinator.reset()


async def protect(delivery_id: str, work: Awaitable[Any]) -> Any:
    """Protect one already-admitted delivery from caller cancellation."""
    return await _coordinator.protect(delivery_id, work)


async def drain(timeout: float = DELIVERY_DRAIN_TIMEOUT_S) -> None:
    """Compatibility seam for tests and callers that only need the drain step."""
    await _coordinator.drain(timeout=timeout)


async def drain_then_close(
    close_runtime: Callable[[], Awaitable[Any]],
    *,
    timeout: float = DELIVERY_DRAIN_TIMEOUT_S,
) -> None:
    """Drain protected deliveries and then close the runtime client channel."""
    await _coordinator.drain_then_close(close_runtime, timeout=timeout)


def phase() -> ShutdownPhase:
    """Expose the coordinator phase for diagnostics and invariant tests."""
    return _coordinator.phase
