"""#2682: a GitHub identity the deck speaks as, suspended by GitHub, is named once."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from agentdeck import gh_cli, github_suspension
from agentdeck.poller_engine.github_authority import Authority

USER_SUSPENDED = (
    b"gh: Sorry. Your account was suspended. (HTTP 403)\n"
    b'{"message":"Sorry. Your account was suspended."}\n'
)
AGENT = Authority(owner="eerovil", name="agent", login="eerovil-agent")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(github_suspension, "_suspended", {})


def _spawner(monkeypatch, results):
    pending = list(results)

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        code, stderr = pending.pop(0)
        return subprocess.CompletedProcess(list(argv), code, b"", stderr if keep_stderr else None)

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)


def test_githubs_suspension_words_mark_the_authority_and_a_success_clears_it():
    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED, now=100.0)

    (item,) = github_suspension.suspensions()
    assert (item.authority, item.login, item.owners, item.since) == (
        "agent",
        "eerovil-agent",
        ("eerovil",),
        100.0,
    )
    assert item.detail == "gh: Sorry. Your account was suspended. (HTTP 403)"
    assert github_suspension.for_owner("EeroVil") == item

    github_suspension.observe(AGENT, ok=True)
    assert github_suspension.suspensions() == ()


@pytest.mark.parametrize(
    "text",
    [b"", None, b"HTTP 404: Not Found", b"HTTP 403: Resource not accessible by integration"],
)
def test_any_other_failure_says_nothing_about_the_account(text):
    github_suspension.observe(AGENT, ok=False, text=text)
    assert github_suspension.suspensions() == ()


def test_a_later_failure_keeps_when_it_was_first_seen():
    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED, now=100.0)
    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED, now=500.0)
    assert github_suspension.suspensions()[0].since == 100.0


def test_an_owner_moved_to_another_authority_is_no_longer_shown(tmp_path, monkeypatch):
    """Moving the owner is the fix; it should not wait for a web restart to show."""
    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED)
    assert github_suspension.for_owner("eerovil") is not None

    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\neerovil = "app:eerovil"\n'
        f'[app]\napp_id = 1\nslug = "agentdeck-eerovil"\nprivate_key_file = "{tmp_path}/k.pem"\n'
        "[app.installations]\neerovil = 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))

    assert github_suspension.suspensions() == ()
    assert github_suspension.for_owner("eerovil") is None


async def test_a_gh_read_that_github_refuses_as_suspended_is_recorded(monkeypatch):
    _spawner(monkeypatch, [(1, USER_SUSPENDED), (0, b"")])

    assert await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0) == (1, b"")
    assert github_suspension.for_owner("eerovil").login == "eerovil-agent"

    await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)
    assert github_suspension.suspensions() == ()


async def test_a_write_refused_as_suspended_is_recorded_too(monkeypatch):
    _spawner(monkeypatch, [(1, USER_SUSPENDED)])

    code, _out, err = await gh_cli.run_once(
        ["gh", "issue", "comment", "1", "--repo", "eerovil/r", "--body-file", "-"],
        timeout=5.0,
        stdin=b"hi",
    )
    assert (code, err) == (1, USER_SUSPENDED)
    assert github_suspension.for_owner("eerovil") is not None


async def test_git_is_never_attributed_to_an_authority(monkeypatch):
    """`git` gets its credential from the helper, per remote; this layer cannot tell whose."""
    _spawner(monkeypatch, [(128, USER_SUSPENDED)])
    await gh_cli.run(["git", "-C", "/x", "fetch", "origin"], timeout=5.0)
    assert github_suspension.suspensions() == ()


async def test_a_suspended_app_installation_is_recorded_from_its_refused_token(monkeypatch):
    from agentdeck import github_identity

    def refused(*_args, **_kwargs):
        raise github_identity.Refusal(
            "the AgentDeck App (app 1) installation 5: GitHub refused a token "
            "(HTTP 403: This installation has been suspended)"
        )

    monkeypatch.setattr(github_identity, "environment_for", refused)
    _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal):
        await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)
    (item,) = github_suspension.suspensions()
    assert item.owners == ("eerovil",)
    assert item.detail.endswith("(HTTP 403: This installation has been suspended)")


# --- where it shows ---------------------------------------------------------------


def _config(tmp_path):
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, PollerConfig

    return AppConfig(
        history=HistoryConfig(db_path=str(tmp_path / "budgets.db")),
        poller=PollerConfig(root=str(tmp_path / "poller")),
        accounts=[
            AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path / "acc"))
        ],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    from agentdeck.app import create_app

    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    monkeypatch.setenv("AGENTDECK_DEPLOY_STATE", str(tmp_path / "deploy"))
    with TestClient(create_app(_config(tmp_path))) as test_client:
        yield test_client


def test_healthz_says_github_is_ok_or_suspended_without_changing_status(client):
    assert client.get("/healthz").json()["github"] == {"state": "ok", "suspended": []}

    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED, now=100.0)
    body = client.get("/healthz").json()

    # `status` is what the deploy script reads as "the web process is up"; it still is.
    assert body["status"] == "ok"
    assert body["github"]["state"] == "suspended"
    assert body["github"]["suspended"] == [
        {
            "authority": "agent",
            "login": "eerovil-agent",
            "owners": ["eerovil"],
            "since": 100.0,
            "detail": "gh: Sorry. Your account was suspended. (HTTP 403)",
        }
    ]


def test_the_topbar_shows_one_red_pill_only_while_suspended(client):
    assert "GitHub suspended" not in client.get("/").text

    github_suspension.observe(AGENT, ok=False, text=USER_SUSPENDED)
    body = " ".join(client.get("/").text.split())

    assert body.count('class="deployment-status deployment-status-blocked github-status"') == 1
    assert "GitHub account suspended: eerovil-agent (agent) is refused for eerovil/*" in body
    assert "github-authority.toml" in body
