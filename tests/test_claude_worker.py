from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import conftest
import pytest
from worker_harness import FakeProcess, FakeStdin, FakeStdout  # noqa: F401
from worker_harness import claude_host as _host
from worker_harness import settle as _settle

from agentdeck.classifier_grants import consume_grant, write_grant
from agentdeck.command_artifacts import load_context
from agentdeck.models import (
    SNAPSHOT_ACTIVITY_CLOCK,
    SNAPSHOT_CAPABILITIES_KEY,
    Account,
)
from agentdeck.providers import account_health
from agentdeck.providers.claude_code.held import HolderInfo, kill_holder
from agentdeck.providers.claude_code.worker import (
    IDLE_RECLAIM_GRACE_S,
    LOGIN_REFRESH_RETRY_DELAY_S,
    NETWORK_RETRY_DELAY_S,
    ClaudeWorkerHost,
    WorkerRecord,
)
from agentdeck.providers.instructions import (
    AGENTDECK_SESSION_INSTRUCTIONS,
    agentdeck_session_instructions,
    relocation_notice,
)


async def test_claude_worker_snapshot_declares_that_it_keeps_an_activity_clock(tmp_path):
    """#1407: the poller reads this to tell a missing clock from a runtime without one."""
    host, _ = _host(tmp_path)

    assert SNAPSHOT_ACTIVITY_CLOCK in host.snapshot()[SNAPSHOT_CAPABILITIES_KEY]
    await host.stop()


async def test_deliver_requires_cwd_for_new_key(tmp_path):
    host, _ = _host(tmp_path)
    result = await host.deliver("issue-1", "work it", delegated=False)
    assert not result.accepted
    assert result.reason == "cwd_required"


async def test_deliver_spawns_and_captures_session_id(tmp_path):
    host, spawned = _host(tmp_path)
    result = await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    assert result.accepted and result.action == "spawned"
    assert result.session_id == "sid-auto-1"
    proc = spawned[0]["process"]
    assert spawned[0]["kwargs"]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert spawned[0]["kwargs"]["cwd"] == str(tmp_path)
    assert proc.stdin.lines[0]["message"]["content"][0]["text"] == "work it"

    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-123"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["session_id"] == "sid-123"
    assert host.snapshot()["workers"]["issue-1"]["session_token"]
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is True

    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is False
    await host.stop()


async def test_rejected_delivery_does_not_change_live_artifact_policy(tmp_path):
    artifact_root = tmp_path / "artifacts"
    host, _spawned = _host(tmp_path, artifact_root=artifact_root)
    started = await host.deliver(
        "issue-1",
        "work it",
        cwd=str(tmp_path),
        delegated=False,
        session_token="opaque-session-token",
        command_artifacts={"mode": "observe"},
    )
    assert started.accepted and started.session_id

    rejected = await host.deliver(
        "issue-1",
        "stale update",
        expected_session_id="not-the-current-session",
        delegated=False,
        command_artifacts={"mode": "enforce"},
    )

    assert not rejected.accepted and rejected.reason == "worker_changed"
    assert host._records["issue-1"].command_artifacts == {"mode": "observe"}
    # Only the poller's kanban workers ever had a capture context published (#3741).
    assert load_context(artifact_root, "claude_code:test", started.session_id) is None
    await host.stop()


async def test_a_spawn_does_not_explicitly_trust_project_mcp_config(tmp_path):
    (tmp_path / ".mcp.json").write_text(
        '{"mcpServers": {"untrusted": {"command": "arbitrary"}}}'
    )
    host, spawned = _host(tmp_path)

    await host.deliver("interactive", "inspect", cwd=str(tmp_path), delegated=False)

    assert not any(arg.startswith("--mcp-config") for arg in spawned[0]["args"])
    await host.stop()


async def test_active_only_claude_followup_refuses_an_ended_turn_in_a_live_process(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "owner/repo#1", "first", cwd=str(tmp_path), delivery_id="one", delegated=False
    )
    process = spawned[0]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert process.returncode is None

    result = await host.deliver(
        "owner/repo#1",
        "late correction",
        delivery_id="two",
        require_active=True,
        delegated=False,
    )

    assert not result.accepted and result.reason == "worker_inactive"
    assert len(spawned) == 1
    await host.stop()


async def test_active_only_claude_followup_refuses_if_turn_ends_while_loading_images(
    tmp_path, monkeypatch
):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "owner/repo#1", "first", cwd=str(tmp_path), delivery_id="one", delegated=False
    )
    process = spawned[0]["process"]

    async def end_turn(_function, _path):
        process.stdout.emit({"type": "result", "subtype": "success"})
        await _settle()
        return {"type": "image", "source": {}}

    monkeypatch.setattr(asyncio, "to_thread", end_turn)
    result = await host.deliver(
        "owner/repo#1", "late correction", images=["shot.png"], delivery_id="two",
        require_active=True, delegated=False,
    )

    assert not result.accepted and result.reason == "worker_inactive"
    assert len(process.stdin.lines) == 1
    await host.stop()


async def test_autonomous_output_after_result_reopens_turn_for_idle_fence(tmp_path):
    """#907: a background task can wake Claude after its prior result event.

    That continuation has no new AgentDeck stdin delivery. Its first user or
    assistant stream message must therefore reopen the runtime lifecycle, or a
    routine idle-only revival can land while Claude is already working again.
    """
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    process = spawned[0]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is False

    process.stdout.emit(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "task finished"}],
            },
        }
    )
    await _settle()

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["turn_active"] is True
    assert worker["effective_activity"] is True
    result = await host.deliver(
        "issue-1", "routine revival", require_idle=True, delegated=False
    )
    assert not result.accepted and result.reason == "active_turn"
    await host.stop()


async def test_poller_continuation_adopts_an_existing_background_shell_task(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "owner/repo#1",
        "start",
        cwd=str(tmp_path),
        delegated=False,
    )
    process = spawned[0]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    launch = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result"}]},
        "toolUseResult": {"backgroundTaskId": "bwait1"},
    }
    transcript.write_text(json.dumps(launch) + "\n")

    process.returncode = 0
    process.stdout.queue.put_nowait(b"")
    await _settle()
    revived = await host.deliver(
        "owner/repo#1", "revive", require_idle=True, delegated=False
    )
    assert revived.accepted and revived.action == "revived"
    assert len(spawned) == 2
    continuation = spawned[1]["process"].stdin.lines[0]["message"]["content"][0]["text"]
    assert "task IDs: bwait1" in continuation
    assert "Do not launch another copy" in continuation
    await host.stop()


async def test_poller_continuation_finds_a_launch_before_the_bounded_scan_window(
    tmp_path,
):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "owner/repo#1",
        "start",
        cwd=str(tmp_path),
        delegated=False,
    )
    process = spawned[0]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    launch = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result"}]},
        "toolUseResult": {"backgroundTaskId": "bwait-old"},
    }
    transcript.write_text(
        json.dumps(launch) + "\n" + json.dumps({"padding": "x" * 600_000}) + "\n"
    )

    continued = await host.deliver(
        "owner/repo#1", "continue", require_idle=True, delegated=False
    )

    assert continued.accepted and continued.action == "queued"
    message = process.stdin.lines[-1]["message"]["content"][0]["text"]
    assert "task IDs: bwait-old" in message
    await host.stop()


async def test_rollover_refuses_outstanding_background_shell_work(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "owner/repo#1", "start", cwd=str(tmp_path), delegated=False
    )
    process = spawned[0]["process"]
    process.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "toolUseResult": {"backgroundTaskId": "background-shell-1"},
            }
        )
        + "\n"
    )

    rolled = await host.deliver(
        "owner/repo#1",
        "handoff",
        cwd=str(tmp_path),
        fresh=True,
        delivery_id="context-rollover",
        require_idle=True,
        expected_session_id=first.session_id,
        context_rollover=True,
        delegated=False,
    )

    assert not rolled.accepted and rolled.reason == "active_turn"
    assert len(spawned) == 1
    await host.stop()


async def test_clean_rollover_spawn_failure_restores_source_record(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1",
        "first",
        cwd=str(tmp_path),
        model="model-a",
        effort="high",
        permission_mode="default",
        delivery_id="one",
        delegated=False,
    )
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    async def fail_spawn(*_args, **_kwargs):
        raise OSError("spawn failed")

    host._process_factory = fail_spawn
    failed = await host.deliver(
        "issue-1",
        "handoff",
        cwd=str(tmp_path),
        fresh=True,
        delivery_id="two",
        require_idle=True,
        expected_session_id=first.session_id,
        context_rollover=True,
        delegated=False,
    )

    restored = host._records["issue-1"]
    assert not failed.accepted and failed.retryable_same_delivery
    assert failed.successor_created is False
    assert restored.session_id == first.session_id
    assert restored.model == "model-a" and restored.effort == "high"
    assert "one" in restored.deliveries
    assert restored.pending_context_delivery_id == "two"
    await host.stop()


async def test_rollover_target_retry_ignores_the_frozen_source_timestamp(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1",
        "first",
        cwd=str(tmp_path),
        delivery_id="one",
        delegated=False,
    )
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    spawned[0]["process"].returncode = 0
    spawned[0]["process"].stdout.queue.put_nowait(b"")
    await _settle()

    rec = host._records["issue-1"]
    source_session_id = first.session_id
    rec.session_id = "target-id"
    rec.last_result_at = 99.0
    rec.pending_context_delivery_id = "two"
    rec.pending_context_operation = "rollover"
    rec.pending_context_source_session_id = source_session_id
    host._save_state()

    retried = await host.deliver(
        "issue-1",
        "handoff",
        cwd=str(tmp_path),
        fresh=True,
        delivery_id="two",
        require_idle=True,
        expected_session_id=source_session_id,
        expected_last_result_at=12.0,
        context_rollover=True,
        delegated=False,
    )

    assert retried.accepted
    assert "--resume" in spawned[1]["args"]
    assert spawned[1]["args"][spawned[1]["args"].index("--resume") + 1] == "target-id"
    await host.stop()


async def test_deliver_to_live_worker_steers_without_new_spawn(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    result = await host.deliver(
        "issue-1", "change course", require_active=True, delegated=False
    )
    assert result.accepted and result.action == "steered"
    assert len(spawned) == 1
    assert spawned[0]["process"].stdin.lines[1]["message"]["content"][0]["text"] == "change course"
    await host.stop()


async def test_delivery_id_replay_is_deduplicated_across_restart(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1", delegated=False
    )
    replay = await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1", delegated=False
    )

    assert first.accepted and replay == first
    assert len(spawned) == 1
    assert len(spawned[0]["process"].stdin.lines) == 1
    await host.stop()

    reloaded, spawned_after_restart = _host(tmp_path)
    replay_after_restart = await reloaded.deliver(
        "issue-1",
        "start",
        cwd=str(tmp_path),
        delivery_id="attempt-1",
        delegated=True,
    )
    assert replay_after_restart == first
    assert spawned_after_restart == []


async def test_delivery_id_rejects_conflicting_payload(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1", delegated=False
    )

    conflict = await host.deliver(
        "issue-1", "different", cwd=str(tmp_path), delivery_id="attempt-1", delegated=False
    )

    assert not conflict.accepted
    assert conflict.reason == "delivery_id_conflict"
    assert len(spawned[0]["process"].stdin.lines) == 1
    await host.stop()


async def test_tokenless_spawn_mints_a_session_token_and_keeps_it_across_revival(
    tmp_path,
):
    # The web proxy always mints a token, but a caller reaching the runtime
    # directly may omit one. Without a fallback the worker is the only kind of
    # deck-owned session that cannot resolve /api/sessions/by-token/<token>.
    bound = []
    host, spawned = _host(
        tmp_path, bind_session_token=lambda token, key: bound.append((token, key))
    )
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    token = host._records["issue-1"].session_token
    assert token
    assert bound == [(token, "claude_code:test:sid-auto-1")]
    prompt = spawned[0]["args"][spawned[0]["args"].index("--append-system-prompt") + 1]
    assert token in prompt

    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-abc"})
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()
    await host.stop()

    # Same key, same lineage, new runtime process: the identity must survive both
    # the revive and the restart that reloads the record from disk.
    revived_host, revived_spawned = _host(tmp_path)
    revived = await revived_host.deliver("issue-1", "follow-up", delegated=False)
    assert revived.accepted and revived.action == "revived"
    assert revived_host._records["issue-1"].session_token == token
    assert (
        token
        in revived_spawned[0]["args"][
            revived_spawned[0]["args"].index("--append-system-prompt") + 1
        ]
    )
    await revived_host.stop()


async def test_caller_supplied_session_token_is_never_replaced_by_a_minted_one(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delegated=False, session_token="token-1"
    )
    assert host._records["issue-1"].session_token == "token-1"
    assert (
        "token-1"
        in spawned[0]["args"][spawned[0]["args"].index("--append-system-prompt") + 1]
    )
    await host.stop()


async def test_deliver_after_exit_revives_with_resume(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=True)
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-abc"})
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")  # EOF → reader drops the live entry
    await _settle()

    result = await host.deliver("issue-1", "follow-up", delegated=False)
    assert result.accepted and result.action == "revived"
    assert "--resume" in spawned[1]["args"]
    assert spawned[1]["args"][spawned[1]["args"].index("--resume") + 1] == "sid-abc"
    assert spawned[1]["args"][spawned[1]["args"].index("--append-system-prompt") + 1] == (
        agentdeck_session_instructions(provider="claude_code",
            role="delegated", session_token=host._records["issue-1"].session_token
        )
    )
    await host.stop()


def _deleted_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose issue worktree a merge is about to remove (#2524)."""
    repository = tmp_path / "project"
    (repository / ".git").mkdir(parents=True)
    worktree = repository / ".worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    return repository, worktree


async def _finished_turn(host, spawned, worktree: Path, *, exit_process: bool) -> None:
    await host.deliver(
        "issue-1", "start", cwd=str(worktree), delegated=False, delivery_id="d-start"
    )
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-abc"})
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    if exit_process:
        proc.returncode = 0
        proc.stdout.queue.put_nowait(b"")
        await _settle()


async def test_a_chat_whose_worktree_is_gone_resumes_in_its_repository(tmp_path):
    """#2524: same conversation, same receipts, new directory, told once."""
    repository, worktree = _deleted_worktree(tmp_path)
    host, spawned = _host(tmp_path)
    await _finished_turn(host, spawned, worktree, exit_process=True)
    worktree.rmdir()

    result = await host.deliver(
        "issue-1", "follow-up", delegated=False, delivery_id="d-follow"
    )

    assert result.accepted and result.action == "revived"
    args = spawned[1]["args"]
    assert args[args.index("--resume") + 1] == "sid-abc"
    assert spawned[1]["kwargs"]["cwd"] == str(repository)
    assert relocation_notice(str(worktree), str(repository)) in args[
        args.index("--append-system-prompt") + 1
    ]
    record = host._records["issue-1"]
    assert record.cwd == str(repository)
    assert record.relocated_from is None
    assert "d-start" in record.deliveries
    # The same delivery again is answered from its receipt, not written twice.
    again = await host.deliver(
        "issue-1", "follow-up", delegated=False, delivery_id="d-follow"
    )
    assert again.accepted
    assert len(spawned) == 2

    # The move was told once; a later revival says nothing more about it.
    proc = spawned[1]["process"]
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()
    await host.deliver("issue-1", "and another", delegated=False)
    later = spawned[2]["args"]
    assert spawned[2]["kwargs"]["cwd"] == str(repository)
    assert "no longer exists" not in later[later.index("--append-system-prompt") + 1]
    await host.stop()


async def test_an_idle_process_left_in_a_deleted_worktree_is_restarted_there(tmp_path):
    repository, worktree = _deleted_worktree(tmp_path)
    host, spawned = _host(tmp_path)
    await _finished_turn(host, spawned, worktree, exit_process=False)
    worktree.rmdir()

    result = await host.deliver("issue-1", "follow-up", delegated=False)

    assert result.accepted
    assert spawned[0]["process"].returncode is not None
    assert spawned[1]["kwargs"]["cwd"] == str(repository)
    assert spawned[1]["args"][spawned[1]["args"].index("--resume") + 1] == "sid-abc"
    await host.stop()


async def test_a_deleted_directory_with_no_repository_above_it_stays_put(
    tmp_path, monkeypatch
):
    from agentdeck.providers.claude_code import worker as worker_mod

    monkeypatch.setattr(worker_mod, "continued_chat_cwd", lambda _cwd: None)
    _repository, worktree = _deleted_worktree(tmp_path)
    host, spawned = _host(tmp_path)
    await _finished_turn(host, spawned, worktree, exit_process=True)
    worktree.rmdir()

    await host.deliver("issue-1", "follow-up", delegated=False)

    assert host._records["issue-1"].cwd == str(worktree)
    assert spawned[1]["kwargs"]["cwd"] == str(worktree)
    await host.stop()


async def test_spawn_injects_agentdeck_url_but_not_session_key_when_fresh(tmp_path):
    host, spawned = _host(tmp_path, base_url="http://127.0.0.1:8756")
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    env = spawned[0]["kwargs"]["env"]
    # A fresh worker's session id is unknown at spawn, so only the base is set.
    assert env["AGENTDECK_URL"] == "http://127.0.0.1:8756"
    assert "AGENTDECK_SESSION_KEY" not in env
    assert env["AGENTDECK_GH_CONSUMER"] == "claude_code:test:issue-1"
    await host.stop()


async def test_resume_spawn_injects_session_key(tmp_path):
    host, spawned = _host(tmp_path, base_url="http://127.0.0.1:8756")
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-abc"})
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")  # EOF → reader drops the live entry
    await _settle()

    await host.deliver("issue-1", "follow-up", delegated=False)
    env = spawned[1]["kwargs"]["env"]
    assert env["AGENTDECK_URL"] == "http://127.0.0.1:8756"
    assert env["AGENTDECK_SESSION_KEY"] == "claude_code:test:sid-abc"
    await host.stop()


async def test_spawn_omits_agentdeck_url_without_base(tmp_path):
    host, spawned = _host(tmp_path)  # no base_url configured
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    env = spawned[0]["kwargs"]["env"]
    assert "AGENTDECK_URL" not in env
    assert "AGENTDECK_SESSION_KEY" not in env
    await host.stop()


async def test_spawn_does_not_inherit_another_sessions_identity(tmp_path, monkeypatch):
    """When this process runs inside an owned session, the ambient AGENTDECK_*
    name *that* session. A fresh worker inheriting them would post and pin as
    its parent — the wrong-identity injection the resume branch avoids."""
    monkeypatch.setenv("AGENTDECK_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("AGENTDECK_SESSION_KEY", "claude_code:other:sid-parent")

    host, spawned = _host(tmp_path, base_url="http://127.0.0.1:8756")
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    env = spawned[0]["kwargs"]["env"]
    assert env["AGENTDECK_URL"] == "http://127.0.0.1:8756"  # ours, not the ambient one
    assert "AGENTDECK_SESSION_KEY" not in env
    await host.stop()

    bare, bare_spawned = _host(tmp_path / "bare")  # no base_url configured
    await bare.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    bare_env = bare_spawned[0]["kwargs"]["env"]
    assert "AGENTDECK_URL" not in bare_env
    assert "AGENTDECK_SESSION_KEY" not in bare_env
    await bare.stop()


async def test_failed_resume_falls_back_to_fresh_spawn(tmp_path):
    original, spawned = _host(tmp_path)
    await original.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-old"}
    )
    await _settle()
    await original.stop()

    attempts = []

    async def factory(*args, **kwargs):
        process = FakeProcess()
        attempts.append({"args": args, "process": process})
        if "--resume" in args:
            asyncio.get_running_loop().call_soon(process.stdout.queue.put_nowait, b"")
        else:
            asyncio.get_running_loop().call_soon(
                process.stdout.emit,
                {"type": "system", "subtype": "init", "session_id": "sid-fresh"},
            )
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver("issue-1", "recover", delegated=False)

    assert result.accepted and result.action == "spawned"
    assert len(attempts) == 2
    assert "--resume" in attempts[0]["args"]
    # The fallback spawn reuses the record, so both attempts carry the identity
    # minted when the key was first delivered to — including across the restart.
    expected = agentdeck_session_instructions(provider="claude_code",
        role="primary", session_token=host._records["issue-1"].session_token
    )
    assert attempts[0]["args"][attempts[0]["args"].index("--append-system-prompt") + 1] == (
        expected
    )
    assert "--resume" not in attempts[1]["args"]
    assert attempts[1]["args"][attempts[1]["args"].index("--append-system-prompt") + 1] == (
        expected
    )
    assert result.session_id == "sid-fresh"
    await host.stop()


async def _host_with_resumable_chat(tmp_path, resume_fails):
    """A host whose ``issue-1`` names ``sid-old``, with its transcript on disk.

    ``resume_fails`` resume attempts die before init; later ones succeed.
    """
    original, spawned = _host(tmp_path)
    await original.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-old"}
    )
    await _settle()
    await original.stop()
    transcript = tmp_path / "cfg" / "projects" / "-repo" / "sid-old.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")

    attempts = []

    async def factory(*args, **kwargs):
        process = FakeProcess()
        attempts.append(args)
        resumes = sum("--resume" in a for a in attempts)
        if "--resume" in args and resumes <= resume_fails:
            asyncio.get_running_loop().call_soon(process.stdout.queue.put_nowait, b"")
        else:
            sid = "sid-old" if "--resume" in args else "sid-fresh"
            asyncio.get_running_loop().call_soon(
                process.stdout.emit,
                {"type": "system", "subtype": "init", "session_id": sid},
            )
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    return host, attempts


async def test_failed_resume_of_a_chat_with_a_transcript_retries_the_resume(tmp_path):
    host, attempts = await _host_with_resumable_chat(tmp_path, resume_fails=1)

    result = await host.deliver("issue-1", "recover", delegated=False)

    assert result.accepted and result.action == "revived"
    assert len(attempts) == 2
    assert all(a[a.index("--resume") + 1] == "sid-old" for a in attempts)
    assert host._records["issue-1"].session_id == "sid-old"
    await host.stop()


async def test_a_chat_with_a_transcript_is_never_swapped_for_an_empty_one(tmp_path):
    host, attempts = await _host_with_resumable_chat(tmp_path, resume_fails=2)

    result = await host.deliver("issue-1", "recover", delegated=False)

    assert not result.accepted and result.action == "rejected"
    assert result.reason.startswith("could not resume this chat")
    assert len(attempts) == 2
    assert all("--resume" in a for a in attempts)
    # The record still names the chat, so a later message resumes it.
    assert host._records["issue-1"].session_id == "sid-old"
    again = await host.deliver("issue-1", "recover", delegated=False)
    assert again.accepted and again.action == "revived"
    assert again.session_id == "sid-old"
    await host.stop()


async def test_fresh_spawns_without_resume(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-abc"}
    )
    await _settle()
    result = await host.deliver(
        "issue-1", "clean slate", cwd=str(tmp_path), fresh=True, delegated=False
    )
    assert result.accepted and result.action == "spawned"
    assert "--resume" not in spawned[1]["args"]
    assert spawned[0]["process"].returncode == 0
    assert host.snapshot()["live_count"] == 1
    await host.stop()


async def test_image_delivery_encodes_anthropic_content_block(tmp_path):
    host, spawned = _host(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = await host.deliver(
        "issue-1", "inspect it", cwd=str(tmp_path), images=[str(image)], delegated=False
    )

    assert result.accepted
    content = spawned[0]["process"].stdin.lines[0]["message"]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "inspect it"}
    await host.stop()


async def test_capacity_rejects_new_spawns_but_not_steering(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    rejected = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    steered = await host.deliver("issue-1", "still fine", delegated=False)
    assert steered.accepted
    assert len(spawned) == 1
    await host.stop()


async def test_a_person_starting_a_chat_is_never_refused_but_a_machine_is(tmp_path):
    # "Don't allow getting at_capacity here" (#559): the cap exists to stop
    # machines from filling an account. A person's explicit start is admitted.
    # The pool is still asked first, so an idle worker is reclaimed before the
    # cap is passed.
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver(
        "chat-ba83", "start", cwd=str(tmp_path), delegated=False
    )  # mid-turn, unreclaimable

    machine = await host.deliver(
        "delegate-1", "work it", cwd=str(tmp_path), role="delegated", delegated=False
    )
    person = await host.deliver("chat-88a8", "start", cwd=str(tmp_path), delegated=False)

    assert not machine.accepted and machine.reason == "at_capacity"
    assert person.accepted and person.action == "spawned"
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is True  # nobody's turn lost
    assert len(spawned) == 2
    await host.stop()


async def test_an_automated_message_to_a_persons_chat_waits_for_a_slot(tmp_path):
    # Revivals of person-started chats went past the cap on the person's behalf
    # and pushed main to 7/4 after the 2026-10-13 restart (#3009). Nobody asked.
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("chat-ba83", "start", cwd=str(tmp_path), delegated=False)

    revival = await host.deliver(
        "chat-88a8", "you were cut off", cwd=str(tmp_path), delegated=False, automated=True
    )

    assert not revival.accepted and revival.reason == "at_capacity"
    assert len(spawned) == 1
    await host.stop()


async def test_a_worker_given_a_message_moments_ago_is_not_reclaimed_as_idle(tmp_path):
    # #3009: a resumed CLI ended a turn of its own right after a person's message
    # was written, and the worker was reaped as idle two seconds later.
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("chat-ba83", "Can you check more often", cwd=str(tmp_path), delegated=False)
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    rejected = await host.deliver(
        "delegate-1", "work it", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is True

    _age_delivery(host, "chat-ba83")
    admitted = await host.deliver(
        "delegate-1", "work it", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert admitted.accepted
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is False
    await host.stop()


def _age_delivery(host, key: str) -> None:
    """Move a worker's last delivery out of the idle-reclaim grace."""
    host._records[key].last_delivery_at -= IDLE_RECLAIM_GRACE_S + 1


async def test_idle_worker_is_evicted_to_admit_new_chat(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    # issue-1's turn finishes: its process stays alive but is now idle, so a
    # second chat must be admitted by reclaiming that idle slot, not rejected.
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is False
    _age_delivery(host, "issue-1")

    admitted = await host.deliver("issue-2", "start", cwd=str(tmp_path), delegated=False)
    assert admitted.accepted and admitted.action == "spawned"
    assert len(spawned) == 2
    snap = host.snapshot()
    assert snap["live_count"] == 1
    assert snap["workers"]["issue-1"]["live"] is False  # evicted, record kept
    assert spawned[0]["process"].returncode == 0
    await host.stop()


async def test_capacity_rejects_when_all_workers_mid_turn(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    # issue-1 never emits a result, so its turn stays active and there is no
    # idle slot to reclaim — a machine's next worker is still rejected.
    rejected = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert len(spawned) == 1
    await host.stop()


def _ask(proc, request_id: str = "req-1") -> None:
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )


async def test_a_turn_waiting_on_a_question_does_not_hold_a_slot(tmp_path):
    """#3026: unanswered questions filled the cap and refused every other chat."""
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    _ask(spawned[0]["process"])
    await _settle()

    admitted = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert admitted.accepted and admitted.action == "spawned"
    # The waiting chat was not reclaimed: its answer still reaches it, once.
    assert host.snapshot()["workers"]["issue-1"]["live"] is True
    answered = await host.answer("issue-1", "req-1", answers={"0": ["a"]}, decision="accept")
    assert answered.accepted and answered.action == "answered"
    assert spawned[0]["process"].stdin.lines[-1]["type"] == "control_response"

    # Answered, it counts again: both workers are mid-turn, so a third is refused.
    rejected = await host.deliver(
        "issue-3", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert len(spawned) == 2
    await host.stop()


async def test_a_question_with_background_agents_running_keeps_its_slot(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    _ask(spawned[0]["process"])
    await _settle()
    host._live["issue-1"].background_agents["agent-1"] = time.time()

    rejected = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    await host.stop()


async def _stall(host, key: str, seconds: float = 200.0) -> None:
    """Backdate a live worker so its open turn reads as silent."""
    host._records[key].last_delivery_at -= seconds
    host._live[key].last_event_at -= seconds


async def test_stalled_turn_is_reaped_instead_of_refusing_forever(tmp_path):
    # A turn that has been silent past the stall threshold is not work in
    # progress; without reaping it the account would answer at_capacity to every
    # later chat until the runtime restarts.
    host, spawned = _host(tmp_path, max_workers=1, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    await _stall(host, "issue-1")
    assert host.snapshot()["workers"]["issue-1"]["stalled"] is True

    admitted = await host.deliver("issue-2", "start", cwd=str(tmp_path), delegated=False)
    assert admitted.accepted and admitted.action == "spawned"
    assert len(spawned) == 2
    snap = host.snapshot()
    assert snap["live_count"] == 1
    assert snap["workers"]["issue-1"]["live"] is False  # reaped, record kept
    assert snap["workers"]["issue-1"]["session_id"] == "sid-auto-1"  # revivable
    await host.stop()


async def test_an_idle_worker_is_reclaimed_before_a_stalled_one(tmp_path):
    # Reclaiming an idle worker costs nobody a turn, so it must be preferred
    # even when a stalled worker has been quiet for longer.
    host, spawned = _host(tmp_path, max_workers=2, stall_after_s=100.0)
    await host.deliver("stalled", "start", cwd=str(tmp_path), delegated=False)
    await _stall(host, "stalled", seconds=400.0)
    await host.deliver("idle", "start", cwd=str(tmp_path), delegated=False)
    spawned[1]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    _age_delivery(host, "idle")

    admitted = await host.deliver("issue-3", "start", cwd=str(tmp_path), delegated=False)
    assert admitted.accepted and admitted.action == "spawned"
    snap = host.snapshot()
    assert snap["workers"]["idle"]["live"] is False
    assert snap["workers"]["stalled"]["live"] is True
    await host.stop()


async def test_a_recently_active_turn_is_never_reaped(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    await _stall(host, "issue-1")
    # It speaks again just before the new chat arrives: the turn is alive after
    # all, so the cleanup must leave it alone.
    spawned[0]["process"].stdout.emit({"type": "assistant", "message": {"content": []}})
    await _settle()

    rejected = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert host.snapshot()["workers"]["issue-1"]["live"] is True
    await host.stop()


async def test_a_worker_waiting_on_the_user_is_never_reaped(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    spawned[0]["process"].stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "rm -rf build"},
            },
        }
    )
    await _settle()
    await _stall(host, "issue-1")

    # Silent because it is blocked on the user's answer, not because it hung —
    # reaping it would throw away the decision they are about to make. It holds
    # no slot while it waits (#3026), so the next chat is admitted beside it.
    admitted = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert admitted.accepted and admitted.action == "spawned"
    assert host.snapshot()["workers"]["issue-1"]["live"] is True
    await host.stop()


async def test_a_stalled_turn_with_background_agents_is_never_reaped(tmp_path):
    # Async Agent tasks report through the parent transcript, not stdout, so a
    # quiet stdout is not evidence that this worker is hung.
    host, spawned = _host(tmp_path, max_workers=1, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": "agent-1",
                },
            }
        )
        + "\n"
    )
    await _stall(host, "issue-1")

    rejected = await host.deliver(
        "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
    )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert host.snapshot()["workers"]["issue-1"]["live"] is True
    await host.stop()


async def _go_idle(spawned, index):
    """Finish the turn on a just-delivered worker so it counts as idle."""
    spawned[index]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()


async def test_a_quiet_worker_is_collected_though_nothing_wants_its_slot(tmp_path):
    # The #1226 bug. With nobody competing for a slot, admission pressure never
    # reclaims a worker, and with no timed sweep nothing collected one at all:
    # 39 sat live for up to 20 days.
    host, spawned = _host(tmp_path, max_workers=4, idle_after_s=3600.0)
    await host.deliver("agentdeck#556", "work it", cwd=str(tmp_path), delegated=False)
    await _go_idle(spawned, 0)

    collected = await host.collect_idle(now=time.time() + 7200)

    assert collected == ["agentdeck#556"]
    assert host.snapshot()["workers"]["agentdeck#556"]["live"] is False
    # The durable record survives, so the chat revives on the next delivery —
    # collection costs a respawn, never the conversation.
    assert host.has_worker("agentdeck#556")
    await host.stop()


async def test_a_worker_inside_the_idle_ceiling_is_left_alone(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1, idle_after_s=3600.0)
    await host.deliver("chat-ba83", "start", cwd=str(tmp_path), delegated=False)
    await _go_idle(spawned, 0)

    assert await host.collect_idle(now=time.time() + 60) == []
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is True
    await host.stop()


async def test_pending_context_survives_collection_park_release_and_forget(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1, idle_after_s=1.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    await _go_idle(spawned, 0)
    rec = host._records["issue-1"]
    rec.pending_context_delivery_id = "context-d2"
    rec.pending_context_operation = "rollover"
    rec.pending_context_source_session_id = rec.session_id
    host._save_state()

    assert await host.collect_idle(now=time.time() + 7200) == []
    released = await host.release_worker("issue-1")
    assert not released.accepted and released.reason == "context_operation_pending"
    parked = await host.park_worker("issue-1")
    assert parked.accepted
    forgotten = await host.forget("issue-1")
    assert not forgotten.accepted
    assert forgotten.reason == "context_operation_pending"
    assert host._records["issue-1"].pending_context_delivery_id == "context-d2"
    await host.stop()


async def test_collection_never_takes_a_worker_mid_turn(tmp_path):
    # No result emitted: the turn is still running however long stdout has been
    # quiet. That is what stall_after_s is for, and it ends work somebody started.
    host, _ = _host(tmp_path, max_workers=1, idle_after_s=3600.0)
    await host.deliver("chat-ba83", "start", cwd=str(tmp_path), delegated=False)

    assert await host.collect_idle(now=time.time() + 86400) == []
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is True
    await host.stop()


async def test_a_quiet_worker_with_background_agents_is_never_collected(tmp_path):
    # Same exclusion stall reclaim makes: async Agent tasks run past the outer
    # turn's result and report through the parent transcript, so an idle-looking
    # worker can still have real work in flight.
    host, spawned = _host(tmp_path, max_workers=1, idle_after_s=3600.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": "agent-1",
                },
            }
        )
        + "\n"
    )
    await _go_idle(spawned, 0)

    assert await host.collect_idle(now=time.time() + 7200) == []
    assert host.snapshot()["workers"]["issue-1"]["live"] is True
    await host.stop()


async def test_collection_is_off_unless_a_ceiling_is_configured(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)  # idle_after_s defaults to 0
    await host.deliver("chat-ba83", "start", cwd=str(tmp_path), delegated=False)
    await _go_idle(spawned, 0)

    assert await host.collect_idle(now=time.time() + 86400) == []
    assert host.snapshot()["workers"]["chat-ba83"]["live"] is True
    await host.stop()


async def test_collection_takes_every_eligible_worker_not_just_one(tmp_path):
    # Unlike admission reclaim, which stops as soon as it has freed the one slot
    # it needed, a sweep has no slot to free — leaving the rest would just mean
    # waiting another interval per worker.
    host, spawned = _host(tmp_path, max_workers=4, idle_after_s=3600.0)
    for index, key in enumerate(("agentdeck#556", "chat-ba83", "delegate-1")):
        await host.deliver(key, "start", cwd=str(tmp_path), delegated=False)
        await _go_idle(spawned, index)

    collected = await host.collect_idle(now=time.time() + 7200)

    assert sorted(collected) == ["agentdeck#556", "chat-ba83", "delegate-1"]
    assert all(not w["live"] for w in host.snapshot()["workers"].values())
    await host.stop()


async def test_a_refusal_names_the_workers_holding_the_slots(tmp_path, caplog):
    # at_capacity is otherwise invisible: it is a field in one HTTP response and
    # nothing in the journal explains why the deck stopped admitting work.
    host, _ = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    with caplog.at_level("WARNING", logger="agentdeck.providers.claude_code.worker"):
        rejected = await host.deliver(
            "issue-2", "start", cwd=str(tmp_path), role="delegated", delegated=False
        )
    assert not rejected.accepted and rejected.reason == "at_capacity"
    assert "at_capacity" in caplog.text
    assert "issue-1(turn=y" in caplog.text
    await host.stop()


async def test_interrupt_round_trip(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    async def respond():
        while not any(line.get("type") == "control_request" for line in proc.stdin.lines):
            await asyncio.sleep(0)
        req = next(line for line in proc.stdin.lines if line["type"] == "control_request")
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    responder = asyncio.create_task(respond())
    result = await host.interrupt("issue-1")
    await responder
    assert result.accepted and result.action == "interrupted"
    assert proc.stdin.lines[-1]["request"]["subtype"] == "interrupt"
    await host.stop()


async def test_interrupt_returns_at_once_when_worker_exits_before_ack(tmp_path):
    # Regression: if the process exits before a control_response arrives, the
    # interrupt waiter must be resolved by the reader's teardown rather than
    # hanging for the full INTERRUPT_TIMEOUT_S.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    async def exit_before_ack():
        while not any(line.get("type") == "control_request" for line in proc.stdin.lines):
            await asyncio.sleep(0)
        proc.returncode = 0
        proc.stdout.queue.put_nowait(b"")  # EOF ends the read loop -> teardown

    task = asyncio.create_task(exit_before_ack())
    result = await asyncio.wait_for(host.interrupt("issue-1"), timeout=1.0)
    await task
    assert not result.accepted and result.reason == "worker_exited"
    await host.stop()


async def test_snapshot_excludes_exited_worker_before_reader_cleanup(tmp_path):
    # Regression: an exited process still lingering in _live (reader not yet
    # drained) must read as live=False / live_count=0, not live=True.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.returncode = 0  # exited, but no EOF emitted yet -> still in _live
    snap = host.snapshot()
    assert snap["workers"]["issue-1"]["live"] is False
    assert snap["workers"]["issue-1"]["turn_active"] is False
    assert snap["live_count"] == 0
    proc.stdout.queue.put_nowait(b"")  # let the reader finish
    await host.stop()


async def test_state_persists_across_host_restarts(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"abacus": {"type": "http"}}}')
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=True)
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-xyz"}
    )
    await _settle()
    await host.stop()
    # A record written while the poller's kanban role existed (#3741) still loads, and the
    # chat resumes as a primary one: it was its issue's top-level coordinator.
    state = tmp_path / "state" / "test.json"
    saved = json.loads(state.read_text())
    saved["workers"][0]["role"] = "kanban"
    state.write_text(json.dumps(saved))

    reloaded, spawned2 = _host(tmp_path)
    assert reloaded.worker_role("issue-1") == "primary"
    result = await reloaded.deliver("issue-1", "resume me", delegated=False)
    assert result.accepted and result.action == "revived"
    args = spawned2[0]["args"]
    assert "--resume" in args
    assert not any(arg.startswith("--mcp-config") for arg in args)
    assert args[args.index("--append-system-prompt") + 1].startswith(
        agentdeck_session_instructions(
            provider="claude_code",
            role="primary",
            session_token=reloaded._records["issue-1"].session_token,
        )
    )
    await reloaded.stop()


async def test_park_frees_capacity_and_next_delivery_revives(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    parked = await host.park_worker("issue-1")
    parked_again = await host.park_worker("issue-1")

    assert parked.accepted and parked.action == "parked"
    assert parked_again.accepted
    assert host.snapshot()["live_count"] == 0
    assert "issue-1" in host.snapshot()["workers"]

    revived = await host.deliver("issue-1", "continue", delegated=False)
    assert revived.accepted and revived.action == "revived"
    assert "--resume" in spawned[1]["args"]
    await host.stop()


async def test_park_unknown_key_is_idempotently_accepted(tmp_path):
    host, _ = _host(tmp_path)
    result = await host.park_worker("already-gone")
    assert result.accepted and result.action == "parked"


async def test_release_is_idempotent_and_forgets_lineage(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    released = await host.release_worker("issue-1")
    released_again = await host.release_worker("issue-1")

    assert released.accepted and released.action == "released"
    assert released_again.accepted and released_again.action == "released"
    assert spawned[0]["process"].returncode == 0
    assert "issue-1" not in host.snapshot()["workers"]


async def test_worker_command_includes_configured_flags(tmp_path):
    host, spawned = _host(tmp_path, permission_mode="acceptEdits", model="haiku")
    await host.deliver("issue-1", "start", cwd=str(tmp_path), effort="high", delegated=False)
    args = spawned[0]["args"]
    # A new chat runs in the configured mode unless start_bypassed is on (#3748).
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is False
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--effort") + 1] == "high"
    await host.stop()


async def test_model_usage_and_legacy_session_cap_do_not_block_delivery(tmp_path):
    host, _spawned = _host(
        tmp_path,
        usage_ceiling_pct=1.0,
        usage_reader=lambda: 100.0,
        session_over_budget=lambda _session_id: True,
    )
    started = await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delegated=False
    )
    followed_up = await host.deliver("issue-1", "continue", delegated=False)

    assert started.accepted and started.action == "spawned"
    assert followed_up.accepted and followed_up.action == "steered"
    await host.stop()


async def test_exhausted_claude_session_does_not_block_cwd_replacement(tmp_path):
    exhausted = False
    host, spawned = _host(
        tmp_path, session_over_budget=lambda _session_id: exhausted
    )
    await host.deliver("issue-1", "start", cwd=str(tmp_path / "old"), delegated=False)
    spawned[0]["process"].terminate()
    await _settle()
    exhausted = True

    replaced = await host.deliver("issue-1", "new work", cwd=str(tmp_path / "new"), delegated=False)

    assert replaced.accepted
    assert replaced.session_id == "sid-auto-2"
    await host.stop()


async def test_stalled_flag_after_threshold(tmp_path):
    host, spawned = _host(tmp_path, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    # backdate the delivery so the live turn looks silent
    host._records["issue-1"].last_delivery_at -= 200
    host._live["issue-1"].last_event_at -= 200
    assert host.snapshot()["workers"]["issue-1"]["stalled"] is True
    await host.stop()


async def test_forget_drops_finished_records_only(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    assert not (await host.forget("issue-1")).accepted  # still live
    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()
    assert (await host.forget("issue-1")).accepted
    assert "issue-1" not in host.snapshot()["workers"]
    await host.stop()


async def test_command_enables_stdio_permission_prompt(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    args = spawned[0]["args"]
    assert "--permission-prompt-tool" in args
    assert args[args.index("--permission-prompt-tool") + 1] == "stdio"
    await host.stop()


async def test_fresh_command_injects_agentdeck_session_guide(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    args = spawned[0]["args"]
    assert args[args.index("--append-system-prompt") + 1] == (
        agentdeck_session_instructions(provider="claude_code",
            role="primary", session_token=host._records["issue-1"].session_token
        )
    )
    assert "Session role: primary user-facing coordinator" in AGENTDECK_SESSION_INSTRUCTIONS
    await host.stop()


async def test_askuserquestion_control_request_becomes_pending_and_answers(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-42",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "display_name": "AskUserQuestion",
                "requires_user_interaction": True,
                "input": {
                    "questions": [
                        {
                            "question": "Pick a fruit",
                            "header": "Fruit",
                            "multiSelect": False,
                            "options": [
                                {"label": "apple", "description": "a"},
                                {"label": "banana", "description": "b"},
                            ],
                        }
                    ]
                },
            },
        }
    )
    await _settle()
    # snapshot ships the normalized (provider-neutral) interaction, not raw wire
    pending = host.snapshot()["workers"]["issue-1"]["pending_interaction"]
    assert pending["id"] == "req-42"
    assert pending["kind"] == "question"
    assert pending["questions"][0]["prompt"] == "Pick a fruit"

    result = await host.answer(
        "issue-1", "req-42", answers={"0": ["banana"]}, decision="accept"
    )
    assert result.accepted and result.action == "answered"
    written = proc.stdin.lines[-1]
    assert written["type"] == "control_response"
    resp = written["response"]["response"]
    assert resp["behavior"] == "allow"
    assert resp["updatedInput"]["answers"] == {"Pick a fruit": "banana"}
    # cleared after answering
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    await host.stop()


async def test_permission_gate_allow_and_deny(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    def emit_gate(rid):
        proc.stdout.emit(
            {
                "type": "control_request",
                "request_id": rid,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "display_name": "Bash",
                    "description": "delete a file",
                    "input": {"command": "rm x"},
                },
            }
        )

    emit_gate("g1")
    await _settle()
    await host.answer("issue-1", "g1", answers={}, decision="accept")
    assert proc.stdin.lines[-1]["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "rm x"},
    }

    emit_gate("g2")
    await _settle()
    await host.answer("issue-1", "g2", answers={}, decision="cancel")
    denied = proc.stdin.lines[-1]["response"]["response"]
    assert denied["behavior"] == "deny" and denied["interrupt"] is True
    await host.stop()


async def test_answer_rejects_unknown_interaction(tmp_path):
    host, _ = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    result = await host.answer("issue-1", "nope", answers={}, decision="accept")
    assert not result.accepted and result.reason == "interaction_not_pending"
    await host.stop()


async def test_result_event_clears_pending_interaction(tmp_path):
    # An interrupt/finish (result event) while a question is pending must drop it,
    # else the widget lingers and a late answer targets an abandoned request.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is not None
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    # a late answer for the abandoned request is now rejected
    result = await host.answer("issue-1", "req-1", answers={"0": ["a"]}, decision="accept")
    assert not result.accepted and result.reason == "interaction_not_pending"
    # The question itself outlives the turn (#3451); its answer reaches the chat as a
    # message, once, however often it is sent.
    view = host.snapshot()["workers"]["issue-1"]["open_questions"]
    assert [q["id"] for q in view["questions"]] == ["req-1.0"]
    for _ in range(2):
        await host.answer("issue-1", view["id"], answers={"req-1.0": ["b"]}, decision="accept")
    sent = [line for line in proc.stdin.lines if line["type"] == "user"]
    assert sent[-1]["message"]["content"][-1]["text"] == (
        "The person answered from the question box:\n- «q?»: b"
    )
    assert sent[-2]["message"]["content"][-1]["text"] == "start"
    assert host.snapshot()["workers"]["issue-1"]["open_questions"] is None
    await host.stop()


async def test_message_closes_open_question_before_reaching_the_chat(tmp_path):
    # A message sent while a question waits used to reach the CLI raw, which cancels the
    # question as "the user doesn't want to proceed"; the agent read that as the owner
    # turning down the question box (#3121). The deck closes it first, saying why.
    from agentdeck.providers.claude_code.worker import QUESTION_SUPERSEDED

    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-7",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )
    await _settle()
    result = await host.deliver(
        "issue-1", "Did someone edit that setting?", cwd=str(tmp_path), delegated=False
    )
    assert result.accepted and result.action == "steered"
    closed, sent = proc.stdin.lines[-2:]
    assert closed["type"] == "control_response"
    assert closed["response"]["request_id"] == "req-7"
    assert closed["response"]["response"] == {
        "behavior": "deny", "message": QUESTION_SUPERSEDED
    }
    assert sent["type"] == "user"
    assert sent["message"]["content"][-1]["text"] == "Did someone edit that setting?"
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    # A permission gate is left to the CLI: only a question is closed this way.
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "g-1",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
        }
    )
    await _settle()
    await host.deliver("issue-1", "and this", cwd=str(tmp_path), delegated=False)
    assert proc.stdin.lines[-1]["type"] == "user"
    assert proc.stdin.lines[-2]["type"] == "user"
    await host.stop()


async def test_open_questions_stack_and_are_answered_one_at_a_time(tmp_path):
    # Several questions stay open at once, and any one of them can be answered or
    # dismissed on its own (#3451).
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    def ask(request_id, *prompts):
        proc.stdout.emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "input": {"questions": [
                        {"question": p, "header": "h", "options": [{"label": "a"}]}
                        for p in prompts
                    ]},
                },
            }
        )

    def view():
        return host.snapshot()["workers"]["issue-1"]["open_questions"]

    ask("r1", "First?")
    await _settle()
    await host.deliver("issue-1", "CI is green", cwd=str(tmp_path), delegated=False)
    ask("r2", "Second?", "Third?")
    await _settle()
    assert [q["id"] for q in view()["questions"]] == ["r2.0", "r2.1", "r1.0"]
    assert view()["dismissible"] is True
    # Only the older question answered while the turn waits inside the newer one: the
    # whole submit goes back as the newer one's tool answer, naming what stays open.
    await host.answer("issue-1", view()["id"], answers={"r1.0": ["a"]}, decision="accept")
    written = proc.stdin.lines[-1]
    assert written["response"]["request_id"] == "r2"
    assert written["response"]["response"] == {
        "behavior": "deny",
        "message": (
            "The person answered from the question box:\n- «First?»: a\n"
            "Still open on their screen; their answers will come as later messages, so do"
            " not ask these again:\n- «Second?»\n- «Third?»"
        ),
    }
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    # Dismissing one writes nothing; the other stays open.
    lines = len(proc.stdin.lines)
    await host.answer("issue-1", view()["id"], answers={}, decision="dismiss:r2.0")
    assert len(proc.stdin.lines) == lines
    assert [q["id"] for q in view()["questions"]] == ["r2.1"]
    # A held worker's log read again after a restart opens nothing twice.
    ask("r1", "First?")
    await _settle()
    assert [q["id"] for q in view()["questions"]] == ["r2.1"]
    # A question asked and answered whole goes back in the CLI's own shape.
    ask("r3", "Fourth?")
    await _settle()
    await host.answer("issue-1", view()["id"], answers={"r3.0": ["a"]}, decision="accept")
    assert proc.stdin.lines[-1]["response"]["response"]["behavior"] == "allow"
    assert [q["id"] for q in view()["questions"]] == ["r2.1"]
    # One part dismissed, the other answered: the call is not given an answer that
    # silently lacks the dismissed part.
    ask("r4", "Fifth?", "Sixth?")
    await _settle()
    await host.answer("issue-1", view()["id"], answers={}, decision="dismiss:r4.0")
    await host.answer("issue-1", view()["id"], answers={"r4.1": ["a"]}, decision="accept")
    assert proc.stdin.lines[-1]["response"]["request_id"] == "r4"
    assert proc.stdin.lines[-1]["response"]["response"]["behavior"] == "deny"
    # Nothing is evicted, however many questions are open.
    for n in range(25):
        ask(f"m{n}", f"Many {n}?")
    await _settle()
    assert len(view()["questions"]) == 26
    await host.stop()


async def test_a_question_asked_again_replaces_its_open_copy(tmp_path):
    # A chat cut off while waiting asks again; the old copy closes, the rest of its ask
    # stays open, and copies left from before close when the host loads (#3637).
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    def ask(request_id, *questions):
        proc.stdout.emit(
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "input": {"questions": [
                        {"question": q, "header": h, "options": [{"label": "a"}]}
                        for h, q in questions
                    ]},
                },
            }
        )

    def view():
        return host.snapshot()["workers"]["issue-1"]["open_questions"]

    ask("r1", ("Storage", "Where?"), ("Groups", "Which?"))
    await _settle()
    ask("r2", ("storage", "  where? "), ("Web", "Which?"))
    await _settle()
    assert [q["id"] for q in view()["questions"]] == ["r2.0", "r2.1", "r1.1"]
    await host.answer("issue-1", view()["id"], answers={"r1.1": ["a"]}, decision="accept")
    ask("r3", ("Storage", "Where?"), ("Web", "Which?"))
    await _settle()
    assert [q["id"] for q in view()["questions"]] == ["r3.0", "r3.1"]
    await host.stop()
    rec = host._records["issue-1"]
    assert {"r1", "r2"} <= set(rec.closed_question_requests)
    rec.open_questions = [
        {**q, "request_id": r, "id": f"{r}.0"}
        for r in ("old1", "old2")
        for q in rec.open_questions[:1]
    ] + rec.open_questions
    host._save_state()
    loaded, _ = _host(tmp_path)
    assert [q["id"] for q in loaded._records["issue-1"].open_questions] == ["r3.0", "r3.1"]


async def test_background_agent_keeps_worker_effectively_active_after_result(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)

    def append_event(event: dict) -> None:
        with transcript.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    for agent_id in ("agent-1", "agent-2"):
        append_event(
            {
                "type": "user",
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": agent_id,
                },
            }
        )
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["turn_active"] is False
    assert worker["background_agent_count"] == 2
    assert worker["effective_activity"] is True

    append_event(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n"
                "<task-id>agent-1</task-id>\n"
                "<task-id>agent-2</task-id>\n"
                "<status>stopped</status>\n"
                "</task-notification>"
            ),
        }
    )
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 0
    assert worker["effective_activity"] is False

    append_event(
        {
            "type": "user",
            "toolUseResult": {
                "success": True,
                "resumedAgentId": "agent-1",
            },
        }
    )
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 1
    assert worker["effective_activity"] is True
    await host.stop()


async def test_interrupt_clears_background_agents_that_will_never_notify(tmp_path):
    """An interrupted turn produces no task-notification, so its background agent
    ids leaked for the life of the process and deferred every runtime deploy
    (#786). The interrupt aborts those agents with the turn — the set clears."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "toolUseResult": {
                        "isAsync": True,
                        "status": "async_launched",
                        "agentId": "agent-1",
                    },
                }
            )
            + "\n"
        )
    assert host.snapshot()["workers"]["issue-1"]["background_agent_count"] == 1

    async def respond():
        while not any(line.get("type") == "control_request" for line in proc.stdin.lines):
            await asyncio.sleep(0)
        req = next(line for line in proc.stdin.lines if line["type"] == "control_request")
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    responder = asyncio.create_task(respond())
    result = await host.interrupt("issue-1")
    await responder
    assert result.accepted
    # The CLI ends the aborted turn with its own result event.
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 0
    assert worker["effective_activity"] is False
    await host.stop()


async def test_a_background_agent_with_no_notification_ages_out(tmp_path):
    """A leaked id (no stop notification, e.g. an unparsed shape) must not hold
    effective_activity — and with it every runtime deploy — forever (#786)."""
    from agentdeck.providers.claude_code.worker import BACKGROUND_AGENT_MAX_AGE_S

    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)
    with transcript.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "user",
                    "toolUseResult": {
                        "isAsync": True,
                        "status": "async_launched",
                        "agentId": "agent-leaked",
                    },
                }
            )
            + "\n"
        )
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["background_agent_count"] == 1
    live = host._live["issue-1"]
    live.background_agents["agent-leaked"] -= BACKGROUND_AGENT_MAX_AGE_S + 1
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 0
    assert worker["effective_activity"] is False
    await host.stop()


async def test_worker_waiting_on_interaction_is_not_stalled(tmp_path):
    host, spawned = _host(tmp_path, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )
    await _settle()
    # backdate so an ordinary open turn would read as stalled
    host._records["issue-1"].last_delivery_at -= 200
    host._live["issue-1"].last_event_at -= 200
    assert host.snapshot()["workers"]["issue-1"]["stalled"] is False
    await host.stop()


async def test_interactive_prompts_disabled_omits_stdio_flag(tmp_path):
    host, spawned = _host(tmp_path, interactive_prompts=False)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    assert "--permission-prompt-tool" not in spawned[0]["args"]
    await host.stop()
async def test_reader_crash_reaps_child(tmp_path):
    # If the stdout reader dies with the child still alive (e.g. an oversized
    # line), the detached process must be terminated — not left as an orphan that
    # a later deliver would --resume a second time on the same transcript.
    class CrashingStdout:
        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        def emit(self, event: dict) -> None:
            self.queue.put_nowait(json.dumps(event).encode() + b"\n")

        async def readline(self) -> bytes:
            item = await self.queue.get()
            if item == "BOOM":
                raise ValueError("stdout line exceeded limit")
            return item

    processes: list[FakeProcess] = []

    async def factory(*args, **fkwargs):
        proc = FakeProcess()
        proc.stdout = CrashingStdout()
        processes.append(proc)
        asyncio.get_running_loop().call_soon(
            proc.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": f"sid-{len(processes)}"},
        )
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver("issue-1", "go", cwd=str(tmp_path), delegated=False)
    assert result.accepted
    await _settle()
    reader_task = host._live["issue-1"].reader_task
    proc = processes[0]
    proc.stdout.queue.put_nowait("BOOM")  # crash the reader
    await asyncio.gather(reader_task, return_exceptions=True)
    assert proc.returncode is not None  # reaped, not orphaned
    assert proc.stdin.closed
    assert "issue-1" not in host._live
    # Session lineage kept so the next deliver revives a single owner.
    assert host._records["issue-1"].session_id == "sid-1"


def _entry(pid: int, session_id: str, proc_start: str = "42"):
    from agentdeck.providers.claude_code.registry import RegistryEntry

    return RegistryEntry(
        pid=pid,
        session_id=session_id,
        cwd=None,
        started_at=None,
        proc_start=proc_start,
        version=None,
        kind=None,
        entrypoint=None,
    )


def _seed_state(tmp_path: Path, session_id: str | None) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "test.json").write_text(
        json.dumps(
            {"workers": [{"key": "issue-9", "cwd": str(tmp_path), "session_id": session_id}]}
        )
    )
    return state_dir


def test_legacy_worker_state_defaults_to_delegated_role(tmp_path):
    _seed_state(tmp_path, "sid-legacy")
    host, _spawned = _host(tmp_path)

    assert host._records["issue-9"].delegated is True


async def test_legacy_tokenless_record_is_repaired_before_revival(tmp_path):
    _seed_state(tmp_path, "sid-legacy")
    bound = []
    host, spawned = _host(
        tmp_path, bind_session_token=lambda token, key: bound.append((token, key))
    )

    result = await host.deliver("issue-9", "continue", delegated=True)

    assert result.accepted and result.action == "revived"
    token = host._records["issue-9"].session_token
    assert token
    assert bound == [(token, "claude_code:test:sid-auto-1")]
    prompt = spawned[0]["args"][spawned[0]["args"].index("--append-system-prompt") + 1]
    assert token in prompt
    await host.stop()


async def test_reconcile_kills_orphan_at_startup(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, "sid-orphan")
    orphan = [_entry(4242, "sid-orphan")]
    monkeypatch.setattr(worker_mod.registry, "read_registry", lambda root: orphan)
    monkeypatch.setattr(worker_mod.registry, "is_alive", lambda e: True)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert killed == [(4242, worker_mod.signal.SIGKILL)]
    # The record survives the reconcile so a later deliver can revive it.
    assert host._records["issue-9"].session_id == "sid-orphan"


async def test_reconcile_ignores_unowned_and_dead(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, "sid-orphan")
    entries = [_entry(1, "sid-other"), _entry(2, "sid-orphan")]  # unowned + owned
    monkeypatch.setattr(worker_mod.registry, "read_registry", lambda root: entries)
    monkeypatch.setattr(worker_mod.registry, "is_alive", lambda e: e.pid != 2)  # owned one is dead
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert killed == []  # sid-other not ours; sid-orphan pid is dead


async def test_reconcile_noop_without_sessions(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, None)  # record without a session id
    called = {"read": False}

    def _read(root):
        called["read"] = True
        return []

    monkeypatch.setattr(worker_mod.registry, "read_registry", _read)
    ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert called["read"] is False  # no owned session ids → registry never read


# --- replay spine: at-most-once delivery + durable, time-bounded receipts -----


async def test_receipt_is_persisted_before_the_child_write(tmp_path):
    # The receipt for a delivery id must be durable *before* the message is
    # written, so a crash in the write→confirm window replays as already-handled
    # instead of re-delivering. Snapshot the receipt at the moment of the write.
    holder: dict = {}
    seen_at_write: list[dict | None] = []

    class SnoopStdin(FakeStdin):
        def write(self, data: bytes) -> None:
            rec = holder["host"]._records.get("issue-1")
            seen_at_write.append(dict(rec.deliveries.get("d1", {})) if rec else None)
            super().write(data)

    async def factory(*args, **fkwargs):
        proc = FakeProcess()
        proc.stdin = SnoopStdin()
        asyncio.get_running_loop().call_soon(
            proc.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": "sid-x"},
        )
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    holder["host"] = host
    result = await host.deliver(
        "issue-1", "go", cwd=str(tmp_path), delivery_id="d1", delegated=False
    )
    assert result.accepted
    # A receipt already existed at write time, in the *prepared* state (no action).
    assert seen_at_write and seen_at_write[0].get("fingerprint")
    assert "action" not in seen_at_write[0]
    # After success it is finalized with the real action.
    assert host._records["issue-1"].deliveries["d1"]["action"] == "spawned"


async def test_prepared_receipt_replays_uncertain_without_rewriting(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1", "go", cwd=str(tmp_path), delivery_id="d1", delegated=False
    )
    assert first.accepted
    proc = spawned[0]["process"]
    writes_before = len(proc.stdin.lines)
    # Simulate a crash that persisted the prepared receipt but never finalized it.
    host._records["issue-1"].deliveries["d1"].pop("action")
    # Replaying the same delivery id must not write a second time.
    replay = await host.deliver(
        "issue-1", "go", cwd=str(tmp_path), delivery_id="d1", delegated=False
    )
    assert replay.accepted and replay.action == "uncertain"
    assert len(proc.stdin.lines) == writes_before


async def test_delivery_receipts_are_time_bounded_not_fifo_64(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1", "m0", cwd=str(tmp_path), delivery_id="d0", delegated=False
    )
    assert first.action == "spawned"
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid"})
    await _settle()
    # 69 further deliveries — well past the old 64-entry FIFO that would have
    # evicted d0's receipt.
    for i in range(1, 70):
        r = await host.deliver(
            "issue-1", f"m{i}", cwd=str(tmp_path), delivery_id=f"d{i}", delegated=False
        )
        assert r.accepted
    writes = len(proc.stdin.lines)
    assert "d0" in host._records["issue-1"].deliveries  # still remembered
    # Replaying the very first id dedups (cached), rather than re-delivering.
    replay = await host.deliver(
        "issue-1", "m0", cwd=str(tmp_path), delivery_id="d0", delegated=False
    )
    assert replay.accepted and replay.action == "spawned"
    assert len(proc.stdin.lines) == writes


async def test_clean_write_failure_keeps_delivery_retryable(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1", "go", cwd=str(tmp_path), delivery_id="d1", delegated=False
    )
    assert first.accepted
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid"})
    await _settle()

    def boom(data: bytes) -> None:
        raise BrokenPipeError("pipe")

    proc.stdin.write = boom  # the steer write fails cleanly — nothing sent
    second = await host.deliver(
        "issue-1", "steer", cwd=str(tmp_path), delivery_id="d2", delegated=False
    )
    assert not second.accepted
    # A cleanly-failed write leaves no receipt, so the id can be retried.
    assert "d2" not in host._records["issue-1"].deliveries


async def test_fresh_delivery_replay_dedups_to_one_spawn(tmp_path):
    # New-chat (issue #37) delivers with fresh=True + a delivery id. A retry with
    # the same id must dedup at the real host — one spawn, cached result — NOT
    # terminate-and-respawn a second worker via the fresh path. (End-to-end proof
    # that the fresh=True + repeated delivery_id combination collapses to one.)
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "chat-x", "hi", cwd=str(tmp_path), fresh=True, delivery_id="act-1", delegated=False
    )
    assert first.accepted and first.action == "spawned"
    await _settle()
    replay = await host.deliver(
        "chat-x", "hi", cwd=str(tmp_path), fresh=True, delivery_id="act-1", delegated=False
    )
    assert replay.accepted and replay.action == "spawned"  # cached original result
    assert len(spawned) == 1  # not respawned despite fresh=True


async def test_reuploaded_image_paths_do_not_break_dedup(tmp_path):
    # A retried image new-chat re-uploads identical bytes to a NEW random path;
    # the delivery fingerprint must ignore the path so the retry dedups instead of
    # falsely tripping delivery_id_conflict.
    host, spawned = _host(tmp_path)
    (tmp_path / "a.png").write_bytes(b"img")
    (tmp_path / "b.png").write_bytes(b"img")
    first = await host.deliver(
        "chat-y", "look", cwd=str(tmp_path), fresh=True,
        images=[str(tmp_path / "a.png")], delivery_id="act-2",
        delegated=False,
    )
    assert first.accepted and first.action == "spawned"
    await _settle()
    replay = await host.deliver(
        "chat-y", "look", cwd=str(tmp_path), fresh=True,
        images=[str(tmp_path / "b.png")], delivery_id="act-2",  # same bytes, new path
        delegated=False,
    )
    assert replay.accepted and replay.action == "spawned"  # deduped, not conflict
    assert len(spawned) == 1


async def test_uncertain_replay_reports_live_session_id(tmp_path):
    # A fresh spawn's receipt is frozen at prepare time with session_id=None. If it
    # is left prepared-but-unconfirmed (crash before finalize), the uncertain replay
    # must report the now-live rec.session_id, not the stale None.
    host, _ = _host(tmp_path)
    await host.deliver(
        "chat-z", "go", cwd=str(tmp_path), fresh=True, delivery_id="act-3", delegated=False
    )
    await _settle()
    rec = host._records["chat-z"]
    assert rec.session_id  # populated from the worker's system/init
    receipt = rec.deliveries["act-3"]
    receipt.pop("action", None)   # simulate prepared-but-unconfirmed
    receipt["session_id"] = None  # frozen before the session existed
    replay = await host.deliver(
        "chat-z", "go", cwd=str(tmp_path), fresh=True, delivery_id="act-3", delegated=False
    )
    assert replay.action == "uncertain"
    assert replay.session_id == rec.session_id  # live session, not None


async def test_revive_reuses_spawn_permission_mode(tmp_path):
    # A follow-up that revives a finished worker must reuse the mode it was spawned
    # with — not escalate to the host default. Spawn a restrictive "plan" chat under
    # a bypassPermissions host default, let it exit, then revive with no explicit
    # mode and assert the revive still uses "plan".
    host, spawned = _host(tmp_path, permission_mode="bypassPermissions")
    await host.deliver(
        "chat-p", "go", cwd=str(tmp_path), fresh=True, permission_mode="plan", delegated=False
    )
    await _settle()
    args0 = spawned[0]["args"]
    assert args0[args0.index("--permission-mode") + 1] == "plan"

    # Worker exits → the next deliver revives it.
    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")  # end the reader loop → dropped from _live
    await _settle()

    await host.deliver("chat-p", "again", cwd=str(tmp_path), delegated=False)  # no mode, not fresh
    await _settle()
    assert len(spawned) == 2  # revived, not steered
    args1 = spawned[1]["args"]
    assert "--resume" in args1  # it is a revive
    assert args1[args1.index("--permission-mode") + 1] == "plan"  # not bypassPermissions


async def test_revive_reuses_spawn_effort(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "chat-e", "go", cwd=str(tmp_path), fresh=True, effort="high", delegated=False
    )
    await _settle()
    assert spawned[0]["args"][spawned[0]["args"].index("--effort") + 1] == "high"

    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()

    await host.deliver("chat-e", "again", cwd=str(tmp_path), effort="low", delegated=False)
    await _settle()
    args = spawned[1]["args"]
    assert "--resume" in args
    assert args[args.index("--effort") + 1] == "high"


async def test_write_then_init_timeout_is_uncertain_not_resent(tmp_path, monkeypatch):
    # If the message is written but the child then fails to initialize by TIMEOUT
    # (still possibly alive/mid-turn), we must NOT re-send it (that could run the
    # task twice). It returns uncertain, spawns exactly once, and a same-id retry
    # replays uncertain without a second spawn.
    from agentdeck.providers.claude_code import worker as worker_mod

    monkeypatch.setattr(worker_mod, "INIT_TIMEOUT_S", 0.1)
    processes: list[FakeProcess] = []

    async def factory(*args, **kwargs):
        proc = FakeProcess()  # alive, never emits init, never exits → init times out
        processes.append(proc)
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver(
        "chat-w", "go", cwd=str(tmp_path), fresh=True, delivery_id="w1", delegated=False
    )
    assert result.action == "uncertain"
    assert len(processes) == 1  # exactly one spawn — not re-sent to a fresh worker
    assert processes[0].stdin.lines  # the message was written once
    assert "w1" in host._records["chat-w"].deliveries  # receipt kept, not forgotten

    replay = await host.deliver(
        "chat-w", "go", cwd=str(tmp_path), fresh=True, delivery_id="w1", delegated=False
    )
    assert replay.action == "uncertain"
    assert len(processes) == 1  # same-id retry still does not re-send
    await host.stop()


def test_tool_approval_fails_closed_without_an_explicit_accept():
    """#427: `allow` must require an explicit accept, not be the default.

    The control plane has no authentication, so a missing or misspelled decision
    falling through to `{"behavior": "allow"}` let any local caller approve any
    pending tool call unattended.
    """
    pending = {"tool_name": "Bash", "input": {"command": "rm -rf /"}}

    for decision in (None, "", "aprove", "allow", "yes", "ACCEPT"):
        body = ClaudeWorkerHost._build_tool_decision(pending, {}, decision)
        assert body["behavior"] == "deny", f"{decision!r} must not approve a tool call"

    for decision in ("accept", "acceptForSession"):
        body = ClaudeWorkerHost._build_tool_decision(pending, {}, decision)
        assert body["behavior"] == "allow"
        assert body["updatedInput"] == pending["input"]

    cancelled = ClaudeWorkerHost._build_tool_decision(pending, {}, "cancel")
    assert cancelled["behavior"] == "deny" and cancelled["interrupt"] is True


def _emit_gate(proc, request_id: str) -> None:
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "rm -rf build"},
            },
        }
    )


def _control_lines(proc) -> list[dict]:
    return [line for line in proc.stdin.lines if line.get("type") == "control_request"]


def _settings(args) -> dict:
    return json.loads(Path(args[args.index("--settings") + 1]).read_text())


async def test_every_auto_chat_gets_the_skip_rules_and_block_hooks(tmp_path):
    # #2271: not only kanban workers; a person's own chat gets them too. It may
    # start in bypass (#2319), but the file follows its own auto mode, so switching
    # bypass off lands in auto with the skip list.
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    args = spawned[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert host._base_permission_mode(host._records["issue-1"]) == "auto"
    settings = _settings(args)
    assert "Bash(git status *)" in settings["permissions"]["allow"]
    programs = {rule.split("(")[1].split()[0] for rule in settings["permissions"]["allow"]}
    assert "gh" not in programs and "curl" not in programs
    assert set(settings["hooks"]) == {"PermissionDenied", "PreToolUse"}
    denied = settings["hooks"]["PermissionDenied"][0]["hooks"][0]
    assert denied["command"].endswith("scripts/claude_permission_hook.py")
    assert denied["timeout"] >= 7 * 24 * 3600
    await host.stop()


async def test_a_chat_outside_auto_mode_gets_the_block_hooks_but_no_skip_list(tmp_path):
    # A read-only delegate runs in plan mode; an allow rule for `git commit` or
    # `cp` would let it write.
    host, spawned = _host(tmp_path)
    await host.deliver(
        "issue-1", "look", cwd=str(tmp_path), permission_mode="plan", delegated=True
    )
    settings = _settings(spawned[0]["args"])
    assert settings["permissions"]["allow"] == []
    assert set(settings["hooks"]) == {"PermissionDenied", "PreToolUse"}
    await host.stop()


def test_configured_skip_rules_are_added_and_guarded_ones_dropped(tmp_path):
    host, _ = _host(
        tmp_path, classifier_skip_rules=["Bash(make test *)", "Bash(gh pr view *)", "Bash(*)"]
    )
    allow = json.loads(host._settings_files[True].read_text())["permissions"]["allow"]
    assert "Bash(make test *)" in allow
    assert "Bash(gh pr view *)" not in allow and "Bash(*)" not in allow


async def test_the_classifier_fallback_gate_is_surfaced_not_denied(tmp_path):
    # After 3 blocks in a row the CLI asks over can_use_tool. Nothing is denied
    # on the boss's behalf any more (#2271).
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    _emit_gate(proc, "g1")
    await _settle()

    pending = host.snapshot()["workers"]["issue-1"]["pending_interaction"]
    assert pending["id"] == "g1" and pending["kind"] == "permission"
    assert not any(line.get("type") == "control_response" for line in proc.stdin.lines)
    result = await host.answer("issue-1", "g1", answers={}, decision="accept")
    assert result.accepted
    assert proc.stdin.lines[-1]["response"]["response"]["behavior"] == "allow"
    await host.stop()


def _emit_safety_check(proc, request_id: str) -> None:
    # The shape Claude Code 2.1.280 sends in bypass mode (#2342).
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "rm -f $E/*.png"},
                "decision_reason": 'Dangerous rm operation on possibly-empty variable path: '
                '$E/*.png (rewrite it as "${E:?}"/*.png or use a literal path)',
                "decision_reason_type": "safetyCheck",
                "classifier_approvable": False,
            },
        }
    )


async def test_a_safety_check_in_bypass_is_refused_with_its_reason(tmp_path):
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    _emit_safety_check(proc, "s1")
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    answer = proc.stdin.lines[-1]
    assert answer["type"] == "control_response"
    assert answer["response"]["request_id"] == "s1"
    body = answer["response"]["response"]
    assert body["behavior"] == "deny" and "interrupt" not in body
    assert '"${E:?}"/*.png' in body["message"]
    await host.stop()


async def test_a_safety_check_with_bypass_off_still_asks_the_boss(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    _emit_safety_check(proc, "s1")
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"]["id"] == "s1"
    assert not any(line.get("type") == "control_response" for line in proc.stdin.lines)
    await host.stop()


async def _live_block(tmp_path, command: str = "gh api -X DELETE repos/o/r"):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    token = host._records["issue-1"].session_token or ""
    block_id, refusal = await host.report_block(
        "issue-1",
        session_token=token,
        tool_name="Bash",
        tool_input={"command": command, "description": "Delete the repo"},
        reason="[Irreversible Destruction]",
    )
    assert refusal == "" and block_id
    return host, spawned, block_id


async def test_a_block_waits_on_the_chat_until_allowed_once(tmp_path):
    host, _, block_id = await _live_block(tmp_path)

    pending = host.snapshot()["workers"]["issue-1"]["pending_interaction"]
    assert pending["id"] == block_id and pending["kind"] == "classifier_block"
    assert pending["command"] == "gh api -X DELETE repos/o/r"
    assert pending["message"] == "[Irreversible Destruction]"
    assert pending["decisions"] == ["accept", "decline"]
    assert await host.wait_block("issue-1", block_id, timeout_s=0.01) == "pending"

    waiter = asyncio.create_task(host.wait_block("issue-1", block_id, timeout_s=5))
    await _settle()
    result = await host.answer("issue-1", block_id, answers={}, decision="accept")

    assert result.accepted
    assert await waiter == "allowed"
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    def used(key: str, command: str) -> bool:
        return consume_grant(
            host._artifact_root, "claude_code:test", key, "Bash", {"command": command}
        )

    # Exactly one retry of exactly this call, whitespace aside, in this chat.
    assert not used("issue-2", "gh api -X DELETE repos/o/r")
    assert not used("issue-1", "gh api -X DELETE repos/o/x")
    assert used("issue-1", "gh  api -X DELETE   repos/o/r")
    assert not used("issue-1", "gh api -X DELETE repos/o/r")
    await host.stop()


async def test_keep_blocked_answers_denied_and_grants_nothing(tmp_path):
    host, _, block_id = await _live_block(tmp_path)

    result = await host.answer("issue-1", block_id, answers={}, decision="decline")

    assert result.accepted
    assert await host.wait_block("issue-1", block_id, timeout_s=1) == "denied"
    # Collected once; asking again finds nothing.
    assert await host.wait_block("issue-1", block_id, timeout_s=1) == "gone"
    assert not consume_grant(
        host._artifact_root, "claude_code:test", "issue-1", "Bash",
        {"command": "gh api -X DELETE repos/o/r"},
    )
    await host.stop()


async def test_an_open_gate_shows_before_a_block(tmp_path):
    host, spawned, block_id = await _live_block(tmp_path)
    _emit_gate(spawned[0]["process"], "g1")
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"]["id"] == "g1"
    await host.answer("issue-1", "g1", answers={}, decision="decline")
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"]["id"] == block_id
    await host.stop()


async def test_a_block_from_the_wrong_chat_or_a_dead_worker_is_refused(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    host._records["issue-1"].session_token = "right"

    wrong = await host.report_block(
        "issue-1", session_token="wrong", tool_name="Bash", tool_input={}, reason=""
    )
    unknown = await host.report_block(
        "nobody", session_token="", tool_name="Bash", tool_input={}, reason=""
    )

    assert wrong == ("", "session_token_mismatch")
    assert unknown == ("", "not_live")
    await host.stop()


async def test_a_worker_that_exits_drops_its_blocks_and_grants(tmp_path):
    host, spawned, block_id = await _live_block(tmp_path)
    write_grant(host._artifact_root, "claude_code:test", "issue-1", "Bash", {"command": "x"})

    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()

    assert await host.wait_block("issue-1", block_id, timeout_s=1) == "gone"
    assert not consume_grant(
        host._artifact_root, "claude_code:test", "issue-1", "Bash", {"command": "x"}
    )
    await host.stop()


def test_a_record_saved_with_the_old_switch_loads_without_it(tmp_path):
    # Before #2259 the switch moved the record's mode and kept the original
    # aside; #2271 removed the switch. Both old shapes must still load.
    state = tmp_path / "state"
    state.mkdir()
    (state / "test.json").write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "key": "on",
                        "cwd": str(tmp_path),
                        "permission_mode": "default",
                        "permission_prompts": True,
                        "permission_mode_before_prompts": None,
                    },
                    {
                        "key": "off",
                        "cwd": str(tmp_path),
                        "permission_mode": "plan",
                        "permission_prompts": False,
                    },
                ]
            }
        )
    )
    host, _ = _host(tmp_path)
    assert host._records["on"].permission_mode is None
    assert host._records["off"].permission_mode == "plan"
    assert not hasattr(host._records["on"], "permission_prompts")


async def test_hard_quota_result_opens_account_circuit(tmp_path):
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "You've hit your weekly usage limit",
    })
    await _settle()

    assert account_health.open_circuit(cache, "claude_code:test") is not None
    await host.stop()


async def test_the_synthetic_weekly_limit_answer_opens_the_circuit(tmp_path):
    """The shape production actually produces (#1733).

    A spent window is not a crash: the CLI answers the turn with one synthetic line and
    reports an ordinary successful result. A classifier that insists on ``is_error`` never
    fires on a real host, which is the whole defect this card was filed about.
    """
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "You've hit your weekly limit · resets 11am (Europe/Helsinki)",
    })
    await _settle()

    circuit = account_health.open_circuit(cache, "claude_code:test")
    assert circuit is not None
    assert "weekly limit" in circuit.evidence
    # The CLI called the turn a success; the row says what really ended it (#2862).
    row = host.snapshot()["workers"]["issue-1"]
    assert row["last_result_subtype"] == "success"
    assert "weekly limit" in row["last_result_limit"]
    assert row["last_result_limit_reset_at"] > 0
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success", "result": "done"})
    await _settle()
    cleared = host.snapshot()["workers"]["issue-1"]
    assert cleared["last_result_limit"] == ""
    assert cleared["last_result_limit_reset_at"] == 0.0
    await host.stop()


async def test_a_lost_connection_or_sign_in_schedules_a_continuation_but_trips_nothing(tmp_path):
    """A host with no network ends turns with the CLI's own "API Error" line (#3086).

    That is not the account's fault, so no circuit opens, but the chat is due a retry once
    the network can have come back. A worker merely quoting such a line is not due one.
    """
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    emit = spawned[0]["process"].stdout.emit
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": "API Error: Can’t reach the API server — check your internet or DNS (EAI_AGAIN)",
    })
    await _settle()
    row = host.snapshot()["workers"]["issue-1"]
    assert account_health.open_circuit(cache, "claude_code:test") is None
    assert row["last_result_limit"] == ""
    assert row["last_result_limit_reset_at"] == row["last_result_at"] + NETWORK_RETRY_DELAY_S
    # A sign-in refresh lost to another process on this host is retried sooner (#3165).
    emit({
        "type": "result",
        "subtype": "success",
        "result": (
            "Failed to refresh OAuth token: another Claude Code process is refreshing it or "
            "exited mid-refresh. This is usually transient; retry in a minute, and if it "
            "persists close other Claude Code processes or sign in again"
        ),
    })
    await _settle()
    row = host.snapshot()["workers"]["issue-1"]
    assert row["last_result_limit_reset_at"] == row["last_result_at"] + LOGIN_REFRESH_RETRY_DELAY_S
    for answer in (
        "API Error: 529 Overloaded",
        "I saw `API Error: Connection refused (ECONNREFUSED)` in the log and fixed the proxy.",
        "I fixed it. Failed to refresh OAuth token: was the old log line.",
        "Failed to refresh OAuth token: invalid_grant. Please run /login",
    ):
        emit({"type": "result", "subtype": "success", "result": answer})
        await _settle()
        assert host.snapshot()["workers"]["issue-1"]["last_result_limit_reset_at"] == 0.0
    await host.stop()


async def test_a_worker_discussing_quota_does_not_break_its_own_account(tmp_path):
    """A turn that merely *talks* about limits is not evidence about the account (#1733).

    A kanban worker working this very issue writes these words into its own final answer.
    Tripping the account on the agent's prose would take the fleet down for a report.
    """
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": (
            "I reviewed the circuit breaker. In the audit main Claude produced 16 hard\n"
            "weekly limit responses, and each one said \"You've hit your weekly limit\".\n"
            "The classifier now anchors on that line instead of scanning the transcript."
        ),
    })
    await _settle()

    assert account_health.open_circuit(cache, "claude_code:test") is None
    await host.stop()


async def test_tool_output_quoting_a_limit_does_not_open_the_circuit(tmp_path):
    """Only fields the provider itself vouches for are read (#1733).

    A ``result`` event carries the turn's own numbers and, in some shapes, nested payloads.
    Grepping every nested string lets a `grep` hit in the worker's own repository decide
    whether the whole account stays eligible.
    """
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "permission_denials": [
            {"tool_name": "Bash", "input": {"command": "grep usage_limit_exceeded src/"}},
        ],
    })
    await _settle()

    assert account_health.open_circuit(cache, "claude_code:test") is None
    await host.stop()


async def test_a_provider_error_field_opens_the_circuit(tmp_path):
    """The crash path still counts: ``errors`` is the provider's own verdict (#1733)."""
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "errors": ["API Error: 429 {\"type\":\"usage_limit_exceeded\"}"],
    })
    await _settle()

    assert account_health.open_circuit(cache, "claude_code:test") is not None
    await host.stop()


async def test_transient_rate_limit_result_does_not_open_account_circuit(tmp_path):
    cache = tmp_path / "usage"
    host, spawned = _host(tmp_path, usage_cache_dir=cache)
    await host.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)

    spawned[0]["process"].stdout.emit({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "rate_limit_exceeded: retry this request later",
    })
    await _settle()

    assert account_health.open_circuit(cache, "claude_code:test") is None
    await host.stop()


async def test_a_leftover_reader_task_is_cancelled_and_collected(tmp_path, request):
    """#1834: the shared session loop must not carry read loops between tests.

    Every async test in a worker now runs on one event loop, so a host a test
    never stopped is no longer swept up by its own loop closing — its reader
    would keep going into the next test and be collected mid-await, which the
    suite reported as "coroutine ignored GeneratorExit". `conftest` cancels and
    collects them after every test; this asserts on that machinery directly
    rather than on two tests running in order, because `--dist load` is free to
    hand two tests in this file to different workers.
    """
    # The collector is only useful if something actually runs it after every
    # test, so assert the autouse fixture is still attached to this very test
    # rather than trusting that it is.
    assert "_collect_leftover_reader_tasks" in request.fixturenames, (
        "the autouse cleanup in tests/conftest.py is no longer applied, so a "
        "leftover reader would survive into the next test on this loop"
    )

    host, spawned = _host(tmp_path)
    delivered = await host.deliver(
        "issue-1", "m0", cwd=str(tmp_path), delivery_id="d0", delegated=False
    )
    assert delivered.action == "spawned"
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid"}
    )
    await _settle()

    # Deliberately never stopped: this is the state a forgetful test leaves.
    alive = conftest.leftover_reader_tasks()
    assert alive, "the delivery should have left a named reader task running"

    collected = await conftest.collect_leftover_reader_tasks()
    assert collected, "the collector found nothing to collect"
    assert not conftest.leftover_reader_tasks(), (
        "a reader task survived collection, so it would run on into the next "
        "test on this worker's shared loop"
    )
    assert all(task.done() for task in collected), (
        "a cancelled reader was left pending rather than awaited, which asyncio "
        "reports as 'Task was destroyed but it is pending'"
    )


# --- the durable "this chat owes an answer" marker (#1844) -------------------


async def test_a_delivery_opens_the_turn_marker_on_disk(tmp_path):
    """Revival reads this off the state file after the process is gone, so it has
    to be durable and not merely a flag on the live worker."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["turn_open_at"] > 0
    state = json.loads((tmp_path / "state" / "test.json").read_text())
    assert state["workers"][0]["turn_open_at"] > 0
    await host.stop()


async def test_a_finished_turn_closes_the_marker(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["turn_open_at"] == 0.0
    await host.stop()


async def test_a_teardown_result_leaves_the_marker_open(tmp_path):
    """The one that cost #1844: a worker killed mid-turn emits its own `result`
    on the way out, and that must not read as a finished turn."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    spawned[0]["process"].stdout.emit(
        {"type": "result", "subtype": "error_during_execution"}
    )
    await _settle()

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["turn_open_at"] > 0
    assert worker["last_result_subtype"] == "error_during_execution"
    # And the deduction revival used to rely on now says the opposite.
    assert worker["last_result_at"] >= worker["last_delivery_at"]
    await host.stop()


async def test_a_continuation_turn_reopens_the_marker(tmp_path):
    """A background task finishing is delivered to the CLI as a user message, so
    the next turn starts with no delivery of ours to stamp (#907)."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_open_at"] == 0.0

    # A background helper agent's own messages after the turn ended do not reopen it:
    # the stream carries them with its Agent call's id (#3529).
    for kind in ("assistant", "user"):
        proc.stdout.emit(
            {"type": kind, "parent_tool_use_id": "toolu_agent", "message": {"content": "x"}}
        )
    await _settle()
    worker = host.snapshot()["workers"]["issue-1"]
    assert (worker["turn_open_at"], worker["turn_active"]) == (0.0, False)

    proc.stdout.emit({"type": "user", "message": {"content": "task 9 finished"}})
    await _settle()

    assert host.snapshot()["workers"]["issue-1"]["turn_open_at"] > 0
    await host.stop()


async def test_the_marker_survives_a_reload_from_disk(tmp_path):
    host, _spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    opened = host.snapshot()["workers"]["issue-1"]["turn_open_at"]
    await host.stop()

    fresh, _ = _host(tmp_path)

    assert fresh.snapshot()["workers"]["issue-1"]["turn_open_at"] == opened
    await fresh.stop()


async def test_state_written_before_the_marker_existed_still_loads(tmp_path):
    """Upgrading must not drop every worker record on the floor."""
    state_path = tmp_path / "state" / "test.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"workers": [{"key": "issue-1", "cwd": str(tmp_path), "session_id": "sid-old"}]})
    )

    host, _spawned = _host(tmp_path)

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["session_id"] == "sid-old"
    assert worker["turn_open_at"] == 0.0
    await host.stop()


# --- a deliberate stop is not a death (#1844 review) ------------------------


def _turn_open_on_disk(tmp_path: Path) -> float:
    """The marker as a fresh runtime would read it at boot."""
    state = json.loads((tmp_path / "state" / "test.json").read_text())
    return state["workers"][0]["turn_open_at"]


async def test_stopping_a_worker_closes_the_turn_marker(tmp_path):
    """An operator stop goes through the same SIGTERM as a runtime teardown, so
    the CLI may write the same aborted result. The next boot must not resume the
    work somebody just cancelled."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    assert _turn_open_on_disk(tmp_path) > 0

    result = await host.stop_worker("issue-1")
    assert result.accepted
    spawned[0]["process"].stdout.emit(
        {"type": "result", "subtype": "error_during_execution"}
    )
    await _settle()

    assert _turn_open_on_disk(tmp_path) == 0.0
    reloaded, _ = _host(tmp_path)
    assert reloaded.snapshot()["workers"]["issue-1"]["turn_open_at"] == 0.0
    assert reloaded.snapshot()["workers"]["issue-1"]["session_id"]  # lineage kept
    await reloaded.stop()
    await host.stop()


async def test_parking_a_worker_closes_the_turn_marker(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert (await host.park_worker("issue-1")).accepted
    spawned[0]["process"].stdout.emit(
        {"type": "result", "subtype": "error_during_execution"}
    )
    await _settle()

    assert _turn_open_on_disk(tmp_path) == 0.0
    await host.stop()


async def test_a_new_delivery_after_a_stop_opens_the_turn_again(tmp_path):
    host, _spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.stop_worker("issue-1")
    assert _turn_open_on_disk(tmp_path) == 0.0

    await host.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert _turn_open_on_disk(tmp_path) > 0
    await host.stop()


async def test_an_acknowledged_interrupt_closes_the_turn_marker(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    proc = spawned[0]["process"]

    async def ack():
        while not any(
            line.get("type") == "control_request" for line in proc.stdin.lines
        ):
            await asyncio.sleep(0)
        req = next(
            line for line in proc.stdin.lines if line.get("type") == "control_request"
        )
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    acker = asyncio.create_task(ack())
    result = await host.interrupt("issue-1")
    await acker
    assert result.accepted

    assert _turn_open_on_disk(tmp_path) == 0.0
    await host.stop()


async def test_a_refused_interrupt_keeps_the_turn_marker(tmp_path):
    """The turn is still running, so its claim on a revival must survive."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    proc = spawned[0]["process"]

    async def refuse():
        while not any(
            line.get("type") == "control_request" for line in proc.stdin.lines
        ):
            await asyncio.sleep(0)
        req = next(
            line for line in proc.stdin.lines if line.get("type") == "control_request"
        )
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": req["request_id"],
                    "error": "interrupt is not supported here",
                },
            }
        )

    refuser = asyncio.create_task(refuse())
    result = await host.interrupt("issue-1")
    await refuser
    assert not result.accepted

    assert _turn_open_on_disk(tmp_path) > 0
    await host.stop()


async def test_a_runtime_shutdown_leaves_the_turn_marker_open(tmp_path):
    """The case the marker exists for: nobody cancelled this, the runtime died."""
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()

    await host.stop()
    spawned[0]["process"].stdout.emit(
        {"type": "result", "subtype": "error_during_execution"}
    )
    await _settle()

    assert _turn_open_on_disk(tmp_path) > 0


# --- the turn is open from the write, not from the acknowledgement ----------


def _host_without_init(tmp_path: Path) -> tuple[ClaudeWorkerHost, list]:
    """Like the shared harness, but the CLI never emits its `init` event, so a
    spawning delivery stays parked in the initialization wait."""
    spawned: list[dict] = []

    async def factory(*args, **kwargs):
        process = FakeProcess()
        spawned.append({"args": args, "kwargs": kwargs, "process": process})
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    return host, spawned


async def test_a_spawn_cut_off_before_init_still_records_the_open_turn(tmp_path):
    """The payload is in the child's stdin and the runtime dies while waiting for
    `init`. Nothing stamps `last_delivery_at` on that path, so without the
    pre-write marker the delivered turn is invisible to the next boot."""
    host, spawned = _host_without_init(tmp_path)
    delivering = asyncio.create_task(
        host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    )
    while not spawned or not spawned[0]["process"].stdin.lines:
        await asyncio.sleep(0)

    assert _turn_open_on_disk(tmp_path) > 0
    state = json.loads((tmp_path / "state" / "test.json").read_text())
    assert state["workers"][0]["last_delivery_at"] == 0.0  # the old rule sees nothing

    delivering.cancel()
    try:
        await delivering
    except asyncio.CancelledError:
        pass
    await host.stop()


async def test_a_write_that_provably_reached_nothing_opens_no_turn(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert _turn_open_on_disk(tmp_path) == 0.0

    def explode(_data):
        raise BrokenPipeError("the child is gone")

    spawned[0]["process"].stdin.write = explode
    result = await host.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert not result.accepted
    assert _turn_open_on_disk(tmp_path) == 0.0
    await host.stop()


# --- lifecycle -> save/load -> boot recovery (#1844 re-review) --------------
#
# The marker assertions above stop at the state file. These carry the reloaded
# snapshot into `find_survivors`, which is what boot actually asks, and they do it
# with **no terminal result** — `_terminate` escalates to SIGKILL and drops the
# reader, so a final `result` is never guaranteed.


def _boot_survivors(host: ClaudeWorkerHost):
    from agentdeck.revival import find_survivors

    return find_survivors(
        {("claude_code", "test"): host.snapshot()}, now=time.time(), ttl_s=3600.0
    )


async def _delivered_host(tmp_path: Path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "work it", cwd=str(tmp_path), delegated=False)
    await _settle()
    return host, spawned


async def test_a_stopped_worker_is_not_revived_at_boot_without_any_result(tmp_path):
    host, _spawned = await _delivered_host(tmp_path)
    assert await host.stop_worker("issue-1")
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert _boot_survivors(reloaded) == []
    await reloaded.stop()


async def test_a_parked_worker_is_not_revived_at_boot_without_any_result(tmp_path):
    host, _spawned = await _delivered_host(tmp_path)
    assert (await host.park_worker("issue-1")).accepted
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert _boot_survivors(reloaded) == []
    await reloaded.stop()


async def test_an_interrupted_worker_is_not_revived_at_boot_without_any_result(tmp_path):
    host, spawned = await _delivered_host(tmp_path)
    proc = spawned[0]["process"]

    async def ack():
        while not any(
            line.get("type") == "control_request" for line in proc.stdin.lines
        ):
            await asyncio.sleep(0)
        req = next(
            line for line in proc.stdin.lines if line.get("type") == "control_request"
        )
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    acker = asyncio.create_task(ack())
    assert (await host.interrupt("issue-1")).accepted
    await acker
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert _boot_survivors(reloaded) == []
    await reloaded.stop()


async def test_a_message_sent_after_a_stop_is_revived_at_boot(tmp_path):
    """A cancellation only speaks for the work it cancelled."""
    host, _spawned = await _delivered_host(tmp_path)
    await host.stop_worker("issue-1")
    await host.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    await _settle()
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert [s.reason for s in _boot_survivors(reloaded)] == ["cut_off"]
    await reloaded.stop()


async def test_a_runtime_shutdown_is_revived_at_boot_without_any_result(tmp_path):
    """Nobody cancelled this one, so the open turn still speaks for it."""
    host, _spawned = await _delivered_host(tmp_path)
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert [s.reason for s in _boot_survivors(reloaded)] == ["cut_off"]
    await reloaded.stop()


async def test_a_record_written_before_the_markers_existed_is_still_revived(tmp_path):
    """Absent is "nothing deliberate was recorded", not "cancelled": a legacy
    record with an unanswered delivery must keep the old behaviour."""
    state_path = tmp_path / "state" / "test.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    state_path.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "key": "issue-1",
                        "cwd": str(tmp_path),
                        "session_id": "sid-legacy",
                        "last_delivery_at": now,
                        "last_result_at": now - 60,
                    }
                ]
            }
        )
    )

    host, _spawned = _host(tmp_path)

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["turn_open_at"] == 0.0 and worker["turn_closed_at"] == 0.0
    assert [s.reason for s in _boot_survivors(host)] == ["cut_off"]
    await host.stop()


async def test_a_legacy_record_that_finished_its_turn_is_left_alone(tmp_path):
    state_path = tmp_path / "state" / "test.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    state_path.write_text(
        json.dumps(
            {
                "workers": [
                    {
                        "key": "issue-1",
                        "cwd": str(tmp_path),
                        "session_id": "sid-legacy",
                        "last_delivery_at": now - 60,
                        "last_result_at": now,
                    }
                ]
            }
        )
    )

    host, _spawned = _host(tmp_path)

    assert _boot_survivors(host) == []
    await host.stop()


# --- a drain failure is uncertain, not refused ------------------------------


class _DrainFails:
    """stdin that accepts the whole payload and then fails to flush it."""

    def __init__(self):
        self.lines: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        raise BrokenPipeError("the child went away mid-flush")

    def close(self) -> None:
        self.closed = True


async def test_a_drain_failure_after_the_payload_left_keeps_the_open_turn(tmp_path):
    """The message may already be in the child, so it is uncertain rather than
    refused: the turn keeps its claim on a revival and nothing re-sends it."""
    host, spawned = await _delivered_host(tmp_path)
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert _turn_open_on_disk(tmp_path) == 0.0

    payload_holder = _DrainFails()
    proc.stdin = payload_holder
    result = await host.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    await _settle()

    assert not result.accepted
    assert payload_holder.lines  # the payload did leave us
    assert _turn_open_on_disk(tmp_path) > 0
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert [s.reason for s in _boot_survivors(reloaded)] == ["cut_off"]
    await reloaded.stop()


async def test_a_drain_failure_keeps_the_receipt_so_a_retry_is_uncertain(tmp_path):
    host, spawned = await _delivered_host(tmp_path)
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    proc.stdin = _DrainFails()
    await host.deliver(
        "issue-1",
        "carry on",
        cwd=str(tmp_path),
        delegated=False,
        delivery_id="d-drain",
    )
    await _settle()

    state = json.loads((tmp_path / "state" / "test.json").read_text())
    assert "d-drain" in state["workers"][0]["deliveries"]
    await host.stop()


async def test_a_message_after_a_stop_that_dies_before_init_is_revived(tmp_path):
    """The cancellation is older than the open turn, so it does not speak for it.

    The payload is in the child's stdin and the runtime dies waiting for `init`,
    which is the one window where the delivery stamp is still the pre-stop one.
    """
    host, _spawned = await _delivered_host(tmp_path)
    assert await host.stop_worker("issue-1")
    state = json.loads((tmp_path / "state" / "test.json").read_text())
    stopped_delivery_at = state["workers"][0]["last_delivery_at"]

    held, spawned = _host_without_init(tmp_path)
    delivering = asyncio.create_task(
        held.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    )
    while not spawned or not spawned[0]["process"].stdin.lines:
        await asyncio.sleep(0)
    delivering.cancel()
    try:
        await delivering
    except asyncio.CancelledError:
        pass
    await held.stop()

    state = json.loads((tmp_path / "state" / "test.json").read_text())
    assert state["workers"][0]["last_delivery_at"] == stopped_delivery_at
    assert state["workers"][0]["turn_open_at"] > state["workers"][0]["turn_closed_at"]

    reloaded, _ = _host(tmp_path)
    assert [s.reason for s in _boot_survivors(reloaded)] == ["cut_off"]
    await reloaded.stop()
    await host.stop()


async def test_a_message_after_an_interrupt_that_fails_in_drain_is_revived(tmp_path):
    """Uncertain, not refused: the child may have it, and the older interrupt
    cannot speak for a turn that started after it."""
    host, spawned = await _delivered_host(tmp_path)
    proc = spawned[0]["process"]

    async def ack():
        while not any(
            line.get("type") == "control_request" for line in proc.stdin.lines
        ):
            await asyncio.sleep(0)
        req = next(
            line for line in proc.stdin.lines if line.get("type") == "control_request"
        )
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    acker = asyncio.create_task(ack())
    assert (await host.interrupt("issue-1")).accepted
    await acker
    assert _boot_survivors(host) == []  # nothing owed yet

    proc.stdin = _DrainFails()
    result = await host.deliver("issue-1", "carry on", cwd=str(tmp_path), delegated=False)
    await _settle()
    assert not result.accepted
    await host.stop()

    reloaded, _ = _host(tmp_path)
    assert [s.reason for s in _boot_survivors(reloaded)] == ["cut_off"]
    await reloaded.stop()


async def test_a_pre_write_rejection_after_a_stop_stays_cancelled(tmp_path):
    """The control: the payload provably reached nothing, so the stop still
    speaks for this chat and nothing is resumed."""
    host, _spawned = await _delivered_host(tmp_path)
    assert await host.stop_worker("issue-1")
    await host.stop()

    def broken_stdin_factory_host():
        spawned: list[dict] = []

        async def factory(*args, **kwargs):
            process = FakeProcess()

            def explode(_data):
                raise BrokenPipeError("the child is gone")

            process.stdin.write = explode
            spawned.append({"args": args, "kwargs": kwargs, "process": process})
            return process

        return (
            ClaudeWorkerHost(
                Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
                state_dir=tmp_path / "state",
                process_factory=factory,
            ),
            spawned,
        )

    broken, _broken_spawned = broken_stdin_factory_host()
    result = await broken.deliver(
        "issue-1", "carry on", cwd=str(tmp_path), delegated=False
    )
    await _settle()

    assert not result.accepted
    assert _boot_survivors(broken) == []
    await broken.stop()


# --- "Bypass permissions" switch (#2285) ------------------------------------


async def _switch_bypass(host, proc, enabled: bool, key: str = "issue-1"):
    """Flip the bypass switch on a live worker, acking its set_permission_mode
    request. Returns the host's result and the mode the worker was told to use."""
    seen = {line.get("request_id") for line in _control_lines(proc)}

    async def ack() -> dict:
        while True:
            fresh = [
                line
                for line in _control_lines(proc)
                if line.get("request_id") not in seen
                and line["request"]["subtype"] == "set_permission_mode"
            ]
            if fresh:
                break
            await asyncio.sleep(0)
        req = fresh[0]
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )
        return req

    acker = asyncio.create_task(ack())
    result = await host.set_bypass_permissions(key, enabled)
    req = await acker
    return result, req["request"]["mode"]


async def test_a_new_worker_starts_in_bypass_and_can_be_switched_out(tmp_path):
    # The CLI refuses a live switch to bypassPermissions unless the process was
    # started with this flag; the flag alone does not turn the mode on, the
    # start_bypassed setting does (#2319, #3748).
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    args = spawned[0]["args"]
    assert "--allow-dangerously-skip-permissions" in args
    assert "--dangerously-skip-permissions" not in args
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["bypass_permissions"] is True
    assert worker["bypass_permissions_available"] is True
    # The chat's own mode stays recorded for switching off.
    assert host._base_permission_mode(host._records["issue-1"]) == "auto"
    await host.stop()


async def test_a_new_worker_starts_in_bypass(tmp_path):
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    args = spawned[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is True
    await host.stop()


async def test_a_read_only_delegate_does_not_start_in_bypass(tmp_path):
    # Plan mode is what keeps a read-only delegation read-only; the new-chat
    # setting must not lift it.
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver(
        "issue-1", "look", cwd=str(tmp_path), permission_mode="plan", delegated=True
    )

    args = spawned[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is False
    await host.stop()


async def test_the_bypass_switch_moves_a_live_worker_and_back(tmp_path):
    host, spawned = _host(tmp_path, permission_mode="acceptEdits", start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    off, off_mode = await _switch_bypass(host, proc, False)
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is False
    assert json.loads((tmp_path / "state" / "test.json").read_text())["workers"][0][
        "bypass_permissions"
    ] is False
    on, on_mode = await _switch_bypass(host, proc, True)

    assert off.accepted and off.action == "updated"
    assert on.accepted and on.action == "updated"
    # Off restores the chat's own mode, not a guess.
    assert (off_mode, on_mode) == ("acceptEdits", "bypassPermissions")
    assert host._records["issue-1"].permission_mode is None
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is True
    unchanged = await host.set_bypass_permissions("issue-1", True)
    assert unchanged.accepted and unchanged.action == "unchanged"
    await host.stop()


async def test_a_refused_bypass_switch_leaves_the_chat_alone(tmp_path):
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]

    async def refuse():
        while not _control_lines(proc):
            await asyncio.sleep(0)
        req = _control_lines(proc)[0]
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": req["request_id"],
                    "error": "Cannot set permission mode to auto",
                },
            }
        )

    refuser = asyncio.create_task(refuse())
    result = await host.set_bypass_permissions("issue-1", False)
    await refuser

    assert not result.accepted and "Cannot set permission mode" in result.reason
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions"] is True
    await host.stop()


async def _finish_and_exit(proc) -> None:
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()


async def test_the_bypass_choice_survives_a_revive(tmp_path):
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    await _switch_bypass(host, spawned[0]["process"], False)
    await _finish_and_exit(spawned[0]["process"])

    revived = await host.deliver("issue-1", "carry on", delegated=False)

    # Off survives: the revive does not fall back to the new-chat default.
    assert revived.accepted and revived.action == "revived"
    args = spawned[1]["args"]
    assert args[args.index("--permission-mode") + 1] == "auto"
    _, mode = await _switch_bypass(host, spawned[1]["process"], True)
    assert mode == "bypassPermissions"
    await _finish_and_exit(spawned[1]["process"])

    # And so does on.
    again = await host.deliver("issue-1", "more", delegated=False)
    assert again.accepted and again.action == "revived"
    args = spawned[2]["args"]
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    await host.stop()


async def test_a_parked_worker_records_the_bypass_choice_for_its_next_spawn(tmp_path):
    host, spawned = _host(tmp_path, start_bypassed=True)
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    await host.park_worker("issue-1")

    result = await host.set_bypass_permissions("issue-1", False)

    assert result.accepted and result.action == "recorded"
    assert not [
        line for line in _control_lines(proc)
        if line["request"]["subtype"] == "set_permission_mode"
    ]
    # Durable: a runtime that reloads the record keeps the choice.
    reloaded, _ = _host(tmp_path)
    assert reloaded._records["issue-1"].bypass_permissions is False
    revived = await host.deliver("issue-1", "carry on", delegated=False)
    args = spawned[-1]["args"]
    assert revived.accepted
    assert args[args.index("--permission-mode") + 1] == "auto"
    await host.stop()


def test_a_record_saved_before_the_switch_existed_loads_with_it_off(tmp_path):
    # #2319 changes the default for new chats only; a record written before
    # #2285 ran without bypass and keeps doing so.
    state = tmp_path / "state"
    state.mkdir()
    (state / "test.json").write_text(
        json.dumps(
            {
                "workers": [
                    {"key": "old", "cwd": str(tmp_path), "session_id": "s-old"},
                    {
                        "key": "chose-on",
                        "cwd": str(tmp_path),
                        "session_id": "s-on",
                        "bypass_permissions": True,
                    },
                ]
            }
        )
    )

    host, _ = _host(tmp_path)

    assert host._records["old"].bypass_permissions is False
    assert host._records["chose-on"].bypass_permissions is True


async def test_a_chat_already_in_bypass_mode_is_not_offered_the_switch(tmp_path):
    host, _ = _host(tmp_path, permission_mode="bypassPermissions")
    await host.deliver("issue-1", "start", cwd=str(tmp_path), delegated=False)

    result = await host.set_bypass_permissions("issue-1", True)

    assert not result.accepted
    assert result.reason == "this chat already bypasses permissions"
    assert host.snapshot()["workers"]["issue-1"]["bypass_permissions_available"] is False
    unknown = await host.set_bypass_permissions("nope", True)
    assert not unknown.accepted and unknown.reason == "unknown_worker"
    await host.stop()


# --- held workers outlive the runtime (#3248) -----------------------------

# A stand-in for the CLI: init, then for each user message an assistant event, an
# optional permission prompt, and a result once ``go`` exists.
_FAKE_CLAUDE = r"""
import json, os, sys, time
go = sys.argv[1]
def say(event):
    sys.stdout.write(json.dumps(event) + "\n"); sys.stdout.flush()
say({"type": "system", "subtype": "init", "session_id": "sid-held"})
for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") != "user":
        continue
    say({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
    if "ask" in json.dumps(message):
        say({"type": "control_request", "request_id": "q1",
             "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}}})
        answer = json.loads(sys.stdin.readline())
        say({"type": "assistant", "message": {"content": [{"type": "text",
             "text": answer["response"]["response"]["behavior"]}]}})
    while not os.path.exists(go):
        time.sleep(0.01)
    say({"type": "result", "subtype": "success", "result": "done"})
"""


async def _until(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@pytest.fixture
def held_sockets(monkeypatch):
    """A short runtime dir: a Unix socket path must stay under 108 bytes, a tmp_path not."""
    run_dir = tempfile.mkdtemp(prefix="held-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", run_dir)
    hosts: list[ClaudeWorkerHost] = []
    yield hosts
    # A failed test must not leave a holder running.
    for host in hosts:
        for info in host._holders.values():
            kill_holder(info)
    shutil.rmtree(run_dir, ignore_errors=True)


def _held_host(tmp_path: Path, monkeypatch, spawned: list, hosts: list) -> ClaudeWorkerHost:
    """A host whose workers run under a real holder, around the fake CLI above."""
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAKE_CLAUDE)

    async def scope(self, key):
        return "", ["env"]  # a prefix that only runs what follows, as a scope would

    monkeypatch.setattr(ClaudeWorkerHost, "_memory_scope", scope)

    async def factory(*args, **kwargs):
        args = list(args)
        held = args[: args.index("--") + 1] + [sys.executable, str(script), str(tmp_path / "go")]
        spawned.append(held)
        return await asyncio.create_subprocess_exec(*held, **kwargs)

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
        hold_processes=True,
    )
    hosts.append(host)
    return host


async def test_a_held_worker_finishes_its_turn_and_question_across_a_restart(
    tmp_path, monkeypatch, held_sockets
):
    spawned: list = []
    first = _held_host(tmp_path, monkeypatch, spawned, held_sockets)
    result = await first.deliver(
        "issue-1", "please ask", cwd=str(tmp_path), delegated=False, delivery_id="d-1"
    )
    assert result.accepted
    assert first._holders["issue-1"].written() == {"d-1"}, "recorded before the CLI read it"
    await _until(lambda: first._live["issue-1"].pending_interaction)
    await first.stop()

    second = _held_host(tmp_path, monkeypatch, spawned, held_sockets)
    assert await second.reattach() == ["issue-1"]
    live = second._live["issue-1"]
    assert live.turn_active
    assert second.turns_a_restart_would_cut() == 0, "a held turn outlives a restart"
    assert live.pending_interaction["request_id"] == "q1"
    answered = await second.answer("issue-1", "q1", answers={}, decision="allow")
    assert answered.accepted
    (tmp_path / "go").touch()
    await _until(lambda: not second._live["issue-1"].turn_active)
    assert second._records["issue-1"].turn_open_at == 0.0
    assert len(spawned) == 1, "the chat was reconnected, not revived"
    await second.stop()  # held again, for the next runtime
    third = _held_host(tmp_path, monkeypatch, spawned, held_sockets)
    assert await third.reattach() == ["issue-1"]
    assert third._live["issue-1"].pending_interaction is None, "an answered question stays answered"
    # A clean exit keeps its own exit code, read before the holder's files go.
    process = third._live["issue-1"].process
    holder = third._holders["issue-1"]
    process.stdin.close()
    assert await asyncio.wait_for(process.wait(), 10) == 0
    await _until(lambda: "issue-1" not in third._holders)
    assert not Path(holder.exit).exists()


async def test_a_turn_that_ended_while_no_runtime_ran_is_read_at_boot(
    tmp_path, monkeypatch, held_sockets
):
    spawned: list = []
    first = _held_host(tmp_path, monkeypatch, spawned, held_sockets)
    assert (await first.deliver("issue-1", "work", cwd=str(tmp_path), delegated=False)).accepted
    await _until(lambda: first._live["issue-1"].turn_active)
    holder = first._holders["issue-1"]
    await first.stop()
    (tmp_path / "go").touch()  # the turn ends while nothing is reading
    await _until(lambda: b'"result"' in Path(holder.log).read_bytes())
    os.killpg(holder.pid, signal.SIGTERM)
    await _until(lambda: not holder.alive())

    second = _held_host(tmp_path, monkeypatch, spawned, held_sockets)
    assert await second.reattach() == []
    assert "issue-1" not in second._live
    assert second._records["issue-1"].turn_open_at == 0.0, "its result was read"
    assert not Path(holder.log).exists()


def test_a_reconnect_settles_the_writes_a_dead_runtime_left_uncertain(tmp_path):
    """#3248: the holder's ledger says which in-flight deliveries reached it."""
    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
    )
    now = time.time()
    info = HolderInfo(
        socket="s", log=str(tmp_path / "l"), exit=str(tmp_path / "e"), pid=1, started_at=now
    )
    rec = WorkerRecord(key="k", cwd=str(tmp_path), session_id="sid")
    started = (("got-there", now + 1), ("never-sent", now + 1), ("older", now - 9))
    for delivery_id, at in started:
        rec.deliveries[delivery_id] = {
            "fingerprint": "f",
            "delivery_stage": "write_may_have_started",
            "at": at,
        }
    host._records["k"] = rec

    # A ledger that exists but cannot be read proves nothing: every receipt stays fenced.
    Path(info.ledger).mkdir()
    host._settle_writes(rec, info)
    assert all("action" not in receipt for receipt in rec.deliveries.values())
    assert "never-sent" in rec.deliveries
    Path(info.ledger).rmdir()
    Path(info.ledger).write_text("got-there\n")

    host._settle_writes(rec, info)

    assert rec.deliveries["got-there"]["action"] == "queued"
    assert "never-sent" not in rec.deliveries, "provably unsent, so the same id may retry"
    assert "action" not in rec.deliveries["older"], "may have reached an earlier CLI"


def test_a_holder_stops_reading_the_cli_while_its_client_is_far_behind(held_sockets):
    """#3248: a stalled runtime must not grow the holder's memory without bound."""
    import socket as socket_module

    from agentdeck.providers.claude_code import holder as holder_module

    run_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    held = holder_module.Holder(
        str(run_dir / "h.sock"),
        str(run_dir / "h.log"),
        str(run_dir / "h.exit"),
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
    )
    ours, theirs = socket_module.socketpair()
    try:
        held.client = theirs
        held.selector.register(theirs, holder_module.selectors.EVENT_READ, "client")
        held.attached = True
        held.to_client += b"x" * (holder_module.BACKLOG_HIGH + 1)
        held._client_events()
        assert not held.reading_child
        del held.to_client[holder_module.BACKLOG_LOW - 1 :]
        held._client_events()
        assert held.reading_child
        held.to_client += b"x" * (holder_module.BACKLOG_HIGH + 1)
        held._client_events()
        held._drop_client()
        assert held.reading_child, "a detached client leaves the log to catch up"
    finally:
        ours.close()
        held.child.kill()
        held.child.wait()
        held.listener.close()


def test_a_holder_replays_a_long_log_a_little_at_a_time(held_sockets):
    """#3248: catching up from an old offset must not read the whole log into memory."""
    import socket as socket_module

    from agentdeck.providers.claude_code import holder as holder_module

    run_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    log_path = run_dir / "h.log"
    held = holder_module.Holder(
        str(run_dir / "h.sock"),
        str(log_path),
        str(run_dir / "h.exit"),
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
    )
    past = holder_module.BACKLOG_HIGH + holder_module.CHUNK * 3
    log_path.write_bytes(b"x" * past)
    ours, theirs = socket_module.socketpair()
    theirs.setblocking(False)
    try:
        held.client = theirs
        held.selector.register(theirs, holder_module.selectors.EVENT_READ, "client")
        held._op(b'{"op": "attach", "offset": 0}')
        received = 0
        largest = 0
        while held.replay_at is not None or held.to_client:
            largest = max(largest, len(held.to_client))
            held._write_client()
            while True:
                try:
                    received += len(ours.recv(1 << 20, socket_module.MSG_DONTWAIT))
                except BlockingIOError:
                    break
        assert largest <= holder_module.BACKLOG_LOW + holder_module.CHUNK * 2
        hello = len(json.dumps({"holder": 1, "pid": os.getpid(), "child": held.child.pid})) + 1
        assert received == hello + past
    finally:
        ours.close()
        held.child.kill()
        held.child.wait()
        held.listener.close()


def test_an_exiting_holder_does_not_wait_forever_on_a_stalled_client(held_sockets, monkeypatch):
    """#3248: the client that stopped reading gets a bounded goodbye; the log keeps the rest."""
    import socket as socket_module

    from agentdeck.providers.claude_code import holder as holder_module

    monkeypatch.setattr(holder_module, "FINAL_FLUSH_S", 0.2)
    run_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    held = holder_module.Holder(
        str(run_dir / "h.sock"),
        str(run_dir / "h.log"),
        str(run_dir / "h.exit"),
        [sys.executable, "-c", "pass"],
    )
    ours, theirs = socket_module.socketpair()
    try:
        held.client = theirs
        held.selector.register(theirs, holder_module.selectors.EVENT_READ, "client")
        held.attached = True
        held.to_client += b"x" * holder_module.BACKLOG_HIGH  # far more than a socket buffers
        started = time.monotonic()
        assert held._finish() == 0
        assert time.monotonic() - started < 2
        assert (run_dir / "h.exit").read_text() == "0\n"
    finally:
        ours.close()


async def test_the_log_a_holder_left_is_read_one_capped_line_at_a_time(tmp_path, monkeypatch):
    """#3248: reading what was missed costs at most one line of memory, as live reading does."""
    from agentdeck.providers.claude_code import worker as worker_module

    monkeypatch.setattr(worker_module, "STDOUT_LINE_LIMIT", 64)
    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
    )
    rec = WorkerRecord(key="k", cwd=str(tmp_path), session_id="sid", turn_open_at=1.0)
    host._records["k"] = rec
    log = tmp_path / "held.log"
    log.write_bytes(
        b'{"type": "assistant", "padding": "' + b"x" * 500 + b'"}\n'
        + b'{"type": "result", "subtype": "success"}\n'
    )
    info = HolderInfo(socket="s", log=str(log), exit=str(tmp_path / "e"), pid=1)

    host._read_rest("k", info)

    assert rec.turn_open_at == 0.0, "the overlong line was skipped and the result still read"


def test_a_holder_hands_one_delivery_to_the_cli_once(held_sockets):
    """#3248: a delivery id the holder already accepted is never written to the CLI again."""
    import socket as socket_module

    from agentdeck.providers.claude_code import holder as holder_module

    run_dir = Path(os.environ["XDG_RUNTIME_DIR"])
    held = holder_module.Holder(
        str(run_dir / "h.sock"),
        str(run_dir / "h.log"),
        str(run_dir / "h.exit"),
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    line = b'{"op": "stdin", "data": "{\\"type\\": \\"user\\"}", "id": "d-1"}'
    try:
        for _attempt in range(2):  # the second time over a fresh connection, as a retry would be
            ours, theirs = socket_module.socketpair()
            held.client = theirs
            held.selector.register(theirs, holder_module.selectors.EVENT_READ, "client")
            held._op(line)
            held._drop_client()
            ours.close()
        assert bytes(held.to_child).count(b'"user"') == 1
        assert (run_dir / "h.exit.writes").read_text() == "d-1\n"
    finally:
        held.child.kill()
        held.child.wait()
        held.listener.close()


def test_a_holder_binds_its_socket_again_when_the_file_is_removed(held_sockets):
    """#3248: systemd empties a stopping runtime's directory; the holder must stay reachable."""
    import socket as socket_module

    from agentdeck.providers.claude_code import holder as holder_module
    from agentdeck.providers.claude_code.worker import HOLDER_RUN_DIR

    run_dir = Path(os.environ["XDG_RUNTIME_DIR"]) / HOLDER_RUN_DIR / "gone"
    sock = run_dir / "h.sock"
    held = holder_module.Holder(
        str(sock),
        str(Path(os.environ["XDG_RUNTIME_DIR"]) / "h.log"),
        str(Path(os.environ["XDG_RUNTIME_DIR"]) / "h.exit"),
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        shutil.rmtree(run_dir)
        held._ensure_socket()
        client = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        client.connect(str(sock))
        client.close()
    finally:
        held.child.kill()
        held.child.wait()
        held.listener.close()


class _SpareProcess(FakeProcess):
    """A fake CLI whose ``wait`` returns only once it has exited, as a real one does."""

    def __init__(self):
        super().__init__()
        self.exited = asyncio.Event()

    def terminate(self) -> None:
        super().terminate()
        self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        return self.returncode or 0


def _spare_host(tmp_path, **kwargs):
    spawned: list[dict] = []

    async def factory(*args, **fkwargs):
        process = _SpareProcess()
        spawned.append({"args": args, "kwargs": fkwargs, "process": process})
        # Queued ahead of anyone reading, as a real CLI's init waits in its pipe.
        process.stdout.emit(
            {"type": "system", "subtype": "init", "session_id": f"sid-{len(spawned)}"}
        )
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
        **kwargs,
    )
    return host, spawned


async def test_a_spare_cli_takes_a_new_chats_first_message_without_a_spawn(tmp_path, caplog):
    """#3420: booting the CLI was most of a new chat's wait; a spare is already booted."""
    host, spawned = _spare_host(tmp_path, max_workers=1)
    booted = await host.prepare_spare(
        "new_chat", "chat-warm", session_token="tok-1", cwd=str(tmp_path)
    )
    again = await host.prepare_spare(
        "new_chat", "chat-warm", session_token="tok-1", cwd=str(tmp_path)
    )

    assert (booted, again) == ("booted", "kept")
    assert len(spawned) == 1
    # Waiting is not a chat: nothing lists it, and it holds no slot of the account's.
    assert host.snapshot()["workers"] == {}
    assert host.snapshot()["live_count"] == 0
    assert "--append-system-prompt" in spawned[0]["args"]
    assert "tok-1" in spawned[0]["args"][spawned[0]["args"].index("--append-system-prompt") + 1]

    caplog.set_level("INFO", logger="agentdeck.providers.claude_code.worker")
    started = await host.deliver(
        "chat-warm",
        "hello",
        cwd=str(tmp_path),
        fresh=True,
        session_token="tok-1",
        delivery_id="press-1",
        delegated=False,
    )
    resent = await host.deliver(
        "chat-warm",
        "hello",
        cwd=str(tmp_path),
        fresh=True,
        session_token="tok-1",
        delivery_id="press-1",
        delegated=False,
    )

    assert started.accepted and started.action == "spawned"
    assert started.session_id == "sid-1"
    assert resent == started
    assert len(spawned) == 1
    written = spawned[0]["process"].stdin.lines
    assert [line["message"]["content"][0]["text"] for line in written] == ["hello"]
    assert host.snapshot()["workers"]["chat-warm"]["session_token"] == "tok-1"
    assert "start claude_code:test:chat-warm warm init_ms=" in caplog.text
    # The key is a chat now, so it is not booted as a spare again.
    assert (
        await host.prepare_spare(
            "new_chat", "chat-warm", session_token="tok-1", cwd=str(tmp_path)
        )
        == "key_in_use"
    )
    await host.stop()


async def test_a_work_spare_is_booted_with_the_work_role_and_taken_by_a_work_start(tmp_path):
    """#3590: a deck-started Work's agent takes the spare its New work form kept, and only
    one booted with the Work role: the role's guide is in the CLI's own arguments."""
    host, spawned = _spare_host(tmp_path)
    await host.prepare_spare(
        "work", "chat-work", session_token="tok-w", cwd=str(tmp_path), role="work"
    )
    await host.prepare_spare("new_chat", "chat-plain", session_token="tok-p", cwd=str(tmp_path))

    warm = await host.deliver(
        "chat-work", "the request", cwd=str(tmp_path), fresh=True, session_token="tok-w",
        role="work", delegated=False,
    )
    cold = await host.deliver(
        "chat-plain", "the request", cwd=str(tmp_path), fresh=True, session_token="tok-p",
        role="work", delegated=False,
    )

    assert warm.accepted and cold.accepted
    assert len(spawned) == 3  # the two spares, and the plain one's mismatch spawned cold
    assert spawned[0]["process"].stdin.lines[0]["message"]["content"][0]["text"] == "the request"
    assert spawned[1]["process"].returncode is not None
    await host.stop()


async def test_a_start_that_differs_from_its_spare_starts_cold_and_stops_it(tmp_path):
    host, spawned = _spare_host(tmp_path)
    await host.prepare_spare("new_chat", "chat-warm", session_token="tok-1", cwd=str(tmp_path))

    started = await host.deliver(
        "chat-warm",
        "hello",
        cwd=str(tmp_path),
        fresh=True,
        model="opus",
        session_token="tok-1",
        delegated=False,
    )

    assert started.accepted
    assert len(spawned) == 2
    assert spawned[0]["process"].returncode is not None
    assert "--model" in spawned[1]["args"]
    assert spawned[1]["process"].stdin.lines[0]["message"]["content"][0]["text"] == "hello"
    await host.stop()


async def test_a_spare_is_replaced_by_its_shapes_next_one_booted_again_and_stopped(
    tmp_path, monkeypatch
):
    from agentdeck.providers.claude_code import worker as worker_mod

    monkeypatch.setattr(worker_mod, "SPARE_REBOOT_DELAY_S", 0.0)
    host, spawned = _spare_host(tmp_path)
    await host.prepare_spare("new_chat", "chat-a", session_token="tok-a", cwd=str(tmp_path))
    await host.prepare_spare("new_chat", "chat-b", session_token="tok-b", cwd=str(tmp_path))

    # One spare per kind of start: the new one replaces the old.
    assert spawned[0]["process"].returncode is not None
    assert spawned[1]["process"].returncode is None

    spawned[1]["process"].terminate()
    for _ in range(20):
        await asyncio.sleep(0)
        if len(spawned) == 3:
            break
    assert len(spawned) == 3
    assert spawned[2]["kwargs"]["env"]["AGENTDECK_WORKER_KEY"] == "chat-b"

    # A start nothing asks for any more is stopped for good, not booted again.
    assert await host.drop_spare("new_chat") == "dropped"
    assert spawned[2]["process"].returncode is not None
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(spawned) == 3
    assert await host.drop_spare("new_chat") == "none"
    await host.stop()
