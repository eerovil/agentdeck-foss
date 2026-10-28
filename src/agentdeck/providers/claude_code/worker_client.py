"""Web-process facade over the runtime service's deck-owned Claude workers.

The worker *processes* live in the long-lived runtime service; this client
reaches them over the same Unix socket the Codex runtime client uses. It keeps
a session-id → worker-key map (refreshed from the worker snapshot) so the
provider can light up inject/steer/interrupt for the sessions the deck owns.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

import httpx

from ... import runtime_feed
from ...models import SNAPSHOT_CAPABILITIES_KEY, Account, ForkFrom, InjectResult
from ..instructions import SessionRole
from ..runtime_socket_client import RuntimeSocketClient

log = logging.getLogger(__name__)

# How often a turn wait re-reads the snapshot when the runtime cannot say it moved (#2865).
TURN_POLL_S = 0.2


class ClaudeWorkerClient(RuntimeSocketClient):
    _runtime_label = "claude worker runtime"
    # How long the runtime may stay continuously unreachable during a turn wait
    # before the wait gives up. A long delegation polls thousands of times, so a
    # single dropped connection must not be mistaken for a failed turn.
    RUNTIME_UNREACHABLE_GRACE_S = 30.0
    _RETRY_INTERVAL_S = 1.0

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[], None] | None = None,
        on_session_token: Callable[[str, str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, transport=transport)
        # The change feed rides the runtime socket; a client given its own transport
        # (a test's in-process runtime) has none and waits on the old cadence.
        self._use_feed = transport is None
        self._on_change = on_change or (lambda: None)
        self._on_session_token = on_session_token or (lambda _token, _session_id: None)
        # session_id -> worker snapshot fields used by provider capabilities.
        self._owned: dict[str, dict] = {}
        # Which cut-off chats the runtime still owes a revival (#3008); None from a runtime
        # that does not say, which promises none.
        self._revival: dict | None = None
        # The exact bytes the owned map was last built from. The watcher re-reads
        # this snapshot every second forever, and it is byte-identical whenever no
        # worker moved — a quarter of a megabyte across two accounts here. Parsing
        # it only to discover the map did not change is pure waste, so the bytes
        # are compared first (#1362). Cleared whenever the runtime goes away, so a
        # reconnect always re-ingests rather than trusting a pre-restart body.
        self._snapshot: bytes | None = None
        self._capabilities: frozenset[str] = frozenset()
        self.available = False
        # True while the last read failed without an answer — the runtime socket is
        # down, as while it restarts. An answer that refuses (workers switched off,
        # unknown account) clears it, so only a reachable-again runtime is offered
        # in the new-chat forms while unavailable (#2975).
        self.unreachable = False
        # Whether the last read got a real answer: the snapshot, or a deliberate refusal
        # (workers off, unknown account). No socket or a 5xx is not one (#3487).
        self.answered = False

    @property
    def _base(self) -> str:
        return f"/claude/accounts/{self.account.label}"

    async def probe(self) -> bool:
        """Return True iff the runtime serves Claude workers for this account
        (workers enabled + account known); populates the owned map on success."""
        try:
            response = await self._client().get(f"{self._base}/workers")
        except httpx.HTTPError:
            self.unreachable = True
            self._snapshot = None
            if self.available:
                self.available = False
                self._on_change()
            return False
        self.unreachable = False
        self.answered = response.status_code < 500
        available = response.status_code == 200
        changed = available != self.available
        self.available = available
        if self.available:
            self._ingest(response.json())
            self._snapshot = response.content
        else:
            self._snapshot = None
        if changed:
            self._on_change()
        return self.available

    async def refresh(self) -> bool:
        """Re-read the worker snapshot; return True if the owned map changed."""
        try:
            response = await self._client().get(f"{self._base}/workers")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.unreachable = not isinstance(exc, httpx.HTTPStatusError)
            self.answered = (
                isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
            )
            self._snapshot = None
            if self.available:
                self.available = False
                self._on_change()
            return False
        self.unreachable = False
        self.answered = True
        availability_changed = not self.available
        self.available = True
        body = response.content
        # Ingesting bytes identical to the last ingested ones can only rebuild
        # the map already held and re-bind tokens already bound, both of which
        # are no-ops. Skipping the decode is therefore the same answer (#1362).
        if body == self._snapshot and not availability_changed:
            return False
        changed = self._ingest(response.json())
        self._snapshot = body
        if availability_changed:
            self._on_change()
        return changed or availability_changed

    def _ingest(self, data: object) -> bool:
        owned: dict[str, dict] = {}
        workers = data.get("workers", {}) if isinstance(data, dict) else {}
        raw_capabilities = (
            data.get(SNAPSHOT_CAPABILITIES_KEY, []) if isinstance(data, dict) else []
        )
        capabilities = frozenset(
            value for value in raw_capabilities if isinstance(value, str)
        )
        if isinstance(workers, dict):
            for key, worker in workers.items():
                if not isinstance(worker, dict):
                    continue
                session_id = worker.get("session_id")
                if isinstance(session_id, str):
                    session_token = worker.get("session_token")
                    if isinstance(session_token, str) and session_token:
                        self._on_session_token(session_token, session_id)
                    pending = worker.get("pending_interaction")
                    owned[session_id] = {
                        "key": key,
                        "cwd": worker.get("cwd"),
                        "live": bool(worker.get("live")),
                        "turn_active": bool(worker.get("turn_active")),
                        # Newer runtimes include Claude-native background agents
                        # in effective activity. Fall back to the outer turn for
                        # compatibility while the web and runtime restart apart.
                        "effective_activity": bool(
                            worker.get(
                                "effective_activity", worker.get("turn_active")
                            )
                        ),
                        "stalled": bool(worker.get("stalled")),
                        # Why this chat's worker died, when the runtime could
                        # name it (#1546). Older runtimes omit it; absent means
                        # "nothing to say", never a wrong reason.
                        "exit_reason": str(worker.get("exit_reason") or ""),
                        "last_result_at": worker.get("last_result_at", 0.0),
                        # The durable "owes an answer" marker revival reads, and the
                        # stamps that can cancel it (#3008). Older runtimes omit them,
                        # which reads as no cut-off turn.
                        "turn_open_at": _stamp(worker.get("turn_open_at")),
                        "turn_closed_at": _stamp(worker.get("turn_closed_at")),
                        "last_delivery_at": _stamp(worker.get("last_delivery_at")),
                        "last_result_limit_reset_at": worker.get(
                            "last_result_limit_reset_at", 0.0
                        ),
                        "pending_interaction": pending if isinstance(pending, dict) else None,
                        # Older runtimes omit it; then a question shows only while the
                        # turn is inside it, as before #3451.
                        "open_questions": (
                            questions
                            if isinstance(questions := worker.get("open_questions"), dict)
                            else None
                        ),
                        "bypass_permissions": bool(worker.get("bypass_permissions")),
                        # Older runtimes omit the field; absence means the switch
                        # is simply not offered yet, never that it is on.
                        "bypass_permissions_available": bool(
                            worker.get("bypass_permissions_available")
                        ),
                        # What a side chat of this one starts on (#2485).
                        "model": worker.get("model"),
                        "effort": worker.get("effort"),
                        # The role the deck started it with; ``work`` may move (#3362).
                        "role": worker.get("role"),
                    }
        revival = data.get("revival") if isinstance(data, dict) else None
        changed = (
            owned != self._owned
            or capabilities != self._capabilities
            or revival != self._revival
        )
        self._owned = owned
        self._capabilities = capabilities
        self._revival = revival if isinstance(revival, dict) else None
        if changed:
            self._on_change()
        return changed

    def owns(self, session_id: str) -> bool:
        return session_id in self._owned

    def worker_key(self, session_id: str) -> str | None:
        return self._key_for(session_id)

    def supports(self, capability: str) -> bool:
        return capability in self._capabilities

    def owned_session_ids(self) -> list[str]:
        return list(self._owned)

    def _key_for(self, session_id: str) -> str | None:
        entry = self._owned.get(session_id)
        return entry["key"] if entry else None

    def settings_for(self, session_id: str) -> tuple[str | None, str | None]:
        """The ``(model, effort)`` this owned worker was spawned with."""
        entry = self._owned.get(session_id) or {}
        model, effort = entry.get("model"), entry.get("effort")
        return (
            model if isinstance(model, str) and model else None,
            effort if isinstance(effort, str) and effort else None,
        )

    def cwd_for(self, session_id: str) -> str | None:
        entry = self._owned.get(session_id)
        cwd = entry.get("cwd") if entry else None
        return cwd if isinstance(cwd, str) else None

    def turn_active(self, session_id: str) -> bool:
        entry = self._owned.get(session_id)
        return bool(entry and entry["turn_active"])

    def turn_started_at(self, session_id: str) -> datetime | None:
        """When the runtime opened this chat's running turn, or None with no turn running.

        The stall clock of a turn starts here at the latest (#3110): a chat woken after a
        long idle has written nothing yet, and its newest transcript progress is the end of
        the turn before.
        """
        entry = self._owned.get(session_id)
        if not entry or not entry["turn_active"] or entry["turn_open_at"] <= 0.0:
            return None
        return datetime.fromtimestamp(entry["turn_open_at"], UTC)

    def effective_activity(self, session_id: str) -> bool:
        """Whether the outer turn or one of its native agents is still working."""
        entry = self._owned.get(session_id)
        return bool(entry and entry["effective_activity"])

    def live(self, session_id: str) -> bool:
        entry = self._owned.get(session_id)
        return bool(entry and entry["live"])

    def turn_cut_off(self, session_id: str) -> bool:
        """The chat's process is gone with a turn still owed: a runtime death cut it off.

        The same reading revival makes from the durable marker (``revival._survivor``):
        a turn opened and never answered, not ended on purpose since (#3008). Until the
        revival brings it back, or when the revival is refused, the chat has not ended
        its turn; nothing it did can be judged as an ending.
        """
        entry = self._owned.get(session_id)
        if not entry or entry["live"] or entry["turn_open_at"] <= 0.0:
            return False
        closed = entry["turn_closed_at"]
        return not (
            closed > 0.0 and closed >= max(entry["last_delivery_at"], entry["turn_open_at"])
        )

    def turn_cut_off_at(self, session_id: str) -> float:
        """When the cut-off turn was last fed, the stamp revival's own staleness window runs
        from (``revival._survivor``); ``0.0`` when the chat is not cut off (#3008)."""
        if not self.turn_cut_off(session_id):
            return 0.0
        entry = self._owned[session_id]
        return max(entry["turn_open_at"], entry["last_delivery_at"])

    def revival_due(self, session_id: str) -> bool:
        """Will the runtime still revive this cut-off chat by itself (#3008)?

        Only at its own boot: every cut-off chat until that boot's scan has run, then only
        the ones a full account turned away, which it keeps trying. A worker that died on its
        own while the runtime stayed up, or one the loop guard stopped, is owed nothing.
        """
        if not self.turn_cut_off(session_id) or self._revival is None:
            return False
        if not self._revival.get("scanned"):
            return True
        due = self._revival.get("due")
        return isinstance(due, list) and f"{self.account.key}:{session_id}" in due

    def role(self, session_id: str) -> str | None:
        """The role this chat's worker was started with, or None."""
        entry = self._owned.get(session_id)
        role = entry.get("role") if entry else None
        return role if isinstance(role, str) and role else None

    def exit_reason(self, session_id: str) -> str:
        """Why this chat's worker process last died, or "" when unremarkable."""
        entry = self._owned.get(session_id)
        return entry.get("exit_reason", "") if entry else ""

    def automatic_retry_at(self, session_id: str) -> datetime | None:
        entry = self._owned.get(session_id)
        value = entry.get("last_result_limit_reset_at", 0.0) if entry else 0.0
        if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
            return None
        try:
            return datetime.fromtimestamp(float(value), UTC)
        except (OverflowError, OSError, ValueError):
            return None

    async def deliver(
        self,
        key: str,
        message: str,
        *,
        cwd: str | None = None,
        fresh: bool = False,
        images: list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        delivery_id: str | None = None,
        allow_over_budget: bool = False,
        require_idle: bool = False,
        expected_last_result_at: float | None = None,
        delegated: bool = False,
        role: SessionRole | None = None,
        session_token: str | None = None,
        fork_from: ForkFrom | None = None,
    ) -> InjectResult:
        """Spawn primitive — addressed by the internal worker ``key`` because the
        session id does not exist until the runtime assigns one. Only
        ``start_session`` mints a key and calls this; follow-ups use ``send``."""
        payload = {
                "key": key,
                "message": message,
                "cwd": cwd,
                "fresh": fresh,
                "images": images or [],
                "model": model,
                "effort": effort,
                "permission_mode": permission_mode,
                "delivery_id": delivery_id,
                "allow_over_budget": allow_over_budget,
                "require_idle": require_idle,
                "delegated": delegated,
                "role": role,
                "session_token": session_token,
            }
        if expected_last_result_at is not None:
            payload["expected_last_result_at"] = expected_last_result_at
        if fork_from is not None:
            payload["fork_session_id"] = fork_from.session_id
            payload["fork_at"] = fork_from.ref
        return await self._post("deliver", payload, fence="delivery_id")

    async def send(
        self,
        session_id: str,
        message: str,
        *,
        images: list[str] | None = None,
        delivery_id: str | None = None,
        allow_over_budget: bool = False,
    ) -> InjectResult:
        """Deliver a follow-up to an already-owned worker, addressed by session id."""
        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self.deliver(
            key,
            message,
            images=images,
            delivery_id=delivery_id,
            allow_over_budget=allow_over_budget,
        )

    def turn_held(self, session_id: str) -> bool:
        """Whether the CLI is stuck on a prompt in this chat right now."""
        entry = self._owned.get(session_id)
        return bool(entry and isinstance(entry.get("pending_interaction"), dict))

    def pending_interaction(self, session_id: str) -> dict | None:
        """What the person is asked in this chat, normalized by the runtime.

        A permission prompt or a classifier block the CLI is stuck on comes first. Then
        the chat's open questions, which outlive the turn that asked them (#3451) and
        include the one a turn may be inside. An older runtime has no such list, so its
        live question is the answer.
        """
        entry = self._owned.get(session_id)
        if not entry:
            return None
        pending = entry.get("pending_interaction")
        pending = pending if isinstance(pending, dict) else None
        if pending is not None and pending.get("kind") != "question":
            return pending
        return entry.get("open_questions") or pending

    async def prepare_spare(
        self,
        shape: str,
        key: str,
        *,
        session_token: str,
        cwd: str,
        model: str | None,
        effort: str | None,
        role: str | None = None,
    ) -> str:
        """Ask the runtime to keep a CLI booted for this kind of new chat (#3420).

        Answers the runtime's word for what it did, or ``""`` when it could not be
        asked. Nothing depends on the answer: a start that finds no spare is cold.
        """
        try:
            response = await self._client().post(
                f"{self._base}/spare",
                json={
                    "shape": shape,
                    "key": key,
                    "session_token": session_token,
                    "cwd": cwd,
                    "model": model,
                    "effort": effort,
                    # Only a Work spare names one, so the other shapes ask exactly as before.
                    **({"role": role} if role else {}),
                },
            )
            response.raise_for_status()
            state = response.json().get("state")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.debug("could not ask for a spare %s on %s: %s", shape, self.account.key, exc)
            return ""
        return state if isinstance(state, str) else ""

    async def drop_spare(self, shape: str) -> str:
        """Ask the runtime to stop the CLI waiting for ``shape``; ``""`` if it could not."""
        try:
            response = await self._client().delete(
                f"{self._base}/spare", params={"shape": shape}
            )
            response.raise_for_status()
            state = response.json().get("state")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.debug("could not drop the spare %s on %s: %s", shape, self.account.key, exc)
            return ""
        return state if isinstance(state, str) else ""

    async def interrupt(self, session_id: str) -> InjectResult:
        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self._post("interrupt", {"key": key})

    async def interrupt_key(self, key: str) -> InjectResult:
        """Interrupt the worker started under ``key``, before its session id is known."""
        return await self._post("interrupt", {"key": key})

    async def rewind(
        self,
        session_id: str,
        at: str,
        *,
        chat_keys: Iterable[str] = (),
        since: float | None = None,
    ) -> InjectResult:
        """Roll the chat back so its next turn continues from line ``at`` (#3474), and
        cancel quietly the jobs ``chat_keys`` submitted from ``since`` on."""
        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self._post(
            "rewind", {"key": key, "at": at, "session_keys": list(chat_keys), "since": since}
        )

    def bypass_permissions_on(self, session_id: str) -> bool:
        entry = self._owned.get(session_id)
        return bool(entry and entry.get("bypass_permissions"))

    def bypass_permissions_available(self, session_id: str) -> bool:
        entry = self._owned.get(session_id)
        return bool(entry and entry.get("bypass_permissions_available"))

    async def set_bypass_permissions(
        self, session_id: str, enabled: bool
    ) -> InjectResult:
        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self._post("bypass-permissions", {"key": key, "enabled": enabled})

    async def answer(
        self,
        session_id: str,
        interaction_id: str,
        *,
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> InjectResult:
        key = self._key_for(session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await self._post(
            "answer",
            {
                "key": key,
                "interaction_id": interaction_id,
                "answers": answers,
                "decision": decision,
            },
        )

    async def wait_for_turn(self, session_id: str, *, timeout_s: float) -> InjectResult:
        """Wait until the owned worker's current turn finishes or exits.

        A poll error is not evidence that the turn failed. This loop polls the
        runtime for the whole of a delegation's lifetime, so over a long turn a
        single dropped keep-alive is close to certain; failing on the first one
        reported healthy, still-running work as a failed delegation. Tolerate
        errors for ``RUNTIME_UNREACHABLE_GRACE_S`` of *continuous* failure --
        long enough to ride out a blip, short enough that a genuinely dead
        runtime still surfaces quickly -- and reset the moment a poll succeeds.

        Between reads it waits for the runtime's change feed to say this account's
        workers moved (#2865), not 0.2 s; subscribed before the first read, so a turn
        ending during that read is still seen.
        """
        if not self._use_feed:
            return await self._wait_for_turn(session_id, timeout_s, None)
        item = runtime_feed.workers_item(self.account.provider_id, self.account.label)
        with runtime_feed.shared().subscribe(item) as changes:
            return await self._wait_for_turn(session_id, timeout_s, changes)

    async def _wait_for_turn(
        self,
        session_id: str,
        timeout_s: float,
        changes: runtime_feed.FeedSubscription | None,
    ) -> InjectResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        unreachable_since: float | None = None
        while True:
            try:
                response = await self._client().get(f"{self._base}/workers")
                response.raise_for_status()
            except httpx.HTTPError as exc:
                now = loop.time()
                if unreachable_since is None:
                    unreachable_since = now
                    log.warning(
                        "claude worker runtime poll failed for %s, retrying: %s",
                        session_id,
                        exc,
                    )
                if now - unreachable_since >= self.RUNTIME_UNREACHABLE_GRACE_S:
                    return InjectResult(
                        False, f"claude worker runtime unavailable: {exc}"
                    )
                remaining = deadline - now
                if remaining <= 0:
                    return InjectResult(
                        False, "Claude turn timed out", session_id=session_id
                    )
                await asyncio.sleep(min(self._RETRY_INTERVAL_S, remaining))
                continue
            unreachable_since = None
            self._ingest(response.json())
            entry = self._owned.get(session_id)
            if entry is None:
                return InjectResult(False, "claude worker session is no longer registered")
            if not entry["turn_active"]:
                return InjectResult(True, session_id=session_id)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return InjectResult(False, "Claude turn timed out", session_id=session_id)
            if changes is None:
                await asyncio.sleep(min(TURN_POLL_S, remaining))
            else:
                await changes.wait(TURN_POLL_S, timeout=remaining)


def _stamp(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
