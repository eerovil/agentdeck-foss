"""The public webhook hop retries a forward the deck could not take (agentdeck#2949)."""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "webhook_gateway.py"


@pytest.fixture(scope="module")
def gateway():
    spec = importlib.util.spec_from_file_location("webhook_gateway_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def deck():
    """A stand-in deck answering each POST with the next status in ``statuses``."""
    seen: list[tuple[bytes, str]] = []
    statuses: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append((body, self.headers.get("X-GitHub-Delivery", "")))
            status = statuses.pop(0) if statuses else 200
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # A short poll, so ``shutdown`` returns at once rather than after the default 0.5 s.
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", statuses, seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_busy_deck_is_tried_again_until_it_takes_the_delivery(gateway, deck):
    url, statuses, seen = deck
    statuses.extend([503, 502])
    waits: list[float] = []

    took = gateway.forward(
        url, b'{"a":1}', {"X-GitHub-Delivery": "d-1"}, delays=(5, 15, 45), sleep=waits.append
    )

    assert took is True
    assert seen == [(b'{"a":1}', "d-1")] * 3
    assert waits == [5, 15]


def test_a_refusal_is_not_retried(gateway, deck):
    url, statuses, seen = deck
    statuses.append(400)
    waits: list[float] = []

    took = gateway.forward(url, b"{}", {}, delays=(5, 15), sleep=waits.append)

    assert took is False
    assert len(seen) == 1
    assert waits == []


def test_an_unreachable_deck_is_dropped_after_the_last_try(gateway):
    waits: list[float] = []

    # Nothing listens on port 1, so every try is a refused connection.
    took = gateway.forward("http://127.0.0.1:1", b"{}", {}, delays=(1, 2), sleep=waits.append)

    assert took is False
    assert waits == [1, 2]


def test_the_default_schedule_is_five_tries(gateway):
    assert len(gateway.RETRY_DELAYS_S) == 4
    assert sum(gateway.RETRY_DELAYS_S) < 600
