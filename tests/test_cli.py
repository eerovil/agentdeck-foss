from __future__ import annotations

import base64
import io
import subprocess

import httpx
import pytest

import agentdeck.__main__ as cli
from agentdeck.session_secrets import SessionSecret


def test_artifact_slice_uses_opaque_capability_and_writes_exact_bytes(monkeypatch, capfd):
    seen = {}

    def request(_socket, artifact_id, operation, payload):
        seen.update(artifact_id=artifact_id, operation=operation, payload=payload)
        return {"data_base64": base64.b64encode(b"binary\x00slice").decode()}

    monkeypatch.setattr(cli, "request_artifact", request)

    assert cli._artifact(
        [
            "slice", "opaque-id", "--cap", "opaque-slice-capability", "--stream", "stdout",
            "--offset", "4", "--max-bytes", "12",
        ]
    ) == 0
    assert capfd.readouterr().out.encode() == b"binary\x00slice"
    assert seen == {
        "artifact_id": "opaque-id",
        "operation": "slice",
        "payload": {
            "slice_capability": "opaque-slice-capability",
            "stream": "stdout",
            "offset": 4,
            "max_bytes": 12,
        },
    }


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://127.0.0.1:8756/api/delegations/x")
            raise httpx.HTTPStatusError(
                "error",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self):
        self.posted = None
        self.post_timeout = None
        self.poll_timeouts = []
        self.polls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def post(self, url, *, json, timeout):
        self.posted = (url, json)
        self.post_timeout = timeout
        return _Response(
            {
                "id": "delegation-1",
                "status_url": "/api/delegations/delegation-1",
                "timeout_s": 14_400.0,
            }
        )

    def get(self, url, *, timeout):
        self.poll_timeouts.append(timeout)
        self.polls += 1
        if self.polls == 1:
            return _Response(
                {
                    "state": "running",
                    "session_url": "/sessions/codex:test:thread-1",
                }
            )
        return _Response(
            {
                "state": "complete",
                "session_url": "/sessions/codex:test:thread-1",
                "final_message": "Codex finished the delegated task.",
            }
        )


def test_secret_run_injects_only_child_environment_and_redacts_output(monkeypatch, capfd):
    value = "exact-secret-value"
    claimed = SessionSecret(
        ticket="ticket",
        session_key="codex:test:sid",
        name="SERVICE_TOKEN",
        value=value,
        action_id="action",
        expires_at=0.0,
    )
    seen = {}

    monkeypatch.setattr(cli, "claim_runtime_secret", lambda *_args: claimed)

    def run(command, **kwargs):
        seen["command"] = command
        seen["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            7,
            stdout=f"stdout {value}\n".encode(),
            stderr=f"stderr {value}\n".encode(),
        )

    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)

    assert cli._secret_run(["ticket", "--", "program", "arg"]) == 7
    captured = capfd.readouterr()
    assert seen["command"] == ["program", "arg"]
    assert seen["environment"]["SERVICE_TOKEN"] == value
    assert "SERVICE_TOKEN" not in cli.os.environ
    assert value not in captured.out + captured.err
    assert "[redacted secret SERVICE_TOKEN]" in captured.out
    assert "[redacted secret SERVICE_TOKEN]" in captured.err


def test_delegate_cli_sends_stdin_and_prints_final_message(tmp_path, monkeypatch, capsys):
    client = _Client()
    client_kwargs = None

    def make_client(**kwargs):
        nonlocal client_kwargs
        client_kwargs = kwargs
        return client

    monkeypatch.setattr(cli.httpx, "Client", make_client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)

    cli._delegate(
        [
            "--cwd",
            str(tmp_path),
            "--sandbox",
            "read-only",
            "--model",
            "gpt-test",
            "--effort",
            "high",
        ]
    )

    captured = capsys.readouterr()
    assert captured.out == "Codex finished the delegated task.\n"
    assert "delegation-1 started" in captured.err
    assert client_kwargs == {}
    assert client.post_timeout.connect == 10.0
    assert client.post_timeout.read == 30.0
    assert client.poll_timeouts[0].connect == 10.0
    assert client.poll_timeouts[0].read == 60.0
    assert client.posted == (
        "http://127.0.0.1:8756/api/delegations",
        {
            "cwd": str(tmp_path),
            "message": "Inspect the change.\n",
            "sandbox": "read-only",
            "model": "gpt-test",
            "effort": "high",
        },
    )


def test_delegate_cli_retries_transient_status_timeout(tmp_path, monkeypatch, capsys):
    class FlakyClient(_Client):
        def get(self, url, *, timeout):
            self.poll_timeouts.append(timeout)
            self.polls += 1
            if self.polls == 1:
                raise httpx.ReadTimeout("loaded server")
            return _Response(
                {
                    "state": "complete",
                    "final_message": "Finished after the slow poll.",
                }
            )

    client = FlakyClient()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cli._delegate(["--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert captured.out == "Finished after the slow poll.\n"
    assert "request failed" not in captured.err
    assert client.polls == 2


def test_delegate_cli_uses_safe_budget_with_older_server(
    tmp_path, monkeypatch, capsys
):
    class OlderServerClient(_Client):
        def post(self, url, *, json, timeout):
            response = super().post(url, json=json, timeout=timeout)
            response.payload.pop("timeout_s")
            return response

    client = OlderServerClient()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cli._delegate(["--cwd", str(tmp_path)])

    assert capsys.readouterr().out == "Codex finished the delegated task.\n"


def test_delegate_cli_reports_server_side_failure(tmp_path, monkeypatch, capsys):
    class FailedClient(_Client):
        def get(self, url, *, timeout):
            return _Response({"state": "failed", "reason": "runtime disconnected"})

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: FailedClient())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit) as raised:
        cli._delegate(["--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert "AgentDeck delegation failed: runtime disconnected" in captured.err


def test_delegate_cli_prints_the_decks_reason_for_a_refusal(tmp_path, monkeypatch, capsys):
    # httpx's own message drops the deck's detail, so reviewers retried blind (#3711).
    reason = "refusing to delegate into the live deploy checkout — pass --cwd a worktree"

    class RefusingClient(_Client):
        def post(self, url, *, json, timeout):
            return _Response({"detail": reason}, status_code=422)

    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: RefusingClient())
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))

    with pytest.raises(SystemExit) as raised:
        cli._delegate(["--cwd", str(tmp_path)])

    assert raised.value.code == 1
    assert f"AgentDeck delegation refused (HTTP 422): {reason}" in capsys.readouterr().err


def test_delegate_cli_expired_budget_identifies_live_job(
    tmp_path, monkeypatch, capsys
):
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.setattr(cli.time, "monotonic", iter((0.0, 0.0, 0.0, 14_400.0)).__next__)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit) as raised:
        cli._delegate(["--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert "server-side job may still be running" in captured.err
    assert "server last reported it as running" in captured.err
    assert "Delegation id: delegation-1" in captured.err
    assert (
        "Status URL: http://127.0.0.1:8756/api/delegations/delegation-1"
        in captured.err
    )


def test_delegate_cli_stops_at_once_when_the_deck_forgot_the_delegation(
    tmp_path, monkeypatch, capsys
):
    """#2582: a 404 from a deck that keeps delegations is final, so say so and stop."""

    class ForgottenClient(_Client):
        def get(self, url, *, timeout):
            self.polls += 1
            if self.polls == 1:
                return _Response(
                    {"state": "running", "session_url": "/chats/child-chat"}
                )
            return _Response({"detail": "unknown delegation"}, status_code=404)

    client = ForgottenClient()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with pytest.raises(SystemExit) as raised:
        cli._delegate(["--cwd", str(tmp_path)])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert client.polls == 2
    assert "no longer knows delegation delegation-1" in captured.err
    assert "http://127.0.0.1:8756/chats/child-chat" in captured.err
    assert "Do not relaunch it before checking" in captured.err


def test_delegate_cli_keeps_retrying_other_poll_errors(tmp_path, monkeypatch, capsys):
    class BusyClient(_Client):
        def get(self, url, *, timeout):
            self.polls += 1
            if self.polls == 1:
                return _Response({"detail": "Not Found"}, status_code=404)
            if self.polls == 2:
                return _Response({"detail": "boom"}, status_code=500)
            return _Response({"state": "complete", "final_message": "Done anyway."})

    client = BusyClient()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Inspect the change.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cli._delegate(["--cwd", str(tmp_path)])

    assert capsys.readouterr().out == "Done anyway.\n"
    assert client.polls == 3


def test_delegate_cli_captures_parent_session_from_env(tmp_path, monkeypatch):
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-uuid")

    cli._delegate(["--cwd", str(tmp_path)])

    assert client.posted[1]["parent_session_id"] == "parent-uuid"


def test_delegate_cli_prefers_the_owned_turns_session_key(tmp_path, monkeypatch):
    # An owned Claude turn carries both; the full key is the unambiguous one, and
    # only it survives an account whose ids collide with another provider's.
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "claude_code:main:parent-uuid")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-uuid")

    cli._delegate(["--cwd", str(tmp_path)])

    assert client.posted[1]["parent_session_id"] == "claude_code:main:parent-uuid"


def test_delegate_cli_sends_a_parent_session_token(tmp_path, monkeypatch):
    # The Codex path: no environment variable can name the calling thread, so the
    # deck-minted token is the parent identity and the server resolves it.
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cli._delegate(["--cwd", str(tmp_path), "--parent-session-token", "tok-parent"])

    assert client.posted[1]["parent_session_token"] == "tok-parent"
    assert "parent_session_id" not in client.posted[1]


def test_delegate_cli_explicit_parent_overrides_env(tmp_path, monkeypatch):
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-uuid")

    cli._delegate(
        [
            "--cwd",
            str(tmp_path),
            "--parent-session",
            "flag-uuid",
            "--parent-session-token",
            "tok-parent",
        ]
    )

    # An explicit --parent-session is the caller acting on another chat's behalf,
    # so it outranks even a token that would resolve.
    assert client.posted[1]["parent_session_id"] == "flag-uuid"
    assert "parent_session_token" not in client.posted[1]


def test_delegate_cli_warns_when_nothing_names_the_parent(tmp_path, monkeypatch, capsys):
    # The silent-loss case from #680: the delegation works, so nothing fails, and
    # without the warning its work silently fails to roll up into the parent.
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cli._delegate(["--cwd", str(tmp_path)])

    err = capsys.readouterr().err
    assert "no parent session identified" in err
    assert "stay out of Deckhand" in err
    assert "--parent-session-token" in err
    assert "parent_session_id" not in client.posted[1]
    assert "parent_session_token" not in client.posted[1]


def test_delegate_cli_warns_when_the_server_resolved_no_parent(
    tmp_path, monkeypatch, capsys
):
    class UnresolvedParentClient(_Client):
        def post(self, url, *, json, timeout):
            super().post(url, json=json, timeout=timeout)
            return _Response(
                {
                    "id": "delegation-1",
                    "status_url": "/api/delegations/delegation-1",
                    "timeout_s": 14_400.0,
                    "parent_session": None,
                }
            )

    client = UnresolvedParentClient()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)

    cli._delegate(["--cwd", str(tmp_path), "--parent-session-token", "tok-stale"])

    err = capsys.readouterr().err
    assert "could not resolve a parent session from --parent-session-token" in err
    assert "stays out of Deckhand" in err


def test_delegate_cli_stays_quiet_about_parentage_on_an_older_server(
    tmp_path, monkeypatch, capsys
):
    # _Client answers without the parent_session field, like a server that
    # predates it. An absent field is no evidence, so do not cry wolf.
    client = _Client()
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("Do it.\n"))
    monkeypatch.setattr(cli.time, "sleep", lambda delay: None)

    cli._delegate(["--cwd", str(tmp_path), "--parent-session", "flag-uuid"])

    assert "could not resolve a parent session" not in capsys.readouterr().err


def _submitted_jobs(monkeypatch, tmp_path) -> list[dict]:
    """Capture what `agentdeck job submit` asks the runtime queue for."""
    calls: list[dict] = []

    def submit_job(_socket, **kwargs):
        calls.append(kwargs)
        return {
            "id": "job-1",
            "log_path": str(tmp_path / "job-1.log"),
            "state": "queued",
        }

    monkeypatch.setattr(cli, "submit_job", submit_job)
    return calls


def test_job_submit_falls_back_to_kube_when_the_runtime_predates_desktop(
    tmp_path, monkeypatch, capsys
):
    """The runtime outlives a deploy, so the CLI can name desktop to a runtime
    that refuses it (#2297), or name the desktop lane by its new name (#3623).
    That refusal queued nothing, so it asks again by a name the runtime knows."""
    calls: list[dict] = []

    def submit_job(_socket, **kwargs):
        calls.append(kwargs)
        if kwargs["lane"] == "desktop":
            raise cli.JobClientError("unknown lane 'desktop'; expected one of local, kube, mac")
        return {"id": "job-1", "log_path": str(tmp_path / "job-1.log"), "state": "queued"}

    monkeypatch.setattr(cli, "submit_job", submit_job)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    worktree = tmp_path / "worktree"
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "kube_pytest.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    assert cli._job(["submit", "--cwd", str(worktree), "--", "pytest", "-q"]) == 0
    # A runtime from before #3623 knows the desktop lane by its old name.
    assert [call["lane"] for call in calls] == ["desktop", "old-desktop"]
    assert calls[-1]["lane_defaulted"] is True
    assert "in a pod on the desktop" in capsys.readouterr().out

    # A named lane is asked for by its old name too; nothing else is chosen for it.
    calls.clear()
    assert cli._job(["submit", "--where", "desktop", "--cwd", str(worktree), "--", "pytest"]) == 0
    assert [(call["lane"], call["lane_defaulted"]) for call in calls] == [
        ("desktop", False),
        ("old-desktop", False),
    ]

    # With no old name to ask by, the default goes to kube and a named lane fails.
    lanes = tmp_path / "job-lanes.toml"
    lanes.write_text("[lanes.kube]\n[lanes.desktop]\n", encoding="utf-8")
    monkeypatch.setenv("AGENTDECK_JOB_LANES", str(lanes))
    calls.clear()
    assert cli._job(["submit", "--cwd", str(worktree), "--", "pytest", "-q"]) == 0
    assert [call["lane"] for call in calls] == ["desktop", "kube"]
    calls.clear()
    with pytest.raises(SystemExit):
        cli._job(["submit", "--where", "desktop", "--cwd", str(worktree), "--", "pytest"])
    assert [call["lane"] for call in calls] == ["desktop"]
    assert "unknown lane 'desktop'" in capsys.readouterr().err


def _claude_accounts(monkeypatch, tmp_path, labels: list[str]) -> None:
    """Configure these claude_code accounts, each with a config dir of its own.

    Also clears ``$AGENTDECK_ACCOUNT_KEY``: these tests exercise what the
    *config* says, and the suite itself often runs inside an owned worker whose
    environment names an account, which would otherwise answer first.
    """
    from agentdeck.config import AppConfig

    config = AppConfig.model_validate(
        {
            "accounts": [
                {
                    "provider": "claude_code",
                    "label": label,
                    "config_dir": str(tmp_path / f"account-{label}"),
                }
                for label in labels
            ]
        }
    )
    monkeypatch.setattr(cli, "load_config", lambda _p=None: config)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.delenv("AGENTDECK_ACCOUNT_KEY", raising=False)


def _claude_transcript(tmp_path, label: str, session_id: str) -> None:
    """Give one account the transcript that proves it holds this chat."""
    project = tmp_path / f"account-{label}" / "projects" / "-home-eero-agentdeck"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")


def test_job_submit_names_a_claude_chat_from_its_bare_session_id(
    tmp_path, monkeypatch, capsys
):
    # #1447: a Claude chat without $AGENTDECK_SESSION_KEY is still identifiable,
    # and losing the completion message sends the agent back to polling.
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] == "claude_code:main:sid-xyz"
    captured = capsys.readouterr()
    assert "end your turn" in captured.out
    assert "nothing will announce" not in captured.err


def test_job_submit_sends_a_suite_to_a_pod_unless_told_otherwise(tmp_path, monkeypatch, capsys):
    """What an agent types by default has to be what the host wants.

    `agentdeck job submit -- pytest -q` from the agentdeck checkout goes to a
    pod on desktop (#2297), and the line it prints says so. Coming back to this
    host is deliberate: `--where local`, for a cluster that is backed up or down.
    """
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    worktree = tmp_path / "worktree"
    (worktree / "scripts").mkdir(parents=True)
    (worktree / "scripts" / "kube_pytest.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    assert cli._job(["submit", "--cwd", str(worktree), "--", "pytest", "-q"]) == 0
    assert calls[-1]["lane"] == "desktop"
    # Chosen for it, so a busy, full or switched-off desktop may move it to the
    # dev cluster (#2297)...
    assert calls[-1]["lane_defaulted"] is True
    assert "in a pod on the desktop" in capsys.readouterr().out

    # ...where an explicit `--where` is obeyed.
    cli._job(["submit", "--where", "desktop", "--cwd", str(worktree), "--", "pytest", "-q"])
    assert (calls[-1]["lane"], calls[-1]["lane_defaulted"]) == ("desktop", False)
    capsys.readouterr()
    cli._job(["submit", "--where", "kube", "--cwd", str(worktree), "--", "pytest", "-q"])
    assert (calls[-1]["lane"], calls[-1]["lane_defaulted"]) == ("kube", False)
    assert "in a pod on the kube cluster" in capsys.readouterr().out

    cli._job(["submit", "--where", "local", "--cwd", str(worktree), "--", "pytest", "-q"])
    assert calls[-1]["lane"] == "local"
    assert "in a pod on" not in capsys.readouterr().out

    cli._job(["submit", "--where", "mac", "--cwd", str(worktree), "--", "pytest", "-q"])
    assert calls[-1]["lane"] == "mac"
    assert "Mac's Kubernetes" in capsys.readouterr().out

    # A build has nowhere else to go, so it runs here rather than being refused.
    cli._job(["submit", "--cwd", str(worktree), "--", "make", "build"])
    assert calls[-1]["lane"] == "local"

    # ...and so does a suite from a checkout the lane does not serve.
    cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"])
    assert calls[-1]["lane"] == "local"

    # A relative --cwd is this shell's directory, not the runtime's (#3338): sent as
    # typed, `--cwd .` from a worktree tested the deck's own checkout.
    monkeypatch.chdir(tmp_path)
    cli._job(["submit", "--cwd", "worktree", "--", "pytest", "-q"])
    assert (calls[-1]["cwd"], calls[-1]["lane"]) == (str(worktree), "desktop")
    monkeypatch.chdir(worktree)
    cli._job(["submit", "--cwd", ".", "--", "pytest", "-q"])
    assert calls[-1]["cwd"] == str(worktree)


def test_job_submit_prefers_the_owned_turns_session_key(tmp_path, monkeypatch):
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "codex:work:thread-7")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"])

    assert calls[0]["session_key"] == "codex:work:thread-7"


def test_job_submit_names_the_chat_from_the_account_that_spawned_it(
    tmp_path, monkeypatch, capsys
):
    """#2092: a fresh owned worker has no session key, but it does have its account.

    Nobody knows a worker's session id at spawn, so the runtime cannot inject
    ``$AGENTDECK_SESSION_KEY``. It does inject the account that spawned it, and
    the key is that plus the id — so a poller worker on a two-account host gets
    its job announced instead of the refusal that used to be certain.
    """
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main", "alt"])
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("AGENTDECK_ACCOUNT_KEY", "claude_code:alt")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] == "claude_code:alt:sid-xyz"
    captured = capsys.readouterr()
    assert "end your turn" in captured.out
    assert "nothing will announce" not in captured.err


def test_job_submit_ignores_another_providers_account_key(tmp_path, monkeypatch):
    """A Claude chat running inside a Codex worker is not that worker's chat.

    The account key names whoever spawned the *process*; pinning a Claude
    session id behind a ``codex:`` label would invent a key no session has, so
    the id falls through to the accounts the config knows instead.
    """
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("AGENTDECK_ACCOUNT_KEY", "codex:work")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"])

    assert calls[0]["session_key"] == "claude_code:main:sid-xyz"


def test_job_submit_names_the_account_that_holds_the_chat(tmp_path, monkeypatch, capsys):
    """With several accounts and no account key, the transcript settles it.

    This is a person's own terminal chat rather than an owned worker: only one
    of the configured accounts has ever held this id, so counting accounts is
    answering a question the accounts themselves can answer exactly.
    """
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main", "alt"])
    _claude_transcript(tmp_path, "alt", "sid-xyz")
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] == "claude_code:alt:sid-xyz"
    assert "nothing will announce" not in capsys.readouterr().err


def test_job_submit_says_no_account_holds_that_chat(tmp_path, monkeypatch, capsys):
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main", "alt"])
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] is None
    err = capsys.readouterr().err
    # A stale id and a genuinely shared one call for different fixes, so the
    # refusal has to say which one happened (#2092).
    assert "no chat with that id exists" in err
    assert "genuinely ambiguous" not in err


def test_job_submit_declines_to_guess_an_ambiguous_claude_account(
    tmp_path, monkeypatch, capsys
):
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main", "alt"])
    _claude_transcript(tmp_path, "main", "sid-xyz")
    _claude_transcript(tmp_path, "alt", "sid-xyz")
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-xyz")

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] is None
    err = capsys.readouterr().err
    assert "2 claude_code accounts" in err
    assert "genuinely ambiguous" in err


def test_job_submit_says_it_cannot_announce_without_recommending_polling(
    tmp_path, monkeypatch, capsys
):
    calls = _submitted_jobs(monkeypatch, tmp_path)
    _claude_accounts(monkeypatch, tmp_path, ["main"])
    monkeypatch.delenv("AGENTDECK_SESSION_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    assert cli._job(["submit", "--cwd", str(tmp_path), "--", "pytest", "-q"]) == 0

    assert calls[0]["session_key"] is None
    captured = capsys.readouterr()
    assert "nothing will announce this job" in captured.err
    assert "nothing in this environment names a chat" in captured.err
    assert "accounts are configured" not in captured.err
    assert str(tmp_path / "job-1.log") in captured.err
    # The repo's own rule is that an agent never sits in a polling loop, so the
    # fallback must not send it into one.
    assert "agentdeck job status" not in captured.out + captured.err
