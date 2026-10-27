"""Reconnectable client for the separate AgentDeck Codex runtime service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from ...models import (
    SNAPSHOT_CAPABILITIES_KEY,
    Account,
    ForkFrom,
    InjectResult,
    PendingInteraction,
)
from ..runtime_socket_client import RuntimeSocketClient, runtime_socket_path

__all__ = ["CodexRuntimeClient", "runtime_socket_path"]


def _interaction(raw: object) -> PendingInteraction | None:
    """The runtime publishes ``asdict(PendingInteraction)`` over the socket; the
    model owns the inverse (and honours ``multiselect``, which this used to drop)."""
    return PendingInteraction.from_dict(raw)


class CodexRuntimeClient(RuntimeSocketClient):
    """Provider-side facade over the long-lived runtime's Unix socket."""

    _runtime_label = "Codex runtime"

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, transport=transport)
        self._on_change = on_change or (lambda _thread_id: None)
        self._refresh_lock = asyncio.Lock()
        self._state: dict[str, dict[str, Any]] = {}
        self._workers: dict[str, dict[str, Any]] = {}
        self._capabilities: frozenset[str] = frozenset()
        # The exact bytes ``_state`` was built from, so a watcher re-reading this
        # snapshot every second can tell "nothing moved" without decoding it. See
        # the same guard in the Claude worker client (#1362).
        self._snapshot: bytes | None = None

    @property
    def _base(self) -> str:
        return f"/accounts/{self.account.label}"

    async def start(self) -> None:
        response = await self._client().get("/healthz")
        response.raise_for_status()
        await self.refresh()

    async def refresh(self) -> set[str]:
        async with self._refresh_lock:
            response = await self._client().get(f"{self._base}/state")
            response.raise_for_status()
            body = response.content
            if body == self._snapshot:
                return set()
            payload = response.json()
            raw = payload.get("threads", {})
            state = raw if isinstance(raw, dict) else {}
            raw_workers = payload.get("workers", {})
            workers = raw_workers if isinstance(raw_workers, dict) else {}
            raw_capabilities = payload.get(SNAPSHOT_CAPABILITIES_KEY, [])
            capabilities = frozenset(
                value for value in raw_capabilities if isinstance(value, str)
            )
            changed = {
                thread_id
                for thread_id in set(self._state) | set(state)
                if self._state.get(thread_id) != state.get(thread_id)
            }
            self._state = state
            self._workers = workers
            self._capabilities = capabilities
            self._snapshot = body
        for thread_id in changed:
            self._on_change(thread_id)
        return changed

    def owns(self, thread_id: str) -> bool:
        return thread_id in self._state

    def worker_key(self, thread_id: str) -> str | None:
        for key, worker in self._workers.items():
            if isinstance(worker, dict) and worker.get("session_id") == thread_id:
                return key
        return None

    def supports(self, capability: str) -> bool:
        return capability in self._capabilities

    def owned_thread_ids(self) -> frozenset[str]:
        """Stable identity union for scans whose ordinary rollout list is capped."""
        return frozenset(self._state)

    def active_turn(self, thread_id: str) -> str | None:
        value = self._state.get(thread_id, {}).get("active_turn")
        return value if isinstance(value, str) else None

    def thread_status(self, thread_id: str) -> str:
        value = self._state.get(thread_id, {}).get("status")
        return value if isinstance(value, str) else "notLoaded"

    def interaction(self, thread_id: str) -> PendingInteraction | None:
        return _interaction(self._state.get(thread_id, {}).get("interaction"))

    def permission_prompts_allowed(self, thread_id: str) -> bool:
        return bool(self._state.get(thread_id, {}).get("permission_prompts"))

    def permission_prompts_available(self, thread_id: str) -> bool:
        return bool(self._state.get(thread_id, {}).get("permission_prompts_available"))

    async def start_thread(
        self,
        cwd: Path,
        message: str,
        *,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        delegated: bool = False,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> InjectResult:
        payload: dict[str, object] = {
            "cwd": str(cwd),
            "message": message,
            "images": [str(path) for path in images or []],
            "sandbox": sandbox,
            "model": model,
            "effort": effort,
            "approval_policy": approval_policy,
            "delegated": delegated,
            "session_token": session_token,
        }
        if fork_from is not None:
            payload["fork_thread_id"] = fork_from.session_id
            payload["fork_turn_id"] = fork_from.ref
        return await self._post("start", payload)

    async def queue_turn(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
        effort: str | None = None,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
    ) -> InjectResult:
        payload = {
            "thread_id": thread_id,
            "message": message,
            "images": [str(path) for path in images or []],
            "effort": effort,
        }
        if require_idle:
            return await self._post_path(
                f"/workers/accounts/codex/{self.account.label}/revive-session",
                {
                    "session_id": thread_id,
                    "message": message,
                    "images": payload["images"],
                    "effort": effort,
                    "expected_last_result_at": expected_last_result_at,
                },
                fence="client_action_id",
            )
        return await self._post("queue", payload, fence="client_action_id")

    async def wait_for_thread(self, thread_id: str) -> InjectResult:
        return await self._post("wait", {"thread_id": thread_id})

    async def compact(self, thread_id: str) -> InjectResult:
        return await self._post("compact", {"thread_id": thread_id})

    async def set_permission_prompts(
        self, thread_id: str, enabled: bool
    ) -> InjectResult:
        return await self._post(
            "permission-prompts", {"thread_id": thread_id, "enabled": enabled}
        )

    async def steer(
        self, thread_id: str, message: str, *, images: list[Path] | None = None
    ) -> InjectResult:
        return await self._post(
            "steer",
            {
                "thread_id": thread_id,
                "message": message,
                "images": [str(path) for path in images or []],
            },
            fence="client_action_id",
        )

    async def interrupt(self, thread_id: str) -> InjectResult:
        return await self._post("interrupt", {"thread_id": thread_id})

    async def answer(
        self,
        thread_id: str,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None = None,
    ) -> InjectResult:
        return await self._post(
            "answer",
            {
                "thread_id": thread_id,
                "interaction_id": interaction_id,
                "answers": dict(answers),
                "decision": decision,
            },
        )
