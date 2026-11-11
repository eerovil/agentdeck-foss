"""Call one tool of a configured MCP server from a shell command (#4256).

A time wait's ``check`` is a shell command the deck runs on a schedule, and most of the
day-later live checks it replaces read through MCP servers a chat has as tools: a business
data tool, Sentry, the log search (#4244). ``agentdeck mcp-call <server> <tool> '<json>'``
makes each of those one command, using the account's own MCP config, so the deck holds no
second copy of any key.

The server is read from a Claude Code config file's ``mcpServers`` (``.claude.json``): the
one ``--config`` names, else the one a check's chat is bound to (``MCP_CONFIG_ENV``), else
the account's own: ``$CLAUDE_CONFIG_DIR``'s, or
``~/.claude.json`` when it is unset, as Claude Code reads it. Never another account's: two
accounts may each name a ``sentry`` with different keys, and a check runs with its chat's
account (``WakeOnService``'s ``check_env``). Two of
Claude Code's transports are spoken: ``stdio`` (a command, JSON-RPC by line) and ``http``
(streamable HTTP, a JSON or an event-stream answer).

Exit codes: 0 the tool answered, 1 the tool answered with an error, 2 the call could not be
made (no such server, a transport fault). Neither is ``wake_on.CHECK_NOT_YET``, so a check
that pipes this into its own condition decides "not yet" itself.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "agentdeck-mcp-call", "version": "1"}
#: How long one call may take, in seconds, unless ``--timeout`` says otherwise.
DEFAULT_TIMEOUT_S = 120.0
#: ``${VAR}`` or ``${VAR:-default}`` in a server's config, which Claude Code expands from its
#: environment: a key is often written as ``Bearer ${SOME_MCP_TOKEN}``.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class McpCallError(RuntimeError):
    """The call could not be made; the message says why."""


#: Set by the deck on a time wait's check (``wake_on.check_account_env``): the chat's own
#: account's config file, or empty when that chat has no Claude account (a Codex chat, an
#: account the deck's config no longer names). Empty refuses the call rather than falling
#: back to another account's servers, which may share a name and read other data.
MCP_CONFIG_ENV = "AGENTDECK_MCP_CONFIG"


def config_files(explicit: str = "") -> list[Path]:
    """The config files looked in, in order."""
    if explicit:
        return [Path(explicit).expanduser()]
    if MCP_CONFIG_ENV in os.environ:
        named = os.environ[MCP_CONFIG_ENV]
        if not named:
            raise McpCallError(
                "this chat has no Claude account whose MCP servers it may use"
                " (a Codex chat, or an account the deck no longer has); pass --config"
            )
        return [Path(named).expanduser()]
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if env_dir:
        return [Path(env_dir).expanduser() / ".claude.json"]
    return [Path.home() / ".claude.json"]


def find_server(name: str, files: list[Path], cwd: str = "") -> dict:
    """The server's config: top-level ``mcpServers``, or the project's for ``cwd``."""
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            return expand(servers[name])
        projects = data.get("projects") if isinstance(data, dict) else None
        project = projects.get(cwd) if isinstance(projects, dict) and cwd else None
        servers = project.get("mcpServers") if isinstance(project, dict) else None
        if isinstance(servers, dict) and isinstance(servers.get(name), dict):
            return expand(servers[name])
    looked = ", ".join(str(path) for path in files)
    raise McpCallError(f"no MCP server named {name!r} in {looked}")


def expand(value: Any) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in every string of a server's config."""
    if isinstance(value, str):

        def one(match: re.Match) -> str:
            name, default = match[1], match[2]
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise McpCallError(f"the server's config uses ${{{name}}}, which is not set here")

        return _ENV_RE.sub(one, value)
    if isinstance(value, list):
        return [expand(item) for item in value]
    if isinstance(value, dict):
        return {key: expand(item) for key, item in value.items()}
    return value


def _request(method: str, params: dict, request_id: int | None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        message["id"] = request_id
    return message


def _initialize() -> dict:
    return _request(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        1,
    )


def _answer(message: dict) -> dict:
    if "error" in message:
        error = message["error"]
        text = error.get("message") if isinstance(error, dict) else error
        raise McpCallError(f"the server refused: {text}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpCallError(f"the server answered without a result: {message!r}"[:500])
    return result


def call_stdio(server: dict, method: str, params: dict, timeout_s: float) -> dict:
    """Start the server, initialize, make one request and return its result."""
    command = server.get("command")
    if not isinstance(command, str) or not command:
        raise McpCallError("this stdio server names no command")
    args = [str(arg) for arg in server.get("args") or []]
    env = {**os.environ, **{str(k): str(v) for k, v in (server.get("env") or {}).items()}}
    lines = [
        json.dumps(_initialize()),
        json.dumps(_request("notifications/initialized", {}, None)),
        json.dumps(_request(method, params, 2)),
    ]
    try:
        process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # The caller's own stderr: a check's wake then quotes why a server failed.
            stderr=None,
            env=env,
            text=True,
        )
    except OSError as exc:
        raise McpCallError(f"the server could not start: {exc}") from exc
    try:
        assert process.stdin is not None and process.stdout is not None
        # One request at a time: some servers read the next line only after answering.
        process.stdin.write(lines[0] + "\n")
        process.stdin.flush()
        buffer = bytearray()
        _read_stdio(process, buffer, 1, timeout_s)
        process.stdin.write(lines[1] + "\n" + lines[2] + "\n")
        process.stdin.flush()
        return _answer(_read_stdio(process, buffer, 2, timeout_s))
    finally:
        process.kill()
        process.wait(timeout=5)


def _read_stdio(
    process: subprocess.Popen, buffer: bytearray, request_id: int, timeout_s: float
) -> dict:
    """The server's answer to ``request_id``, skipping notifications and log lines.

    Read from the raw descriptor into this process's own buffer: a buffered ``readline``
    could hold the next line where ``select`` no longer sees it, and wait out the timeout.
    """
    assert process.stdout is not None
    fd = process.stdout.fileno()
    end = time.monotonic() + timeout_s
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            while b"\n" in buffer:
                line, _, rest = bytes(buffer).partition(b"\n")
                buffer[:] = rest
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # a log line on stdout
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
            left = end - time.monotonic()
            if left <= 0 or not selector.select(timeout=left):
                raise McpCallError(f"the server did not answer within {round(timeout_s)} s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise McpCallError("the server exited before answering")
            buffer.extend(chunk)


def call_http(server: dict, method: str, params: dict, timeout_s: float) -> dict:
    """Initialize over streamable HTTP, then make one request and return its result."""
    url = server.get("url")
    if not isinstance(url, str) or not url:
        raise McpCallError("this http server names no url")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **{str(k): str(v) for k, v in (server.get("headers") or {}).items()},
    }
    first, session = _post(url, headers, _initialize(), timeout_s)
    _answer(first)
    if session:
        headers["Mcp-Session-Id"] = session
    headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
    _post(url, headers, _request("notifications/initialized", {}, None), timeout_s)
    message, _ = _post(url, headers, _request(method, params, 2), timeout_s)
    return _answer(message)


def _post(url: str, headers: dict, body: dict, timeout_s: float) -> tuple[dict, str]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            session = response.headers.get("Mcp-Session-Id", "")
            kind = response.headers.get("Content-Type", "")
            if "id" not in body:
                return {}, session  # a notification: 202 and no body
            if "text/event-stream" in kind:
                # Read event by event: a server may keep the stream open after its answer.
                return _read_events(response, body["id"]), session
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise McpCallError(f"the server answered HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise McpCallError(f"the server could not be reached: {exc}") from exc
    return _parse_http(raw, kind, body["id"]), session


def _read_events(lines: Iterable[bytes], request_id: int) -> dict:
    """The answer to ``request_id`` from an event stream, returned as soon as its event ends."""
    data: list[str] = []
    seen: list[str] = []
    for raw_line in _with_end(lines):
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
            continue
        if line or not data:
            continue
        text, data = "\n".join(data), []
        seen.append(text)
        found = _reply_in(text, request_id)
        if found is not None:
            return found
    raise McpCallError(f"the server's answer held no reply: {' '.join(seen)[:300]!r}")


def _with_end(lines: Iterable[bytes]) -> Iterator[bytes]:
    yield from lines
    yield b""


def _reply_in(text: str, request_id: int) -> dict | None:
    try:
        message = json.loads(text)
    except ValueError:
        return None
    for item in message if isinstance(message, list) else [message]:
        if isinstance(item, dict) and item.get("id") == request_id:
            return item
    return None


def _parse_http(raw: str, kind: str, request_id: int) -> dict:
    """The answer to ``request_id`` in a JSON body or a whole event stream's ``data:`` lines."""
    if "text/event-stream" in kind:
        return _read_events([line.encode() for line in raw.splitlines()], request_id)
    found = _reply_in(raw, request_id)
    if found is None:
        raise McpCallError(f"the server's answer held no reply: {raw[:300]!r}")
    return found


def call(server: dict, method: str, params: dict, timeout_s: float) -> dict:
    kind = server.get("type") or ("http" if server.get("url") else "stdio")
    if kind == "stdio":
        return call_stdio(server, method, params, timeout_s)
    if kind in ("http", "streamable-http"):
        return call_http(server, method, params, timeout_s)
    raise McpCallError(f"transport {kind!r} is not supported; only stdio and http are")


def result_text(result: dict) -> str:
    """The tool's answer as text: its text parts, else its structured content as JSON."""
    parts = [
        str(part.get("text", ""))
        for part in result.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    if parts:
        return "\n".join(parts)
    if "structuredContent" in result:
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return json.dumps(result.get("content") or [], ensure_ascii=False)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck mcp-call",
        description="Call one tool of an MCP server from the account's own Claude config and"
        " print its answer. Exit 0: answered; 1: the tool reported an error; 2: no call made."
        " With no tool, list the server's tools.",
    )
    parser.add_argument("server", help="the server's name in mcpServers, such as sentry")
    parser.add_argument("tool", nargs="?", help="the tool's name, such as search_issues")
    parser.add_argument("arguments", nargs="?", default="{}", help="the tool's arguments, JSON")
    parser.add_argument("--config", default="", help="a .claude.json to read the server from")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds")
    args = parser.parse_args(argv)
    try:
        arguments = json.loads(args.arguments)
        if not isinstance(arguments, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print(f"agentdeck mcp-call: arguments must be a JSON object: {exc}", file=sys.stderr)
        return 2
    try:
        server = find_server(args.server, config_files(args.config), os.getcwd())
        if not args.tool:
            listed = call(server, "tools/list", {}, args.timeout)
            for tool in listed.get("tools") or []:
                about = (tool.get("description") or "").strip().splitlines()
                print(f"{tool.get('name')}: {about[0] if about else ''}")
            return 0
        result = call(
            server, "tools/call", {"name": args.tool, "arguments": arguments}, args.timeout
        )
    except McpCallError as exc:
        print(f"agentdeck mcp-call: {exc}", file=sys.stderr)
        return 2
    print(result_text(result))
    return 1 if result.get("isError") else 0
