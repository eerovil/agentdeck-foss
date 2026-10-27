"""Every opener of the shared SQLite file survives another opener at the same moment (#3145)."""

from __future__ import annotations

import sqlite3
import threading

from agentdeck.sqlite_open import create_schema, use_wal


def test_switching_a_new_file_to_wal_waits_out_a_writer(tmp_path):
    path = tmp_path / "a.db"
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    writer.execute("CREATE TABLE t (x)")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO t VALUES (1)")
    threading.Timer(0.1, writer.execute, ("COMMIT",)).start()

    conn = sqlite3.connect(path)
    use_wal(conn)

    assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_creating_the_schema_survives_a_commit_between_its_read_and_its_write(tmp_path):
    path = tmp_path / "a.db"
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("CREATE TABLE t (x)")
    conn = sqlite3.connect(path, timeout=0)

    def commit_from_the_other_connection() -> int:
        try:
            other.execute("INSERT INTO t VALUES (1)")
        except sqlite3.OperationalError:
            pass  # it waits for the write lock this connection already holds
        return 0

    conn.set_progress_handler(commit_from_the_other_connection, 1)
    create_schema(conn, "CREATE TABLE IF NOT EXISTS t (x); CREATE TABLE u (y);")
    conn.set_progress_handler(None, 1)

    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'u'").fetchone()
