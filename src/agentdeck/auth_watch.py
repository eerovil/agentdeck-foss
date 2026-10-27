"""Per-account Claude login health, and the recovery in progress (#298).

Owned by the web process, on purpose. A login is a minute of a human's attention,
not a durable agent turn: if a deploy restarts the web app mid-flow the answer is to
press Reconnect again, and that is cheaper than teaching the persistent runtime to
hold a subprocess it has no other reason to know about.

Two things are cached here: the last answer to "is this account signed in", and the
at-most-one login flow per account. Status reads shell out, so they never happen on
a render path — a render takes whatever is cached (possibly nothing, which renders
as nothing) and asks for a refresh in the background.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .models import Account
from .providers.claude_code.auth import AuthStatus, LoginFlow, read_status

log = logging.getLogger(__name__)

CLAUDE_PROVIDER_ID = "claude_code"


class AuthWatch:
    """Cached ``claude auth status`` per account + the login flow for fixing it."""

    # A login does not break often, and each check is a subprocess. Five minutes
    # keeps the deck honest without making every dashboard render pay for it.
    TTL_S = 300.0

    def __init__(self, *, ttl_s: float | None = None, reader=read_status) -> None:
        self._ttl_s = self.TTL_S if ttl_s is None else ttl_s
        self._reader = reader
        self._status: dict[str, AuthStatus] = {}
        self._flows: dict[str, LoginFlow] = {}
        self._refreshing: set[str] = set()
        # asyncio keeps only weak references to scheduled tasks. A refresh is
        # deliberately fire-and-forget from the render path, so the watch owns
        # it until completion rather than letting GC silently cancel the work.
        self._refresh_tasks: set[asyncio.Task[None]] = set()

    # --- status ---

    def status(self, account_key: str) -> AuthStatus | None:
        return self._status.get(account_key)

    def stale(self, account_key: str, *, now: datetime | None = None) -> bool:
        status = self._status.get(account_key)
        if status is None or status.checked_at is None:
            return True
        moment = now or datetime.now(UTC)
        return (moment - status.checked_at).total_seconds() >= self._ttl_s

    def refresh(self, account: Account) -> AuthStatus:
        """Read this account's status now (blocking — call it off the event loop)."""
        status = self._reader(Path(account.root))
        self._status[account.key] = status
        return status

    async def refresh_async(self, account: Account) -> AuthStatus:
        return await asyncio.to_thread(self.refresh, account)

    def request_refresh(self, accounts: Iterable[Account]) -> None:
        """Schedule refreshes for stale accounts without making anyone wait.

        Called from render paths, so it must never block and never raise: a missing
        event loop (a synchronous render in a test) simply means no refresh.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for account in accounts:
            if account.provider_id != CLAUDE_PROVIDER_ID:
                continue
            if account.key in self._refreshing or not self.stale(account.key):
                continue
            self._refreshing.add(account.key)
            task = loop.create_task(self._refresh_once(account))
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)

    async def _refresh_once(self, account: Account) -> None:
        try:
            await self.refresh_async(account)
        except Exception as exc:  # noqa: BLE001 — a health check must not break a render
            log.debug("claude auth status failed for %s: %s", account.key, exc)
        finally:
            self._refreshing.discard(account.key)

    # --- the login flow ---

    def flow(self, account_key: str) -> LoginFlow | None:
        return self._flows.get(account_key)

    def start_login(self, account: Account) -> LoginFlow:
        """Begin (or restart) the login for one account. Blocking; run in a thread."""
        existing = self._flows.get(account.key)
        if existing is not None and existing.active:
            # Starting a second login would create a second PKCE challenge and make
            # the code the operator is holding invalid. Cancel first, deliberately.
            existing.cancel()
        flow = LoginFlow(account_key=account.key, config_dir=Path(account.root))
        self._flows[account.key] = flow
        flow.start()
        return flow

    def submit_code(self, account: Account, code: str) -> LoginFlow:
        flow = self._flows.get(account.key)
        if flow is None:
            raise KeyError(account.key)
        flow.submit_code(code)
        if flow.status is not None:
            self._status[account.key] = flow.status
        return flow

    def forget(self, account_key: str) -> None:
        flow = self._flows.pop(account_key, None)
        if flow is not None:
            flow.cancel()
