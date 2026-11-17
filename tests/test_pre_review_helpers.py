"""``agentdeck pre-review-helpers``: three Haiku helpers before a first review (#4437)."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from agentdeck import pre_review_helpers as helpers
from agentdeck.pre_review_helpers import NotRun, Outcome, Pull


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path):
    """An ``origin`` with ``master`` at b2, and a checkout whose branch left master at b1."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "master")
    b1 = commit(origin, "one")
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    git(checkout, "checkout", "-q", "-b", "feature")
    head = commit(checkout, "change")
    b2 = commit(origin, "two")
    return origin, checkout, b1, b2, head


def pull(**fields) -> Pull:
    values = dict(
        repository="o/r",
        number=9,
        title="t",
        body="Refs #7",
        base_ref="master",
        base_oid="",
        head_oid="",
    )
    values.update(fields)
    return Pull(**values)


def test_each_helper_runs_isolated_and_bounded():
    """The design review's isolation: its own 5-minute ``timeout``, no transcript, no user
    settings or hooks, no MCP, sandboxed Bash with no network, no edits or web tools."""
    argv = helpers.helper_command(repo=Path("/var/tmp/x/repo"))
    assert argv[:4] == ["timeout", "-k", "10", "300"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert {"--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"} <= set(
        argv
    )
    assert argv[argv.index("--setting-sources") + 1] == ""
    sandbox = json.loads(argv[argv.index("--settings") + 1])["sandbox"]
    assert sandbox["enabled"] and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == ["none.invalid"]
    denied = argv[argv.index("--disallowedTools") + 1 :]
    assert {"Edit", "Write", "WebFetch", "WebSearch", "Agent"} <= set(denied)
    assert "bypassPermissions" not in argv
    # Only Bash is allowed outright: Read keeps its default, the clone only (a read elsewhere
    # asks, and ``-p`` refuses), and neither Bash nor Read sees the home folder.
    assert argv[argv.index("--allowedTools") + 1 : argv.index("--disallowedTools")] == ["Bash"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    # #4437 reviews: of the shared temporary folders Bash reads only its own clone (checked
    # live), so no other run's clone or chat's scratch file.
    assert settings["permissions"]["deny"] == ["Read(~/**)"]
    assert {"/var/tmp", "/tmp"} <= set(sandbox["filesystem"]["denyRead"])
    assert sandbox["filesystem"]["allowRead"] == ["/var/tmp/x/repo", "/var/tmp/x/bin"]
    assert helpers.helper_tmp(Path("/var/tmp/x/repo")) == Path("/var/tmp/x/repo/.git/helper-tmp")
    assert "filesystem" not in helpers.helper_settings(None)["sandbox"]


def test_the_helper_env_drops_deck_and_github_variables(tmp_path, monkeypatch):
    for name in (
        "AGENTDECK_SESSION_TOKEN",
        "CLAUDE_CODE_SESSION_ID",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "GIT_CONFIG_COUNT",
        "GIT_ASKPASS",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "DATABASE_URL",
    ):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/own")
    env = helpers.helper_env(tmp_path / "bin", tmp_path / "acct")
    # #4437 review: an allowlist, so an unrelated secret never reaches the model's Bash.
    assert set(env) - helpers._KEPT_ENV <= {"CLAUDE_CONFIG_DIR", "ENABLE_CLAUDEAI_MCP_SERVERS"} | {
        k for k in env if k.startswith("LC_")
    }
    assert "OPENAI_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"] == os.environ["HOME"]
    assert env["PATH"].startswith(f"{tmp_path / 'bin'}{os.pathsep}")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "acct")
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"


@pytest.mark.parametrize(
    ("body", "given", "issue"),
    [
        ("Refs #7", "", ("o/r", 7)),
        ("Closes other/x#3\nRefs #7", "", ("other/x", 3)),
        ("Refs https://github.com/o/r/issues/12", "", ("o/r", 12)),
        ("no reference", "", None),
        ("Refs #7", "a/b#5", ("a/b", 5)),
        ("Refs #7", "8", ("o/r", 8)),
    ],
)
def test_the_issue_is_given_or_the_bodys_reference(body, given, issue):
    assert helpers.issue_of(pull(body=body), given) == issue


def test_a_bad_pull_or_issue_is_refused():
    with pytest.raises(NotRun):
        helpers.read_pull("not-a-pull", ".")
    with pytest.raises(NotRun):
        helpers.issue_of(pull(), "o/r#x")


USAGE = [
    {"account_key": "claude_code:main", "five_hour_pct": 10.0, "seven_day_pct": 50.0},
    {"account_key": "claude_code:alt", "five_hour_pct": 1.0, "seven_day_pct": 20.0},
    {"account_key": "codex:codex", "five_hour_pct": 0.0},
]


def test_the_chats_own_account_while_it_has_room_else_the_roomiest():
    assert helpers.choose_account(USAGE, "claude_code:main") == "claude_code:main"
    full = [dict(USAGE[0], seven_day_pct=100.0), *USAGE[1:]]
    assert helpers.choose_account(full, "claude_code:main") == "claude_code:alt"
    # A Codex chat has no Claude account: the roomiest, never a Codex one.
    assert helpers.choose_account(USAGE, "") == "claude_code:alt"
    assert helpers.choose_account(USAGE, "claude_code:gone") == "claude_code:alt"
    with pytest.raises(NotRun, match="no Claude account with room"):
        helpers.choose_account([full[0], USAGE[2]], "claude_code:main")


def test_the_own_account_is_the_chats_key_or_its_config_dir(tmp_path, monkeypatch):
    roots = {"claude_code:main": tmp_path / "main", "claude_code:alt": tmp_path / "alt"}
    monkeypatch.delenv("AGENTDECK_ACCOUNT_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert helpers.own_claude_account(roots) == ""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alt"))
    assert helpers.own_claude_account(roots) == "claude_code:alt"
    monkeypatch.setenv("AGENTDECK_ACCOUNT_KEY", "codex:codex")
    assert helpers.own_claude_account(roots) == "claude_code:alt"
    monkeypatch.setenv("AGENTDECK_ACCOUNT_KEY", "claude_code:main")
    assert helpers.own_claude_account(roots) == "claude_code:main"


def test_no_network_stops_the_run_before_any_helper(monkeypatch):
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    with pytest.raises(NotRun, match="escalated"):
        helpers.check_network(lambda *a: pytest.fail("no connect in a Codex sandbox"))
    monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED")

    def refused(address, timeout):
        assert address == ("api.anthropic.com", 443) and timeout == 5
        raise OSError("unreachable")

    with pytest.raises(NotRun, match="no network to the Claude API"):
        helpers.check_network(refused)


def test_the_base_is_fetched_then_the_pull_requests_base_then_the_local_copy(repos):
    origin, checkout, b1, b2, head = repos
    # (a) fetched now: the merge base is b1 even though master moved on to b2.
    assert helpers.base_commit(str(checkout), pull()) == (b1, "")
    # (b) the fetch fails (a Codex sandbox): baseRefOid, when the checkout has it.
    git(checkout, "remote", "set-url", "origin", str(origin) + "-gone")
    assert helpers.base_commit(str(checkout), pull(base_oid=b2)) == (b1, "")
    # (c) neither: the checkout's own origin/master, with a warning.
    sha, warning = helpers.base_commit(str(checkout), pull(base_oid="ab" * 20))
    assert sha == b1 and "may be older" in warning
    with pytest.raises(NotRun, match="base branch unreadable"):
        helpers.base_commit(str(checkout), pull(base_ref="nope", base_oid=""))


def test_the_clone_holds_the_head_and_a_base_branch(repos, tmp_path):
    _origin, checkout, b1, _b2, head = repos
    (tmp_path / "run").mkdir()
    worktree = tmp_path / "wt"
    git(checkout, "worktree", "add", "-q", "--detach", str(worktree), head)
    repo = helpers.make_clone(str(worktree), tmp_path / "run", head, b1)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", "base") == b1
    assert git(repo, "diff", "--name-only", "base", "HEAD") == "change"
    assert git(repo, "remote") == ""
    (repo / ".bashrc").write_text("")
    assert git(repo, "status", "--short") == ""


def test_the_helpers_read_the_pull_requests_head(repos):
    _origin, checkout, _b1, _b2, head = repos
    commit(checkout, "later")
    assert helpers.pull_head(str(checkout), pull(head_oid=head)) == head
    with pytest.raises(NotRun, match="lacks the pull request's head"):
        helpers.pull_head(str(checkout), pull(head_oid="ab" * 20))
    with pytest.raises(NotRun, match="head is unreadable"):
        helpers.pull_head(str(checkout), pull())


def test_stale_run_directories_are_swept(tmp_path):
    old, new = tmp_path / "o--r-9-0123abcd", tmp_path / "o--r-9-4567abcd"
    other = tmp_path / "something-else"
    for path in (old, new, other):
        path.mkdir()
    os.utime(old, (0, 0))
    os.utime(other, (0, 0))
    helpers.sweep_stale(tmp_path, now=helpers.STALE_S + 10)
    assert not old.exists() and new.exists() and other.exists(), "only old run directories"


@pytest.mark.parametrize(
    ("raw", "code", "found", "failed"),
    [
        (
            json.dumps({"type": "result", "result": "x\n## Findings\n1. a.py:1 bad"}),
            0,
            "1. a.py:1 bad",
            "",
        ),
        (
            "warning line\n" + json.dumps({"type": "result", "result": "No findings."}),
            0,
            "No findings.",
            "",
        ),
        (
            json.dumps({"type": "result", "is_error": True, "result": "Credit balance"}),
            0,
            "",
            "Credit balance",
        ),
        # #4437 review: a result with a failing exit code is a failure, not findings.
        (
            json.dumps({"type": "result", "result": "## Findings\n1. half"}),
            1,
            "",
            'exit 1: {"type": "result", "result": "## Findings\\n1. half"}',
        ),
        ("", 124, "", "timed out after 5 minutes"),
        ("", 137, "", "timed out after 5 minutes"),
        (
            json.dumps({"type": "result", "result": "`## Findings` is last\n## Findings\n1. b"}),
            0,
            "1. b",
            "",
        ),
        ("Error: not logged in", 1, "", "exit 1: Error: not logged in"),
        ("", 1, "", "exit 1"),
    ],
)
def test_each_helpers_outcome(tmp_path, raw, code, found, failed):
    path = tmp_path / "1.json"
    path.write_text(raw)
    outcome = helpers.read_outcome(1, path, code)
    assert outcome.findings == found and outcome.failed == failed


def test_the_report_ends_with_what_to_post():
    some = [
        Outcome(1, findings="1. a.py:1 bad"),
        Outcome(2, failed="timed out after 5 minutes"),
        Outcome(3, findings="No findings."),
    ]
    text = helpers.report(pull(), "o/r#7", "h" * 40, "b" * 40, "claude_code:main", some, [])
    assert "## Helper 1 (every path and input)" in text and "1. a.py:1 bad" in text
    assert "## Helper 2 (the rest of the repository): did not run (timed out" in text
    assert "`Helper checks: o/r#9`" in text and "helper 2 did not run" in text
    none = [Outcome(n, failed="no result") for n in (1, 2, 3)]
    text = helpers.report(pull(), "o/r#7", "h" * 40, "b" * 40, "claude_code:main", none, [])
    assert text.endswith(
        "Helper checks: o/r#9 did not run "
        "(helper 1: no result; helper 2: no result; helper 3: no result)"
    )


FAKE_CLAUDE = """#!/bin/sh
cat > "$PWD/../out/seen-$$"
env > "$PWD/../out/env-$$"
gh pr list >/dev/null 2>&1 && echo gh-worked > "$PWD/../out/gh-$$"
printf '{"type":"result","result":"## Findings\\\\n1. a.py:1 from %s","duration_ms":60000}' "$PWD"
"""


def test_a_whole_run_prints_the_findings_and_leaves_nothing(repos, tmp_path, monkeypatch, capsys):
    _origin, checkout, b1, _b2, head = repos
    root = tmp_path / "runs"
    claude = tmp_path / "fake-claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    monkeypatch.setattr(helpers, "ROOT", root)
    monkeypatch.setenv("AGENTDECK_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("AGENTDECK_SESSION_TOKEN", "secret")
    monkeypatch.setattr(helpers, "read_pull", lambda spec, cwd: pull(head_oid=head))
    monkeypatch.setattr(helpers, "issue_text", lambda *a: "THE ISSUE BODY")
    monkeypatch.setattr(helpers, "check_network", lambda: None)
    monkeypatch.setattr(helpers, "check_sandbox", lambda: None)
    monkeypatch.setattr(helpers, "account_roots", lambda: {"claude_code:main": tmp_path / "acct"})
    monkeypatch.setattr(helpers, "read_usage", lambda url: USAGE[:1])
    monkeypatch.setattr(helpers, "helper_command", lambda repo: [str(claude)])
    left = tmp_path / "acct" / "projects"
    left.mkdir(parents=True)
    real_start = helpers.start_helpers

    def start(run, repos, *rest):
        # Claude's own folder for each clone, which it writes even without session persistence.
        for repo in repos.values():
            name = str(repo).replace("/", "-").replace("_", "-").replace(".", "-")
            (left / name / "m").mkdir(parents=True)
        assert sorted(run.transcripts) == sorted(left / name for name in os.listdir(left))
        # #4437 review: one clone each, so no helper changes what another reads.
        assert len({str(repo) for repo in repos.values()}) == 3
        return real_start(run, repos, *rest)

    monkeypatch.setattr(helpers, "start_helpers", start)
    seen: list = []
    real_stop = helpers.stop

    def stop(run):
        out = run.directory / "out"
        seen.extend(p.read_text() for p in sorted(out.glob("seen-*")))
        seen.append([p.read_text() for p in out.glob("env-*")])
        seen.append(list(out.glob("gh-*")))
        real_stop(run)

    monkeypatch.setattr(helpers, "stop", stop)
    # #4437 review: the checkout has moved on past the pushed head; the helpers still read the
    # pull request's own head, not what happens to be checked out.
    later = commit(checkout, "later")
    args = argparse.Namespace(cwd=str(checkout), pull="9", issue="", url="http://deck")
    assert helpers.run_helpers(args) == 0
    text = capsys.readouterr().out
    assert later[:12] not in text
    assert f"at {head[:12]} (base {b1[:12]}), on claude_code:main" in text
    assert text.count("1. a.py:1 from ") == 3 and "/repo" in text
    prompts, envs, gh = seen[:3], seen[3], seen[4]
    assert all("THE ISSUE BODY" in p and "git diff base HEAD" in p for p in prompts)
    assert len({p.split("**")[1] for p in prompts}) == 3, "one list per helper"
    assert all("AGENTDECK_SESSION_TOKEN" not in e for e in envs)
    assert all(f"CLAUDE_CONFIG_DIR={tmp_path / 'acct'}" in e for e in envs)
    assert gh == []
    assert list(root.iterdir()) == [], "the clone is removed"
    assert list(left.iterdir()) == [], "and Claude's folder for it"
    # The slot is free again.
    from agentdeck.host_slots import helper_slot_paths, try_take_slot

    fd = try_take_slot(helper_slot_paths(1))
    assert fd is not None
    os.close(fd)


def test_an_unreadable_pull_request_still_names_it(repos, monkeypatch, capsys):
    """#4437 review: GitHub down while reading the pull request; the line the coder posts
    still names it, so ``pre-review-done`` takes it."""
    from agentdeck.review_paperwork import helper_receipt

    _origin, checkout, *_ = repos
    git(checkout, "remote", "set-url", "origin", "git@github.com:Own/Code.git")

    def down(spec, cwd):
        raise NotRun("gh: HTTP 502")

    monkeypatch.setattr(helpers, "read_pull", down)
    monkeypatch.setattr(helpers, "check_network", lambda: None)
    for spec, ref in (("9", "Own/Code#9"), ("o/r#9", "o/r#9")):
        args = argparse.Namespace(cwd=str(checkout), pull=spec, issue="", url="http://deck")
        assert helpers.run_helpers(args) == 0
        line = capsys.readouterr().out.strip()
        assert line == f"Helper checks: {ref} did not run (gh: HTTP 502)"
        owner_repo = ref.split("#")[0]
        assert helper_receipt(line, owner_repo, 9, "Own/Code")
    git(checkout, "remote", "set-url", "origin", "https://github.com/Own/Code")
    assert helpers.spec_ref("9", str(checkout)) == "Own/Code#9"
    git(checkout, "remote", "set-url", "origin", "/somewhere/else")
    assert helpers.spec_ref("9", str(checkout)) == "#9"
    assert helpers.spec_ref("nine", str(checkout)) == ""
    # A Codex chat without escalation hears to escalate, before any gh call fails.
    monkeypatch.undo()
    monkeypatch.setattr(helpers, "read_pull", down)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    args = argparse.Namespace(cwd=str(checkout), pull="o/r#9", issue="", url="http://deck")
    assert helpers.run_helpers(args) == 0
    assert capsys.readouterr().out.strip() == (
        "Helper checks: o/r#9 did not run (run it escalated: the Codex sandbox has no network)"
    )


def test_a_run_that_cannot_start_says_why_and_exits_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(helpers, "read_pull", lambda spec, cwd: pull(head_oid="h" * 40))
    monkeypatch.setattr(helpers, "issue_text", lambda *a: "")

    def offline():
        raise NotRun("no network to the Claude API: unreachable")

    monkeypatch.setattr(helpers, "check_network", offline)
    args = argparse.Namespace(cwd=str(tmp_path), pull="9", issue="", url="http://deck")
    assert helpers.run_helpers(args) == 0
    assert capsys.readouterr().out.strip() == (
        "Helper checks: #9 did not run (no network to the Claude API: unreachable)"
    )
    # A helper that cannot start (no ``timeout`` on the host) still says what to post.
    monkeypatch.setattr(helpers, "check_network", lambda: None)
    monkeypatch.setattr(helpers, "check_sandbox", lambda: None)
    monkeypatch.setattr(helpers, "account_roots", lambda: {"claude_code:main": tmp_path})
    monkeypatch.setattr(helpers, "read_usage", lambda url: USAGE[:1])
    monkeypatch.setattr(helpers, "base_commit", lambda *a: ("b" * 40, ""))
    monkeypatch.setattr(helpers, "_git", lambda *a, **k: "")
    monkeypatch.setattr(helpers, "make_clone", lambda *a: tmp_path / "repo")
    monkeypatch.setattr(helpers, "ROOT", tmp_path / "runs")
    monkeypatch.setenv("AGENTDECK_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setattr(helpers, "helper_command", lambda repo: [str(tmp_path / "no-such-binary")])
    assert helpers.run_helpers(args) == 0
    assert (
        capsys.readouterr()
        .out.splitlines()[-1]
        .startswith("Helper checks: o/r#9 did not run (the helpers cannot start: ")
    )
    monkeypatch.setattr(helpers, "read_pull", lambda spec, cwd: pull(body="nothing"))
    assert helpers.run_helpers(args) == 0
    assert "names no issue" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "linux", reason="PR_SET_PDEATHSIG is Linux's")
def test_a_killed_command_leaves_no_helper_running(tmp_path):
    """The design review: the caller SIGKILLed (a tool timeout, a stopped chat) runs no
    ``finally``, so each helper's ``timeout`` gets SIGTERM from the kernel and stops it."""
    pids = tmp_path / "pids"
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(Path(helpers.__file__).parents[1])!r})
        from agentdeck import pre_review_helpers as h
        h.helper_command = lambda repo: [
            "timeout", "-k", "1", "60", "sh", "-c", "echo $$ >> {pids}; exec sleep 60"
        ]
        run = h.Run(Path({str(tmp_path)!r}))
        here = Path({str(tmp_path)!r})
        h.start_helpers(run, {{1: here, 2: here}}, {{1: "", 2: ""}}, {{}})
        while len(Path({str(pids)!r}).read_text().split()) < 2 if Path({str(pids)!r}).exists() \\
                else True:
            time.sleep(0.01)
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    parent = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert parent.stdout.readline().strip() == "ready"
        children = [int(pid) for pid in pids.read_text().split()]
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(pid) for pid in children):
            time.sleep(0.02)
        assert not any(_alive(pid) for pid in children)
    finally:
        parent.kill()


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


def test_a_long_issue_body_is_cut_before_its_use_cases(monkeypatch):
    comments = [{"body": "chatter"}, {"body": "Use-case walkthrough:\n1. the case"}]
    data = {"title": "T", "body": "x" * 50_000, "comments": comments}
    monkeypatch.setattr(helpers, "_gh_json", lambda argv, cwd: data)
    text = helpers.issue_text("o/r", 7, ".")
    assert text.startswith("o/r#7: T\n\nxxx") and "chatter" not in text
    assert text.endswith("Use-case walkthrough:\n1. the case")
    assert "[… the rest of the issue body is cut]" in text
    assert len(text) < helpers.MAX_ISSUE_CHARS + 100
    # A walkthrough too long for its half says so, rather than losing its later steps unseen.
    comments.append({"body": "Use-case walkthrough:\n" + "y" * 50_000})
    text = helpers.issue_text("o/r", 7, ".")
    assert text.endswith("[… the rest of the walkthroughs is cut]")
    assert len(text) < helpers.MAX_ISSUE_CHARS + 200
