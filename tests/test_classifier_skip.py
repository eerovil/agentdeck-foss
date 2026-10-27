"""#2271: everyday commands skip Claude's classifier, and a block reaches the boss."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from functools import cache
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from agentdeck.classifier_grants import GRANT_MAX_AGE_S, consume_grant, write_grant
from agentdeck.command_artifacts import claude_permission_settings
from agentdeck.providers.claude_code import permission_rules

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "claude_permission_hook.py"


# --- the allow list ---------------------------------------------------------


def _program(rule: str) -> str:
    return rule[len("Bash(") : -1].rsplit(" *", 1)[0]


def test_no_default_rule_lets_a_github_reaching_command_through():
    for rule in permission_rules.default_rules():
        words = _program(rule).split()
        assert words[0] not in {"gh", "curl", "wget", "http", "ssh", "rm", "xargs", "find"}, rule
        if words[0] == "git":
            assert words[1] not in permission_rules.GUARDED_GIT_SUBCOMMANDS, rule


@pytest.mark.parametrize(
    "rule",
    [
        "Bash",
        "Bash()",
        "Bash(*)",
        "Bash(:*)",
        "Bash(gh:*)",
        "Bash(gh pr view *)",
        "Bash(  GH   pr view *)",
        "Bash(/usr/bin/gh *)",
        "Bash(curl:*)",
        "Bash(Curl -s *)",
        "Bash(wget *)",
        "Bash(git push:*)",
        "Bash(Git PUSH origin *)",
        "Bash(git *)",
        "Bash(git:*)",
        "Bash(git -C /repo *)",
        "Bash(git remote *)",
        "Bash(git config *)",
        "Bash(git reset *)",
        "Bash(python:*)",
        "Bash(python3 -c *)",
        "Bash(bash -c *)",
        "Bash(g* *)",
        "Bash(rm -rf *)",
        "Bash(FOO=1 gh *)",
        "Bash(timeout 5 gh api *)",
        "Bash(nice git push *)",
        "Bash(make deploy && git push *)",
        "Bash(echo foo;gh api *)",
        "Bash(echo $(gh api user) *)",
        "Bash(echo `curl x` *)",
        "Bash(make test|gh api *)",
        "Bash(make x>out *)",
        "Bash(echo 'gh api' *)",
        "Bash(make\ngh api *)",
        "WebFetch(domain:api.github.com)",
        "Read(**)",
        "",
    ],
)
def test_a_configured_rule_that_reaches_github_or_covers_everything_is_refused(rule):
    assert permission_rules.refusal(rule) is not None
    extras = permission_rules.allow_rules([rule])[len(permission_rules.default_rules()) :]
    assert extras == []


@pytest.mark.parametrize("rule", ["Bash(make test *)", "Bash(git status:*)", "Bash(npm run lint)"])
def test_a_narrow_configured_rule_is_added_once(rule):
    rules = permission_rules.allow_rules([rule, rule])
    assert permission_rules.refusal(rule) is None
    assert rules.count(rule) == 1


@pytest.mark.parametrize(
    ("command", "reaches"),
    [
        ("gh issue view 1", True),
        ("GH api user", True),
        ("/usr/bin/gh api user", True),
        ("cd x && gh pr create", True),
        ("echo $(gh api user)", True),
        ("echo `curl -s x`", True),
        ("git status; git push origin HEAD", True),
        ("git -C repo push", True),
        ("FOO=1 wget https://x", True),
        ("echo 'unbalanced", True),
        ('bash -c "gh api -X DELETE repos/o/r"', True),
        ("sh -c 'cd x && git push -f'", True),
        ('echo "all done"', False),
        ("git status && git commit -m 'reset things'", False),
        ("pytest -q tests", False),
        ("ls -la; cat README.md | head", False),
    ],
)
def test_reaches_github_errs_towards_the_classifier(command, reaches):
    from agentdeck.guarded_commands import reaches_github

    assert reaches_github(command) is reaches


def test_the_settings_carry_the_rules_and_both_hook_events():
    settings = claude_permission_settings(["Bash(ls *)"])
    assert settings["permissions"] == {"allow": ["Bash(ls *)"]}
    assert set(settings["hooks"]) == {"PermissionDenied", "PreToolUse"}
    assert "Bash" in settings["hooks"]["PreToolUse"][0]["matcher"]


# --- one-time grants --------------------------------------------------------


def test_a_grant_is_used_once_for_that_chat_and_call_only(tmp_path):
    write_grant(tmp_path, "claude_code:main", "w1", "Bash", {"command": "gh api  user"})

    assert not consume_grant(tmp_path, "claude_code:alt", "w1", "Bash", {"command": "gh api user"})
    assert not consume_grant(
        tmp_path, "claude_code:main", "w1", "Bash", {"command": "gh api users"}
    )
    assert not consume_grant(tmp_path, "claude_code:main", "w1", "Edit", {"command": "gh api user"})
    assert consume_grant(tmp_path, "claude_code:main", "w1", "Bash", {"command": " gh api user "})
    assert not consume_grant(tmp_path, "claude_code:main", "w1", "Bash", {"command": "gh api user"})
    assert not consume_grant(tmp_path, "", "", "Bash", {"command": "gh api user"})


def test_a_stale_grant_is_used_up_but_approves_nothing(tmp_path):
    grant = write_grant(tmp_path, "a", "w", "WebFetch", {"url": "https://example.com"})
    old = grant.stat().st_mtime - GRANT_MAX_AGE_S - 5
    os.utime(grant, (old, old))

    assert not consume_grant(tmp_path, "a", "w", "WebFetch", {"url": "https://example.com"})
    assert not grant.exists()


# --- the hook, run as Claude runs it ----------------------------------------


class _FakeRuntime:
    """The runtime's two block routes on a Unix socket."""

    def __init__(self, directory: Path, answers: list[str], block_id: str = "block-1") -> None:
        self.path = str(directory / "rt.sock")
        self.reports: list[dict] = []
        self.polls: list[str] = []
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _reply(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                runtime.reports.append(json.loads(self.rfile.read(length)))
                self._reply({"id": block_id, "reason": "" if block_id else "not_live"})

            def do_GET(self) -> None:  # noqa: N802
                runtime.polls.append(self.path)
                self._reply({"state": answers.pop(0) if answers else "gone"})

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def get_request(self):
                request, _ = super().get_request()
                return request, ("local", 0)

        self.server = Server(self.path, Handler)
        # `shutdown` waits for the serving loop's next poll, half a second by default.
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def short_dir():
    # A Unix socket path must stay under ~108 bytes; pytest's tmp_path may not.
    directory = Path(tempfile.mkdtemp(prefix="ad-2271-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _run_hook(payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **env},
    )


@cache
def _hook_module():
    """The hook script, loaded once, for the cases whose subject is its dialogue
    with the runtime rather than how Claude starts it. Starting a fresh Python
    for each of those only paid for the imports again; the two tests below that
    use `_run_hook` still run it as Claude does."""
    spec = importlib.util.spec_from_file_location("claude_permission_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_hook_here(payload: dict, env: dict[str, str], monkeypatch, capsys):
    """`_run_hook`, in this process: (exit code, stdout)."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = _hook_module().main()
    return code, capsys.readouterr().out


def _env(short_dir: Path, root: Path) -> dict[str, str]:
    return {
        "AGENTDECK_CODEX_SOCKET": str(short_dir / "rt.sock"),
        "AGENTDECK_COMMAND_ARTIFACT_ROOT": str(root),
        "AGENTDECK_ACCOUNT_KEY": "claude_code:main",
        "AGENTDECK_WORKER_KEY": "owner/repo#7",
        "AGENTDECK_SESSION_TOKEN": "tok",
    }


_DENIED = {
    "hook_event_name": "PermissionDenied",
    "session_id": "s1",
    "tool_name": "Bash",
    "tool_input": {"command": "gh api -X DELETE repos/o/r"},
    "reason": "[Irreversible Destruction]",
}


def test_allow_once_answers_retry_after_waiting_out_pending_polls(
    short_dir, tmp_path, monkeypatch, capsys
):
    runtime = _FakeRuntime(short_dir, ["pending", "pending", "allowed"])
    try:
        code, out = _run_hook_here(_DENIED, _env(short_dir, tmp_path), monkeypatch, capsys)
    finally:
        runtime.close()

    assert code == 0
    assert json.loads(out) == {
        "hookSpecificOutput": {"hookEventName": "PermissionDenied", "retry": True}
    }
    assert runtime.reports == [
        {
            "account_key": "claude_code:main",
            "key": "owner/repo#7",
            "session_token": "tok",
            "tool_name": "Bash",
            "tool_input": {"command": "gh api -X DELETE repos/o/r"},
            "reason": "[Irreversible Destruction]",
        }
    ]
    assert len(runtime.polls) == 3
    assert "key=owner%2Frepo%237" in runtime.polls[0]


@pytest.mark.parametrize("answer", ["denied", "gone"])
def test_keep_blocked_or_a_vanished_block_prints_nothing(
    short_dir, tmp_path, answer, monkeypatch, capsys
):
    runtime = _FakeRuntime(short_dir, [answer])
    try:
        done = _run_hook_here(_DENIED, _env(short_dir, tmp_path), monkeypatch, capsys)
    finally:
        runtime.close()
    assert done == (0, "")


def test_a_refused_report_gives_up_at_once(short_dir, tmp_path, monkeypatch, capsys):
    runtime = _FakeRuntime(short_dir, [], block_id="")
    try:
        code, out = _run_hook_here(_DENIED, _env(short_dir, tmp_path), monkeypatch, capsys)
    finally:
        runtime.close()
    assert (code, out, runtime.polls) == (0, "", [])


def test_pre_tool_use_allows_only_a_granted_call_and_only_once(short_dir, tmp_path):
    env = _env(short_dir, tmp_path)
    call = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh api -X DELETE repos/o/r"},
    }
    write_grant(tmp_path, "claude_code:main", "owner/repo#7", "Bash", call["tool_input"])

    first = _run_hook(call, env)
    second = _run_hook(call, env)

    decision = json.loads(first.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"
    assert (second.returncode, second.stdout) == (0, "")


def test_a_garbled_payload_approves_nothing(short_dir, tmp_path):
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **_env(short_dir, tmp_path)},
    )
    assert (done.returncode, done.stdout) == (0, "")


# --- the prompt the boss sees ----------------------------------------------


def _render_interaction(interaction) -> str:
    from jinja2 import Environment, FileSystemLoader

    import agentdeck

    root = Path(agentdeck.__file__).parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(root)), autoescape=True)
    return env.get_template("partials/pending_interaction.html").render(
        interaction=interaction, session_key="claude_code:main:sid1"
    )


def test_a_block_offers_allow_once_and_keep_blocked_only():
    from agentdeck.models import PendingInteraction

    block = PendingInteraction.from_dict(
        {
            "id": "block-1",
            "kind": "classifier_block",
            "thread_id": "sid1",
            "title": "Claude's safety check blocked Bash",
            "message": "[Irreversible Destruction]",
            "command": "gh api -X DELETE repos/o/r",
            "decisions": ["accept", "decline"],
        }
    )

    html = _render_interaction(block)

    assert "safety check blocked Bash" in html
    assert "gh api -X DELETE repos/o/r" in html
    assert 'value="accept">Allow once</button>' in html
    assert 'value="decline">Keep blocked</button>' in html
    assert "Cancel turn" not in html and ">Decline<" not in html
