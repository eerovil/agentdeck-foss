"""Shared usage-poller scaffolding for both providers.

Each provider reads its own account limits its own way (Claude over an OAuth HTTP
endpoint with a persistent client; Codex over a short-lived ``app-server``
subprocess), but the loop *around* the read is identical: a phase offset so
accounts don't fire in lockstep, publish-and-cache on success, and mark-stale +
exponential backoff on failure. That loop, the backoff, and the snapshot cache
that other host tools read live here once. A provider subclass overrides three
seams — ``_fetch`` (the read), ``_resource`` (any loop-scoped resource), and
``_on_error`` (provider-specific failure handling).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from ..models import Account, UsageSnapshot
from . import account_health

log = logging.getLogger(__name__)

# The exponential backoff ceiling shared by both pollers.
BACKOFF_CAP_S = 1800.0


class UsageUnavailable(Exception):
    """Usage could not be read this cycle (no credential / network / parse error)."""


class UsageAuthError(UsageUnavailable):
    """The account's meter credential is missing, expired, or rejected.

    Distinct from a transport failure because the fix is a human one — sign in
    again — and no amount of retrying changes it. One hierarchy for all three
    providers on purpose: the loop below has to recognize "this credential does
    not work" without knowing which provider raised it (#758).
    """


class UsageRateLimited(UsageUnavailable):
    """429 or 5xx — caller should back off exponentially."""


def shared_cache_dir(configured: str = "") -> Path:
    """Resolve the shared usage-cache directory, with an XDG fallback."""
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("~/.cache").expanduser()
    return base / "agentdeck"


def usage_cache_path(account: Account, cache_dir: Path) -> Path:
    """Canonical provider-qualified cache path for one account."""
    return cache_dir / f"usage-{account.key.replace(':', '-', 1)}.json"


def legacy_usage_cache_path(account: Account, cache_dir: Path) -> Path:
    """Label-only compatibility alias used by pre-provider-qualified host tools."""
    return cache_dir / f"usage-{account.label}.json"


def _write_cache_file(target: Path, payload: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_shared_cache(snapshot: UsageSnapshot, account: Account, cache_dir: Path) -> None:
    """Atomically publish a snapshot other host tools (e.g. kanban_poll.sh) can read."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": account.key,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "five_hour_pct": snapshot.five_hour_pct,
            "five_hour_resets_at": snapshot.five_hour_resets_at.isoformat()
            if snapshot.five_hour_resets_at
            else None,
            "seven_day_pct": snapshot.seven_day_pct,
            "seven_day_resets_at": snapshot.seven_day_resets_at.isoformat()
            if snapshot.seven_day_resets_at
            else None,
            # Absent from every file written before #730; readers below and the
            # poller engine's own copy of this read both treat a missing key as
            # "this provider reports no billing-cycle window".
            "month_pct": snapshot.month_pct,
            "month_resets_at": snapshot.month_resets_at.isoformat()
            if snapshot.month_resets_at
            else None,
        }
        _write_cache_file(usage_cache_path(account, cache_dir), payload)
        # Keep the old path during migration. New readers accept it only when its embedded
        # provider-qualified identity matches, so equal labels can no longer cross-contaminate.
        _write_cache_file(legacy_usage_cache_path(account, cache_dir), payload)
        if account.provider_id == "claude_code":
            # Claude's hard circuit is opened by CLI evidence about either current usage window.
            # A later meter read proves health only when both windows are present and sub-limit;
            # a partial payload is not evidence that the missing exhausted window recovered.
            account_health.reconcile_healthy_usage(
                cache_dir,
                account.key,
                fetched_at=snapshot.fetched_at,
                percentages=(snapshot.five_hour_pct, snapshot.seven_day_pct),
            )
    except OSError as exc:
        log.warning("could not write shared usage cache for %s: %s", account.key, exc)


def clear_shared_cache(account: Account, cache_dir: Path) -> None:
    """Unpublish this account's snapshot — the third member of the write/read pair.

    Called when the provider refuses the meter credential (#758). Dropping the
    in-memory snapshot alone was not enough: the cache outlives the web process,
    and ``warm_usage_state`` deliberately applies no max age, so the next restart
    put the withdrawn percentage straight back on the header. Every other reader
    already treats a missing file as "usage unknown", which fails open — the same
    answer it reaches on its own once the file ages out.

    The legacy label-only alias is removed only when it names this account: it is
    a shared filename, and two providers can hold the same label.
    """
    try:
        usage_cache_path(account, cache_dir).unlink(missing_ok=True)
        legacy = legacy_usage_cache_path(account, cache_dir)
        try:
            payload = json.loads(legacy.read_text())
        except (OSError, ValueError):
            return
        if isinstance(payload, dict) and payload.get("account") == account.key:
            legacy.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear shared usage cache for %s: %s", account.key, exc)


def _parse_iso(value: object) -> datetime | None:
    """Decode an ISO timestamp write_shared_cache emitted, tolerant of junk."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_shared_cache(account: Account, cache_dir: Path) -> UsageSnapshot | None:
    """Reconstruct the last snapshot ``write_shared_cache`` persisted for this
    account, or ``None`` if it is absent or unreadable. The read counterpart of
    ``write_shared_cache``, used to warm in-memory state on startup so a web
    restart shows aged bars instead of blank "no usage data yet" until the first
    live poll (which, under usage-endpoint 429 backoff, can be minutes)."""
    raw = None
    for path in (
        usage_cache_path(account, cache_dir),
        legacy_usage_cache_path(account, cache_dir),
    ):
        try:
            candidate = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict) and candidate.get("account") == account.key:
            raw = candidate
            break
    if raw is None:
        return None
    fetched = _parse_iso(raw.get("fetched_at"))
    if fetched is None:
        return None
    return UsageSnapshot(
        account_key=account.key,
        five_hour_pct=raw.get("five_hour_pct"),
        five_hour_resets_at=_parse_iso(raw.get("five_hour_resets_at")),
        seven_day_pct=raw.get("seven_day_pct"),
        seven_day_resets_at=_parse_iso(raw.get("seven_day_resets_at")),
        fetched_at=fetched,
        month_pct=raw.get("month_pct"),
        month_resets_at=_parse_iso(raw.get("month_resets_at")),
    )


def warm_usage_state(state, accounts: list[Account], cache_dir: Path) -> None:
    """Seed each account's usage bars at startup from its last persisted snapshot:
    the shared cache first (full fidelity, incl. reset times), else the history
    DB (percentages only — reset times aren't stored). Age-based staleness then
    renders anything old as "stale · updated …" on its own. Runs before the live
    poller starts, so a restart never blanks the bars."""
    for account in accounts:
        snap = read_shared_cache(account, cache_dir)
        if snap is None and state.db is not None:
            snap = state.db.latest_usage(account.key)
        if snap is not None:
            state.warm_usage(snap)


class UsagePoller:
    """Long-lived per-account usage loop. Injectable ``sleep``/``jitter`` for tests."""

    def __init__(
        self,
        account: Account,
        state,
        *,
        interval_s: float = 300.0,
        cache_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),
    ):
        self.account = account
        self.state = state
        self.interval_s = interval_s
        self.cache_dir = cache_dir or shared_cache_dir()
        self._sleep = sleep
        self._jitter = jitter
        self._backoff: float | None = None

    def _next_backoff(self) -> float:
        self._backoff = (
            self.interval_s
            if self._backoff is None
            else min(self._backoff * 2, BACKOFF_CAP_S)
        )
        return self._backoff

    def _resource(self) -> contextlib.AbstractAsyncContextManager:
        """A resource entered once around the whole loop (e.g. a persistent HTTP
        client). Default: none — the loop yields ``None`` to ``_fetch``."""
        return contextlib.nullcontext(None)

    async def _fetch(self, resource: object) -> UsageSnapshot:
        """One usage read; ``resource`` is whatever ``_resource`` yielded."""
        raise NotImplementedError

    async def _publish_once(self, resource: object) -> None:
        snapshot = await self._fetch(resource)
        self.state.set_usage(snapshot)
        write_shared_cache(snapshot, self.account, self.cache_dir)
        self._backoff = None

    async def _on_error(self, exc: Exception, resource: object) -> float:
        """Handle a failed cycle and return the next delay. Default: mark the
        account's usage stale and back off exponentially.

        A rejected credential is the one failure that is *not* just staleness: the
        meter has no idea what the account's real usage is, so the last good
        percentage is withdrawn rather than aged (#758) — from the shared cache as
        well as from memory, or the next web restart would warm it back onto the
        header. Everything else keeps the last read and marks it stale, because a
        transport blip does not make the previous number wrong.
        """
        log.debug("usage poll failed for %s: %s", self.account.key, exc)
        if isinstance(exc, UsageAuthError):
            self.state.mark_usage_credential_rejected(self.account.key, str(exc))
            clear_shared_cache(self.account, self.cache_dir)
        else:
            self.state.mark_usage_stale(self.account.key)
        return self._next_backoff()

    async def run(self) -> None:
        # Phase offset so multiple accounts never fire in lockstep.
        await self._sleep(self.interval_s * (self._jitter() - 0.9))
        async with self._resource() as resource:
            while True:
                try:
                    await self._publish_once(resource)
                    delay = self.interval_s * self._jitter()
                except Exception as exc:  # noqa: BLE001 -- keep the long-lived poller alive
                    delay = await self._on_error(exc, resource)
                await self._sleep(delay)
