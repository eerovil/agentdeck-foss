"""The App's installation tokens: signed, minted, cached and refreshed (#2289)."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agentdeck.poller_engine import github_authority


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key, fmt=serialization.PrivateFormat.TraditionalOpenSSL) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption())


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


@pytest.mark.parametrize(
    "fmt", [serialization.PrivateFormat.TraditionalOpenSSL, serialization.PrivateFormat.PKCS8]
)
def test_the_signature_verifies_with_the_public_key(key, fmt):
    message = b"header.payload"
    signature = github_authority.rs256_sign(message, _pem(key, fmt))

    key.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())


def test_the_app_jwt_is_what_github_asks_for(key):
    token = github_authority.app_jwt(4242, _pem(key), now=1_000_000)
    header, payload, signature = token.split(".")

    assert json.loads(_b64(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64(payload))
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "4242"}
    key.public_key().verify(
        _b64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def test_a_file_that_is_not_an_rsa_key_is_refused():
    with pytest.raises(ValueError):
        github_authority.rs256_sign(b"x", b"not a key")


def test_a_key_that_is_not_rsa_is_refused():
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValueError, match="not an RSA key"):
        github_authority.rs256_sign(b"x", pem)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _GitHub:
    """Answers `POST /app/installations/{id}/access_tokens` and records each request."""

    def __init__(self, key, expires="2030-01-01T01:00:00Z"):
        self.key = key
        self.expires = expires
        self.requests: list = []
        self.status = 201
        self.permissions: dict[str, str] = {}

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.status != 201:
            raise urllib.error.HTTPError(request.full_url, self.status, "no", {}, None)
        bearer = request.get_header("Authorization").removeprefix("Bearer ")
        header, payload, signature = bearer.split(".")
        self.key.public_key().verify(
            _b64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        number = len(self.requests)
        body = {"token": f"ghs_minted_{number}", "expires_at": self.expires}
        if self.permissions:
            body["permissions"] = self.permissions
        return _Response(json.dumps(body).encode())


@pytest.fixture
def app(tmp_path, key):
    path = tmp_path / "app.pem"
    path.write_bytes(_pem(key))
    path.chmod(0o600)
    return github_authority.AppConfig(
        app_id=42, slug="agentdeck-eerovil", private_key_file=str(path),
        installations={"acmecorp": 111},
    )


EXPIRY = datetime(2030, 1, 1, 1, 0, tzinfo=UTC).timestamp()


def test_an_installation_token_is_minted_against_the_right_installation(app, key):
    github = _GitHub(key)

    token, expires = github_authority.mint_installation_token(
        app, 111, now=EXPIRY - 3600, opener=github
    )

    assert token == "ghs_minted_1"
    assert expires == EXPIRY
    request = github.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.github.com/app/installations/111/access_tokens"


def test_a_token_is_reused_until_ten_minutes_before_expiry_then_minted_again(app, key, tmp_path):
    github = _GitHub(key)

    first = github_authority.installation_token(app, 111, now=EXPIRY - 3600, opener=github)
    still = github_authority.installation_token(app, 111, now=EXPIRY - 601, opener=github)
    renewed = github_authority.installation_token(app, 111, now=EXPIRY - 600, opener=github)

    assert (first, still, renewed) == ("ghs_minted_1", "ghs_minted_1", "ghs_minted_2")
    assert len(github.requests) == 2


def test_a_second_process_reuses_the_cached_token_file(app, key, tmp_path):
    github = _GitHub(key)
    github_authority.installation_token(app, 111, now=EXPIRY - 3600, opener=github)
    github_authority.clear_caches()  # as a fresh shim or helper process would start

    again = github_authority.installation_token(app, 111, now=EXPIRY - 3000, opener=github)

    assert again == "ghs_minted_1"
    assert len(github.requests) == 1
    cached = next((tmp_path / "github-app-tokens").glob("42-111.json"))
    assert cached.stat().st_mode & 0o777 == 0o600


def test_a_refused_mint_names_the_installation_and_never_tries_anything_else(app, key):
    github = _GitHub(key)
    github.status = 404

    with pytest.raises(github_authority.Refusal) as refused:
        github_authority.installation_token(app, 111, now=EXPIRY - 3600, opener=github)
    assert "installation 111" in str(refused.value)
    assert "HTTP 404" in str(refused.value)
    assert len(github.requests) == 1


def test_a_refused_mint_carries_githubs_own_words(app, key):
    """A suspended installation says so only in the body (#2682); a bare 403 hides it."""

    def suspended(request, timeout):
        body = io.BytesIO(b'{"message": "This installation has been suspended\\nmore"}')
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, body)

    with pytest.raises(github_authority.Refusal) as refused:
        github_authority.mint_installation_token(app, 111, now=EXPIRY - 3600, opener=suspended)
    assert str(refused.value).endswith(
        "GitHub refused a token (HTTP 403: This installation has been suspended)"
    )


def test_a_refused_mint_with_no_readable_body_still_names_the_status(app, key):
    def bare(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(github_authority.Refusal) as refused:
        github_authority.mint_installation_token(app, 111, now=EXPIRY - 3600, opener=bare)
    assert str(refused.value).endswith("GitHub refused a token (HTTP 403)")


def test_an_unreadable_private_key_is_a_refusal(tmp_path):
    app = github_authority.AppConfig(42, "s", str(tmp_path / "missing.pem"), {"acmecorp": 1})

    with pytest.raises(github_authority.Refusal, match="private key cannot be read"):
        github_authority.mint_installation_token(app, 1, opener=lambda *_: None)


def test_the_token_for_an_app_owner_comes_from_its_installation(tmp_path, monkeypatch, app, key):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nacmecorp = "app:acmecorp"\n[app]\napp_id = 42\nslug = "agentdeck-eerovil"\n'
        f'private_key_file = "{app.private_key_file}"\ninstallations = {{ acmecorp = 111 }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    github = _GitHub(key)
    monkeypatch.setattr(github_authority, "_default_open", github)

    authority = github_authority.authority_for("ACMECORP")
    environment = github_authority.environment(
        authority, github_authority.token(authority), {"GH_TOKEN": "human"}
    )

    assert environment["GH_TOKEN"] == "ghs_minted_1"
    assert environment["AGENTDECK_GITHUB_AUTHORITY"] == "app:acmecorp"


# --- naming what a failed call lacked -------------------------------------------------------

_TRACE = (
    "* Request at 2026-09-27\n"
    "> GET /repos/acmecorp/bazaar/issues\n"
    "< HTTP/2.0 403 Forbidden\n"
    "< X-Accepted-Github-Permissions: issues=read\n"
    "\n"
    "HTTP 403: Resource not accessible by integration\n"
)


def test_a_403_with_no_recorded_grants_names_githubs_answer_but_blames_nothing():
    """Unknown grants are not proof (#2714 review): no "lacks a permission" without them."""
    hint = github_authority.failure_hint(
        _TRACE, {"AGENTDECK_GITHUB_AUTHORITY": "app:acmecorp"}, "acmecorp/bazaar"
    )

    assert hint == (
        "github: GitHub refused GET /repos/acmecorp/bazaar/issues (HTTP 403) on"
        " acmecorp/bazaar for app:acmecorp; GitHub accepts: issues=read. This host has no"
        " record of the token's grants, so it cannot tell whether the App installation lacks"
        " that permission"
    )


def test_an_agent_404_names_the_missing_collaborator_grant():
    hint = github_authority.failure_hint(
        "HTTP 404: Not Found (https://api.github.com/repos/eerovil/x)\n",
        {"AGENTDECK_GITHUB_AUTHORITY": "agent"},
        "eerovil/x",
    )

    # No login was configured, so the hint names the account by its role (#3623).
    assert hint.startswith("github: the agent account has no access to eerovil/x: invite it")
    assert "no other identity is tried" in hint


def test_no_hint_without_a_chosen_authority_or_a_recognisable_failure():
    assert github_authority.failure_hint(_TRACE, {}, "acmecorp/bazaar") == ""
    assert github_authority.failure_hint(
        "HTTP 500\n", {"AGENTDECK_GITHUB_AUTHORITY": "agent"}, "eerovil/x"
    ) == ""


def _trace(status: str, *, headers: str = "", body: str = "", path="/repos/o/r/issues/1") -> str:
    return (
        "* Request at 2026-10-07\n"
        f"* Request to https://api.github.com{path}\n"
        f"> POST {path} HTTP/1.1\n"
        "> Content-Type: application/json\n"
        "\n"
        f"< HTTP/2.0 {status}\n"
        "< X-Accepted-Github-Permissions: issues=write; pull_requests=write\n"
        f"{headers}"
        "\n"
        f"{body}"
        "* Request took 200ms\n"
    )


_APP = {"AGENTDECK_GITHUB_AUTHORITY": "app:o"}


@pytest.mark.parametrize(
    "trace",
    [
        _trace("502 Bad Gateway"),  # empty body, the observed comment failure
        _trace("404 Not Found", body='{"message": "Not Found"}\n'),
        _trace("200 OK", body='{"id": 1}\n'),
        _trace(
            "200 OK",
            body='{"errors":[{"message":"Something went wrong while executing your query"}]}\n',
            path="/graphql",
        ),
        _trace(
            "403 Forbidden",
            headers="< X-Accepted-Github-Permissions: allows_permissionless_access=true\n",
            body='{"message": "Resource not accessible by integration"}\n',
        ),
        _trace(
            "403 Forbidden",
            headers="< Retry-After: 60\n",
            body='{"message": "You have exceeded a secondary rate limit"}\n',
        ),
    ],
    ids=["5xx", "404", "200", "graphql-error", "permissionless", "secondary-rate-limit"],
)
def test_the_permission_header_alone_never_blames_a_missing_permission(trace):
    """GitHub sends the header on nearly every answer (#2714): only a real 403 counts."""
    assert github_authority.failure_hint(trace, _APP, "o/r") == ""


def test_an_app_403_on_get_user_says_it_is_expected_and_nothing_else_failed():
    # Reviewers read this 403 as a failed write and redid finished work (#3711).
    trace = _trace("200 OK", body='{"id": 1}\n') + _trace(
        "403 Forbidden",
        body='{"message": "Resource not accessible by integration"}\n',
        path="/user",
    )
    hint = github_authority.failure_hint(trace, _APP, "o/r")
    assert "has no user" in hint and "app:o" in hint
    assert "already went through still stands" in hint
    assert "lacks a permission" not in hint


def test_a_403_the_cached_token_already_covers_is_not_called_a_missing_permission(
    tmp_path, monkeypatch, app, key
):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\no = "app:o"\n[app]\napp_id = 42\nslug = "agentdeck-eerovil"\n'
        f'private_key_file = "{app.private_key_file}"\ninstallations = {{ o = 111 }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    github = _GitHub(key)
    github.permissions = {"issues": "write", "metadata": "read"}
    monkeypatch.setattr(github_authority, "_default_open", github)
    token = github_authority.token(github_authority.authority_for("o"))
    trace = _trace("403 Forbidden", body='{"message": "Forbidden"}\n')

    covered = github_authority.failure_hint(trace, {**_APP, "GH_TOKEN": token}, "o/r")
    other_token = github_authority.failure_hint(trace, {**_APP, "GH_TOKEN": "x"}, "o/r")

    assert "this is not a missing App permission" in covered
    assert "holds issues=write, metadata=read" in covered
    assert "lacks a permission" not in other_token
    assert "cannot tell whether the App installation lacks" in other_token
    facts = github_authority.cached_token_facts({**_APP, "GH_TOKEN": token})
    assert facts["installation_id"] == 111
    assert facts["token_is_cached"] is True
    assert facts["permissions"] == {"issues": "write", "metadata": "read"}


def test_a_failure_summary_names_the_real_status_and_whether_it_was_a_hiccup():
    gateway = github_authority.failed_responses(_trace("502 Bad Gateway"))
    graphql = github_authority.failed_responses(
        _trace(
            "200 OK",
            body='{"errors":[{"message":"Something went wrong while executing your query"}]}\n',
            path="/graphql",
        )
    )
    refused = github_authority.failed_responses(
        _trace("422 Unprocessable Entity", body='{"message": "Validation Failed"}\n')
    )

    assert [r.transient for r in gateway + graphql + refused] == [True, True, False]
    assert github_authority.failure_summary(_trace("502 Bad Gateway")) == (
        "github answered: POST /repos/o/r/issues/1 -> HTTP 502, empty body"
    )
    assert "Validation Failed" in github_authority.failure_summary(
        _trace("422 Unprocessable Entity", body='{"message": "Validation Failed"}\n')
    )
    assert github_authority.failure_summary(_trace("200 OK", body="{}\n")) == ""
    assert github_authority.failure_summary("") == ""


def test_a_github_outage_is_transient_and_held_for_a_minute(app, key):
    github = _GitHub(key)
    github.status = 502

    with pytest.raises(github_authority.TransientRefusal):
        github_authority.installation_token(app, 111, now=EXPIRY - 3600, opener=github)
    # Within the hold the answer is remembered: no second slow request.
    with pytest.raises(github_authority.TransientRefusal):
        github_authority.installation_token(app, 111, now=EXPIRY - 3570, opener=github)
    assert len(github.requests) == 1

    github.status = 201
    token = github_authority.installation_token(app, 111, now=EXPIRY - 3500, opener=github)
    assert token == "ghs_minted_2" and len(github.requests) == 2


def test_a_refused_installation_is_not_transient(app, key):
    github = _GitHub(key)
    github.status = 404

    with pytest.raises(github_authority.Refusal) as refused:
        github_authority.installation_token(app, 111, now=EXPIRY - 3600, opener=github)
    assert not isinstance(refused.value, github_authority.TransientRefusal)


# --- the staged map and the #2274 file do not overlap (#2289) ------------------------------


_STAGED = (
    '[owners]\neerovil = "agent"\nacmecorp = "app:acmecorp"\n'
    '[app]\napp_id = 7\nslug = "agentdeck-eerovil"\nprivate_key_file = "/k"\n'
    "installations = { acmecorp = 11 }\n"
)


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "agentdeck").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(github_authority.ACCOUNTS_FILE_ENV, raising=False)
    github_authority.clear_caches()
    return home / ".config" / "agentdeck"


def test_the_map_lives_where_the_2274_code_never_looks():
    """The #2274 code reads `github-accounts.toml` or `$AGENTDECK_GITHUB_ACCOUNTS`; staging the
    App map anywhere it reads would let it misread `app:` before this code deploys."""
    assert github_authority.DEFAULT_ACCOUNTS_FILE != github_authority.LEGACY_ACCOUNTS_FILE
    assert github_authority.ACCOUNTS_FILE_ENV != github_authority.LEGACY_ACCOUNTS_FILE_ENV


def test_the_authority_stamped_into_a_child_is_not_read_as_the_map(tmp_path, monkeypatch):
    """A child runs with `AGENTDECK_GITHUB_AUTHORITY=app:acmecorp`; that names its authority,
    and must never be taken for the path of the owner map."""
    assert github_authority.ACCOUNTS_FILE_ENV != github_authority.AUTHORITY_ENV
    config = _home(tmp_path, monkeypatch)
    (config / "github-authority.toml").write_text(_STAGED, encoding="utf-8")
    monkeypatch.setenv(github_authority.AUTHORITY_ENV, "app:acmecorp")

    assert github_authority.accounts_path() == config / "github-authority.toml"
    assert github_authority.authority_for("acmecorp").name == "app:acmecorp"


def test_a_staged_map_is_read_on_first_start_and_the_old_file_is_ignored(tmp_path, monkeypatch):
    config = _home(tmp_path, monkeypatch)
    (config / "github-authority.toml").write_text(_STAGED, encoding="utf-8")
    # What a host running #2274 may still hold, and the variable it may still export.
    (config / "github-accounts.toml").write_text('[owners]\nacmecorp = "operator"\n')
    legacy = str(config / "github-accounts.toml")
    monkeypatch.setenv(github_authority.LEGACY_ACCOUNTS_FILE_ENV, legacy)

    assert github_authority.authority_for("acmecorp").name == "app:acmecorp"
    assert github_authority.authority_for("eerovil").name == "agent"


def test_the_old_file_alone_is_never_interpreted(tmp_path, monkeypatch):
    config = _home(tmp_path, monkeypatch)
    (config / "github-accounts.toml").write_text('[owners]\nacmecorp = "operator"\n')

    # With no current file there is no owner at all, not even a built-in one (#3623).
    for owner in ("eerovil", "acmecorp"):
        with pytest.raises(github_authority.Refusal, match="no GitHub authority configured"):
            github_authority.authority_for(owner)


def _app_owner(tmp_path, monkeypatch, app, key, permissions):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\no = "app:o"\n[app]\napp_id = 42\nslug = "agentdeck-eerovil"\n'
        f'private_key_file = "{app.private_key_file}"\ninstallations = {{ o = 111 }}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    github = _GitHub(key)
    github.permissions = permissions
    monkeypatch.setattr(github_authority, "_default_open", github)
    return github_authority.token(github_authority.authority_for("o"))


def test_a_missing_app_permission_is_named_only_when_the_recorded_grants_prove_it(
    tmp_path, monkeypatch, app, key
):
    token = _app_owner(tmp_path, monkeypatch, app, key, {"issues": "read", "metadata": "read"})
    trace = _trace("403 Forbidden", body='{"message": "Resource not accessible"}\n')

    hint = github_authority.failure_hint(trace, {**_APP, "GH_TOKEN": token}, "o/r")

    assert hint == (
        "github: the AgentDeck App installation (app:o) lacks a permission on o/r"
        " (POST /repos/o/r/issues/1 answered HTTP 403); GitHub accepts:"
        " issues=write; pull_requests=write; the token holds issues=read, metadata=read"
    )


def test_a_token_cached_before_grants_were_recorded_is_unknown_not_lacking(
    tmp_path, monkeypatch, app, key
):
    token = _app_owner(tmp_path, monkeypatch, app, key, {"issues": "write"})
    # Rewrite the cache file the way a pre-#2714 mint left it: no ``permissions`` at all.
    cached = next((tmp_path / "github-app-tokens").glob("42-111.json"))
    record = json.loads(cached.read_text(encoding="utf-8"))
    cached.write_text(
        json.dumps({"token": record["token"], "expires_at": record["expires_at"]}),
        encoding="utf-8",
    )
    trace = _trace("403 Forbidden", body='{"message": "Forbidden"}\n')

    hint = github_authority.failure_hint(trace, {**_APP, "GH_TOKEN": token}, "o/r")

    assert "lacks a permission" not in hint
    assert "no record of the token's grants" in hint
    assert "permissions" not in github_authority.cached_token_facts({**_APP, "GH_TOKEN": token})
