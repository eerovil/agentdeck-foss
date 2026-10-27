"""What stops a worker before it eats the poller's reserve.

Caching alone bounds nothing.  It collapses *repeat* reads, but a worker asking
many *distinct* questions still spends straight down through the floor
``poller_engine/github.py`` holds live phases behind — which is the inversion
#814 is about: the poller throttling itself to leave room for polling it
forbids.

So there are two gates, and they answer different failures:

* A **shared floor** on GitHub's own remaining quota, set strictly above the
  poller's ``core_reserve``/``graphql_reserve``.  A worker is refused while the
  poller still has its whole reserve intact.  The ordering is the point: the
  poller must be the last thing standing, not the first thing throttled.
* A **per-session token bucket**, because one runaway worker can drain
  everything down to a shared floor by itself without ever crossing it.

Both live in the same SQLite file as the metadata cache.  A shim invocation is
a short-lived process and cannot hold this in memory the way ``GitHubAdapter``
does across one wake.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..poller_engine import rate_limits as shared_limits

log = logging.getLogger(__name__)

# How much quota a worker must leave untouched.  Deliberately above the
# poller's own DEFAULT_CORE_RESERVE (500) and DEFAULT_GRAPHQL_RESERVE (200):
# the reserve exists so the poller can still run a wake, and a worker spending
# inside it is exactly the bug.
DEFAULT_CORE_FLOOR = 1000
DEFAULT_GRAPHQL_FLOOR = 500

# Live fetches one session may make per window.  Cache hits are free and never
# counted, so this bounds only genuinely new questions.  A worker doing real
# work asks a few dozen; a worker in a loop asks thousands.
DEFAULT_BUCKET_CAPACITY = 120
DEFAULT_BUCKET_WINDOW_S = 3600.0

# How long the shared view of remaining quota may be reused. The probes are
# real requests, so this bounds both latency and their own small quota cost.
LIMITS_TTL_S = 30.0
ACCOUNTING_RETENTION_S = 7 * 24 * 3600.0
CONSUMER_ENV = "AGENTDECK_GH_CONSUMER"


def _non_negative_int(name: str, fallback: int) -> int:
    """An override read from the environment; 0 is a legitimate "no floor"."""
    raw = os.environ.get(name)
    if raw is None:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        return fallback
    return value if value >= 0 else fallback


def core_floor() -> int:
    return _non_negative_int("AGENTDECK_GH_CORE_FLOOR", DEFAULT_CORE_FLOOR)


def graphql_floor() -> int:
    return _non_negative_int("AGENTDECK_GH_GRAPHQL_FLOOR", DEFAULT_GRAPHQL_FLOOR)


def bucket_capacity() -> int:
    return _non_negative_int("AGENTDECK_GH_FETCH_BUDGET", DEFAULT_BUCKET_CAPACITY)


@dataclass(frozen=True)
class RateLimits:
    """Remaining quota, or the admission that we do not know it.

    Both fields absent is *unknown*, never *exhausted*.  A worker whose
    rate-limit probe failed is not thereby forbidden to work — that would let one broken
    lookup halt every worker on the host — but it is also not licensed to
    ignore the floor, so an unknown reading allows the fetch and says so.
    """

    core: int | None = None
    graphql: int | None = None
    core_source: str = ""
    graphql_source: str = ""

    def below_floor(self) -> str:
        """Which floor this reading has fallen through, if any."""
        if self.core is not None and self.core < core_floor():
            source = f" ({self.core_source})" if self.core_source else ""
            return f"REST core quota is down to {self.core}{source}"
        if self.graphql is not None and self.graphql < graphql_floor():
            source = f" ({self.graphql_source})" if self.graphql_source else ""
            return f"GraphQL quota is down to {self.graphql}{source}"
        return ""


def parse_rate_limit_headers(*responses: str) -> RateLimits:
    """Read enforced quota from real GitHub response headers.

    GitHub's `/rate_limit` diagnostic can disagree with requests made by the
    same credential. The headers on an ordinary REST or GraphQL response are
    the admission decision GitHub actually enforced, so those are the facts
    used for worker and promotion floors.
    """
    values: dict[str, int | None] = {"core": None, "graphql": None}
    for response in responses:
        headers: dict[str, str] = {}
        for line in response.splitlines():
            match = re.match(r"(?i)^(x-ratelimit-[a-z-]+):\s*(.*?)\s*$", line)
            if match:
                headers[match.group(1).lower()] = match.group(2)
        resource = headers.get("x-ratelimit-resource")
        remaining = headers.get("x-ratelimit-remaining")
        if resource not in values or remaining is None:
            continue
        try:
            parsed = int(remaining)
        except ValueError:
            continue
        if parsed >= 0:
            values[resource] = parsed
    return RateLimits(
        core=values["core"],
        graphql=values["graphql"],
        core_source=shared_limits.HEADER_SOURCE if values["core"] is not None else "",
        graphql_source=(
            shared_limits.HEADER_SOURCE if values["graphql"] is not None else ""
        ),
    )


def parse_rate_limit(payload: object) -> RateLimits:
    """Read the explicitly untrusted ``/rate_limit`` cold-start estimate."""
    resources = payload.get("resources") if isinstance(payload, dict) else None
    if not isinstance(resources, dict):
        return RateLimits()
    values: dict[str, int | None] = {"core": None, "graphql": None}
    for name in values:
        resource = resources.get(name)
        remaining = resource.get("remaining") if isinstance(resource, dict) else None
        if isinstance(remaining, int) and not isinstance(remaining, bool):
            values[name] = remaining
    return RateLimits(
        core=values["core"],
        graphql=values["graphql"],
        core_source=shared_limits.ESTIMATE_SOURCE if values["core"] is not None else "",
        graphql_source=(
            shared_limits.ESTIMATE_SOURCE if values["graphql"] is not None else ""
        ),
    )


def from_shared(readings: dict[str, shared_limits.Reading]) -> RateLimits:
    """Translate the process-shared readings into the shim's floor model."""
    core = readings.get("core")
    graphql = readings.get("graphql")
    return RateLimits(
        core=core.remaining if core else None,
        graphql=graphql.remaining if graphql else None,
        core_source=core.source if core else "",
        graphql_source=graphql.source if graphql else "",
    )


class CallAccounting:
    """Durable hourly counts of real `gh` calls by named worker consumer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout = 2000")
        return connection

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gh_shim_calls_v1 (
                        consumer TEXT NOT NULL,
                        window_start INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        calls INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (consumer, window_start, kind)
                    )
                    """
                )
        except sqlite3.Error as exc:
            log.debug("gh shim call accounting init failed: %s", exc)

    def record(self, consumer: str, kind: str, *, now: float | None = None) -> None:
        """Count one call. Accounting is observational and must always fail open."""
        moment = time.time() if now is None else now
        window = int(moment // 3600 * 3600)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO gh_shim_calls_v1 (consumer, window_start, kind, calls)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(consumer, window_start, kind) DO UPDATE SET
                        calls = calls + 1
                    """,
                    (consumer, window, kind),
                )
                connection.execute(
                    "DELETE FROM gh_shim_calls_v1 WHERE window_start < ?",
                    (int(moment - ACCOUNTING_RETENTION_S),),
                )
        except sqlite3.Error as exc:
            log.debug("gh shim call accounting write failed: %s", exc)

    def totals(self, *, since: float) -> list[tuple[str, str, int]]:
        """Return consumer/kind totals for operator diagnosis."""
        first_window = int(since // 3600 * 3600)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT consumer, kind, SUM(calls)
                    FROM gh_shim_calls_v1
                    WHERE window_start >= ?
                    GROUP BY consumer, kind
                    ORDER BY consumer, kind
                    """,
                    (first_window,),
                ).fetchall()
        except sqlite3.Error as exc:
            log.debug("gh shim call accounting read failed: %s", exc)
            return []
        return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]


class FetchBudget:
    """A per-session cap on live fetches, in the shared cache's database.

    Rolling windows are approximated by a reset stamp rather than a true
    sliding window: the failure this bounds is a loop making thousands of
    calls, which a fixed window catches just as well and with one row.
    """

    def __init__(self, path: Path, *, capacity: int | None = None) -> None:
        self.path = path
        self.capacity = bucket_capacity() if capacity is None else capacity
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout = 2000")
        return connection

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gh_shim_budget_v1 (
                        session TEXT PRIMARY KEY,
                        window_start REAL NOT NULL DEFAULT 0,
                        spent INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
        except sqlite3.Error as exc:
            log.debug("gh shim budget init failed: %s", exc)

    def spend(self, session: str, *, now: float | None = None) -> tuple[bool, int]:
        """Claim one live fetch.  Returns whether it was allowed, and the count.

        A database this cannot reach must not become a fence: an unreachable
        budget allows the fetch, because refusing every read on a broken cache
        file would be a worse outage than the polling it guards against.
        """
        moment = time.time() if now is None else now
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT window_start, spent FROM gh_shim_budget_v1 WHERE session = ?",
                    (session,),
                ).fetchone()
                started, spent = (
                    (float(row[0]), int(row[1])) if row else (moment, 0)
                )
                if moment - started >= DEFAULT_BUCKET_WINDOW_S:
                    started, spent = moment, 0
                if spent >= self.capacity:
                    return (False, spent)
                spent += 1
                connection.execute(
                    """
                    INSERT INTO gh_shim_budget_v1 (session, window_start, spent)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session) DO UPDATE SET
                        window_start = excluded.window_start,
                        spent = excluded.spent
                    """,
                    (session, started, spent),
                )
                return (True, spent)
        except sqlite3.Error as exc:
            log.debug("gh shim budget claim failed: %s", exc)
            return (True, 0)


def session_identity(environment: dict[str, str] | None = None) -> str:
    """Whose bucket this invocation spends from.

    A deck-owned worker carries its session key; anything else falls back to
    its process group, so an unowned shell still gets a bucket rather than
    sharing one global counter with every worker on the host.
    """
    env = os.environ if environment is None else environment
    consumer = env.get(CONSUMER_ENV)
    if consumer:
        return consumer
    key = env.get("AGENTDECK_SESSION_KEY")
    if key:
        return key
    return f"pgid:{os.getpgrp()}"
