"""Per-worker memory caps (#1546).

One deck-owned worker that runs away takes the whole host with it. Workers are
spawned as plain children of the persistent runtime, so their memory *is* the
runtime's memory: when a Claude worker reached 25 GB on 2026-09-05 the kernel
raised a **global** OOM, killed the runtime's cgroup and every owned chat on the
host died with it.

The fix is to give each worker a cgroup of its own. A transient
``systemd-run --user --scope`` does that with no unit files and no privileges:
systemd creates the scope and then ``exec``s the command in the *same* process,
so the pid stays the one we hold, the stdin/stdout pipes the stream-json worker
protocol needs are inherited untouched, and the existing ``os.killpg`` teardown
still addresses the right process group. Only the accounting moves.

A scope that exits cleanly garbage-collects itself. A scope whose cgroup hit
``MemoryMax`` is left behind in ``failed`` state with ``Result=oom-kill``, which
is what lets us say *why* a worker died rather than guess it from a SIGKILL that
an ordinary ``kill -9`` produces just as well. Reading it costs one
``systemctl show`` per worker death, and the failed unit is cleared afterwards.

Where systemd is not usable — CI, a container, any host with no user manager —
``available()`` says so once and every spawn falls back to the plain, unwrapped
call. An uncapped worker is exactly today's behaviour, which is the right
failure direction: a missing cap must never stop a chat from starting.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import shutil
import uuid

log = logging.getLogger(__name__)

# Generous but bounded. A healthy worker on this host rests at 200-400 MB, so
# this is ~20x normal and still far under the 25 GB that took the host down.
DEFAULT_MEMORY_MAX = "8G"

# systemd's own word for "this cgroup went over MemoryMax", reported on the
# transient scope after the kernel killed what was inside it.
RESULT_OOM_KILL = "oom-kill"

# The deck's own token for the same fact, carried on the worker record and out
# through the worker snapshot so the chat can explain itself.
EXIT_MEMORY_CAP = "memory_cap"

_PROBE_TIMEOUT_S = 10.0
_QUERY_TIMEOUT_S = 5.0
_UNIT_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")
_counter = itertools.count()
# A scope can outlive the runtime that named it: a process group a worker's
# command started survives the worker's own kill and keeps the scope loaded.
# The counter restarts with every runtime, so without this a fresh runtime
# could pick a name still held, and systemd-run refuses at once (#3079).
_INCARNATION = "r" + uuid.uuid4().hex[:8]

# Resolved once per runtime process, keyed by the cap that was probed: an
# unparseable MemoryMax fails the probe exactly like a missing systemd does, so
# a typo in the config degrades to today's behaviour instead of refusing to
# spawn anything.
_probed: dict[str, bool] = {}


def scope_unit(account: str, key: str) -> str:
    """A unique transient unit name for one worker process."""
    stem = _UNIT_SAFE.sub("-", f"{account}-{key}").strip("-")[:120]
    return f"agentdeck-worker-{stem}-{_INCARNATION}-{next(_counter)}.scope"


def scope_prefix(unit: str, memory_max: str) -> list[str]:
    """The argv prefix that runs a command inside its own capped scope.

    ``MemorySwapMax=0`` goes with the cap deliberately. Without it a runaway
    worker buys itself headroom in swap first — the incident unit reported a
    3.4 GB swap peak — which drags the whole host before the cap ever fires.
    """
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={memory_max}",
        "-p",
        "MemorySwapMax=0",
        "--",
    ]


async def available(memory_max: str) -> bool:
    """Whether this host can actually run a capped scope with ``memory_max``."""
    cached = _probed.get(memory_max)
    if cached is not None:
        return cached
    ok = await _probe(memory_max)
    _probed[memory_max] = ok
    if not ok:
        log.warning(
            "worker memory cap unavailable (MemoryMax=%s); spawning workers uncapped",
            memory_max,
        )
    return ok


async def _probe(memory_max: str) -> bool:
    if shutil.which("systemd-run") is None:
        return False
    unit = scope_unit("probe", "memory-cap")
    try:
        process = await asyncio.create_subprocess_exec(
            *scope_prefix(unit, memory_max),
            "true",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(process.wait(), timeout=_PROBE_TIMEOUT_S) == 0
    except (OSError, TimeoutError, asyncio.CancelledError):
        return False
    except Exception:  # noqa: BLE001 -- a probe must never break worker startup
        log.exception("worker memory cap probe failed")
        return False


async def scope_result(unit: str) -> str:
    """systemd's verdict on a finished scope, or "" when it left nothing behind.

    A scope that ended cleanly is already collected, so "" is the ordinary
    answer and means "not a cap kill".
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=Result",
            "--value",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=_QUERY_TIMEOUT_S)
    except (OSError, TimeoutError):
        return ""
    except Exception:  # noqa: BLE001 -- reading a verdict must never break teardown
        log.exception("could not read scope result for %s", unit)
        return ""
    return out.decode(errors="replace").strip()


async def clear_scope(unit: str) -> None:
    """Drop a failed scope so it stops accumulating in ``systemctl --failed``."""
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            "reset-failed",
            unit,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(process.wait(), timeout=_QUERY_TIMEOUT_S)
    except (OSError, TimeoutError):
        return
    except Exception:  # noqa: BLE001
        log.exception("could not clear scope %s", unit)
