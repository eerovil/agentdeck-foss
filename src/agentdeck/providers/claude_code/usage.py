"""OAuth usage-limit reads + the per-account poller.

The undocumented endpoint ``GET https://api.anthropic.com/api/oauth/usage``
returns the same 5-hour / 7-day limit data the ``/usage`` slash command shows.
It is server-side rate limited, so the poller runs sparingly (minutes), reads
the bearer token *fresh* each cycle (tokens rotate), backs off on 429/5xx, and
publishes each snapshot to a shared cache other host tools can read.

Observed response shape (CLI v2.1.198):
    {"five_hour": {"utilization": 0.0, "resets_at": null},
     "seven_day": {"utilization": 5.0, "resets_at": "2026-07-06T20:59:59Z"}, ...}
``utilization`` is already a 0-100 percentage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from ...models import Account, UsageSnapshot

# The meter-failure hierarchy lives once, in ``..usage``, so the shared loop can
# recognize a refused credential without knowing which provider raised it.
from ..usage import UsageAuthError, UsagePoller, UsageRateLimited, UsageUnavailable
from .credentials import read_access_token

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Must look like Claude Code or the request lands in an aggressively throttled
# bucket. Kept generic + version-tagged; update alongside claude-code-internals.md.
USER_AGENT = "claude-code/2.1.198 (agentdeck)"
BETA_HEADER = "oauth-2025-04-20"


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(bucket: object) -> float | None:
    if isinstance(bucket, dict):
        util = bucket.get("utilization")
        if isinstance(util, (int, float)):
            return float(util)
    return None


def parse_usage(account_key: str, data: dict) -> UsageSnapshot:
    five = data.get("five_hour")
    seven = data.get("seven_day")
    return UsageSnapshot(
        account_key=account_key,
        five_hour_pct=_pct(five),
        five_hour_resets_at=_parse_ts(five.get("resets_at") if isinstance(five, dict) else None),
        seven_day_pct=_pct(seven),
        seven_day_resets_at=_parse_ts(seven.get("resets_at") if isinstance(seven, dict) else None),
        fetched_at=datetime.now(UTC),
    )


async def fetch_usage_once(account: Account, client: httpx.AsyncClient) -> UsageSnapshot:
    """One request. Reads the token fresh from disk; raises on any failure."""
    token = read_access_token(account.root)
    if not token:
        raise UsageUnavailable(f"no access token for {account.key}")
    resp = await client.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
    )
    if resp.status_code == 401:
        raise UsageAuthError(f"401 for {account.key}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise UsageRateLimited(f"{resp.status_code} for {account.key}")
    if resp.status_code >= 400:
        raise UsageUnavailable(f"{resp.status_code} for {account.key}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UsageUnavailable(f"malformed usage JSON for {account.key}") from exc
    if not isinstance(data, dict):
        raise UsageUnavailable(f"unexpected usage payload for {account.key}")
    return parse_usage(account.key, data)


class ClaudeUsagePoller(UsagePoller):
    """Claude usage over a persistent httpx client, with a one-shot retry when
    the bearer token rotated between read and use."""

    def __init__(
        self,
        account: Account,
        state,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        **kwargs,
    ):
        super().__init__(account, state, **kwargs)
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30.0))

    def _resource(self):
        return self._client_factory()

    async def _fetch(self, client: httpx.AsyncClient) -> UsageSnapshot:
        return await fetch_usage_once(self.account, client)

    async def _on_error(self, exc: Exception, client: object) -> float:
        if isinstance(exc, UsageAuthError):
            # Token may have rotated between read and use — retry once immediately
            # (fetch_usage_once re-reads the token from disk).
            try:
                await self._publish_once(client)
                return self.interval_s * self._jitter()
            except UsageUnavailable:
                pass
        return await super()._on_error(exc, client)
