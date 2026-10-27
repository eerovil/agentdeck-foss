import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentdeck.chat_identity import ChatLinkConflict
from agentdeck.context_delivery import ContextDelivery
from agentdeck.db import Db, _declared_columns, open_db
from agentdeck.db_migrations import applied_versions
from agentdeck.models import (
    IssueRef,
    PullRequestRef,
    Session,
    SessionStatus,
    UsageSnapshot,
)
from agentdeck.scheduled_messages import RUNTIME_HOLD, ScheduledMessage
from agentdeck.state import AppState


def _snap(pct):
    return UsageSnapshot(
        account_key="claude_code:main",
        five_hour_pct=pct,
        five_hour_resets_at=None,
        seven_day_pct=1.0,
        seven_day_resets_at=None,
        fetched_at=datetime.now(UTC),
    )


def _context_row(action_id="action-1", worker_key="worker-1", delivery_id="delivery-1"):
    return ContextDelivery(
        action_id=action_id,
        attempt=1,
        provider_id="codex",
        account_key="codex:main",
        worker_key=worker_key,
        delivery_id=delivery_id,
        request_sha256="a" * 64,
        chat_id="chat-1",
        source_session_key="codex:main:source",
        source_session_id="source",
        mode="observe",
        proposed_action="compact",
        executed_action="none",
        reason="threshold",
    )


def test_context_delivery_ledger_reserves_reloads_and_transitions(tmp_path):
    db = Db(tmp_path / "context.db")
    try:
        row = db.reserve_context_delivery(_context_row())
        assert row is not None
        assert db.reserve_context_delivery(_context_row()) == row
        assert db.mark_context_pending(row.action_id)
        assert db.mark_message_pending(row.action_id)
        assert db.mark_completed(
            row.action_id,
            final_accepted=True,
            final_action="queued",
            provider_write_state="started",
        )
        stored = db.get_context_delivery(
            "codex", "codex:main", "worker-1", "delivery-1"
        )
        assert stored is not None
        assert stored.status == "completed"
        assert stored.final_accepted is True
        assert stored.provider_write_state == "started"
        assert db.list_chat_context_deliveries("chat-1") == (stored,)
    finally:
        db.close()


def test_context_delivery_identity_includes_worker_key(tmp_path):
    db = Db(tmp_path / "identity.db")
    try:
        first = db.reserve_context_delivery(_context_row())
        second = db.reserve_context_delivery(_context_row("action-2", "worker-2"))
        assert first is not None and second is not None
        assert first.action_id != second.action_id
    finally:
        db.close()


def test_context_delivery_rearm_and_retention_keep_uncertain(tmp_path):
    db = Db(tmp_path / "retention.db")
    try:
        cancelled = db.reserve_context_delivery(_context_row())
        uncertain = db.reserve_context_delivery(
            _context_row("action-2", delivery_id="delivery-2")
        )
        retryable = db.reserve_context_delivery(
            replace(
                _context_row("action-3", delivery_id="delivery-3"),
                executed_action="compact",
            )
        )
        assert cancelled is not None and uncertain is not None and retryable is not None
        assert db.mark_cancelled(cancelled.action_id, "worker_changed")
        assert db.rearm_cancelled_context_delivery(cancelled.action_id)
        rearmed = db.get_context_delivery(
            "codex", "codex:main", "worker-1", "delivery-1"
        )
        assert rearmed is not None and rearmed.attempt == 2
        assert db.mark_uncertain(uncertain.action_id, provider_write_state="unknown")
        assert db.mark_clean_failed(
            retryable.action_id,
            retryable_same_delivery=True,
            provider_write_state="not_started",
        )
        old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
        db._write("UPDATE context_deliveries SET updated_at=?", (old,))
        assert db.prune_context_deliveries(30) == 0
        assert (
            db.get_context_delivery("codex", "codex:main", "worker-1", "delivery-2")
            is not None
        )
        assert (
            db.get_context_delivery("codex", "codex:main", "worker-1", "delivery-3")
            is not None
        )
    finally:
        db.close()


def test_context_delivery_rearm_requires_clean_worker_changed_without_target(tmp_path):
    db = Db(tmp_path / "rearm-guards.db")
    try:
        wrong_reason = db.reserve_context_delivery(_context_row())
        linked = db.reserve_context_delivery(
            _context_row("action-2", delivery_id="delivery-2")
        )
        assert wrong_reason is not None and linked is not None
        assert db.mark_cancelled(wrong_reason.action_id, "other")
        assert not db.rearm_cancelled_context_delivery(wrong_reason.action_id)
        assert db.link_target(linked.action_id, "codex:main:target")
        assert db.mark_cancelled(linked.action_id, "worker_changed")
        assert not db.rearm_cancelled_context_delivery(linked.action_id)
    finally:
        db.close()


def test_a_clean_failure_rearms_under_the_retrys_own_request(tmp_path):
    """#1491: an `at_capacity` refusal must not make its delivery ID undeliverable.

    The poller reuses one delivery ID for a card and re-renders its brief on every attempt,
    so the retry's request hash legitimately differs from the refused attempt's. Frozen
    against the old hash, that read as `delivery_id_conflict` for good.
    """
    db = Db(tmp_path / "clean-rearm.db")
    try:
        row = db.reserve_context_delivery(_context_row())
        assert row is not None
        assert db.mark_clean_failed(
            row.action_id,
            final_accepted=False,
            final_action="rejected",
            final_reason="at_capacity",
            provider_write_state="not_started",
        )

        assert db.rearm_clean_failed_context_delivery(row.action_id)
        retry = replace(_context_row(), request_sha256="b" * 64)
        assert db.refresh_rearmed_context_delivery(row.action_id, retry, request_hash=True)

        stored = db.get_context_delivery("codex", "codex:main", "worker-1", "delivery-1")
        assert stored is not None
        assert stored.status == "reserved"
        assert stored.attempt == 2
        assert stored.request_sha256 == "b" * 64
        assert stored.final_reason is None
        assert stored.final_accepted is None
    finally:
        db.close()


def test_only_a_provably_unwritten_refusal_rearms(tmp_path):
    """At-most-once: a delivery that may have written is `uncertain`, and stays fenced."""
    db = Db(tmp_path / "clean-rearm-guards.db")
    try:
        wrote = db.reserve_context_delivery(_context_row())
        unknown = db.reserve_context_delivery(
            _context_row("action-2", delivery_id="delivery-2")
        )
        linked = db.reserve_context_delivery(
            _context_row("action-3", delivery_id="delivery-3")
        )
        assert wrote is not None and unknown is not None and linked is not None

        assert db.mark_clean_failed(wrote.action_id, provider_write_state="started")
        assert not db.rearm_clean_failed_context_delivery(wrote.action_id)

        assert db.mark_uncertain(unknown.action_id, provider_write_state="unknown")
        assert not db.rearm_clean_failed_context_delivery(unknown.action_id)

        assert db.link_target(linked.action_id, "codex:main:target")
        assert db.mark_clean_failed(linked.action_id, provider_write_state="not_started")
        assert not db.rearm_clean_failed_context_delivery(linked.action_id)
    finally:
        db.close()


def test_a_refusal_the_provider_wants_resumed_is_not_rearmed(tmp_path):
    """#1493's own clean failure keeps its delivery: re-rendering would drop what it began.

    ``retryable_same_delivery`` says the provider refused before writing but wants this
    exact delivery resumed, because context maintenance it started is standing behind it.
    That row belongs to the resume path, not to #1491's re-arm.
    """
    db = Db(tmp_path / "resume-not-rearm.db")
    try:
        row = db.reserve_context_delivery(_context_row())
        assert row is not None
        assert db.mark_clean_failed(
            row.action_id,
            provider_write_state="not_started",
            retryable_same_delivery=True,
        )
        assert not db.rearm_clean_failed_context_delivery(row.action_id)
        stored = db.get_context_delivery("codex", "codex:main", "worker-1", "delivery-1")
        assert stored is not None and stored.status == "clean_failed"
    finally:
        db.close()


def test_a_worker_change_rearm_keeps_its_request_hash(tmp_path):
    """That rearm re-decides the *same* request, so its hash stays immutable."""
    db = Db(tmp_path / "worker-change-hash.db")
    try:
        row = db.reserve_context_delivery(_context_row())
        assert row is not None
        assert db.mark_cancelled(row.action_id, "worker_changed")
        assert db.rearm_cancelled_context_delivery(row.action_id)
        assert db.refresh_rearmed_context_delivery(
            row.action_id, replace(_context_row(), request_sha256="b" * 64)
        )
        stored = db.get_context_delivery("codex", "codex:main", "worker-1", "delivery-1")
        assert stored is not None and stored.request_sha256 == "a" * 64
    finally:
        db.close()


def test_latest_context_cycle_ignores_unexecuted_rows(tmp_path):
    db = Db(tmp_path / "cycles.db")
    try:
        reserved = replace(_context_row(), executed_action="compact")
        row = db.reserve_context_delivery(reserved)
        assert row is not None
        assert db.latest_executed_context_cycle(
            "codex", "codex:main", "worker-1", "source"
        ) is None
        assert db.mark_completed(
            row.action_id,
            maintenance_result="succeeded",
            provider_write_state="started",
        )
        assert db.latest_executed_context_cycle(
            "codex", "codex:main", "worker-1", "source"
        ).action_id == row.action_id
    finally:
        db.close()


def test_context_observations_are_newer_exact_leg_and_monotonic(tmp_path):
    db = Db(tmp_path / "observations.db")
    try:
        row = db.reserve_context_delivery(
            replace(_context_row(), mode="enforce", executed_action="compact")
        )
        assert row is not None
        assert db.mark_completed(
            row.action_id,
            maintenance_result="succeeded",
            provider_write_state="started",
        )
        completed = db.get_context_delivery(
            "codex", "codex:main", "worker-1", "delivery-1"
        )
        assert completed is not None and completed.maintenance_completed_at
        completion = datetime.fromisoformat(completed.maintenance_completed_at)
        db._write(
            "UPDATE context_deliveries SET maintenance_completed_at=NULL WHERE action_id=?",
            (row.action_id,),
        )

        assert not db.record_context_observation(
            "codex", "codex:main", "worker-1", "source", 55_000,
            observed_at=completion, source_version=1, rearm_below_tokens=60_000,
        )
        assert not db.record_context_observation(
            "codex", "codex:main", "worker-1", "different-leg", 55_000,
            observed_at=completion + timedelta(seconds=1),
            source_version=1, rearm_below_tokens=60_000,
        )
        assert not db.record_context_observation(
            "codex", "codex:main", "worker-1", "source", None,
            observed_at=completion + timedelta(seconds=1),
            source_version=1, rearm_below_tokens=60_000,
        )
        assert db.record_context_observation(
            "codex", "codex:main", "worker-1", "source", 55_000,
            observed_at=completion + timedelta(seconds=1),
            source_version=1, rearm_below_tokens=60_000,
        )
        assert not db.record_context_observation(
            "codex", "codex:main", "worker-1", "source", 40_000,
            observed_at=completion + timedelta(seconds=2),
            source_version=1, rearm_below_tokens=60_000,
        )
        assert db.record_context_observation(
            "codex", "codex:main", "worker-1", "source", 90_000,
            observed_at=completion + timedelta(seconds=3),
            source_version=2, rearm_below_tokens=60_000,
        )

        observed = db.get_context_delivery(
            "codex", "codex:main", "worker-1", "delivery-1"
        )
        assert observed is not None
        assert observed.after_tokens == 90_000
        assert observed.maintenance_completed_at is not None
        assert observed.after_observation_version == 2
        assert observed.after_observed_at == (
            completion + timedelta(seconds=3)
        ).isoformat()
        assert observed.rearmed is True
    finally:
        db.close()


def test_pending_context_delivery_fences_worker_until_terminal_state(tmp_path):
    db = Db(tmp_path / "pending-context.db")
    try:
        row = db.reserve_context_delivery(
            replace(_context_row(), executed_action="rollover")
        )
        assert row is not None
        assert db.mark_context_pending(row.action_id)
        assert db.pending_context_delivery("codex", "codex:main", "worker-1") == (
            db.get_context_delivery(
                "codex", "codex:main", "worker-1", "delivery-1"
            )
        )
        assert db.mark_clean_failed(
            row.action_id,
            retryable_same_delivery=True,
            provider_write_state="not_started",
        )
        assert db.pending_context_delivery("codex", "codex:main", "worker-1") is not None
        assert db.mark_context_pending(row.action_id)
        retried = db.get_context_delivery(
            "codex", "codex:main", "worker-1", "delivery-1"
        )
        assert retried is not None and retried.attempt == 2
        assert db.mark_uncertain(row.action_id, provider_write_state="unknown")
        assert db.pending_context_delivery("codex", "codex:main", "worker-1") is not None
    finally:
        db.close()


def test_uncertain_context_resolution_is_exact_idempotent_and_audited(tmp_path):
    db = Db(tmp_path / "resolution.db")
    try:
        row = db.reserve_context_delivery(_context_row())
        assert row is not None
        assert db.mark_uncertain(
            row.action_id,
            provider_write_state="unknown",
            delivery_stage="irreducibly_uncertain",
        )

        resolved = db.resolve_context_delivery(
            row.action_id,
            row.delivery_id,
            "not_written",
            "provider log proves no request",
        )
        assert resolved is not None
        assert resolved.status == "clean_failed"
        assert resolved.retryable_same_delivery is True
        assert resolved.resolution_choice == "not_written"
        assert resolved.resolution_rationale == "provider log proves no request"
        assert resolved.resolved_at is not None
        assert (
            db.resolve_context_delivery(
                row.action_id,
                row.delivery_id,
                "not_written",
                "provider log proves no request",
            )
            == resolved
        )
        assert (
            db.resolve_context_delivery(
                row.action_id, row.delivery_id, "accepted", "different claim"
            )
            is None
        )
        assert (
            db.resolve_context_delivery(
                row.action_id, "wrong-delivery", "not_written", "same proof"
            )
            is None
        )
    finally:
        db.close()


def test_usage_history_roundtrip(tmp_path):
    db = open_db(str(tmp_path / "h.db"), 30)
    try:
        for pct in (10.0, 20.0, 30.0):
            db.record_usage(_snap(pct))
        recent = db.recent_five_hour("claude_code:main", limit=10)
        assert recent == [10.0, 20.0, 30.0]  # chronological
    finally:
        db.close()


def test_latest_usage_returns_most_recent_snapshot(tmp_path):
    db = open_db(str(tmp_path / "h.db"), 30)
    try:
        for pct in (10.0, 20.0, 30.0):
            db.record_usage(_snap(pct))
        latest = db.latest_usage("claude_code:main")
        assert latest is not None
        assert latest.five_hour_pct == 30.0
        # Reset times aren't persisted in history.
        assert latest.five_hour_resets_at is None
    finally:
        db.close()


def test_latest_usage_none_when_empty(tmp_path):
    db = open_db(str(tmp_path / "h.db"), 30)
    try:
        assert db.latest_usage("claude_code:main") is None
    finally:
        db.close()


def test_sessions_seen_upsert(tmp_path):
    db = open_db(str(tmp_path / "h.db"), 30)
    try:
        s = Session(
            key="claude_code:main:sid",
            account_key="claude_code:main",
            session_id="sid",
            status=SessionStatus.IDLE,
            title="T",
        )
        db.upsert_sessions_seen([s])
        db.upsert_sessions_seen([s])  # second time updates, no duplicate row / no error
    finally:
        db.close()


def test_existing_sessions_are_backfilled_into_stable_single_leg_chats(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sessions_seen (session_key TEXT PRIMARY KEY, account_key TEXT,"
        " title TEXT, cwd TEXT, first_seen TEXT, last_seen TEXT)"
    )
    connection.executemany(
        "INSERT INTO sessions_seen VALUES (?, ?, NULL, NULL, ?, ?)",
        [
            ("codex:main:first", "codex:main", "2026-08-01", "2026-08-01"),
            ("claude_code:alt:second", "claude_code:alt", "2026-08-02", "2026-08-02"),
        ],
    )
    connection.commit()
    connection.close()

    db = Db(path)
    first = db.chat_for_session("codex:main:first")
    second = db.chat_for_session("claude_code:alt:second")
    assert first is not None and first.legs == ("codex:main:first",)
    assert second is not None and second.legs == ("claude_code:alt:second",)
    assert first.chat_id != second.chat_id
    db.close()

    reopened = Db(path)
    assert reopened.chat_for_session("codex:main:first") == first
    assert reopened.chat_for_session("claude_code:alt:second") == second
    reopened.close()


def test_chat_successor_binding_is_linear_idempotent_and_restart_safe(tmp_path):
    path = tmp_path / "control.db"
    db = Db(path)
    source = "codex:main:source"
    target = "claude_code:alt:target"
    token = "a" * 32

    # A scan may seed the target before the provider callback binds its token;
    # linking must absorb that singleton rather than reject the real successor.
    target_singleton = db.ensure_session_chats((target,))[0]
    assert db.reserve_chat_successor(token, source) == "new"
    runtime_db = Db(path)
    linked = runtime_db.record_session_token(token, target)
    assert linked is not None and linked.legs == (source, target)
    assert linked.chat_id != target_singleton.chat_id
    assert db.chat_by_id(linked.chat_id) == linked
    assert db.chat_by_id(target_singleton.chat_id) == linked
    assert db.reserve_chat_successor(token, source) == "linked"
    assert runtime_db.record_session_token(token, target) == linked

    # A second successor from the same leg would fork V1 and is refused before
    # provider I/O. The current tail may still continue linearly.
    assert db.reserve_chat_successor("b" * 32, source) == "conflict"
    assert db.reserve_chat_successor("c" * 32, target) == "new"
    with pytest.raises(ChatLinkConflict, match="already has a successor"):
        db.append_chat_leg(source, "codex:main:fork")
    runtime_db.close()
    db.close()

    reopened = Db(path)
    assert reopened.chat_by_id(linked.chat_id) == linked
    assert reopened.reserve_chat_successor("b" * 32, source) == "conflict"
    reopened.close()


def test_chat_identity_reads_do_not_query_the_chat_tables_per_lookup(tmp_path):
    """Chat identity is read once per session per pass by three hot callers.

    Two indexed SELECTs each, thousands of sessions, several times a second was
    about half the web worker's event-loop thread (#1291). The lookups must be
    served from memory, so the only chat-table reads a sweep pays for are the
    ones that load the mirror.
    """
    path = tmp_path / "control.db"
    db = Db(path)
    keys = tuple(f"claude_code:main:s{i:04d}" for i in range(200))
    db.ensure_session_chats(keys)

    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    identities = [db.chat_for_session(key) for key in keys]
    identities += [db.chat_by_id(chat.chat_id) for chat in identities if chat]
    db._conn.set_trace_callback(None)

    assert all(chat is not None for chat in identities)
    assert [chat.legs for chat in identities[:200]] == [(key,) for key in keys]
    chat_table_reads = [s for s in statements if "chat_legs" in s or "chat_aliases" in s]
    assert chat_table_reads == [], chat_table_reads

    db.close()


def test_another_process_linking_a_leg_is_visible_without_a_restart(tmp_path):
    """The runtime writes chat legs on its own connection to the same file.

    A cache only its own writers invalidated would keep serving a continued
    chat's pre-link identity until the web process restarted (#1291).
    """
    path = tmp_path / "control.db"
    db = Db(path)
    source = "codex:main:source"
    target = "claude_code:alt:target"
    before = db.chat_for_session(source)
    assert before is None
    db.ensure_session_chats((source,))
    assert db.chat_for_session(source).legs == (source,)

    runtime_db = Db(path)
    linked = runtime_db.append_chat_leg(source, target)

    assert db.chat_for_session(source) == linked
    assert db.chat_for_session(target) == linked
    assert db.chat_by_id(linked.chat_id) == linked
    runtime_db.close()
    db.close()


def test_another_process_committing_elsewhere_keeps_the_chat_index(tmp_path):
    """The runtime commits to other tables about once a second. Each of those used to
    reload every chat leg and drop every view keyed on the snapshot, which kept the web
    worker busy on an idle deck (#2665); only a chat-table write may reload it."""
    path = tmp_path / "control.db"
    db = Db(path)
    source = "codex:main:source"
    db.ensure_session_chats((source,))
    runtime_db = Db(path)
    # This connection's own chat write is caught up by one reload at the next foreign commit.
    runtime_db.record_session_opened("codex:main:other", datetime(2026, 10, 6, tzinfo=UTC))
    snapshot = db.chat_snapshot()

    runtime_db.record_session_opened("codex:main:other", datetime(2026, 10, 7, tzinfo=UTC))
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    assert db.chat_snapshot() is snapshot
    assert db.chat_snapshot() is snapshot
    db._conn.set_trace_callback(None)
    assert [s for s in statements if "chat_legs" in s or "chat_aliases" in s] == []
    assert sum("chat_index_stamp" in s for s in statements) == 1

    linked = runtime_db.append_chat_leg(source, "claude_code:alt:target")
    fresh = db.chat_snapshot()
    assert fresh is not snapshot
    assert fresh.chat_for_session("claude_code:alt:target") == linked
    runtime_db.close()
    db.close()


def test_a_refused_chat_link_leaves_no_phantom_leg_behind(tmp_path):
    """A conflict rolls the transaction back, so the mirror must go with it."""
    path = tmp_path / "control.db"
    db = Db(path)
    source = "codex:main:source"
    db.ensure_session_chats((source,))
    db.append_chat_leg(source, "claude_code:alt:target")

    with pytest.raises(ChatLinkConflict):
        db.append_chat_leg(source, "codex:main:fork")

    assert db.chat_for_session("codex:main:fork") is None
    assert db.chat_for_session(source).legs == (source, "claude_code:alt:target")
    reopened = Db(path)
    assert reopened.chat_for_session("codex:main:fork") is None
    reopened.close()
    db.close()


def test_concurrent_successor_reservations_have_one_deterministic_winner(tmp_path):
    path = tmp_path / "control.db"
    first = Db(path)
    second = Db(path)
    source = "codex:main:source"
    barrier = threading.Barrier(2)

    def reserve(db, token):
        barrier.wait()
        return db.reserve_chat_successor(token, source)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(reserve, first, "a" * 32),
            pool.submit(reserve, second, "b" * 32),
        )
        outcomes = {future.result() for future in futures}

    assert outcomes == {"new", "conflict"}
    first.close()
    second.close()


def test_session_last_opened_round_trip_and_replace(tmp_path):
    path = tmp_path / "h.db"
    first = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    second = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)

    db = Db(path)
    assert db.load_session_last_opened() == {}
    assert db.record_session_opened("codex:test:sid", first) == first
    assert db.record_session_opened("codex:test:sid", second) == second
    db.close()

    reopened = Db(path)
    try:
        assert reopened.load_session_last_opened() == {"codex:test:sid": second}
        assert AppState(db=reopened).session_last_opened == {"codex:test:sid": second}
    finally:
        reopened.close()


def test_a_database_with_the_removed_spend_tables_still_opens(tmp_path):
    """#2864 removed spend and capacity, but kept their tables: an old database that
    still holds rows in them must open and keep working, and nothing deletes them."""
    import sqlite3

    path = tmp_path / "h.db"
    Db(path).close()
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO session_projects(account_key, session_id, project, updated_at)"
        " VALUES ('claude_code:main', 'sid', 'agentdeck', 1.0)"
    )
    raw.execute(
        "INSERT INTO capacity_history(account_key, window, changed_at, capacity_micros,"
        " previous_micros, reason) VALUES ('claude_code:main', 'seven_day', 1.0, 5, 4, 'r')"
    )
    raw.commit()
    raw.close()

    reopened = Db(path)
    try:
        reopened.record_generated_title("codex:test:sid", "Still works", "sig-1")
        assert "codex:test:sid" in reopened.load_generated_titles()
    finally:
        reopened.close()
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT count(*) FROM session_projects").fetchone() == (1,)
        assert raw.execute("SELECT count(*) FROM capacity_history").fetchone() == (1,)
    finally:
        raw.close()


def test_generated_title_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    record = db.record_generated_title("codex:test:sid", "Fix refund errors", "sig-1")
    assert db.load_generated_titles() == {"codex:test:sid": record}
    db.close()

    reopened = Db(path)
    try:
        loaded = reopened.load_generated_titles()["codex:test:sid"]
        assert loaded.title == "Fix refund errors"
        assert loaded.evidence_signature == "sig-1"
        assert loaded.updated_at == record.updated_at
    finally:
        reopened.close()


def test_generated_titles_migration_adds_source_to_an_existing_db(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE generated_titles (session_key TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " evidence_signature TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO generated_titles VALUES (?, ?, ?, ?)",
        ("codex:test:old", "Fix refund errors", "sig-1", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        # Titles that predate agent authorship are Deckhand's own.
        assert db.load_generated_titles()["codex:test:old"].source == "deckhand"
        db.record_generated_title("codex:test:new", "Name chosen by the chat", "sig-2", "agent")
        assert db.load_generated_titles()["codex:test:new"].source == "agent"
        db.delete_generated_title("codex:test:new")
        assert "codex:test:new" not in db.load_generated_titles()
    finally:
        db.close()


def test_session_pull_requests_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    first = PullRequestRef("eerovil/agentdeck", 230)
    second = PullRequestRef("acmecorp/bazaar", 7, "github_issue:acmecorp/bazaar#3")

    db = Db(path)
    db.record_session_pull_request("codex:test:sid", first)
    db.record_session_pull_request("codex:test:sid", second)
    db.record_session_pull_request("codex:test:sid", first)  # idempotent
    db.close()

    reopened = Db(path)
    # Registration order is preserved; re-registering is not a duplicate.
    assert reopened.load_session_pull_requests() == {"codex:test:sid": (first, second)}
    # The Work it was registered for survives too (#3392).
    assert [ref.work_id for ref in reopened.load_session_pull_requests()["codex:test:sid"]] == [
        "",
        "github_issue:acmecorp/bazaar#3",
    ]
    reopened.delete_session_pull_request("codex:test:sid", first)
    assert reopened.load_session_pull_requests() == {"codex:test:sid": (second,)}
    reopened.delete_session_pull_requests("codex:test:sid")
    reopened.close()

    final = Db(path)
    try:
        assert final.load_session_pull_requests() == {}
    finally:
        final.close()


def test_session_issues_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    first = IssueRef("eerovil/agentdeck", 961)
    second = IssueRef("acmecorp/bazaar", 7)

    db = Db(path)
    db.record_session_issue("codex:test:sid", first)
    db.record_session_issue("codex:test:sid", second)
    db.record_session_issue("codex:test:sid", first)
    db.close()

    reopened = Db(path)
    assert reopened.load_session_issues() == {"codex:test:sid": (first, second)}
    reopened.delete_session_issue("codex:test:sid", first)
    assert reopened.load_session_issues() == {"codex:test:sid": (second,)}
    reopened.delete_session_issues("codex:test:sid")
    reopened.close()

    final = Db(path)
    try:
        assert final.load_session_issues() == {}
    finally:
        final.close()


def test_session_attention_report_round_trip_and_replace(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    first = db.record_session_attention_report("codex:test:sid", "waiting")
    assert db.load_session_attention_reports() == {"codex:test:sid": first}

    replacement = db.record_session_attention_report("codex:test:sid", "finished")
    db.close()

    reopened = Db(path)
    try:
        assert reopened.load_session_attention_reports() == {
            "codex:test:sid": replacement
        }
        reopened.delete_session_attention_report("codex:test:sid")
        assert reopened.load_session_attention_reports() == {}
    finally:
        reopened.close()


def test_registered_pull_requests_reattach_to_a_rescanned_session(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.add_pull_request("codex:test:sid", PullRequestRef("eerovil/agentdeck", 230))
    db.close()

    reopened = Db(path)
    try:
        restored = AppState(db=reopened)
        restored.replace_account_sessions(
            "codex:test",
            [
                Session(
                    key="codex:test:sid",
                    account_key="codex:test",
                    session_id="sid",
                    status=SessionStatus.IDLE,
                )
            ],
        )
        assert restored.sessions["codex:test:sid"].pull_requests == (
            PullRequestRef("eerovil/agentdeck", 230),
        )
    finally:
        reopened.close()


def test_registered_and_inferred_issues_reattach_without_duplicates(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.add_issue("codex:test:sid", IssueRef("eerovil/agentdeck", 961))
    db.close()

    reopened = Db(path)
    try:
        restored = AppState(db=reopened)
        restored.replace_account_sessions(
            "codex:test",
            [
                Session(
                    key="codex:test:sid",
                    account_key="codex:test",
                    session_id="sid",
                    status=SessionStatus.IDLE,
                    issue_url="https://github.com/eerovil/agentdeck/issues/961",
                )
            ],
        )
        assert restored.sessions["codex:test:sid"].issues == (
            IssueRef("eerovil/agentdeck", 961),
        )
    finally:
        reopened.close()


def test_manual_new_chat_cwd_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    assert db.load_manual_new_chat_cwd() is None
    db.record_manual_new_chat_cwd("/srv/first")
    db.record_manual_new_chat_cwd("/srv/last")
    db.close()

    reopened = Db(path)
    try:
        assert reopened.load_manual_new_chat_cwd() == "/srv/last"
    finally:
        reopened.close()


def test_account_new_chat_defaults_round_trip_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    assert db.load_account_new_chat_defaults("codex:test") == {}
    db.record_account_new_chat_defaults("codex:test", "gpt-5.6-luna", "high")
    db.close()

    reopened = Db(path)
    try:
        assert reopened.load_account_new_chat_defaults("codex:test") == {
            "model": "gpt-5.6-luna",
            "effort": "high",
        }
    finally:
        reopened.close()


def test_chat_model_preference_round_trips_and_outlives_the_leg(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    # The preference is keyed by chat because a leg is exactly what it has to
    # outlive: the boundary that spends it also retires the session key (#1665).
    chat = db.ensure_session_chats(["claude_code:main:sid-one"])[0].chat_id
    assert db.chat_model_preference(chat) == ""
    db.record_chat_model_preference(chat, "opus")
    db.close()

    reopened = Db(path)
    try:
        assert reopened.chat_model_preference(chat) == "opus"
        # A second leg of the same chat reads the same preference.
        reopened.append_chat_leg("claude_code:main:sid-one", "claude_code:alt:sid-two")
        successor = reopened.chat_for_session("claude_code:alt:sid-two")
        assert successor is not None and successor.chat_id == chat
        assert reopened.chat_model_preference(successor.chat_id) == "opus"
        reopened.record_chat_model_preference(chat, None)
        assert reopened.chat_model_preference(chat) == ""
    finally:
        reopened.close()


def test_delegated_session_marker_survives_reopen_and_rescan(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.mark_delegated_session("codex:test:child")
    db.close()

    reopened = Db(path)
    try:
        restored = AppState(db=reopened)
        session = Session(
            key="codex:test:child",
            account_key="codex:test",
            session_id="child",
            status=SessionStatus.IDLE,
        )
        restored.replace_account_sessions("codex:test", [session])
        assert restored.sessions[session.key].is_delegated is True
    finally:
        reopened.close()


def test_delegation_parent_survives_reopen_and_nests(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.mark_delegated_session("codex:test:child", parent_session_id="p")
    db.close()

    reopened = Db(path)
    try:
        # The raw parent session_id is persisted, not a resolved key.
        assert reopened.load_delegation_parents() == {"codex:test:child": "p"}
        restored = AppState(db=reopened)
        assert restored.recorded_delegation_parents == {"codex:test:child": "p"}
        for s in (
            Session(
                key="claude_code:main:p",
                account_key="claude_code:main",
                session_id="p",
                status=SessionStatus.LIVE,
            ),
            Session(
                key="codex:test:child",
                account_key="codex:test",
                session_id="child",
                status=SessionStatus.LIVE,
            ),
        ):
            restored.update_session(s)
        children = restored.session_presentation().children_of
        assert [c.key for c in children["claude_code:main:p"]] == ["codex:test:child"]
    finally:
        reopened.close()


def test_delegated_sessions_migration_adds_parent_column_to_existing_db(tmp_path):
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE delegated_sessions (session_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO delegated_sessions VALUES (?, ?)",
        ("codex:test:old", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        # Old row survives the ALTER, with a NULL (thus unlisted) parent.
        assert db.load_delegated_sessions() == {"codex:test:old"}
        assert db.load_delegation_parents() == {}
        # The new column is usable: a parent can now be recorded and read back.
        db.record_delegated_session("codex:test:new", "parent-uuid")
        assert db.load_delegation_parents() == {"codex:test:new": "parent-uuid"}
    finally:
        db.close()


def test_assistant_checkpoint_round_trip(tmp_path):
    db = Db(tmp_path / "history.db")
    try:
        assert db.load_assistant_checkpoint() is None
        checkpoint = {
            "version": 1,
            "view": {"state": "ready", "summary": "One item."},
            "analysis_signature": "material-evidence",
        }
        db.record_assistant_checkpoint(checkpoint)
        assert db.load_assistant_checkpoint() == checkpoint

        replacement = {"version": 1, "view": {"state": "ready", "summary": "Clear."}}
        db.record_assistant_checkpoint(replacement)
        assert db.load_assistant_checkpoint() == replacement
    finally:
        db.close()


def test_opening_an_existing_db_drops_deckhands_acknowledgement_tables(tmp_path):
    """Done, Snooze and Pin are gone (#2599); the checkpoint they sat beside stays."""
    path = tmp_path / "history.db"
    connection = sqlite3.connect(path)
    for table in (
        "assistant_handled",
        "assistant_snooze",
        "assistant_pinned",
        "assistant_triage_stamps",
    ):
        connection.execute(f"CREATE TABLE {table} (session_key TEXT PRIMARY KEY)")
        connection.execute(f"INSERT INTO {table} VALUES ('codex:test:old')")
    connection.execute(
        "CREATE TABLE assistant_checkpoint (singleton INTEGER PRIMARY KEY"
        " CHECK (singleton = 1), payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO assistant_checkpoint VALUES (1, ?, ?)",
        ('{"version": 10, "working": ["codex:test:old"]}', datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        assert db.load_assistant_checkpoint() == {
            "version": 10,
            "working": ["codex:test:old"],
        }
    finally:
        db.close()
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()
    assert "assistant_checkpoint" in tables
    versions = applied_versions(sqlite3.connect(path))
    assert 4 in versions  # the retired step keeps its number (#2599)
    assert 12 in versions
    assert not tables & {
        "assistant_handled",
        "assistant_snooze",
        "assistant_pinned",
        "assistant_triage_stamps",
    }


def test_write_and_query_degrade_on_sqlite_error(tmp_path):
    # The durability seam swallows sqlite errors and degrades — this pins the one
    # consolidated contract instead of an arbitrary per-method except.
    db = Db(tmp_path / "degrade.db")

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("forced")

    db._conn = _Boom()
    db.record_usage(_snap(1.0))  # a write must degrade (not raise)
    assert db.recent_five_hour("claude_code:main") == []  # a query returns its default


def test_usage_history_pruned_on_open(tmp_path):
    from datetime import timedelta

    path = tmp_path / "prune.db"
    db = Db(path, retention_days=1)
    old_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    db._write(
        "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
        " VALUES (?, ?, ?, ?)",
        ("claude_code:main", old_ts, 5.0, None),
    )
    db.record_usage(_snap(9.0))
    db.close()
    reopened = Db(path, retention_days=1)  # prune runs on open
    assert reopened.recent_five_hour("claude_code:main") == [9.0]  # old row pruned
    reopened.close()


def test_retention_zero_keeps_everything(tmp_path):
    from datetime import timedelta

    path = tmp_path / "keep.db"
    db = Db(path, retention_days=0)
    old_ts = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    db._write(
        "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
        " VALUES (?, ?, ?, ?)",
        ("claude_code:main", old_ts, 5.0, None),
    )
    db.close()
    reopened = Db(path, retention_days=0)  # 0 disables pruning
    assert reopened.recent_five_hour("claude_code:main") == [5.0]
    reopened.close()


def test_session_token_binding_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    state = AppState(db=db)
    state.bind_session_token("tok-abc", "codex:test:thread-1")
    # Re-binding the same pair is a no-op; a re-used token follows the new session.
    state.bind_session_token("tok-abc", "codex:test:thread-1")
    db.close()

    reopened = Db(path)
    try:
        restored = AppState(db=reopened)
        assert restored.session_key_for_token("tok-abc") == "codex:test:thread-1"
        assert restored.session_key_for_token("tok-missing") is None
    finally:
        reopened.close()


def test_continued_chat_start_reservation_is_durable_and_releasable(tmp_path):
    path = tmp_path / "h.db"
    db = Db(path)
    assert db.reserve_continued_chat_start("a" * 32, "codex:test:source") == "new"
    assert db.reserve_continued_chat_start("a" * 32, "codex:test:source") == "reserved"
    assert db.mark_continued_chat_dispatched("a" * 32) is True
    # Only the move from ``reserved`` counts: a caller can tell it holds the fence.
    assert db.mark_continued_chat_dispatched("a" * 32) is False
    db.close()

    reopened = Db(path)
    try:
        assert (
            reopened.reserve_continued_chat_start("a" * 32, "codex:test:source")
            == "dispatched"
        )
        reopened.release_continued_chat_start("a" * 32)
        assert (
            reopened.reserve_continued_chat_start("a" * 32, "codex:test:source")
            == "new"
        )
    finally:
        reopened.close()


def test_live_state_finds_a_token_written_by_the_runtime_connection(tmp_path):
    path = tmp_path / "control.db"
    web_db = Db(path)
    runtime_db = Db(path)
    state = AppState(db=web_db, budget_db=web_db)
    try:
        runtime_db.record_session_token("tok-runtime", "claude_code:main:sid-1")

        assert state.session_key_for_token("tok-runtime") == "claude_code:main:sid-1"
        assert state.session_tokens["tok-runtime"] == "claude_code:main:sid-1"
    finally:
        runtime_db.close()
        web_db.close()


# The exact shape the live database carried before #579: created by a build whose
# session_budgets had no boss_granted, plus a column that build added and current
# code knows nothing about.
_PRE_GRANT_BUDGETS = """
CREATE TABLE session_budgets (
    session_key TEXT PRIMARY KEY,
    limit_micros INTEGER NOT NULL,
    spent_micros INTEGER,
    unpriced_tokens INTEGER NOT NULL DEFAULT 0,
    partial INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    default_kind TEXT
)
"""


def test_a_column_added_to_the_schema_reaches_every_existing_table(tmp_path):
    """The general form of #579: no declared column may be unreachable on reopen."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(_PRE_GRANT_BUDGETS)
    # generated_titles.source and delegated_sessions.parent_session_id were both
    # added after their tables shipped, so they exercise the same seam.
    connection.execute(
        "CREATE TABLE generated_titles (session_key TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " evidence_signature TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE delegated_sessions (session_key TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        for table, columns in _declared_columns().items():
            present = {
                row[1] for row in db._conn.execute(f"PRAGMA table_info({table})")
            }
            assert {name for name, *_ in columns} <= present, table
    finally:
        db.close()


def test_a_schema_error_is_logged_loudly_not_swallowed(tmp_path, caplog):
    db = Db(tmp_path / "h.db")
    try:
        with caplog.at_level(logging.DEBUG, logger="agentdeck.db"):
            assert db._query("SELECT nope FROM session_budgets") == []
            db._write("INSERT INTO no_such_table(a) VALUES (1)")
    finally:
        db.close()
    levels = {record.levelno for record in caplog.records}
    assert levels == {logging.ERROR}


def test_saved_quick_phrases_survive_a_reopen_and_reset_restores_the_builtins(tmp_path):
    """The edited ⚡ list is one ordered setting: order is part of the value, and
    forgetting it is what hands the composer back to the built-in vocabulary."""
    from agentdeck.composer_phrases import (
        DEFAULT_COMPOSER_PHRASES,
        ComposerPhrase,
        composer_phrases,
    )

    path = tmp_path / "phrases.db"
    db = open_db(str(path), 30)
    try:
        assert db.load_composer_phrases() == ()
        assert composer_phrases(db) == DEFAULT_COMPOSER_PHRASES
        db.record_composer_phrases(
            [
                ComposerPhrase("Ship", "Merge it and deploy."),
                ComposerPhrase("Hold", "Stop and wait for me."),
            ]
        )
    finally:
        db.close()

    reopened = open_db(str(path), 30)
    try:
        assert [phrase.label for phrase in reopened.load_composer_phrases()] == [
            "Ship",
            "Hold",
        ]
        # A later save replaces the whole list rather than merging into it, so a
        # removed phrase cannot linger behind the one that replaced it.
        reopened.record_composer_phrases([ComposerPhrase("Hold", "Stop and wait.")])
        assert reopened.load_composer_phrases() == (
            ComposerPhrase("Hold", "Stop and wait."),
        )
        reopened.clear_composer_phrases()
        assert composer_phrases(reopened) == DEFAULT_COMPOSER_PHRASES
    finally:
        reopened.close()


def test_scheduled_message_survives_reopen_and_retries_a_dispatching_row(tmp_path):
    path = tmp_path / "scheduled.db"
    created_at = datetime.now(UTC)
    message = ScheduledMessage(
        id="scheduled-action-123",
        chat_id="chat-1",
        text="Check the build",
        effort="high",
        due_at=created_at + timedelta(minutes=5),
        created_at=created_at,
        # Only a send held for a restarting runtime is drawn in its chat (#2847).
        hold_reason=RUNTIME_HOLD,
    )
    db = Db(path)
    try:
        assert db.record_scheduled_message(message) == message
        # A safe client retry keeps the first due time instead of postponing it.
        replay = db.record_scheduled_message(
            replace(message, due_at=created_at + timedelta(minutes=6))
        )
        assert replay == message
        assert db.claim_scheduled_message(message.id) is True
    finally:
        db.close()

    reopened = Db(path)
    try:
        visible = reopened.load_visible_scheduled_messages("chat-1")
        assert len(visible) == 1
        assert visible[0].state == "dispatching"
        # A restart retries a claim whose provider outcome was uncertain.
        assert reopened.load_due_scheduled_messages(
            created_at + timedelta(minutes=10)
        ) == visible
        assert reopened.claim_scheduled_message(message.id) is True
        reopened.settle_scheduled_message(message.id, "delivered")
        assert reopened.load_visible_scheduled_messages("chat-1") == ()
    finally:
        reopened.close()


def test_the_next_scheduled_due_time_is_the_earliest_waiting_row_after_now(tmp_path):
    """#2865: the scheduler sleeps until this instead of looking every second."""
    now = datetime.now(UTC)
    db = Db(tmp_path / "scheduled.db")
    try:
        assert db.next_scheduled_message_due(now) is None
        for name, minutes in (("due", -1), ("soon", 5), ("later", 9)):
            db.record_scheduled_message(
                ScheduledMessage(name, "chat-1", name, now + timedelta(minutes=minutes), now)
            )
        assert db.next_scheduled_message_due(now) == now + timedelta(minutes=5)
        # A claimed row is no longer waiting for its time.
        db.claim_scheduled_message("soon")
        assert db.next_scheduled_message_due(now) == now + timedelta(minutes=9)
    finally:
        db.close()


def test_scheduled_message_handoffs_name_each_chat_being_sent_to(tmp_path):
    """#3084: a due row still to send or one delivered, from its newest moment, per chat; one
    that stays unsent past ``since`` (a target that never resolves) counts no longer."""
    now = datetime.now(UTC)
    db = Db(tmp_path / "scheduled.db")
    try:
        for name, chat, minutes in (
            ("due", "chat-1", -1), ("future", "chat-2", 5), ("sent", "chat-3", -1),
            ("stuck", "chat-4", -10),
        ):
            db.record_scheduled_message(
                ScheduledMessage(name, chat, name, now + timedelta(minutes=minutes), now)
            )
        db.claim_scheduled_message("sent")
        db.settle_scheduled_message("sent", "delivered")
        since = now - timedelta(minutes=2)
        found = db.scheduled_message_handoffs(now, since)
        # A row counts from its due time or its last move, whichever is later.
        assert set(found) == {"chat-1", "chat-3", "chat-4"}
        assert all(at >= now for at in found.values())
        later = datetime.now(UTC) + timedelta(minutes=10)
        assert db.scheduled_message_handoffs(later, later - timedelta(minutes=2)) == {}
    finally:
        db.close()


def test_only_the_owning_chat_can_cancel_a_scheduled_message(tmp_path):
    db = Db(tmp_path / "scheduled-cancel.db")
    message = ScheduledMessage(
        id="scheduled-action-456",
        chat_id="chat-1",
        text="Do this later",
        due_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC),
    )
    try:
        assert db.record_scheduled_message(message) == message
        assert db.cancel_scheduled_message(message.id, "other-chat") is False
        assert db.cancel_scheduled_message(message.id, "chat-1") is True
        assert db.load_visible_scheduled_messages("chat-1") == ()
    finally:
        db.close()


def test_permission_prompt_override_defaults_off_and_roundtrips(tmp_path):
    path = tmp_path / "permission-prompts.db"
    db = Db(path, 30)
    key = "codex:main:thread-1"

    assert not db.permission_prompts_allowed(key)
    assert db.permission_prompts_previous_policy(key) is None
    original_policy = {"reject": {"rules": [{"type": "network"}]}}
    db.set_permission_prompts(key, True, original_policy)
    assert db.permission_prompts_allowed(key)
    assert db.permission_prompts_previous_policy(key) == original_policy
    db.close()

    reopened = Db(path, 30)
    try:
        assert reopened.permission_prompts_allowed(key)
        assert reopened.permission_prompts_previous_policy(key) == original_policy
        reopened.set_permission_prompts(key, False)
        assert not reopened.permission_prompts_allowed(key)
        assert reopened.permission_prompts_previous_policy(key) is None
    finally:
        reopened.close()


def test_legacy_permission_prompt_table_gains_restorable_policy(tmp_path):
    path = tmp_path / "legacy-permission-prompts.db"
    key = "codex:main:thread-1"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE session_permission_prompts (
                session_key TEXT PRIMARY KEY,
                enabled_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO session_permission_prompts(session_key, enabled_at) VALUES (?, ?)",
            (key, "2026-08-21T06:24:47+00:00"),
        )

    db = Db(path, 30)
    try:
        columns = {
            row[1]
            for row in db._conn.execute(
                "PRAGMA table_info(session_permission_prompts)"
            )
        }
        assert "previous_policy_json" in columns
        assert db.permission_prompts_allowed(key)
        # The old table did not record this value. Preserve that fact instead of
        # inventing a policy that may make an autonomous session more permissive.
        assert db.permission_prompts_previous_policy(key) is None
        assert db.set_permission_prompts(key, False) is True
        assert not db.permission_prompts_allowed(key)
    finally:
        db.close()


# --- poller review history, keyed by repository and commit (#1122) ---

def _verdict(sha="a" * 40, verdict="pass", result="release", repo="org/store", at=None):
    from agentdeck.models import PollerReviewVerdict

    return PollerReviewVerdict(
        repo=repo,
        sha=sha,
        verdict=verdict,
        result=result,
        pull_request=f"{repo}#7",
        issue_repo="org/store",
        issue_number=5,
        auto=True,
        generation="gen1",
        reviewed_at=datetime(2026, 7, 25, 12, tzinfo=UTC),
        recorded_at=at or datetime(2026, 7, 25, 12, 1, tzinfo=UTC),
    )


def test_a_review_verdict_round_trips(tmp_path):
    db = Db(tmp_path / "h.db", 30)
    db.record_poller_review_verdict(_verdict())

    found = db.poller_review_verdict("org/store", "a" * 40)
    assert found is not None
    assert (found.verdict, found.result, found.auto) == ("pass", "release", True)
    assert found.pull_request == "org/store#7" and found.issue_number == 5
    assert found.reviewed_at == datetime(2026, 7, 25, 12, tzinfo=UTC)
    db.close()


def test_re_reviewing_a_commit_replaces_its_row_rather_than_adding_one(tmp_path):
    """The poller may hand the same record over twice when its own cleanup failed."""
    db = Db(tmp_path / "h.db", 30)
    db.record_poller_review_verdict(_verdict(verdict="changes-needed", result="handback"))
    db.record_poller_review_verdict(_verdict())

    rows = db.load_poller_review_verdicts()
    assert len(rows) == 1 and rows[0].verdict == "pass"
    db.close()


def test_the_same_commit_in_two_repositories_is_two_reviews(tmp_path):
    db = Db(tmp_path / "h.db", 30)
    db.record_poller_review_verdict(_verdict(repo="org/store"))
    db.record_poller_review_verdict(_verdict(repo="org/other"))

    assert len(db.load_poller_review_verdicts()) == 2
    assert [r.repo for r in db.load_poller_review_verdicts(repo="org/other")] == ["org/other"]
    db.close()


def test_review_history_reads_newest_first(tmp_path):
    db = Db(tmp_path / "h.db", 30)
    db.record_poller_review_verdict(
        _verdict(sha="b" * 40, at=datetime(2026, 7, 25, 12, tzinfo=UTC))
    )
    db.record_poller_review_verdict(
        _verdict(sha="c" * 40, at=datetime(2026, 7, 25, 13, tzinfo=UTC))
    )

    assert [r.sha[0] for r in db.load_poller_review_verdicts()] == ["c", "b"]
    assert [r.sha[0] for r in db.load_poller_review_verdicts(limit=1)] == ["c"]
    db.close()


def test_the_commit_a_review_chat_is_about_round_trips(tmp_path):
    # #2088: the chip compares this with the pull request's head, so it has to survive the
    # web restart that happens between opening a chat and reading its verdict.
    db = Db(tmp_path / "h.db", 30)
    db.record_chat_review_head("org/review-chats", 41, "org/store", 3309, "a" * 40)
    db.record_chat_review_head("org/review-chats", 42, "org/store", 3310, "b" * 40)

    assert db.chat_review_heads("org/review-chats") == {
        41: {("org/store", 3309): "a" * 40},
        42: {("org/store", 3310): "b" * 40},
    }
    # Another deck's chat repository is not this one's.
    assert db.chat_review_heads("org/other-chat") == {}
    db.close()


def test_a_review_chat_written_twice_keeps_one_row(tmp_path):
    """One chat is one commit; a retried write must not become a second answer."""
    db = Db(tmp_path / "h.db", 30)
    db.record_chat_review_head("org/review-chats", 41, "org/store", 3309, "a" * 40)
    db.record_chat_review_head("org/review-chats", 41, "org/store", 3309, "c" * 40)

    assert db.chat_review_heads("org/review-chats") == {41: {("org/store", 3309): "c" * 40}}
    db.close()


def test_a_work_review_chat_keeps_a_commit_per_pull_request(tmp_path):
    """#2637: one chat covers the Work, so each of its pull requests is a row of its own."""
    db = Db(tmp_path / "h.db", 30)
    db.record_chat_review_head("org/review-chats", 41, "org/store", 3309, "a" * 40)
    db.record_chat_review_head("org/review-chats", 41, "Org/Lib", 12, "b" * 40)

    assert db.chat_review_heads("org/review-chats") == {
        41: {("org/store", 3309): "a" * 40, ("org/lib", 12): "b" * 40}
    }
    db.close()


def test_the_old_one_row_per_chat_table_is_migrated_with_its_rows(tmp_path):
    """#2637: migration 13 rebuilds chat_review_heads on the wider key and keeps every row."""
    path = tmp_path / "h.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE chat_review_heads (chat_repository TEXT NOT NULL,"
        " chat_number INTEGER NOT NULL, target_repo TEXT NOT NULL,"
        " target_number INTEGER NOT NULL, head_sha TEXT NOT NULL, recorded_at TEXT NOT NULL,"
        " PRIMARY KEY (chat_repository, chat_number))"
    )
    old.execute(
        "INSERT INTO chat_review_heads VALUES (?, ?, ?, ?, ?, ?)",
        ("org/review-chats", 40, "Org/Store", 3300, "e" * 40, "2026-09-01T00:00:00+00:00"),
    )
    old.commit()
    old.close()

    db = Db(path, 30)
    db.record_chat_review_head("org/review-chats", 40, "org/lib", 7, "f" * 40)

    assert db.chat_review_heads("org/review-chats") == {
        40: {("org/store", 3300): "e" * 40, ("org/lib", 7): "f" * 40}
    }
    db.close()
    connection = sqlite3.connect(path)
    try:
        assert 13 in applied_versions(connection)
        info = connection.execute("PRAGMA table_info(chat_review_heads)").fetchall()
        keyed = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()
    assert keyed == ["chat_repository", "chat_number", "target_repo", "target_number"]
    assert "chat_review_heads_2637" not in tables, "no scratch table is left behind"
    # Opening again is a no-op: the migration is recorded and idempotent besides.
    Db(path, 30).close()


def test_the_widening_runs_once_however_often_it_is_asked(tmp_path):
    from agentdeck.db_migrations import _widen_chat_review_heads

    db = Db(tmp_path / "h.db", 30)
    db.record_chat_review_head("org/review-chats", 41, "org/store", 3309, "a" * 40)
    db.close()
    connection = sqlite3.connect(tmp_path / "h.db")
    _widen_chat_review_heads(connection)
    _widen_chat_review_heads(connection)
    connection.commit()
    rows = connection.execute("SELECT chat_number, target_repo FROM chat_review_heads").fetchall()
    connection.close()

    assert rows == [(41, "org/store")]


def test_latest_context_delivery_keeps_the_run_from_a_completed_noop(tmp_path):
    db = Db(tmp_path / "generations.db")
    try:
        row = db.reserve_context_delivery(
            replace(_context_row(), run_id="generation-1")
        )
        assert row is not None
        assert db.mark_completed(row.action_id, final_accepted=True, final_action="queued")

        latest = db.latest_context_delivery(
            "codex", "codex:main", "worker-1", "source"
        )
        assert latest is not None and latest.run_id == "generation-1"
        assert (
            db.latest_executed_context_cycle(
                "codex", "codex:main", "worker-1", "source"
            )
            is None
        )
    finally:
        db.close()
