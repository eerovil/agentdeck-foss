"""The deck's public address, and the file-link guidance built on it (#1838)."""

from __future__ import annotations

import json

from agentdeck.providers.instructions import (
    agentdeck_session_instructions,
    file_presentation_instructions,
)
from agentdeck.public_origin import (
    ORIGIN_FILE_ENV,
    PUBLIC_URL_ENV,
    forget_recorded_origin,
    normalize_origin,
    public_origin,
    public_url,
    record_public_origin,
)


def test_loopback_is_never_a_public_address(tmp_path):
    """A link to 127.0.0.1 opens the reader's own machine, not this deck."""
    for value in ("http://127.0.0.1:8756/", "http://localhost:8756", "http://[::1]:8756"):
        assert normalize_origin(value) == ""
        assert record_public_origin(value, path=tmp_path / "o.json") == ""
    assert not (tmp_path / "o.json").exists()


def test_unusable_values_are_dropped():
    for value in ("", None, "deck.example", "ftp://deck.example", "https://"):
        assert normalize_origin(value) == ""


def test_an_observed_address_survives_for_another_process(tmp_path):
    """The web process sees the request; the runtime starts the sessions."""
    store = tmp_path / "origin.json"
    recorded = record_public_origin("https://deck.example/chats/abc?x=1", path=store)

    assert recorded == "https://deck.example"
    assert json.loads(store.read_text())["origin"] == "https://deck.example"
    forget_recorded_origin()
    assert public_origin(path=store) == "https://deck.example"
    assert public_url("/var/home/me/p/worker.py:42", path=store) == (
        "https://deck.example/var/home/me/p/worker.py:42"
    )


def test_a_stated_address_wins_over_an_observed_one(tmp_path, monkeypatch):
    store = tmp_path / "origin.json"
    record_public_origin("https://observed.example", path=store)
    monkeypatch.setenv(PUBLIC_URL_ENV, "https://stated.example/")

    assert public_origin(path=store) == "https://stated.example"


def test_the_port_is_part_of_the_address(tmp_path):
    assert normalize_origin("http://deck.example:8756/anything") == "http://deck.example:8756"


def test_rewriting_the_same_address_does_not_rewrite_the_file(tmp_path):
    store = tmp_path / "origin.json"
    record_public_origin("https://deck.example", path=store)
    # A sentinel the recorder would overwrite if it wrote on every request.
    store.write_text(json.dumps({"origin": "https://sentinel.example"}))
    record_public_origin("https://deck.example", path=store)

    assert json.loads(store.read_text())["origin"] == "https://sentinel.example"


def test_the_guide_names_the_address_when_the_deck_knows_it():
    known = file_presentation_instructions("https://deck.example")

    assert "https://deck.example/home/me/project/worker.py:42" in known
    assert ":line[:column]" in known
    # The session URL is exactly what a poller-started worker does not have.
    assert "public session URL" not in known
    assert "AGENTDECK_URL" in known


def test_the_guide_falls_back_when_no_address_is_known():
    unknown = file_presentation_instructions("")

    assert "agentdeck.example" in unknown
    assert "public session URL" in unknown


def test_a_session_guide_carries_the_address(tmp_path, monkeypatch):
    store = tmp_path / "origin.json"
    monkeypatch.setenv(ORIGIN_FILE_ENV, str(store))
    record_public_origin("https://deck.example", path=store)

    resolved = agentdeck_session_instructions(role="work", provider="claude_code")
    stated = agentdeck_session_instructions(
        role="work", provider="claude_code", origin="https://other.example"
    )

    assert "https://deck.example/home/me/project/worker.py:42" in resolved
    assert "https://other.example/home/me/project/worker.py:42" in stated


async def test_a_request_teaches_the_deck_its_address_and_a_worker_reads_it(tmp_path):
    """The one lookup an owned worker already makes now carries the address."""
    from test_web import _app_with_state, _client

    app = _app_with_state(tmp_path)
    session_key = "claude_code:test:sid1"
    app.state.app_state.bind_session_token("tok-1838", session_key)

    async with _client(app) as client:
        # A full URL so the request really arrives as this host, the way a
        # browser on the overlay reaches the deck.
        await client.get("http://deck.example/")
        # The worker asks over loopback, which teaches the deck nothing and
        # must not displace the address a browser arrived on.
        lookup = await client.get("http://127.0.0.1:8756/api/sessions/by-token/tok-1838")

    payload = lookup.json()
    assert payload["public_origin"] == "http://deck.example"
    assert payload["public_session_url"] == f"http://deck.example{payload['session_url']}"


def test_naming_the_address_does_not_grow_the_guide():
    """Keeping token cost down was the ask, so the known case must not cost more."""
    known = agentdeck_session_instructions(
        role="primary", provider="claude_code", origin="https://deck.example"
    )
    unknown = agentdeck_session_instructions(role="primary", provider="claude_code", origin="")

    assert len(known) <= len(unknown)
