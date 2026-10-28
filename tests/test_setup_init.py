"""``agentdeck-init`` writes files the deck's own readers accept (#3623)."""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from agentdeck import setup_init
from agentdeck.config import load_config
from agentdeck.poller_engine import config as poller_config
from agentdeck.poller_engine import github_authority

ROOT = Path(__file__).resolve().parent.parent
KEY = (ROOT / "tests" / "fixtures" / "rsa_2048_test_key.pem").read_text()


def test_the_written_files_pass_the_deck_and_poller_readers(tmp_path, monkeypatch):
    """The config, the accounts file and the poller project are read by the real parsers."""
    (tmp_path / ".claude").mkdir()
    accounts = setup_init.detected_accounts(tmp_path)
    assert accounts == [("claude_code", "main", "~/.claude")]

    config_file = tmp_path / "config.toml"
    text = setup_init.config_text((ROOT / "config.example.toml").read_text(), accounts)
    assert "# One [[accounts]] block per" in text  # the comment naming it is kept whole
    config_file.write_text(text.replace("~/.claude", str(tmp_path / ".claude")))
    config = load_config(config_file)
    assert config.inject.enabled is True
    # A Work's agent is a deck Claude agent, in Claude Code's auto mode, written out (#3623).
    assert (config.claude_workers.enabled, config.claude_workers.permission_mode) == (True, "auto")
    assert "[claude_workers]\nenabled = true" in text and 'permission_mode = "auto"' in text
    assert [(a.provider, a.label) for a in config.accounts] == [("claude_code", "main")]

    authority = github_authority.parse_accounts(
        setup_init.authority_text("Alice", 123, "agentdeck-alice", 456)
    )
    assert authority.owners == {"alice": "app:alice"}
    assert authority.app.installations == {"alice": 456}

    checkout = tmp_path / "agent-tasks"
    checkout.mkdir()
    manifest = tmp_path / "poller.toml"
    manifest.write_text(setup_init.manifest_text("Alice", "agent-tasks"))
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(
        setup_init.overlay_text(tmp_path, "agent-tasks", str(checkout), "claude_code:main")
    )
    inspected = poller_config.inspect(manifest, overlay)
    assert inspected.ok, inspected.report.render()


def test_the_app_comes_back_from_the_code_github_sends(monkeypatch):
    """The callback's code is checked against this run's state and traded for the App,
    whose installation on the owner is then found with the App's own JWT."""
    page = setup_init.creation_page(
        setup_init.app_creation_url("acmecorp", True, "s1"),
        setup_init.app_manifest("alice", "https://github.com/alice/agent-tasks", "http://x/cb"),
    )
    assert 'action="https://github.com/organizations/acmecorp/settings/apps/new?state=s1"' in page
    assert "&quot;issues&quot;: &quot;write&quot;" in page

    monkeypatch.setattr(setup_init.socket, "gethostname", lambda: "devbox")
    monkeypatch.setattr(setup_init.getpass, "getuser", lambda: "alice")
    prompt = setup_init.app_prompt(41234, 15)
    assert prompt.startswith("PERSON: Ask the person to open http://127.0.0.1:41234/")
    # A browser on another machine reaches the page, and GitHub's return to it, by a tunnel.
    assert "ssh -L 41234:127.0.0.1:41234 alice@devbox" in prompt

    assert setup_init.code_from("/callback?code=abc123def&state=s1", "s1") == "abc123def"
    with pytest.raises(setup_init.SetupError):
        setup_init.code_from("http://127.0.0.1:9/callback?code=abc123def&state=other", "s1")

    seen: list[urllib.request.Request] = []

    class Answer(io.BytesIO):
        def __init__(self, status, body):
            super().__init__(json.dumps(body).encode())
            self.status = status

    def opener(request, timeout):
        seen.append(request)
        if request.full_url.endswith("/conversions"):
            return Answer(201, {"id": 7, "slug": "agentdeck-alice", "pem": KEY})
        return Answer(200, [{"id": 99, "account": {"login": "Alice"}}])

    app = setup_init.convert_manifest("abc123def", opener=opener)
    assert setup_init.find_installation(app["id"], app["pem"], "alice", opener=opener) == 99
    assert seen[1].get_header("Authorization").startswith("Bearer ey")


def test_a_run_stops_at_a_persons_step_with_what_to_ask_and_what_to_run(monkeypatch, capsys):
    """An agent runs it: no prompt, and a step only a person can do ends the run with exit 3,
    one PERSON line to relay and the THEN command to run once the person has done it."""
    monkeypatch.setattr(setup_init, "step_config", lambda home: "claude_code:main")
    monkeypatch.setattr(setup_init, "step_repository", lambda answers: False)

    def app(answers, *, repository_ready):
        assert (answers.full, repository_ready) == ("alice/agent-tasks", False)
        raise setup_init.NeedsPerson("Ask the person to install it", answers.command())

    monkeypatch.setattr(setup_init, "step_app", app)
    assert setup_init.run(["--login", "alice"]) == setup_init.NEEDS_PERSON
    out = capsys.readouterr().out.splitlines()
    assert out[-2] == "PERSON: Ask the person to install it"
    assert out[-1] == (
        "THEN: ~/agentdeck/.venv/bin/agentdeck-init --login alice --repo alice/agent-tasks"
    )


def test_on_macos_the_services_are_launchd_agents(tmp_path):
    """macOS has no systemd: the web supervisor, the runtime and the poller instance become
    launchd agents with the PATH Homebrew's tools need, loaded fresh on a rerun."""
    import plistlib
    import subprocess

    from agentdeck import launchd

    repo, root = Path("/opt/agentdeck"), tmp_path / "poller"
    jobs = [
        launchd.runtime_job(repo, tmp_path),
        launchd.web_job(repo, tmp_path),
        launchd.poller_job(repo, tmp_path, root, "agent-tasks"),
    ]
    calls: list[list[str]] = []

    def run(command):
        calls.append(command)
        failed = command[1] == "bootstrap" and command[-1].endswith("poller.agent-tasks.plist")
        return subprocess.CompletedProcess(command, 5 if failed else 0, "", "Input/output error")

    failures = launchd.install(jobs, tmp_path, run=run)

    agents = tmp_path / "Library" / "LaunchAgents"
    web = plistlib.loads((agents / "com.agentdeck.web.plist").read_bytes())
    assert web["ProgramArguments"] == [
        "/opt/agentdeck/.venv/bin/python",
        "-m",
        "agentdeck.web_supervisor",
    ]
    assert web["KeepAlive"] is True
    assert "/opt/homebrew/bin" in web["EnvironmentVariables"]["PATH"].split(":")
    poller = plistlib.loads((agents / "com.agentdeck.poller.agent-tasks.plist").read_bytes())
    assert poller["ProgramArguments"][1:] == [
        "/opt/agentdeck/scripts/poller_wake.py",
        "--root",
        str(root),
        "--instance",
        "agent-tasks",
    ]
    assert poller["StartInterval"] == 120
    assert [c[1] for c in calls] == ["bootout", "bootstrap"] * 3
    assert len(failures) == 1 and "poller.agent-tasks" in failures[0]
