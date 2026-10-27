"""Let an unobserved web instance stop itself until the next request wakes it.

Socket activation only decides how the web unit *starts*. Without this, the
first request starts it for good: the supervisor keeps a worker alive until
SIGTERM, so a canary nobody is looking at costs the same as one under test
(#1330). The watchdog closes that loop — the worker exits with
``IDLE_EXIT_CODE``, the supervisor exits cleanly rather than respawning, and the
systemd socket starts the pair again on the next request.

Idle here means two things at once, because either alone is unsafe:

* nothing is being served — no open connection and no request in the window. An
  attached board holds an SSE stream open, so a watched deck never looks idle.
* nothing would be lost — the runtime reports no active turn, no live owned
  worker process and no unfinished job. The runtime unit follows the web unit
  down through ``StopWhenUnneeded=``, which would kill those processes.

It is off unless ``AGENTDECK_WEB_IDLE_TIMEOUT_S`` is set, so the live deck, which
is observed around the clock and must answer instantly, is unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

_LOG = logging.getLogger(__name__)

#: Distinguished worker exit status: "I stopped because nothing needed me."
IDLE_EXIT_CODE = 88

_TIMEOUT_ENV = "AGENTDECK_WEB_IDLE_TIMEOUT_S"
_DEFAULT_POLL_INTERVAL_S = 15.0
_RUNTIME_PROBE_TIMEOUT_S = 5.0
_BUSY_KEYS = ("live_worker_processes", "unfinished_jobs")


def idle_timeout_s(environ: dict[str, str] | None = None) -> float:
    """Configured idle window in seconds; 0 (the default) disables the watchdog."""

    raw = (environ if environ is not None else os.environ).get(_TIMEOUT_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        timeout = float(raw)
    except ValueError:
        _LOG.warning("ignoring non-numeric %s=%r", _TIMEOUT_ENV, raw)
        return 0.0
    return timeout if timeout > 0 else 0.0


@dataclass(frozen=True)
class WebActivity:
    """What this worker has served: a monotonic counter plus live connections."""

    total_requests: int
    open_connections: int


async def runtime_is_busy(client) -> bool:
    """True when the runtime holds work, or its reachable state is uncertain."""

    if client is None:
        return False
    try:
        response = await client.get("/activity", timeout=_RUNTIME_PROBE_TIMEOUT_S)
    except httpx.ConnectError:
        # No runtime process/socket means there is nothing for this web worker's
        # systemd dependency edge to stop. Keep socket-activated canaries able to
        # stand down when the runtime was never started.
        return False
    except Exception:
        # A timeout/read failure is different: a runtime may be alive and busy,
        # but we failed to learn its state. Stopping here could kill its workers.
        _LOG.warning("idle watchdog could not read runtime activity; keeping web alive")
        return True
    try:
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _LOG.warning("idle watchdog received unreadable runtime activity; keeping web alive")
        return True
    if not isinstance(payload, dict) or not isinstance(payload.get("active"), bool):
        _LOG.warning("idle watchdog received malformed runtime activity; keeping web alive")
        return True
    if payload["active"]:
        return True
    for key in _BUSY_KEYS:
        value = payload.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _LOG.warning("idle watchdog received malformed runtime activity; keeping web alive")
            return True
        if value:
            return True
    return False


class IdleWatchdog:
    """Stop the worker once it has served nothing for the whole idle window."""

    def __init__(
        self,
        *,
        timeout_s: float,
        web_activity: Callable[[], WebActivity],
        runtime_busy: Callable[[], Awaitable[bool]],
        on_idle: Callable[[], None],
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.timeout_s = timeout_s
        self.poll_interval_s = min(poll_interval_s, max(timeout_s / 4, 0.05))
        self._web_activity = web_activity
        self._runtime_busy = runtime_busy
        self._on_idle = on_idle
        self._clock = clock
        self._sleep = sleep

    async def run(self) -> None:
        served = self._web_activity().total_requests
        last_active = self._clock()
        while True:
            await self._sleep(self.poll_interval_s)
            sample = self._web_activity()
            if sample.open_connections or sample.total_requests != served:
                served = sample.total_requests
                last_active = self._clock()
                continue
            if self._clock() - last_active < self.timeout_s:
                continue
            if await self._runtime_busy():
                # Something is still running behind the deck. Do not restart the
                # window from zero work — just wait out another full one.
                last_active = self._clock()
                continue
            # That probe is a round trip over the runtime socket. A request or an
            # SSE connection can land while it is in flight, so the decision is
            # only safe against a sample taken after it returns.
            arrived = self._web_activity()
            if arrived.open_connections or arrived.total_requests != served:
                served = arrived.total_requests
                last_active = self._clock()
                continue
            _LOG.info(
                "nothing served for %.0fs and nothing running; stopping until the "
                "next request",
                self.timeout_s,
            )
            self._on_idle()
            return
