from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from agentdeck.gh_shim import install, limits, main
from agentdeck.gh_shim.classify import classify, credential_identity
from agentdeck.github_cache import GitHubLookup, GitHubMetadataCache
from agentdeck.poller_engine import rate_limits

PLENTY = json.dumps({"resources": {"core": {"remaining": 4000}, "graphql": {"remaining": 4000}}})
NEARLY_GONE = json.dumps(
    {"resources": {"core": {"remaining": 600}, "graphql": {"remaining": 4000}}}
)


class FakeGh:
    """Stands in for the real binary, recording what was actually asked of it."""

    def __init__(self, answer="ISSUE BODY", code=0, rate_limit=PLENTY):
        self.answer = answer
        self.code = code
        self.rate_limit = rate_limit
        self.calls: list[list[str]] = []
        # A label the repository lacks, as `gh issue create` refuses it before writing.
        self.refuse_labels = False

    def __call__(self, argv, environment, *, stderr_seen=None, kind="", quiet=False):
        self.calls.append(list(argv))
        if self.refuse_labels and "--label" in argv:
            if stderr_seen is not None:
                stderr_seen.append("could not add label: 'agent' not found\n")
            return (1, "")
        if argv[:2] == ["api", "user"]:
            return (0, '{"id":20142828}')
        if argv[:2] == ["api", "installation/repositories?per_page=1"]:
            return (0, '{"total_count":1}')
        if argv[:2] == ["api", "rate_limit"]:
            return (0, self.rate_limit)
        if argv and argv[0] == "api" and "--include" in argv:
            resource = "graphql" if "graphql" in argv else "core"
            payload = json.loads(self.rate_limit)
            remaining = payload["resources"][resource]["remaining"]
            return (
                0,
                f"x-ratelimit-resource: {resource}\n"
                f"x-ratelimit-remaining: {remaining}\n\n{{}}",
            )
        return (self.code, self.answer)

    @property
    def reads(self) -> list[list[str]]:
        probes = (
            ["api", "rate_limit"],
            ["api", "user"],
            ["api", "installation/repositories?per_page=1"],
        )
        return [
            call
            for call in self.calls
            if call[:2] not in probes and "--include" not in call
        ]


# Every variable the shim reads, so no test in this file inherits one from the host it runs on.
# The leak that made this necessary: a deck-owned worker carries `AGENTDECK_GH_CONSUMER`, and
# `session_identity` prefers it over the session key — so the two-sessions test set a session key
# that was silently ignored, both halves spent from one bucket, and the second read was refused.
# It passed in CI, whose environment is bare, and failed for exactly the agents validating changes
# on this host (#1112). The floors and the budget have the same reach; they are just not set here.
_SHIM_ENVIRONMENT = (
    "AGENTDECK_GH_CONSUMER",
    "AGENTDECK_GH_CORE_FLOOR",
    "AGENTDECK_GH_FETCH_BUDGET",
    "AGENTDECK_GH_FRESH",
    "AGENTDECK_GH_GRAPHQL_FLOOR",
    "AGENTDECK_GH_SHIM_ACTIVE",
    "AGENTDECK_GH_SHIM_DIR",
    "AGENTDECK_GITHUB_CACHE",
    "AGENTDECK_NO_OWNER_LABEL",
    "AGENTDECK_POLLER_ROOT",
    "AGENTDECK_SESSION_KEY",
    # Read by `gh issue create` to tell the deck it filed one: never this host's live deck.
    "AGENTDECK_URL",
)


@pytest.fixture(autouse=True)
def _no_inherited_shim_environment(monkeypatch):
    for name in _SHIM_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def shim(tmp_path, monkeypatch):
    # The fixtures' owner `o` speaks as the agent account (#2289): an owner the map does not
    # name is refused before the real `gh` is ever reached.
    accounts = tmp_path / "github-authority.toml"
    accounts.write_text('[owners]\no = "agent"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_GITHUB_AUTHORITY_FILE", str(accounts))
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(tmp_path / "shim"))
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "acct:session")
    # An empty poller root, so `gh issue create` never reads this host's real manifests.
    monkeypatch.setenv("AGENTDECK_POLLER_ROOT", str(tmp_path / "poller"))
    monkeypatch.setattr(
        "agentdeck.gh_shim.classify.repo_identity", lambda **_: "git@github.com:o/r.git"
    )
    gh = FakeGh()
    monkeypatch.setattr(main, "_run_real", gh)
    return gh


def test_the_same_read_twice_costs_one_api_call(shim, capsys):
    first = main.main(["issue", "view", "814"])
    second = main.main(["issue", "view", "814"])

    assert (first, second) == (0, 0)
    assert len(shim.reads) == 1
    assert capsys.readouterr().out == "ISSUE BODY" * 2


def test_a_worker_can_demand_its_own_write_back(shim, monkeypatch):
    main.main(["issue", "view", "814"])
    shim.answer = "EDITED BODY"
    monkeypatch.setenv("AGENTDECK_GH_FRESH", "1")

    assert main.main(["issue", "view", "814"]) == 0
    assert len(shim.reads) == 2


def test_the_fresh_flag_is_stripped_before_gh_sees_it(shim):
    main.main(["issue", "view", "814", main.FRESH_FLAG])

    assert shim.reads == [["issue", "view", "814"]]


def test_a_write_passes_through_and_makes_its_reads_stale(shim, capsys):
    main.main(["issue", "view", "814"])
    shim.answer = "COMMENTED"
    assert main.main(["issue", "comment", "814", "--body", "hi"]) == 0
    shim.answer = "NEW BODY"
    main.main(["issue", "view", "814"])

    assert capsys.readouterr().out.endswith("NEW BODY")
    assert len(shim.reads) == 3  # first read, the write itself, the re-read


def test_github_not_the_poller_lease_decides_whether_a_worker_write_succeeds(shim):
    """#2152: an ordinary comment or follow-up issue from a poller worker is not gated on its
    lease. The shim passes it through, and GitHub's own answer — a refusal included — is the
    worker's answer."""
    assert main.main(["issue", "comment", "814", "--body", "documentary"]) == 0
    assert main.main(["issue", "create", "--title", "Follow-up", "--body", "From #814"]) == 0

    shim.code, shim.answer = 1, "HTTP 403: Resource not accessible by integration"
    assert main.main(["issue", "comment", "814", "--body", "denied"]) == 1
    assert ["issue", "comment", "814", "--body", "denied"] in shim.calls


def test_below_the_floor_a_cached_answer_is_served_as_stale(shim, capsys, monkeypatch):
    main.main(["issue", "view", "814"])
    monkeypatch.setattr(limits, "LIMITS_TTL_S", 0.0)
    shim.rate_limit = NEARLY_GONE
    shim.answer = "NEVER FETCHED"

    code = main.main(["issue", "view", "814", main.FRESH_FLAG])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out.endswith("ISSUE BODY")
    assert "may be stale" in captured.err
    assert "600" in captured.err
    assert "untrusted /rate_limit cold-start estimate" in captured.err


def test_below_the_floor_with_nothing_cached_fails_loudly(shim, capsys):
    shim.rate_limit = NEARLY_GONE

    code = main.main(["issue", "view", "999"])
    captured = capsys.readouterr()

    assert code == main.EXIT_REFUSED
    assert captured.out == ""
    assert "unknown, not empty" in captured.err
    assert shim.reads == []


def test_real_response_headers_override_the_rate_limit_payload(shim, capsys):
    # The shim keys its quota on the authority it chose for `o/r`, the agent account.
    environment = {"GH_TOKEN": "test-agent-token", "AGENTDECK_GITHUB_AUTHORITY": "agent"}
    credential = credential_identity(environment)
    store = rate_limits.RateLimitStore()
    store.bind(credential, "20142828")
    store.record_headers(
        credential,
        (
            rate_limits.Reading(
                resource="core", remaining=0, used=5000, limit=5000
            ),
        ),
    )

    code = main.main(["issue", "view", "999"])
    captured = capsys.readouterr()

    assert code == main.EXIT_REFUSED
    assert shim.reads == []
    assert "response headers from a real GitHub call" in captured.err


def test_api_debug_trace_is_removed_while_rate_headers_are_kept():
    debug = """ordinary warning before
[git remote -v]
[git config --get-regexp ^remote\\..*\\.gh-resolved$]
* Request at now
> Authorization: token secret
< HTTP/2.0 403 Forbidden
< X-RateLimit-Limit: 5000
< X-RateLimit-Remaining: 0
< X-RateLimit-Used: 5000
< X-RateLimit-Resource: core
* Request took 10ms
gh: API rate limit exceeded (HTTP 403)
"""

    clean, readings = rate_limits.strip_api_trace(debug, now=123.0)

    assert clean == "ordinary warning before\ngh: API rate limit exceeded (HTTP 403)\n"
    assert readings == (
        rate_limits.Reading(
            resource="core",
            remaining=0,
            used=5000,
            limit=5000,
            observed_at=123.0,
        ),
    )
    assert "secret" not in clean


def test_two_credentials_bound_to_one_user_share_the_real_reading(tmp_path):
    store = rate_limits.RateLimitStore(tmp_path / "quota.sqlite3")
    store.bind("token-a", "20142828", now=100.0)
    store.record_headers(
        "token-a",
        (rate_limits.Reading(resource="core", remaining=17, observed_at=100.0),),
    )
    store.bind("token-b", "20142828", now=101.0)

    shared = store.read("token-b", now=101.0)
    assert shared["core"].remaining == 17
    assert shared["core"].source == rate_limits.HEADER_SOURCE


def test_cold_start_estimate_replaces_only_a_stale_real_reading(tmp_path):
    store = rate_limits.RateLimitStore(tmp_path / "quota.sqlite3")
    store.record_headers(
        "token",
        (rate_limits.Reading(resource="graphql", remaining=10, observed_at=100.0),),
    )
    payload = {"resources": {"graphql": {"remaining": 4000}}}

    store.record_estimate("token", payload, now=110.0)
    assert store.read("token", now=110.0)["graphql"].remaining == 10

    store.record_estimate("token", payload, now=131.0)
    reading = store.read("token", now=131.0)["graphql"]
    assert reading.remaining == 4000
    assert reading.source == rate_limits.ESTIMATE_SOURCE


def test_the_hosts_own_shim_environment_never_reaches_a_test():
    """#1112: a deck-owned worker's `AGENTDECK_GH_CONSUMER` silently decided a test's outcome.

    Guards the isolation itself, so removing the autouse fixture fails here rather than in
    whichever test happens to read the variable the host has set.
    """
    assert [name for name in _SHIM_ENVIRONMENT if name in os.environ] == []


def test_an_exhausted_session_bucket_stops_that_worker_only(shim, monkeypatch, capsys):
    monkeypatch.setenv("AGENTDECK_GH_FETCH_BUDGET", "2")

    assert main.main(["issue", "view", "1"]) == 0
    assert main.main(["issue", "view", "2"]) == 0
    refused = main.main(["issue", "view", "3"])
    assert refused == main.EXIT_REFUSED
    assert "already made 2 live GitHub reads" in capsys.readouterr().err

    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "acct:other-session")
    assert main.main(["issue", "view", "3"]) == 0


def test_a_watch_loop_is_refused_rather_than_made_cheap(shim, capsys):
    code = main.main(["pr", "checks", "--watch"])

    assert code == main.EXIT_REFUSED
    assert shim.calls == []
    assert "Watching is polling" in capsys.readouterr().err


def test_a_failed_read_is_not_cached_and_its_retry_makes_its_own_call(shim, capsys):
    """A failure is never pinned as an answer, and a retry inside its back-off still asks.

    The back-off used to refuse the retry outright as "GitHub did not answer", which this
    caller had never asked (#3344). The quota floor and the session's hourly budget are what
    protect quota, and both have already passed the retry, so it makes its own one call.
    """
    shim.code, shim.answer = 1, ""
    assert main.main(["issue", "view", "814"]) == 1
    shim.code, shim.answer = 0, "ISSUE BODY"

    assert main.main(["issue", "view", "814"]) == 0
    assert capsys.readouterr().out == "ISSUE BODY"
    assert len(shim.reads) == 2


def _leased_cache(monkeypatch, after_wait):
    """Every resolve finds the key leased; waiting on it gives ``after_wait``."""

    async def leased(self, key, fetch, **_):
        return GitHubLookup(None, False, reason="leased")

    monkeypatch.setattr(GitHubMetadataCache, "resolve", leased)
    monkeypatch.setattr(GitHubMetadataCache, "wait_for", lambda self, key, timeout_s: after_wait)


def test_a_read_another_process_is_fetching_waits_for_its_answer(shim, monkeypatch, capsys):
    """Leased with nothing cached: the other fetch's answer is served, no call made (#3344)."""
    _leased_cache(monkeypatch, GitHubLookup({"code": 0, "stdout": "THEIRS"}, True))

    assert main.main(["issue", "view", "814"]) == 0
    assert capsys.readouterr().out == "THEIRS"
    assert shim.reads == []


def test_a_read_still_leased_after_waiting_is_refused_as_github_not_quota(
    shim, tmp_path, monkeypatch, capsys
):
    _leased_cache(monkeypatch, GitHubLookup(None, False, reason="leased"))

    assert main.main(["issue", "view", "814"]) == main.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "still running after 15 s" in err
    assert "quota is fine" in err and "wait for quota" not in err
    logged = [
        json.loads(line)
        for line in (tmp_path / main.FAILURE_LOG_NAME).read_text().splitlines()
    ]
    assert [(entry["kind"], entry["cause"]) for entry in logged] == [("refused", "github")]


def test_a_pending_pr_checks_keeps_exit_8_and_is_cached(shim, capsys):
    """Exit 8 with output is an answer, and it is the read workers loop on.

    `_CHECKS_PENDING` in poller_engine/github.py is load-bearing, so the code
    travels with stdout; caching it is what makes the loop cost nothing.
    """
    shim.code, shim.answer = 8, "still running"

    assert main.main(["pr", "checks"]) == 8
    assert main.main(["pr", "checks"]) == 8
    assert capsys.readouterr().out == "still running" * 2
    assert len(shim.reads) == 1


def test_a_shim_launched_gh_never_wraps_itself_again(shim, monkeypatch):
    monkeypatch.setenv(install.RECURSION_GUARD, "1")

    assert main.main(["issue", "view", "814"]) == 0
    assert shim.reads == [["issue", "view", "814"]]


def test_the_shim_installs_itself_first_on_a_workers_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(tmp_path / "shim"))
    environment = {"PATH": "/usr/bin:/bin"}

    path = install.worker_path(environment)

    assert path.split(os.pathsep)[0] == str(tmp_path / "shim")
    assert path.split(os.pathsep)[1:] == ["/usr/bin", "/bin"]
    generated = tmp_path / "shim" / "gh"
    assert os.access(generated, os.X_OK)
    assert "agentdeck.gh_shim" in generated.read_text()


def test_installing_twice_does_not_stack_the_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(tmp_path / "shim"))
    once = install.worker_path({"PATH": "/usr/bin"})

    assert install.worker_path({"PATH": once}) == once


def test_installing_from_a_worktree_uses_the_stable_checkout_interpreter(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "agentdeck"
    worktree = checkout / ".worktrees" / "issue-926"
    module = worktree / "src" / "agentdeck" / "gh_shim" / "install.py"
    (worktree / "pyproject.toml").parent.mkdir(parents=True)
    (worktree / "pyproject.toml").touch()
    stable_python = checkout / ".venv" / "bin" / "python"
    stable_python.parent.mkdir(parents=True)
    stable_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n")
    stable_python.chmod(0o755)
    worktree_python = worktree / ".venv" / "bin" / "python"
    monkeypatch.setattr(install, "__file__", str(module))
    monkeypatch.setattr(install.sys, "executable", str(worktree_python))

    directory = install.ensure_shim(tmp_path / "shim")

    assert directory == tmp_path / "shim"
    launcher = (directory / "gh").read_text()
    assert str(stable_python) in launcher
    assert str(worktree_python) not in launcher
    shutil.rmtree(worktree)
    result = subprocess.run(
        [directory / "gh", "api", "user"], check=True, capture_output=True, text=True
    )
    assert result.stdout == "-m agentdeck.gh_shim api user\n"


def test_a_worktree_interpreter_is_never_written_without_a_stable_venv(
    tmp_path, monkeypatch
):
    checkout = tmp_path / "agentdeck"
    worktree = checkout / ".worktrees" / "issue-926"
    module = worktree / "src" / "agentdeck" / "gh_shim" / "install.py"
    (worktree / "pyproject.toml").parent.mkdir(parents=True)
    (worktree / "pyproject.toml").touch()
    worktree_python = worktree / ".venv" / "bin" / "python"
    monkeypatch.setattr(install, "__file__", str(module))
    monkeypatch.setattr(install.sys, "executable", str(worktree_python))

    directory = install.ensure_shim(tmp_path / "shim")

    assert directory is None
    assert not (tmp_path / "shim" / "gh").exists()


def test_the_real_gh_is_found_past_the_shims_own_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTDECK_GH_SHIM_DIR", str(tmp_path / "shim"))
    install.ensure_shim()
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    genuine = elsewhere / "gh"
    genuine.write_text("#!/bin/sh\n")
    genuine.chmod(0o755)

    found = install.real_gh(
        {"PATH": os.pathsep.join([str(tmp_path / "shim"), str(elsewhere)])}
    )

    assert found == str(genuine)


def test_an_unreadable_quota_is_unknown_and_does_not_fence_the_worker():
    assert limits.RateLimits().below_floor() == ""
    assert limits.parse_rate_limit_headers("nonsense") == limits.RateLimits()


def test_real_response_headers_report_the_budget_github_enforced():
    parsed = limits.parse_rate_limit_headers(
        "HTTP/2 200\nX-RateLimit-Resource: core\nX-RateLimit-Remaining: 0\n\n{}",
        "x-ratelimit-resource: graphql\nx-ratelimit-remaining: 321\n",
    )

    assert parsed == limits.RateLimits(
        core=0,
        graphql=321,
        core_source=rate_limits.HEADER_SOURCE,
        graphql_source=rate_limits.HEADER_SOURCE,
    )


def test_real_calls_are_counted_by_consumer_and_kind(shim, tmp_path, monkeypatch):
    monkeypatch.setenv(limits.CONSUMER_ENV, "claude_code:main:issue-914")
    main.main(["issue", "view", "914"])
    main.main(["issue", "comment", "914", "--body", "done"])

    accounting = limits.CallAccounting(tmp_path / "github.sqlite3")
    assert accounting.totals(since=0) == [
        ("claude_code:main:issue-914", "quota-probe", 2),
        ("claude_code:main:issue-914", "read", 1),
        ("claude_code:main:issue-914", "write", 1),
    ]


def test_named_consumer_is_preferred_to_session_and_process_group(monkeypatch):
    monkeypatch.setenv(limits.CONSUMER_ENV, "codex:main")
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "wrong-session")

    assert limits.session_identity() == "codex:main"


def test_gh_run_watch_is_refused_even_though_it_carries_no_watch_flag(shim, capsys):
    """`run watch` *is* the wait, with no flag to spot it by.

    It is not a cacheable read, so without naming it outright it would fall to
    the passthrough branch and run as an unbounded, ungated poll.
    """
    assert main.main(["run", "watch", "123"]) == main.EXIT_REFUSED
    assert shim.calls == []
    assert "poll, not a read" in capsys.readouterr().err


def test_a_graphql_query_is_bounded_but_a_graphql_mutation_passes_through(shim):
    query = "query=query{repository(name:\"r\"){id}}"
    assert main.main(["api", "graphql", "-f", query]) == 0
    assert main.main(["api", "graphql", "-f", query]) == 0
    assert len(shim.reads) == 1  # gated and cached, not waved through as a write

    mutation = "query=mutation{addComment(input:{}){id}}"
    assert main.main(["api", "graphql", "-f", mutation]) == 0
    assert main.main(["api", "graphql", "-f", mutation]) == 0
    assert len(shim.reads) == 3  # every mutation reaches GitHub


def test_a_flag_value_is_never_mistaken_for_the_subcommand(shim):
    decision = classify(
        ["--repo", "o/x", "pr", "view", "5"],
        remote="git@github.com:o/r.git",
        environment={"GH_TOKEN": "t"},
    )

    assert (decision.group, decision.verb) == ("pr", "view")


# --- one explicit authority per call, and nothing to fall back to (#2289) ---------------------


class RecordingGh(FakeGh):
    """A fake `gh` that also records the credential each call carried."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.environments: list[dict[str, str]] = []

    def __call__(self, argv, environment, **kwargs):
        self.environments.append(dict(environment))
        return super().__call__(argv, environment, **kwargs)


def test_a_call_about_an_unmapped_owner_is_refused_and_nothing_runs(shim, monkeypatch, capsys):
    monkeypatch.setattr(
        "agentdeck.gh_shim.classify.repo_identity", lambda **_: "git@github.com:stranger/r.git"
    )
    monkeypatch.setenv("GH_TOKEN", "inherited-human-token")

    assert main.main(["issue", "view", "814"]) == main.EXIT_NO_AUTHORITY
    err = capsys.readouterr().err
    assert "no GitHub authority configured for owner stranger" in err
    assert "no other identity is tried" in err
    assert shim.calls == []


def test_a_call_with_no_target_owner_is_refused(shim, monkeypatch, capsys):
    monkeypatch.setattr("agentdeck.gh_shim.classify.repo_identity", lambda **_: "")

    assert main.main(["api", "graphql", "-f", "query={viewer{login}}"]) == 77
    assert "no target owner for gh api graphql" in capsys.readouterr().err
    assert shim.calls == []


def test_the_owners_authority_is_the_only_credential_the_real_gh_sees(tmp_path, shim, monkeypatch):
    recording = RecordingGh()
    monkeypatch.setattr(main, "_run_real", recording)
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(name, "inherited-human-token")

    assert main.main(["issue", "comment", "814", "--body", "hi"]) == 0
    environment = recording.environments[-1]
    assert environment["GH_TOKEN"] == environment["GITHUB_TOKEN"] == "test-agent-token"
    assert "GH_ENTERPRISE_TOKEN" not in environment
    assert environment["AGENTDECK_GITHUB_AUTHORITY"] == "agent"


def test_a_failed_read_is_never_retried_under_another_identity(shim, capsys):
    shim.code, shim.answer = 1, ""

    assert main.main(["issue", "view", "814"]) == 1
    assert len(shim.reads) == 1
    assert "operator" not in capsys.readouterr().err


def test_cache_and_quota_identity_follow_the_authority_not_the_token():
    """An App installation token rotates hourly; its cache and quota must not reset with it."""
    first = {"GH_TOKEN": "ghs_one", "AGENTDECK_GITHUB_AUTHORITY": "app:acmecorp"}
    rotated = {"GH_TOKEN": "ghs_two", "AGENTDECK_GITHUB_AUTHORITY": "app:acmecorp"}
    other = {"GH_TOKEN": "ghs_one", "AGENTDECK_GITHUB_AUTHORITY": "app:northwind"}

    assert credential_identity(first) == credential_identity(rotated)
    assert credential_identity(first) != credential_identity(other)
    assert credential_identity(first) != credential_identity({"GH_TOKEN": "ghs_one"})


def test_an_app_authority_probes_its_installation_never_user(tmp_path, monkeypatch):
    """An installation token gets a 403 from `GET /user`; the probe asks what it can read."""
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    gh = FakeGh()
    monkeypatch.setattr(main, "_run_real", gh)
    environment = {"GH_TOKEN": "ghs_one", "AGENTDECK_GITHUB_AUTHORITY": "app:acmecorp"}

    main._quota(GitHubMetadataCache(), environment)

    assert ["api", "user"] not in gh.calls
    assert ["api", "installation/repositories?per_page=1"] in gh.calls


def test_a_failed_quota_probe_is_logged_and_never_shown(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho 'gh: Resource not accessible (HTTP 403)' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(main, "real_gh", lambda environment: str(fake))

    code, _ = main._run_real(["api", "user"], {}, kind="quota-probe", quiet=True)

    assert code == 1
    assert capsys.readouterr().err == ""
    logged = json.loads((tmp_path / main.FAILURE_LOG_NAME).read_text().splitlines()[-1])
    assert (logged["kind"], logged["command"]) == ("quota-probe", "api user")


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("gh", 120), OSError("exec format error")]
)
def test_a_probe_that_never_ran_is_logged_and_never_shown(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "github.sqlite3"))
    monkeypatch.setattr(main, "real_gh", lambda environment: "/bin/gh")

    def fails(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(main.subprocess, "run", fails)

    code, _ = main._run_real(["api", "user"], {}, kind="quota-probe", quiet=True)

    assert code in (124, main.EXIT_NO_GH)
    assert capsys.readouterr().err == ""
    logged = json.loads((tmp_path / main.FAILURE_LOG_NAME).read_text().splitlines()[-1])
    assert logged["kind"] == "quota-probe" and "gh" in logged["reason"]


def test_an_app_credential_is_bound_to_its_installation_without_asking_github(tmp_path):
    store = rate_limits.RateLimitStore(tmp_path / "limits.sqlite3")
    environment = {"GH_TOKEN": "ghs_one", "AGENTDECK_GITHUB_AUTHORITY": "app:acmecorp"}
    credential = credential_identity(environment)

    rate_limits.bind_authority(store, environment, credential)

    assert store.is_bound(credential)


def _managed(tmp_path, manifest='ownership_label = ["agent", "claude"]'):
    """Install one poller instance managing `o/r` under the fixture's poller root."""
    instance = tmp_path / "poller" / "instances" / "proj"
    instance.mkdir(parents=True)
    (instance / "poller.toml").write_text(
        f'[board]\norg = "o"\n{manifest}\n\n[[repos]]\nid = "r"\nholds_issues = true\n',
        encoding="utf-8",
    )


def test_an_issue_filed_in_a_managed_repository_gains_its_ownership_label(
    shim, tmp_path, capsys
):
    """Agent-filed issues were left unlabelled and forgotten (#2505)."""
    _managed(tmp_path)
    shim.answer = "https://github.com/o/r/issues/9\n"

    assert main.main(["issue", "create", "--title", "t", "--body", "b"]) == 0

    assert shim.reads == [["issue", "create", "--title", "t", "--body", "b", "--label", "agent"]]
    captured = capsys.readouterr()
    assert captured.out == "https://github.com/o/r/issues/9\n"
    assert "added label 'agent'" in captured.err
    assert "AGENTDECK_NO_OWNER_LABEL=1" in captured.err


def test_a_filed_issue_is_told_to_the_deck_so_its_work_starts_now(
    shim, tmp_path, monkeypatch, capsys
):
    """GitHub held the new issue's webhook back and the Work waited (#3371)."""
    _managed(tmp_path)
    shim.answer = "https://github.com/o/r/issues/9\n"
    told: list[tuple[str, dict]] = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"starting": true}'

    def urlopen(request, timeout):
        told.append((request.full_url, json.loads(request.data)))
        return Answer()

    monkeypatch.setattr(main.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("AGENTDECK_URL", "http://127.0.0.1:9/")

    assert main.main(["issue", "create", "--title", "t", "-l", "Bug"]) == 0

    assert told == [
        (
            "http://127.0.0.1:9/api/works/filed",
            {"issue": "https://github.com/o/r/issues/9"},
        )
    ]
    assert "the deck is starting the Work" in capsys.readouterr().err

    # A failed filing tells the deck nothing, and a deck that does not answer fails nothing.
    shim.code, shim.answer = 1, ""
    assert main.main(["issue", "create", "--title", "t"]) == 1
    assert len(told) == 1
    shim.code, shim.answer = 0, "https://github.com/o/r/issues/10\n"

    def down(request, timeout):
        raise OSError("down")

    monkeypatch.setattr(main.urllib.request, "urlopen", down)
    assert main.main(["issue", "create", "--title", "t"]) == 0
    assert "could not tell the deck" in capsys.readouterr().err


def test_the_opt_out_files_the_issue_as_written(shim, tmp_path, monkeypatch, capsys):
    _managed(tmp_path)
    monkeypatch.setenv("AGENTDECK_NO_OWNER_LABEL", "1")

    assert main.main(["issue", "create", "--title", "t"]) == 0

    assert shim.reads == [["issue", "create", "--title", "t"]]
    assert "added label" not in capsys.readouterr().err


def test_an_issue_already_handed_over_by_an_alias_is_left_alone(shim, tmp_path):
    _managed(tmp_path)

    assert main.main(["issue", "create", "--title", "t", "-lClaude"]) == 0

    assert shim.reads == [["issue", "create", "--title", "t", "-lClaude"]]


def test_an_unmanaged_repository_is_left_alone(shim):
    assert main.main(["issue", "create", "--title", "t", "-R", "o/other"]) == 0

    assert shim.reads == [["issue", "create", "--title", "t", "-R", "o/other"]]


def test_an_unreadable_manifest_still_files_the_issue(shim, tmp_path, monkeypatch, capsys):
    """The label is a default, never a gate: a write is never blocked."""
    _managed(tmp_path)

    def broken(_root):
        raise RuntimeError("boom")

    monkeypatch.setattr("agentdeck.poller_ops.issue_label_targets", broken)

    assert main.main(["issue", "create", "--title", "t"]) == 0

    assert shim.reads == [["issue", "create", "--title", "t"]]
    assert "could not read the poller manifests (boom)" in capsys.readouterr().err


def test_a_label_the_repository_lacks_is_dropped_once(shim, tmp_path, capsys):
    """GitHub refuses an unknown label before creating anything, so filing again without it
    cannot duplicate the issue. Any other failure is never retried."""
    _managed(tmp_path)
    shim.refuse_labels = True

    assert main.main(["issue", "create", "--title", "t"]) == 0

    assert shim.reads == [
        ["issue", "create", "--title", "t", "--label", "agent"],
        ["issue", "create", "--title", "t"],
    ]
    assert "filing again without 'agent'" in capsys.readouterr().err


def test_any_other_failure_of_a_labelled_filing_is_not_retried(shim, tmp_path):
    _managed(tmp_path)
    shim.code = 1
    shim.answer = ""

    assert main.main(["issue", "create", "--title", "t"]) == 1

    assert shim.reads == [["issue", "create", "--title", "t", "--label", "agent"]]


def test_a_read_never_opens_the_manifests(shim, tmp_path, monkeypatch):
    _managed(tmp_path)

    def unexpected(_root):
        raise AssertionError("a read must not read the poller manifests")

    monkeypatch.setattr("agentdeck.poller_ops.issue_label_targets", unexpected)

    assert main.main(["issue", "view", "814"]) == 0


# --- what a failed call reports (#2714) ------------------------------------------------------

_FAILED_COMMENT_TRACE = """\
* Request at 2026-10-07 12:00:00
* Request to https://api.github.com/repos/o/r/issues/314/comments
> POST /repos/o/r/issues/314/comments HTTP/1.1
> Content-Type: application/json; charset=utf-8

< HTTP/2.0 502 Bad Gateway
< X-Accepted-Github-Permissions: issues=write; pull_requests=write
< X-Github-Request-Id: AAAA:BBBB

* Request took 812ms
unexpected end of JSON input
"""


@pytest.fixture
def failing_gh(tmp_path, monkeypatch):
    """A real `gh` stand-in on PATH that prints a 502 trace and exits 1."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir()
    trace = tmp_path / "trace.txt"
    trace.write_text(_FAILED_COMMENT_TRACE, encoding="utf-8")
    binary.write_text(f"#!/bin/sh\ncat {trace} >&2\nexit 1\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("AGENTDECK_GITHUB_CACHE", str(tmp_path / "cache" / "github.sqlite3"))
    monkeypatch.setattr(main, "repo_identity", lambda **_: "git@github.com:o/r.git")
    return {
        "PATH": f"{binary.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_TOKEN": "ghs_secret_token",
        "AGENTDECK_GITHUB_AUTHORITY": "app:o",
    }


def test_a_failed_write_says_what_github_answered_and_never_blames_a_permission(
    failing_gh, tmp_path, capsys
):
    argv = ["issue", "comment", "314", "--body", "private words"]
    code, _stdout = main._run_real(argv, failing_gh, kind="write")

    err = capsys.readouterr().err
    assert code == 1
    assert "lacks a permission" not in err
    assert "POST /repos/o/r/issues/314/comments -> HTTP 502, empty body" in err
    assert "request AAAA:BBBB" in err
    assert "spoke as app:o" in err
    log = tmp_path / "cache" / main.FAILURE_LOG_NAME
    record = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert record["command"] == "issue comment"
    assert record["kind"] == "write"
    assert "o/r" in record["target"]
    assert record["authority"] == "app:o"
    assert record["responses"][0]["status"] == 502
    assert record["token_digest"] and len(record["token_digest"]) == 12
    raw = log.read_text(encoding="utf-8")
    assert "private words" not in raw
    assert "ghs_secret_token" not in raw


def test_comments_off_a_terminal_come_after_the_issue_body(shim, monkeypatch, capsys):
    # Off a terminal the real `gh … --comments` prints no body (#3584).
    real = main._run_real

    def answer(argv, environment, **kwargs):
        code, out = real(argv, environment, **kwargs)
        return (code, "COMMENTS" if {"--comments", "-c"} & set(argv) else out)

    monkeypatch.setattr(main, "_run_real", answer)

    assert main.main(["issue", "view", "814", "--comments"]) == 0
    assert capsys.readouterr().out == "ISSUE BODY\n--- comments ---\nCOMMENTS"
    assert shim.reads == [["issue", "view", "814"], ["issue", "view", "814", "--comments"]]

    assert main.main(["issue", "view", "815", "--comments", "--json", "comments"]) == 0
    assert shim.reads[2:] == [["issue", "view", "815", "--comments", "--json", "comments"]]
    capsys.readouterr()

    # A global flag in front still names the same subcommand.
    assert main.main(["-R", "o/r", "issue", "view", "816", "-c"]) == 0
    assert capsys.readouterr().out == "ISSUE BODY\n--- comments ---\nCOMMENTS"
    assert shim.reads[3:] == [
        ["-R", "o/r", "issue", "view", "816"],
        ["-R", "o/r", "issue", "view", "816", "-c"],
    ]


def test_a_failed_comments_read_prints_no_body_as_if_whole(shim, monkeypatch, capsys):
    real = main._run_real

    def answer(argv, environment, **kwargs):
        if "--comments" in argv:
            return (1, "")
        return real(argv, environment, **kwargs)

    monkeypatch.setattr(main, "_run_real", answer)

    assert main.main(["issue", "view", "814", "--comments"]) == 1
    assert capsys.readouterr().out == ""
