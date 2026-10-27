"""Shared transport for the two web-process facades that drive the persistent
runtime over its Unix socket.

Both the Codex runtime client and the Claude worker client speak the same small
wire dialect to the same socket: a reopenable httpx client bound to the runtime's
UDS, a ``POST {/account-base}/{action}`` that returns an ``InjectResult`` wire
envelope, and a graceful close-on-cancel so a web deploy can shut down while the
separate runtime keeps processing. That transport, the reopen lifecycle, and the
envelope decode live here once; a subclass supplies only its ``_base`` prefix,
its ``refresh`` of runtime-owned state, and its ``_runtime_label`` for messages.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..action_context import current_client_action_id
from ..models import InjectResult
from ..runtime_socket import runtime_socket_path  # noqa: F401 — re-exported

log = logging.getLogger(__name__)

# The errors a request meets when its connection breaks before any answer comes
# back: the socket could not be opened, or it was closed under the request. A
# timeout is not among them — that request may still be running — and neither is
# an error status, which is the runtime's own answer.
_CONNECTION_LOST = (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)


class RuntimeSocketClient:
    """A reopenable httpx client over the runtime's Unix socket.

    Subclasses set ``_runtime_label`` (used in wire-decode and error messages)
    and implement ``_base`` (the per-account URL prefix) and ``refresh`` (re-read
    runtime-owned state after a mutating post)."""

    _runtime_label = "runtime"
    # How long a fenced send waits for a restarting runtime to listen again. A
    # deploy that restarts the runtime leaves its socket missing until startup
    # completes, which took 57 seconds on 2026-10-26; a person's message sent in
    # that gap failed with "[Errno 2] No such file or directory", and so did
    # Send again (#3567).
    RUNTIME_RESTART_WAIT_S = 180.0
    _RESTART_POLL_S = 1.0

    def __init__(
        self,
        account,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account = account
        # A cancelled _post() closes self._http so uvicorn can shut down promptly,
        # but a still-running web process must be able to reopen it. Keep a factory
        # so any call can lazily rebuild a closed client; otherwise a later refresh
        # would die silently and the cached runtime state would freeze forever.
        self._new_transport = (
            (lambda: transport)
            if transport is not None
            else (
                lambda: httpx.AsyncHTTPTransport(
                    uds=str(runtime_socket_path()),
                    # The runtime's uvicorn closes an idle connection after 5
                    # seconds and httpx keeps one for 5 seconds too, so a send
                    # made just under 5 seconds after the last one could go out on
                    # a connection the runtime was closing. The runtime never read
                    # it, and the message was lost with "Server disconnected
                    # without sending a response" (#2662). Dropping idle
                    # connections well before the runtime does closes that window
                    # from this side, without a runtime restart. Set here, on the
                    # transport: a client given its own transport ignores limits.
                    limits=httpx.Limits(keepalive_expiry=2.0),
                )
            )
        )
        self._http = self._make_client()

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._new_transport(),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )

    def _client(self) -> httpx.AsyncClient:
        # Reopen after a shutdown-path aclose() so the live process keeps syncing.
        if self._http.is_closed:
            self._http = self._make_client()
        return self._http

    @property
    def _base(self) -> str:
        raise NotImplementedError

    async def refresh(self):
        raise NotImplementedError

    async def _post(self, action: str, payload: dict, *, fence: str | None = None) -> InjectResult:
        return await self._post_path(f"{self._base}/{action}", payload, fence=fence)

    async def _post_path(
        self, path: str, payload: dict, *, fence: str | None = None
    ) -> InjectResult:
        """POST one action, decode the runtime's InjectResult envelope, then
        refresh runtime-owned state. Closes the socket (and re-raises) on cancel
        so a web-service shutdown does not block on an in-flight turn.

        ``fence`` names the payload field the runtime de-duplicates this action
        on. When it is set and carries an id, a request whose connection broke
        before any answer is posted once more, unchanged (#2662). That is safe
        only because of the runtime's delivery receipts: a send it finished
        replays its result, one whose write had begun comes back ``uncertain``
        and is never written again, and one it never received is delivered now.
        Anything without a fence id stays a single attempt.

        A repost that cannot even connect means the runtime is not listening,
        as while it restarts, so nothing reached it: that one keeps trying every
        second for up to ``RUNTIME_RESTART_WAIT_S`` before the send fails.
        """
        client_action_id = current_client_action_id()
        if client_action_id:
            payload = {**payload, "client_action_id": client_action_id}
        try:
            try:
                response = await self._client().post(path, json=payload)
            except _CONNECTION_LOST as exc:
                if not (fence and payload.get(fence)):
                    raise
                log.warning(
                    "%s connection lost on %s, posting %s %s once more: %s",
                    self._runtime_label,
                    path,
                    fence,
                    payload[fence],
                    exc,
                )
                response = await self._post_when_listening(path, payload)
            response.raise_for_status()
            result = InjectResult.from_wire(response.json(), source=self._runtime_label)
            await self.refresh()
            return result
        except asyncio.CancelledError:
            # A web deploy cancels InjectionService tasks that may be awaiting a
            # queued turn for minutes. Close the local UDS client immediately; the
            # separate runtime keeps processing the request while uvicorn finishes
            # its shutdown.
            await self._http.aclose()
            raise
        except httpx.HTTPError as exc:
            return InjectResult(False, f"{self._runtime_label} unavailable: {exc}")

    async def _post_when_listening(self, path: str, payload: dict) -> httpx.Response:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RUNTIME_RESTART_WAIT_S
        while True:
            try:
                return await self._client().post(path, json=payload)
            except httpx.ConnectError:
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(self._RESTART_POLL_S)

    async def worker_version(self, provider: str, session_id: str) -> float | None:
        try:
            response = await self._client().get(
                f"/workers/accounts/{provider}/{self.account.label}/session-version",
                params={"session_id": session_id},
            )
            response.raise_for_status()
            value = response.json().get("last_result_at")
            return float(value) if isinstance(value, (int, float)) else None
        except (httpx.HTTPError, TypeError, ValueError):
            return None

    async def delivery_spent(self, provider: str, session_id: str, delivery_id: str) -> bool:
        """Whether the runtime already holds a durable receipt for this delivery.

        The fence that collapses a repeat lives with the persistent runtime and is
        disk-backed, so this is the only answer that survives everything the web
        process does not — including being cancelled mid-send by a frontend
        deploy while the runtime carries the request through to the provider
        (#2003). Read-only: it reconciles, it never arms anything.

        False on any doubt. An unreachable runtime, an unparseable answer, or a
        worker the runtime no longer holds must read as "not spent", because the
        harm of guessing spent is suppressing a chat that really was given work,
        which nothing later corrects.

        The receipt is stored against the runtime's own **worker key**, not the
        session id — the hosts look it up with `_records.get(key)`. Asking with a
        session id is not an error, it is a quiet miss: the endpoint answers 200
        with no receipt, so the fence goes unseen exactly when it matters.
        """
        key = self.worker_key(session_id) if hasattr(self, "worker_key") else None
        if not key:
            return False
        try:
            response = await self._client().get(
                f"/workers/accounts/{provider}/{self.account.label}/delivery-receipt",
                params={"key": key, "delivery_id": delivery_id},
            )
            response.raise_for_status()
            return response.json().get("receipt") is not None
        except (httpx.HTTPError, TypeError, ValueError, AttributeError):
            return False

    async def stop(self) -> None:
        await self._http.aclose()
