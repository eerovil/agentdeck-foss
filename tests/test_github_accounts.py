"""`agentdeck github-account show|prove|doctor` (#2274, #2289)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentdeck import github_accounts
from agentdeck.poller_engine import github_authority

HUMAN = "ghp_human_secret_value_123"
AGENT = "ghp_agent_secret_value_456"
BOT = "ghs_bot_secret_value_789"
LOGINS = {HUMAN: "eerovil", AGENT: "eerovil-agent", BOT: "agentdeck-eerovil[bot]"}


def _fetch(token: str, url: str, body: bytes | None):
    if url.endswith("/user"):
        login = LOGINS.get(token)
        if login is None:
            return 401, {}, b"{}"
        return 200, {"X-RateLimit-Limit": "5000"}, json.dumps({"login": login}).encode()
    raise AssertionError(f"unexpected request {url}")


class _Host:
    """What the doctor's commands answer on a fixture host."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv, environment):
        self.calls.append(argv)
        key = argv[0] if argv[0] != "docker" else f"docker-{argv[1]}"
        answer = self.answers.get(key)
        if callable(answer):
            return answer(argv)
        return answer or (1, "", "")


def _clean_host() -> _Host:
    helper = "credential.https://github.com.helper !/venv/bin/python -m agentdeck.git_credential\n"
    return _Host(
        gh=(1, "", "You are not logged into any GitHub hosts.\n"),
        git=(0, helper, ""),
        ssh=(255, "", "git@github.com: Permission denied (publickey).\n"),
    )


_REAL_DOCTOR = github_accounts.doctor


def _doctor(tmp_path, host, **kwargs):
    return _REAL_DOCTOR(
        home=tmp_path / "home", env_files=kwargs.pop("env_files", ()),
        root=tmp_path / "poller", fetch=_fetch, run=host, **kwargs,
    )


def _rendered(findings) -> str:
    return "\n".join(finding.render() for finding in findings)


def test_a_clean_host_has_no_findings(tmp_path):
    assert _doctor(tmp_path, _clean_host()) == []


def test_unit_environment_files_and_dev_container_env_files_are_read(tmp_path):
    home = tmp_path / "home"
    units = home / ".config" / "systemd" / "user"
    (units / "agentdeck.service.d").mkdir(parents=True)
    trailhead_env = home / "trailhead" / ".env"
    trailhead_env.parent.mkdir(parents=True)
    trailhead_env.write_text(f"CURSOR_KEY=x\nGH_TOKEN={HUMAN}\n", encoding="utf-8")
    (units / "agentdeck.service.d" / "override.conf").write_text(
        f"[Service]\nEnvironmentFile=-{trailhead_env}\n", encoding="utf-8"
    )
    bazaar_env = home / "bazaar" / ".env"
    bazaar_env.parent.mkdir(parents=True)
    bazaar_env.write_text(f'export GITHUB_TOKEN="{HUMAN}"\n', encoding="utf-8")
    agent_env = home / "agent.env"
    agent_env.write_text(f"GH_TOKEN={AGENT}\n", encoding="utf-8")

    findings = _doctor(
        tmp_path, _clean_host(), env_files=("~/bazaar/.env", str(agent_env))
    )

    kinds = sorted((f.kind, Path(f.location.split()[0]).name, f.identity) for f in findings)
    assert kinds == [
        ("token in env file", ".env", "eerovil"),
        ("token in env file", ".env", "eerovil"),
    ]
    assert any("EnvironmentFile of override.conf" in f.location for f in findings)
    assert HUMAN not in _rendered(findings) and AGENT not in _rendered(findings)


def test_a_token_whose_identity_cannot_be_resolved_is_reported_as_possibly_human(tmp_path):
    env = tmp_path / "x.env"
    env.write_text("GH_TOKEN=ghp_revoked\n", encoding="utf-8")

    findings = _doctor(tmp_path, _clean_host(), env_files=(str(env),))

    assert [f.identity for f in findings] == ["identity unresolved"]


def test_a_logged_in_gh_is_found(tmp_path):
    host = _clean_host()
    status = "github.com\n  ✓ Logged in to github.com account eerovil (keyring)\n"
    host.answers["gh"] = (0, "", status)

    findings = _doctor(tmp_path, host)

    assert [(f.kind, f.identity) for f in findings] == [("gh login", "eerovil")]
    gh_call = next(call for call in host.calls if call[0] == "gh")
    assert gh_call == ["gh", "auth", "status", "--hostname", "github.com"]


def test_a_global_git_helper_that_is_not_agentdecks_is_found(tmp_path):
    host = _clean_host()
    host.answers["git"] = (
        0,
        "credential.https://github.com.helper !/home/e/.local/bin/gh auth git-credential\n"
        "credential.https://gist.github.com.helper !/home/e/.local/bin/gh auth git-credential\n",
        "",
    )

    findings = _doctor(tmp_path, host)

    kinds = [f.kind for f in findings]
    assert kinds.count("git credential helper") == 3  # two foreign helpers, and ours missing
    assert any("not installed globally" in f.remediation for f in findings)


def test_the_old_operator_token_and_overlays_naming_credential_files_are_found(tmp_path):
    operator = tmp_path / "home" / ".config" / "agentdeck" / "poller-token"
    operator.parent.mkdir(parents=True)
    operator.write_text(HUMAN + "\n", encoding="utf-8")
    instance = tmp_path / "poller" / "instances" / "bazaar"
    instance.mkdir(parents=True)
    (instance / "poller.toml").write_text('[board]\norg = "acmecorp"\n', encoding="utf-8")
    (instance / "overlay.toml").write_text(
        '[credentials]\ngithub_token_file = "~/.config/agentdeck/poller-token"\n'
        '[extensions.chatgpt-nudge]\ntoken_file = "~/.config/agentdeck/poller-token"\n'
        '[extensions.sentry-autofix]\ntoken_file = "~/.config/agentdeck/sentry-token"\n',
        encoding="utf-8",
    )

    findings = _doctor(tmp_path, _clean_host())

    rendered = _rendered(findings)
    assert "FOUND operator token file" in rendered and "[eerovil]" in rendered
    overlay = [f.location for f in findings if f.kind == "overlay credential file"]
    assert sorted(location.split()[-1] for location in overlay) == [
        "credentials.github_token_file",
        "extensions.chatgpt-nudge.token_file",
    ]
    assert HUMAN not in rendered


def test_ssh_answering_for_github_is_found(tmp_path):
    host = _clean_host()
    host.answers["ssh"] = (
        1, "", "Hi eerovil! You've successfully authenticated, but GitHub does not provide"
        " shell access.\n",
    )

    findings = _doctor(tmp_path, host)

    assert [(f.kind, f.identity) for f in findings] == [("ssh to github.com", "eerovil")]


def test_a_container_carrying_a_human_token_is_found_and_a_bot_token_is_not(tmp_path):
    host = _clean_host()
    host.answers["docker-ps"] = (0, "web\nbazaar-bazaar-1\nbot-box\n", "")
    environments = {
        "web": [f"GH_TOKEN={HUMAN}", "PATH=/bin"],
        "bazaar-bazaar-1": ["GH_TOKEN="],
        "bot-box": [f"GITHUB_TOKEN={BOT}"],
    }
    host.answers["docker-inspect"] = lambda argv: (0, json.dumps(environments[argv[-1]]), "")

    findings = _doctor(tmp_path, host)

    assert [(f.kind, f.location, f.identity) for f in findings] == [
        ("token in container", "container web GH_TOKEN", "eerovil")
    ]


def _reintroduced(tmp_path, monkeypatch):
    """A human token put back into the trailhead env file after the cutover (#2310)."""
    env_file = tmp_path / "home" / "trailhead" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"CURSOR_KEY=x\nGH_TOKEN={HUMAN}\n", encoding="utf-8")
    monkeypatch.setattr(
        github_accounts, "doctor",
        lambda: _doctor(tmp_path, _clean_host(), env_files=(str(env_file),)),
    )


def test_the_doctor_command_fails_on_a_reintroduced_human_token_without_printing_it(
    tmp_path, monkeypatch, capsys
):
    _reintroduced(tmp_path, monkeypatch)

    assert github_accounts.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FOUND token in env file:" in out
    assert "GH_TOKEN" in out and "[eerovil]" in out
    assert "1 human-credential exposure(s) found (enforced)" in out
    assert HUMAN not in out


def test_the_doctor_report_flag_prints_the_same_and_exits_0(tmp_path, monkeypatch, capsys):
    _reintroduced(tmp_path, monkeypatch)

    assert github_accounts.main(["doctor", "--report"]) == 0
    out = capsys.readouterr().out
    assert "FOUND token in env file:" in out
    assert "report mode: nothing enforced" in out
    assert HUMAN not in out


def test_a_clean_host_passes_the_enforced_doctor(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(github_accounts, "doctor", lambda: _doctor(tmp_path, _clean_host()))

    assert github_accounts.main(["doctor"]) == 0
    assert capsys.readouterr().out.strip() == "no human GitHub credential found"


def test_a_process_start_logs_each_finding_as_an_error_without_the_value(
    tmp_path, monkeypatch, caplog
):
    _reintroduced(tmp_path, monkeypatch)

    with caplog.at_level("INFO", logger="agentdeck.github_accounts"):
        github_accounts.check_at_start("web-worker").join(timeout=10)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "web-worker start" in errors[0].getMessage()
    assert "FOUND token in env file:" in errors[0].getMessage()
    assert HUMAN not in caplog.text


def test_a_doctor_that_breaks_at_start_is_logged_not_raised(monkeypatch, caplog):
    def broken():
        raise OSError("no /proc")

    monkeypatch.setattr(github_accounts, "doctor", broken)

    with caplog.at_level("INFO", logger="agentdeck.github_accounts"):
        github_accounts.check_at_start("codex-runtime").join(timeout=10)

    assert "github doctor could not run at codex-runtime start" in caplog.text


# --- show ------------------------------------------------------------------------------------


def test_show_names_each_owners_authority_and_each_pollers(tmp_path, monkeypatch):
    key = tmp_path / "app.pem"
    key.write_text("k", encoding="utf-8")
    key.chmod(0o600)
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[agent]\nlogin = "eerovil-agent"\n'
        '[owners]\neerovil = "agent"\nacmecorp = "app:acmecorp"\n[app]\napp_id = 5\n'
        f'slug = "agentdeck-eerovil"\nprivate_key_file = "{key}"\n'
        "installations = { acmecorp = 9 }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    agent = tmp_path / "agent.env"
    agent.write_text(AGENT, encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_AGENT_GITHUB_ENV", str(agent))
    for name, org in (("bazaar", "acmecorp"), ("stranger", "einarivil")):
        instance = tmp_path / "poller" / "instances" / name
        instance.mkdir(parents=True)
        (instance / "poller.toml").write_text(f'[board]\norg = "{org}"\n', encoding="utf-8")
        (instance / "overlay.toml").write_text(
            '[credentials]\ngithub_token_file = "x"\n', encoding="utf-8"
        )

    lines = github_accounts.show(root=tmp_path / "poller", fetch=_fetch)
    text = "\n".join(lines)

    assert "eerovil -> agent (eerovil-agent)" in text
    assert "acmecorp -> app:acmecorp (agentdeck-eerovil[bot])" in text
    assert "every other owner -> refused" in text
    assert "agent account: eerovil-agent, limit 5000/h, ok" in text
    assert "app: agentdeck-eerovil[bot] (app 5)" in text and ": ok" in text
    assert "bazaar [acmecorp] speaks as app:acmecorp" in text
    assert "stranger [einarivil] REFUSED" in text
    assert "ignored by the engine since #2289" in text
    assert AGENT not in text


def test_show_says_every_owner_is_refused_when_the_file_names_operator(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text('[owners]\neerovil = "operator"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))

    text = "\n".join(github_accounts.show(root=tmp_path / "poller", fetch=_fetch))

    assert "UNUSABLE — every owner is refused" in text


def test_there_is_no_switch_to_the_operator_any_more(capsys):
    with pytest.raises(SystemExit):
        github_accounts.main(["set", "eerovil", "operator"])


# --- prove -----------------------------------------------------------------------------------


def test_prove_checks_each_installations_repositories_board_and_git(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nacmecorp = "app:acmecorp"\n[app]\napp_id = 5\nslug = "agentdeck-eerovil"\n'
        'private_key_file = "k"\ninstallations = { acmecorp = 9 }\n'
        '[checks]\nboards = { acmecorp = 3 }\nrepositories = { acmecorp = "bazaar" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    monkeypatch.setattr(github_authority, "installation_token", lambda app, ident, **_: BOT)

    def fetch(token, url, body):
        assert token == BOT
        if "/installation/repositories" in url:
            repos = [{"full_name": "acmecorp/bazaar"}, {"full_name": "acmecorp/gallery"}]
            return 200, {}, json.dumps({"repositories": repos}).encode()
        nodes = [{"content": {"__typename": "Issue", "number": 5,
                              "repository": {"nameWithOwner": "acmecorp/bazaar"}}}]
        data = {"organization": {"projectV2": {"items": {"nodes": nodes}}}}
        return 200, {}, json.dumps({"data": data}).encode()

    commands = []

    def run(argv, environment):
        commands.append((argv, environment))
        return 0, "abc\trefs/heads/master\n", ""

    ok, lines = github_accounts.prove(fetch=fetch, run=run)

    assert ok, lines
    assert "acmecorp/bazaar, acmecorp/gallery" in lines[0]
    assert "project #3 items (HTTP 200): acmecorp/bazaar#5" in lines[1]
    argv, environment = commands[0]
    assert argv == ["git", "ls-remote", "--heads", "https://github.com/acmecorp/bazaar.git"]
    assert "GH_TOKEN" not in environment
    assert any(value.endswith("agentdeck.git_credential") for value in environment.values())


def test_prove_fails_on_redacted_board_items(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nacmecorp = "app:acmecorp"\n[app]\napp_id = 5\nslug = "s"\n'
        'private_key_file = "k"\ninstallations = { acmecorp = 9 }\n'
        '[checks]\nboards = { acmecorp = 3 }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    monkeypatch.setattr(github_authority, "installation_token", lambda app, ident, **_: BOT)

    def fetch(token, url, body):
        if "/installation/repositories" in url:
            repos = [{"full_name": "acmecorp/bazaar"}]
            return 200, {}, json.dumps({"repositories": repos}).encode()
        nodes = [{"content": {"__typename": "RedactedItem"}}]
        data = {"organization": {"projectV2": {"items": {"nodes": nodes}}}}
        return 200, {}, json.dumps({"data": data}).encode()

    ok, lines = github_accounts.prove(fetch=fetch, run=lambda argv, env: (0, "", ""))

    assert not ok
    assert "REDACTED" in "\n".join(lines)


def test_prove_without_host_checks_reads_only_the_installation(tmp_path, monkeypatch):
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(
        '[owners]\nacmecorp = "app:acmecorp"\n[app]\napp_id = 5\nslug = "s"\n'
        'private_key_file = "k"\ninstallations = { acmecorp = 9 }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    monkeypatch.setattr(github_authority, "installation_token", lambda app, ident, **_: BOT)
    urls, commands = [], []

    def fetch(token, url, body):
        urls.append(url)
        return 200, {}, json.dumps({"repositories": [{"full_name": "acmecorp/bazaar"}]}).encode()

    ok, lines = github_accounts.prove(fetch=fetch, run=lambda argv, env: commands.append(argv))

    assert ok and len(lines) == 1, lines
    assert not any("graphql" in url for url in urls) and commands == []


def test_prove_refuses_without_any_app_installation():
    ok, lines = github_accounts.prove(fetch=_fetch, run=lambda argv, env: (0, "", ""))

    assert not ok and "no [app] installations" in lines[0]


def test_a_unit_setting_a_token_inline_is_found(tmp_path):
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "agentdeck.service").write_text(
        f'[Service]\nEnvironment="GH_TOKEN={HUMAN}" AGENTDECK_AGENT_GITHUB=1\n', encoding="utf-8"
    )

    findings = _doctor(tmp_path, _clean_host())

    assert [(f.kind, f.identity) for f in findings] == [("token in unit file", "eerovil")]
    assert HUMAN not in _rendered(findings)


def test_one_env_file_reached_by_two_spellings_is_one_finding(tmp_path):
    real = tmp_path / "real-home"
    (real / "trailhead").mkdir(parents=True)
    (real / "trailhead" / ".env").write_text(f"GH_TOKEN={HUMAN}\n", encoding="utf-8")
    (tmp_path / "home").symlink_to(real)

    findings = _doctor(
        tmp_path, _clean_host(), env_files=("~/trailhead/.env", str(real / "trailhead" / ".env"))
    )

    assert len(findings) == 1
