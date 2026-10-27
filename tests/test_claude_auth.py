"""Recovering an expired Claude login from the deck (#298).

The CLI is stubbed by a real script on PATH rather than mocked: the flow's whole
difficulty is that ``claude auth login`` prints a URL and then *blocks on stdin*, and
a mock of `Popen` would prove nothing about reading one without deadlocking on the
other. The stub's output is copied from the real thing (claude 2.1.220).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentdeck.auth_watch import AuthWatch
from agentdeck.models import Account
from agentdeck.providers.claude_code import auth

_REAL_LOGIN_OUTPUT = """\
Opening browser to sign in…
If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a&response_type=code&code_challenge=Zfh&state=IOk
Paste code here if prompted >\
"""

_STUB = '''#!/usr/bin/env python3
"""A stand-in for `claude`, doing only what this flow asks of it."""
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
config = Path(os.environ.get("CLAUDE_CONFIG_DIR", ""))
argv = sys.argv[1:]

def signed_in() -> bool:
    return (config / "signed-in").exists()

if argv[:2] == ["auth", "status"]:
    (state / "status-calls").open("a").write(f"{config}\\n")
    if os.environ.get("STUB_STATUS_BROKEN_JSON"):
        print("something went very wrong", file=sys.stderr)
        raise SystemExit(1)
    if signed_in():
        print(json.dumps({
            "loggedIn": True, "authMethod": "claude.ai", "email": "eero@example.com",
            "orgName": "Northwind", "subscriptionType": "team",
        }))
    else:
        print(json.dumps({"loggedIn": False}))
    raise SystemExit(0)

if argv[:2] == ["auth", "login"]:
    (state / "login-env").write_text(json.dumps({
        "config": str(config), "browser": os.environ.get("BROWSER", ""), "argv": argv,
    }))
    print(LOGIN_OUTPUT)
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    (state / "code-received").write_text(code)
    if code == "good-code":
        (config / "signed-in").touch()
        print("Logged in")
        raise SystemExit(0)
    print("Invalid code", file=sys.stderr)
    raise SystemExit(1)

print(f"stub does not implement {argv}", file=sys.stderr)
raise SystemExit(2)
'''.replace("LOGIN_OUTPUT", repr(_REAL_LOGIN_OUTPUT))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """A `claude` on PATH, plus somewhere for it to record what it was asked."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    state = tmp_path / "stub-state"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_STATE", str(state))
    return state


@pytest.fixture
def account(tmp_path):
    config = tmp_path / "claude-account"
    config.mkdir()
    return Account(key="claude_code:main", provider_id="claude_code", label="main", root=config)


# --- what a broken login looks like from the outside ------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "OAuth token expired",
        "Please run `claude login` to continue",
        "Invalid API key · Please run /login",
    ],
)
def test_a_failed_turn_is_recognised_as_an_authentication_failure(text):
    assert auth.looks_like_auth_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Done — the tests pass and the PR is open.",
        # A session *reporting on* authentication is not a session that failed it.
        "I read the auth docs. " + "The flow is described in detail. " * 80,
    ],
)
def test_ordinary_answers_are_not_mistaken_for_one(text):
    assert not auth.looks_like_auth_failure(text)


# --- is this account signed in --------------------------------------------


def test_the_status_read_asks_about_this_account_and_nothing_else(cli, account):
    status = auth.read_status(account.root)

    assert status.logged_in is False
    assert status.known
    assert status.broken
    assert (cli / "status-calls").read_text().strip() == str(account.root), (
        "an account is its CLAUDE_CONFIG_DIR; asking about the wrong one is a wrong answer"
    )


def test_a_signed_in_account_reports_who_it_is(cli, account):
    (account.root / "signed-in").touch()

    status = auth.read_status(account.root)

    assert status.logged_in is True
    assert (status.email, status.organization) == ("eero@example.com", "Northwind")
    assert status.subscription == "team"


def test_an_unanswerable_check_is_unknown_rather_than_signed_out(cli, account, monkeypatch):
    """"We could not tell" must never render as "you are signed out"."""
    monkeypatch.setenv("STUB_STATUS_BROKEN_JSON", "1")

    status = auth.read_status(account.root)

    assert status.logged_in is None
    assert not status.known and not status.broken
    assert "something went very wrong" in status.detail


def test_no_claude_on_path_is_also_unknown(account, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")

    status = auth.read_status(account.root)

    assert status.logged_in is None
    assert "not on PATH" in status.detail


# --- fixing it -------------------------------------------------------------


def test_the_login_shows_the_url_without_waiting_for_the_code(cli, account):
    """The CLI blocks on stdin after printing the URL: reading to EOF would hang."""
    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)

    flow.start()

    assert flow.state == "awaiting_code"
    assert flow.url.startswith("https://claude.com/cai/oauth/authorize?")
    launched = json.loads((cli / "login-env").read_text())
    assert launched["config"] == str(account.root), "the login must target this account"
    assert launched["browser"] == "true", "opening a browser on the host helps nobody"
    assert launched["argv"] == ["auth", "login", "--claudeai"]
    flow.cancel()


def test_the_code_finishes_the_login_and_the_account_confirms_it(cli, account):
    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)
    flow.start()

    flow.submit_code("  good-code  ")

    assert (cli / "code-received").read_text() == "good-code"
    assert flow.state == "done"
    assert flow.status is not None and flow.status.logged_in is True
    assert flow.status.email == "eero@example.com"


def test_a_rejected_code_fails_the_flow_rather_than_claiming_success(cli, account):
    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)
    flow.start()

    flow.submit_code("wrong-code")

    assert flow.state == "failed"
    assert flow.status is not None and flow.status.logged_in is False


def test_the_flow_never_repeats_the_code_it_was_given(cli, account):
    """The code becomes a token. It is not shown, stored or logged."""
    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)
    flow.start()
    flow.submit_code("good-code")

    rendered = json.dumps(flow.snapshot())

    assert "good-code" not in rendered
    assert "good-code" not in repr(flow)


def test_an_empty_code_is_refused_without_touching_the_process(cli, account):
    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)
    flow.start()

    with pytest.raises(auth.LoginError):
        flow.submit_code("   ")

    assert flow.state == "awaiting_code", "the flow is still waiting, not broken"
    flow.cancel()


def test_a_login_that_prints_no_url_fails_visibly(tmp_path, account, monkeypatch):
    bin_dir = tmp_path / "silent"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text("#!/bin/sh\necho 'nothing useful'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    flow = auth.LoginFlow(account_key=account.key, config_dir=account.root)
    flow.start()

    assert flow.state == "failed"
    assert "authorization URL" in flow.detail


# --- the watch -------------------------------------------------------------


def test_the_watch_caches_the_answer_and_expires_it(cli, account):
    watch = AuthWatch(ttl_s=60)
    assert watch.status(account.key) is None
    assert watch.stale(account.key), "never asked is stale"

    status = watch.refresh(account)

    assert status.broken
    assert watch.status(account.key) is status
    assert not watch.stale(account.key)


def test_a_second_login_replaces_the_first(cli, account):
    """Two live flows mean two PKCE challenges, and the code in hand fits neither."""
    watch = AuthWatch()
    first = watch.start_login(account)
    assert first.state == "awaiting_code"

    second = watch.start_login(account)

    assert second is not first
    assert watch.flow(account.key) is second
    watch.forget(account.key)


def test_submitting_a_code_updates_what_the_deck_believes(cli, account):
    watch = AuthWatch()
    watch.refresh(account)
    assert watch.status(account.key).broken

    watch.start_login(account)
    watch.submit_code(account, "good-code")

    assert watch.status(account.key).logged_in is True


def test_a_render_never_waits_for_a_status_read(cli, account):
    """`request_refresh` is called from render paths, so it must not block or raise."""
    watch = AuthWatch()
    watch.request_refresh([account])  # no running loop here — must be a no-op
    assert watch.status(account.key) is None
    assert (cli / "status-calls").exists() is False


def test_only_claude_accounts_are_watched(cli, tmp_path):
    watch = AuthWatch()
    codex = Account(
        key="codex:local", provider_id="codex", label="local", root=tmp_path / "codex"
    )

    watch.request_refresh([codex])

    assert watch.status(codex.key) is None


def test_the_config_dir_is_a_path_even_when_the_account_carries_a_string(cli, tmp_path):
    """Account.root is typed as a Path, but a config-built account can hand a str."""
    root = tmp_path / "as-string"
    root.mkdir()
    account = Account(
        key="claude_code:alt", provider_id="claude_code", label="alt", root=str(root)
    )

    status = AuthWatch().refresh(account)

    assert status.logged_in is False
    assert isinstance(Path(account.root), Path)
