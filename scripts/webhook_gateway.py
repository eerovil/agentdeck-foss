#!/usr/bin/env python3
"""The public hop that forwards GitHub webhooks to a private AgentDeck host (#1601).

AgentDeck itself stays private. This is the only piece with a public address, so it is
written to be read in one sitting: standard library only, no dependencies, no state, and
no knowledge of boards, cards, repositories, or GitHub's API. It is a minimal descendant
of the gateway written for #1175 and removed in #1186, with that experiment's routing,
delivery ledger, and GitHub App concerns left out.

It does four things:

1. accepts ``POST`` on one path and nothing else;
2. verifies ``X-Hub-Signature-256`` over the raw body before anything else touches it;
3. answers GitHub immediately, forwarding on a background thread;
4. forwards the exact bytes and the GitHub headers to AgentDeck over the private hop.

Point 3 matters: GitHub gives a webhook ten seconds and marks a slower delivery failed.
The gateway must never wait for AgentDeck. A forward that fails for a reason that can pass
— a timeout, a dropped connection, a 5xx from a busy or restarting deck — is tried again on
that thread, five tries over about three minutes, and then dropped. Until then a failed
forward was dropped outright, because nothing acted on deliveries yet; the Work router now
starts work from a label delivery, and a busy deck lost those starts (agentdeck#2949). A
retry can store one delivery twice when the deck stored it but its answer was lost; the
deck's tailer skips a repeated ``X-GitHub-Delivery``, so consumers still see it once. A 4xx
is the deck refusing that delivery, and the same bytes would be refused again, so it is not
retried.

Run it as::

    AGENTDECK_WEBHOOK_SECRET=... AGENTDECK_WEBHOOK_TARGET=http://deck.internal:8756 \\
        python3 scripts/webhook_gateway.py --port 8080

Terminate HTTPS in front of it with a reverse proxy holding a real certificate. GitHub
will not deliver to plain HTTP, and this process should not hold a certificate it does
not need. Set the same ``AGENTDECK_WEBHOOK_SECRET`` in the AgentDeck web process so the
private end re-verifies rather than trusting the hop.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("webhook-gateway")

WEBHOOK_PATH = "/github/webhook"
FORWARD_PATH = "/api/github/webhook"
# GitHub's own documented ceiling for a webhook payload. This must not be smaller than
# the private end's MAX_BODY_BYTES in agentdeck/github_webhook_log.py: a tighter bound
# here would refuse a legitimate large delivery on the public hop, and the host would
# never learn the event existed. Repeated as a literal because this script is stdlib-only
# and runs on a VPS that has no AgentDeck checkout to import from.
MAX_BODY_BYTES = 25 * 1024 * 1024
FORWARD_TIMEOUT_S = 10
# The waits between forward attempts: five tries over about three minutes, enough to ride
# out a stalled event loop or a web restart on the deck.
RETRY_DELAYS_S = (5, 15, 45, 120)

# The headers the private end keeps. Everything else GitHub sends is dropped: forwarding
# only what is used keeps the private side's input surface small.
FORWARD_HEADERS = (
    "X-GitHub-Event",
    "X-GitHub-Delivery",
    "X-GitHub-Hook-ID",
    "X-GitHub-Hook-Installation-Target-ID",
    "X-GitHub-Hook-Installation-Target-Type",
    "X-Hub-Signature-256",
)


def verify(secret: str, body: bytes, header_value: str | None) -> bool:
    """Same check AgentDeck makes, deliberately duplicated.

    The gateway verifies so unverified traffic never crosses the private hop; AgentDeck
    verifies again so the hop is not itself trusted. Neither check is redundant — they
    defend different things.
    """
    if not secret or not header_value:
        return False
    prefix, _, digest = header_value.strip().partition("=")
    if prefix != "sha256" or not digest:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.lower())


def _send_once(url: str, body: bytes, headers: dict[str, str]) -> int:
    request = urllib.request.Request(url, data=body, method="POST")  # noqa: S310
    request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=FORWARD_TIMEOUT_S) as response:  # noqa: S310
        return response.status


def forward(
    target: str,
    body: bytes,
    headers: dict[str, str],
    *,
    delays: tuple[float, ...] = RETRY_DELAYS_S,
    sleep=time.sleep,
) -> bool:
    """Forward one delivery, retrying a failure that can pass. True once the deck took it."""
    url = target.rstrip("/") + FORWARD_PATH
    delivery = headers.get("X-GitHub-Delivery", "?")
    tries = len(delays) + 1
    for attempt in range(1, tries + 1):
        try:
            status = _send_once(url, body, headers)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                log.warning("forward of %s refused: HTTP %s, not retried", delivery, exc.code)
                return False
            reason: object = f"HTTP {exc.code}"
        except ValueError as exc:
            log.warning("forward of %s failed: %s, not retried", delivery, exc)
            return False
        except OSError as exc:  # URLError, timeouts and resets are all OSError
            reason = exc
        else:
            log.info("forwarded %s -> HTTP %s (try %s)", delivery, status, attempt)
            return True
        if attempt == tries:
            log.warning("forward of %s failed after %s tries: %s; dropped", delivery, tries, reason)
            return False
        log.warning("forward of %s failed (try %s of %s): %s", delivery, attempt, tries, reason)
        sleep(delays[attempt - 1])
    return False


def make_handler(secret: str, target: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agentdeck-webhook-gateway"
        # Do not advertise the Python version to the public internet.
        sys_version = ""

        def _reply(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            # A liveness probe and nothing else. No delivery state is exposed publicly.
            if self.path == "/healthz":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != WEBHOOK_PATH:
                self._reply(404, {"error": "not found"})
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(413, {"error": "bad body length"})
                return
            body = self.rfile.read(length)

            if not verify(secret, body, self.headers.get("X-Hub-Signature-256")):
                log.warning("rejected a delivery whose signature did not verify")
                self._reply(401, {"error": "signature did not verify"})
                return

            headers = {
                name: self.headers.get(name, "")
                for name in FORWARD_HEADERS
                if self.headers.get(name)
            }
            # Answer GitHub now; the private hop happens on its own thread. GitHub's
            # ten-second budget is for this response, not for AgentDeck's work.
            threading.Thread(target=forward, args=(target, body, headers), daemon=True).start()
            self._reply(202, {"status": "accepted"})

        def log_message(self, fmt: str, *args) -> None:
            log.debug(fmt, *args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind address (put a TLS proxy in front)"
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--target",
        default=os.environ.get("AGENTDECK_WEBHOOK_TARGET", ""),
        help="AgentDeck base URL reachable over the private hop",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    secret = os.environ.get("AGENTDECK_WEBHOOK_SECRET", "")
    if not secret:
        print("AGENTDECK_WEBHOOK_SECRET is required", file=sys.stderr)
        return 2
    if not args.target:
        print("--target or AGENTDECK_WEBHOOK_TARGET is required", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((args.host, args.port), make_handler(secret, args.target))
    log.info("gateway listening on %s:%s -> %s", args.host, args.port, args.target)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
