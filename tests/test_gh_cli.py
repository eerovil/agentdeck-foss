from __future__ import annotations

import subprocess

import pytest

from agentdeck import gh_cli


def _spawner(monkeypatch, results):
    """Record each spawn's environment and answer from ``results`` in order."""
    calls: list[dict[str, str] | None] = []
    pending = list(results)

    def spawn(argv, environment, timeout, *, stdin=None, keep_stderr=False):
        calls.append(environment)
        code, stdout = pending.pop(0)
        return subprocess.CompletedProcess(list(argv), code, stdout, b"")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    return calls


def _map(tmp_path, monkeypatch, text: str) -> None:
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text(text, encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))


async def test_a_read_runs_once_as_the_owners_authority_and_nothing_else(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "inherited-human-token")
    calls = _spawner(monkeypatch, [(0, b'{"title":"Real title"}')])

    assert await gh_cli.run(["gh", "api", "repos/eerovil/r/issues/1"], timeout=5.0) == (
        0,
        b'{"title":"Real title"}',
    )
    assert len(calls) == 1
    assert calls[0]["GH_TOKEN"] == calls[0]["GITHUB_TOKEN"] == "test-agent-token"
    assert "GH_ENTERPRISE_TOKEN" not in calls[0]
    assert calls[0]["AGENTDECK_GITHUB_AUTHORITY"] == "agent"


async def test_a_failed_read_is_never_retried_as_another_identity(monkeypatch):
    """The old retry on the host's own `gh` login spoke as a person (#2289)."""
    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")
    calls = _spawner(monkeypatch, [(1, b"")])

    assert await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0) == (1, b"")
    assert len(calls) == 1
    assert calls[0]["GH_TOKEN"] == "test-agent-token"


async def test_an_unmapped_owner_is_refused_before_anything_runs(monkeypatch):
    calls = _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal, match="no GitHub authority configured for owner"):
        await gh_cli.run(["gh", "issue", "view", "1", "--repo", "stranger/r"], timeout=5.0)
    assert calls == []


async def test_a_call_with_no_parsable_target_is_refused(monkeypatch):
    calls = _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal, match="github: no target owner for gh api graphql"):
        await gh_cli.run(["gh", "api", "graphql", "-f", "query=q"], timeout=5.0)
    assert calls == []


async def test_an_explicit_owner_places_a_call_the_argv_cannot(monkeypatch):
    calls = _spawner(monkeypatch, [(0, b"{}")])

    assert await gh_cli.run(
        ["gh", "api", "graphql", "-f", "query=q"], timeout=5.0, owner="eerovil"
    ) == (0, b"{}")
    assert calls[0]["GH_TOKEN"] == "test-agent-token"


async def test_a_refusal_is_an_oserror_so_every_caller_already_handles_it(monkeypatch):
    _spawner(monkeypatch, [])

    with pytest.raises(OSError):
        await gh_cli.run(["gh", "pr", "list", "--repo", "acmecorp/bazaar"], timeout=5.0)


async def test_a_missing_agent_credential_refuses_rather_than_falling_back(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENTDECK_AGENT_GITHUB_ENV", str(tmp_path / "missing"))
    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")
    calls = _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal, match="eerovil-agent has no credential"):
        await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)
    assert calls == []


async def test_a_config_naming_operator_refuses_every_owner(tmp_path, monkeypatch):
    _map(tmp_path, monkeypatch, '[owners]\neerovil = "operator"\n')
    calls = _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal, match="operator"):
        await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)
    assert calls == []


async def test_a_write_runs_exactly_once_as_the_owners_authority(monkeypatch):
    calls = _spawner(monkeypatch, [(1, b"")])

    assert await gh_cli.run_once(
        ["gh", "issue", "comment", "1", "--repo", "eerovil/r", "--body-file", "-"],
        timeout=5.0,
        stdin=b"hello",
    ) == (1, b"", b"")
    assert len(calls) == 1
    assert calls[0]["GH_TOKEN"] == "test-agent-token"


async def test_a_write_cannot_choose_its_own_environment():
    """There is no escape hatch to the host login any more (#2289)."""
    assert not hasattr(gh_cli, "environment_without_token")
    with pytest.raises(TypeError):
        await gh_cli.run_once(["gh", "issue", "comment", "1"], timeout=5.0, environment={})


async def test_git_keeps_its_environment_minus_every_inherited_credential(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")
    monkeypatch.setenv("SOME_SETTING", "kept")
    calls = _spawner(monkeypatch, [(0, b"")])

    await gh_cli.run(["/usr/bin/git", "-C", "/x", "fetch", "origin/master"], timeout=5.0)
    assert "GH_TOKEN" not in calls[0]
    assert "AGENTDECK_GITHUB_AUTHORITY" not in calls[0]
    assert calls[0]["SOME_SETTING"] == "kept"


async def test_failures_propagate_to_the_caller(monkeypatch):
    def spawn(*_args, **_kwargs):
        raise OSError("gh vanished")

    monkeypatch.setattr("agentdeck.gh_cli._spawn", spawn)
    with pytest.raises(OSError):
        await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)


async def test_the_credential_is_chosen_off_the_event_loop(monkeypatch):
    """Minting an App token is a network call; the web loop must never wait on it."""
    import threading

    from agentdeck import github_identity

    threads: list[str] = []
    real = github_identity.environment_for

    def recording(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(github_identity, "environment_for", recording)
    _spawner(monkeypatch, [(0, b"")])

    await gh_cli.run(["gh", "pr", "list", "--repo", "eerovil/r"], timeout=5.0)

    assert threads and threads[0].startswith("gh-spawn")


async def test_a_branch_shaped_argument_is_not_read_as_a_repository(monkeypatch):
    calls = _spawner(monkeypatch, [])

    with pytest.raises(gh_cli.GitHubRefusal, match="no target owner"):
        await gh_cli.run(["gh", "pr", "view", "agent/issue-2289"], timeout=5.0)
    assert calls == []


async def test_git_gets_the_agentdeck_helper_as_its_only_credential(monkeypatch):
    calls = _spawner(monkeypatch, [(0, b"")])

    await gh_cli.run(["git", "-C", "/x", "fetch", "origin"], timeout=5.0)

    values = [
        calls[0][f"GIT_CONFIG_VALUE_{i}"] for i in range(int(calls[0]["GIT_CONFIG_COUNT"]))
    ]
    assert any(value.endswith("-m agentdeck.git_credential") for value in values)
