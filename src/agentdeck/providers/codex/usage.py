"""Codex account rate-limit reads through ``codex app-server``.

The app-server owns authentication and exposes the same rolling limits Codex
shows in its UI.  agentdeck speaks the small JSON-RPC subset it needs instead
of reading credentials or calling an undocumented HTTP endpoint directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ...models import Account, UsageSnapshot

# The meter-failure hierarchy lives once, in ``..usage``; this provider only ever
# raises the base. Re-exported, so callers keep importing it from here.
from ..usage import UsagePoller, UsageUnavailable

log = logging.getLogger(__name__)

FIVE_HOUR_MINS = 5 * 60
SEVEN_DAY_MINS = 7 * 24 * 60
REQUEST_TIMEOUT_S = 30.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _limit_buckets(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every rate-limit bucket the app-server reports, then the legacy view.

    Reading only ``rateLimitsByLimitId["codex"]`` dropped every other limit id
    before parsing (#790). Buckets are per model family — observed live, this
    host reports ``codex`` alongside ``codex_bengalfox`` ("GPT-5.3-Codex-Spark").
    """
    by_id = data.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        buckets = [bucket for bucket in by_id.values() if isinstance(bucket, dict)]
        if buckets:
            return buckets
    legacy = data.get("rateLimits")
    return [legacy] if isinstance(legacy, dict) else []


def _bucket_windows(
    limits: dict[str, Any],
) -> dict[int, tuple[float | None, datetime | None]]:
    windows: dict[int, tuple[float | None, datetime | None]] = {}
    raw_windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        raw_windows.append(window)
        duration = window.get("windowDurationMins")
        if isinstance(duration, int) and not isinstance(duration, bool):
            windows[duration] = (
                _number(window.get("usedPercent")),
                _timestamp(window.get("resetsAt")),
            )

    # Older app-server responses may omit durations. Preserve the historical
    # primary=5h / secondary=7d ordering only for otherwise-unidentified windows.
    if FIVE_HOUR_MINS not in windows and raw_windows:
        first = raw_windows[0]
        if first.get("windowDurationMins") is None:
            windows[FIVE_HOUR_MINS] = (
                _number(first.get("usedPercent")),
                _timestamp(first.get("resetsAt")),
            )
    if SEVEN_DAY_MINS not in windows and len(raw_windows) > 1:
        second = raw_windows[1]
        if second.get("windowDurationMins") is None:
            windows[SEVEN_DAY_MINS] = (
                _number(second.get("usedPercent")),
                _timestamp(second.get("resetsAt")),
            )
    return windows


def parse_usage(account_key: str, data: dict[str, Any]) -> UsageSnapshot:
    buckets = _limit_buckets(data)
    if not buckets:
        raise UsageUnavailable("app-server response has no Codex rate-limit bucket")

    # Per window, report the fullest bucket. The number has to mean what the
    # operator feels while working: reporting the emptiest bucket showed 0% on a
    # host whose default `codex` bucket was 3% used, and would show 0% again with
    # the default bucket exhausted just because a per-model bucket was untouched.
    # Admission reads the same snapshot, so it stops the account on the limit the
    # normal model actually hit.
    windows: dict[int, tuple[float | None, datetime | None]] = {}
    for bucket in buckets:
        for duration, window in _bucket_windows(bucket).items():
            current = windows.get(duration)
            if current is None:
                windows[duration] = window
                continue
            current_pct, candidate_pct = current[0], window[0]
            if current_pct is None or (
                candidate_pct is not None and candidate_pct > current_pct
            ):
                windows[duration] = window

    five = windows.get(FIVE_HOUR_MINS)
    seven = windows.get(SEVEN_DAY_MINS)
    if five is None and seven is None:
        raise UsageUnavailable("Codex rate-limit bucket has no recognized rolling window")

    return UsageSnapshot(
        account_key=account_key,
        five_hour_pct=five[0] if five else None,
        five_hour_resets_at=five[1] if five else None,
        seven_day_pct=seven[0] if seven else None,
        seven_day_resets_at=seven[1] if seven else None,
        fetched_at=datetime.now(UTC),
    )


async def _read_response(process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
    assert process.stdout is not None
    while line := await process.stdout.readline():
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        error = message.get("error")
        if error is not None:
            raise UsageUnavailable(f"Codex app-server request failed: {error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise UsageUnavailable("Codex app-server returned an invalid result")
        return result
    raise UsageUnavailable("Codex app-server closed before replying")


async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise UsageUnavailable("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    await process.stdin.drain()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def fetch_usage_once(
    account: Account,
    *,
    process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> UsageSnapshot:
    """Start a short-lived app-server and read this account's rate limits."""
    factory = process_factory or asyncio.create_subprocess_exec
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    try:
        process = await factory(
            "codex",
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"could not start Codex app-server: {exc}") from exc

    try:
        async with asyncio.timeout(timeout_s):
            await _send(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "agentdeck", "version": "0.3.1"}},
                },
            )
            await _read_response(process, 1)
            await _send(process, {"method": "initialized", "params": {}})
            await _send(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
            result = await _read_response(process, 2)
            return parse_usage(account.key, result)
    except TimeoutError as exc:
        raise UsageUnavailable("Codex app-server usage request timed out") from exc
    except (BrokenPipeError, ConnectionError, OSError) as exc:
        raise UsageUnavailable(f"Codex app-server communication failed: {exc}") from exc
    finally:
        await _stop_process(process)


class CodexUsagePoller(UsagePoller):
    """Codex usage over a short-lived ``app-server`` subprocess per cycle. No
    loop-scoped resource; the default broad-catch backoff covers every failure."""

    def __init__(
        self,
        account: Account,
        state,
        *,
        fetch: Callable[[Account], Awaitable[UsageSnapshot]] = fetch_usage_once,
        **kwargs,
    ):
        super().__init__(account, state, **kwargs)
        self._fetch_fn = fetch

    async def _fetch(self, resource: object) -> UsageSnapshot:
        return await self._fetch_fn(self.account)
