"""``agentdeck pre-review-helpers``: three Haiku helpers and an Opus whole review before a first
review (#4437, #4531)."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
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


@pytest.fixture(autouse=True)
def usage_log(tmp_path, monkeypatch) -> Path:
    """Every test's usage log is its own (#4510), never the host's."""
    path = tmp_path / "usage" / "usage.jsonl"
    monkeypatch.setenv("AGENTDECK_HELPER_USAGE_LOG", str(path))
    return path


def logged(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


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
    # #4531: the whole review is the same command, on Opus with its own 10-minute limit.
    whole = helpers.helper_command(repo=Path("/var/tmp/x/repo"), part=helpers.WHOLE)
    assert whole[:4] == ["timeout", "-k", "10", "600"]
    assert whole[whole.index("--model") + 1] == "claude-opus-5-5"
    assert whole[4:] == [
        "claude-opus-5-5" if arg == "claude-haiku-5-5" else arg for arg in argv[4:]
    ], "otherwise the same isolation"


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
    # #4531: a day-old report and a lock nobody holds go; a held lock stays, whatever its age.
    report, free = tmp_path / "o--r-9-0123456789ab.report", tmp_path / "o--r-9-0123456789ab.lock"
    held_lock = tmp_path / "o--r-10-0123456789ab.lock"
    part = tmp_path / "o--r-9-0123456789ab.report.4242.part"  # a SIGKILL before the rename
    report.write_text("r")
    part.write_text("r")
    free.write_text("")
    held, _ = helpers.take_head_lock(held_lock)
    for path in (report, part, free, held_lock):
        os.utime(path, (0, 0))
    try:
        helpers.sweep_stale(tmp_path, now=helpers.STALE_S + 10)
    finally:
        os.close(held)
    assert not old.exists() and new.exists() and other.exists(), "only old run directories"
    assert not report.exists() and not free.exists() and held_lock.exists()
    assert not part.exists()


def test_the_whole_review_keeps_finds_at_the_floor_or_above(tmp_path):
    """#4531: the Opus pass's suspicions under confidence 25 are left out and counted."""
    findings = [
        {"kind": "bug", "file": "a.py", "line": 3, "text": "bad", "confidence": 25},
        {"kind": "edge case", "file": "b.py", "text": "unsure", "confidence": 24.5},
        {"kind": "design", "text": "Simplify: x", "confidence": 90},
        {"kind": "bug", "text": "no number", "confidence": "high"},
        {"kind": "bug", "text": "a string number", "confidence": "10"},
        {"kind": "bug", "text": "line one\nline two", "confidence": " 60 "},
        {"kind": "bug", "text": "not a number", "confidence": "NaN"},
        {"kind": "bug", "text": "none at all"},
    ]
    reply = (
        "Findings comment, with an example:\n```json\n{}\n```\n"
        f"Architecture: …\n```json\r\n{json.dumps({'findings': findings})}\r\n```\n"
    )
    # #4532 helper checks: a CRLF fence, a raw newline in a text, a number given as a string.
    reply = reply.replace("line one\\nline two", "line one\nline two")
    text, dropped = helpers.whole_findings(reply)
    assert dropped == 2
    assert text.splitlines() == [
        "1. a.py:3 [bug, confidence 25] bad",
        "2. [design, confidence 90] Simplify: x",
        "3. [bug, confidence 60] line one",
        "line two",
        "",
        "(Without a readable confidence, so not checked against the floor of 25; judge each as"
        " a suspicion:)",
        "1. [bug] no number",
        "2. [bug] not a number",
        "3. [bug] none at all",
    ]
    # #4532 review: an entry that is not a finding object is not lost in silence.
    odd = '```json\n{"findings": ["a suspicion as a bare string"]}\n```'
    assert helpers.whole_findings(odd) == (
        "(No readable findings block; the reply as it is:)\n" + odd,
        0,
    )
    assert helpers.whole_findings('```json\n{"findings": [], "verdict": "clean"}\n```') == (
        "No findings.",
        0,
    )
    assert helpers.whole_findings("no block")[0].endswith("the reply as it is:)\nno block")
    # Through ``read_outcome``: the report names the floor and what fell under it.
    path = tmp_path / "4.json"
    path.write_text(json.dumps({"type": "result", "result": reply, "total_cost_usd": 1.8}))
    outcome = helpers.read_outcome(helpers.WHOLE, path, 0)
    assert outcome.dropped == 2 and outcome.findings == text
    shown = helpers.report(pull(), "o/r#7", "h" * 40, "b" * 40, "c", [outcome], [])
    assert "## Helper 4 (whole review by claude-opus-5-5, confidence 25 or above)" in shown
    assert "(2 more under confidence 25 left out.)" in shown
    # No total cost: the report shows the per-model cost, as the usage log does.
    event = {"type": "result", "result": reply, "modelUsage": {"m": {"costUSD": 0.75}}}
    path.write_text(json.dumps(event))
    assert helpers.read_outcome(helpers.WHOLE, path, 0).cost_usd == 0.75
    assert helpers.read_outcome(helpers.WHOLE, path, 124).failed == "timed out after 10 minutes"


def test_the_whole_review_reads_the_reviewers_rules_without_fetching(tmp_path):
    """#4531 design review: from the review-chat checkout's ``origin/main`` (no ``origin/HEAD``
    needed, nothing fetched), the profile matched without case, its workflow source when it
    is in the review-chat repository, and REVIEW.md from the deck's ``origin/master``."""
    chat, deck = tmp_path / "chat", tmp_path / "deck"
    files = {
        "skills/github-repo-reviewer/SKILL.md": "SKILL",
        "skills/github-repo-reviewer/references/baseline-review.md": "BASE",
        "skills/github-repo-reviewer/profiles/_default.yaml": "DEFAULT",
        "skills/github-repo-reviewer/profiles/Big--Shop.yaml": (
            "review:\n  workflow_source:\n    repository: o/Chat\n    path: skills/fix/SKILL.md\n"
        ),
        "skills/fix/SKILL.md": "FIX",
    }
    for root, tree, ref in (
        (chat, files, "main"),
        (deck, {"skills/rate-my-pr/REVIEW.md": "RV"}, "master"),
    ):
        root.mkdir()
        git(root, "init", "-q", "-b", "work")
        for name, text in tree.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git(root, "add", ".")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
        git(root, "update-ref", f"refs/remotes/origin/{ref}", "HEAD")
        # What is on disk is not what is read.
        (root / next(iter(tree))).write_text("DRIFTED")
    rules, warnings = helpers.reviewer_rules("big/shop", str(chat), "o/chat", deck)
    assert {name.split(" ")[0]: text for name, text in rules.items()} == {
        "reviewer-SKILL.md": "SKILL",
        "reviewer-baseline-review.md": "BASE",
        "reviewer-Big--Shop.yaml": files["skills/github-repo-reviewer/profiles/Big--Shop.yaml"],
        "fix-SKILL.md": "FIX",
        "rate-my-pr-REVIEW.md": "RV",
    }
    assert len(warnings) == 1 and "references/review-policy.md" in warnings[0]
    rules, _ = helpers.reviewer_rules("o/none", str(chat), "o/chat", deck)
    assert (
        "reviewer-_default.yaml (the default profile; this repository has none of its own)" in rules
    )
    # The workflow source in another repository is not read, and says so.
    _, warnings = helpers.reviewer_rules("big/shop", str(chat), "o/elsewhere", deck)
    assert any("not in the review-chat repository" in warning for warning in warnings)
    # A layout the pattern misses is named, not skipped in silence (#4532 helper check).
    odd = "review:\n  workflow_source: {repository: o/chat, path: skills/fix/SKILL.md}\n"
    profile = chat / "skills/github-repo-reviewer/profiles/Big--Shop.yaml"
    profile.write_text(odd)
    # A Latin-1 byte does not end the run (#4532 helper check).
    (chat / "skills/github-repo-reviewer/references/baseline-review.md").write_bytes(b"caf\xe9")
    git(chat, "add", ".")
    git(chat, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "y")
    git(chat, "update-ref", "refs/remotes/origin/main", "HEAD")
    rules, warnings = helpers.reviewer_rules("big/shop", str(chat), "o/chat", deck)
    assert any("not laid out as" in warning for warning in warnings)
    assert rules["reviewer-baseline-review.md"] == "caf\ufffd"
    # No SKILL.md: the whole review alone does not run.
    git(chat, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(NotRun, match="SKILL.md is unreadable"):
        helpers.reviewer_rules("big/shop", str(chat), "o/chat", deck)
    with pytest.raises(NotRun, match="no review-chat checkout"):
        helpers.reviewer_rules("big/shop", "", "o/chat", deck)


def test_the_whole_reviews_context_is_this_body_the_issue_and_its_comments(monkeypatch):
    data = {
        "title": "T",
        "body": "BODY",
        "comments": [
            {"author": {"login": "eero"}, "createdAt": "2026-11-19T10:00:00Z", "body": "ONE"},
            {"body": "TWO"},
            "junk",
        ],
    }
    text = helpers.whole_context("o/r", 7, data, pull(title="PT", body="Refs #7 NOW"))
    assert text.index("# Pull request o/r#9: PT") < text.index("NOW") < text.index("BODY")
    assert text.index("BODY") < text.index("--- eero at 2026-11-19T10:00:00Z\nONE")
    assert text.index("ONE") < text.index("--- someone at ?\nTWO")
    # A cut takes comments, never this body (#4532 helper check).
    monkeypatch.setattr(helpers, "MAX_CONTEXT_CHARS", 200)
    data["comments"] = [{"body": "X" * 500}]
    text = helpers.whole_context("o/r", 7, data, pull(title="PT", body="Refs #7 NOW"))
    assert "NOW" in text and text.endswith("[… the rest is cut]\n")


def test_a_closed_output_does_not_stop_the_run(tmp_path):
    """#4531 design review: the caller of a background run is gone; printing goes nowhere and
    the run carries on to its usage lines and report file."""
    read, write = os.pipe()
    os.close(read)
    stream = os.fdopen(write, "w")
    helpers._say("into a dead pipe", stream=stream)
    helpers._say("and again", stream=stream)
    helpers.save_report(tmp_path / "r.report", "THE REPORT")
    assert (tmp_path / "r.report").read_text() == "THE REPORT\n"
    assert list(tmp_path.iterdir()) == [tmp_path / "r.report"]


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
    expected = "ok" if not failed else "timeout" if code in (124, 137) else "failed"
    assert outcome.exit == expected


# The shape ``claude -p --output-format json`` printed on 2026-11-19 (#4510), trimmed.
REAL_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "## Findings\nNo findings.",
    "duration_ms": 2236,
    "total_cost_usd": 0.00147552,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 6665,
        "cache_read_input_tokens": 11382,
        "output_tokens": 57,
    },
    "modelUsage": {
        "claude-haiku-5-5": {
            "inputTokens": 2,
            "outputTokens": 57,
            "cacheReadInputTokens": 11382,
            "cacheCreationInputTokens": 6665,
            "costUSD": 0.00147552,
        }
    },
}


def test_a_helpers_usage_is_read_whatever_its_exit(tmp_path):
    spent = {
        "model": "claude-haiku-5-5",
        "input_tokens": 2,
        "output_tokens": 57,
        "cache_read_tokens": 11382,
        "cache_write_tokens": 6665,
        "cost_usd": 0.00147552,
    }
    assert helpers.usage_of(REAL_RESULT) == spent
    # Without ``modelUsage``, the top-level ``usage``; without either, zeros and no cost.
    top = {k: v for k, v in REAL_RESULT.items() if k != "modelUsage"}
    assert helpers.usage_of(top) == spent
    assert helpers.usage_of({"type": "result", "modelUsage": {"m": {"inputTokens": True}}}) == {
        **spent,
        "model": "m",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": None,
    }
    assert helpers.usage_of(None)["cost_usd"] is None
    # No total: the per-model costs (#4510 helper check).
    no_total = {k: v for k, v in REAL_RESULT.items() if k != "total_cost_usd"}
    assert helpers.usage_of(no_total)["cost_usd"] == 0.00147552
    path = tmp_path / "1.json"
    path.write_text(json.dumps({**REAL_RESULT, "is_error": True}))
    failed = helpers.read_outcome(1, path, 1, wall_s=3.0)
    assert failed.exit == "failed" and failed.usage == spent and failed.wall_s == 3.0


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
printf '{"type":"result","result":"## Findings\\\\n1. a.py:1 from %s","duration_ms":60000,'\\
'"total_cost_usd":0.25,"modelUsage":{"claude-haiku-5-5":{"inputTokens":10,"outputTokens":20,'\\
'"cacheReadInputTokens":30,"cacheCreationInputTokens":40}}}' "$PWD"
"""


def test_a_whole_run_prints_the_findings_and_leaves_nothing(
    repos, tmp_path, monkeypatch, capsys, usage_log
):
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
    issue = {"title": "T", "body": "THE ISSUE BODY", "comments": [{"body": "A LATE COMMENT"}]}
    monkeypatch.setattr(helpers, "read_issue", lambda *a: issue)
    monkeypatch.setattr(helpers, "review_chat_settings", lambda: ("~/chat", "o/chat"))
    monkeypatch.setattr(
        helpers, "reviewer_rules", lambda *a: ({"reviewer-SKILL.md": "THE RULES"}, ["lacks x"])
    )
    monkeypatch.setattr(helpers, "check_network", lambda: None)
    monkeypatch.setattr(helpers, "check_sandbox", lambda: None)
    monkeypatch.setattr(helpers, "account_roots", lambda: {"claude_code:main": tmp_path / "acct"})
    monkeypatch.setattr(helpers, "read_usage", lambda url: USAGE[:1])
    monkeypatch.setattr(helpers, "helper_command", lambda repo, part=1: [str(claude)])
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
        assert len({str(repo) for repo in repos.values()}) == 4
        # #4531: the whole review's inputs sit in its own clone, where its sandbox reads them.
        inputs = repos[helpers.WHOLE] / ".git" / "helper-review"
        assert (inputs / "rules" / "reviewer-SKILL.md").read_text() == "THE RULES"
        assert "A LATE COMMENT" in (inputs / "context.md").read_text()
        assert "+change" in (inputs / "diff.patch").read_text()
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
    assert text.count("1. a.py:1 from ") == 4 and "/repo" in text
    # The fake's reply has no findings block: the whole review shows it as it is.
    assert "(No readable findings block; the reply as it is:)" in text
    assert "Warning: lacks x" in text
    whole = [p for p in seen[:4] if "standing in for AgentDeck's pull-request reviewer" in p]
    assert len(whole) == 1 and "rules/reviewer-SKILL.md" in whole[0] and "lacks x" in whole[0]
    prompts = [p for p in seen[:4] if p not in whole]
    envs, gh = seen[4], seen[5]
    assert all("THE ISSUE BODY" in p and "git diff base HEAD" in p for p in prompts)
    assert len({p.split("**")[1] for p in prompts}) == 3, "one list per helper"
    # #4482: the "Every way in" question rides with the rest of the repository, once.
    way_in = [p for p in prompts if "**Every way in.**" in p]
    assert len(way_in) == 1 and "**The rest of the repository.**" in way_in[0]
    assert all("AGENTDECK_SESSION_TOKEN" not in e for e in envs)
    assert all(f"CLAUDE_CONFIG_DIR={tmp_path / 'acct'}" in e for e in envs)
    assert gh == []
    lock, saved = helpers.head_files(pull(head_oid=head), root)
    assert sorted(root.iterdir()) == [lock, saved], "the clone is removed; the report is kept"
    assert saved.read_text().rstrip("\n") == text.split("\n", 1)[1].rstrip("\n")
    assert list(left.iterdir()) == [], "and Claude's folder for it"
    # #4510: one usage line per helper, kept after the run's folder is gone.
    lines = logged(usage_log)
    assert [line["helper"] for line in lines] == [1, 2, 3, 4]
    assert lines[3]["name"] == "whole review"
    for line in lines:
        assert line["pull"] == "o/r#9" and line["head"] == head
        assert line["account"] == "claude_code:main" and line["exit"] == "ok"
        assert (line["input_tokens"], line["output_tokens"]) == (10, 20)
        assert (line["cache_read_tokens"], line["cache_write_tokens"]) == (30, 40)
        assert line["cost_usd"] == 0.25 and line["model"] == "claude-haiku-5-5"
        assert line["wall_s"] >= 0 and line["name"] == helpers.NAMES[line["helper"]]
    # The slot is free again.
    from agentdeck.host_slots import helper_slot_paths, try_take_slot

    fd = try_take_slot(helper_slot_paths(1))
    assert fd is not None
    os.close(fd)
    # #4531 design review: the caller died and the coder runs it again on the same head. It
    # prints the kept report, spending nothing and logging nothing.
    assert helpers.run_helpers(args) == 0
    again = capsys.readouterr().out
    assert f"from {saved}, follows" in again and text.split("\n", 1)[1].strip() in again
    assert len(logged(usage_log)) == 4
    # While a run on that head holds the lock, a second one refuses and names the report file.
    held, _ = helpers.take_head_lock(lock)
    try:
        refused = subprocess.run(
            [
                sys.executable,
                "-c",
                REFUSED.format(
                    src=str(Path(helpers.__file__).parents[1]), root=str(root), head=head
                ),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "AGENTDECK_HELPER_USAGE_LOG": str(usage_log)},
        )
    finally:
        os.close(held)
    assert refused.returncode == 1, refused.stderr
    assert f"already running (pid {os.getpid()})" in refused.stdout
    assert str(saved) in refused.stdout and "Helper checks:" not in refused.stdout
    assert len(logged(usage_log)) == 4, "a refused run logs nothing"


REFUSED = """
import argparse, sys
from pathlib import Path
sys.path.insert(0, {src!r})
from agentdeck import pre_review_helpers as h
h.ROOT = Path({root!r})
h.check_network = lambda: None
h.read_pull = lambda spec, cwd: h.Pull("o/r", 9, "t", "Refs #7", "master", "", {head!r})
def boom(*a):
    raise AssertionError("spent past the lock")
h.read_issue = boom
sys.exit(h.run_helpers(argparse.Namespace(cwd=".", pull="9", issue="", url="x", again=False)))
"""


def test_an_unreadable_pull_request_still_names_it(repos, monkeypatch, capsys, usage_log):
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
    # The undo dropped the autouse fixture's log path too: keep the host's log untouched.
    monkeypatch.setenv("AGENTDECK_HELPER_USAGE_LOG", str(usage_log))
    monkeypatch.setattr(helpers, "read_pull", down)
    monkeypatch.setenv("CODEX_SANDBOX_NETWORK_DISABLED", "1")
    args = argparse.Namespace(cwd=str(checkout), pull="o/r#9", issue="", url="http://deck")
    assert helpers.run_helpers(args) == 0
    assert capsys.readouterr().out.strip() == (
        "Helper checks: o/r#9 did not run (run it escalated: the Codex sandbox has no network)"
    )


def test_a_run_that_cannot_start_says_why_and_exits_0(tmp_path, monkeypatch, capsys, usage_log):
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
    # #4510: a run that never starts its helpers still logs a line for each.
    lines = logged(usage_log)
    assert [(line["helper"], line["exit"]) for line in lines] == [
        (1, "did_not_run"),
        (2, "did_not_run"),
        (3, "did_not_run"),
        (4, "did_not_run"),
    ]
    assert lines[0]["why"] == "no network to the Claude API: unreachable"
    assert lines[0]["input_tokens"] == 0 and lines[0]["cost_usd"] is None
    assert lines[0]["model"] == "", "no model ran"
    usage_log.unlink()
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
    monkeypatch.setattr(helpers, "read_issue", lambda *a: {})
    monkeypatch.setattr(helpers, "review_chat_settings", lambda: ("", ""))
    monkeypatch.setattr(
        helpers, "helper_command", lambda repo, part=1: [str(tmp_path / "no-such-binary")]
    )
    assert helpers.run_helpers(args) == 0
    assert (
        capsys.readouterr()
        .out.splitlines()[-1]
        .startswith("Helper checks: o/r#9 did not run (the helpers cannot start: ")
    )
    lines = logged(usage_log)
    assert len(lines) == 4 and {line["exit"] for line in lines} == {"did_not_run"}
    assert lines[0]["head"] == "h" * 40 and lines[0]["account"] == "claude_code:main"
    assert "no review-chat checkout" not in str(lines), "one line each, not two for helper 4"
    usage_log.unlink()
    # #4532 helper check: killed in the slot wait, with the whole review already skipped for
    # want of the reviewer's rules; its line is not lost.
    import agentdeck.host_slots as host_slots

    def killed(*a):
        raise SystemExit(143)

    monkeypatch.setattr(host_slots, "take_a_slot", killed)
    with pytest.raises(SystemExit):
        helpers.run_helpers(argparse.Namespace(**{**vars(args), "again": True}))
    capsys.readouterr()
    lines = logged(usage_log)
    assert [(line["helper"], line["exit"]) for line in lines] == [(4, "did_not_run")]
    assert "no review-chat checkout" in lines[0]["why"]
    monkeypatch.setattr(helpers, "read_pull", lambda spec, cwd: pull(body="nothing"))
    assert helpers.run_helpers(args) == 0
    assert "names no issue" in capsys.readouterr().out
    # #4532 review: a lock that cannot be taken stops the run, rather than letting a second
    # paid run go beside another.
    usage_log.unlink(missing_ok=True)
    monkeypatch.setattr(helpers, "read_pull", lambda spec, cwd: pull(head_oid="h" * 40))

    def no_lock(path):
        raise PermissionError("denied")

    monkeypatch.setattr(helpers, "take_head_lock", no_lock)
    monkeypatch.setattr(helpers, "read_issue", lambda *a: pytest.fail("spent past the lock"))
    assert helpers.run_helpers(args) == 0
    assert "did not run (this head's lock" in capsys.readouterr().out
    lines = logged(usage_log)
    assert len(lines) == 4 and {line["exit"] for line in lines} == {"did_not_run"}


def test_a_run_cut_short_logs_what_its_helpers_spent(tmp_path, usage_log, monkeypatch):
    """#4510 helper check: killed while helpers run, the finished one keeps its usage and the
    running one gets an ``interrupted`` line, before ``stop`` removes the output."""
    run = helpers.Run(tmp_path / "run")
    (run.directory / "out").mkdir(parents=True)
    (run.directory / "out" / "1.json").write_text(json.dumps(REAL_RESULT))
    done = subprocess.Popen(["true"])
    done.wait()
    running = subprocess.Popen(["sleep", "30"])
    run.processes = [done, running]
    # #4531: each process's own helper, the whole review here among them.
    run.parts = [1, helpers.WHOLE]
    # #4511 review: helper 1 ended 2 s in (as ``wait_all`` saw it), the kill comes at 60 s;
    # its line keeps its own 2 s, not the time to the kill.
    run.ended[0] = (0, 2.0)
    try:
        helpers.log_cut_short(run, "o/r#9", "h" * 40, "claude_code:main", time.monotonic() - 60)
    finally:
        running.kill()
        running.wait()
    lines = logged(usage_log)
    assert [(line["helper"], line["exit"]) for line in lines] == [(1, "ok"), (4, "interrupted")]
    assert lines[1]["model"] == "claude-opus-5-5" and lines[1]["name"] == "whole review"
    assert lines[0]["cache_read_tokens"] == 11382 and lines[1]["input_tokens"] == 0
    assert lines[0]["wall_s"] == 2.0 and lines[1]["wall_s"] >= 60
    # The default path is the deck's data folder, honouring XDG_DATA_HOME.
    monkeypatch.delenv("AGENTDECK_HELPER_USAGE_LOG")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert helpers.usage_log_path() == tmp_path / "data/agentdeck/pre-review-helpers/usage.jsonl"


@pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP is POSIX")
def test_a_kill_while_the_usage_is_written_waits_until_it_is_noted(monkeypatch):
    """#4511 review: a SIGTERM between writing the usage lines and noting them written would
    log them twice; it is held until both are done, then still stops the command.

    #4520: held for the whole process, not only the main thread. A pytest-xdist worker has
    other threads; a per-thread signal mask let one of them take the kill, and the handler
    then ran in the main thread before ``noted``. A live thread here pins that case."""
    old = signal.signal(signal.SIGTERM, helpers._on_signal)
    steps = []
    other = threading.Event()
    bystander = threading.Thread(target=other.wait, daemon=True)
    bystander.start()
    try:
        with pytest.raises(SystemExit) as stopped:
            with helpers._signals_held():
                steps.append("written")
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)  # time for the bystander to take it, as it can under xdist
                steps.append("noted")
        assert steps == ["written", "noted"] and stopped.value.code == 128 + signal.SIGTERM
        assert signal.getsignal(signal.SIGTERM) is helpers._on_signal
    finally:
        other.set()
        signal.signal(signal.SIGTERM, old)


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
        h.helper_command = lambda repo, part=1: [
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
