#!/usr/bin/env python3
"""Check what a locked chat really sends to the model, with the installed Claude CLI (#4606).

Starts ``claude`` with the command and environment the deck gives a locked chat, in a
throwaway folder with a planted auto-memory and, above it, another guide. Its settings do
not deny ``Read``: that makes Claude Code drop every guide, and would hide one it loads.
The API it talks to is a small server in this process, so the check costs no quota and
reads the exact request. It fails unless

- the folder's guide is in the request, in a fresh chat and in a resumed one;
- the owner's auto-memory and the guide above are in neither, while a control run without
  the deck's environment holds both, so the check can see them;
- the only tools are web search and the deck's page reader.

    .venv/bin/python scripts/check_locked_chat.py    # exit 0 when all hold
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentdeck.providers.claude_code import locked  # noqa: E402

GUIDE = "locked-guide-marker-7f3a"
ABOVE = "guide-above-marker-c52d"
MEMORY = "owner-memory-marker-91bc"
OWNER = {MEMORY: "owner's auto-memory", ABOVE: "guide above its folder"}
TIMEOUT_S = 120
# Sent by the CLI itself to keep deferred tool loading on; it does nothing.
PLACEHOLDERS = {"DeferredToolPlaceholder"}


def _reply() -> bytes:
    """One streamed answer, as the Messages API sends it."""
    message = {
        "id": "msg_check",
        "type": "message",
        "role": "assistant",
        "model": "check",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    events = [
        {"type": "message_start", "message": message},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class _Api(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.split("?")[0] != "/v1/messages":
            self.send_error(404)
            return
        self.server.requests.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_reply())


def _run(api: _Api, command: list[str], cwd: Path, env: dict[str, str]) -> tuple[list[dict], str]:
    """The requests one turn of ``command`` made, and its session id."""
    api.requests.clear()
    turn = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }
    done = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=json.dumps(turn) + "\n",
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    session = ""
    for line in done.stdout.splitlines():
        event = json.loads(line) if line.startswith("{") else {}
        session = session or event.get("session_id") or ""
    if not api.requests or not session:
        raise RuntimeError(
            f"claude exited {done.returncode} without a request: "
            f"{(done.stderr or done.stdout).strip()[-800:]}"
        )
    return list(api.requests), session


def problems(control: list[dict], fresh: list[dict], resumed: list[dict]) -> list[str]:
    """What is wrong with the requests of the three runs; empty when all is right."""
    found = []
    for marker, what in OWNER.items():
        if marker not in json.dumps(control):
            found.append(f"the control run held no {what}, so this check cannot see one")
    for name, requests in (("fresh", fresh), ("resumed", resumed)):
        if not requests:
            found.append(f"the {name} chat made no request")
            continue
        sent = json.dumps(requests)
        if GUIDE not in sent:
            found.append(f"the {name} chat did not get its folder's guide")
        found += [
            f"the {name} chat got the {what}" for marker, what in OWNER.items() if marker in sent
        ]
        names = {str(tool.get("name")) for body in requests for tool in body.get("tools") or []}
        extra = names - {*locked.BUILT_IN_TOOLS, locked.FETCH_TOOL} - PLACEHOLDERS
        if extra:
            found.append(f"the {name} chat has tools it should not: {sorted(extra)}")
    return found


def check() -> list[str]:
    api = _Api()
    threading.Thread(target=api.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="locked-chat-check-") as name:
        root = Path(name).resolve()
        repo, config = root / "repo", root / "config"
        folder = repo / "folder"
        folder.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "CLAUDE.md").write_text(f"{ABOVE}\n", encoding="utf-8")
        (folder / "CLAUDE.md").write_text(f"{GUIDE}\n", encoding="utf-8")
        (folder / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
        memory = config / "projects" / slug / "memory"
        memory.mkdir(parents=True)
        (memory / "MEMORY.md").write_text(f"- {MEMORY}\n", encoding="utf-8")
        env = locked.lock_env(
            {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(root),
                "CLAUDE_CONFIG_DIR": str(config),
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api.server_port}",
                "ANTHROPIC_API_KEY": "sk-ant-not-a-key",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        fetch = locked.write_fetch_config(root / "artifacts")

        def command(resume_id: str | None = None) -> list[str]:
            return locked.command(
                str(folder),
                fetch,
                locked.mcp_servers(str(folder)),
                locked.folder_guide(str(folder)),
                model="sonnet",
                resume_id=resume_id,
            )

        open_env = {key: value for key, value in env.items() if key not in locked.LOCKED_ENV}
        control, _ = _run(api, command(), folder, open_env)
        fresh, session = _run(api, command(), folder, env)
        resumed, _ = _run(api, command(session), folder, env)
    api.shutdown()
    return problems(control, fresh, resumed)


def main() -> int:
    try:
        found = check()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        found = [f"the check could not run: {error}"]
    for line in found:
        print(f"FAIL: {line}")
    if not found:
        print("ok: the guide reaches a locked chat, the owner's memory does not")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
