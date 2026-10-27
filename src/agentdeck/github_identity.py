"""Which GitHub identity speaks for a given repository (#2105, #2274, #2289).

Every automated GitHub call the deck makes — the web process's ``gh`` lookups,
a worker's ``gh`` through the shim, a worker's ``git`` push through the
credential helper — asks here first, and gets exactly one answer: the
non-human authority configured for the repository's owner, or a refusal.

* each owner listed in ``github-authority.toml`` → the dedicated AgentDeck GitHub
  App's installation on that account, or the agent account named for it (``eerovil/*``
  used the ``eerovil-agent`` account until GitHub suspended it, #2682).
* anything else → refused, with a reason naming the owner.

There is no fallback. The operator's own ``gh`` login, an inherited
``GH_TOKEN``, a second identity tried after the first failed — none of them is
ever used, because each of them is a human's credential (#2289). A missing grant
fails loudly instead of quietly succeeding as a person.

The rule itself lives in ``poller_engine.github_authority``, because the Poller
must apply the same one and may import only the standard library. This module is
the deck's face on it: reading an owner off whatever a caller holds, and
building the child environment for a ``gh`` or ``git`` call.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Sequence

from .poller_engine import github_authority as _authority
from .poller_engine.github_authority import (  # noqa: F401 — re-exported
    ACCOUNTS_FILE_ENV,
    AGENT,
    AGENT_TOKEN_FILE_ENV,
    AUTHORITY_ENV,
    DEFAULT_ACCOUNTS_FILE,
    DEFAULT_AGENT_TOKEN_FILE,
    DEFAULT_OWNERS,
    TOKEN_CACHE_ENV,
    TOKEN_VARS,
    AccountsConfig,
    Authority,
    ConfigError,
    Refusal,
    accounts_path,
    load_accounts,
    owner_of,
    parse_accounts,
)

# Kept under its old name: the agent account's credential file is still this one.
TOKEN_FILE_ENV = AGENT_TOKEN_FILE_ENV
DEFAULT_TOKEN_FILE = DEFAULT_AGENT_TOKEN_FILE


def _owner(target: object = None, owner: str | None = None) -> str:
    if owner:
        return str(owner).strip().lower()
    return owner_of(target)


def authority_for(target: object = None, *, owner: str | None = None) -> Authority:
    """The authority for ``target`` (a repository reference) or an explicit ``owner``.

    Raises ``Refusal`` when neither names an owner, or the owner has no authority.
    """
    resolved = _owner(target, owner)
    if not resolved:
        shown = str(target or "").strip()
        raise Refusal(f"no target owner{f' in {shown!r}' if shown else ''}")
    return _authority.authority_for(resolved)


def environment_for(
    target: object = None,
    environment: dict[str, str] | None = None,
    *,
    owner: str | None = None,
) -> dict[str, str]:
    """A child environment that speaks as ``target``'s authority, and only as it.

    Every credential variable is replaced or removed. Raises ``Refusal``; there is
    no "keep the caller's own credential" answer any more.
    """
    authority = authority_for(target, owner=owner)
    return _authority.environment(authority, _authority.token(authority), environment)


def gh_main(args: Sequence[str]) -> int:
    """``agentdeck gh …``: one ``gh`` call as its target owner's authority (#2289).

    For a script outside an owned worker — which has the shim on ``PATH`` — that
    must not run ``gh`` as whoever the host is logged in as. The owner is read off
    the command exactly as the web process reads it; no owner, or an owner with no
    authority, is refused with exit 77 before ``gh`` runs.
    """
    argv = ["gh", *[str(part) for part in args]]
    try:
        environment = environment_for(owner=owner_in_argv(argv))
    except Refusal as refusal:
        print(f"agentdeck gh: refused — github: {refusal}", file=sys.stderr)
        return 77
    try:
        return subprocess.run(argv, env=environment, check=False).returncode
    except FileNotFoundError:
        print("agentdeck gh: gh is not installed", file=sys.stderr)
        return 127


def owner_in_argv(argv: Sequence[str] | None) -> str:
    """Which owner a ``gh`` command line is about, or ``""``.

    Read off the command the caller already wrote — ``--repo owner/name``, an
    ``api repos/owner/name/...`` path, a ``github.com`` URL — so it cannot drift
    out of step with the call. A positional merely shaped like ``owner/name`` (a
    branch, an API path such as ``search/issues``) names nobody. Only for ``gh``:
    ``git`` gets its credential from the helper instead.
    """
    parts = [str(part) for part in (argv or [])]
    if not parts or os.path.basename(parts[0]) not in ("gh", "gh.exe"):
        return ""
    positionals: list[str] = []
    skip = False
    for index, part in enumerate(parts[1:], start=1):
        if skip:
            skip = False
            continue
        if part in ("-R", "--repo"):
            return owner_of(parts[index + 1]) if index + 1 < len(parts) else ""
        flag, sep, value = part.partition("=")
        if sep and flag in ("-R", "--repo"):
            return owner_of(value)
        if part.startswith("-"):
            # A flag's value is not a positional: `-f query=…`, `--jq .x`, `--json a,b`.
            skip = not sep and part not in _BOOLEAN_FLAGS
            continue
        positionals.append(part)
    return _authority.owner_in_positionals(positionals)


# `gh` flags that take no value, so the word after them is a positional.
_BOOLEAN_FLAGS = frozenset(
    {"--paginate", "--slurp", "--include", "-i", "--silent", "--verbose", "--web", "-w",
     "--draft", "-d", "--fill", "--admin", "--auto", "--squash", "--merge", "--rebase",
     "--delete-branch", "--disable-auto", "--comments", "-c"}
)


def credential_helper_command() -> str:
    """The git ``credential.helper`` value for AgentDeck's own helper.

    Run with this interpreter, so a worker spawned by the runtime uses the same
    checkout and virtualenv the runtime does.
    """
    return f"!{shlex.quote(sys.executable)} -m agentdeck.git_credential"


# The programs git runs to ask a person for a username or password.
PROMPT_VARS = ("GIT_ASKPASS", "SSH_ASKPASS")


def git_config_entries() -> list[tuple[str, str]]:
    """The git settings that send every GitHub transport through the helper.

    SSH spellings are rewritten to HTTPS, because an SSH key answers for whoever
    holds it and not per owner; the helper list is reset before AgentDeck's helper
    is added, so no earlier helper (the operator's ``gh auth git-credential``)
    is ever asked; and ``useHttpPath`` hands the helper the repository path it
    needs to pick an owner.
    """
    return [
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        ("credential.https://github.com.helper", ""),
        ("credential.https://github.com.helper", credential_helper_command()),
        ("credential.https://github.com.useHttpPath", "true"),
        # A refused credential must fail the command, never ask a person (#3632).
        ("credential.interactive", "false"),
    ]


def configure_github_git(environment: dict[str, str]) -> None:
    """Make a child's GitHub ``git`` and ``gh`` speak only through the owner rule.

    Appended to whatever ``GIT_CONFIG_*`` entries the caller already set, and every
    inherited credential variable is removed so nothing the parent process carries
    can speak instead: the shim injects the right token per ``gh`` call, and the
    helper answers per ``git`` remote.

    It never prompts, either: a credential the helper refuses fails the command.
    The deck's services inherit the desktop session's ``SSH_ASKPASS``, and git
    runs that before it looks at ``GIT_TERMINAL_PROMPT`` — so a fetch for an owner
    nobody mapped opened a password box on the host's screen (#3632).
    """
    for name in (*TOKEN_VARS, *PROMPT_VARS):
        environment.pop(name, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        index = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        index = 0
    for key, value in git_config_entries():
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
        index += 1
    environment["GIT_CONFIG_COUNT"] = str(index)


def github_git_environment() -> dict[str, str]:
    """This process's environment for a host-side ``git`` that may reach GitHub.

    The same rule a worker gets: no inherited token, SSH GitHub URLs rewritten to
    HTTPS, AgentDeck's helper as the only credential (#2289), and no prompt (#3632).
    """
    environment = dict(os.environ)
    configure_github_git(environment)
    return environment
