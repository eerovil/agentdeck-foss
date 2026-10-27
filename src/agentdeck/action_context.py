"""Correlation context shared by web actions and background runtime calls."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_client_action_id: ContextVar[str | None] = ContextVar(
    "agentdeck_client_action_id", default=None
)


def current_client_action_id() -> str | None:
    return _client_action_id.get()


@contextmanager
def client_action_context(client_action_id: str) -> Iterator[None]:
    token = _client_action_id.set(client_action_id)
    try:
        yield
    finally:
        _client_action_id.reset(token)
