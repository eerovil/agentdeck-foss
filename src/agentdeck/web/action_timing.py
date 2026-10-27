"""Low-noise timing and correlation for direct user actions."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import Request

log = logging.getLogger(__name__)

_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SESSION_ACTION_RE = re.compile(
    r"^/sessions/(?P<session_key>[^/]+)/(?P<action>inject|steer|interrupt|interaction)$"
)
_SESSION_NAVIGATION_RE = re.compile(r"^/sessions/(?P<session_key>[^/]+)$")
# A side chat (#2485) is a start like New chat, but cut from one leg's history, so the
# client's action ID is what makes a resent press find the start its first one made.
_SESSION_FORK_RE = re.compile(r"^/sessions/(?P<session_key>[^/]+)/fork$")
# Message this Work (#2218) is a send like the chat's own: the chat is chosen only once the
# route runs, and ``inject_message`` binds it, but the client's action ID must be taken here
# so a retried Work send reaches the queue under the ID its first attempt used.
_WORK_MESSAGE_RE = re.compile(r"^/works/.+/message$")


def _request_action(request: Request) -> tuple[str | None, str | None]:
    if request.method == "GET":
        match = _SESSION_NAVIGATION_RE.match(request.url.path)
        if match:
            action = (
                "open_session"
                if request.headers.get("x-agentdeck-session-navigation") == "detail"
                else "load_session"
            )
            return action, match.group("session_key")
    if request.method != "POST":
        return None, None
    match = _SESSION_ACTION_RE.match(request.url.path)
    if match:
        action = {
            "inject": "send",
            "steer": "steer",
            "interrupt": "stop",
            "interaction": "interaction",
        }[match.group("action")]
        return action, match.group("session_key")
    if request.url.path == "/sessions/new":
        return "new_session", None
    match = _SESSION_FORK_RE.match(request.url.path)
    if match:
        return "fork_session", match.group("session_key")
    # The scope's own path: a Work id carries a ``#``, which ``request.url`` reads as the
    # start of a fragment and cuts off.
    if _WORK_MESSAGE_RE.match(request.scope.get("path", "")):
        return "send", None
    return None, None


@dataclass
class ActionTiming:
    client_action_id: str
    action: str
    session_key: str | None
    started_ns: int = field(default_factory=time.perf_counter_ns)
    provider: str | None = None
    spans_ms: dict[str, float] = field(default_factory=dict)
    # Whether the client sent the id. One made up here is new on every attempt, so it cannot
    # make a retried write happen once (#2416).
    supplied: bool = False

    @classmethod
    def from_request(cls, request: Request, action: str, session_key: str | None):
        supplied = request.headers.get("x-agentdeck-action-id", "")
        if _ACTION_ID_RE.fullmatch(supplied):
            return cls(supplied, action, session_key, supplied=True)
        return cls(uuid.uuid4().hex, action, session_key)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            self.spans_ms[name] = (time.perf_counter_ns() - started) / 1_000_000

    def bind(self, *, session_key: str | None = None, provider: str | None = None) -> None:
        if session_key:
            self.session_key = session_key
        if provider:
            self.provider = provider

    def finish(self, status_code: int) -> tuple[str, float]:
        total_ms = (time.perf_counter_ns() - self.started_ns) / 1_000_000
        parts = [
            f"{name};dur={duration:.1f}"
            for name, duration in self.spans_ms.items()
        ]
        parts.append(f"total;dur={total_ms:.1f}")
        log.debug(
            "action_timing action_id=%s action=%s session_key=%s provider=%s "
            "status=%d elapsed_ms=%.1f spans_ms=%s",
            self.client_action_id,
            self.action,
            self.session_key,
            self.provider,
            status_code,
            total_ms,
            {name: round(value, 1) for name, value in self.spans_ms.items()},
        )
        return ", ".join(parts), total_ms


def action_timing(request: Request) -> ActionTiming | None:
    value = getattr(request.state, "action_timing", None)
    return value if isinstance(value, ActionTiming) else None


@contextmanager
def timing_span(request: Request, name: str) -> Iterator[None]:
    timing = action_timing(request)
    if timing is None:
        yield
        return
    with timing.span(name):
        yield


def bind_action(
    request: Request, *, session_key: str | None = None, provider: str | None = None
) -> str | None:
    timing = action_timing(request)
    if timing is None:
        return None
    timing.bind(session_key=session_key, provider=provider)
    return timing.client_action_id


def supplied_action_id(request: Request) -> str | None:
    """The client's own action id, or None when the request carried none."""
    timing = action_timing(request)
    return timing.client_action_id if timing is not None and timing.supplied else None


def identify_action(request: Request) -> tuple[str | None, str | None]:
    return _request_action(request)
