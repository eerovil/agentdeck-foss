"""``agentdeck-init``: set a fresh machine up to run one Work, in one command (#3623).

The manual setup is four files and a dozen commands (``docs/github-setup.md``,
``poller/reference/README.md``). This walks the same path and asks only what it cannot
find out:

1. the deck's ``config.toml``, with the Claude Code and Codex folders that exist here;
2. the first project's repository, ``<login>/agent-tasks`` unless another is named;
3. the deck's own GitHub App, made with GitHub's App-manifest flow (one press of
   "Create", the key comes back here), and its installation;
4. ``github-authority.toml`` mapping that owner to the App;
5. a checkout, and a small poller project for the repository (no board, no extensions,
   merged when a person presses Merge), turned on with ``activate --apply``;
6. the deck's systemd units and the project's timer.

Each step that finds its result already there leaves it alone and says so, so running it
again after a failure carries on where it stopped. Nothing here keeps or prints the user's
own GitHub credential: creating the repository uses ``gh`` only when it is already logged
in, and everything after it speaks as the App.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import html
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import poller_host
from .poller_engine import github_authority

REPO = Path(__file__).resolve().parent.parent.parent
API = github_authority.API
CONFIG = Path("~/.config/agentdeck/config.toml")
KEY_FILE = "~/.config/agentdeck/github-app.pem"
# The App's id and slug, kept the moment it exists, so a run stopped before the accounts
# file is written reuses that App instead of creating a second one.
APP_RECORD = "~/.config/agentdeck/github-app.json"
# The state of an App creation waiting for its code, for an `--app-code` rerun.
APP_PENDING = "~/.config/agentdeck/github-app-pending.json"
PROJECTS = Path("~/.config/agentdeck/projects")
DECK_UNITS = ("agentdeck.socket", "agentdeck.service", "agentdeck-codex.service")
DEFAULT_REPO_NAME = "agent-tasks"
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_REPO = re.compile(r"^([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)$")

# What the deck's App may do: the list in docs/github-setup.md, step 1. No `workflows`: with it an
# agent could change a CI file on its own branch and run code with the repository's secrets, and
# without it GitHub refuses any push that touches `.github/workflows` (#3623, owner's choice).
APP_PERMISSIONS = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "actions": "read",
    "checks": "read",
    "statuses": "read",
    "metadata": "read",
}


class SetupError(Exception):
    """A step that cannot go on; the message says what to do."""


# --- what gets written ------------------------------------------------------------------------


def detected_accounts(home: Path) -> list[tuple[str, str, str]]:
    """``(provider, label, config_dir)`` for each agent CLI folder that exists here."""
    found = []
    if (home / ".claude").is_dir():
        found.append(("claude_code", "main", "~/.claude"))
    if (home / ".codex").is_dir():
        found.append(("codex", "codex", "~/.codex"))
    return found


def config_text(example: str, accounts: list[tuple[str, str, str]]) -> str:
    """The example config with injection and the deck's Claude agents on, and only the
    accounts found on this machine.

    Injection is what lets the deck start and message its own chats, and a Work's agent is one
    of its Claude agents. Their permission mode is written out as ``auto`` (Claude Code's
    classifier reviews risky tool calls; a blocked one waits on the Work page for a person),
    so nobody has to know it is the default (#3623, owner's choice).
    """
    text = example.replace("[inject]\nenabled = false", "[inject]\nenabled = true", 1)
    text = text.replace("#[claude_workers]\n#enabled = true", "[claude_workers]\nenabled = true", 1)
    text = text.replace('#permission_mode = "auto"', 'permission_mode = "auto"', 1)
    head, sep, _rest = text.partition("\n[[accounts]]\n")
    blocks = [
        f'[[accounts]]\nprovider = "{provider}"\nlabel = "{label}"\nconfig_dir = "{folder}"\n'
        for provider, label, folder in accounts
    ]
    return head.rstrip() + "\n\n" + "\n".join(blocks) if sep else text


def authority_text(owner: str, app_id: int, slug: str, installation_id: int) -> str:
    """``github-authority.toml`` mapping one owner to the deck's App installation."""
    owner = owner.lower()
    return (
        "# Written by `agentdeck-init`. Which identity speaks for each GitHub owner;\n"
        "# docs/github-setup.md explains each table.\n\n"
        f'[owners]\n{owner} = "app:{owner}"\n\n'
        f"[app]\napp_id = {app_id}\n"
        f'slug = "{slug}"\n'
        f'private_key_file = "{KEY_FILE}"\n'
        f"installations = {{ {owner} = {installation_id} }}\n"
    )


def manifest_text(owner: str, repo: str) -> str:
    """A poller project for one repository with no board: Works start from labelled issues,
    or the deck's New work button, and merge when a person presses Merge."""
    return f"""# Written by `agentdeck-init`: one repository, no board, no extensions.
# poller/reference/poller.toml explains every key.
contract = 1
org = "{owner.lower()}"
ownership_label = "agent"
spec_only_label = "spec-only"
paused_label = "paused"

[policy]
merge_method = "squash"
worker_cap = 3
terminal_recheck_s = 900
graphql_reserve = 200
core_reserve = 500

[[repos]]
id = "{repo}"
holds_issues = true
holds_code = true
verification = "review_only"

[authorization]
[authorization.work]
role = "triage"
[authorization.release]
role = "maintain"

[capabilities]
work_control = true
guarded_release = true
merge_review = true
deployment_observation = false
artifacts = false
auto_release = false
"""


def overlay_text(instance: Path, repo: str, checkout: str, account: str) -> str:
    return f"""# Written by `agentdeck-init`. This machine's half of the project:
# poller/reference/overlay.example.toml explains every key.
contract = 1

[paths]
state = "{instance / "state.json"}"
log = "{instance / "cycle.log"}"
extensions = "{REPO / "poller" / "extensions"}"

[checkouts]
{repo} = "{checkout}"

[worker]
account = "{account}"

[credentials]
"""


def app_manifest(login: str, repo_url: str, redirect_url: str) -> dict:
    """What GitHub's App-manifest flow creates: a private App with the deck's permissions.

    App names are unique across GitHub, so a short random tail keeps a second deck of the same
    person (or a rerun after deleting an App) from landing on a taken name; GitHub caps it at 34.
    """
    return {
        "name": f"AgentDeck {login}"[:29] + f" {secrets.token_hex(2)}",
        "url": repo_url,
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": APP_PERMISSIONS,
        "default_events": [],
    }


def app_creation_url(owner: str, is_org: bool, state: str) -> str:
    base = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if is_org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={urllib.parse.quote(state)}"


def creation_page(action: str, manifest: dict) -> str:
    """The local page that posts the manifest to GitHub; GitHub takes it only as a form."""
    return (
        "<!doctype html><title>AgentDeck setup</title>"
        f'<form id="f" method="post" action="{html.escape(action)}">'
        f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">'
        "<p>Sending you to GitHub to create the deck's App…</p>"
        '<button type="submit">Continue to GitHub</button></form>'
        '<script>document.getElementById("f").submit()</script>'
    )


def tunnel_command(port: int) -> str:
    """How a browser on another machine reaches a page this machine serves on 127.0.0.1."""
    return f"ssh -L {port}:127.0.0.1:{port} {getpass.getuser()}@{socket.gethostname()}"


def app_prompt(port: int, wait_minutes: float) -> str:
    """The person's step for creating the App, printed while this run waits for it.

    GitHub sends the browser back to the same local address, which the tunnel also carries;
    without one, the address the browser ended on finishes the step on a rerun.
    """
    return (
        "PERSON: Ask the person to open this link and press 'Create GitHub App' on GitHub. "
        "If their browser is on another machine, they first run the TUNNEL command there and "
        "keep it open.\n"
        f"OPEN: http://127.0.0.1:{port}/\n"
        f"TUNNEL: {tunnel_command(port)}\n"
        f"WAITING: up to {wait_minutes:g} minutes for GitHub to send the browser back here."
    )


def code_from(text: str, state: str) -> str:
    """The manifest code from the address GitHub sent the browser back to (or the bare code)."""
    text = text.strip()
    if "?" not in text and re.fullmatch(r"[A-Za-z0-9_-]{8,}", text):
        return text
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
    if query.get("state", [""])[0] != state:
        raise SetupError("that address is not from this setup (its state does not match)")
    code = query.get("code", [""])[0]
    if not code:
        raise SetupError("that address carries no code")
    return code


# --- GitHub -----------------------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], object]


def _call(
    method: str,
    path: str,
    *,
    auth: str = "",
    body: dict | None = None,
    opener: Opener | None = None,
) -> tuple[int, object]:
    request = urllib.request.Request(
        f"{API}{path}",
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentdeck-init",
            **({"Authorization": auth} if auth else {}),
        },
    )
    try:
        response = (opener or github_authority._default_open)(request, 30.0)
        with response:  # type: ignore[attr-defined]
            raw = response.read()  # type: ignore[attr-defined]
            return response.status, json.loads(raw) if raw else None  # type: ignore[attr-defined]
    except urllib.error.HTTPError as exc:
        return exc.code, {"message": github_authority._error_message(exc)}


def convert_manifest(code: str, opener: Opener | None = None) -> dict:
    """Trade the flow's one-time code for the App: its id, slug and private key."""
    status, body = _call("POST", f"/app-manifests/{code}/conversions", opener=opener)
    if status != 201 or not isinstance(body, dict) or not body.get("pem"):
        said = body.get("message") if isinstance(body, dict) else ""
        raise SetupError(f"GitHub did not hand the App over (HTTP {status}: {said})")
    return body


def find_installation(app_id: int, pem: str, owner: str, opener: Opener | None = None) -> int:
    """The App's installation on ``owner``, or 0 when it is not installed there yet."""
    auth = f"Bearer {github_authority.app_jwt(app_id, pem)}"
    status, body = _call("GET", "/app/installations?per_page=100", auth=auth, opener=opener)
    if status != 200 or not isinstance(body, list):
        raise SetupError(f"could not list the App's installations (HTTP {status})")
    for item in body:
        account = item.get("account") or {}
        if str(account.get("login", "")).lower() == owner.lower():
            return int(item["id"])
    return 0


def least_privilege_problems(
    app_id: int, pem: str, installation_id: int, opener: Opener | None = None
) -> list[tuple[str, str]]:
    """What makes this App reach further than the deck needs: each fix, and its settings page.

    The App must not hold ``workflows`` (an App created by hand, or before it was dropped, may),
    and its installation must list the repositories it may use rather than take them all.
    """
    auth = f"Bearer {github_authority.app_jwt(app_id, pem)}"
    problems = []
    status, app = _call("GET", "/app", auth=auth, opener=opener)
    if status != 200 or not isinstance(app, dict):
        raise SetupError(f"could not read the App's permissions (HTTP {status})")
    if (app.get("permissions") or {}).get("workflows"):
        owner = app.get("owner") or {}
        settings = (
            f"https://github.com/organizations/{owner.get('login', '')}/settings"
            if owner.get("type") == "Organization"
            else "https://github.com/settings"
        )
        problems.append(
            (
                "set the App's Workflows permission to No access, so agents cannot change CI files",
                f"{settings}/apps/{app.get('slug', '')}/permissions",
            )
        )
    status, installation = _call(
        "GET", f"/app/installations/{installation_id}", auth=auth, opener=opener
    )
    if status != 200 or not isinstance(installation, dict):
        raise SetupError(f"could not read the App's installation (HTTP {status})")
    if installation.get("repository_selection") != "selected":
        problems.append(
            (
                "set the App's installation to 'Only select repositories' and pick only the "
                "ones the deck should work on",
                str(installation.get("html_url", "")),
            )
        )
    return problems


def owner_is_org(owner: str, opener: Opener | None = None) -> bool:
    status, body = _call("GET", f"/users/{owner}", opener=opener)
    if status != 200 or not isinstance(body, dict):
        raise SetupError(f"GitHub knows no account called {owner!r}")
    return body.get("type") == "Organization"


# --- the steps --------------------------------------------------------------------------------


@dataclass
class Answers:
    login: str
    owner: str
    repo: str
    app_code: str = ""
    wait_minutes: float = 15.0

    @property
    def full(self) -> str:
        return f"{self.owner}/{self.repo}"

    def command(self, *, app_code: bool = False) -> str:
        """This run's command line, for the agent to run next."""
        line = f"~/agentdeck/.venv/bin/agentdeck-init --login {self.login} --repo {self.full}"
        return line + (" --app-code '<the address it ended on>'" if app_code else "")


class NeedsPerson(Exception):  # noqa: N818 — a stop, not an error
    """A step only a person can do: what to ask them, the links they open (each printed on a
    line of its own, so an agent can pass it on as a link), and what to run once they have."""

    def __init__(self, ask: str, then: str, links: tuple[str, ...] = ()) -> None:
        super().__init__(ask)
        self.ask = ask
        self.then = then
        self.links = links


#: The exit code of a run that stopped at a person's step; 0 is done, 1 is a failure.
NEEDS_PERSON = 3


def _say(text: str) -> None:
    print(text, flush=True)


def step_config(home: Path) -> tuple[str, str, str]:
    """Write the deck's config if there is none; return the account that runs the Works, as
    its key, provider and folder."""
    from .config import config_path, load_config

    path = config_path()
    if path.exists():
        _say(f"✓ {path} is already there; leaving it as it is.")
        loaded = load_config(path)
        if not loaded.claude_workers.enabled:
            raise SetupError(
                f"{path} leaves the deck's Claude agents off, so no Work could start: set "
                '[claude_workers] enabled = true and permission_mode = "auto"'
            )
        configured = loaded.accounts
        if not configured:
            raise SetupError(f"{path} lists no [[accounts]]; add your Claude Code or Codex folder")
        first = configured[0]
        return f"{first.provider}:{first.label}", first.provider, first.config_dir
    accounts = detected_accounts(home)
    if not accounts:
        raise SetupError(
            "no ~/.claude or ~/.codex here: install Claude Code or the Codex CLI and log in first"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config_text((REPO / "config.example.toml").read_text(), accounts))
    _say(f"✓ wrote {path} with {', '.join(f'{p}:{label}' for p, label, _ in accounts)}")
    provider, label, folder = accounts[0]
    return f"{provider}:{label}", provider, folder


def new_repository_link(answers: Answers) -> str:
    query = urllib.parse.urlencode(
        {"name": answers.repo, "owner": answers.owner, "visibility": "private"}
    )
    return f"https://github.com/new?{query}"


def step_repository(answers: Answers) -> bool:
    """Create the repository with ``gh`` when it is logged in; ``False`` when that could not
    be done here, so the person is asked to create it with the App's install."""
    gh = shutil.which("gh")
    # `gh api user` asks as the active account only; `gh auth status` fails when any stored
    # account is broken, even an unused one (agentdeck-foss#1).
    if not gh or subprocess.run([gh, "api", "user"], capture_output=True).returncode != 0:
        return False
    if subprocess.run([gh, "repo", "view", answers.full], capture_output=True).returncode == 0:
        _say(f"✓ {answers.full} exists.")
        return True
    made = subprocess.run(
        [
            gh,
            "repo",
            "create",
            answers.full,
            "--private",
            "--add-readme",
            "--description",
            "Tasks for my AgentDeck agents",
        ],
        capture_output=True,
        text=True,
    )
    if made.returncode == 0:
        _say(f"✓ created {answers.full} (private) with the gh login.")
        return True
    _say(f"gh could not create {answers.full}: {made.stderr.strip()}")
    return False


def step_app(answers: Answers, *, repository_ready: bool) -> github_authority.AppConfig:
    """Create the App (or reuse the one already set up) and find its installation."""
    path = github_authority.accounts_path()
    key = Path(KEY_FILE).expanduser()
    record = Path(APP_RECORD).expanduser()
    if path.exists():
        config = github_authority.parse_accounts(path.read_text())
        if config.app is None:
            raise SetupError(
                f"{path} exists without an [app]; finish it by hand (docs/github-setup.md)"
            )
        app = config.app
        _say(f"✓ using the App {app.slug} from {path}.")
    elif record.exists() and key.exists():
        made = json.loads(record.read_text())
        app = github_authority.AppConfig(int(made["id"]), str(made["slug"]), KEY_FILE, {})
        _say(f"✓ using the App {app.slug} this setup created earlier.")
    else:
        app = _create_app(answers, key)
    owner = answers.owner.lower()
    if owner in app.installations:
        return app
    pem = Path(app.private_key_file).expanduser().read_text()
    installation = find_installation(app.app_id, pem, owner)
    if not installation:
        create = (
            ""
            if repository_ready
            else f"create the private repository {answers.full} (first link), then "
        )
        install = f"https://github.com/apps/{app.slug}/installations/new"
        raise NeedsPerson(
            f"Ask the person to {create}install the deck's GitHub App on {answers.owner}, "
            f"choosing 'Only select repositories' and picking {answers.full}.",
            answers.command(),
            (install,) if repository_ready else (new_repository_link(answers), install),
        )
    if path.exists():
        raise SetupError(
            f"{path} is yours and init does not rewrite it: add `{owner} = {installation}` to "
            f'[app] installations and `{owner} = "app:{owner}"` to [owners], then run again'
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(authority_text(owner, app.app_id, app.slug, installation))
    _say(f"✓ wrote {path}: {owner} speaks as {app.slug}[bot].")
    return github_authority.AppConfig(app.app_id, app.slug, KEY_FILE, {owner: installation})


def _create_app(answers: Answers, key: Path) -> github_authority.AppConfig:
    """GitHub's App-manifest flow. A person presses Create; GitHub sends the browser back to
    this machine with a one-time code, which is traded here for the App and its key.

    The run waits for that return; if the browser is elsewhere, or the wait ends first, the
    address the browser ended on carries the code, and ``--app-code`` takes it on a rerun.
    The state that proves the code is this setup's is kept in a file for that rerun.
    """
    pending = Path(APP_PENDING).expanduser()
    if answers.app_code:
        if not pending.exists():
            raise SetupError("--app-code has no App creation to finish; run without it")
        code = code_from(answers.app_code, json.loads(pending.read_text())["state"])
    else:
        code = _wait_for_code(answers, pending)
    app = convert_manifest(code)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.touch(mode=0o600, exist_ok=True)
    key.chmod(0o600)
    key.write_text(app["pem"])
    Path(APP_RECORD).expanduser().write_text(json.dumps({"id": app["id"], "slug": app["slug"]}))
    pending.unlink(missing_ok=True)
    _say(f"✓ created the App {app['slug']}; its key is in {key}.")
    return github_authority.AppConfig(int(app["id"]), str(app["slug"]), KEY_FILE, {})


def _wait_for_code(answers: Answers, pending: Path) -> str:
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}
    done = threading.Event()
    repo_url = f"https://github.com/{answers.full}"
    is_org = owner_is_org(answers.owner)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
            if self.path.startswith("/callback"):
                received["url"] = self.path
                body = b"<p>Done: the App is created. You can close this page.</p>"
                done.set()
            else:
                port = self.server.server_address[1]
                manifest = app_manifest(
                    answers.login, repo_url, f"http://127.0.0.1:{port}/callback"
                )
                body = creation_page(
                    app_creation_url(answers.owner, is_org, state), manifest
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"state": state}))
    _say(app_prompt(port, answers.wait_minutes))
    done.wait(answers.wait_minutes * 60)
    server.shutdown()
    if not received:
        raise NeedsPerson(
            "Ask the person for the address their browser ended on after pressing "
            "'Create GitHub App': a page that did not load, whose address starts "
            "http://127.0.0.1:",
            answers.command(app_code=True),
        )
    return code_from(received["url"], state)


def step_access(answers: Answers, app: github_authority.AppConfig) -> None:
    """The App reaches no further than it needs, and it can read the repository: it exists
    and the installation was given it."""
    installation = app.installations[answers.owner.lower()]
    pem = Path(app.private_key_file).expanduser().read_text()
    problems = least_privilege_problems(app.app_id, pem, installation)
    if problems:
        raise NeedsPerson(
            "Ask the person to " + "; and to ".join(text for text, _ in problems) + ".",
            answers.command(),
            tuple(link for _, link in problems),
        )
    token, _ = github_authority.mint_installation_token(app, installation)
    status, _ = _call("GET", f"/repos/{answers.full}", auth=f"Bearer {token}")
    if status == 200:
        # GitHub answers 409 for the commits of an empty repository.
        commits, _ = _call(
            "GET", f"/repos/{answers.full}/commits?per_page=1", auth=f"Bearer {token}"
        )
        if commits == 409:
            _first_commit(answers, token)
        return
    raise NeedsPerson(
        f"Ask the person to create the private repository {answers.full} (first link) if it "
        "does not exist, and to give the deck's App access to it (second link).",
        answers.command(),
        (new_repository_link(answers), f"https://github.com/apps/{app.slug}/installations/new"),
    )


def _first_commit(answers: Answers, token: str) -> None:
    """An empty repository has nothing for an agent to branch from: write its README as the
    App, rather than send the person back to GitHub for it (agentdeck-foss#1)."""
    readme = f"# {answers.repo}\n\nTasks for my AgentDeck agents.\n"
    status, body = _call(
        "PUT",
        f"/repos/{answers.full}/contents/README.md",
        auth=f"Bearer {token}",
        body={
            "message": "Add a README",
            "content": base64.b64encode(readme.encode()).decode(),
        },
    )
    if status not in (200, 201):
        said = body.get("message") if isinstance(body, dict) else ""
        raise SetupError(
            f"{answers.full} is empty and its README could not be written ({status}: {said})"
        )
    _say(f"✓ {answers.full} was empty: added a README as its first commit.")


def step_claude_login(provider: str, config_dir: str, then: str) -> None:
    """The deck's Claude agents can log in: asked exactly as a worker would be asked.

    On macOS a person can be logged in to ``claude`` while every process with
    ``CLAUDE_CONFIG_DIR`` set reads as logged out (``claude_env``); this asks with the
    environment the deck's own agents get, so setup never ends on a deck that cannot start one.
    """
    if provider != "claude_code":
        return
    from . import claude_env
    from .providers.claude_code import auth

    root = Path(config_dir).expanduser()
    status = auth.read_status(root)
    if status.logged_in:
        _say("✓ Claude Code is logged in for the deck's agents.")
        return
    if status.logged_in is None:
        raise SetupError(f"could not ask Claude Code whether it is logged in: {status.detail}")
    login = (
        "claude auth login"
        if claude_env.uses_default_login(root)
        else f"CLAUDE_CONFIG_DIR={config_dir} claude auth login"
    )
    raise NeedsPerson(
        "Ask the person to log Claude Code in for the deck's agents: in a terminal on this "
        f"machine, run `{login}` and follow it.",
        then,
    )


def step_checkout(answers: Answers, home: Path) -> Path:
    checkout = home / answers.repo
    if (checkout / ".git").exists():
        _say(f"✓ {checkout} is already a checkout.")
        return checkout
    if checkout.exists():
        raise SetupError(
            f"{checkout} exists and is not a checkout; move it away and run agentdeck-init again"
        )
    from .github_identity import github_git_environment

    url = f"https://github.com/{answers.owner}/{answers.repo}.git"
    env = {**os.environ, **github_git_environment()}
    cloned = subprocess.run(
        ["git", "clone", "-q", url, str(checkout)], env=env, text=True, capture_output=True
    )
    if cloned.returncode != 0:
        raise SetupError(f"could not clone {url}: {cloned.stderr.strip()}")
    _say(f"✓ cloned {url} to {checkout}.")
    return checkout


def step_project(answers: Answers, checkout: Path, account: str) -> str:
    instance_id = answers.repo.lower()
    root = Path(poller_host.DEFAULT_ROOT).expanduser()
    if sys.platform == "darwin":
        poller_host.instance_dir(root, instance_id).mkdir(parents=True, exist_ok=True)
    else:
        poller_host.install(root, poller_host.DEFAULT_UNIT_DIR, instances=[instance_id])
    instance = poller_host.instance_dir(root, instance_id)
    overlay = instance / "overlay.toml"
    if not overlay.exists():
        overlay.write_text(overlay_text(instance, answers.repo, str(checkout), account))
    manifest = PROJECTS.expanduser() / instance_id / "poller.toml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if not manifest.exists():
        manifest.write_text(manifest_text(answers.owner, answers.repo))
    engine = [sys.executable, str(REPO / "scripts" / "poller_engine.py")]
    applied = subprocess.run(
        [
            *engine,
            "activate",
            "--instance",
            instance_id,
            "--manifest",
            str(manifest),
            "--overlay",
            str(overlay),
            "--binding",
            str(instance / "binding.json"),
            "--apply",
            answers.login,
        ],
        text=True,
        capture_output=True,
    )
    if applied.returncode != 0:
        raise SetupError(
            f"turning the project on failed:\n{applied.stderr.strip() or applied.stdout.strip()}"
        )
    _say(f"✓ the poller project {instance_id} is on ({manifest}).")
    return instance_id


def step_services(instance_id: str) -> None:
    if sys.platform == "darwin":
        from . import launchd

        home = Path.home()
        root = Path(poller_host.DEFAULT_ROOT).expanduser()
        failures = launchd.install(
            [
                launchd.runtime_job(REPO, home),
                launchd.web_job(REPO, home),
                launchd.poller_job(REPO, home, root, instance_id),
            ],
            home,
        )
        if failures:
            raise SetupError("; ".join(failures))
        _say("✓ the deck and the project's poller are running as launchd agents.")
        return
    units = Path(poller_host.DEFAULT_UNIT_DIR).expanduser()
    units.mkdir(parents=True, exist_ok=True)
    for name in DECK_UNITS:
        target = units / name
        if not target.exists():
            target.symlink_to(REPO / "systemd" / name)
    systemctl = ["systemctl", "--user"]
    for command in (
        [*systemctl, "daemon-reload"],
        [*systemctl, "enable", "--now", "agentdeck.socket", "agentdeck-codex", "agentdeck"],
        [*systemctl, "enable", "--now", poller_host.timer_unit(instance_id)],
        # Keeps the deck and the timer running after logout, as the README's manual setup does.
        ["loginctl", "enable-linger", getpass.getuser()],
    ):
        ran = subprocess.run(command, capture_output=True, text=True)
        if ran.returncode != 0:
            raise SetupError(f"`{' '.join(command)}` failed: {ran.stderr.strip()}")
    _say("✓ the deck and the project's timer are running, and keep running after logout.")


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck-init",
        description=(
            "Set this machine up to run Works on one GitHub repository. It never prompts: it does"
            " what it can, and where only a person can act it prints PERSON: (what to ask them)"
            " and THEN: (what to run next), and exits 3. SETUP.md is the guide for agents."
        ),
    )
    parser.add_argument("--login", required=True, help="the person's GitHub login")
    parser.add_argument(
        "--repo",
        default="",
        help=f"owner/name of the first project (default <login>/{DEFAULT_REPO_NAME})",
    )
    parser.add_argument(
        "--app-code", default="", help="the address the browser ended on after creating the App"
    )
    parser.add_argument(
        "--wait-minutes",
        type=float,
        default=15.0,
        help="how long to wait for the person to create the App (default 15)",
    )
    args = parser.parse_args(argv)
    home = Path.home()
    try:
        if not _NAME.fullmatch(args.login):
            raise SetupError(f"{args.login!r} is not a GitHub login")
        chosen = args.repo or f"{args.login}/{DEFAULT_REPO_NAME}"
        match = _REPO.fullmatch(chosen)
        if not match:
            raise SetupError(f"{chosen!r} is not owner/name")
        answers = Answers(args.login, match[1], match[2], args.app_code, args.wait_minutes)
        account, provider, config_dir = step_config(home)
        step_claude_login(provider, config_dir, answers.command())
        ready = step_repository(answers)
        app = step_app(answers, repository_ready=ready)
        step_access(answers, app)
        checkout = step_checkout(answers, home)
        instance_id = step_project(answers, checkout, account)
        step_services(instance_id)
    except NeedsPerson as stop:
        _say(
            "\n".join(
                [
                    f"PERSON: {stop.ask}",
                    *(f"OPEN: {link}" for link in stop.links),
                    f"THEN: {stop.then}",
                ]
            )
        )
        return NEEDS_PERSON
    except (SetupError, github_authority.Refusal) as exc:
        _say(f"FAILED: {exc}\nFinished steps are kept; fix that and run the same command again.")
        return 1
    _say(
        f"DONE: tell the person to open the deck, press New work, pick {answers.full} and "
        "write for example 'Add a hello-world script that prints Hello, world'. An agent starts "
        "on it at once and opens a pull request; they press Merge on its Work page. If their "
        "browser is on another machine, they first run the TUNNEL command there.\n"
        "OPEN: http://127.0.0.1:8756/\n"
        f"TUNNEL: {tunnel_command(8756)}"
    )
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))
