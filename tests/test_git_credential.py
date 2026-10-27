"""AgentDeck's git credential helper answers per repository owner, and nothing else (#2289)."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from agentdeck import git_credential
from agentdeck.poller_engine import github_authority


def _request(**fields: str) -> list[str]:
    return [f"{key}={value}\n" for key, value in fields.items()] + ["\n"]


def _ask(**fields: str) -> tuple[str, str]:
    return git_credential.answer(git_credential.parse_request(_request(**fields)))


@pytest.mark.parametrize(
    "path",
    ["eerovil/agentdeck.git", "eerovil/agentdeck", "EEROVIL/agentdeck.git", "Eerovil/agentdeck/"],
)
def test_an_agent_owner_gets_the_agent_account(path):
    stdout, stderr = _ask(protocol="https", host="github.com", path=path)

    assert stdout == "username=eerovil-agent\npassword=test-agent-token\n\n"
    assert stderr == ""


def test_an_app_owner_gets_an_installation_token(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nNorthwind = "app:Northwind"\n[app]\napp_id = 7\n'
        'slug = "agentdeck-eerovil"\nprivate_key_file = "k"\n'
        "installations = { Northwind = 99 }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    seen = []

    def installation_token(app, installation_id, **_kwargs):
        seen.append(installation_id)
        return "ghs_installation"

    monkeypatch.setattr(github_authority, "installation_token", installation_token)

    stdout, _ = _ask(protocol="https", host="github.com", path="northwind/store.git")

    assert stdout == "username=x-access-token\npassword=ghs_installation\n\n"
    assert seen == [99]


@pytest.mark.parametrize(
    "fields",
    [
        {"protocol": "https", "host": "github.com", "path": "stranger/repo.git"},
        {"protocol": "https", "host": "gist.github.com", "path": "eerovil/abc.git"},
        {"protocol": "https", "host": "github.com"},  # useHttpPath off: no path to judge
        {"protocol": "https", "host": "github.com", "path": "eerovil"},
        {"protocol": "http", "host": "github.com", "path": "eerovil/agentdeck.git"},
        {"protocol": "https", "host": "example.com", "path": "eerovil/agentdeck.git"},
    ],
)
def test_anything_else_gets_no_credential_and_says_why(fields):
    stdout, stderr = _ask(**fields)

    assert stdout == ""
    assert stderr.startswith("agentdeck git-credential: ")


def test_an_unknown_owner_is_named_in_the_refusal():
    _stdout, stderr = _ask(protocol="https", host="github.com", path="stranger/repo.git")

    assert "no GitHub authority configured for owner stranger" in stderr


def test_a_url_line_is_understood_too():
    request = git_credential.parse_request(["url=https://github.com/eerovil/agentdeck.git\n"])

    assert git_credential.owner_from_request(request) == "eerovil"


def test_store_and_erase_are_accepted_and_do_nothing(monkeypatch, capsys):
    for action in ("store", "erase"):
        monkeypatch.setattr(sys, "stdin", io.StringIO("".join(_request(
            protocol="https", host="github.com", path="eerovil/agentdeck.git",
            username="x", password="y",
        ))))
        assert git_credential.main([action]) == 0
    assert capsys.readouterr().out == ""


def test_git_itself_takes_the_helpers_answer(tmp_path):
    """The real protocol, end to end: `git credential fill` with only this helper configured."""
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        **{key: value for key, value in __import__("os").environ.items() if key.startswith(
            ("AGENTDECK_", "PYTHONPATH")
        )},
    }
    helper = f"!{sys.executable} -m agentdeck.git_credential"
    done = subprocess.run(
        [
            "git", "-c", "credential.helper=", "-c", f"credential.helper={helper}",
            "-c", "credential.useHttpPath=true", "credential", "fill",
        ],
        input="protocol=https\nhost=github.com\npath=eerovil/agentdeck.git\n\n",
        capture_output=True, text=True, env=environment, check=False, timeout=30,
    )

    assert done.returncode == 0, done.stderr
    assert "username=eerovil-agent" in done.stdout
    assert "password=test-agent-token" in done.stdout
