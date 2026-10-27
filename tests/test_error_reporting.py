"""Optional centralized error reporting (#1514).

The two things worth pinning here are that an unconfigured deck stays exactly as
it was, and that a configured one cannot export deck content.
"""

from __future__ import annotations

import logging

import pytest
import sentry_sdk

from agentdeck import error_reporting


@pytest.fixture(autouse=True)
def _forget_initialization(monkeypatch):
    monkeypatch.setattr(error_reporting, "_initialized_process", None)


def test_reporting_is_off_without_a_dsn(monkeypatch) -> None:
    monkeypatch.delenv(error_reporting.DSN_ENV, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(sentry_sdk, "init", lambda **options: calls.append(options))

    assert error_reporting.reporting_enabled() is False
    assert error_reporting.initialize_error_reporting("web") is False
    assert calls == []


def test_initialization_keeps_deck_content_on_the_host(monkeypatch) -> None:
    monkeypatch.setenv(error_reporting.DSN_ENV, "https://public@example.invalid/1")
    monkeypatch.setenv(error_reporting.ENVIRONMENT_ENV, "canary")
    options: dict = {}
    tags: dict = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: options.update(kwargs))
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda key, value: tags.update({key: value}))

    assert error_reporting.initialize_error_reporting("web-worker") is True

    assert options["environment"] == "canary"
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] == 0.0
    assert tags == {"agentdeck.process": "web-worker"}
    handler = options["integrations"][0]._handler
    assert handler.level == logging.ERROR


def test_a_broken_dsn_does_not_stop_the_process(monkeypatch, caplog) -> None:
    monkeypatch.setenv(error_reporting.DSN_ENV, "not-a-dsn")

    def explode(**_options):
        raise ValueError("bad dsn")

    monkeypatch.setattr(sentry_sdk, "init", explode)

    with caplog.at_level(logging.ERROR):
        assert error_reporting.initialize_error_reporting("web") is False
    assert "continuing without it" in caplog.text


def test_missing_sdk_is_reported_but_not_fatal(monkeypatch, caplog) -> None:
    monkeypatch.setenv(error_reporting.DSN_ENV, "https://public@example.invalid/1")
    monkeypatch.setattr(error_reporting, "sentry_sdk", None)

    with caplog.at_level(logging.WARNING):
        assert error_reporting.initialize_error_reporting("web") is False
    assert "sentry-sdk is not installed" in caplog.text


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abcdefghijklmnop",
        "sk-ant-api03-abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-abcdefghijklmnop",
    ],
)
def test_credentials_never_survive_redaction(secret: str) -> None:
    redacted = error_reporting.redact(f"failed with {secret} while reading")
    assert "[redacted]" in redacted
    assert secret.split()[-1] not in redacted


def test_before_send_scrubs_nested_strings_and_credential_keys() -> None:
    event = {
        "message": "sent sk-ant-api03-abcdefghijklmnop upstream",
        "request": {
            "headers": {"Authorization": "Bearer abcdefghijklmnop", "Accept": "text/html"},
            "cookies": {"session": "whatever"},
        },
        "exception": {
            "values": [{"value": "token ghp_abcdefghijklmnopqrstuvwxyz012345 rejected"}]
        },
    }

    scrubbed = error_reporting._before_send(event, {})

    assert "sk-ant" not in scrubbed["message"]
    assert scrubbed["request"]["headers"]["Authorization"] == "[redacted]"
    assert scrubbed["request"]["headers"]["Accept"] == "text/html"
    assert scrubbed["request"]["cookies"] == "[redacted]"
    assert "ghp_" not in scrubbed["exception"]["values"][0]["value"]


def test_browser_reports_are_bounded_and_redacted(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        summary = error_reporting.record_browser_error(
            kind="unhandledrejection",
            message="boom sk-ant-api03-abcdefghijklmnop",
            source="/static/sse.js",
            line=12,
            column=3,
            stack="x" * (error_reporting.MAX_FIELD_CHARS + 500),
            page="/sessions/claude_code:main:abc",
            user_agent="Mozilla/5.0",
        )

    assert summary.startswith("browser unhandledrejection on /sessions/claude_code:main:abc")
    assert "sk-ant" not in summary
    assert "sk-ant" not in caplog.text
    assert "/static/sse.js:12:3" in caplog.text
    assert len(max(caplog.text.split("\n"), key=len)) < error_reporting.MAX_FIELD_CHARS + 200
