"""Ephemeral, transcript-free secrets for commands run by owned sessions.

The persistent runtime owns values only in memory.  The web process registers a
value under one stable browser action, then delivers only the returned opaque
ticket to the provider.  ``agentdeck secret-run`` resolves that ticket over the
local runtime socket and injects the value into one child command.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_SECRET_CHARS = 65_536
_SECRET_TTL_S = 3600.0
_CONTROL_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "NODE_OPTIONS",
    "PATH",
    "PERL5OPT",
    "PYTHONHOME",
    "PYTHONPATH",
    "RUBYOPT",
}
_CONTROL_ENV_PREFIXES = ("DYLD_", "GIT_CONFIG_", "LD_")


class SecretCommandError(ValueError):
    """A message intended as ``/secret`` is malformed."""


class SecretRuntimeError(RuntimeError):
    """The private runtime store could not issue or claim a secret."""


@dataclass(frozen=True)
class SecretCommand:
    name: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class SessionSecret:
    ticket: str
    session_key: str
    name: str
    value: str = field(repr=False)
    action_id: str
    expires_at: float


@dataclass(frozen=True)
class SecretTicket:
    ticket: str
    name: str
    fingerprint: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True)
class IssuedSecret:
    ticket: str
    name: str


def parse_secret_command(message: str) -> SecretCommand | None:
    """Parse ``/secret NAME VALUE`` without ever including VALUE in an error."""
    if not message.startswith("/secret"):
        return None
    if len(message) > len("/secret") and not message[len("/secret")].isspace():
        return None
    parts = message.split(maxsplit=2)
    if len(parts) == 1:
        raise SecretCommandError("Use /secret NAME VALUE")
    name = parts[1]
    value = parts[2] if len(parts) == 3 else ""
    validate_secret(name, value)
    return SecretCommand(name=name, value=value)


def validate_secret(name: str, value: str) -> None:
    """Validate the runtime boundary as well as the human command parser."""
    if not _ENV_NAME_RE.fullmatch(name):
        raise SecretCommandError(
            "Secret names must look like environment variables, for example OPENAI_API_KEY"
        )
    if name in _CONTROL_ENV_NAMES or name.startswith(_CONTROL_ENV_PREFIXES):
        raise SecretCommandError("That name controls program execution and is not allowed")
    if not value:
        raise SecretCommandError("The secret value cannot be empty")
    if "\x00" in value or len(value) > _MAX_SECRET_CHARS:
        raise SecretCommandError("The secret value is not supported")


def secret_notice(name: str, ticket: str) -> str:
    """Provider-visible instructions containing a name and ticket, never a value."""
    return (
        f"A temporary secret named {name} is available for this session. "
        "Its value was not included in this message. To give it to one command, run "
        f"`agentdeck secret-run {ticket} -- <command>`. The runner sets {name} only "
        "for that command and removes exact copies of the value from stdout and stderr. "
        # The store pops on claim, so a second run fails with no way to tell it
        # apart from a broken ticket (#1171). Say so, or an agent plans around a
        # ticket it can only spend once and reports the reuse failure as a bug.
        "The ticket is good for exactly one run and expires an hour after it was "
        "entered; running it again fails, and getting the secret back then needs a "
        "fresh /secret from the person who sent it. Make the one run count. "
        "Do not print, transform, or otherwise expose the secret."
    )


class SessionSecretStore:
    """Runtime-memory store with stable retry results per browser action."""

    def __init__(
        self,
        *,
        ttl_s: float = _SECRET_TTL_S,
        max_pending: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._by_ticket: dict[str, SessionSecret] = {}
        self._by_action: dict[tuple[str, str], SecretTicket] = {}
        self._ttl_s = ttl_s
        self._max_pending = max_pending
        self._clock = clock

    @staticmethod
    def _fingerprint(name: str, value: str) -> str:
        return hashlib.sha256(f"{name}\0{value}".encode()).hexdigest()

    def register(self, *, session_key: str, action_id: str, name: str, value: str) -> SecretTicket:
        validate_secret(name, value)
        now = self._clock()
        self._purge(now)
        fingerprint = self._fingerprint(name, value)
        action_key = (session_key, action_id)
        existing = self._by_action.get(action_key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise ValueError("secret action payload changed")
            return existing
        if len(self._by_ticket) >= self._max_pending:
            raise ValueError("too many temporary secrets are pending")
        ticket = secrets.token_urlsafe(32)
        expires_at = now + self._ttl_s
        stored = SessionSecret(
            ticket=ticket,
            session_key=session_key,
            name=name,
            value=value,
            action_id=action_id,
            expires_at=expires_at,
        )
        reference = SecretTicket(
            ticket=ticket,
            name=name,
            fingerprint=fingerprint,
            expires_at=expires_at,
        )
        self._by_ticket[ticket] = stored
        self._by_action[action_key] = reference
        return reference

    def claim(self, ticket: str) -> SessionSecret | None:
        """Return and forget one value; a ticket authorizes exactly one command."""
        self._purge(self._clock())
        return self._by_ticket.pop(ticket, None)

    def discard(self, ticket: str) -> bool:
        """Forget a value after a delivery was conclusively rejected."""
        stored = self._by_ticket.pop(ticket, None)
        if stored is None:
            return False
        self._by_action.pop((stored.session_key, stored.action_id), None)
        return True

    def _purge(self, now: float) -> None:
        expired = {
            action_key
            for action_key, reference in self._by_action.items()
            if reference.expires_at <= now
        }
        for action_key in expired:
            reference = self._by_action.pop(action_key)
            self._by_ticket.pop(reference.ticket, None)


async def issue_runtime_secret(
    socket_path: Path,
    *,
    session_key: str,
    action_id: str,
    name: str,
    value: str,
) -> IssuedSecret:
    """Issue one opaque ticket over the runtime's private Unix socket."""
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentdeck-runtime", timeout=10.0
        ) as client:
            response = await client.post(
                "/session-secrets",
                json={
                    "session_key": session_key,
                    "client_action_id": action_id,
                    "name": name,
                    "value": value,
                },
            )
            response.raise_for_status()
            payload = response.json()
        ticket = payload.get("ticket")
        issued_name = payload.get("name")
        if not isinstance(ticket, str) or not isinstance(issued_name, str):
            raise ValueError("invalid runtime response")
        return IssuedSecret(ticket=ticket, name=issued_name)
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise SecretRuntimeError("temporary secret storage is unavailable") from exc


async def revoke_runtime_secret(socket_path: Path, ticket: str) -> None:
    """Best-effort cleanup after the provider definitively rejects a notice."""
    transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentdeck-runtime", timeout=10.0
        ) as client:
            response = await client.post("/session-secrets/revoke", json={"ticket": ticket})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SecretRuntimeError("temporary secret cleanup is unavailable") from exc


def claim_runtime_secret(socket_path: Path, ticket: str) -> SessionSecret:
    """Claim one ticket without putting the capability in an access-log URL."""
    transport = httpx.HTTPTransport(uds=str(socket_path))
    try:
        with httpx.Client(
            transport=transport, base_url="http://agentdeck-runtime", timeout=10.0
        ) as client:
            response = client.post("/session-secrets/claim", json={"ticket": ticket})
            response.raise_for_status()
            payload = response.json()
        name = payload.get("name")
        value = payload.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("invalid runtime response")
        return SessionSecret(
            ticket=ticket,
            session_key="",
            name=name,
            value=value,
            action_id="",
            expires_at=0.0,
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        raise SecretRuntimeError("temporary secret is unavailable or already used") from exc
