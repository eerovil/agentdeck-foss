"""`agentdeck mcp-call` calls one tool of a configured MCP server (#4256).

A time wait's check is one shell command, and most live checks read through an MCP server.
The stdio server here is a real child process speaking JSON-RPC by line; the HTTP answers
are the two shapes a streamable-HTTP server sends.
"""

from __future__ import annotations

import json
import sys

import pytest

from agentdeck import mcp_call

FAKE_SERVER = r"""
import json, sys
print("starting up", flush=True)  # a log line on stdout is skipped
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {}}
    elif message["method"] == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo back.\nMore."}]}
    else:
        args = message["params"]["arguments"]
        result = {"content": [{"type": "text", "text": json.dumps(args)}],
                  "isError": bool(args.get("fail"))}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(FAKE_SERVER)
    path = tmp_path / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {"type": "stdio", "command": sys.executable, "args": [str(script)]},
                    "web": {
                        "type": "http",
                        "url": "https://example.invalid/mcp/",
                        "headers": {"Authorization": "Bearer ${MCP_TEST_KEY}"},
                    },
                }
            }
        )
    )
    monkeypatch.setenv("MCP_TEST_KEY", "k123")
    return path


def test_a_stdio_tool_answers_and_its_error_is_exit_1(config, capsys):
    assert mcp_call.main(["fake", "echo", '{"q": 1}', "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == {"q": 1}
    assert mcp_call.main(["fake", "echo", '{"fail": true}', "--config", str(config)]) == 1
    assert mcp_call.main(["fake", "--config", str(config)]) == 0
    assert capsys.readouterr().out.strip().endswith("echo: Echo back.")


def test_no_server_bad_arguments_or_an_unset_variable_is_exit_2(config, capsys, monkeypatch):
    assert mcp_call.main(["nosuch", "x", "--config", str(config)]) == 2
    assert "no MCP server named 'nosuch'" in capsys.readouterr().err
    assert mcp_call.main(["fake", "echo", "[1]", "--config", str(config)]) == 2
    monkeypatch.delenv("MCP_TEST_KEY")
    assert mcp_call.main(["web", "x", "--config", str(config)]) == 2
    assert "${MCP_TEST_KEY}" in capsys.readouterr().err


def test_the_config_expands_variables_like_claude_code(config, monkeypatch):
    server = mcp_call.find_server("web", [config])
    assert server["headers"]["Authorization"] == "Bearer k123"
    monkeypatch.delenv("MCP_TEST_KEY")
    assert mcp_call.expand("${MCP_TEST_KEY:-none}") == "none"


def test_an_http_answer_is_read_from_json_or_an_event_stream():
    reply = {"jsonrpc": "2.0", "id": 2, "result": {"content": []}}
    assert mcp_call._parse_http(json.dumps(reply), "application/json", 2) == reply
    stream = (
        "event: message\ndata: "
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
        + "\n\nevent: message\ndata: "
        + json.dumps(reply)
        + "\n\n"
    )
    assert mcp_call._parse_http(stream, "text/event-stream", 2) == reply
    with pytest.raises(mcp_call.McpCallError):
        mcp_call._parse_http("data: {}\n\n", "text/event-stream", 2)


def test_the_answer_prints_text_parts_else_structured_content():
    assert mcp_call.result_text({"content": [{"type": "text", "text": "a"}]}) == "a"
    assert mcp_call.result_text({"content": [], "structuredContent": {"n": 1}}) == '{"n": 1}'


def test_a_call_reads_only_its_own_account_config(tmp_path, monkeypatch):
    """Two accounts may each name a ``sentry`` with their own key: a check uses its chat's."""
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        server = {"type": "http", "url": f"https://{name}.invalid/mcp/"}
        (root / ".claude.json").write_text(json.dumps({"mcpServers": {"sentry": server}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "two"))
    assert mcp_call.config_files() == [tmp_path / "two" / ".claude.json"]
    found = mcp_call.find_server("sentry", mcp_call.config_files())
    assert found["url"] == "https://two.invalid/mcp/"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "one"))
    assert mcp_call.config_files() == [tmp_path / "one" / ".claude.json"]


def test_an_event_stream_left_open_after_its_answer_returns_at_once():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "id" not in body:
                self.send_response(202)
                self.end_headers()
                return
            result = {"content": [{"type": "text", "text": "ok"}]}
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Mcp-Session-Id", "s1")
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {json.dumps(reply)}\n\n".encode())
            self.wfile.flush()
            done.wait(10)  # the stream stays open after the answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        result = mcp_call.call_http({"url": url}, "tools/call", {"name": "x"}, timeout_s=5)
        assert mcp_call.result_text(result) == "ok"
    finally:
        done.set()
        server.shutdown()
        server.server_close()
