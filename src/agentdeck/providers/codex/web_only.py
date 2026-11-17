"""Codex chats that can only search and read the web (#4381).

``agentdeck research`` hands a question that may come from anyone (a voice chat in the
kitchen) to a Codex chat. A read-only sandbox stops writes but not reads, so the lock
is Codex's own configuration, sent with ``thread/start``: no shell, no MCP servers, no
sub-agents, no plugins, apps, memories, images, browser or computer use, and live web
search on. Each setting below was checked against codex-cli 0.166 (see the design on
#4381); two obvious ones do not work there and are not used: ``mcp_servers = {}``
merges into the account's servers instead of clearing them, and
``features.multi_agent = false`` leaves ``spawn_agent`` in place.

One tool cannot be switched off: ``apply_patch``. The sandbox stops it writing, and its
error shows only the patch's own lines, but it can tell whether an exact guessed line is
in a file.

A web-only chat is one-shot: it gets the first turn its start sends and nothing else.
It is never resumed, so whether ``thread/resume`` keeps ``config`` never matters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# Every Codex feature that hands the model a way to reach this host, an account or
# another agent. The shell is two features (`shell_tool`, `unified_exec`).
FEATURES_OFF = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "memories",
    "view_image",
    "computer_use",
    "browser_use",
    "in_app_browser",
    "image_generation",
    "hooks",
    "goals",
    "tool_suggest",
    "skill_search",
    "multi_agent",
)

# The one folder a web-only chat runs in: empty, and nobody's checkout.
RESEARCH_CWD = Path.home() / ".local/share/agentdeck/research-cwd"

# A web-only chat's whole developer instructions. Not the deck's session guide: that
# names this chat's session token (a loopback credential) and the deck's own tools,
# none of which an untrusted question may see or use (#4381). The token stays in
# the deck's own records.
WEB_ONLY_INSTRUCTIONS = (
    "You are a web researcher. Your only tool is web search; this chat has no shell,"
    " no files and no other tools. Answer the question you are given, citing each page"
    " you used by its full URL."
)

WEB_ONLY_TURN_REFUSAL = (
    "this is a web-only research chat: it answers one question and takes no further"
    " messages; ask again with agentdeck research"
)
WEB_ONLY_SWITCH_REFUSAL = "a web-only research chat has no permission switches"
WEB_ONLY_RELOAD_REFUSAL = (
    "this web-only research chat is not running in this process and is never reopened"
)
WEB_ONLY_CUT_OFF = "web-only research chat was cut off by a runtime restart"
WEB_ONLY_FORK_REFUSAL = "a web-only research chat cannot be copied into a side chat"


def mcp_server_names(*configs: Mapping[str, Any] | None) -> set[str]:
    """Every MCP server name any of these Codex configs declares."""
    names: set[str] = set()
    for config in configs:
        servers = (config or {}).get("mcp_servers")
        if isinstance(servers, Mapping):
            names.update(str(name) for name in servers)
    return names


def web_only_config(mcp_servers: Iterable[str]) -> dict[str, Any]:
    """The ``thread/start`` config override that locks a chat to web search."""
    return {
        "features": {name: False for name in FEATURES_OFF},
        # `features.multi_agent = false` does not remove `spawn_agent`; these do.
        "agents": {"enabled": False, "max_depth": 0},
        "web_search": "live",
        "project_doc_max_bytes": 0,
        # An empty table merges; each server has to be switched off by name.
        "mcp_servers": {name: {"enabled": False} for name in sorted(mcp_servers)},
    }


def research_cwd_refusal(cwd: Path) -> str | None:
    """Why ``cwd`` cannot hold a web-only chat, or ``None`` when it is an empty folder."""
    try:
        if not cwd.is_dir():
            return f"the research folder {cwd} does not exist"
        if any(cwd.iterdir()):
            return f"the research folder {cwd} is not empty"
    except OSError as exc:
        return f"the research folder {cwd} cannot be read: {exc}"
    return None


def ensure_research_cwd(path: Path = RESEARCH_CWD) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
