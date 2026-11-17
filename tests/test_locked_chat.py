"""A ``locked`` chat starts only with its limits, and reads only public pages.

The role is for a chat people use who must not reach this host: no shell, no files, no
helper agents or skills, only its folder's MCP servers (``providers/claude_code/locked.py``)
and the deck's page reader, which looks a host up once, refuses anything but a public
address and connects to the address it checked (``scripts/locked_fetch_server.py``).
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from worker_harness import claude_host as _host
from worker_harness import settle as _settle

from agentdeck.providers.claude_code import locked
from agentdeck.providers.instructions import (
    GUIDED_ROLES,
    LOCKED_SYSTEM_PROMPT,
    agentdeck_session_instructions,
    stored_session_role,
)

ROOT = Path(__file__).resolve().parent.parent
READER = ROOT / "scripts" / "locked_fetch_server.py"


def _reader():
    spec = importlib.util.spec_from_file_location("locked_fetch_server", READER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader = _reader()


def _numeric(host: str) -> list[str]:
    """What the system resolver makes of an address written as a number; names fail."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP, flags=socket.AI_NUMERICHOST)
    return [str(info[4][0]) for info in infos]


def _names(table: dict[str, list[str]], asked: list[str] | None = None):
    def resolve(host: str) -> list[str]:
        if asked is not None:
            asked.append(host)
        if host in table:
            return table[host]
        try:
            return _numeric(host)
        except socket.gaierror:
            raise socket.gaierror(f"no address for {host}") from None

    return resolve


PUBLIC = "193.166.4.1"
SECOND = "193.166.4.2"
V6 = "2001:708:10:10::1"
OK = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: 47\r\n\r\n"
OK_BODY = b"<html><title>Uutiset</title><p>Hello page</p></html>"[:47]


class _Site:
    """Stands in for the network: each connect gets the next canned answer, over TLS
    with ``tls`` (a server context) as its certificate."""

    def __init__(self, *answers, drip: float = 0.0, tls=None):
        self.answers = list(answers)
        self.drip = drip
        self.tls = tls
        self.connected: list[tuple[str, int]] = []
        self.requests: list[bytes] = []
        self.server_names: list[str | None] = []

    def __call__(self, address: str, port: int, timeout: float) -> socket.socket:
        self.connected.append((address, port))
        ours, theirs = socket.socketpair()
        answer = self.answers.pop(0)

        def talk() -> None:
            nonlocal theirs
            if self.tls is not None:
                self.tls.sni_callback = lambda conn, name, ctx: self.server_names.append(name)
                try:
                    theirs = self.tls.wrap_socket(theirs, server_side=True)
                except (OSError, ssl.SSLError):
                    return
            request = b""
            while b"\r\n\r\n" not in request:
                part = theirs.recv(4096)
                if not part:
                    return
                request += part
            self.requests.append(request)
            try:
                if self.drip:
                    for byte in answer:
                        theirs.sendall(bytes([byte]))
                        time.sleep(self.drip)
                else:
                    theirs.sendall(answer)
            except OSError:
                pass
            finally:
                theirs.close()

        threading.Thread(target=talk, daemon=True).start()
        return ours


def _never(address, port, timeout):
    raise AssertionError(f"connected to {address}:{port}")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:80/api/sessions",
        "http://localhost/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://127.0.0.1.nip.io/",
        "http://100.64.1.2/",
        "http://192.168.1.20/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "https://deck.example.ts.net/",
        "https://printer.local/",
        "https://rebinds.example/",
        "https://unknown.example/",
        "file:///etc/passwd",
        "https://user@yle.fi/",
        "https://yle.fi@127.0.0.1/",
        "https://yle.fi:8756/",
        "",
        None,
    ],
)
def test_the_reader_refuses_every_address_that_is_not_public(url):
    resolve = _names(
        {"127.0.0.1.nip.io": ["127.0.0.1"], "rebinds.example": [PUBLIC, "192.168.1.20"]}
    )
    with pytest.raises(reader.Refused):
        reader.fetch(url, resolve=resolve, connect=_never, deadline=2)


def test_the_reader_connects_once_to_the_address_it_checked(monkeypatch):
    """DNS rebinding: a second lookup would answer a private address, but there is none;
    and a proxy in the environment does not take the connection elsewhere."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    asked: list[str] = []

    def rebinding(host):
        asked.append(host)
        return next(answers)

    site = _Site(OK + OK_BODY)
    text, failed = reader.read_page("http://news.example/a?b=1", resolve=rebinding, connect=site)
    assert not failed, text
    assert asked == ["news.example"]
    assert site.connected == [(PUBLIC, 80)]
    request = site.requests[0]
    assert request.startswith(b"GET /a?b=1 HTTP/1.1\r\n")
    assert b"Host: news.example\r\n" in request
    assert b"Accept-Encoding: identity\r\n" in request
    assert "Uutiset" in text and "Hello page" in text
    assert "urllib.request" not in READER.read_text()


def _certificates(tmp_path: Path, name: str):
    """A test authority, and a server context holding its certificate for ``name``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.UTC)

    def make(subject, issuer, key, signer, *, ca, san=None):
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        if ca:
            builder = builder.add_extension(
                x509.KeyUsage(False, False, False, False, False, True, True, False, False), True
            )
        if san:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), False)
        return builder.sign(signer, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = make("Test authority", "Test authority", ca_key, ca_key, ca=True)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = make(name, "Test authority", key, ca_key, ca=False, san=name)
    pem = serialization.Encoding.PEM
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(pem))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(pem) + ca.public_bytes(pem))
    (tmp_path / "leaf.key").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    return tmp_path / "ca.pem", server


def test_https_checks_the_name_over_the_pinned_address(tmp_path, monkeypatch):
    """HTTPS connects to the checked address, sends the name as SNI and checks the
    certificate against it: a certificate for another name is refused."""
    (tmp_path / "good").mkdir()
    ca, server = _certificates(tmp_path / "good", "news.example")
    monkeypatch.setattr(reader, "_tls_context", lambda: ssl.create_default_context(cafile=str(ca)))
    answers = iter([[PUBLIC], ["127.0.0.1"]])
    site = _Site(OK + OK_BODY, tls=server)
    text, failed = reader.read_page(
        "https://news.example/", resolve=lambda host: next(answers), connect=site
    )
    assert not failed, text
    assert site.connected == [(PUBLIC, 443)]
    assert site.server_names == ["news.example"]
    assert b"Host: news.example\r\n" in site.requests[0]
    assert "Hello page" in text

    (tmp_path / "other").mkdir()
    other_ca, other = _certificates(tmp_path / "other", "other.example")
    monkeypatch.setattr(
        reader, "_tls_context", lambda: ssl.create_default_context(cafile=str(other_ca))
    )
    wrong = _Site(OK + OK_BODY, tls=other)
    text, failed = reader.read_page(
        "https://news.example/", resolve=_names({"news.example": [PUBLIC]}), connect=wrong
    )
    assert failed and "certificate" in text
    assert wrong.requests == []

    # A certificate refused at the first address is not tried at the next one.
    tried = _Site(OK + OK_BODY, tls=other)
    text, failed = reader.read_page(
        "https://news.example/", resolve=_names({"news.example": [PUBLIC, SECOND]}), connect=tried
    )
    assert failed and "certificate" in text
    assert tried.connected == [(PUBLIC, 443)]


def test_an_address_that_does_not_answer_moves_on_to_the_next_checked_one():
    """A dual-stack site whose first address is down is read from its second, from the
    same lookup; when none answers, the page is refused."""
    site = _Site(OK + OK_BODY)
    attempts: list[tuple[str, float]] = []

    def first_down(address, port, timeout):
        attempts.append((address, timeout))
        if address == V6:
            raise ConnectionRefusedError("down")
        return site(address, port, timeout)

    asked: list[str] = []

    def once(host):
        asked.append(host)
        return [V6, PUBLIC, V6]

    text, failed = reader.read_page("http://news.example/", resolve=once, connect=first_down)
    assert not failed, text
    assert asked == ["news.example"]
    assert [address for address, _ in attempts] == [V6, PUBLIC]
    assert attempts[0][1] < attempts[1][1] <= reader.DEADLINE_S
    assert site.connected == [(PUBLIC, 80)]

    def all_down(address, port, timeout):
        attempts.append((address, timeout))
        raise ConnectionRefusedError("down")

    attempts.clear()
    text, failed = reader.read_page(
        "http://news.example/", resolve=_names({"news.example": [V6, PUBLIC]}), connect=all_down
    )
    assert failed and "could not be read" in text
    assert [address for address, _ in attempts] == [V6, PUBLIC]


def test_a_redirect_is_checked_again_and_a_public_one_followed():
    to_private = _Site(
        b"HTTP/1.1 302 Found\r\nLocation: http://10.0.0.1/\r\nContent-Length: 0\r\n\r\n"
    )
    with pytest.raises(reader.Refused, match="private"):
        reader.fetch(
            "http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=to_private
        )
    assert len(to_private.connected) == 1

    moved = b"HTTP/1.1 301 Moved\r\nLocation: /new\r\nContent-Length: 0\r\n\r\n"
    site = _Site(moved, OK + OK_BODY)
    final, status, *_ = reader.fetch(
        "http://a.example/old", resolve=_names({"a.example": [PUBLIC]}), connect=site
    )
    assert (final, status) == ("http://a.example/new", 200)
    assert site.requests[1].startswith(b"GET /new ")

    loop = _Site(*[moved] * (reader.MAX_REDIRECTS + 1))
    with pytest.raises(reader.Refused, match="too many"):
        reader.fetch("http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=loop)


def test_a_compressed_or_binary_page_is_refused_and_a_big_one_cut():
    gzip = _Site(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 3\r\n\r\nabc")
    text, failed = reader.read_page(
        "http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=gzip
    )
    assert failed and "compressed" in text
    image = _Site(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 3\r\n\r\nabc")
    text, failed = reader.read_page(
        "http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=image
    )
    assert failed and "not text" in text
    size = reader.MAX_BODY + 10
    big = _Site(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % size
        + b"a" * size
    )
    *_, body, cut = reader.fetch(
        "http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=big
    )
    assert cut and len(body) == reader.MAX_BODY


def test_one_deadline_ends_a_hanging_lookup_or_a_dripping_page():
    def hang(host):
        time.sleep(2)
        return [PUBLIC]

    started = time.monotonic()
    with pytest.raises(reader.Refused, match="in time"):
        reader.fetch("https://slow.example/", resolve=hang, connect=_never, deadline=0.1)
    assert time.monotonic() - started < 0.4

    drip = _Site(OK + OK_BODY, drip=0.02)
    started = time.monotonic()
    with pytest.raises(reader.Refused, match="in time"):
        reader.fetch(
            "http://a.example/", resolve=_names({"a.example": [PUBLIC]}), connect=drip, deadline=0.2
        )
    assert time.monotonic() - started < 0.4


def test_the_server_answers_mcp_and_runs_calls_side_by_side():
    def hang(host):
        time.sleep(1)
        return [PUBLIC]

    lines = [
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        ),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        *[
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": n,
                    "method": "tools/call",
                    "params": {"name": "hae_sivu", "arguments": {"url": "https://slow.example/"}},
                }
            )
            for n in (3, 4)
        ],
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "other", "arguments": {}},
            }
        ),
    ]
    out: list[str] = []
    started = time.monotonic()
    reader.serve(lines, out.append, resolve=hang, connect=_never, deadline=0.15)
    assert time.monotonic() - started < 0.4
    replies = {reply["id"]: reply for reply in map(json.loads, out)}
    assert sorted(replies) == [1, 2, 3, 4, 5]
    assert replies[1]["result"]["serverInfo"]["name"] == locked.FETCH_SERVER
    assert [tool["name"] for tool in replies[2]["result"]["tools"]] == ["hae_sivu"]
    assert locked.FETCH_TOOL == f"mcp__{locked.FETCH_SERVER}__hae_sivu"
    for n in (3, 4):
        assert replies[n]["result"]["isError"] is True
        assert "in time" in replies[n]["result"]["content"][0]["text"]
    assert "error" in replies[5]


def test_the_reader_script_refuses_over_stdio():
    """The script itself, as Claude Code runs it: a URL that needs no lookup."""
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hae_sivu", "arguments": {"url": "file:///etc/passwd"}},
        },
    ]
    done = subprocess.run(
        [sys.executable, "-I", str(READER)],
        input="".join(json.dumps(line) + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": "/nonexistent-home"},
    )
    assert done.returncode == 0, done.stderr
    replies = {reply["id"]: reply for reply in map(json.loads, done.stdout.splitlines())}
    assert replies[2]["result"]["isError"] is True
    assert replies[2]["result"]["content"][0]["text"].startswith("Refused")


# --- the start command ------------------------------------------------------------------


def _folder(tmp_path: Path, servers=("keep",)) -> Path:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / ".mcp.json").write_text(
        json.dumps({"mcpServers": {name: {"command": "true"} for name in servers}})
    )
    return folder


def _flag(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


async def test_a_locked_chat_spawns_with_its_limits_fresh_and_resumed(tmp_path, monkeypatch):
    from agentdeck.gh_shim.install import shim_dir

    monkeypatch.setenv("GH_TOKEN", "secret")
    # What this process may itself carry from an owned chat or the deck's own setup.
    monkeypatch.setenv("AGENTDECK_ACCOUNT_KEY", "claude_code:main")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!agentdeck-helper")
    monkeypatch.setenv("PATH", f"{shim_dir()}{os.pathsep}/usr/bin")
    folder = _folder(tmp_path)
    host, spawned = _host(tmp_path, base_url="http://127.0.0.1:8756")
    result = await host.deliver(
        "chat-k",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role="locked",
        model="haiku",
        delegated=False,
    )
    await _settle()
    assert result.accepted, result
    args = [str(item) for item in spawned[0]["args"]]
    args = args[args.index("claude") :]
    assert _flag(args, "--tools") == "WebSearch"
    assert "WebFetch" not in " ".join(args)
    assert _flag(args, "--setting-sources") == "project"
    configs = args[args.index("--mcp-config") + 1 : args.index("--mcp-config") + 3]
    assert configs[1] == str(folder / ".mcp.json")
    reader_config = json.loads(Path(configs[0]).read_text())
    assert reader_config == locked.fetch_config(READER)
    assert reader_config["mcpServers"]["deckfetch"]["args"] == ["-I", str(READER)]
    assert "--strict-mcp-config" in args and "--disable-slash-commands" in args
    assert _flag(args, "--permission-mode") == "dontAsk"
    assert _flag(args, "--allowedTools") == "WebSearch mcp__deckfetch__hae_sivu mcp__keep__*"
    assert _flag(args, "--system-prompt") == LOCKED_SYSTEM_PROMPT
    assert _flag(args, "--model") == "haiku"
    assert not any("bypass" in item or "dangerously" in item for item in args)
    assert "--settings" not in args
    env = spawned[0]["kwargs"]["env"]
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    for name in locked.WITHHELD_ENV:
        assert name not in env
    # No deck identity, gh shim or git credential setup reaches it.
    assert not [name for name in env if name.startswith(locked.WITHHELD_PREFIXES)]
    assert str(shim_dir()) not in env["PATH"].split(os.pathsep)
    assert "/usr/bin" in env["PATH"].split(os.pathsep)

    record = host._records["chat-k"]
    _env, _unit, _prefix, command = await host._spawn_plan("chat-k", record, resume_id="sid-1")
    assert _flag(command, "--resume") == "sid-1"
    assert _flag(command, "--tools") == "WebSearch"
    assert _flag(command, "--model") == "haiku"
    await host.stop()


@pytest.mark.parametrize(
    "broken", ["no .mcp.json", "reader gone", "config unwritable", "folder names the reader"]
)
async def test_a_locked_chat_without_its_limits_does_not_start(tmp_path, monkeypatch, broken):
    folder = _folder(tmp_path)
    host, spawned = _host(tmp_path)
    if broken == "no .mcp.json":
        (folder / ".mcp.json").unlink()
    elif broken == "reader gone":
        monkeypatch.setattr(locked, "fetch_server_script", lambda: tmp_path / "missing.py")
    elif broken == "folder names the reader":
        (folder / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"deckfetch": {"command": "true"}}})
        )
    else:
        blocker = tmp_path / "blocker"
        blocker.write_text("")
        host._artifact_root = blocker / "below"
    result = await host.deliver(
        "chat-k",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role="locked",
        delegated=False,
    )
    await _settle()
    assert not result.accepted
    assert "locked chat refused" in (result.reason or "")
    assert spawned == []
    await host.stop()


async def test_a_locked_chat_never_bypasses_and_never_takes_a_spare(tmp_path):
    folder = _folder(tmp_path)
    host, spawned = _host(tmp_path)
    assert (
        await host.prepare_spare(
            "new_chat", "chat-k", session_token="tok-k", cwd=str(folder), role="locked"
        )
        == "locked_chat"
    )
    await host.deliver(
        "chat-k",
        "hello",
        cwd=str(folder),
        fresh=True,
        session_token="tok-k",
        role="locked",
        delegated=False,
    )
    await _settle()
    refused = await host.set_bypass_permissions("chat-k", True)
    assert not refused.accepted
    assert "never bypasses" in (refused.reason or "")
    await host.stop()


def test_a_bad_server_name_refuses_and_codex_has_no_locked_role(tmp_path):
    folder = _folder(tmp_path, servers=("bad name*",))
    with pytest.raises(locked.LockedChatRefused):
        locked.mcp_servers(str(folder))
    with pytest.raises(ValueError):
        agentdeck_session_instructions(provider="codex", role="locked")
    # A resumed locked chat stays locked: it never falls back to a guided role.
    assert stored_session_role("locked") == "locked"
    assert "locked" not in GUIDED_ROLES
