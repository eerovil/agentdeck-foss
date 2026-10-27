"""Reading a GitHub credential file, in either shape one is written in (#2105).

Two conventions already exist on this host and both are legitimate:

* A **bare token** on its own line — what ``~/.config/agentdeck/poller-token``
  has always been, and what a poller overlay's ``github_token_file`` reads.
* A **shell environment file** assigning a named variable — what
  ``~/.config/agentdeck/agent-github.env`` is, because that is the shape a
  person writes when they want ``set -a; . file`` to work from a terminal, and
  it is the file the agent account's PAT was actually installed into.

Pointing the deck at a file the operator has not got is how a credential change
silently leaves everything unauthenticated, so the answer is one reader that
accepts both rather than a second file nobody creates. It lives in the
engine package for the same reason ``rate_limits`` does: the poller and the deck
both need it, and the deck may import the engine but never the reverse.
"""

from __future__ import annotations

from pathlib import Path

# The variable an environment-file credential is assigned to. The agent
# account's installed file uses this name.
AGENT_TOKEN_VAR = "AGENTDECK_AGENT_GITHUB_TOKEN"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def parse_token(text: str, variable: str = AGENT_TOKEN_VAR) -> str:
    """The token in a credential file's contents, or ``""``.

    Comments, blank lines, ``export`` and quotes are tolerated. An assignment to
    *variable* wins over a bare line wherever it appears, so an environment file
    that happens to open with a comment-free bare line still reads correctly.

    An unparseable file yields ``""`` rather than raising: a broken credential
    file must cost the identity it carries, not the process reading it.
    """
    fallback = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == variable:
            return _unquote(value)
        if not sep and not fallback:
            fallback = _unquote(line)
    return fallback


def read_token(path: str | Path, variable: str = AGENT_TOKEN_VAR) -> str:
    """The token in the credential file at *path*, or ``""`` if unreadable."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return parse_token(text, variable)
