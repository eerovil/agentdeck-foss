#!/usr/bin/env python3
"""The one web page reader of a ``locked`` chat: an MCP server over stdio.

A ``locked`` chat (``providers/claude_code/locked.py``) may read public web pages, but it
runs on this host, so a reader that looked a name up, checked it, and let something
else look it up again to connect could be sent to the deck's own API or the home
network by a name whose DNS answer changes in between (DNS rebinding). This server
does both itself: it looks the host up once, refuses unless every address is public,
and connects its own socket to that checked address. HTTPS checks the certificate and
sends SNI for the name, over the pinned socket. Every redirect is a new URL, looked up
and checked again.

One deadline (``DEADLINE_S``) covers a whole call from the moment it is read: every
lookup, connect, read and redirect. Lookups run in daemon threads waited on with the
time left; sockets time out on the time left; and a watchdog closes the socket at the
deadline, so neither a slow nameserver nor a page sent one byte at a time can hold a
call longer. Calls run in threads of their own, so two at once do not queue.

It never uses ``urllib``'s opener, so ``HTTP_PROXY``/``HTTPS_PROXY``/``ALL_PROXY`` in
the environment cannot take the connection away from the checked address. It asks for
``Accept-Encoding: identity``, refuses any other encoding, and reads at most
``MAX_BODY`` bytes. Standard library only; run with ``python3 -I``. The protocol is
plain JSON-RPC, one message a line: ``initialize``, ``tools/list``, ``tools/call``.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

SERVER_NAME = "deckfetch"
TOOL_NAME = "hae_sivu"
DEADLINE_S = 20.0
MAX_BODY = 2_000_000
MAX_TEXT = 50_000
MAX_REDIRECTS = 5
REFUSED_SUFFIXES = (".ts.net", ".local", ".lan", ".internal", ".home.arpa", ".localhost")
REFUSED_NAMES = frozenset({"localhost"})
ALLOWED_PORTS = frozenset({None, 80, 443})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
TEXT_TYPES = ("text/", "application/xhtml+xml", "application/json", "application/xml")
LOG = Path.home() / ".local" / "share" / "agentdeck" / "locked-fetch.log"
USER_AGENT = "Mozilla/5.0 (compatible; page-reader)"

TOOL = {
    "name": TOOL_NAME,
    "description": (
        "Read a public web page (http or https) and answer its text. Pages on the local "
        "network or this computer are refused."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The page's full address."}},
        "required": ["url"],
    },
}


class Refused(Exception):
    """The page is not read, and why, in words the chat can repeat."""


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.at = time.monotonic() + seconds

    def left(self) -> float:
        left = self.at - time.monotonic()
        if left <= 0:
            raise Refused("the page did not answer in time")
        return left


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not ip.is_global


def target(url: object) -> tuple[str, str, int, str]:
    """``(scheme, host, port, path and query)`` of a URL that may be read at all."""
    if not isinstance(url, str) or not url.strip():
        raise Refused("no URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise Refused("the URL does not parse") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise Refused("only http and https pages may be read")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise Refused("a URL with a user name is refused")
    if port not in ALLOWED_PORTS:
        raise Refused("only the normal web ports may be used")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise Refused("the URL names no host")
    if host in REFUSED_NAMES or host.endswith(REFUSED_SUFFIXES):
        raise Refused("local network names are refused")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port or (443 if scheme == "https" else 80), path


def checked_addresses(host: str, deadline: Deadline, resolve=_resolve) -> list[str]:
    """The addresses to connect to, in order: ``host`` looked up once, every answer public."""
    answer: dict[str, object] = {}

    def work() -> None:
        try:
            answer["addresses"] = resolve(host)
        except Exception as exc:  # noqa: BLE001 -- a failed lookup is a refusal
            answer["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(deadline.left())
    if thread.is_alive():
        raise Refused("the address could not be looked up in time")
    addresses = answer.get("addresses")
    if not addresses:
        raise Refused("the name has no address")
    if any(_private(address) for address in addresses):  # type: ignore[union-attr]
        raise Refused("local and private network addresses are refused")
    return list(dict.fromkeys(addresses))  # type: ignore[arg-type]


def _tls_context() -> ssl.SSLContext:
    """The system's trusted certificates, the name checked: what every HTTPS page meets."""
    return ssl.create_default_context()


def _connect(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


class _Pinned(http.client.HTTPConnection):
    """An HTTP connection over a socket already opened to the checked address."""

    def __init__(self, host: str, port: int, sock: socket.socket, scheme: str) -> None:
        super().__init__(f"[{host}]" if ":" in host else host, port)
        # The Host header leaves out the scheme's own port, 443 included.
        self.default_port = 443 if scheme == "https" else 80
        self.sock = sock

    def connect(self) -> None:  # pragma: no cover -- the socket is given
        raise RuntimeError("a pinned connection never opens its own socket")


def _open(addresses: list[str], port: int, deadline: Deadline, connect) -> socket.socket:
    """A socket to the first of ``addresses`` that answers, all from the one checked lookup.

    Each try gets an even share of the time left, so one silent address cannot take
    it all. Only the TCP connect moves on: a refused certificate is never retried."""
    failure: OSError | None = None
    for tried, address in enumerate(addresses):
        try:
            return connect(address, port, deadline.left() / (len(addresses) - tried))
        except OSError as exc:
            failure = exc
    assert failure is not None
    raise failure


def _get(
    url: str, deadline: Deadline, resolve, connect, sockets: list
) -> tuple[int, dict[str, str], bytes, bool]:
    scheme, host, port, path = target(url)
    addresses = checked_addresses(host, deadline, resolve)
    sock = _open(addresses, port, deadline, connect)
    sockets.append(sock)
    if scheme == "https":
        context = _tls_context()
        sock.settimeout(deadline.left())
        sock = context.wrap_socket(sock, server_hostname=host)
        sockets.append(sock)
    sock.settimeout(deadline.left())
    connection = _Pinned(host, port, sock, scheme)
    connection.request(
        "GET",
        path,
        headers={
            "Accept-Encoding": "identity",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
            "User-Agent": USER_AGENT,
        },
    )
    response = connection.getresponse()
    headers = {name.lower(): value for name, value in response.getheaders()}
    if response.status in REDIRECTS:
        connection.close()
        return response.status, headers, b"", False
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        connection.close()
        raise Refused("the page came compressed although it was asked not to be")
    body = bytearray()
    cut = False
    while True:
        sock.settimeout(deadline.left())
        chunk = response.read(65536)
        if not chunk:
            break
        body += chunk
        if len(body) > MAX_BODY:
            del body[MAX_BODY:]
            cut = True
            break
    connection.close()
    return response.status, headers, bytes(body), cut


def fetch(url: object, *, resolve=_resolve, connect=_connect, deadline: float = DEADLINE_S):
    """``(final URL, status, headers, body, cut)`` of ``url``, or :class:`Refused`.

    The whole call, redirects included, ends by ``deadline`` seconds: past it the
    watchdog closes the open socket and the call is refused.
    """
    clock = Deadline(deadline)
    sockets: list = []
    answer: dict[str, object] = {}

    def work() -> None:
        try:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                status, headers, body, cut = _get(current, clock, resolve, connect, sockets)
                if status in REDIRECTS:
                    location = headers.get("location")
                    if not location:
                        raise Refused("the page redirected to nowhere")
                    current = urljoin(str(current), location)
                    continue
                answer["page"] = (current, status, headers, body, cut)
                return
            raise Refused("the page redirected too many times")
        except Refused as exc:
            answer["refused"] = exc
        except ssl.SSLError as exc:
            answer["refused"] = Refused(f"the page's certificate is not valid ({exc.reason})")
        except (OSError, http.client.HTTPException) as exc:
            answer["refused"] = Refused(f"the page could not be read ({type(exc).__name__})")

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(max(0.0, clock.at - time.monotonic()))
    if thread.is_alive():
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        raise Refused("the page did not answer in time")
    if "refused" in answer:
        raise answer["refused"]  # type: ignore[misc]
    return answer["page"]


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg"})
    BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def page_text(headers: dict[str, str], body: bytes) -> str:
    kind = headers.get("content-type", "text/html").lower()
    if not kind.startswith(TEXT_TYPES):
        raise Refused(f"the page is not text ({kind.split(';')[0].strip()})")
    charset = "utf-8"
    for part in kind.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name == "charset" and value:
            charset = value.strip("\"'")
    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if "html" in kind:
        parser = _Text()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def read_page(url: object, **kwargs) -> tuple[str, bool]:
    """The tool's answer text, and whether it is an error."""
    try:
        final, status, headers, body, cut = fetch(url, **kwargs)
        text = page_text(headers, body)
    except Refused as exc:
        _note(url, str(exc))
        return f"Refused: {exc}.", True
    if len(text) > MAX_TEXT:
        text, cut = text[:MAX_TEXT], True
    head = f"{final} (HTTP {status})"
    if cut:
        head += " — only the start of the page"
    return f"{head}\n\n{text}", status >= 400


def _note(url: object, reason: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
            handle.write(json.dumps({"at": stamp, "url": str(url)[:500], "why": reason}) + "\n")
    except OSError:
        pass


def answer(message: dict, **kwargs) -> dict | None:
    """The reply to one JSON-RPC message, or None for a notification."""
    method = message.get("method")
    ident = message.get("id")
    params = message.get("params") or {}
    if ident is None:
        return None
    if method == "initialize":
        result: dict = {
            "protocolVersion": params.get("protocolVersion") or "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "error": {"code": -32602, "message": "unknown tool"},
            }
        arguments = params.get("arguments") or {}
        text, failed = read_page(arguments.get("url"), **kwargs)
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "no method"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(lines, write, **kwargs) -> None:
    """Answer each message line; a tool call runs in a thread of its own."""
    lock = threading.Lock()
    calls: list[threading.Thread] = []

    def reply(message: dict) -> None:
        try:
            out = answer(message, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- one bad call never ends the server
            out = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": type(exc).__name__},
            }
        if out is not None:
            with lock:
                write(json.dumps(out) + "\n")

    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("method") == "tools/call":
            call = threading.Thread(target=reply, args=(message,), daemon=True)
            call.start()
            calls.append(call)
        else:
            reply(message)
    for call in calls:
        call.join(DEADLINE_S + 1)


def main() -> None:
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    serve(sys.stdin, write)


if __name__ == "__main__":
    main()
