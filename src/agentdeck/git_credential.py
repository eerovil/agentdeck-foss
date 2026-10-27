"""AgentDeck's git credential helper: one HTTPS credential per repository owner (#2289).

Git runs a helper as ``<helper> get`` and writes the request to its stdin as
``key=value`` lines (``protocol``, ``host``, ``path``). With
``credential.useHttpPath=true`` the path is the repository, ``owner/name.git``,
so the owner rule can answer:

* an owner mapped to the agent account → ``eerovil-agent`` and its PAT;
* an owner mapped to an App installation → ``x-access-token`` and a minted
  installation token;
* anything else — another owner, another host, ``gist.github.com``, no path —
  prints nothing. That is the refusal; the reason goes to stderr, which git shows
  to whoever ran it. Nothing here makes git fail: left alone it asks a person
  instead (``SSH_ASKPASS``, a terminal), so ``configure_github_git`` switches
  prompting off (#3632).

``store`` and ``erase`` are accepted and ignored: nothing is stored, because the
authority is the source of truth and an installation token expires anyway.

Installed globally by the host cutover (``credential.https://github.com.helper``);
``github_identity.configure_github_git`` sets it for owned workers.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

from .poller_engine import github_authority

# The username GitHub documents for an installation token over HTTPS.
APP_USERNAME = "x-access-token"


def parse_request(lines: Iterable[str]) -> dict[str, str]:
    """Git's request, up to the blank line that ends it."""
    request: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            request[key.strip()] = value
    return request


def owner_from_request(request: dict[str, str]) -> str:
    """The case-folded repository owner git is asking about, or ``""``.

    Only ``https://github.com/<owner>/<name>[.git]``. A ``url=`` line is honoured
    for a git that sends one instead of the split fields.
    """
    protocol = request.get("protocol", "").strip().lower()
    host = request.get("host", "").strip().lower()
    path = request.get("path", "")
    url = request.get("url", "").strip()
    if url and not (protocol and host):
        scheme, _, rest = url.partition("://")
        protocol = scheme.lower()
        host, _, path = rest.partition("/")
        host = host.rsplit("@", 1)[-1].lower()
    if protocol != "https" or host != "github.com":
        return ""
    segments = [part for part in path.split("?", 1)[0].strip("/").split("/") if part]
    if len(segments) < 2:
        return ""
    name = segments[1][:-4] if segments[1].endswith(".git") else segments[1]
    if not name:
        return ""
    return github_authority.owner_of(f"{segments[0]}/{name}")


def answer(request: dict[str, str]) -> tuple[str, str]:
    """``(stdout, stderr)`` for one ``get`` request."""
    owner = owner_from_request(request)
    if not owner:
        host = request.get("host", "") or "this host"
        return "", (
            f"agentdeck git-credential: no GitHub repository owner in the request for {host}\n"
        )
    try:
        authority = github_authority.authority_for(owner)
        credential = github_authority.token(authority)
    except github_authority.Refusal as refusal:
        return "", f"agentdeck git-credential: {refusal}\n"
    username = authority.login if authority.name == github_authority.AGENT else APP_USERNAME
    return f"username={username}\npassword={credential}\n\n", ""


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = arguments[0] if arguments else ""
    request = parse_request(sys.stdin)
    if action != "get":
        return 0
    stdout, stderr = answer(request)
    if stderr:
        sys.stderr.write(stderr)
    sys.stdout.write(stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
