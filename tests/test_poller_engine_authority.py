"""The Poller speaks as one explicit non-human authority, or not at all (#2289)."""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agentdeck.poller_engine import __main__ as engine_main
from agentdeck.poller_engine import authorization as auth
from agentdeck.poller_engine import github, github_authority
from agentdeck.poller_engine.extensions import Extension


def _manifest(org: str, authorization: dict | None = None):
    return SimpleNamespace(board=SimpleNamespace(org=org), authorization=authorization or {})


def _no_subprocess(monkeypatch):
    def refuse(*_args, **_kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("gh was run")

    monkeypatch.setattr(github.subprocess, "run", refuse)


def test_a_gh_with_no_credential_refuses_instead_of_using_the_host_login(monkeypatch):
    _no_subprocess(monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")

    code, out, err = github.Gh().run("issue", "view", "1", "-R", "eerovil/r")

    assert (code, out) == (github.EXIT_NO_AUTHORITY, "")
    assert "no GitHub credential" in err


def test_a_gh_speaks_only_with_the_credential_its_source_gives_each_call(monkeypatch):
    seen: list[dict] = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(github.subprocess, "run", run)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "inherited-human-token")
    tokens = iter(["ghs_first", "ghs_second"])
    gh = github.Gh(authority="app:acmecorp", token_source=lambda: next(tokens))

    gh.run("api", "repos/acmecorp/bazaar")
    gh.run("api", "repos/acmecorp/bazaar")

    assert [env["GH_TOKEN"] for env in seen] == ["ghs_first", "ghs_second"]
    for env in seen:
        assert "GITHUB_TOKEN" not in env and "GH_ENTERPRISE_TOKEN" not in env
        assert env["AGENTDECK_GITHUB_AUTHORITY"] == "app:acmecorp"


def test_a_refusing_source_refuses_the_call(monkeypatch):
    _no_subprocess(monkeypatch)

    def refused() -> str:
        raise github_authority.Refusal("no GitHub authority configured for owner x")

    code, _out, err = github.Gh(token_source=refused).run("api", "user")

    assert code == 77 and "owner x" in err


def test_host_and_api_are_one_path():
    gh = github.Gh(token="t")

    adapter = github.GitHubAdapter(
        org="eerovil", issue_repos=(), code_repos=(), project_number=1, api=gh
    )

    assert adapter.host is adapter.api is gh


def test_the_engine_builds_one_gh_for_both_paths_from_its_owner(monkeypatch):
    gh = engine_main._instance_gh(_manifest("eerovil"))

    assert gh.authority == "agent"
    assert gh.token_source() == "test-agent-token"
    assert gh.token == ""  # no static credential, and no Overlay file behind it


def test_an_owner_with_no_authority_refuses_the_wake_up_front():
    with pytest.raises(github_authority.Refusal, match="owner acmecorp"):
        engine_main._authority(_manifest("acmecorp"))


def test_an_empty_or_unreadable_credential_refuses_the_wake(tmp_path, monkeypatch):
    empty = tmp_path / "agent.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_AGENT_GITHUB_ENV", str(empty))

    with pytest.raises(github_authority.Refusal, match="no credential"):
        engine_main._authority(_manifest("eerovil"))


def test_no_operator_credential_path_remains_in_the_engine():
    source = engine_main.__file__
    text = open(source, encoding="utf-8").read()  # noqa: SIM115 — a one-line read
    # The Overlay's credential file is never read, and no `Gh` is built without a credential.
    assert "overlay.credentials" not in text and "_token_from" not in text
    assert "Gh()" not in text


# --- the bot is the authority ------------------------------------------------------------------


@pytest.fixture
def agent():
    return github_authority.authority_for("eerovil")


def test_a_manifest_naming_the_authority_as_bot_keeps_it(agent):
    policy = engine_main._policy(_manifest("eerovil", {"bot": "eerovil-agent"}), agent)

    assert policy.bot == "eerovil-agent"


def test_a_manifest_naming_no_bot_gets_the_authoritys_login():
    app = github_authority.Authority("acmecorp", "app:acmecorp", "agentdeck-eerovil[bot]", 9)

    policy = engine_main._policy(_manifest("acmecorp"), app)

    assert policy.bot == "agentdeck-eerovil[bot]"


def test_a_manifest_naming_another_bot_is_refused(agent):
    with pytest.raises(github_authority.Refusal, match="set \\[authorization\\] bot"):
        engine_main._policy(_manifest("eerovil", {"bot": "someone-else"}), agent)


def test_the_engine_can_never_authorize_itself(agent):
    policy = engine_main._policy(_manifest("eerovil"), agent)

    assert not auth.holds(policy, auth.Tier.RELEASE, login="eerovil-agent", permission="admin")
    assert not policy.names("EEROVIL-AGENT")


# --- extensions get the instance's own credential ----------------------------------------------


def test_extensions_are_handed_the_instances_credential_in_a_private_file(tmp_path, agent):
    loaded = SimpleNamespace(overlay=SimpleNamespace(state_path=str(tmp_path / "state.json")))
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={"repos": ["o/r"]})

    (handed,) = engine_main._hand_token_to_extensions([nudge], loaded, agent)

    path = tmp_path / "github-token"
    assert handed.config == {
        "repos": ["o/r"],
        "github_token_file": str(path),
        "github_token_files": {"eerovil": str(path)},
    }
    assert path.read_text(encoding="utf-8") == "test-agent-token\n"
    assert path.stat().st_mode & 0o777 == 0o600


def _org_accounts(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\neerovil = "agent"\nacmecorp = "app:acmecorp"\n'
        'Northwind = "app:Northwind"\n'
        '[app]\napp_id = 7\nslug = "agentdeck-eerovil"\nprivate_key_file = "/nope"\n'
        "installations = { acmecorp = 11, Northwind = 22 }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    monkeypatch.setattr(
        github_authority, "installation_token", lambda _app, installation: f"ghs-{installation}"
    )
    github_authority.clear_caches()


def test_an_org_instance_is_handed_its_app_and_the_agent_but_no_other_org(tmp_path, monkeypatch):
    """Bazaar's nudge reads its own pull requests as its installation and the review chats in
    `eerovil/review-chats` as the agent; Trailhead's installation token is never written."""
    _org_accounts(tmp_path, monkeypatch)
    loaded = SimpleNamespace(overlay=SimpleNamespace(state_path=str(tmp_path / "state.json")))
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={})

    (handed,) = engine_main._hand_token_to_extensions(
        [nudge], loaded, github_authority.authority_for("acmecorp")
    )

    files = handed.config["github_token_files"]
    assert files == {
        "acmecorp": str(tmp_path / "github-token"),
        "eerovil": str(tmp_path / "github-token-eerovil"),
    }
    assert handed.config["github_token_file"] == files["acmecorp"]
    assert (tmp_path / "github-token").read_text() == "ghs-11\n"
    assert (tmp_path / "github-token-eerovil").read_text() == "test-agent-token\n"
    assert (tmp_path / "github-token-eerovil").stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("github-token-northwind"))


def test_an_org_instance_is_handed_the_review_chat_owner_through_its_app(tmp_path, monkeypatch):
    """#2682: once `eerovil` speaks through the App, Bazaar's nudge still gets a credential for
    the review chats in `eerovil/review-chats` — that installation's token, not the agent's."""
    _org_accounts(tmp_path, monkeypatch)
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        accounts.read_text(encoding="utf-8")
        .replace('eerovil = "agent"', 'eerovil = "app:eerovil"')
        .replace("{ acmecorp = 11,", "{ eerovil = 33, acmecorp = 11,"),
        encoding="utf-8",
    )
    github_authority.clear_caches()
    loaded = SimpleNamespace(overlay=SimpleNamespace(state_path=str(tmp_path / "state.json")))
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={})

    (handed,) = engine_main._hand_token_to_extensions(
        [nudge], loaded, github_authority.authority_for("acmecorp")
    )

    assert handed.config["github_token_files"] == {
        "acmecorp": str(tmp_path / "github-token"),
        "eerovil": str(tmp_path / "github-token-eerovil"),
    }
    assert (tmp_path / "github-token-eerovil").read_text() == "ghs-33\n"
    assert not list(tmp_path.glob("github-token-northwind"))


def test_a_missing_agent_credential_does_not_stop_an_org_wake(tmp_path, monkeypatch):
    """Only the instance's own credential is required; without the agent's the chat reads are
    refused by the extension, and the wake still runs."""
    _org_accounts(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTDECK_AGENT_GITHUB_ENV", str(tmp_path / "absent.env"))
    stale = tmp_path / "github-token-eerovil"
    stale.write_text("old\n")
    loaded = SimpleNamespace(overlay=SimpleNamespace(state_path=str(tmp_path / "state.json")))
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={})

    (handed,) = engine_main._hand_token_to_extensions(
        [nudge], loaded, github_authority.authority_for("acmecorp")
    )

    assert handed.config["github_token_files"] == {
        "acmecorp": str(tmp_path / "github-token"),
        # Kept, with no file: the extension reads it as unreadable, never as "no chat there".
        "eerovil": "",
    }
    assert not stale.exists(), "a credential that was not refreshed is not left behind"


def test_a_credential_file_for_an_owner_no_longer_handed_is_removed(tmp_path, monkeypatch):
    _org_accounts(tmp_path, monkeypatch)
    gone = tmp_path / "github-token-northwind"
    gone.write_text("old-agent-token\n")
    loaded = SimpleNamespace(overlay=SimpleNamespace(state_path=str(tmp_path / "state.json")))
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={})

    engine_main._hand_token_to_extensions(
        [nudge], loaded, github_authority.authority_for("acmecorp")
    )

    assert not gone.exists()
    assert (tmp_path / "github-token-eerovil").exists()


# --- the wake refuses, and says why -----------------------------------------------------------


@dataclass
class _Loaded:
    manifest: object
    overlay: object = field(default_factory=lambda: SimpleNamespace(state_path="/nonexistent"))


def test_a_wake_with_no_authority_stops_before_github_and_records_why(monkeypatch, capsys):
    recorded: list[str] = []
    monkeypatch.setattr(
        engine_main.config, "load", lambda *_a, **_k: _Loaded(_manifest("stranger"))
    )
    monkeypatch.setattr(engine_main, "_enabled_extensions", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(engine_main, "_record_drift", lambda _args, why: recorded.append(why))
    monkeypatch.setattr(
        engine_main, "_adapter", lambda *_a, **_k: pytest.fail("an adapter was built")
    )
    args = argparse.Namespace(manifest="m", overlay="o", binding="b")

    assert engine_main._cmd_cycle(args) == 77
    assert "refused: github: no GitHub authority configured for owner stranger" in (
        capsys.readouterr().err
    )
    assert recorded and "GitHub authority refused" in recorded[0]


def test_a_transient_mint_failure_defers_the_wake_without_recording_a_refusal(monkeypatch, capsys):
    recorded: list[str] = []
    monkeypatch.setattr(
        engine_main.config, "load", lambda *_a, **_k: _Loaded(_manifest("eerovil"))
    )
    monkeypatch.setattr(engine_main, "_enabled_extensions", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(engine_main, "_record_drift", lambda _args, why: recorded.append(why))

    def outage(_manifest):
        raise github_authority.TransientRefusal("GitHub refused a token (HTTP 502)")

    monkeypatch.setattr(engine_main, "_authority", outage)
    args = argparse.Namespace(manifest="m", overlay="o", binding="b")

    assert engine_main._cmd_cycle(args) == 75
    assert "deferred: github" in capsys.readouterr().err
    assert recorded == []


def test_an_extension_never_inherits_a_github_token(monkeypatch):
    from agentdeck.poller_engine import extensions

    seen: list[dict] = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    monkeypatch.setattr(extensions.subprocess, "run", run)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "inherited-human-token")
    monkeypatch.setenv("KEEP_ME", "1")

    extensions.run_program(["x"], "{}", 5.0)

    assert not {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"} & set(seen[0])
    assert seen[0]["KEEP_ME"] == "1"


def test_the_engine_process_drops_every_inherited_token_at_start(monkeypatch):
    import os

    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")
    monkeypatch.setattr(
        engine_main, "build_parser",
        lambda: SimpleNamespace(parse_args=lambda _argv: SimpleNamespace(handler=lambda _a: 0)),
    )

    assert engine_main.main([]) == 0
    assert "GH_TOKEN" not in os.environ


def test_a_credential_that_cannot_be_handed_over_defers_the_wake(tmp_path, agent):
    loaded = SimpleNamespace(
        overlay=SimpleNamespace(state_path=str(tmp_path / "missing-dir" / "state.json"))
    )
    nudge = Extension(name="chatgpt-nudge", program=("x",), config={})

    assert engine_main._hand_token_to_extensions([nudge], loaded, agent) is None
