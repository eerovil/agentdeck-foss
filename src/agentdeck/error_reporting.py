"""Optional centralized error reporting for AgentDeck processes (#1514).

AgentDeck's failures are journal-only. A workflow that deliberately turns its
own failure into state gets noticed; everything else lands in ``journalctl``,
and nothing watches ``journalctl``. So when the live deck reaches a *new* error
state, the honest answer to "who notices it?" has been "nobody, until somebody
happens to look."

This module gives every long-lived process one opt-in destination for those
failures: the web supervisor, its replaceable worker, the persistent runtime,
and uncaught browser errors the page forwards back to the server. It is inert
unless the operator sets ``AGENTDECK_SENTRY_DSN``, so a default deck behaves
exactly as before.

Privacy is the constraint that shapes the options below. AgentDeck holds
prompts, transcripts, and provider credentials, and none of that may leave the
host: local variables, request bodies, and PII are switched off, and every
string in an outgoing event is scrubbed for credential shapes first.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

try:  # pragma: no cover - exercised by the ImportError test
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
except ImportError:  # pragma: no cover - a checkout that has not been synced
    sentry_sdk = None  # type: ignore[assignment]
    LoggingIntegration = None  # type: ignore[assignment]

_LOG = logging.getLogger(__name__)

DSN_ENV = "AGENTDECK_SENTRY_DSN"
ENVIRONMENT_ENV = "AGENTDECK_SENTRY_ENVIRONMENT"
RELEASE_ENV = "AGENTDECK_SENTRY_RELEASE"

# Credential shapes that must never reach an external service, even when they
# only ever appear inside an exception message somebody logged by accident.
_SECRET_VALUE = re.compile(
    r"(?i)(?:Bearer\s+)[A-Za-z0-9._~+/=-]{8,}"
    r"|sk-(?:ant|proj|live|test)-[A-Za-z0-9._-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|xox[abposr]-[A-Za-z0-9-]{8,}"
)
# Keys whose value is a credential regardless of how it is spelled.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "secret",
        "session_token",
        "set-cookie",
        "token",
        "x-api-key",
    }
)
_REDACTED = "[redacted]"
# One browser report is a bounded thing on purpose: a loop in a page must not be
# able to post an unbounded stack trace, and a truncated trace still names the
# failing function.
MAX_FIELD_CHARS = 2000

_initialized_process: str | None = None


def reporting_enabled() -> bool:
    """Whether the operator opted into external error reporting."""

    return bool(os.environ.get(DSN_ENV, "").strip())


def redact(value: str) -> str:
    """Replace credential-shaped substrings in ``value``."""

    return _SECRET_VALUE.sub(_REDACTED, value)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively redact strings and drop credential-keyed values."""

    if depth > 12:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        scrubbed: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_KEYS:
                scrubbed[key] = _REDACTED
            else:
                scrubbed[key] = _scrub(item, depth + 1)
        return scrubbed
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, depth + 1) for item in value)
    return value


def _before_send(event: dict, _hint: dict) -> dict:
    """Last gate before an event leaves the host."""

    return _scrub(event)


def initialize_error_reporting(process: str) -> bool:
    """Start reporting for this process; return whether it is now active.

    Safe to call more than once and safe to call when the SDK is missing or no
    DSN is configured — in every one of those cases the process simply keeps
    logging to the journal as it always has.
    """

    global _initialized_process

    dsn = os.environ.get(DSN_ENV, "").strip()
    if not dsn:
        return False
    if sentry_sdk is None:
        _LOG.warning(
            "%s is set but sentry-sdk is not installed; error reporting is off", DSN_ENV
        )
        return False
    if _initialized_process is not None:
        return True

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=os.environ.get(ENVIRONMENT_ENV, "").strip() or "production",
            release=os.environ.get(RELEASE_ENV, "").strip() or _release(),
            # Everything below keeps deck content on the host. `send_default_pii`
            # off drops user identifiers and request cookies; local variables
            # would otherwise carry whole prompts and transcripts; request bodies
            # carry injected messages.
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            # This is failure reporting, not APM. Performance traces would sample
            # every dashboard poll and SSE stream for no diagnostic gain.
            traces_sample_rate=0.0,
            integrations=[
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
            ],
            before_send=_before_send,
        )
        sentry_sdk.set_tag("agentdeck.process", process)
    except Exception:
        # A misconfigured DSN must never stop the deck from starting.
        _LOG.exception("error reporting failed to initialize; continuing without it")
        return False

    _initialized_process = process
    _LOG.info("error reporting enabled for %s", process)
    return True


def _release() -> str:
    try:
        from importlib.metadata import version

        return f"agentdeck@{version('agentdeck')}"
    except Exception:  # pragma: no cover - only on a broken install
        return ""


def _bounded(value: object) -> str:
    text = "" if value is None else str(value)
    if len(text) > MAX_FIELD_CHARS:
        text = text[:MAX_FIELD_CHARS] + "…"
    return redact(text)


def record_browser_error(
    *,
    kind: str,
    message: str,
    source: str = "",
    line: int | None = None,
    column: int | None = None,
    stack: str = "",
    page: str = "",
    user_agent: str = "",
) -> str:
    """Log one uncaught browser failure so the server-side reporter sees it.

    Browser failures are the half of the deck no journal ever saw: a page that
    throws leaves nothing behind on the host at all. This writes one bounded,
    redacted ERROR line, which the logging integration forwards when reporting
    is configured and which is a normal journal line when it is not. Returns the
    logged summary so callers and tests can assert on exactly what was recorded.
    """

    where = _bounded(source)
    if where and line is not None:
        where = f"{where}:{line}" + (f":{column}" if column is not None else "")
    summary = "browser {kind} on {page}: {message}".format(
        kind=_bounded(kind) or "error",
        page=_bounded(page) or "unknown page",
        message=_bounded(message) or "(no message)",
    )
    _LOG.error(
        "%s (at %s, ua %s)\n%s",
        summary,
        where or "unknown location",
        _bounded(user_agent) or "unknown",
        _bounded(stack) or "(no stack)",
    )
    return summary
