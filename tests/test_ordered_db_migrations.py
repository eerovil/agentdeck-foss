import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from agentdeck.db import Db, _declared_columns, open_db
from agentdeck.db_migrations import MIGRATIONS, applied_versions, apply_ordered_migrations


def _legacy_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session_budgets (
            session_key TEXT PRIMARY KEY,
            limit_micros INTEGER NOT NULL,
            spent_micros INTEGER,
            unpriced_tokens INTEGER NOT NULL DEFAULT 0,
            partial INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            default_kind TEXT
        );
        CREATE TABLE generated_titles (
            session_key TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            evidence_signature TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delegated_sessions (
            session_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE assistant_handled (
            session_key TEXT PRIMARY KEY,
            evidence_signature TEXT NOT NULL,
            handled_at TEXT NOT NULL
        );
        CREATE TABLE session_permission_prompts (
            session_key TEXT PRIMARY KEY,
            enabled_at TEXT NOT NULL
        );
        CREATE TABLE usage_history (
            account_key TEXT NOT NULL,
            ts TEXT NOT NULL,
            five_hour_pct REAL,
            seven_day_pct REAL
        );
        """
    )
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO generated_titles VALUES (?, ?, ?, ?)",
        ("codex:test:old", "Old title", "sig", now),
    )
    connection.execute(
        "INSERT INTO delegated_sessions VALUES (?, ?)",
        ("codex:test:child", now),
    )
    connection.execute(
        "INSERT INTO assistant_handled VALUES (?, ?, ?)",
        ("codex:test:old", "evidence", now),
    )
    connection.execute(
        "INSERT INTO session_permission_prompts VALUES (?, ?)",
        ("codex:test:old", now),
    )
    connection.commit()
    connection.close()


def test_ordered_migrations_are_idempotent(tmp_path):
    path = tmp_path / "idempotent.db"
    _legacy_database(path)
    first = Db(path)
    versions = applied_versions(first._conn)
    first.close()

    second = Db(path)
    try:
        assert applied_versions(second._conn) == versions
        count = second._conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert count == len(MIGRATIONS)
    finally:
        second.close()


def test_ordered_migrations_tolerate_two_processes_opening_together(tmp_path):
    path = tmp_path / "concurrent.db"
    _legacy_database(path)
    barrier = Barrier(2)
    expected = tuple(m.version for m in MIGRATIONS)

    def migrate() -> tuple[int, ...]:
        connection = sqlite3.connect(path, timeout=5)
        try:
            barrier.wait()
            with connection:
                apply_ordered_migrations(connection)
            return applied_versions(connection)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate) for _ in range(2)]
        results = tuple(future.result() for future in futures)

    assert results == (expected, expected)


def test_the_oldest_database_on_the_host_gains_the_columns_once_inferred(tmp_path):
    """Migration 10 states the columns the removed inferred upgrader still added (#2601).

    The tables are in the shape of the #1254 spike database, the oldest on the host.
    """
    path = tmp_path / "spike.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions_seen (
            session_key TEXT PRIMARY KEY, account_key TEXT, title TEXT, cwd TEXT,
            first_seen TEXT, last_seen TEXT
        );
        CREATE TABLE session_attention_reports (
            session_key TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE session_open_questions (
            session_key TEXT PRIMARY KEY, question TEXT NOT NULL, asked_at TEXT NOT NULL,
            source TEXT NOT NULL, asked_seq INTEGER NOT NULL, observed_seq INTEGER NOT NULL,
            answered_seq INTEGER
        );
        INSERT INTO session_attention_reports
            VALUES ('codex:test:a', 'waiting', '2026-09-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        declared = _declared_columns()
        for table in ("sessions_seen", "session_attention_reports", "session_open_questions"):
            actual = tuple(
                (str(name), str(decl), bool(notnull), default)
                for _cid, name, decl, notnull, default, _pk in db._conn.execute(
                    f"PRAGMA table_info({table})"
                )
            )
            assert {c[0]: c[1:] for c in actual} == {c[0]: c[1:] for c in declared[table]}
        assert db.load_session_attention_reports()["codex:test:a"].detail == ""
        assert 10 in applied_versions(db._conn)
    finally:
        db.close()


def test_open_db_applies_every_migration(tmp_path):
    db = open_db(str(tmp_path / "control.db"), 30)
    try:
        assert applied_versions(db._conn) == tuple(m.version for m in MIGRATIONS)
    finally:
        db.close()


def test_open_db_refuses_rather_than_running_without_a_store(tmp_path):
    corrupt = tmp_path / "agentdeck.db"
    corrupt.write_bytes(b"not a database" * 100)
    with pytest.raises(RuntimeError, match="control-plane storage unavailable"):
        open_db(str(corrupt), 30)


def test_an_old_database_keeps_its_boss_windows_and_loses_the_secretary_tables(tmp_path):
    """Migration 11 replaces the Secretary cleanup that ran on every open (#1851, #2601).

    The windows are copied into ``boss_turn_windows`` before the drop, including
    when that table does not exist yet, and a window already there is kept.
    Migration 17 drops the removed message pins the same way (#3478).
    """
    path = tmp_path / "secretary.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE secretary_attempt_windows (
            session_key TEXT PRIMARY KEY, opened_at TEXT NOT NULL
        );
        INSERT INTO secretary_attempt_windows
            VALUES ('codex:test:old', '2026-08-01T00:00:00+00:00');
        CREATE TABLE secretary_desks (id TEXT PRIMARY KEY);
        CREATE TABLE secretary_issue_nudges (id TEXT PRIMARY KEY);
        CREATE TABLE message_pins (session_key TEXT, event_seq INTEGER);
        INSERT INTO message_pins VALUES ('codex:test:old', 1);
        """
    )
    connection.commit()
    connection.close()

    db = Db(path)
    try:
        tables = {
            str(row[0])
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert not {t for t in tables if t.startswith(("secretary_", "desk_"))}
        assert "message_pins" not in tables
        assert db._conn.execute(
            "SELECT session_key, opened_at FROM boss_turn_windows"
        ).fetchall() == [("codex:test:old", "2026-08-01T00:00:00+00:00")]
        assert 11 in applied_versions(db._conn)
    finally:
        db.close()

    reopened = Db(path)  # the ledger makes the second open a no-op
    try:
        assert reopened._conn.execute("SELECT COUNT(*) FROM boss_turn_windows").fetchone() == (1,)
    finally:
        reopened.close()
