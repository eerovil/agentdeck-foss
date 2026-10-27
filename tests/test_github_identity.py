"""Which non-human identity speaks for which repository, and nothing else can (#2289)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentdeck import github_identity
from agentdeck.gh_shim import classify
from agentdeck.poller_engine import credentials, github_authority

AGENT = "eerovil/agentdeck"
ORG = "acmecorp/bazaar"


def _map(tmp_path, monkeypatch, text: str) -> Path:
    path = tmp_path / "github-authority.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(github_identity.ACCOUNTS_FILE_ENV, str(path))
    return path


_APP = """
[agent]
login = "eerovil-agent"

[owners]
eerovil = "agent"
acmecorp = "app:acmecorp"
Northwind = "app:Northwind"

[app]
app_id = 42
slug = "agentdeck-eerovil"
private_key_file = "{key}"
installations = {{ acmecorp = 111, Northwind = 222 }}
"""


def test_the_owner_is_read_from_every_shape_a_reference_arrives_in():
    for reference in (
        "eerovil/agentdeck",
        "eerovil/agentdeck#2105",
        "https://github.com/eerovil/agentdeck",
        "https://github.com/eerovil/agentdeck/pull/2105",
        "git@github.com:eerovil/agentdeck.git",
        "ssh://git@github.com/eerovil/agentdeck.git",
        "https://eero@github.com/eerovil/agentdeck.git",
        "EEROVIL/agentdeck",
    ):
        assert github_identity.owner_of(reference) == "eerovil", reference


def test_an_unreadable_reference_has_no_owner_and_is_refused():
    assert github_identity.owner_of("/var/home/me/agentdeck") == ""
    assert github_identity.owner_of("") == ""
    with pytest.raises(github_identity.Refusal, match="no target owner"):
        github_identity.authority_for("/var/home/me/agentdeck")


# --- the owner map --------------------------------------------------------------------------


def test_no_owner_map_is_the_agent_for_eerovil_and_nothing_for_anyone_else():
    assert github_identity.authority_for(AGENT).name == "agent"
    assert github_identity.authority_for(AGENT).login == "eerovil-agent"
    with pytest.raises(github_identity.Refusal, match="owner acmecorp"):
        github_identity.authority_for(ORG)


def test_each_owner_maps_deterministically_to_its_non_human_authority(tmp_path, monkeypatch):
    _map(tmp_path, monkeypatch, _APP.format(key=tmp_path / "key.pem"))

    agent = github_identity.authority_for(AGENT)
    bazaar = github_identity.authority_for(ORG)
    store = github_identity.authority_for("Northwind/store")

    assert (agent.name, agent.login, agent.installation_id) == ("agent", "eerovil-agent", 0)
    assert (bazaar.name, bazaar.login, bazaar.installation_id) == (
        "app:acmecorp", "agentdeck-eerovil[bot]", 111
    )
    assert (store.name, store.installation_id) == ("app:northwind", 222)


def test_owners_are_case_folded_everywhere(tmp_path, monkeypatch):
    _map(tmp_path, monkeypatch, _APP.format(key=tmp_path / "key.pem"))

    assert github_identity.authority_for("northwind/abacus").installation_id == 222
    assert github_identity.authority_for("NORTHWIND/abacus").installation_id == 222
    assert github_identity.authority_for(owner="Northwind").installation_id == 222


def test_an_unknown_owner_is_a_hard_refusal_with_a_useful_reason(tmp_path, monkeypatch):
    _map(tmp_path, monkeypatch, _APP.format(key=tmp_path / "key.pem"))

    with pytest.raises(github_identity.Refusal) as refused:
        github_identity.authority_for("einarivil/thing")
    assert "no GitHub authority configured for owner einarivil" in str(refused.value)
    assert "github-authority.toml" in str(refused.value)


@pytest.mark.parametrize("spelled", ["operator", "Operator", " operator "])
def test_operator_is_not_a_valid_authority(spelled):
    with pytest.raises(github_authority.ConfigError, match="no longer an authority"):
        github_authority.parse_accounts(f'[owners]\neerovil = "{spelled}"\n')


def test_a_file_naming_operator_refuses_every_owner_rather_than_guessing(tmp_path, monkeypatch):
    _map(tmp_path, monkeypatch, '[owners]\neerovil = "agent"\nacmecorp = "operator"\n')

    for target in (AGENT, ORG):
        with pytest.raises(github_identity.Refusal, match="operator"):
            github_identity.authority_for(target)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('[owners]\neerovil = "agnet"\n', "must be"),
        ('[owners]\nacmecorp = "app:acmecorp"\n', r"needs an \[app\] table"),
        (
            '[owners]\nacmecorp = "app:acmecorp"\n[app]\napp_id = 1\nslug = "s"\n'
            'private_key_file = "k"\ninstallations = { other = 2 }\n',
            "installations has no acmecorp",
        ),
        ('[owners]\neerovil = "agent"\nEerovil = "agent"\n', "listed twice"),
        ('[app]\napp_id = 0\nslug = "s"\nprivate_key_file = "k"\n', "positive integer"),
        ('[app]\napp_id = 1\nprivate_key_file = "k"\n', "slug"),
        ("not toml [", "not valid TOML"),
    ],
)
def test_a_bad_accounts_file_is_refused_with_its_reason(text, reason):
    with pytest.raises(github_authority.ConfigError, match=reason):
        github_authority.parse_accounts(text)


def test_a_switch_takes_effect_without_a_restart(tmp_path, monkeypatch):
    path = _map(tmp_path, monkeypatch, '[owners]\neerovil = "agent"\n')
    assert github_identity.authority_for(AGENT).name == "agent"

    path.write_text("[owners]\n", encoding="utf-8")
    os.utime(path, ns=(1, 1))
    with pytest.raises(github_identity.Refusal):
        github_identity.authority_for(AGENT)


# --- the credential and the child environment -----------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["AGENTDECK_AGENT_GITHUB_TOKEN=ghp_agent_account\n", "ghp_agent_account\n",
     "# comment\nexport AGENTDECK_AGENT_GITHUB_TOKEN='ghp_agent_account'\n"],
)
def test_both_credential_file_shapes_are_read(tmp_path, monkeypatch, body):
    path = tmp_path / "agent.env"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(github_identity.TOKEN_FILE_ENV, str(path))

    assert github_identity.environment_for(AGENT, {})["GH_TOKEN"] == "ghp_agent_account"


def test_the_poller_reads_the_same_credential_file_through_the_same_parser(tmp_path):
    path = tmp_path / "agent.env"
    path.write_text("AGENTDECK_AGENT_GITHUB_TOKEN=ghp_x\n", encoding="utf-8")
    assert credentials.read_token(path) == "ghp_x"


def test_every_credential_variable_is_replaced_and_the_authority_is_stamped():
    inherited = {
        "GH_TOKEN": "human",
        "GITHUB_TOKEN": "human",
        "GH_ENTERPRISE_TOKEN": "human",
        "GITHUB_ENTERPRISE_TOKEN": "human",
        "PATH": "/bin",
    }

    built = github_identity.environment_for(AGENT, inherited)

    assert built["GH_TOKEN"] == built["GITHUB_TOKEN"] == "test-agent-token"
    assert "GH_ENTERPRISE_TOKEN" not in built and "GITHUB_ENTERPRISE_TOKEN" not in built
    assert built[github_identity.AUTHORITY_ENV] == "agent"
    assert built["PATH"] == "/bin"
    assert inherited["GH_TOKEN"] == "human"  # the caller's dict is not mutated


def test_a_missing_agent_credential_is_refused_naming_the_account(tmp_path, monkeypatch):
    monkeypatch.setenv(github_identity.TOKEN_FILE_ENV, str(tmp_path / "missing"))

    with pytest.raises(github_identity.Refusal, match="eerovil-agent has no credential"):
        github_identity.environment_for(AGENT, {"GH_TOKEN": "human"})


def test_the_default_credential_file_is_the_one_that_is_actually_installed():
    assert github_identity.DEFAULT_TOKEN_FILE == "~/.config/agentdeck/agent-github.env"


def test_no_example_overlay_names_a_github_credential_file():
    """The engine derives its authority from the owner map; an Overlay path would be ignored."""
    root = Path(__file__).resolve().parents[1] / "poller"
    for overlay in root.rglob("overlay.example.toml"):
        text = overlay.read_text(encoding="utf-8")
        assert "github_token_file" not in text, overlay
        assert "poller-token" not in text, overlay


# --- reading the target owner off a command line -------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["gh", "pr", "list", "--repo", AGENT], "eerovil"),
        (["gh", "pr", "list", f"--repo={AGENT}"], "eerovil"),
        (["gh", "pr", "list", "-R", "Northwind/store"], "northwind"),
        (["gh", "api", f"repos/{AGENT}/issues/5"], "eerovil"),
        (["gh", "api", f"/repos/{AGENT}/issues/5?per_page=1"], "eerovil"),
        (["gh", "api", "--paginate", f"repos/{AGENT}/pulls"], "eerovil"),
        (["gh", "api", "orgs/acmecorp/projectsV2"], "acmecorp"),
        (["gh", "api", "users/eerovil"], "eerovil"),
        (["gh", "issue", "view", f"https://github.com/{AGENT}/issues/5"], "eerovil"),
        (["gh", "repo", "view", ORG], "acmecorp"),
        (["/usr/bin/gh", "pr", "view", "1", "--repo", ORG], "acmecorp"),
        # Shaped like owner/name, naming nobody (#2289 review): refused, never misread.
        (["gh", "pr", "view", "agent/issue-2289"], ""),
        (["gh", "api", "search/issues", "-f", "q=x"], ""),
        (["gh", "pr", "checkout", "feature/x"], ""),
        # A URL inside a flag's value is not the target.
        (["gh", "pr", "comment", "7", "--body", "see https://github.com/acmecorp/bazaar"], ""),
        (["gh", "api", "graphql", "-f", "query=q"], ""),
        (["gh", "pr", "list"], ""),
        ([], ""),
    ],
)
def test_the_owner_is_read_off_the_command_the_caller_already_wrote(argv, expected):
    assert github_identity.owner_in_argv(argv) == expected


def test_git_command_lines_are_never_read_for_an_owner():
    assert github_identity.owner_in_argv(["git", "log", "origin/master"]) == ""
    assert github_identity.owner_in_argv(["/usr/bin/git", "-C", AGENT, "status"]) == ""


def test_the_shim_reads_the_target_owner_the_same_way():
    remote = f"git@github.com:{AGENT}.git"
    assert classify.target_owner(["pr", "list", "--repo", ORG], remote=remote) == "acmecorp"
    assert classify.target_owner(["api", f"repos/{ORG}/issues/5"], remote=remote) == "acmecorp"
    assert classify.target_owner(["pr", "list"], remote=remote) == "eerovil"
    # A branch name is not a repository: the checkout's own remote decides (#2289 review).
    assert classify.target_owner(["pr", "view", "agent/issue-2289"], remote=remote) == "eerovil"
    assert classify.target_owner(["api", "search/issues"], remote=remote) == "eerovil"
    assert classify.target_owner(["pr", "list"], remote="/var/home/me/agentdeck") == ""
    assert classify.target_owner(["pr", "list"]) == ""


# --- git transport for workers --------------------------------------------------------------


def _git_config(environment: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (environment[f"GIT_CONFIG_KEY_{index}"], environment[f"GIT_CONFIG_VALUE_{index}"])
        for index in range(int(environment["GIT_CONFIG_COUNT"]))
    ]


def test_a_worker_environment_speaks_only_through_the_agentdeck_helper():
    environment = {
        "GH_TOKEN": "human",
        "GITHUB_TOKEN": "human",
        "PATH": "/bin",
        "SSH_ASKPASS": "/usr/bin/ksshaskpass",
        "GIT_ASKPASS": "/usr/bin/ksshaskpass",
    }

    github_identity.configure_github_git(environment)

    assert "GH_TOKEN" not in environment and "GITHUB_TOKEN" not in environment
    # Nothing it runs may ask a person: no askpass program, no terminal prompt (#3632).
    assert "SSH_ASKPASS" not in environment and "GIT_ASKPASS" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    entries = _git_config(environment)
    helpers = [value for key, value in entries if key == "credential.https://github.com.helper"]
    # The list is reset first, so no earlier helper (`gh auth git-credential`) is ever asked.
    assert helpers[0] == ""
    assert helpers[1].endswith("-m agentdeck.git_credential")
    assert ("credential.https://github.com.useHttpPath", "true") in entries
    assert ("credential.interactive", "false") in entries
    rewrites = {value for key, value in entries if key == "url.https://github.com/.insteadOf"}
    assert rewrites == {"git@github.com:", "ssh://git@github.com/"}
    assert not any("gh auth git-credential" in value for _key, value in entries)


def test_a_refused_credential_fails_without_opening_a_password_box(tmp_path):
    """The deck's services inherit the desktop's ``SSH_ASKPASS``; when the helper
    refused an owner, git ran it and a password box opened on the host (#3632)."""
    marker = tmp_path / "asked"
    askpass = tmp_path / "askpass"
    askpass.write_text(f"#!/bin/sh\ntouch {marker}\necho guessed\n")
    askpass.chmod(0o755)
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "SSH_ASKPASS": str(askpass)}
    github_identity.configure_github_git(environment)
    # Stand in for the helper refusing this owner: it answers nothing.
    index = int(environment["GIT_CONFIG_COUNT"])
    environment[f"GIT_CONFIG_KEY_{index}"] = "credential.https://github.com.helper"
    environment[f"GIT_CONFIG_VALUE_{index}"] = "!true"
    environment["GIT_CONFIG_COUNT"] = str(index + 1)

    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=Shop/Store.git\n\n",
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert not marker.exists()


def test_existing_git_config_entries_are_kept():
    environment = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/hooks",
    }

    github_identity.configure_github_git(environment)

    assert _git_config(environment)[0] == ("core.hooksPath", "/hooks")


# --- `agentdeck gh`: one call as its owner's authority (#2289) ------------------------------


def test_agentdeck_gh_runs_as_the_owners_authority_not_the_inherited_login(monkeypatch):
    seen = []
    monkeypatch.setenv("GH_TOKEN", "human-token")
    monkeypatch.setattr(
        github_identity.subprocess,
        "run",
        lambda argv, env, check: seen.append((argv, env)) or SimpleNamespace(returncode=0),
    )

    assert github_identity.gh_main(["repo", "view", "eerovil/lane"]) == 0

    ((argv, env),) = seen
    assert argv == ["gh", "repo", "view", "eerovil/lane"]
    assert env["GH_TOKEN"] == "test-agent-token"


@pytest.mark.parametrize(
    "args", [["repo", "view", "stranger/lane"], ["api", "search/issues"], ["auth", "status"]]
)
def test_agentdeck_gh_refuses_before_gh_runs(monkeypatch, capsys, args):
    monkeypatch.setenv("GH_TOKEN", "human-token")

    def refuse(*_a, **_k):  # pragma: no cover - reaching it is the failure
        raise AssertionError("gh ran")

    monkeypatch.setattr(github_identity.subprocess, "run", refuse)

    assert github_identity.gh_main(args) == 77
    assert "refused" in capsys.readouterr().err
