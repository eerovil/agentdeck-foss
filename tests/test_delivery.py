"""Unit tests for the DeliveryReceipts at-most-once fence.

The receipt store is exercised dict-in/dict-out here, so the invariant is pinned
without a whole ClaudeWorkerHost + subprocess. The integration pins live in
test_claude_worker.py / test_restart.py.
"""

import hashlib
import json

from agentdeck.providers.claude_code.delivery import (
    DELIVERY_RECEIPT_CAP,
    DELIVERY_RECEIPT_TTL_S,
    DeliverResult,
    DeliveryReceipts,
)


def _fp(**over):
    base = dict(
        message="hi",
        cwd="/tmp/x",
        fresh=False,
        images=[],
        model=None,
        effort=None,
        permission_mode=None,
    )
    base.update(over)
    return DeliveryReceipts.fingerprint(base.pop("message"), **base)


def test_fingerprint_ignores_image_paths_but_not_count():
    # A legitimate retry re-uploads to fresh random paths; only the count is stable.
    assert _fp(images=["/a/1.png"]) == _fp(images=["/b/2.png"])
    assert _fp(images=["/a/1.png"]) != _fp(images=["/a/1.png", "/a/2.png"])
    # Real payload changes do move the fingerprint.
    assert _fp(message="hi") != _fp(message="bye")
    assert _fp(model=None) != _fp(model="opus")
    assert _fp(effort=None) != _fp(effort="high")
    assert _fp(permission_mode=None) != _fp(permission_mode="bypassPermissions")
    assert _fp(allow_over_budget=False) != _fp(allow_over_budget=True)


def test_absent_effort_keeps_legacy_fingerprint():
    legacy = json.dumps(
        {
            "message": "hi",
            "cwd": "/tmp/x",
            "fresh": False,
            "images": 0,
            "model": None,
            "permission_mode": None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert _fp() == hashlib.sha256(legacy.encode()).hexdigest()




def test_lookup_no_id_or_miss_returns_none():
    r = DeliveryReceipts({})
    assert r.lookup(None, "fp", live_session_id=None) is None
    assert r.lookup("d1", "fp", live_session_id=None) is None  # unknown id


def test_lookup_fingerprint_mismatch_is_conflict():
    r = DeliveryReceipts({})
    r.prepare("d1", "fp-A", session_id=None)
    out = r.lookup("d1", "fp-B", live_session_id=None)
    assert out == DeliverResult(False, "rejected", reason="delivery_id_conflict")


def test_lookup_finalized_replays_original_result():
    r = DeliveryReceipts({})
    r.finalize("d1", "fp", DeliverResult(True, "spawned", session_id="sid-1"))
    assert r.lookup("d1", "fp", live_session_id="live") == DeliverResult(
        True, "spawned", session_id="sid-1"
    )


def test_context_metadata_round_trips_without_changing_legacy_receipts():
    store: dict = {}
    receipts = DeliveryReceipts(store)
    result = DeliverResult(
        False,
        "rejected",
        reason="busy",
        session_id="sid-1",
        provider_write_state="not_started",
        successor_created=True,
        retryable_same_delivery=True,
        context_maintenance_status="succeeded",
        context_rollover=True,
    )
    receipts.finalize("d1", "fp", result)
    assert receipts.lookup("d1", "fp", live_session_id=None) == result


def test_lookup_prepared_only_is_uncertain_and_prefers_live_session():
    r = DeliveryReceipts({})
    r.prepare("d1", "fp", session_id=None)  # frozen None (fresh spawn)
    # Live session id (populated after the CLI emits system/init) wins.
    assert r.lookup("d1", "fp", live_session_id="live-sid") == DeliverResult(
        True, "uncertain", session_id="live-sid", provider_write_state="unknown"
    )
    # Falls back to the receipt's frozen id when there is no live one.
    r.prepare("d2", "fp", session_id="frozen")
    assert r.lookup("d2", "fp", live_session_id=None) == DeliverResult(
        True, "uncertain", session_id="frozen", provider_write_state="unknown"
    )


def test_forget_drops_clean_failure_but_refuses_write_started():
    store: dict = {}
    r = DeliveryReceipts(store)
    r.prepare("d1", "fp", session_id=None)
    # A write that already started must never be re-armed: the fence stays.
    assert r.forget("d1", write_started=True) is False
    assert "d1" in store
    # A clean failure drops the receipt so the same id can be retried.
    assert r.forget("d1", write_started=False) is True
    assert "d1" not in store
    # Forgetting an absent / id-less receipt is a no-op.
    assert r.forget("d1", write_started=False) is False
    assert r.forget(None, write_started=False) is False


def test_prune_evicts_by_ttl_on_write():
    store = {
        "old": {
            "fingerprint": "fp",
            "action": "spawned",
            "session_id": None,
            "at": 0.0,
        }
    }
    r = DeliveryReceipts(store)
    r.prepare("fresh", "fp", session_id=None)  # prune runs on write
    assert "old" not in store  # at=0 is well past the 24h TTL
    assert "fresh" in store


def test_settled_history_survives_the_count_cap_inside_the_horizon():
    """#1720 retired the count backstop: volume is not evidence a retry is stale.

    This test used to assert the opposite — that a settled receipt is evicted once
    a newer one pushes the count past `DELIVERY_RECEIPT_CAP`. That is exactly the
    eviction #1720 removed, because a caller may retry the same delivery id for the
    whole retry horizon and a busy deck would otherwise turn a still-valid retry
    into a second provider write. The constant stays as a compatibility export.
    """
    import time

    now = time.time()
    # All settled and all inside the retry horizon, so neither age nor count evicts.
    store = {
        f"d{i}": {
            "fingerprint": "fp",
            "action": "spawned",
            "session_id": None,
            "at": now - (DELIVERY_RECEIPT_CAP - i),
        }
        for i in range(DELIVERY_RECEIPT_CAP)
    }
    r = DeliveryReceipts(store)
    r.finalize("newest", "fp", DeliverResult(True, "spawned"))  # now CAP + 1

    assert len(store) == DELIVERY_RECEIPT_CAP + 1
    assert "d0" in store, "the oldest settled receipt can still be replayed"
    assert "newest" in store


def test_persisted_record_shapes_are_stable():
    # Golden shape: prepared vs finalized receipts, so a future edit that changes
    # the on-disk keys (breaking restart replay) fails here.
    store: dict = {}
    r = DeliveryReceipts(store)
    r.prepare("d1", "fp", session_id="s")
    assert set(store["d1"]) == {"fingerprint", "session_id", "at"}
    assert "action" not in store["d1"]  # the write-started fence marker
    r.finalize("d1", "fp", DeliverResult(True, "revived", session_id="s"))
    assert set(store["d1"]) == {"fingerprint", "action", "session_id", "at"}
    assert store["d1"]["action"] == "revived"


def test_ttl_and_cap_constants_are_the_documented_policy():
    assert DELIVERY_RECEIPT_TTL_S == 24 * 3600.0
    assert DELIVERY_RECEIPT_CAP == 4096
