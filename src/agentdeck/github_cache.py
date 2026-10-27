"""Host-shared, best-effort cache for GitHub metadata lookups.

The production and staging web processes monitor the same agent account data,
so they also ask many of the same GitHub questions.  This module keeps that
coordination out of callers: a short SQLite lease lets one process refresh a
stale key while the other continues using the last trusted value.

The table name is versioned rather than migrated in place.  Adjacent AgentDeck
versions can therefore share the v1 contract without one process rewriting the
schema under another; a future incompatible format can use a new table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubMetadata:
    """A successful fetch result and whether it can still change on GitHub."""

    value: Any
    terminal: bool = False


@dataclass(frozen=True)
class GitHubLookup:
    """Resolved metadata.

    ``complete`` is false when ``value`` is stale (or absent) because another
    process owns the refresh lease or the fetch failed.  A stale value remains
    trustworthy historical evidence; callers must not reinterpret an
    incomplete empty result as proof that remote metadata disappeared.
    """

    value: Any | None
    complete: bool
    refreshed: bool = False
    # How long ago this value was read from GitHub, in seconds. ``complete`` only
    # answers "is it inside the TTL"; a caller deciding whether an *absence* from
    # the value is evidence needs to know how far outside it fell, because a
    # value minutes past its TTL and one days past it are not the same claim
    # (#1115). None means there is no value, so there is no age either.
    age_s: float | None = None
    # Why there was no refresh, when nobody fetched (#3344): ``"leased"`` while another
    # process holds the refresh lease, ``"backoff"`` while the key waits out a failed
    # fetch. A caller that has nothing to serve says which, because "GitHub did not
    # answer" is wrong for both: this process never asked.
    reason: str = ""
    # For ``"backoff"``: how long ago the failed fetch gave up, in seconds.
    failed_s: float | None = None


class GitHubMetadataCache:
    """Resolve opaque GitHub metadata through a process-safe shared cache."""

    OPEN_TTL_S = 300.0
    # A merged pull request and a closed issue are finished, so re-reading one
    # is nearly always a call spent on an answer already held. The deck
    # accumulates these for ever — 1036 of the 1137 pull requests it tracked
    # were terminal — and an hourly TTL turned that history into ~520 GraphQL
    # calls an hour, a tenth of the 5000/hour budget, growing with every merge
    # (#1394). Hence a day rather than an hour: ~43 calls an hour for the same
    # history, which is under 1% of the budget.
    #
    # But not for ever. An issue can be reopened straight in GitHub, and the
    # `gh` shim's write invalidation only hears about state changes this host
    # caused, so a permanently fresh `closed` would let the poller act on a
    # reopened issue as if it were still closed with nothing able to correct it
    # (#1395). Local invalidation stays the fast path; this TTL is the bound on
    # how long a state change nobody told us about can go unnoticed.
    TERMINAL_TTL_S = 24 * 3600.0
    NEGATIVE_TTL_S = 6 * 3600.0
    FAILURE_BACKOFF_S = 60.0
    LEASE_S = 15.0

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(
            os.environ.get(
                "AGENTDECK_GITHUB_CACHE",
                "~/.cache/agentdeck/github_metadata.sqlite3",
            )
        ).expanduser()
        self._locks: dict[str, asyncio.Lock] = {}
        self._connections = threading.local()
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        """This thread's connection to the shared cache file.

        Opening SQLite is cheap but not free, and this cache is read on the hot
        paths: a session card asks for its issue badge on every render, and a
        scan peeks and claims once per referenced issue. That was several
        hundred fresh connections per scan on the event loop, which is time no
        page could be rendered in (#1088).

        The handle is kept per thread because a SQLite connection may not cross
        threads, and only for reuse — the cross-process coordination is still
        the lease in the file itself, unchanged. Each caller keeps its own
        ``with connection:`` transaction, which commits as before and does not
        close the handle.
        """
        existing = getattr(self._connections, "handle", None)
        if existing is not None:
            return existing
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout = 2000")
        self._connections.handle = connection
        return connection

    def _discard_connection(self) -> None:
        """Throw this thread's handle away after a SQLite error.

        A failure can leave the connection mid-transaction, and a reused handle
        would carry that into every later call. Dropping it restores exactly the
        old behaviour of the error path — the next caller opens a fresh one.
        """
        connection = getattr(self._connections, "handle", None)
        self._connections.handle = None
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_metadata_v1 (
                        key TEXT PRIMARY KEY,
                        payload TEXT,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        fetched_at REAL NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT
                    )
                    """
                )
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache init failed: %s", exc)

    @staticmethod
    def _decode(payload: str | None) -> Any | None:
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return None

    def _ttl(self, value: Any | None, terminal: bool) -> float:
        """How long this row may be reused.

        Read off the instance, not the class: callers (and tests) narrow these
        windows per resolver, and a classmethod would silently ignore them.
        """
        if value is None:
            return self.NEGATIVE_TTL_S
        return self.TERMINAL_TTL_S if terminal else self.OPEN_TTL_S

    def is_fresh(self, key: str, *, now: float | None = None) -> bool:
        """Would resolving ``key`` answer without calling GitHub?

        Callers that must charge a budget for a live fetch need to know this
        *before* claiming the lease: a gate applied inside the fetch would
        record its refusal as GitHub's failure and back the shared key off for
        every other process too.  A racing refresh only makes this answer
        pessimistic, which costs a lookup rather than correctness.
        """
        moment = time.time() if now is None else now
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT payload, terminal, fetched_at
                    FROM github_metadata_v1 WHERE key = ?
                    """,
                    (key,),
                ).fetchone()
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache freshness read failed: %s", exc)
            return False
        if row is None or not row[2]:
            return False
        return moment - float(row[2]) < self._ttl(self._decode(row[0]), bool(row[1]))

    def peek(self, key: str) -> GitHubLookup:
        """Return the last shared value without refreshing it.

        ``complete`` says the value is still fresh and no failed/in-flight refresh
        has put the key in its lease window. The value itself remains useful
        historical evidence when incomplete.
        """
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """SELECT payload, terminal, fetched_at, lease_until
                    FROM github_metadata_v1 WHERE key = ?""",
                    (key,),
                ).fetchone()
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache read failed: %s", exc)
            return GitHubLookup(None, False)
        if row is None or not row[2]:
            return GitHubLookup(None, False)
        value = self._decode(row[0])
        now = time.time()
        age = max(0.0, now - float(row[2]))
        fresh = age < self._ttl(value, bool(row[1]))
        available = float(row[3]) <= now
        return GitHubLookup(value, fresh and available, age_s=age)

    def wait_for(
        self,
        key: str,
        timeout_s: float,
        *,
        poll_s: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> GitHubLookup:
        """Wait, reading only this file, for another process's lease on ``key`` to end.

        For a caller with nothing cached that found the key leased (#3344): the other
        fetch's answer is the one it wants, and waiting for it costs no GitHub call.
        Returns the row once no live lease holds it, with ``reason="backoff"`` when that
        fetch failed, or ``reason="leased"`` when ``timeout_s`` passed first.
        """
        deadline = clock() + timeout_s
        while True:
            try:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT lease_until, lease_token FROM github_metadata_v1 WHERE key = ?",
                        (key,),
                    ).fetchone()
            except sqlite3.Error as exc:
                self._discard_connection()
                log.debug("GitHub metadata cache wait failed: %s", exc)
                return self.peek(key)
            now = time.time()
            leased = row is not None and bool(row[1]) and float(row[0]) > now
            if not leased:
                lookup = self.peek(key)
                if row is not None and float(row[0]) > now:
                    failed = max(0.0, now - (float(row[0]) - self.FAILURE_BACKOFF_S))
                    lookup = GitHubLookup(
                        lookup.value,
                        False,
                        age_s=lookup.age_s,
                        reason="backoff",
                        failed_s=failed,
                    )
                return lookup
            if clock() >= deadline:
                lookup = self.peek(key)
                return GitHubLookup(lookup.value, False, age_s=lookup.age_s, reason="leased")
            sleep(poll_s)

    def peek_values(self, keys: Iterable[str], *, strict: bool = False) -> dict[str, Any]:
        """The last shared value of each key that has one, in one read per 500 keys.

        ``peek`` without the freshness answer, for a caller that reads a set of keys on a
        cadence and wants no query per key (#2219). A key never fetched is absent.

        A failed read is empty unless ``strict``, which raises it instead: a caller that
        reads absence as a fact (an issue not closed, no paused label) must be able to
        tell "none" from "could not read" (#3553).
        """
        wanted = list(dict.fromkeys(keys))
        found: dict[str, Any] = {}
        try:
            with self._connect() as connection:
                for start in range(0, len(wanted), 500):
                    chunk = wanted[start : start + 500]
                    marks = ", ".join("?" for _ in chunk)
                    for key, payload, fetched_at in connection.execute(
                        f"SELECT key, payload, fetched_at FROM github_metadata_v1"
                        f" WHERE key IN ({marks})",
                        chunk,
                    ):
                        if fetched_at:
                            found[str(key)] = self._decode(payload)
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache read failed: %s", exc)
            if strict:
                raise
            return {}
        return found

    def offer(self, key: str, metadata: GitHubMetadata) -> bool:
        """Store a value produced without a fetch, as if it had just been fetched.

        The webhook write-through path (#1627): a delivery's payload already holds the
        row a fetch would produce, so it lands with a fresh ``fetched_at`` and restarts
        the ordinary TTL. Any lease in flight is released — the offer supersedes the
        fetch it raced, and if the fetch still lands afterwards, the newer of the two
        wins on the caller's own ``updatedAt`` guard at the next event.
        """
        try:
            payload = json.dumps(metadata.value, separators=(",", ":"), sort_keys=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO github_metadata_v1
                        (key, payload, terminal, fetched_at, lease_until, lease_token)
                    VALUES (?, ?, ?, ?, 0, NULL)
                    ON CONFLICT(key) DO UPDATE SET
                        payload = excluded.payload, terminal = excluded.terminal,
                        fetched_at = excluded.fetched_at,
                        lease_until = 0, lease_token = NULL
                    """,
                    (key, payload, int(metadata.terminal), time.time()),
                )
            return True
        except (TypeError, sqlite3.Error) as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache offer failed: %s", exc)
            return False

    def revise(self, key: str, value: Any) -> bool:
        """Replace a held row's value and nothing else: its age, TTL and lease stay.

        For a writer that knows one part of a cached value changed, not that the whole value
        was re-read (#2226): the row keeps the age of its last real fetch, so it goes stale
        and is re-read exactly when it would have. A key with no row is left absent.
        """
        try:
            payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE github_metadata_v1 SET payload = ? WHERE key = ? AND fetched_at > 0",
                    (payload, key),
                )
            return cursor.rowcount == 1
        except (TypeError, sqlite3.Error) as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache revise failed: %s", exc)
            return False

    def expire(self, key: str) -> bool:
        """Mark a held row stale and keep its value: the next resolve reads it again (#3622).

        For a writer that knows a row's facts moved without knowing to what, where dropping
        the row would leave its readers with nothing meanwhile. A terminal row too, whose day
        long TTL would otherwise hold the old value. A key with no row is left absent."""
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE github_metadata_v1 SET fetched_at = 1 WHERE key = ? AND fetched_at > 1",
                    (key,),
                )
            return cursor.rowcount == 1
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache expire failed: %s", exc)
            return False

    def invalidate(self, prefix: str) -> int:
        """Drop every cached key under ``prefix``, returning how many went.

        A write makes the reads around it stale immediately, and the caller
        knows that before any TTL does.  Dropping the rows rather than marking
        them keeps the next read on the ordinary miss path, lease and all.
        """
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM github_metadata_v1 WHERE key LIKE ? ESCAPE '\\'",
                    (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                     + "%",),
                )
            return cursor.rowcount or 0
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache invalidate failed: %s", exc)
            return 0

    def _claim(
        self,
        key: str,
        *,
        force: bool,
        fresh_after: float | None,
        now: float,
        ignore_backoff: bool = False,
    ) -> tuple[str | None, GitHubLookup]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT payload, terminal, fetched_at, lease_until, lease_token
                    FROM github_metadata_v1 WHERE key = ?
                    """,
                    (key,),
                ).fetchone()
                value = self._decode(row[0]) if row and row[2] else None
                age = max(0.0, now - float(row[2])) if row and row[2] else None
                if row and row[2]:
                    ttl = self._ttl(value, bool(row[1]))
                    forced_stale = force and (
                        fresh_after is None or float(row[2]) < fresh_after
                    )
                    if not forced_stale and now - float(row[2]) < ttl:
                        return (None, GitHubLookup(value, True, age_s=age))
                if row and float(row[3]) > now:
                    # `_back_off` clears the token; a live lease always holds one.
                    if row[4]:
                        return (None, GitHubLookup(value, False, age_s=age, reason="leased"))
                    if not ignore_backoff:
                        failed = max(0.0, now - (float(row[3]) - self.FAILURE_BACKOFF_S))
                        return (
                            None,
                            GitHubLookup(
                                value, False, age_s=age, reason="backoff", failed_s=failed
                            ),
                        )
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO github_metadata_v1 (key, lease_until, lease_token)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        lease_until = excluded.lease_until,
                        lease_token = excluded.lease_token
                    """,
                    (key, now + self.LEASE_S, token),
                )
                return (token, GitHubLookup(value, False, age_s=age))
        except sqlite3.Error as exc:
            self._discard_connection()
            # Cache failure must not disable GitHub-backed behavior.  Fetch
            # directly; storing the result below remains best-effort.
            log.debug("GitHub metadata cache claim failed: %s", exc)
            return ("", GitHubLookup(None, False))

    def _store(
        self, key: str, metadata: GitHubMetadata, now: float, token: str
    ) -> bool:
        try:
            payload = json.dumps(metadata.value, separators=(",", ":"), sort_keys=True)
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE github_metadata_v1 SET
                        payload = ?, terminal = ?, fetched_at = ?,
                        lease_until = 0, lease_token = NULL
                    WHERE key = ? AND lease_token = ?
                    """,
                    (payload, int(metadata.terminal), now, key, token),
                )
            return cursor.rowcount == 1
        except (TypeError, sqlite3.Error) as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache save failed: %s", exc)
            return False

    def _fetched_at(self, key: str) -> float:
        """When ``key``'s stored value was fetched, or 0 when it has none or is unreadable."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT fetched_at FROM github_metadata_v1 WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache read failed: %s", exc)
            return 0.0
        return float(row[0]) if row and row[0] else 0.0

    def _back_off(self, key: str, now: float, token: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE github_metadata_v1
                    SET lease_until = ?, lease_token = NULL
                    WHERE key = ? AND lease_token = ?
                    """,
                    (now + self.FAILURE_BACKOFF_S, key, token),
                )
        except sqlite3.Error as exc:
            self._discard_connection()
            log.debug("GitHub metadata cache backoff failed: %s", exc)

    async def resolve(
        self,
        key: str,
        fetch: Callable[[], Awaitable[GitHubMetadata | None]],
        *,
        force: bool = False,
        fresh_after: float | None = None,
        now: float | None = None,
        ignore_backoff: bool = False,
    ) -> GitHubLookup:
        """Resolve ``key``, refreshing it once across all sharing processes.

        ``fetch`` returns ``None`` only when GitHub is unavailable.  A
        successful negative lookup is represented by ``GitHubMetadata(None)``.
        ``ignore_backoff`` fetches through a failed fetch's back-off, never through
        another process's live lease (#3344).
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            results = await self.resolve_many(
                [(key, fetch)],
                force=force,
                fresh_after=fresh_after,
                now=now,
                max_refreshes=1,
                ignore_backoff=ignore_backoff,
            )
            return results[key]

    async def resolve_many(
        self,
        requests: list[
            tuple[str, Callable[[], Awaitable[GitHubMetadata | None]]]
        ],
        *,
        force: bool = False,
        fresh_after: float | None = None,
        now: float | None = None,
        max_refreshes: int | None = None,
        ignore_backoff: bool = False,
    ) -> dict[str, GitHubLookup]:
        """Resolve a batch while optionally bounding its actual GitHub calls."""
        checked_at = time.time() if now is None else now
        results: dict[str, GitHubLookup] = {}
        claimed: list[
            tuple[
                str,
                Callable[[], Awaitable[GitHubMetadata | None]],
                GitHubLookup,
                str,
            ]
        ] = []
        for key, fetch in dict(requests).items():
            if max_refreshes is not None and len(claimed) >= max_refreshes:
                results[key] = self.peek(key)
                continue
            token, cached = self._claim(
                key,
                force=force,
                fresh_after=fresh_after,
                now=checked_at,
                ignore_backoff=ignore_backoff,
            )
            if token is not None:
                claimed.append((key, fetch, cached, token))
            else:
                results[key] = cached

        async def finish(
            key: str,
            fetch: Callable[[], Awaitable[GitHubMetadata | None]],
            cached: GitHubLookup,
            token: str,
        ) -> tuple[str, GitHubLookup]:
            metadata = await fetch()
            if metadata is None:
                self._back_off(key, checked_at, token)
                return (key, cached)
            if not token:
                return (key, GitHubLookup(metadata.value, True, True, age_s=0.0))
            if not self._store(key, metadata, checked_at, token):
                # Our lease ran out mid-fetch. A result a later fetch already stored wins;
                # otherwise the answer we got is still GitHub's newest answer, and handing
                # back the row (empty, or older than ours) turned it into "GitHub did not
                # answer" or a stale value (#3344, #3347 review).
                if self._fetched_at(key) > checked_at:
                    return (key, self.peek(key))
                return (key, GitHubLookup(metadata.value, True, True, age_s=0.0))
            return (key, GitHubLookup(metadata.value, True, True, age_s=0.0))

        if claimed:
            refreshed = await asyncio.gather(
                *(
                    finish(key, fetch, cached, token)
                    for key, fetch, cached, token in claimed
                )
            )
            results.update(refreshed)
        return results
