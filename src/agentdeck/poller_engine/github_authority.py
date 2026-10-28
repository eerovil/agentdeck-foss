"""Which non-human GitHub identity speaks for a repository owner (#2289).

One rule, asked before any GitHub call: **the repository owner picks exactly one
non-human authority, or the call is refused.** There is no fallback of any kind —
not to the host's `gh` login, not to an inherited ``GH_TOKEN``, not to a second
identity after the first one failed. A refusal names the owner and what to
configure, and nothing else is tried.

Two kinds of authority exist:

* ``agent`` — a bot account's PAT (``eerovil-agent`` here). Used for ``eerovil/*`` until
  GitHub suspended that account (#2682); still accepted. With no file, no owner has an
  authority at all (#3623).
* ``app:<owner>`` — the dedicated AgentDeck GitHub App's installation on that
  organization or personal account. Used for organizations, which must never spend
  an organization seat on a bot, and since #2682 for ``eerovil/*``. An App cannot
  reach a Project owned by a personal account, so a personal account's pollers are
  boardless.

The configuration is the owner map from #2274, extended::

    [owners]
    eerovil = "app:eerovil"
    example-org = "app:example-org"

    [app]
    app_id = 123
    slug = "agentdeck-eerovil"
    private_key_file = "~/.config/agentdeck/github-app.pem"
    installations = { eerovil = 3, example-org = 1 }

``operator`` is not a value: a file naming it does not load, and every owner is
then refused with that reason.

This lives in the engine package because the Poller, the deck, the worker ``gh``
shim and the git credential helper must all apply the *same* rule, and the deck may
import the engine but the engine must never import the app (#2597). The App's JWT is
signed with ``cryptography``, which the engine can use now that it runs under the
checkout's venv rather than any Python 3.

An installation token is cached until ten minutes before it expires, in memory
and in one 0600 file per installation so that short-lived processes (the shim,
the git helper) do not mint a fresh token for every call. On expiry it is simply
minted again: there is no other state.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import credentials

# Stamped into every child environment this rule builds; rate-limit and cache identity key on it.
from .rate_limits import AUTHORITY_ENV

AGENT = "agent"
APP_PREFIX = "app:"
# A path and variable the #2274 code never reads, so the App map can be written before this
# code deploys: the old code keeps its own file and cannot misread `app:`, and this code
# reads the staged map on its first start. There is no file to switch at merge time.
ACCOUNTS_FILE_ENV = "AGENTDECK_GITHUB_AUTHORITY_FILE"
DEFAULT_ACCOUNTS_FILE = "~/.config/agentdeck/github-authority.toml"
# The #2274 owner map. Never read here: it may still say "operator".
LEGACY_ACCOUNTS_FILE = "~/.config/agentdeck/github-accounts.toml"
LEGACY_ACCOUNTS_FILE_ENV = "AGENTDECK_GITHUB_ACCOUNTS"
# With no accounts file at all, no owner: every one is refused with the file to create
# (#3623). Each host names its own owners; there is no built-in account.
DEFAULT_OWNERS: dict[str, str] = {}
# No built-in agent account login (#3623): a host whose owner uses one names it as [agent]
# login; without it the account is shown as "the agent account" and its echo is not known.
DEFAULT_AGENT_LOGIN = ""
AGENT_TOKEN_FILE_ENV = "AGENTDECK_AGENT_GITHUB_ENV"
DEFAULT_AGENT_TOKEN_FILE = "~/.config/agentdeck/agent-github.env"
TOKEN_CACHE_ENV = "AGENTDECK_GITHUB_TOKEN_CACHE"
DEFAULT_TOKEN_CACHE = "~/.cache/agentdeck/github-app-tokens"

# Every variable `gh` (or a tool beside it) reads a credential from. All of them
# are replaced or removed, so a leftover inherited one cannot speak instead.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")

API = "https://api.github.com"
# Refresh this long before GitHub's stated expiry (an installation token lives an hour).
REFRESH_MARGIN_S = 600
_ISSUE = 2289
_OWNER = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# `owner/name`, tolerating the decorations a slug arrives with: a full URL, a
# `.git` suffix, an `#123` issue reference, a trailing path segment.
_SLUG = re.compile(
    r"""
    (?:^|//(?:[^/@\s]+@)?github\.com/|^git@github\.com:)
    (?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)
    /
    (?P<name>[A-Za-z0-9._-]+?)
    (?:\.git)?
    (?:[#/].*)?
    $
    """,
    re.VERBOSE,
)


class ConfigError(ValueError):
    """The accounts file cannot be used. The message is a sentence to act on."""


class Refusal(Exception):  # noqa: N818 — it is a refusal, not an error in this code
    """No non-human authority may make this call. ``str()`` is the reason."""


class TransientRefusal(Refusal):
    """The authority is configured, but GitHub could not mint its token just now.

    A network error or a 5xx from the token endpoint: still a refusal (nothing falls
    back), but one to wait out rather than a configuration to fix.
    """


def owner_of(repo: object) -> str:
    """The owner in a repository reference, lowercased, or ``""``.

    Accepts ``owner/name``, a clone URL, an ``owner/name#12`` reference. A bare
    owner name is *not* a repository reference and answers ``""``; callers that
    hold an owner pass it as ``owner=`` instead.
    """
    text = str(repo or "").strip()
    if not text:
        return ""
    match = _SLUG.search(text)
    return match.group("owner").lower() if match else ""


def repository_of(repo: object) -> str:
    """The ``owner/name`` in a repository reference, lowercased, or ``""``.

    The same forms ``owner_of`` accepts, so a clone URL and ``-R owner/name``
    compare equal once both have been through here.
    """
    text = str(repo or "").strip()
    if not text:
        return ""
    match = _SLUG.search(text)
    return f"{match.group('owner')}/{match.group('name')}".lower() if match else ""


def owner_in_positionals(positionals: list[str]) -> str:
    """The owner a `gh` command's positional arguments name, or ``""``.

    Deliberately narrow, because a guess here picks a credential: only a
    ``github.com`` URL, an API path (``repos/<o>/<n>``, ``orgs/<o>``, ``users/<o>``),
    or the repository argument of ``gh repo <verb> <owner>/<name>``. A branch
    such as ``agent/issue-2289`` or a path such as ``search/issues`` is shaped
    like ``owner/name`` and names no owner at all.
    """
    parts = [str(part) for part in positionals]
    for part in parts:
        if "github.com" in part:
            owner = owner_of(part)
            if owner:
                return owner
        segments = [segment for segment in part.split("?", 1)[0].split("/") if segment]
        if len(segments) >= 3 and segments[0] == "repos" and _OWNER.match(segments[1].lower()):
            return segments[1].lower()
        if len(segments) >= 2 and segments[0] in ("orgs", "users") and _OWNER.match(
            segments[1].lower()
        ):
            return segments[1].lower()
    if len(parts) >= 3 and parts[0] == "repo" and not parts[2].startswith("-"):
        return owner_of(parts[2])
    return ""


@dataclass(frozen=True)
class AppConfig:
    app_id: int
    slug: str
    private_key_file: str
    # Case-folded organization -> installation id.
    installations: dict[str, int] = field(default_factory=dict)

    @property
    def login(self) -> str:
        return f"{self.slug}[bot]"


@dataclass(frozen=True)
class AccountsConfig:
    # Case-folded owner -> "agent" or "app:<case-folded org>".
    owners: dict[str, str]
    app: AppConfig | None = None
    agent_login: str = DEFAULT_AGENT_LOGIN
    agent_token_file: str = ""


@dataclass(frozen=True)
class Authority:
    """One explicit non-human identity, chosen for one owner."""

    owner: str
    name: str  # "agent" or "app:<org>"
    login: str  # the GitHub login its writes are signed with
    installation_id: int = 0

    @property
    def kind(self) -> str:
        return AGENT if self.name == AGENT else "app"


def _require_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{where} must be a positive integer")
    return value


def parse_accounts(text: str) -> AccountsConfig:
    """Parse and check an accounts file. Raises ``ConfigError``."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from exc
    table = raw.get("owners", {})
    if not isinstance(table, dict):
        raise ConfigError("[owners] must be a table of owner = authority")
    app_raw = raw.get("app")
    app: AppConfig | None = None
    if app_raw is not None:
        if not isinstance(app_raw, dict):
            raise ConfigError("[app] must be a table")
        installations_raw = app_raw.get("installations", {})
        if not isinstance(installations_raw, dict):
            raise ConfigError("[app] installations must be a table of org = installation id")
        installations: dict[str, int] = {}
        for org, ident in installations_raw.items():
            key = str(org).strip().lower()
            if not _OWNER.match(key):
                raise ConfigError(f"[app] installations: {org!r} is not a GitHub owner name")
            if key in installations:
                raise ConfigError(f"[app] installations: {org!r} is listed twice")
            installations[key] = _require_int(ident, f"[app] installations.{org}")
        slug = app_raw.get("slug")
        key_file = app_raw.get("private_key_file")
        if not isinstance(slug, str) or not slug.strip():
            raise ConfigError("[app] slug must name the App (its bot login is <slug>[bot])")
        if not isinstance(key_file, str) or not key_file.strip():
            raise ConfigError("[app] private_key_file must name the App's private key")
        app = AppConfig(
            app_id=_require_int(app_raw.get("app_id"), "[app] app_id"),
            slug=slug.strip(),
            private_key_file=key_file.strip(),
            installations=installations,
        )
    agent_raw = raw.get("agent", {})
    if not isinstance(agent_raw, dict):
        raise ConfigError("[agent] must be a table")
    agent_login = agent_raw.get("login", DEFAULT_AGENT_LOGIN)
    if not isinstance(agent_login, str):
        raise ConfigError("[agent] login must be the agent account's GitHub login")
    agent_token_file = agent_raw.get("token_file", "")
    if not isinstance(agent_token_file, str):
        raise ConfigError("[agent] token_file must be a path")

    owners: dict[str, str] = {}
    for owner, value in table.items():
        key = str(owner).strip().lower()
        if not _OWNER.match(key):
            raise ConfigError(f"{owner!r} is not a GitHub owner name")
        if key in owners:
            raise ConfigError(f"{owner!r} is listed twice, in different letter case")
        if not isinstance(value, str):
            raise ConfigError(f"{owner} must name an authority as a string")
        spelled = value.strip()
        if spelled.lower() == "operator":
            raise ConfigError(
                f"{owner} = \"operator\": the operator's own login is no longer an authority "
                f"(#{_ISSUE}); use \"agent\" or \"app:<org>\""
            )
        if spelled == AGENT:
            owners[key] = AGENT
            continue
        if not spelled.startswith(APP_PREFIX):
            raise ConfigError(
                f"{owner} = {value!r}: the authority must be \"agent\" or \"app:<org>\""
            )
        org = spelled[len(APP_PREFIX) :].strip().lower()
        if not _OWNER.match(org):
            raise ConfigError(f"{owner} = {value!r}: {org!r} is not an organization name")
        if app is None:
            raise ConfigError(f"{owner} = {value!r} needs an [app] table")
        if org not in app.installations:
            raise ConfigError(f"{owner} = {value!r}: [app] installations has no {org}")
        owners[key] = APP_PREFIX + org
    return AccountsConfig(
        owners=owners,
        app=app,
        agent_login=agent_login.strip(),
        agent_token_file=agent_token_file.strip(),
    )


def _configured(name: str, default: str) -> Path:
    """A path from *this host's* environment, never from a child's dictionary."""
    configured = str(os.environ.get(name) or "").strip()
    return Path(configured or default).expanduser()


def accounts_path() -> Path:
    return _configured(ACCOUNTS_FILE_ENV, DEFAULT_ACCOUNTS_FILE)


_lock = threading.Lock()
_loaded: tuple[object, AccountsConfig | ConfigError] | None = None


_tables: dict[str, tuple[object, dict]] = {}


def _file_table(name: str) -> dict:
    """The accounts file's ``[name]`` table, re-read when the file changes.

    A missing, unreadable or malformed file or table is an empty one: what these tables grant
    is never guessed at (#3623).
    """
    path = accounts_path()
    try:
        stat = path.stat()
        key: object = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    with _lock:
        cached = _tables.get(name)
        if cached is not None and cached[0] == key:
            return cached[1]
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get(name)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        table = None
    table = table if isinstance(table, dict) else {}
    with _lock:
        _tables[name] = (key, table)
    return table


def reviewer_logins() -> frozenset[str]:
    """The outside reviewers' logins, lowercased, from the accounts file's ``[reviewers]``."""
    listed = _file_table("reviewers").get("logins")
    return frozenset(
        str(login).strip().lower() for login in (listed if isinstance(listed, list) else ())
        if isinstance(login, str) and login.strip()
    )


def deck_person() -> str:
    """The GitHub login of the person this deck works for, lowercased, or ``""`` (#3731).

    ``[deck] person`` in the accounts file. Two decks on one repository each take only the
    issues assigned to their own person (``work_claims``); unset, the assignee is ignored.
    """
    person = _file_table("deck").get("person")
    person = person.strip().lower() if isinstance(person, str) else ""
    return person if _OWNER.match(person) else ""


def load_accounts() -> AccountsConfig:
    """This host's accounts file, re-read when it changes. Raises ``ConfigError``.

    A missing file names no owner, so each is refused with the file to create (#3623);
    an unreadable or invalid one is an error, so every owner is refused with its reason
    rather than guessed at.
    """
    path = accounts_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return AccountsConfig(owners=dict(DEFAULT_OWNERS))
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read: {exc}") from exc
    fingerprint = (str(path), stat.st_mtime_ns, stat.st_size)
    global _loaded
    with _lock:
        if _loaded is not None and _loaded[0] == fingerprint:
            cached = _loaded[1]
            if isinstance(cached, ConfigError):
                raise cached
            return cached
    try:
        result: AccountsConfig | ConfigError = parse_accounts(
            path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError as exc:
        result = ConfigError(f"{path} cannot be read: {exc}")
    except ConfigError as exc:
        result = ConfigError(f"{path}: {exc}")
    with _lock:
        _loaded = (fingerprint, result)
    if isinstance(result, ConfigError):
        raise result
    return result


def resolve(owner: str, config: AccountsConfig) -> Authority:
    """The authority for ``owner`` under ``config``. Raises ``Refusal``."""
    key = str(owner or "").strip().lower()
    if not key:
        raise Refusal("no target owner")
    spelled = config.owners.get(key)
    if spelled is None:
        raise Refusal(
            f"no GitHub authority configured for owner {owner}; "
            f"add it to {DEFAULT_ACCOUNTS_FILE} [owners]"
        )
    if spelled == AGENT:
        return Authority(owner=key, name=AGENT, login=config.agent_login)
    org = spelled[len(APP_PREFIX) :]
    assert config.app is not None  # parse_accounts guarantees it
    return Authority(
        owner=key,
        name=spelled,
        login=config.app.login,
        installation_id=config.app.installations[org],
    )


def authority_for(owner: str) -> Authority:
    """This host's authority for ``owner``. Raises ``Refusal``."""
    try:
        config = load_accounts()
    except ConfigError as exc:
        raise Refusal(f"no GitHub authority for owner {owner}: {exc}") from exc
    return resolve(owner, config)


# --- RS256 ------------------------------------------------------------------------------------


def rs256_sign(message: bytes, pem: bytes | str) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 over an RSA private key in PEM (PKCS#1 or PKCS#8).

    ``cryptography`` is imported here rather than at the top: the gh shim and the git
    credential helper import this module on every call and only an App token ever signs.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    data = pem.encode("ascii") if isinstance(pem, str) else pem
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError("not a PEM RSA private key") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("not an RSA key")
    return key.sign(message, padding.PKCS1v15(), hashes.SHA256())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: int, pem: bytes | str, *, now: float | None = None) -> str:
    """The App's own short-lived JWT, as GitHub specifies it.

    ``iat`` is backdated a minute against clock drift; ``exp`` is nine minutes on,
    inside GitHub's ten-minute ceiling.
    """
    issued = int(time.time() if now is None else now) - 60
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(
            {"iat": issued, "exp": issued + 600, "iss": str(app_id)}, separators=(",", ":")
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_b64url(rs256_sign(signing_input, pem))}"


# --- installation tokens ----------------------------------------------------------------------

Opener = Callable[[urllib.request.Request, float], object]


def _default_open(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — fixed https host


def _parse_expiry(value: object) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("no expires_at")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _error_message(exc: urllib.error.HTTPError) -> str:
    """GitHub's own ``message`` from an error answer, one short line, or ``""``.

    Kept because it is the only place a suspended installation says so ("This
    installation has been suspended", #2682); a bare status code hides it.
    """
    try:
        body = json.loads(exc.read(4096).decode("utf-8", "replace"))
    except (OSError, ValueError, AttributeError):
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message.strip():
        return ""
    first = message.strip().splitlines()[0]
    return "".join(ch for ch in first if ch.isprintable()).strip()[:160]


def mint_installation_token(
    app: AppConfig,
    installation_id: int,
    *,
    now: float | None = None,
    opener: Opener | None = None,
    timeout: float = 20.0,
) -> tuple[str, float]:
    """Exchange the App JWT for an installation token: ``(token, expires_at_epoch)``.

    Raises ``Refusal`` naming the installation and GitHub's answer; never retries
    as anything else.
    """
    token, expires, _granted = _mint(app, installation_id, now=now, opener=opener, timeout=timeout)
    return token, expires


def _mint(
    app: AppConfig,
    installation_id: int,
    *,
    now: float | None = None,
    opener: Opener | None = None,
    timeout: float = 20.0,
) -> tuple[str, float, dict[str, str]]:
    """``mint_installation_token`` plus the permissions GitHub granted the token (#2714)."""
    where = f"the AgentDeck App (app {app.app_id}) installation {installation_id}"
    try:
        pem = Path(app.private_key_file).expanduser().read_bytes()
    except OSError as exc:
        raise Refusal(f"{where}: its private key cannot be read: {exc.strerror or exc}") from exc
    try:
        token_jwt = app_jwt(app.app_id, pem, now=now)
    except ValueError as exc:
        raise Refusal(f"{where}: its private key is unusable: {exc}") from exc
    request = urllib.request.Request(
        f"{API}/app/installations/{installation_id}/access_tokens",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentdeck",
        },
    )
    try:
        response = (opener or _default_open)(request, timeout)
        with response:  # type: ignore[attr-defined]
            body = json.loads(response.read().decode("utf-8", "replace"))  # type: ignore[attr-defined]
    except urllib.error.HTTPError as exc:
        kind = TransientRefusal if exc.code >= 500 else Refusal
        said = _error_message(exc)
        raise kind(
            f"{where}: GitHub refused a token (HTTP {exc.code}{f': {said}' if said else ''})"
        ) from exc
    except OSError as exc:
        raise TransientRefusal(f"{where}: no token could be minted: {exc}") from exc
    except ValueError as exc:
        raise Refusal(f"{where}: no token could be minted: {exc}") from exc
    token = body.get("token") if isinstance(body, dict) else None
    try:
        expires = _parse_expiry(body.get("expires_at") if isinstance(body, dict) else None)
    except ValueError as exc:
        raise Refusal(f"{where}: GitHub's answer carried no usable expiry") from exc
    if not isinstance(token, str) or not token:
        raise Refusal(f"{where}: GitHub's answer carried no token")
    return token, expires, _permissions(body.get("permissions"))


def _permissions(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(name): str(level) for name, level in value.items() if isinstance(level, str)}


_memory: dict[tuple[int, int], tuple[str, float]] = {}
# A failed mint is remembered this long, so an outage costs each process one slow
# request a minute rather than one per call.
FAILURE_HOLD_S = 60.0
_failures: dict[tuple[int, int], tuple[float, Refusal]] = {}


def _cache_file(app_id: int, installation_id: int) -> Path:
    return _configured(TOKEN_CACHE_ENV, DEFAULT_TOKEN_CACHE) / f"{app_id}-{installation_id}.json"


def _read_cached(path: Path) -> tuple[str, float] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = raw.get("token") if isinstance(raw, dict) else None
    expires = raw.get("expires_at") if isinstance(raw, dict) else None
    if not isinstance(token, str) or not token or not isinstance(expires, int | float):
        return None
    return token, float(expires)


def _write_cached(
    path: Path, token: str, expires: float, permissions: dict[str, str] | None = None
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".token-")
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(
                    {"token": token, "expires_at": expires, "permissions": permissions or {}},
                    stream,
                )
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError:
        # A cache that cannot be written costs a mint per process, never the call.
        pass


def installation_token(
    app: AppConfig,
    installation_id: int,
    *,
    now: float | None = None,
    opener: Opener | None = None,
) -> str:
    """A valid installation token, minted again only within ten minutes of expiry."""
    clock = time.time() if now is None else now
    key = (app.app_id, installation_id)
    with _lock:
        held = _memory.get(key)
    if held and held[1] - REFRESH_MARGIN_S > clock:
        return held[0]
    path = _cache_file(app.app_id, installation_id)
    cached = _read_cached(path)
    if cached and cached[1] - REFRESH_MARGIN_S > clock:
        with _lock:
            _memory[key] = cached
        return cached[0]
    with _lock:
        failed = _failures.get(key)
    if failed and failed[0] > clock:
        raise failed[1]
    try:
        token, expires, granted = _mint(app, installation_id, now=clock, opener=opener)
    except Refusal as refusal:
        with _lock:
            _failures[key] = (clock + FAILURE_HOLD_S, refusal)
        raise
    with _lock:
        _failures.pop(key, None)
        _memory[key] = (token, expires)
    _write_cached(path, token, expires, granted)
    return token


def cached_token_facts(environment: dict[str, str]) -> dict[str, object]:
    """What this host's token cache says about the App token ``environment`` speaks with.

    For diagnosing a failed call (#2714), so it never mints, never reads the network and never
    returns the token itself: the installation, whether the token in use is the cached one,
    when that expires, and the permissions GitHub granted it. ``{}`` for anything that is not
    an App authority or has no readable cache file. A token minted before #2714 has no
    recorded permissions, which reads as ``permissions`` absent — unknown, not none.
    """
    authority = environment.get(AUTHORITY_ENV, "")
    if not authority.startswith(APP_PREFIX):
        return {}
    try:
        config = load_accounts()
    except ConfigError:
        return {}
    if config.app is None:
        return {}
    installation = config.app.installations.get(authority[len(APP_PREFIX) :].casefold(), 0)
    if not installation:
        return {}
    facts: dict[str, object] = {"installation_id": installation}
    try:
        raw = json.loads(_cache_file(config.app.app_id, installation).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return facts
    if not isinstance(raw, dict):
        return facts
    in_use = environment.get("GH_TOKEN", "")
    facts["token_is_cached"] = bool(in_use) and raw.get("token") == in_use
    if isinstance(raw.get("expires_at"), int | float):
        facts["token_expires_at"] = float(raw["expires_at"])
    if isinstance(raw.get("permissions"), dict) and raw["permissions"]:
        facts["permissions"] = _permissions(raw["permissions"])
    return facts


def token(authority: Authority, *, config: AccountsConfig | None = None) -> str:
    """The credential for ``authority``. Raises ``Refusal``; never answers ``""``."""
    if config is None:
        try:
            config = load_accounts()
        except ConfigError as exc:
            raise Refusal(str(exc)) from exc
    if authority.name == AGENT:
        path = (
            Path(config.agent_token_file).expanduser()
            if config.agent_token_file
            else _configured(AGENT_TOKEN_FILE_ENV, DEFAULT_AGENT_TOKEN_FILE)
        )
        value = credentials.read_token(path)
        if not value:
            raise Refusal(
                f"{config.agent_login} has no credential on this host ({path} is missing or empty)"
            )
        return value
    if config.app is None:
        raise Refusal(f"{authority.name} needs an [app] table in {DEFAULT_ACCOUNTS_FILE}")
    return installation_token(config.app, authority.installation_id)


def environment(
    authority: Authority, credential: str, base: dict[str, str] | None = None
) -> dict[str, str]:
    """A child environment that speaks as ``authority`` and as nothing else."""
    child = dict(os.environ if base is None else base)
    for name in TOKEN_VARS:
        child.pop(name, None)
    child["GH_TOKEN"] = credential
    child["GITHUB_TOKEN"] = credential
    child[AUTHORITY_ENV] = authority.name
    return child


_NOT_FOUND = re.compile(r"HTTP 404|Could not resolve to a Repository", re.I)
_REQUEST_LINE = re.compile(r"^> (?P<method>[A-Z]+) (?P<path>\S+)")
_STATUS_LINE = re.compile(r"^< HTTP/\S+ (?P<status>\d{3})")
_LEVELS = {"read": 1, "write": 2, "admin": 3}
_BODY_EXCERPT = 300


@dataclass(frozen=True)
class ApiResponse:
    """One request in a ``GH_DEBUG=api`` trace and what GitHub answered it (#2714)."""

    method: str = ""
    path: str = ""
    status: int | None = None  # None: the trace shows no answer at all
    headers: dict[str, str] = field(default_factory=dict)  # names lower-cased
    body: str = ""

    @property
    def failed(self) -> bool:
        """No answer, an error status, a GraphQL error, or an empty body where one was due."""
        if self.status is None or self.status >= 400:
            return True
        if self.status == 204:
            return False
        if not self.body:
            return True
        return self.path.endswith("/graphql") and '"errors"' in self.body

    @property
    def transient(self) -> bool:
        """A failure GitHub may not repeat: no answer, a 5xx, an empty body, a server-side
        GraphQL error. A 4xx is GitHub's considered refusal, and a retry gets it again."""
        if not self.failed:
            return False
        if self.status is None or self.status >= 500 or not self.body:
            return True
        return "something went wrong" in self.body.casefold()

    def describe(self) -> str:
        said = f"HTTP {self.status}" if self.status is not None else "no answer"
        if self.status is not None and not self.body and self.status != 204:
            said += ", empty body"
        request = self.headers.get("x-github-request-id", "")
        line = f"{self.method} {self.path} -> {said}"
        if request:
            line += f" (request {request})"
        excerpt = " ".join(self.body.split())[:_BODY_EXCERPT]
        if excerpt and self.status is not None and (self.status >= 400 or self.failed):
            line += f": {excerpt}"
        return line


def api_responses(raw_stderr: str) -> list[ApiResponse]:
    """Every request in ``gh``'s ``GH_DEBUG=api`` trace, in order, with its answer.

    The trace is the only place a failed call's real HTTP status, request id and the
    permission header live; `gh` itself often prints only "unexpected end of JSON input".
    """
    responses: list[ApiResponse] = []
    block: dict | None = None

    def close() -> None:
        if block is not None:
            responses.append(
                ApiResponse(
                    method=block["method"],
                    path=block["path"],
                    status=block["status"],
                    headers=block["headers"],
                    body="".join(block["body"]).strip(),
                )
            )

    for line in (raw_stderr or "").splitlines(keepends=True):
        if line.startswith("* Request at "):
            close()
            block = {"method": "", "path": "", "status": None, "headers": {}, "body": []}
            continue
        if block is None:
            continue
        if line.startswith("* Request took "):
            close()
            block = None
            continue
        request = _REQUEST_LINE.match(line)
        if request and not block["method"]:
            block["method"], block["path"] = request.group("method"), request.group("path")
            continue
        status = _STATUS_LINE.match(line)
        if status:
            block["status"] = int(status.group("status"))
            block["body"] = []
            continue
        if line.startswith("< ") and ":" in line:
            name, value = line[2:].split(":", 1)
            block["headers"][name.strip().lower()] = value.strip()
            continue
        if block["status"] is not None and len("".join(block["body"])) < 4 * _BODY_EXCERPT:
            block["body"].append(line)
    close()
    return responses


def failed_responses(raw_stderr: str) -> list[ApiResponse]:
    return [response for response in api_responses(raw_stderr) if response.failed]


def failure_summary(raw_stderr: str, limit: int = 3) -> str:
    """One line saying what GitHub actually answered a failed call, or ``""``.

    ``""`` when the trace shows nothing failed: ``gh pr checks`` exits 8 on pending checks
    with every request answered, and that is not a GitHub failure worth a line.
    """
    failed = failed_responses(raw_stderr)
    if not failed:
        return ""
    return "github answered: " + "; ".join(response.describe() for response in failed[-limit:])


def _permission_sets(accepted: str) -> list[dict[str, str]]:
    """GitHub's ``X-Accepted-GitHub-Permissions``: ``;`` separates alternatives, ``,`` joins
    the permissions one alternative needs together."""
    sets = []
    for alternative in accepted.split(";"):
        needed = {}
        for part in alternative.split(","):
            name, _, level = part.strip().partition("=")
            if name.strip():
                needed[name.strip().lower()] = level.strip().lower()
        if needed:
            sets.append(needed)
    return sets


def _satisfies(granted: dict[str, str], accepted: str) -> bool:
    held = {name.lower(): _LEVELS.get(level.lower(), 0) for name, level in granted.items()}
    return any(
        all(held.get(name, 0) >= _LEVELS.get(level, 1) for name, level in needed.items())
        for needed in _permission_sets(accepted)
    )


def _asks_for_a_permission(response: ApiResponse) -> bool:
    """A 403 that is GitHub refusing a permission, and not one of the other 403s.

    GitHub sends ``X-Accepted-GitHub-Permissions`` on nearly every answer, a 200 and a 404
    included, so the header alone proves nothing (#2714). A secondary rate limit is a 403
    too, and ``allows_permissionless_access`` names an endpoint no permission unlocks.
    """
    accepted = response.headers.get("x-accepted-github-permissions", "")
    if response.status != 403 or not accepted:
        return False
    if "allows_permissionless_access" in accepted.lower():
        return False
    if response.headers.get("retry-after") or response.headers.get("x-ratelimit-remaining") == "0":
        return False
    return "rate limit" not in response.body.lower()


def failure_hint(
    raw_stderr: str, environment: dict[str, str], target: str = "", login: str = ""
) -> str:
    """One line naming what a failed call's authority lacks, or ``""``.

    ``raw_stderr`` is `gh`'s output *before* the ``GH_DEBUG=api`` trace is stripped,
    because the permission GitHub wanted is only in a response header there
    (``X-Accepted-GitHub-Permissions``). A missing App permission is named only for a 403
    that asks for one the token does not hold (#2714); a 5xx, a GraphQL error or an empty
    body is not a permission problem whatever headers came with it. An agent-account 404
    on a repository names the grant that is missing instead. Never a hint to retry as
    anybody else.
    """
    authority = environment.get(AUTHORITY_ENV, "")
    if not authority:
        return ""
    where = f" on {target}" if target else ""
    if authority.startswith(APP_PREFIX):
        responses = api_responses(raw_stderr)
        if any(r.status == 403 and r.path.split("?", 1)[0] == "/user" for r in responses):
            # The 403 every App token gets from `GET /user`: no missing permission, and no
            # sign that anything else in the command failed. Reviewers read it as a failed
            # write and redid finished work (#3711).
            return (
                f"github: an App installation token has no user, so GitHub always refuses"
                f" GET /user (HTTP 403) for {authority}. It says nothing about any other call"
                " in this command; a write that already went through still stands"
            )
        refused = [r for r in responses if _asks_for_a_permission(r)]
        if not refused:
            return ""
        response = refused[-1]
        accepted = response.headers["x-accepted-github-permissions"]
        facts = cached_token_facts(environment)
        granted = facts.get("permissions") if facts.get("token_is_cached") else None
        refused_line = f"GitHub refused {response.method} {response.path} (HTTP 403){where}"
        if not isinstance(granted, dict):
            # No recorded grants — a token minted before #2714, or not the cached one — is
            # unknown, not proof: say what GitHub answered and assert nothing about the App.
            return (
                f"github: {refused_line} for {authority}; GitHub accepts: {accepted}. This"
                " host has no record of the token's grants, so it cannot tell whether the App"
                " installation lacks that permission"
            )
        held = ", ".join(f"{name}={level}" for name, level in sorted(granted.items()))
        if _satisfies(granted, accepted):
            return (
                f"github: {refused_line} although the AgentDeck App installation token"
                f" ({authority}) holds {held}; this is not a missing App permission"
            )
        return (
            f"github: the AgentDeck App installation ({authority}) lacks a permission{where}"
            f" ({response.method} {response.path} answered HTTP 403); GitHub accepts:"
            f" {accepted}; the token holds {held or 'nothing'}"
        )
    if authority == AGENT and target and _NOT_FOUND.search(raw_stderr or ""):
        who = login or "the agent account"
        return (
            f"github: {who} has no access to {target}: invite it (Settings → Collaborators);"
            " no other identity is tried"
        )
    return ""


def clear_caches() -> None:
    """Forget every cached config and token in this process. For tests."""
    global _loaded
    with _lock:
        _loaded = None
        _tables.clear()
        _memory.clear()
        _failures.clear()
