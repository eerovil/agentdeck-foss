from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentdeck.command_artifacts import (
    ACCOUNT_KEY_ENV,
    CONTEXT_DIR_ENV,
    SESSION_TOKEN_ENV,
    UNCOVERED_SUMMARY_LIMIT,
    ArtifactError,
    ArtifactStore,
    context_identity_from_environment,
    load_context,
    render_event,
    safe_to_rewrite,
    write_context,
)

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "command_output_hook.py"
TOKEN = "opaque-session-token"


def _context(*, mode: str = "enforce", visible: int = 8_000) -> dict:
    return {
        "account_key": "codex:main",
        "session_id": "session-1",
        "session_key": "codex:main:session-1",
        "worker_key": "eerovil/agentdeck#1484",
        "access_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "policy": {
            "mode": mode,
            "phase": "execute",
            "generation": "generation-1",
            "attempt": 1,
            "visible_tool_result_chars": visible,
            "artifact_retention_days": 30,
            "artifact_slice_max_bytes": 16,
            "artifact_slice_call_limit": 2,
        },
    }


def _capture(store: ArtifactStore, *, stdout: bytes = b"ok\n", stderr: bytes = b""):
    now = datetime.now(UTC)
    return store.capture(
        context=_context(),
        command="printf result",
        stdout=stdout,
        stderr=stderr,
        exit_code=0,
        term_signal=None,
        timed_out=False,
        started_at=now,
        finished_at=now + timedelta(milliseconds=12),
    )


def test_a_huge_single_line_is_private_complete_and_model_bounded(tmp_path):
    store = ArtifactStore(tmp_path)
    # Eight times the visible bound; nothing in the capture changes past that, and a
    # 40 MB line spent seconds in the failure scan proving no more.
    raw = b"x" * (64 * 1024)

    result = _capture(store, stdout=raw)

    metadata = result.metadata
    assert len(result.event) <= 8_000
    assert metadata["stdout_bytes"] == len(raw)
    assert metadata["stdout_sha256"] == hashlib.sha256(raw).hexdigest()
    artifact = tmp_path / "artifacts" / metadata["artifact_id"]
    assert (artifact.stat().st_mode & 0o777) == 0o700
    assert ((artifact / "stdout.bin").stat().st_mode & 0o777) == 0o600
    assert (artifact / "stdout.bin").read_bytes() == raw
    assert str(artifact) not in result.event


def test_failure_extraction_is_labelled_and_head_tail_stay_chronological():
    metadata = {
        "artifact_id": "opaque",
        "exit_code": 1,
        "signal": None,
        "timeout": False,
        "duration_ms": 4,
        "stdout_bytes": 18,
        "stderr_bytes": 11,
        "stdout_sha256": "a",
        "stderr_sha256": "b",
    }
    event = render_event(
        metadata,
        b"start\nsuccess noise\n",
        b"fatal error\n",
        max_chars=2_000,
    )

    assert "failure extraction (labelled extraction; not chronological):" in event
    assert event.index("[stderr] fatal error") < event.index("stdout chronological head")
    assert "start\nsuccess noise" in event


def test_head_tail_omission_is_deterministic():
    metadata = {
        "artifact_id": "opaque",
        "exit_code": 0,
        "signal": None,
        "timeout": False,
        "duration_ms": 4,
        "stdout_bytes": 4_000,
        "stderr_bytes": 0,
        "stdout_sha256": "a",
        "stderr_sha256": "b",
    }
    stdout = (b"ordinary output\n" * 250) + b"last line\n"

    first = render_event(metadata, stdout, b"", max_chars=8_000)
    second = render_event(metadata, stdout, b"", max_chars=8_000)

    assert first == second
    assert "bytes and" in first and "lines omitted" in first


def test_event_hard_bound_holds_even_below_the_clip_marker_size():
    metadata = {
        "artifact_id": "opaque",
        "exit_code": 0,
        "signal": None,
        "timeout": False,
        "duration_ms": 1,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "stdout_sha256": "a",
        "stderr_sha256": "b",
    }

    assert len(render_event(metadata, b"x" * 100, b"", max_chars=7)) == 7


def test_secrets_are_redacted_before_persistence_not_only_before_delivery(tmp_path):
    """Nothing under the store may hold a secret shape — #1484 says *before* persistence."""
    store = ArtifactStore(tmp_path)
    secrets_seen = [
        b"top-secret-token",
        b"also-secret",
        b"opaque-session-value",
        b"openai-secret",
        b"aws-secret",
        b"github-secret",
        b"json-secret-value",
    ]
    secret = (
        b"Authorization: Bearer top-secret-token\n"
        b"api_key=also-secret\nsession_token=opaque-session-value\n"
        b"OPENAI_API_KEY=openai-secret AWS_SECRET_ACCESS_KEY=aws-secret "
        b'GITHUB_TOKEN=github-secret {"api_key":"json-secret-value"}'
    )

    result = _capture(store, stdout=secret, stderr=secret)

    # The metadata still describes what the command really produced.
    assert result.metadata["stdout_bytes"] == len(secret)
    assert result.metadata["stdout_sha256"] == hashlib.sha256(secret).hexdigest()

    retained = Path(result.metadata["retained_paths"]["stderr"]).read_bytes()
    assert retained != secret
    assert hashlib.sha256(retained).hexdigest() == result.metadata["retained_stderr_sha256"]
    sliced = store.read_slice(
        result.metadata["artifact_id"],
        session_key="codex:main:session-1",
        card="eerovil/agentdeck#1484",
        session_token=TOKEN,
        stream="stderr",
        offset=0,
        max_bytes=16,
    )
    assert sliced["bytes"] == 16

    # Every byte the store wrote anywhere, not just the two stream files.
    on_disk = b"".join(
        path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    )
    for value in secrets_seen:
        assert value not in on_disk, value
        assert value.decode() not in result.event


def test_json_secret_shapes_are_redacted_from_excerpt_and_slice(tmp_path):
    store = ArtifactStore(tmp_path)
    result = _capture(store, stdout=b'{"api_key":"json-secret-value"}')

    assert "json-secret-value" not in result.event
    sliced = store.read_slice(
        result.metadata["artifact_id"],
        session_key="codex:main:session-1",
        card="eerovil/agentdeck#1484",
        session_token=TOKEN,
        stream="stdout",
        offset=0,
        max_bytes=16,
    )
    assert "json-secret" not in sliced["text"]


def test_bounded_slices_check_chat_card_bytes_and_call_count(tmp_path):
    store = ArtifactStore(tmp_path)
    artifact_id = _capture(store, stdout=b"0123456789abcdefmore").metadata["artifact_id"]

    with pytest.raises(PermissionError):
        store.read_slice(
            artifact_id,
            session_key="codex:other:session",
            card="eerovil/agentdeck#1484",
            session_token=TOKEN,
            stream="stdout",
            offset=0,
            max_bytes=4,
        )
    with pytest.raises(ArtifactError, match="byte limit"):
        store.read_slice(
            artifact_id,
            session_key="codex:main:session-1",
            card="eerovil/agentdeck#1484",
            session_token=TOKEN,
            stream="stdout",
            offset=0,
            max_bytes=17,
        )
    first = store.read_slice(
        artifact_id,
        session_key="codex:main:session-1",
        card="eerovil/agentdeck#1484",
        session_token=TOKEN,
        stream="stdout",
        offset=2,
        max_bytes=4,
    )
    assert first["text"] == "2345"
    store.read_slice(
        artifact_id,
        session_key="codex:main:session-1",
        card="eerovil/agentdeck#1484",
        session_token=TOKEN,
        stream="stdout",
        offset=6,
        max_bytes=4,
    )
    with pytest.raises(ArtifactError, match="call limit"):
        store.read_slice(
            artifact_id,
            session_key="codex:main:session-1",
            card="eerovil/agentdeck#1484",
            session_token=TOKEN,
            stream="stdout",
            offset=10,
            max_bytes=4,
        )


def test_model_slice_capability_is_bounded_to_one_artifact_and_not_stored_plaintext(tmp_path):
    store = ArtifactStore(tmp_path)
    first = _capture(store, stdout=b"first private result")
    second = _capture(store, stdout=b"second private result")
    capability = first.event.split(" --cap ", 1)[1].split(" ", 1)[0]

    assert capability not in json.dumps(first.metadata)
    assert capability not in (
        tmp_path / "artifacts" / first.metadata["artifact_id"] / "metadata.json"
    ).read_text()
    sliced = store.read_slice(
        first.metadata["artifact_id"],
        slice_capability=capability,
        stream="stdout",
        offset=0,
        max_bytes=5,
    )
    assert sliced["text"] == "first"
    with pytest.raises(PermissionError):
        store.read_slice(
            second.metadata["artifact_id"],
            slice_capability=capability,
            stream="stdout",
            offset=0,
            max_bytes=5,
        )


def test_cleanup_removes_an_artifact_past_its_own_retention_and_nothing_else(tmp_path):
    """#2154: retention is the artifact's own timestamp and policy. Nothing outside it — no
    phase state, no card record — decides whether it is still kept."""
    store = ArtifactStore(tmp_path)
    fresh = _capture(store)
    expired = _capture(store)
    metadata_path = tmp_path / "artifacts" / expired.metadata["artifact_id"] / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["finished_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    metadata_path.write_text(json.dumps(metadata))

    assert store.cleanup() == [expired.metadata["artifact_id"]]
    assert (tmp_path / "artifacts" / fresh.metadata["artifact_id"]).is_dir()
    assert store.cleanup() == []


def test_unknown_sensitive_output_uses_only_a_private_opaque_local_artifact(tmp_path):
    result = _capture(ArtifactStore(tmp_path), stdout=b"customer-private-value")
    artifact_id = result.metadata["artifact_id"]
    directory = tmp_path / "artifacts" / artifact_id

    assert len(artifact_id) >= 24
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "stdout.bin").stat().st_mode & 0o777) == 0o600
    assert "http://" not in result.event and "https://" not in result.event
    assert (directory / "stdout.bin").read_bytes() == b"customer-private-value"


def test_context_identity_is_opaque_and_restrictive(tmp_path):
    path = write_context(
        tmp_path,
        account_key="codex:main",
        session_id="session-1",
        worker_key="repo#1",
        session_token=TOKEN,
        policy={"mode": "observe"},
    )
    assert "session-1" not in path.name
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_context(tmp_path, "codex:main", "session-1")["worker_key"] == "repo#1"
    assert TOKEN not in path.read_text()


def test_pending_command_is_encrypted_one_shot_and_expired_pending_is_cleaned(tmp_path):
    store = ArtifactStore(tmp_path)
    request = store.prepare(_context(), {"command": "printf inline-secret-value"})
    pending = next((tmp_path / "pending").glob("*.json"))

    assert "inline-secret-value" not in pending.read_text()
    assert store.take_prepared(request)["tool_input"]["command"] == (
        "printf inline-secret-value"
    )
    assert not pending.exists()

    store.prepare(_context(), {"command": "printf abandoned-secret"})
    abandoned = next((tmp_path / "pending").glob("*.json"))
    old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(abandoned, (old, old))
    store.cleanup()
    assert not abandoned.exists()


def test_context_identity_comes_from_the_worker_environment(tmp_path, monkeypatch):
    write_context(
        tmp_path,
        account_key="codex:main",
        session_id="session-1",
        worker_key="repo#1",
        session_token=TOKEN,
        policy={"mode": "enforce"},
    )
    monkeypatch.setenv(CONTEXT_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(ACCOUNT_KEY_ENV, "codex:main")
    monkeypatch.setenv("AGENTDECK_WORKER_KEY", "repo#1")
    monkeypatch.setenv(SESSION_TOKEN_ENV, TOKEN)

    assert context_identity_from_environment() == (
        "codex:main:session-1",
        "repo#1",
        TOKEN,
    )


@pytest.mark.parametrize(
    ("tool_input", "reason"),
    [
        ({"command": "echo ok", "tty": True}, "interactive_or_streaming"),
        ({"command": "sleep 1 &"}, "background_shell"),
        ({"command": "echo ok", "session_id": 4}, "interactive_or_streaming"),
        ({"command": "echo ok", "shell": "/bin/zsh"}, "interactive_or_streaming"),
        ({"command": "echo ok", "login": True}, "interactive_or_streaming"),
    ],
)
def test_uncovered_shell_shapes_are_reported_instead_of_rewritten(tool_input, reason):
    assert safe_to_rewrite("Bash", tool_input) == (False, reason)


def _run_hook(tmp_path: Path, payload: dict, *, mode: str) -> subprocess.CompletedProcess:
    write_context(
        tmp_path,
        account_key="codex:main",
        session_id="session-1",
        worker_key="eerovil/agentdeck#1484",
        session_token=TOKEN,
        policy=_context(mode=mode)["policy"],
    )
    environment = {
        **os.environ,
        CONTEXT_DIR_ENV: str(tmp_path),
        ACCOUNT_KEY_ENV: "codex:main",
        SESSION_TOKEN_ENV: TOKEN,
        "PYTHONPATH": str(REPO / "src"),
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


@pytest.mark.parametrize(
    "command", ["gh api -X DELETE repos/o/r", "git status && git push --force", "echo $(curl -s x)"]
)
def test_enforce_leaves_a_github_reaching_claude_command_to_the_classifier(tmp_path, command):
    # The rewrite answers "allow", which would skip Claude's classifier (#2271).
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    hook = _run_hook(tmp_path, payload, mode="enforce")

    assert (hook.returncode, hook.stdout) == (0, "")


def test_enforce_still_rewrites_a_codex_github_command(tmp_path):
    payload = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh issue view 1"},
    }
    hook = _run_hook(tmp_path, payload, mode="enforce")

    decision = json.loads(hook.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_pre_hook_rewrites_a_covered_command_and_preserves_exit_and_streams(
    tmp_path, provider
):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "printf out; printf err >&2; exit 7"},
    }
    if provider == "codex":
        payload["turn_id"] = "turn-1"
    hook = _run_hook(tmp_path, payload, mode="enforce")
    updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    executed = subprocess.run(
        updated,
        shell=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            CONTEXT_DIR_ENV: str(tmp_path),
            ACCOUNT_KEY_ENV: "codex:main",
            "PYTHONPATH": str(REPO / "src"),
        },
        check=False,
    )
    assert executed.returncode == 7
    assert "AgentDeck command result" in executed.stdout
    metadata_files = list((tmp_path / "artifacts").glob("*/metadata.json"))
    metadata = json.loads(metadata_files[0].read_text())
    assert Path(metadata["retained_paths"]["stdout"]).read_bytes() == b"out"
    assert Path(metadata["retained_paths"]["stderr"]).read_bytes() == b"err"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_observe_hook_records_without_changing_provider_visible_output(tmp_path, provider):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "printf observed"},
        "tool_response": {"stdout": "observed", "stderr": "", "exit_code": 0},
    }
    if provider == "codex":
        payload["turn_id"] = "turn-1"
    hook = _run_hook(
        tmp_path,
        payload,
        mode="observe",
    )

    assert hook.stdout == ""
    if provider == "codex":
        assert list((tmp_path / "artifacts").glob("*/metadata.json")) == []
        row = json.loads((tmp_path / "uncovered.jsonl").read_text())
        assert row["reason"] == "provider_post_result_incomplete"
    else:
        metadata = json.loads(
            next((tmp_path / "artifacts").glob("*/metadata.json")).read_text()
        )
        assert Path(metadata["retained_paths"]["stdout"]).read_bytes() == b"observed"


def test_observe_hook_records_an_uncovered_shape_in_the_private_ledger(tmp_path):
    hook = _run_hook(
        tmp_path,
        {
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "sleep 1 & wait"},
        },
        mode="observe",
    )

    assert hook.stdout == ""
    row = json.loads((tmp_path / "uncovered.jsonl").read_text())
    assert row["reason"] == "background_shell"
    assert row["chat"] == "codex:main:session-1"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_representative_journal_pipeline_is_bounded_at_each_provider_seam(
    tmp_path, provider
):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            # The #1470 shape: a huge producer piped through a filter and a
            # bound. `grep` stands in for `rg`, which is not on every host.
            "command": "printf 'ok\\nfatal issue\\n' | grep fatal | head -1"
        },
    }
    if provider == "codex":
        payload["turn_id"] = "turn-1"
    hook = _run_hook(tmp_path, payload, mode="enforce")
    updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    executed = subprocess.run(
        updated,
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, CONTEXT_DIR_ENV: str(tmp_path), "PYTHONPATH": str(REPO / "src")},
        check=False,
    )

    assert executed.returncode == 0
    assert len(executed.stdout) <= 8_001
    metadata = json.loads(next((tmp_path / "artifacts").glob("*/metadata.json")).read_text())
    assert Path(metadata["retained_paths"]["stdout"]).read_bytes() == b"fatal issue\n"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_timeout_and_pipe_backpressure_are_preserved_at_each_provider_seam(
    tmp_path, provider, monkeypatch, capfd
):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        # Three times a pipe's 64 KiB buffer: a launcher that only read after the
        # command ended would hold it there until the deadline.
        "tool_input": {"command": "head -c 200000 /dev/zero; sleep 4"},
    }
    if provider == "codex":
        payload["turn_id"] = "turn-1"
        payload["tool_input"]["timeout_ms"] = 200
    else:
        payload["tool_input"]["timeout"] = 200
    write_context(
        tmp_path,
        account_key="codex:main",
        session_id="session-1",
        worker_key="eerovil/agentdeck#1484",
        session_token=TOKEN,
        policy=_context(mode="enforce")["policy"],
    )
    monkeypatch.setenv(CONTEXT_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(ACCOUNT_KEY_ENV, "codex:main")
    monkeypatch.setenv(SESSION_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    # The hook and its launcher run in this process: two interpreter starts cost more
    # than the rest of the test, and the tests around this one already run the
    # rewritten command through a real shell.
    hook = _hook_module()
    assert hook._main_hook() == 0
    updated = json.loads(capfd.readouterr().out)["hookSpecificOutput"]["updatedInput"]
    assert "timeout" not in updated and "timeout_ms" not in updated
    *launcher, run_id = shlex.split(updated["command"])
    assert launcher == [sys.executable, str(HOOK.resolve()), "--run"]

    # The deadline starts when bash does, so this host's own login profile (most of a
    # second on a busy desktop) would eat the window the output needs. That profile is
    # not the launcher's to test; everything after it is.
    real_popen = subprocess.Popen

    def without_login_profile(argv, **kwargs):
        assert argv[:2] == ["/bin/bash", "-lc"]
        return real_popen(["/bin/bash", "-c", *argv[2:]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", without_login_profile)
    assert hook.execute_prepared(tmp_path, run_id) == 124
    metadata = json.loads(next((tmp_path / "artifacts").glob("*/metadata.json")).read_text())
    assert metadata["timeout"] is True
    assert metadata["stdout_bytes"] == 200_000


def _hook_module():
    spec = importlib.util.spec_from_file_location("command_output_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_archive_command_leaves_no_raw_artifact(tmp_path):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "env | head -1"},
    }
    hook = _run_hook(tmp_path, payload, mode="enforce")
    updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    executed = subprocess.run(
        updated,
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, CONTEXT_DIR_ENV: str(tmp_path), "PYTHONPATH": str(REPO / "src")},
        check=False,
    )

    assert "artifact: not archived" in executed.stdout
    assert list((tmp_path / "artifacts").iterdir()) == []
    assert len(list((tmp_path / "events").glob("*.json"))) == 1
    assert ArtifactStore(tmp_path).model_visible_bytes("codex:main:session-1") > 0


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_launcher_preserves_pipefail_binary_and_signal_status(tmp_path, provider):
    def launch(root: Path, command: str) -> subprocess.CompletedProcess:
        root.mkdir()
        payload = {
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        if provider == "codex":
            payload["turn_id"] = "turn-1"
        hook = _run_hook(root, payload, mode="enforce")
        updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        return subprocess.run(
            updated,
            shell=True,
            # Pin bash rather than inherit `/bin/sh`: production always runs the
            # launcher as `/bin/bash -lc`, and dash (`/bin/sh` on Ubuntu, so on the
            # kube lane's image) reports a signalled child as `128 + signum`
            # instead of re-raising the signal on itself.
            executable="/bin/bash",
            capture_output=True,
            env={**os.environ, CONTEXT_DIR_ENV: str(root), "PYTHONPATH": str(REPO / "src")},
            check=False,
        )

    # Each case is two Python starts and a login shell, so run the two side by
    # side, each in its own context directory: one after the other they went
    # over the per-test time cap on a loaded pod (#3167).
    with ThreadPoolExecutor(max_workers=2) as pool:
        piped = pool.submit(
            launch, tmp_path / "piped", "set -o pipefail; printf '\\000\\377'; false | true"
        )
        signalled = pool.submit(launch, tmp_path / "signalled", "kill -TERM $$")
        result, signalled = piped.result(), signalled.result()
    assert result.returncode == 1
    metadata_path = next((tmp_path / "piped" / "artifacts").glob("*/metadata.json"))
    metadata = json.loads(metadata_path.read_text())
    assert Path(metadata["retained_paths"]["stdout"]).read_bytes() == b"\x00\xff"
    assert signalled.returncode == -signal.SIGTERM


def test_launcher_forwards_external_interrupt_to_the_command_group(tmp_path):
    child_pid = tmp_path / "child.pid"
    hook = _run_hook(
        tmp_path,
        {
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": f"echo $$ > {child_pid}; sleep 30",
            },
        },
        mode="enforce",
    )
    updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    launcher = subprocess.Popen(
        updated,
        shell=True,
        # Same reason as above: dash and bash differ over whether they `exec`
        # the command and over how they report its death by signal.
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, CONTEXT_DIR_ENV: str(tmp_path), "PYTHONPATH": str(REPO / "src")},
    )
    # The file appearing and the pid landing in it are two events, and under a
    # loaded parallel run we can win that race and read an empty file. Wait for
    # content we can actually parse, not merely for the path to exist. The
    # deadline is generous because a loaded host can be slow to start the
    # command at all; the loop leaves as soon as the pid is readable, and the
    # command sleeps for 30s, so the wait cannot outlive what it waits for.
    deadline = time.monotonic() + 10
    command_pid = None
    while command_pid is None and time.monotonic() < deadline:
        try:
            command_pid = int(child_pid.read_text().strip())
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
    written = child_pid.read_text() if child_pid.exists() else None
    assert command_pid is not None, f"no readable pid at {child_pid}: {written!r}"

    launcher.send_signal(signal.SIGTERM)
    launcher.communicate(timeout=3)

    assert launcher.returncode == -signal.SIGTERM
    with pytest.raises(ProcessLookupError):
        os.kill(command_pid, 0)


_SIGNAL_WHILE_STARTING = """
import os, signal, subprocess, sys, time
from pathlib import Path
from agentdeck import command_artifacts

root, run_id, child_pid = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
real_popen = subprocess.Popen

def popen_then_signal(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if child_pid.exists() and child_pid.read_text().strip():
            break
        time.sleep(0.01)
    # The interrupt lands before execute_prepared has its process back.
    os.kill(os.getpid(), signal.SIGTERM)
    return process

command_artifacts.subprocess.Popen = popen_then_signal
raise SystemExit(command_artifacts.execute_prepared(root, run_id))
"""


def test_launcher_forwards_an_interrupt_that_lands_while_the_command_starts(tmp_path):
    # #2872: the launcher used to install its forwarding handlers only after
    # the command had started, so a SIGTERM in that gap killed the launcher
    # with the default action and left the command's own session running.
    child_pid = tmp_path / "child.pid"
    hook = _run_hook(
        tmp_path,
        {
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": f"echo $$ > {child_pid}; sleep 30"},
        },
        mode="enforce",
    )
    updated = json.loads(hook.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    run_id = updated.rsplit("--run ", 1)[1].strip().strip("'")
    launcher = subprocess.run(
        [sys.executable, "-c", _SIGNAL_WHILE_STARTING, str(tmp_path), run_id, str(child_pid)],
        capture_output=True,
        env={**os.environ, CONTEXT_DIR_ENV: str(tmp_path), "PYTHONPATH": str(REPO / "src")},
        timeout=20,
        check=False,
    )

    assert launcher.returncode == -signal.SIGTERM
    assert b"AgentDeck command result" in launcher.stdout
    with pytest.raises(ProcessLookupError):
        os.kill(int(child_pid.read_text().strip()), 0)


def _capture_for(store: ArtifactStore, session_key: str, *, stdout: bytes = b"ok\n"):
    context = _context()
    context["session_key"] = session_key
    context["session_id"] = session_key.rsplit(":", 1)[-1]
    now = datetime.now(UTC)
    return store.capture(
        context=context,
        command="printf result",
        stdout=stdout,
        stderr=b"",
        exit_code=0,
        term_signal=None,
        timed_out=False,
        started_at=now,
        finished_at=now + timedelta(milliseconds=12),
    )


def test_coverage_index_totals_every_chat_exactly_as_the_per_chat_read(tmp_path):
    store = ArtifactStore(tmp_path)
    for _ in range(3):
        _capture_for(store, "codex:main:session-1")
    _capture_for(store, "codex:main:session-2")

    index = store.coverage_index()

    assert index.bytes_for("codex:main:session-1") == store.model_visible_bytes(
        "codex:main:session-1"
    )
    assert index.bytes_for("codex:main:session-2") == store.model_visible_bytes(
        "codex:main:session-2"
    )
    assert index.bytes_for("codex:main:session-1") > 0
    assert index.bytes_for("codex:main:never-ran") == 0


def test_coverage_index_honours_the_since_cut(tmp_path):
    store = ArtifactStore(tmp_path)
    _capture_for(store, "codex:main:session-1")

    future = datetime.now(UTC) + timedelta(hours=1)

    assert store.coverage_index().bytes_for("codex:main:session-1") > 0
    assert store.coverage_index(since=future).bytes_for("codex:main:session-1") == 0
    assert store.model_visible_bytes("codex:main:session-1", since=future) == 0


def test_coverage_index_reports_uncovered_rows_per_chat(tmp_path):
    store = ArtifactStore(tmp_path)
    for session_key in ("codex:main:session-1", "codex:main:session-2"):
        store.observe_uncovered(
            {
                "session_key": session_key,
                "worker_key": "eerovil/agentdeck#1553",
                "policy": {"phase": "execute"},
            },
            "sleep 1 & wait",
            "background_shell",
        )

    index = store.coverage_index()

    assert index.uncovered_for("codex:main:session-1") == store.uncovered_summary(
        "codex:main:session-1"
    )
    assert index.uncovered_for("codex:main:session-1")[0]["reason"] == "background_shell"
    assert index.uncovered_for("codex:main:session-2")[0]["reason"] == "background_shell"
    assert index.uncovered_for("codex:main:session-3") == []


def test_coverage_index_caps_uncovered_rows_per_chat(tmp_path):
    store = ArtifactStore(tmp_path)
    for _ in range(UNCOVERED_SUMMARY_LIMIT + 5):
        store.observe_uncovered(
            {
                "session_key": "codex:main:session-1",
                "worker_key": "eerovil/agentdeck#1553",
                "policy": {"phase": "execute"},
            },
            "sleep 1 & wait",
            "background_shell",
        )

    rows = store.coverage_index().uncovered_for("codex:main:session-1")

    assert len(rows) == UNCOVERED_SUMMARY_LIMIT
    assert rows == store.uncovered_summary("codex:main:session-1")


def test_an_unreadable_ledger_row_condemns_coverage_for_every_chat(tmp_path):
    store = ArtifactStore(tmp_path)
    store.observe_uncovered(
        {
            "session_key": "codex:main:session-1",
            "worker_key": "eerovil/agentdeck#1553",
            "policy": {"phase": "execute"},
        },
        "sleep 1 & wait",
        "background_shell",
    )
    with (tmp_path / "uncovered.jsonl").open("a") as handle:
        handle.write("not json\n")

    index = store.coverage_index()

    for session_key in ("codex:main:session-1", "codex:main:never-ran"):
        assert index.uncovered_for(session_key) == [
            {"reason": "unreadable_coverage_ledger", "command_fingerprint": ""}
        ]
        assert index.uncovered_for(session_key) == store.uncovered_summary(session_key)


def test_coverage_index_ignores_artifacts_captured_outside_enforce_mode(tmp_path):
    store = ArtifactStore(tmp_path)
    context = _context(mode="observe")
    context["session_key"] = "codex:main:session-9"
    now = datetime.now(UTC)
    store.capture(
        context=context,
        command="printf result",
        stdout=b"ok\n",
        stderr=b"",
        exit_code=0,
        term_signal=None,
        timed_out=False,
        started_at=now,
        finished_at=now + timedelta(milliseconds=12),
    )

    assert store.coverage_index().bytes_for("codex:main:session-9") == 0
    assert store.model_visible_bytes("codex:main:session-9") == 0


def test_the_generated_hooks_include_the_approval_cache(tmp_path):
    """The cache only ever sees a request if Codex is told to run its hook (#1738)."""
    from agentdeck.command_artifacts import approval_cache_hook_script, hook_groups, hook_script

    groups = hook_groups()
    assert set(groups) == {"PreToolUse", "PostToolUse", "PermissionRequest"}

    def commands(event):
        return [handler["command"] for group in groups[event] for handler in group["hooks"]]

    capture, approvals = str(hook_script()), str(approval_cache_hook_script())
    assert commands("PreToolUse") == [capture]
    # Both, and in this order: the capture hook must not be displaced by the one
    # that turns a deferred approval into a grant.
    assert commands("PostToolUse") == [capture, approvals]
    assert commands("PermissionRequest") == [approvals]


def test_the_hook_overrides_keep_every_handler_for_one_event():
    """A second `-c` for the same key would replace the first, not add to it."""
    from agentdeck.command_artifacts import approval_cache_hook_script, hook_script
    from agentdeck.providers.codex.appserver import _hook_config_overrides

    overrides = _hook_config_overrides()
    post = next(value for value in overrides if value.startswith("hooks.PostToolUse="))
    assert str(hook_script()) in post
    assert str(approval_cache_hook_script()) in post
    assert any(value.startswith("hooks.PermissionRequest=") for value in overrides)
