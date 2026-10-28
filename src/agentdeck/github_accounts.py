"""``agentdeck github-account``: which non-human identity speaks for each owner (#2274, #2289).

Three read-only commands:

* ``show`` — the owner map, each owner's authority, whether its credential is
  usable, and each poller instance's authority (which it now derives from the same
  map rather than from its Overlay).
* ``prove`` — the App's read-only merge-gate proof from #2289: for each App
  installation, ``GET /installation/repositories``, a ProjectV2 read of the owner's
  board, and an HTTPS ``git ls-remote`` through AgentDeck's credential helper.
* ``doctor`` — every place on this host from which a process could still reach a
  *human* GitHub credential. It prints locations and identities, never a value,
  and since the host cutover (#2310) it enforces: any finding exits 1.
  ``--report`` prints the same and exits 0.

The web worker and the runtime also run the doctor once as they start
(``check_at_start``), in the background, and log every finding as an error.

There is nothing to switch any more. The engine, the deck, the worker shim and the
git credential helper all read ``~/.config/agentdeck/github-authority.toml``, so an
edit to that file is the whole change.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import threading
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import github_identity
from .poller_engine import github_authority

log = logging.getLogger(__name__)

DEFAULT_POLLER_ROOT = "~/.local/share/agentdeck/poller"
POLLER_ROOT_ENV = "AGENTDECK_POLLER_ROOT"
# The operator's old poller credential. It must not exist after the cutover.
OPERATOR_TOKEN_FILE = "~/.config/agentdeck/poller-token"
# GitHub's anonymous tier. A token that authenticates but is capped here is what a flagged
# account looks like (#2117): it answers 200 and then starves every poller wake.
FLAGGED_LIMIT = 60
_API = "https://api.github.com"
_TOKEN_NAMES = github_authority.TOKEN_VARS
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_ENTERPRISE_TOKEN)"
    r"\s*=\s*(?P<value>.*)$"
)


# -- GitHub requests ------------------------------------------------------------------------

Fetch = Callable[[str, str, bytes | None], tuple[int, dict[str, str], bytes]]


def _fetch(token: str, url: str, body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    """One request straight to GitHub, never through ``gh`` (the shim would choose for us)."""
    request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "agentdeck-github-account")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed host
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    except OSError as exc:
        return 0, {}, str(exc).encode()


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


@dataclass(frozen=True)
class Probe:
    """One real answer from GitHub about a credential."""

    ok: bool
    login: str = ""
    limit: int = 0
    detail: str = ""


def probe_account(token: str, fetch: Fetch = _fetch) -> Probe:
    """Who a user token is and which rate-limit tier GitHub keeps it on."""
    if not token:
        return Probe(False, detail="no credential")
    status, headers, body = fetch(token, f"{_API}/user", None)
    try:
        limit = int(_header(headers, "x-ratelimit-limit") or 0)
    except ValueError:
        limit = 0
    if status != 200:
        return Probe(False, limit=limit, detail=f"/user answered HTTP {status}")
    try:
        login = str(json.loads(body).get("login") or "")
    except (ValueError, AttributeError):
        login = ""
    if limit <= 0:
        return Probe(False, login=login, detail="/user gave no usable rate limit")
    if limit <= FLAGGED_LIMIT:
        return Probe(
            False, login=login, limit=limit,
            detail=f"rate limit is {limit}/h — GitHub has flagged the account",
        )
    return Probe(True, login=login, limit=limit)


def login_of(token: str, fetch: Fetch = _fetch) -> str:
    """The login a token authenticates as, or ``""`` when GitHub will not say."""
    status, _headers, body = fetch(token, f"{_API}/user", None)
    if status != 200:
        return ""
    try:
        return str(json.loads(body).get("login") or "")
    except (ValueError, AttributeError):
        return ""


# -- poller instances -----------------------------------------------------------------------


@dataclass
class Instance:
    """A poller instance as far as its GitHub identity is concerned."""

    name: str
    owner: str
    overlay: Path
    repos: tuple[str, ...] = ()
    project_number: int | None = None
    # Overlay keys that still name a GitHub credential file. The engine ignores them now.
    legacy_token_paths: tuple[str, ...] = ()
    problem: str = ""


def poller_root() -> Path:
    return Path(os.environ.get(POLLER_ROOT_ENV) or DEFAULT_POLLER_ROOT).expanduser()


def _legacy_paths(raw: dict, prefix: str = "") -> list[str]:
    found: list[str] = []
    for key, value in raw.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            found += _legacy_paths(value, f"{path}.")
        elif key == "github_token_file" or (
            key == "token_file"
            and isinstance(value, str)
            and Path(value).expanduser() == Path(OPERATOR_TOKEN_FILE).expanduser()
        ):
            found.append(path)
    return found


def instances(root: Path | None = None) -> list[Instance]:
    """Every installed poller instance, read from its published Manifest and its Overlay."""
    base = (root or poller_root()) / "instances"
    found: list[Instance] = []
    if not base.is_dir():
        return found
    for directory in sorted(base.iterdir()):
        manifest_path = directory / "poller.toml"
        overlay_path = directory / "overlay.toml"
        if not directory.is_dir() or not (manifest_path.exists() or overlay_path.exists()):
            continue
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            found.append(Instance(directory.name, "", overlay_path, problem=f"poller.toml: {exc}"))
            continue
        board = manifest.get("board") if isinstance(manifest.get("board"), dict) else {}
        owner = str(board.get("org") or manifest.get("org") or "").strip().lower()
        try:
            overlay = tomllib.loads(overlay_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            found.append(
                Instance(directory.name, owner, overlay_path, problem=f"overlay.toml: {exc}")
            )
            continue
        number = board.get("project_number")
        found.append(
            Instance(
                name=directory.name,
                owner=owner,
                overlay=overlay_path,
                repos=tuple(
                    str(repo["id"])
                    for repo in manifest.get("repos", [])
                    if isinstance(repo, dict) and repo.get("id")
                ),
                project_number=number if isinstance(number, int) else None,
                legacy_token_paths=tuple(_legacy_paths(overlay)),
                problem="" if owner else "poller.toml names no org",
            )
        )
    return found


# -- show -----------------------------------------------------------------------------------


def show(*, root: Path | None = None, fetch: Fetch = _fetch) -> list[str]:
    lines: list[str] = []
    path = github_identity.accounts_path()
    state = "" if path.exists() else " (no file — built-in default)"
    lines.append(f"owners ({path}{state}):")
    try:
        config = github_authority.load_accounts()
    except github_authority.ConfigError as exc:
        lines.append(f"  UNUSABLE — every owner is refused: {exc}")
        config = None
    if config is not None:
        for owner in sorted(config.owners):
            authority = github_authority.resolve(owner, config)
            lines.append(f"  {owner} -> {authority.name} ({authority.login})")
        lines.append("  every other owner -> refused")
        person = github_authority.deck_person()
        lines.append(
            f"deck person: {person} (an issue assigned to anyone else is left to their deck)"
            if person
            else "deck person: not set (the deck ignores who an issue is assigned to)"
        )
        agent_token = ""
        try:
            agent_token = github_authority.token(
                github_authority.Authority("", github_authority.AGENT, config.agent_login),
                config=config,
            )
        except github_authority.Refusal as refusal:
            lines.append(f"agent account: NOT USABLE — {refusal}")
        if agent_token:
            probe = probe_account(agent_token, fetch)
            verdict = "ok" if probe.ok else f"NOT USABLE — {probe.detail}"
            lines.append(
                f"agent account: {probe.login or '?'}, limit {probe.limit or '?'}/h, {verdict}"
            )
        if config.app is None:
            lines.append("app: not configured")
        else:
            key = Path(config.app.private_key_file).expanduser()
            key_state = "missing"
            if key.is_file():
                mode = key.stat().st_mode & 0o777
                key_state = "ok" if mode & 0o077 == 0 else f"readable by others (mode {mode:o})"
            lines.append(
                f"app: {config.app.login} (app {config.app.app_id}), private key {key}: {key_state}"
            )
            for org, ident in sorted(config.app.installations.items()):
                lines.append(f"  installation {org}: {ident}")

    found = instances(root)
    if found:
        lines.append("pollers:")
    for instance in found:
        if instance.problem:
            lines.append(
                f"  {instance.name} [{instance.owner or '?'}] UNREADABLE: {instance.problem}"
            )
            continue
        try:
            authority = github_identity.authority_for(owner=instance.owner)
            speaks = f"speaks as {authority.name} ({authority.login})"
        except github_authority.Refusal as refusal:
            speaks = f"REFUSED: {refusal}"
        lines.append(f"  {instance.name} [{instance.owner}] {speaks}")
        for key in instance.legacy_token_paths:
            lines.append(f"    {key}: ignored by the engine since #2289 — remove it")
    return lines


# -- prove ----------------------------------------------------------------------------------

Runner = Callable[[list[str], dict[str, str]], tuple[int, str, str]]


def _run(argv: list[str], environment: dict[str, str]) -> tuple[int, str, str]:
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv
            argv, capture_output=True, text=True, timeout=60, check=False,
            env=environment, cwd="/var/tmp" if Path("/var/tmp").is_dir() else None,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def host_checks() -> dict:
    """This host's extra proofs, from the accounts file's optional ``[checks]`` table.

    ``boards`` maps an organization to a Project number whose items ``prove`` reads,
    ``repositories`` an organization to a repository it ``git ls-remote``s, and
    ``env_files`` lists dev-container env files ``doctor`` searches for human tokens::

        [checks]
        boards = { example-org = 3 }
        repositories = { example-org = "shop" }
        env_files = ["~/shop/.env"]

    Each is empty when absent or malformed; a check that has nothing to look at is
    skipped, never guessed at.
    """
    try:
        raw = tomllib.loads(github_authority.accounts_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    table = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    boards = table.get("boards") if isinstance(table.get("boards"), dict) else {}
    repositories = table.get("repositories")
    repositories = repositories if isinstance(repositories, dict) else {}
    env_files = table.get("env_files") if isinstance(table.get("env_files"), list) else []
    return {
        "boards": {
            str(k).lower(): v for k, v in boards.items() if isinstance(v, int) and v > 0
        },
        "repositories": {
            str(k).lower(): v for k, v in repositories.items() if isinstance(v, str) and v
        },
        "env_files": tuple(v for v in env_files if isinstance(v, str) and v),
    }


def prove(
    *,
    fetch: Fetch = _fetch,
    run: Runner = _run,
    root: Path | None = None,
    streams: Path | None = None,
) -> tuple[bool, list[str]]:
    """The #2289 read-only App proof, one installation at a time.

    Also checks each repository this deck's poller projects declare against the App's
    webhook (#3755): ``root`` is the poller root and ``streams`` the webhook log folder.
    """
    from .poller_ops import work_repositories
    from .poller_quiet import DEFAULT_STREAMS_ROOT

    declared = work_repositories(root or poller_root())
    streams = Path(streams or DEFAULT_STREAMS_ROOT).expanduser()
    lines: list[str] = []
    try:
        config = github_authority.load_accounts()
    except github_authority.ConfigError as exc:
        return False, [f"refused: {exc}"]
    if config.app is None or not config.app.installations:
        return False, ["refused: no [app] installations are configured"]
    checks = host_checks()
    ok = True
    for org, _ident in sorted(config.app.installations.items()):
        try:
            authority = github_authority.authority_for(org)
            token = github_authority.token(authority, config=config)
        except github_authority.Refusal as refusal:
            ok = False
            lines.append(f"{org}: FAILED — {refusal}")
            continue
        repos: list[str] = []
        for page in range(1, 11):
            # Paged, so the webhook check below never calls a repository on page two missing.
            status, _h, body = fetch(
                token, f"{_API}/installation/repositories?per_page=100&page={page}", None
            )
            try:
                rows = json.loads(body).get("repositories", [])
                repos.extend(str(row.get("full_name")) for row in rows)
            except (ValueError, AttributeError, TypeError):
                rows = []
            if status != 200 or len(rows) < 100:
                break
        repos.sort()
        lines.append(f"{org}: installation repositories (HTTP {status}): {', '.join(repos) or '-'}")
        ok = ok and status == 200 and bool(repos)
        if status == 200:
            covered, webhook = _webhook_coverage(
                org, repos, declared, streams, config.app.app_id
            )
            ok = ok and covered
            lines.extend(webhook)
        number = checks["boards"].get(org)
        if number is not None:
            query = (
                "query($login:String!,$number:Int!){organization(login:$login){projectV2("
                "number:$number){title items(first:5){nodes{content{__typename ... on Issue"
                "{number repository{nameWithOwner}}}}}}}}"
            )
            payload = json.dumps({"query": query, "variables": {"login": org, "number": number}})
            status, _h, body = fetch(token, f"{_API}/graphql", payload.encode())
            try:
                nodes = json.loads(body)["data"]["organization"]["projectV2"]["items"]["nodes"]
            except (ValueError, KeyError, TypeError):
                nodes = []
            issues = [
                f"{node['content']['repository']['nameWithOwner']}#{node['content']['number']}"
                for node in nodes
                if isinstance(node, dict)
                and isinstance(node.get("content"), dict)
                and node["content"].get("__typename") == "Issue"
            ]
            redacted = any(
                isinstance(node, dict)
                and isinstance(node.get("content"), dict)
                and node["content"].get("__typename") == "RedactedItem"
                for node in nodes
            )
            good = status == 200 and bool(issues) and not redacted
            ok = ok and good
            lines.append(
                f"{org}: project #{number} items (HTTP {status}): "
                f"{', '.join(issues) or '-'}{' REDACTED items present' if redacted else ''}"
            )
        repo = checks["repositories"].get(org)
        if repo:
            environment = {
                key: value for key, value in os.environ.items() if key not in _TOKEN_NAMES
            }
            environment["GIT_TERMINAL_PROMPT"] = "0"
            github_identity.configure_github_git(environment)
            code, _out, err = run(
                ["git", "ls-remote", "--heads", f"https://github.com/{org}/{repo}.git"],
                environment,
            )
            ok = ok and code == 0
            detail = "ok" if code == 0 else f"exit {code}: {err.strip().splitlines()[-1:] or ''}"
            lines.append(f"{org}: git ls-remote {org}/{repo}: {detail}")
    return ok, lines


def _app_delivered(owner_dir: Path, app_id: int) -> bool:
    """Whether any of the owner's webhook logs holds a delivery from this App's own hook."""
    for stream in sorted(owner_dir.glob("*.jsonl")) if owner_dir.is_dir() else []:
        try:
            with stream.open("rb") as handle:
                start = max(0, stream.stat().st_size - 256 * 1024)
                handle.seek(start)
                # A read from the middle starts inside a record: skip that part line.
                tail = handle.read().splitlines()[1 if start else 0 :]
        except OSError:
            continue
        for line in reversed(tail):
            try:
                headers = json.loads(line).get("headers") or {}
            except (ValueError, AttributeError):
                continue
            folded = {str(k).lower(): str(v) for k, v in headers.items()}
            if (
                folded.get("x-github-hook-installation-target-type") == "integration"
                and folded.get("x-github-hook-installation-target-id") == str(app_id)
            ):
                return True
    return False


def _webhook_coverage(
    org: str, installed: list[str], declared: frozenset[str], streams: Path, app_id: int
) -> tuple[bool, list[str]]:
    """Each declared repository of ``org`` whose webhook log is missing, and why (#3755).

    A log file appears with a repository's first delivery, and until then the poller cannot
    trust its silence. A repository the App is not installed on never gets one: that fails.
    One the App covers, while the App's deliveries already reach this deck, is only quiet:
    its empty log is created here, which is what the poller's own note asks for.
    """
    canonical = {name.lower(): name for name in installed}
    owner_dirs = [path for path in streams.glob("*") if path.name.lower() == org.lower()]
    lines: list[str] = []
    ok = True
    delivered: bool | None = None
    for slug in sorted(slug for slug in declared if slug.split("/", 1)[0] == org.lower()):
        name = canonical.get(slug)
        if name is None:
            ok = False
            lines.append(
                f"{slug}: the App is not installed here, so its webhook never reaches this deck "
                "— GitHub → organization settings → GitHub Apps → Configure → Repository access"
            )
            continue
        owner, repo = name.split("/", 1)
        owner_dir = owner_dirs[0] if owner_dirs else streams / owner
        if any(path.name.lower() == f"{repo.lower()}.jsonl" for path in owner_dir.glob("*.jsonl")):
            continue
        if delivered is None:
            delivered = _app_delivered(owner_dir, app_id)
        if not delivered:
            lines.append(
                f"{name}: no webhook log, and no delivery from the App has reached this deck "
                "yet — turn the App's webhook on (docs/github-setup.md, step 4)"
            )
            continue
        owner_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (owner_dir / f"{repo}.jsonl").touch(mode=0o600, exist_ok=True)
        lines.append(f"✓ {name}: covered by the App's webhook; created its empty webhook log")
    return ok, lines


# -- doctor ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One place a human credential may be reachable. Never carries a secret value."""

    kind: str
    location: str
    identity: str = ""
    remediation: str = ""

    def render(self) -> str:
        who = f" [{self.identity}]" if self.identity else ""
        fix = f" — {self.remediation}" if self.remediation else ""
        return f"FOUND {self.kind}: {self.location}{who}{fix}"


def _home() -> Path:
    return Path("~").expanduser()


def _is_non_human(login: str, agent_login: str, app_login: str) -> bool:
    return bool(login) and (login == agent_login or login == app_login or login.endswith("[bot]"))


def _identity(
    token: str, fetch: Fetch, cache: dict[str, str], agent_login: str, app_login: str
) -> tuple[str, bool]:
    """(label, is_human). Unresolvable tokens are treated as possibly human."""
    if token not in cache:
        cache[token] = login_of(token, fetch)
    login = cache[token]
    if not login:
        return "identity unresolved", True
    return login, not _is_non_human(login, agent_login, app_login)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _file_assignments(path: Path) -> list[tuple[str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if match and _unquote(match.group("value")):
            found.append((match.group("name"), _unquote(match.group("value"))))
    return found


def _unit_assignments(path: Path) -> list[tuple[str, str]]:
    """Token variables a unit sets inline with ``Environment=``."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != "Environment":
            continue
        for part in value.split():
            name, eq, token = _unquote(part).partition("=")
            if eq and name in _TOKEN_NAMES and token:
                found.append((name, token))
    return found


def _unit_files(home: Path) -> list[Path]:
    base = home / ".config" / "systemd" / "user"
    if not base.is_dir():
        return []
    files = sorted(base.glob("agentdeck*.service"))
    for directory in sorted(base.glob("agentdeck*.service.d")):
        files += sorted(directory.glob("*.conf"))
    return files


def _environment_files(unit: Path) -> list[Path]:
    try:
        text = unit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "EnvironmentFile":
            for part in value.split():
                found.append(Path(part.lstrip("-")).expanduser())
    return found


def doctor(
    *,
    home: Path | None = None,
    env_files: Sequence[str] | None = None,
    root: Path | None = None,
    fetch: Fetch = _fetch,
    run: Runner = _run,
) -> list[Finding]:
    """Every human-credential exposure this host still has. Never a value."""
    home = home or _home()
    try:
        config = github_authority.load_accounts()
        agent_login = config.agent_login
        app_login = config.app.login if config.app else ""
    except github_authority.ConfigError:
        agent_login, app_login = github_authority.DEFAULT_AGENT_LOGIN, ""
    identities: dict[str, str] = {}
    findings: list[Finding] = []

    def token_finding(kind: str, location: str, token: str, remediation: str) -> None:
        label, human = _identity(token, fetch, identities, agent_login, app_login)
        if human:
            findings.append(Finding(kind, location, label, remediation))

    # 1. Unit EnvironmentFiles and the known dev-container env files.
    candidates: dict[Path, str] = {}
    for unit in _unit_files(home):
        for name, value in _unit_assignments(unit):
            token_finding(
                "token in unit file", f"{unit} {name}", value, "delete the Environment= line"
            )
        for path in _environment_files(unit):
            candidates.setdefault(path, f"EnvironmentFile of {unit.name}")
    if env_files is None:
        env_files = host_checks()["env_files"]
    for spelled in env_files:
        path = Path(spelled.replace("~", str(home), 1) if spelled.startswith("~") else spelled)
        candidates.setdefault(path, "dev-container env file")
    seen_files: set[Path] = set()
    for path, why in candidates.items():
        # One file reached by two spellings (`/home` links to `/var/home`) is one finding.
        try:
            real = path.resolve()
        except OSError:
            real = path
        if real in seen_files:
            continue
        seen_files.add(real)
        for name, value in _file_assignments(path):
            token_finding(
                "token in env file", f"{path} {name} ({why})", value,
                f"delete the {name} line and chmod 600 the file",
            )

    # 2. A logged-in gh.
    real_env = {key: value for key, value in os.environ.items() if key not in _TOKEN_NAMES}
    real_env.pop(github_authority.AUTHORITY_ENV, None)
    real_env["AGENTDECK_GH_SHIM_ACTIVE"] = "1"  # never the shim; ask the real binary
    real_env["HOME"] = str(home)
    code, out, err = run(["gh", "auth", "status", "--hostname", "github.com"], real_env)
    for match in re.finditer(r"account (\S+)", f"{out}\n{err}"):
        findings.append(Finding(
            "gh login", "gh auth status (keyring / hosts.yml)", match.group(1).strip("()"),
            "gh auth logout -h github.com",
        ))

    # 3. The global git credential helper.
    code, out, _err = run(
        ["git", "config", "--global", "--get-regexp", r"^credential\..*helper$"], real_env
    )
    helpers = [line.split(None, 1) for line in out.splitlines() if line.strip()]
    for key, *value in helpers:
        spelled = value[0] if value else ""
        if spelled and "agentdeck" not in spelled:
            findings.append(Finding(
                "git credential helper", f"git config --global {key}", spelled.split()[0][:60],
                "replace it with `agentdeck git-credential`",
            ))
    if not any("agentdeck" in (value[0] if value else "") for _key, *value in helpers):
        findings.append(Finding(
            "git credential helper", "git config --global credential.https://github.com.helper",
            "", "the AgentDeck helper is not installed globally",
        ))

    # 4. The old operator poller token, and Overlays still pointing at a credential file.
    operator = Path(OPERATOR_TOKEN_FILE.replace("~", str(home), 1))
    if operator.exists():
        value = github_authority.credentials.read_token(operator)
        label = _identity(value, fetch, identities, agent_login, app_login)[0] if value else "empty"
        findings.append(Finding("operator token file", str(operator), label, "delete it"))
    legacy = Path(github_authority.LEGACY_ACCOUNTS_FILE.replace("~", str(home), 1))
    if legacy.exists():
        findings.append(Finding(
            "old owner map", str(legacy), "",
            f"ignored since #2289 (the map is {github_authority.DEFAULT_ACCOUNTS_FILE}); delete it",
        ))
    for instance in instances(root):
        for key in instance.legacy_token_paths:
            findings.append(Finding(
                "overlay credential file", f"{instance.overlay} {key}", "",
                f"remove the key; the {instance.owner or '?'} instance uses the owner map",
            ))

    # 5. SSH straight to github.com.
    code, out, err = run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "git@github.com"], real_env
    )
    match = re.search(r"Hi (\S+?)!", f"{out}\n{err}")
    if match:
        findings.append(Finding(
            "ssh to github.com", "ssh -T git@github.com", match.group(1),
            "add `Host github.com` / `IdentityAgent none` above `Host *` in ~/.ssh/config",
        ))

    # 6. Running containers carrying a GitHub token.
    code, out, _err = run(["docker", "ps", "--format", "{{.Names}}"], real_env)
    if code == 0:
        for name in [line.strip() for line in out.splitlines() if line.strip()]:
            code, env_json, _err = run(
                ["docker", "inspect", "--format", "{{json .Config.Env}}", name], real_env
            )
            try:
                entries = json.loads(env_json) if code == 0 else []
            except ValueError:
                entries = []
            for entry in entries or []:
                key, sep, value = str(entry).partition("=")
                if sep and key in _TOKEN_NAMES and value:
                    token_finding(
                        "token in container", f"container {name} {key}", value,
                        "remove it from the compose env file and recreate the container",
                    )
    return findings


def check_at_start(process: str) -> threading.Thread:
    """Run the doctor once, off the start path, and log each finding as an error.

    A process start is not refused: the doctor takes seconds and asks the network,
    and the deck going dark would not remove the credential. This makes a
    credential that came back loud, because an error log is a Sentry event
    wherever error reporting is on.
    """

    def run() -> None:
        try:
            findings = doctor()
        except Exception:  # a broken check must not take the process with it
            log.exception("github doctor could not run at %s start", process)
            return
        for finding in findings:
            log.error("human GitHub credential on this host (%s start): %s",
                      process, finding.render())
        if not findings:
            log.info("github doctor at %s start: no human GitHub credential found", process)

    thread = threading.Thread(target=run, name="github-doctor", daemon=True)
    thread.start()
    return thread


# -- the command ----------------------------------------------------------------------------


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agentdeck github-account",
        description="Which non-human GitHub identity speaks for each owner (#2274, #2289).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="the owner map, each authority's health, and each poller")
    commands.add_parser("prove", help="the App's read-only proof: repositories, board, git")
    doctor_command = commands.add_parser(
        "doctor", help="fail if any human GitHub credential is still reachable on this host"
    )
    doctor_command.add_argument(
        "--report", action="store_true", help="print the findings but exit 0"
    )
    args = parser.parse_args(list(argv))

    if args.command == "show":
        print("\n".join(show()))
        return 0
    if args.command == "prove":
        ok, lines = prove()
        print("\n".join(lines))
        print("proof: PASS" if ok else "proof: FAIL")
        return 0 if ok else 1
    findings = doctor()
    for finding in findings:
        print(finding.render())
    if not findings:
        print("no human GitHub credential found")
        return 0
    mode = "report mode: nothing enforced" if args.report else "enforced"
    print(f"{len(findings)} human-credential exposure(s) found ({mode})")
    return 0 if args.report else 1
