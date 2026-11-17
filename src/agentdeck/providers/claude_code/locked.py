"""The start command of a ``locked`` chat: web search, public pages and its folder's tools.

A locked chat is for people who must not reach this host: it has no shell, cannot read
or edit files, start helper agents, run skills, or use any MCP server but the deck's
page reader and the ones its own folder's ``.mcp.json`` names. The limits are in the
command that starts it, not in settings a folder could change:

- ``--tools WebSearch``: the only built-in tool that exists in the chat. The built-in
  WebFetch is not one: it looks a name up again when it connects, after any check, so
  a name whose DNS answer changes in between would reach this host or the home network.
- ``--strict-mcp-config --mcp-config <artifacts>/locked-fetch-mcp.json
  <cwd>/.mcp.json``: the deck's page reader (``scripts/locked_fetch_server.py``, which
  looks a host up once, refuses any address that is not public and connects to the one
  it checked) and the folder's own servers; no account or user MCP server, and
  ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` keeps the account's claude.ai connectors off. A
  folder server named like the reader is refused, so a folder cannot replace it.
- ``--setting-sources project``: the owner's own guide and user settings are not loaded.
- ``--disable-slash-commands``: no skills.
- ``--permission-mode dontAsk`` with an allow list: whatever is not allowed is refused
  without a prompt. Never bypass, and no ``--allow-dangerously-skip-permissions``.
- ``--system-prompt``: a short prompt instead of AgentDeck's session guide.

Every spawn, fresh or resumed, writes and reads back the reader's MCP config and reads
the folder's ``.mcp.json``; when either fails the spawn is refused rather than started
without its limits.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from ..instructions import LOCKED_SYSTEM_PROMPT

LOCKED_ROLE = "locked"
FETCH_CONFIG_NAME = "locked-fetch-mcp.json"
FETCH_SERVER = "deckfetch"
FETCH_TOOL = f"mcp__{FETCH_SERVER}__hae_sivu"
BUILT_IN_TOOLS = ("WebSearch",)
_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class LockedChatRefused(RuntimeError):
    """A locked chat cannot start with all of its limits, so it does not start."""


def fetch_server_script() -> Path:
    return Path(__file__).resolve().parents[4] / "scripts" / "locked_fetch_server.py"


def fetch_config(script: Path) -> dict:
    """The MCP config naming the deck's page reader, and nothing else."""
    return {
        "mcpServers": {
            FETCH_SERVER: {"type": "stdio", "command": sys.executable, "args": ["-I", str(script)]}
        }
    }


def write_fetch_config(directory: Path, script: Path | None = None) -> Path:
    """Write the page reader's MCP config, read it back, and answer its path.

    Called on every spawn, so a cleaned-up artifact directory is filled again and a
    write that did not land refuses the spawn.
    """
    script = script or fetch_server_script()
    if not script.is_file():
        raise LockedChatRefused(f"the page reader {script} is missing")
    expected = fetch_config(script)
    path = directory / FETCH_CONFIG_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{FETCH_CONFIG_NAME}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(expected, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        written = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LockedChatRefused(f"could not write {path}: {exc}") from exc
    if written != expected:
        raise LockedChatRefused(f"{path} does not hold the page reader")
    return path


def mcp_servers(cwd: str) -> list[str]:
    """The MCP server names the chat's folder declares in ``.mcp.json``."""
    path = Path(cwd) / ".mcp.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LockedChatRefused(f"could not read {path}: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise LockedChatRefused(f"{path} has no mcpServers table")
    names = list(servers)
    bad = [name for name in names if not isinstance(name, str) or not _SERVER_NAME.match(name)]
    if bad:
        raise LockedChatRefused(f"{path} names a server the allow list cannot hold: {bad!r}")
    if FETCH_SERVER in names:
        raise LockedChatRefused(f"{path} names the deck's own page reader {FETCH_SERVER!r}")
    return names


def allowed_tools(servers: list[str]) -> str:
    return " ".join([*BUILT_IN_TOOLS, FETCH_TOOL, *(f"mcp__{name}__*" for name in servers)])


def command(
    cwd: str,
    fetch: Path,
    servers: list[str],
    *,
    model: str | None,
    resume_id: str | None = None,
    resume_at: str | None = None,
) -> list[str]:
    """The argv of one locked chat spawn."""
    cmd = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "project",
        "--tools",
        ",".join(BUILT_IN_TOOLS),
        "--strict-mcp-config",
        "--mcp-config",
        str(fetch),
        str(Path(cwd) / ".mcp.json"),
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        allowed_tools(servers),
        "--system-prompt",
        LOCKED_SYSTEM_PROMPT,
    ]
    if model:
        cmd += ["--model", model]
    if resume_id:
        cmd += ["--resume", resume_id]
        if resume_at:
            cmd += ["--resume-session-at", resume_at]
    return cmd


# What a locked chat's process is not given: the deck's address and every identity
# it hands its workers (``AGENTDECK_*``: session, account, worker, gh consumer), and
# any GitHub or git credential setup (tokens, the deck's credential helper entries,
# askpass programs). It has no tool to use them with, and must not have them.
WITHHELD_PREFIXES = ("AGENTDECK_", "GH_", "GITHUB_", "GIT_CONFIG_")
WITHHELD_ENV = (
    "AGENTDECK_URL",
    "AGENTDECK_SESSION_KEY",
    "AGENTDECK_SESSION_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_CONFIG_COUNT",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
)


def lock_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` with nothing of the deck's in it, for a locked chat's process.

    Called on the plain inherited environment, before a worker's own deck setup,
    and strips again what an inherited one may already carry, the gh shim's
    directory on PATH included."""
    from ...gh_shim.install import shim_dir

    for name in list(env):
        if name in WITHHELD_ENV or name.startswith(WITHHELD_PREFIXES):
            del env[name]
    shim = str(shim_dir())
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(
            part for part in env["PATH"].split(os.pathsep) if part and part != shim
        )
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env
