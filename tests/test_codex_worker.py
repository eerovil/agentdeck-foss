from __future__ import annotations

import json
from unittest.mock import AsyncMock

from worker_harness import codex_account as _account
from worker_harness import codex_client as _client

from agentdeck.models import (
    SNAPSHOT_ACTIVITY_CLOCK,
    SNAPSHOT_CAPABILITIES_KEY,
    InjectResult,
)
from agentdeck.providers.codex.worker import _ACTIVITY_SAVE_S, CodexWorkerHost
from agentdeck.providers.instructions import SESSION_GUIDE_OPEN, strip_session_guide


async def test_keyed_codex_worker_persists_lineage_and_queues_followups(tmp_path):
    client = _client()
    bound = []
    first = CodexWorkerHost(
        _account(tmp_path),
        client,
        state_dir=tmp_path / "state",
        bind_session_token=lambda token, key: bound.append((token, key)),
    )

    spawned = await first.deliver(
        "owner/repo#12",
        "first",
        cwd="/work/issue-12",
        delivery_id="delivery-1",
        delegated=True,
        session_token="token-1",
    )

    assert spawned.accepted and spawned.action == "spawned"
    assert bound == [("token-1", "codex:codex:thread-1")]
    client.create_thread.assert_awaited_once()
    client.start_turn.assert_awaited_once()

    client.owns.return_value = True
    second = CodexWorkerHost(
        _account(tmp_path),
        client,
        state_dir=tmp_path / "state",
        bind_session_token=lambda token, key: bound.append((token, key)),
    )
    assert bound[-1] == ("token-1", "codex:codex:thread-1")
    client.adopt_owned.assert_called_with({"thread-1"})
    queued = await second.deliver(
        "owner/repo#12",
        "follow up",
        cwd="/work/issue-12",
        delivery_id="delivery-2",
        delegated=True,
    )

    assert queued.accepted and queued.action == "queued"
    client.queue_turn.assert_awaited_once_with(
        "thread-1", "follow up", images=[], effort=None
    )


async def test_clean_rollover_creation_failure_restores_source_record(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    first = await host.deliver(
        "issue-1",
        "first",
        cwd="/work",
        model="model-a",
        effort="high",
        delivery_id="one",
        delegated=True,
    )
    client.create_thread.side_effect = RuntimeError("create failed")

    failed = await host.deliver(
        "issue-1",
        "handoff",
        cwd="/work",
        fresh=True,
        delivery_id="two",
        require_idle=True,
        expected_session_id=first.session_id,
        context_rollover=True,
        delegated=True,
    )

    restored = host._records["issue-1"]
    assert not failed.accepted and failed.retryable_same_delivery
    assert failed.successor_created is False
    assert restored.session_id == first.session_id
    assert restored.model == "model-a" and restored.effort == "high"
    assert "one" in restored.deliveries
    assert restored.pending_context_delivery_id == "two"


async def test_clean_first_message_failure_retries_existing_rollover_target(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    first = await host.deliver(
        "issue-1", "first", cwd="/work", delivery_id="one", delegated=True
    )
    host._records["issue-1"].last_result_at = 12.0
    host._save_state()
    client.create_thread.return_value = InjectResult(True, session_id="thread-2")
    client.start_turn.return_value = InjectResult(False, "start refused")

    failed = await host.deliver(
        "issue-1",
        "handoff",
        cwd="/work",
        fresh=True,
        delivery_id="two",
        require_idle=True,
        expected_session_id=first.session_id,
        expected_last_result_at=12.0,
        context_rollover=True,
        delegated=True,
    )
    client.queue_turn.return_value = InjectResult(True)
    retried = await host.deliver(
        "issue-1",
        "handoff",
        cwd="/work",
        fresh=True,
        delivery_id="two",
        require_idle=True,
        expected_session_id=first.session_id,
        expected_last_result_at=12.0,
        context_rollover=True,
        delegated=True,
    )

    assert not failed.accepted and failed.successor_created
    assert failed.retryable_same_delivery
    assert retried.accepted and retried.session_id == "thread-2"
    assert client.create_thread.await_count == 2  # source plus one successor




async def test_the_role_is_persisted_and_sent_to_new_thread(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    result = await host.deliver(
        "owner/repo#1", "inspect", cwd=str(tmp_path), role="work", delegated=True
    )

    assert result.accepted
    assert host._records["owner/repo#1"].role == "work"
    client.create_thread.assert_awaited_once_with(
        tmp_path,
        model=None,
        role="work",
        session_token=host._records["owner/repo#1"].session_token,
    )
    # A thread recorded under the poller's retired kanban role (#3741) loads as primary.
    state = json.loads(host._state_path.read_text())
    state["workers"][0]["role"] = "kanban"
    host._state_path.write_text(json.dumps(state))
    reloaded = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    assert reloaded.worker_role("owner/repo#1") == "primary"


async def test_artifact_rollout_configures_an_existing_codex_thread_before_followup(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver(
        "owner/repo#1", "first", cwd=str(tmp_path), role="delegated", delegated=True
    )

    result = await host.deliver(
        "owner/repo#1",
        "next",
        role="delegated",
        delegated=True,
        command_artifacts={"mode": "observe"},
    )

    assert result.accepted
    client.set_command_artifacts.assert_awaited_once_with(
        "thread-1",
        worker_key="owner/repo#1",
        session_token=host._records["owner/repo#1"].session_token,
        artifact_root=tmp_path / "state" / "command-artifacts",
        artifact_policy={"mode": "observe"},
    )
    client.queue_turn.assert_awaited_once()

    await host.deliver(
        "owner/repo#1",
        "enforce now",
        role="delegated",
        delegated=True,
        command_artifacts={"mode": "enforce", "visible_tool_result_chars": 1600},
    )
    assert client.set_command_artifacts.await_count == 2
    assert client.set_command_artifacts.await_args.kwargs["artifact_policy"] == {
        "mode": "enforce",
        "visible_tool_result_chars": 1600,
    }


async def test_keyed_codex_worker_steers_an_active_turn(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True)
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.steer = AsyncMock(return_value=InjectResult(True))

    result = await host.deliver(
        "owner/repo#12", "urgent correction", cwd="/work", delivery_id="two",
        require_active=True, delegated=True,
    )

    assert result.action == "steered"
    client.steer.assert_awaited_once_with("thread-1", "urgent correction", images=[])
    client.queue_turn.assert_not_awaited()


async def test_active_only_codex_followup_refuses_an_ended_turn(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True)
    client.active_turn.return_value = None

    result = await host.deliver(
        "owner/repo#12",
        "late correction",
        delivery_id="two",
        require_active=True,
        delegated=True,
    )

    assert not result.accepted and result.reason == "worker_inactive"
    client.steer.assert_not_called()
    client.queue_turn.assert_not_awaited()








async def test_replacing_a_key_interrupts_its_active_old_thread_first(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/old", delivery_id="one", delegated=True)
    client.active_turn.side_effect = ["turn-1", None]
    client.create_thread.return_value = InjectResult(True, session_id="thread-2")

    replaced = await host.deliver(
        "owner/repo#12", "replacement", cwd="/new", delivery_id="two", delegated=True
    )

    assert replaced.accepted and replaced.session_id == "thread-2"
    client.interrupt.assert_awaited_once_with("thread-1")
    client.wait_for_thread.assert_awaited_once_with("thread-1")
    assert host.snapshot()["workers"]["owner/repo#12"]["session_id"] == "thread-2"


async def test_replacement_does_not_spawn_when_old_turn_interruption_is_uncertain(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/old", delivery_id="one", delegated=True)
    client.active_turn.return_value = "turn-1"
    client.interrupt.side_effect = RuntimeError("transport vanished")

    result = await host.deliver(
        "owner/repo#12", "replacement", cwd="/new", delivery_id="two", delegated=True
    )

    assert not result.accepted and result.action == "uncertain"
    assert result.session_id == "thread-1"
    assert client.create_thread.await_count == 1


async def test_keyed_codex_worker_queues_if_active_turn_ends_before_steer(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True)
    client.active_turn.return_value = "turn-1"
    client.steer = AsyncMock(
        return_value=InjectResult(False, "there is no agentdeck-owned active turn")
    )

    result = await host.deliver(
        "owner/repo#12", "follow up", cwd="/work", delivery_id="two", delegated=True
    )

    assert result.action == "queued"
    client.queue_turn.assert_awaited_once()


async def test_active_only_codex_followup_refuses_if_turn_ends_during_steer(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True)
    client.active_turn.return_value = "turn-1"
    client.steer = AsyncMock(
        return_value=InjectResult(False, "there is no agentdeck-owned active turn")
    )

    result = await host.deliver(
        "owner/repo#12", "follow up", delivery_id="two",
        require_active=True, delegated=True,
    )

    assert not result.accepted and result.reason == "worker_inactive"
    client.queue_turn.assert_not_awaited()


async def test_keyed_codex_worker_replays_delivery_receipts_without_resending(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    first = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )
    replay = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )
    conflict = await host.deliver(
        "owner/repo#12", "different", cwd="/work", delivery_id="same", delegated=True
    )

    assert first == replay
    assert client.create_thread.await_count == 1
    assert client.start_turn.await_count == 1
    assert not conflict.accepted and conflict.reason == "delivery_id_conflict"


async def test_clean_thread_creation_failure_can_retry_the_same_delivery(tmp_path, caplog):
    client = _client()
    client.create_thread.side_effect = [
        RuntimeError("clean failure: Bearer secret-token"),
        InjectResult(True, session_id="thread-2"),
    ]
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    failed = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )
    retried = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )

    assert not failed.accepted and failed.reason == "codex_create_failed"
    assert "Codex thread creation failed for codex:codex" in caplog.text
    assert "clean failure" in caplog.text
    assert "Bearer [redacted]" in caplog.text
    assert "secret-token" not in caplog.text
    assert retried.accepted and retried.session_id == "thread-2"
    assert client.create_thread.await_count == 2
    client.start_turn.assert_awaited_once()


async def test_clean_thread_creation_rejection_logs_the_account_and_reason(tmp_path, caplog):
    client = _client()
    client.create_thread.return_value = InjectResult(
        False, "invalid app-server config: sk-test-secret"
    )
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    failed = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )

    assert not failed.accepted and failed.reason == "codex_create_failed"
    assert "Codex thread creation rejected for codex:codex" in caplog.text
    assert "invalid app-server config" in caplog.text
    assert "[redacted]" in caplog.text
    assert "sk-test-secret" not in caplog.text


async def test_clean_initial_turn_rejection_can_retry_the_same_delivery(tmp_path):
    client = _client()
    client.start_turn.return_value = InjectResult(False, "thread unavailable")
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    failed = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )
    client.queue_turn.return_value = InjectResult(True)
    retried = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )

    assert not failed.accepted and failed.reason == "codex_start_failed"
    assert failed.session_id == "thread-1"
    assert retried.accepted and retried.action == "queued"
    assert client.create_thread.await_count == 1


async def test_keyed_codex_worker_persists_thread_before_ambiguous_first_turn(tmp_path):
    client = _client()
    client.start_turn.side_effect = RuntimeError("transport vanished")
    first = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    uncertain = await first.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )
    recovered = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    replay = await recovered.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="same", delegated=True
    )

    assert uncertain.action == "uncertain" and uncertain.session_id == "thread-1"
    assert replay.action == "uncertain" and replay.session_id == "thread-1"
    assert client.create_thread.await_count == 1


async def test_keyed_codex_worker_reports_turn_completion_for_poller_watches(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "work", cwd="/work", delivery_id="one", delegated=True)
    client.owns.return_value = True
    client.last_result.return_value = (1234.5, "completed", "")

    worker = host.snapshot()["workers"]["owner/repo#12"]

    assert worker["session_id"] == "thread-1"
    assert worker["turn_active"] is False
    assert worker["last_result_at"] == 1234.5
    assert worker["last_result_subtype"] == "completed"
    assert worker["last_result_refusal"] == "", "nothing refused this turn"


async def test_a_turn_the_account_refused_is_published_as_such(tmp_path):
    """#1519: `failed` is every failure at once. The poller has to tell apart the one that
    means no work was attempted, or it spends the card's restarts on an account that will
    refuse again and tells the card its worker died."""
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "work", cwd="/work", delivery_id="one", delegated=True)
    client.owns.return_value = True
    client.last_result.return_value = (
        1234.5, "failed", "usage_limit_exceeded: You've hit your usage limit."
    )

    worker = host.snapshot()["workers"]["owner/repo#12"]

    assert worker["last_result_refusal"].startswith("usage_limit_exceeded")


async def test_a_client_on_the_older_two_value_result_shape_still_snapshots(tmp_path):
    """`snapshot()` syncs every record, so one unexpected shape must cost one field rather
    than the whole account's worker map."""
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "work", cwd="/work", delivery_id="one", delegated=True)
    client.owns.return_value = True
    client.last_result.return_value = (1234.5, "completed")

    worker = host.snapshot()["workers"]["owner/repo#12"]

    assert worker["last_result_subtype"] == "completed"
    assert worker["last_result_refusal"] == ""


async def test_keyed_codex_worker_publishes_an_activity_clock_that_survives_a_restart(tmp_path):
    """#1400: the poller cannot tell a wedged thread from a working one without this.

    It has to be durable. A runtime restart is exactly when the in-memory evidence of a
    wedge is lost — the app-server goes on owning the thread, its unfinished turn is
    forgotten, and `last_result_at` stays zero because no result event ever arrived.
    """
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver("owner/repo#12", "work", cwd="/work", delivery_id="one", delegated=True)
    client.owns.return_value = True
    client.last_result.return_value = None

    delivered = host.snapshot()["workers"]["owner/repo#12"]["last_activity_at"]
    host.thread_changed("thread-1")
    worked = host.snapshot()["workers"]["owner/repo#12"]["last_activity_at"]
    restarted = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    survived = restarted.snapshot()["workers"]["owner/repo#12"]["last_activity_at"]

    assert delivered > 0, "being handed work is activity, and it is the write that always lands"
    assert worked >= delivered
    # Written at most every `_ACTIVITY_SAVE_S`, so the durable clock may sit that far behind
    # the live one — irrelevant against a window of hours, and hundreds of writes cheaper.
    assert worked - survived < _ACTIVITY_SAVE_S


async def test_keyed_codex_worker_snapshot_declares_that_it_keeps_an_activity_clock(tmp_path):
    """#1407: the reader has to be able to tell a missing field from a runtime without it.

    The runtime outlives every web deploy, so a poller from newer code reads snapshots built
    by older runtimes. Naming the capability is what stops a clockless record there from
    reading as two hours of silence.
    """
    host = CodexWorkerHost(_account(tmp_path), _client(), state_dir=tmp_path / "state")

    assert SNAPSHOT_ACTIVITY_CLOCK in host.snapshot()[SNAPSHOT_CAPABILITIES_KEY]


async def test_keyed_codex_worker_requires_cwd_for_a_new_key(tmp_path):
    host = CodexWorkerHost(_account(tmp_path), _client(), state_dir=tmp_path / "state")

    result = await host.deliver("owner/repo#12", "work", delivery_id="one", delegated=True)

    assert not result.accepted and result.reason == "cwd_required"








async def test_a_usage_cache_does_not_block_codex(tmp_path):
    host = CodexWorkerHost(
        _account(tmp_path),
        _client(),
        state_dir=tmp_path / "state",
        usage_cache_dir=tmp_path / "usage",
    )
    started = await host.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="one", delegated=True
    )
    followed_up = await host.deliver(
        "owner/repo#12", "continue", delivery_id="two", delegated=True
    )

    assert started.accepted and started.action == "spawned"
    assert followed_up.accepted and followed_up.action == "queued"


async def test_codex_thread_started_before_a_guide_edit_is_refreshed_once(tmp_path):
    """Codex takes developer instructions only at `thread/start`.

    `thread/resume` has no parameter for them, so an existing thread would keep
    the guide it was born with forever. One prefixed turn is the only channel
    left — and it has to be exactly one, because the text stays in the thread's
    history and costs input tokens on every later turn.
    """
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver(
        "owner/repo#12",
        "first",
        cwd="/work",
        delivery_id="delivery-1",
        delegated=True,
        role="delegated",
        session_token="token-1",
    )

    # Age the persisted record the way a real one is aged: written by a build
    # that predates the field, so it cannot say which guide the thread holds.
    state = json.loads(host._state_path.read_text())
    for worker in state["workers"]:
        worker.pop("guide_fingerprint", None)
    host._state_path.write_text(json.dumps(state))

    client.owns.return_value = True
    revived = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await revived.deliver(
        "owner/repo#12",
        "follow up",
        cwd="/work",
        delivery_id="delivery-2",
        delegated=True,
        role="delegated",
    )

    refreshed = client.queue_turn.await_args.args[1]
    assert refreshed.startswith(SESSION_GUIDE_OPEN)
    # The operator's own turn survives the prefix intact.
    assert strip_session_guide(refreshed) == "follow up"
    # And it is this thread's own guide, not the generic one: role is part of it.
    assert "Session role: delegated worker" in refreshed

    await revived.deliver(
        "owner/repo#12",
        "third",
        cwd="/work",
        delivery_id="delivery-3",
        delegated=True,
        role="delegated",
    )
    assert client.queue_turn.await_args.args[1] == "third"


async def test_guide_refresh_does_not_change_a_delivery_receipt(tmp_path):
    """The prefix is transport, not content.

    Delivery is at-most-once by fingerprint. If wrapping changed the
    fingerprint, the retry of a delivery that had already landed would read as a
    new one and run the same instruction twice.
    """
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver(
        "owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True
    )
    state = json.loads(host._state_path.read_text())
    for worker in state["workers"]:
        worker.pop("guide_fingerprint", None)
    host._state_path.write_text(json.dumps(state))
    client.owns.return_value = True
    revived = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    wrapped = await revived.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="two", delegated=True
    )
    assert client.queue_turn.await_args.args[1].startswith(SESSION_GUIDE_OPEN)
    client.queue_turn.reset_mock()

    replay = await revived.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="two", delegated=True
    )

    assert replay.accepted and replay.action == wrapped.action
    client.queue_turn.assert_not_awaited()


async def test_refused_turn_leaves_the_thread_still_owed_its_guide(tmp_path):
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver(
        "owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True
    )
    state = json.loads(host._state_path.read_text())
    for worker in state["workers"]:
        worker.pop("guide_fingerprint", None)
    host._state_path.write_text(json.dumps(state))
    client.owns.return_value = True
    revived = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    client.queue_turn.return_value = InjectResult(False, "codex is busy")
    refused = await revived.deliver(
        "owner/repo#12", "work", cwd="/work", delivery_id="two", delegated=True
    )
    assert not refused.accepted

    client.queue_turn.return_value = InjectResult(True)
    await revived.deliver(
        "owner/repo#12", "work again", cwd="/work", delivery_id="three", delegated=True
    )

    # The refused turn carried the guide nowhere, so the next one still owes it.
    assert client.queue_turn.await_args.args[1].startswith(SESSION_GUIDE_OPEN)


async def test_steer_and_compaction_never_carry_a_guide_refresh(tmp_path):
    """Neither is a turn the operator chose to spend.

    A steer interrupts work already running, and compaction is the deck talking
    to itself; prefixing either would put 13k characters where the reader
    expects a nudge.
    """
    client = _client()
    host = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")
    await host.deliver(
        "owner/repo#12", "first", cwd="/work", delivery_id="one", delegated=True
    )
    state = json.loads(host._state_path.read_text())
    for worker in state["workers"]:
        worker.pop("guide_fingerprint", None)
    host._state_path.write_text(json.dumps(state))
    client.owns.return_value = True
    revived = CodexWorkerHost(_account(tmp_path), client, state_dir=tmp_path / "state")

    client.steer = AsyncMock(return_value=InjectResult(True))
    client.active_turn.return_value = "turn-1"
    await revived.deliver(
        "owner/repo#12", "stop that", cwd="/work", delivery_id="two", delegated=True
    )
    assert client.steer.await_args.args[1] == "stop that"

    async def queue_turn(*args, **kwargs):
        kwargs["after_maintenance"]("succeeded", None)
        kwargs["before_start"]()
        return InjectResult(True)

    client.queue_turn.side_effect = queue_turn
    client.active_turn.return_value = None
    compacted = await revived.deliver(
        "owner/repo#12",
        "compact",
        cwd="/work",
        delivery_id="three",
        delegated=True,
        require_idle=True,
        context_maintenance=True,
    )
    assert compacted.accepted
    assert client.queue_turn.await_args.args[1] == "compact"
