"""The `KeyedWorkerHost` contract, run against every provider adapter.

The persistent runtime drives Claude and Codex workers through one seam
(`providers/delivery.py::KeyedWorkerHost`), so the at-most-once replay spine
these tests cover is not a Claude property — it is the seam's property. Each
test runs once per adapter; a receipt behaviour that holds for only one of them
is a bug in that adapter, not a difference of provider.
"""

from __future__ import annotations

import inspect

import pytest
from worker_harness import HARNESSES, settle

from agentdeck.models import InjectResult
from agentdeck.providers.delivery import KeyedWorkerHost

adapters = pytest.mark.parametrize(
    "build_harness", [h for _, h in HARNESSES], ids=[name for name, _ in HARNESSES]
)
context_adapters = pytest.mark.parametrize(
    "build_harness",
    [h for name, h in HARNESSES if name in {"claude", "codex"}],
    ids=[name for name, _ in HARNESSES if name in {"claude", "codex"}],
)

KEY = "owner/repo#12"


# --- the declared seam ------------------------------------------------------


def test_the_protocol_covers_exactly_the_provider_neutral_operations():
    # Claude-only lifecycle controls (interrupt, answer, stop_worker, forget,
    # has_worker, has_live_worker) and the Codex-only thread_changed stay off the
    # seam: a contract only one adapter can keep is worse than no contract.
    # Widening it must be a deliberate edit here: park_worker joined when every
    # provider gained it for the Runtime's Run stop (#2159).
    declared = {name for name in vars(KeyedWorkerHost) if not name.startswith("_")}

    assert declared == {
        "deliver",
        "snapshot",
        "release_worker",
        "park_worker",
        "find_key_by_session",
        "worker_role",
        "active_role_keys",
        "role_last_result_at",
        "delivery_is_finalized",
        "delivery_receipt",
        "resolve_context_delivery",
    }


@adapters
def test_both_adapters_satisfy_the_keyed_worker_protocol(build_harness, tmp_path):
    # Structural, not nominal: neither adapter inherits from the protocol, so a
    # future adapter that half-implements the seam fails here rather than at the
    # first runtime dispatch through it.
    harness = build_harness(tmp_path)

    assert isinstance(harness.host, KeyedWorkerHost)


@adapters
def test_deliver_never_defaults_the_trust_level(build_harness, tmp_path):
    # `delegated` resolves to the role deciding whether a session may spawn
    # sub-agents. The adapters once defaulted it to opposite values (Claude
    # False, Codex True), so a caller that omitted it got a different trust
    # level per provider. It must be keyword-only and required on both.
    harness = build_harness(tmp_path)
    parameter = inspect.signature(type(harness.host).deliver).parameters["delegated"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# --- the at-most-once replay spine -----------------------------------------


@adapters
async def test_delivery_id_rejects_conflicting_payload(build_harness, tmp_path):
    # Reusing one delivery id for a different message is a caller bug, not a
    # retry: accepting it would let the second payload hide behind the first
    # id's fence and never be delivered at all.
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "start", cwd=harness.cwd, delivery_id="attempt-1", delegated=False
    )
    await settle()

    conflict = await harness.host.deliver(
        KEY, "different", cwd=harness.cwd, delivery_id="attempt-1", delegated=False
    )

    assert not conflict.accepted
    assert conflict.reason == "delivery_id_conflict"
    assert harness.writes == ["start"]
    await harness.stop()


@adapters
async def test_idle_delivery_rejects_a_changed_worker_version(build_harness, tmp_path):
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "start", cwd=harness.cwd, delivery_id="d1", delegated=False
    )
    await settle()

    changed = await harness.host.deliver(
        KEY,
        "continue",
        delivery_id="d2",
        require_idle=True,
        expected_last_result_at=99.0,
        delegated=True,
        role="kanban",
    )

    assert not changed.accepted
    assert changed.reason == "worker_changed"
    assert harness.writes == ["start"]
    await harness.stop()


@adapters
async def test_receipt_is_persisted_before_the_child_write(build_harness, tmp_path):
    # The fence must be durable *before* the message reaches the child, so a
    # crash in the write→confirm window replays as already-handled instead of
    # re-delivering. Snapshot the on-disk receipt at the moment of the write.
    harness = build_harness(tmp_path)
    seen_at_write: list[dict | None] = []
    harness.write_hooks.append(
        lambda: seen_at_write.append(harness.persisted_receipts(KEY).get("d1"))
    )

    result = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, delivery_id="d1", delegated=False
    )
    await settle()

    assert result.accepted
    # A receipt was already durable at write time, in the *prepared* state.
    assert seen_at_write and seen_at_write[0] is not None
    assert seen_at_write[0].get("fingerprint")
    assert "action" not in seen_at_write[0]
    # After success it is finalized with the real action, ready to replay.
    assert harness.receipts(KEY)["d1"]["action"] == result.action
    await harness.stop()


@adapters
async def test_delivery_receipts_are_time_bounded_not_fifo_64(build_harness, tmp_path):
    # Receipts are pruned by age with a high count backstop. Under the old
    # 64-entry FIFO, a delivery id retried after enough intervening work found
    # no record and silently re-executed.
    harness = build_harness(tmp_path)
    first = await harness.host.deliver(
        KEY, "m0", cwd=harness.cwd, delivery_id="d0", delegated=False
    )
    assert first.accepted
    await settle()
    for i in range(1, 70):
        followup = await harness.host.deliver(
            KEY, f"m{i}", cwd=harness.cwd, delivery_id=f"d{i}", delegated=False
        )
        assert followup.accepted
    writes = len(harness.writes)
    assert writes == 70  # every one of them really reached the child

    assert "d0" in harness.receipts(KEY)  # still remembered, 69 deliveries later
    replay = await harness.host.deliver(
        KEY, "m0", cwd=harness.cwd, delivery_id="d0", delegated=False
    )

    assert replay == first  # the cached original result, not a fresh delivery
    assert len(harness.writes) == writes
    await harness.stop()


@adapters
async def test_uncertain_replay_reports_live_session_id(build_harness, tmp_path):
    # A fresh spawn's receipt is frozen at prepare time, before the session
    # exists. Left prepared-but-unconfirmed (a crash before finalize), its
    # uncertain replay must point at the now-live session so the caller can
    # still reach the work it may have started — not at the stale None.
    harness = build_harness(tmp_path)
    await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, fresh=True, delivery_id="act-3", delegated=False
    )
    await settle()
    record = harness.record(KEY)
    assert record.session_id
    receipt = harness.receipts(KEY)["act-3"]
    receipt.pop("action", None)  # simulate prepared-but-unconfirmed
    receipt["session_id"] = None  # frozen before the session existed
    writes = len(harness.writes)
    assert writes == 1

    replay = await harness.host.deliver(
        KEY, "go", cwd=harness.cwd, fresh=True, delivery_id="act-3", delegated=False
    )

    assert replay.action == "uncertain"
    assert replay.session_id == record.session_id  # live session, not None
    assert len(harness.writes) == writes  # and nothing was written a second time
    await harness.stop()


@adapters
async def test_finalized_receipt_replays_across_a_runtime_restart(build_harness, tmp_path):
    # The fence lives in the host's persisted state, so a restarted runtime that
    # retries an in-flight delivery id replays the original result rather than
    # starting the work again.
    harness = build_harness(tmp_path)
    first = await harness.host.deliver(
        KEY, "start", cwd=harness.cwd, delivery_id="attempt-1", delegated=False
    )
    assert first.accepted
    await settle()
    await harness.stop()

    harness.reload()
    replay = await harness.host.deliver(
        KEY, "start", cwd=harness.cwd, delivery_id="attempt-1", delegated=False
    )

    assert replay == first
    assert harness.writes == ["start"]
    await harness.stop()


@context_adapters
async def test_rollover_preserves_receipts_and_fences_the_source_lineage(
    build_harness, tmp_path
):
    harness = build_harness(tmp_path)
    first = await harness.host.deliver(
        KEY,
        "start",
        cwd=harness.cwd,
        model="model-a",
        effort="high",
        delivery_id="d1",
        delegated=False,
    )
    await settle()
    source_id = first.session_id
    assert source_id
    live = getattr(harness.host, "_live", {}).get(KEY)
    if live is not None:
        live.turn_active = False
    client = getattr(harness.host, "client", None)
    if harness.provider == "codex":
        client.create_thread.return_value = InjectResult(True, session_id="thread-2")

    rolled = await harness.host.deliver(
        KEY,
        "handoff\n\ncontinue",
        cwd=harness.cwd,
        fresh=True,
        delivery_id="d2",
        require_idle=True,
        expected_session_id=source_id,
        context_rollover=True,
        delegated=False,
    )

    assert rolled.accepted
    assert rolled.successor_created is True
    assert rolled.context_rollover is True
    assert rolled.session_id != source_id
    assert set(harness.receipts(KEY)) >= {"d1", "d2"}
    record = harness.record(KEY)
    assert record.pending_context_delivery_id is None
    assert record.model == "model-a" and record.effort == "high"
    assert record.role == "primary" and record.delegated is False
    await harness.stop()


@context_adapters
async def test_pending_context_operation_blocks_other_delivery_and_release(
    build_harness, tmp_path
):
    harness = build_harness(tmp_path)
    first = await harness.host.deliver(
        KEY, "start", cwd=harness.cwd, delivery_id="d1", delegated=False
    )
    await settle()
    record = harness.record(KEY)
    record.pending_context_delivery_id = "context-d2"
    record.pending_context_operation = "rollover"
    record.pending_context_source_session_id = first.session_id
    harness.host._save_state()
    await harness.stop()
    harness.reload()

    blocked = await harness.host.deliver(
        KEY, "other", delivery_id="d3", delegated=False
    )
    released = await harness.host.release_worker(KEY)

    assert not blocked.accepted and blocked.reason == "context_operation_pending"
    assert not released.accepted and released.reason == "context_operation_pending"
    assert harness.record(KEY).session_id == first.session_id
    await harness.stop()
