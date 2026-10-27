"""Host-shared GitHub rate-limit readings from real response headers.

GitHub's ``/rate_limit`` payload can describe a token-local view while actual
requests are charged to a shared user budget.  The response headers on those
requests are the authority.  This module is in the engine package so both the
poller and the worker ``gh`` shim can publish to the same SQLite store.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

HEADER_SOURCE = "response headers from a real GitHub call"
ESTIMATE_SOURCE = "untrusted /rate_limit cold-start estimate"
READING_MAX_AGE_S = 30.0

# How old a reading may be when all it has to answer is "is there room left?" (#2082).
#
# `READING_MAX_AGE_S` is the freshness a *report* needs. The reserve gate needs much less, and
# insisting on 30s there cost a real call: `current_rate_limits` probes `gh api user` whenever no
# core reading is that fresh, and on a webhook-quiet wake that probe is the only GitHub call the
# whole cycle makes — so the reading was always stale, and the gate meant to protect the budget
# was the thing spending it, on seven instances, across roughly 670 wakes an hour.
#
# Staleness is safe in the direction that matters, **on the wakes this is offered to**. `remaining`
# falls only when something spends and rises only at the hourly reset, so an old reading
# under-reports the budget: it can make the gate refuse while there is really room — a cheap,
# self-correcting mistake — and cannot make it permit while there is not.
#
# That argument holds only while every spender records what it spent, and one does not. The
# `chatgpt-nudge` extension reads GitHub over raw `urllib` and throws the `x-ratelimit-*` response
# headers away; it is a separate stdlib-only program with no way to reach this store. So a stale
# reading is offered **only to a wake that has already decided to be webhook-quiet**, which is
# exactly the wake where that extension returns without asking GitHub anything. A full wake goes
# back to `READING_MAX_AGE_S` and probes. That keeps nearly all of the saving — quiet wakes are
# roughly 670 an hour against 84 full ones — and makes the contract above true rather than nearly
# true.
GATE_MAX_AGE_S = 300.0


# Set by `github_authority.environment` on every child it builds (#2289).
AUTHORITY_ENV = "AGENTDECK_GITHUB_AUTHORITY"


def bind_authority(store: RateLimitStore, environment: dict[str, str], credential: str) -> None:
    """Bind an App installation's credential to the installation itself (#2289).

    An installation token cannot ask ``GET /user`` who it is, so the usual binding
    never happens and every call would probe. The authority name is the identity.
    """
    authority = environment.get(AUTHORITY_ENV, "")
    if authority.startswith("app:") and not store.is_bound(credential):
        store.bind(credential, authority)


def credential_identity(environment: dict[str, str]) -> str:
    """Return a non-secret fingerprint for one GitHub credential source.

    A chosen authority is its own identity (#2289): an App installation token is
    replaced every hour, and keying on the token would forget the installation's
    quota and cache readings each time it rotated.
    """
    authority = environment.get(AUTHORITY_ENV, "")
    if authority:
        material = "\x00".join(("authority", authority, environment.get("GH_HOST", "")))
        return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]
    material = "\x00".join(
        environment.get(name, "")
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR", "GH_HOST")
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(frozen=True)
class Reading:
    resource: str
    remaining: int
    used: int | None = None
    limit: int | None = None
    reset: int | None = None
    source: str = HEADER_SOURCE
    observed_at: float = 0.0


def _integer(value: str | object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def strip_api_trace(stderr: str, *, now: float | None = None) -> tuple[str, tuple[Reading, ...]]:
    """Remove ``GH_DEBUG=api`` traffic and retain its rate-limit headers.

    Request traces contain request headers and response bodies, so none of the
    trace is forwarded to the caller. Ordinary diagnostics outside a request
    block are preserved verbatim.
    """
    clean: list[str] = []
    readings: list[Reading] = []
    headers: dict[str, str] = {}
    tracing = False
    observed_at = time.time() if now is None else now

    def finish_response() -> None:
        resource = headers.get("x-ratelimit-resource", "").strip().lower()
        remaining = _integer(headers.get("x-ratelimit-remaining"))
        if resource and remaining is not None:
            readings.append(
                Reading(
                    resource=resource,
                    remaining=remaining,
                    used=_integer(headers.get("x-ratelimit-used")),
                    limit=_integer(headers.get("x-ratelimit-limit")),
                    reset=_integer(headers.get("x-ratelimit-reset")),
                    observed_at=observed_at,
                )
            )
        headers.clear()

    for line in stderr.splitlines(keepends=True):
        if line.startswith("* Request at "):
            if tracing:
                finish_response()
            tracing = True
            continue
        if tracing:
            if line.startswith("< ") and ":" in line:
                name, value = line[2:].split(":", 1)
                headers[name.strip().lower()] = value.strip()
            if line.startswith("* Request took "):
                finish_response()
                tracing = False
            continue
        clean.append(line)
    if tracing:
        finish_response()
    return "".join(clean), tuple(readings)


def user_id(payload: str) -> str:
    """Read the stable authenticated-user id returned by ``GET /user``."""
    try:
        value = json.loads(payload or "null")
    except ValueError:
        return ""
    identifier = value.get("id") if isinstance(value, dict) else None
    return str(identifier) if _integer(identifier) is not None else ""


class RateLimitStore:
    """Share current readings across processes and credentials for one user."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(
            os.environ.get(
                "AGENTDECK_GITHUB_CACHE",
                "~/.cache/agentdeck/github_metadata.sqlite3",
            )
        ).expanduser()
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
                    CREATE TABLE IF NOT EXISTS github_rate_limit_identity_v1 (
                        credential TEXT PRIMARY KEY,
                        user_key TEXT NOT NULL,
                        observed_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_rate_limit_v1 (
                        subject TEXT NOT NULL,
                        resource TEXT NOT NULL,
                        remaining INTEGER NOT NULL,
                        used INTEGER,
                        quota_limit INTEGER,
                        reset_at INTEGER,
                        source TEXT NOT NULL,
                        observed_at REAL NOT NULL,
                        PRIMARY KEY (subject, resource)
                    )
                    """
                )
        except sqlite3.Error:
            pass

    @staticmethod
    def _fallback(credential: str) -> str:
        return f"credential:{credential}"

    def _subject(self, connection: sqlite3.Connection, credential: str) -> str:
        row = connection.execute(
            "SELECT user_key FROM github_rate_limit_identity_v1 WHERE credential = ?",
            (credential,),
        ).fetchone()
        return str(row[0]) if row else self._fallback(credential)

    def is_bound(self, credential: str) -> bool:
        try:
            with self._connect() as connection:
                return self._subject(connection, credential) != self._fallback(credential)
        except sqlite3.Error:
            return False

    def bind(self, credential: str, identifier: str, *, now: float | None = None) -> None:
        """Bind a credential to a user and migrate any token-local observations."""
        if not identifier:
            return
        user_key = f"github.com:user:{identifier}"
        observed_at = time.time() if now is None else now
        fallback = self._fallback(credential)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO github_rate_limit_identity_v1
                        (credential, user_key, observed_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(credential) DO UPDATE SET
                        user_key = excluded.user_key,
                        observed_at = excluded.observed_at
                    """,
                    (credential, user_key, observed_at),
                )
                connection.execute(
                    """
                    INSERT INTO github_rate_limit_v1
                    SELECT ?, resource, remaining, used, quota_limit, reset_at,
                           source, observed_at
                    FROM github_rate_limit_v1 WHERE subject = ?
                    ON CONFLICT(subject, resource) DO UPDATE SET
                        remaining = excluded.remaining,
                        used = excluded.used,
                        quota_limit = excluded.quota_limit,
                        reset_at = excluded.reset_at,
                        source = excluded.source,
                        observed_at = excluded.observed_at
                    WHERE excluded.observed_at >= github_rate_limit_v1.observed_at
                    """,
                    (user_key, fallback),
                )
                connection.execute(
                    "DELETE FROM github_rate_limit_v1 WHERE subject = ?", (fallback,)
                )
        except sqlite3.Error:
            pass

    def _write(self, credential: str, reading: Reading, *, estimate: bool) -> None:
        try:
            with self._connect() as connection:
                subject = self._subject(connection, credential)
                connection.execute(
                    """
                    INSERT INTO github_rate_limit_v1
                        (subject, resource, remaining, used, quota_limit, reset_at,
                         source, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject, resource) DO UPDATE SET
                        remaining = excluded.remaining,
                        used = excluded.used,
                        quota_limit = excluded.quota_limit,
                        reset_at = excluded.reset_at,
                        source = excluded.source,
                        observed_at = excluded.observed_at
                    WHERE
                        (? = 0 AND excluded.observed_at >= github_rate_limit_v1.observed_at)
                        OR
                        (? = 1 AND (
                            github_rate_limit_v1.source != ?
                            OR github_rate_limit_v1.observed_at
                                < excluded.observed_at - ?
                        ))
                    """,
                    (
                        subject,
                        reading.resource,
                        reading.remaining,
                        reading.used,
                        reading.limit,
                        reading.reset,
                        reading.source,
                        reading.observed_at or time.time(),
                        int(estimate),
                        int(estimate),
                        HEADER_SOURCE,
                        READING_MAX_AGE_S,
                    ),
                )
        except sqlite3.Error:
            pass

    def record_headers(self, credential: str, readings: tuple[Reading, ...]) -> None:
        for reading in readings:
            self._write(credential, reading, estimate=False)

    def record_estimate(
        self, credential: str, payload: object, *, now: float | None = None
    ) -> None:
        """Fill missing resources from the explicitly untrusted cold-start payload."""
        resources = payload.get("resources") if isinstance(payload, dict) else None
        if not isinstance(resources, dict):
            return
        observed_at = time.time() if now is None else now
        for resource in ("core", "graphql"):
            value = resources.get(resource)
            remaining = _integer(value.get("remaining")) if isinstance(value, dict) else None
            if remaining is None:
                continue
            self._write(
                credential,
                Reading(
                    resource=resource,
                    remaining=remaining,
                    used=_integer(value.get("used")),
                    limit=_integer(value.get("limit")),
                    reset=_integer(value.get("reset")),
                    source=ESTIMATE_SOURCE,
                    observed_at=observed_at,
                ),
                estimate=True,
            )

    def read(
        self,
        credential: str,
        *,
        max_age: float = READING_MAX_AGE_S,
        now: float | None = None,
    ) -> dict[str, Reading]:
        observed_after = (time.time() if now is None else now) - max_age
        try:
            with self._connect() as connection:
                subject = self._subject(connection, credential)
                rows = connection.execute(
                    """
                    SELECT resource, remaining, used, quota_limit, reset_at,
                           source, observed_at
                    FROM github_rate_limit_v1
                    WHERE subject = ? AND observed_at >= ?
                    """,
                    (subject, observed_after),
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {
            str(row[0]): Reading(
                resource=str(row[0]),
                remaining=int(row[1]),
                used=int(row[2]) if row[2] is not None else None,
                limit=int(row[3]) if row[3] is not None else None,
                reset=int(row[4]) if row[4] is not None else None,
                source=str(row[5]),
                observed_at=float(row[6]),
            )
            for row in rows
        }
