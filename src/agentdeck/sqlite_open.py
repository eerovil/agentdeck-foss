"""Open the shared SQLite file safely when several processes open it at once (#3145).

The web process, the runtime and every parallel test worker open the same file, and
SQLite refuses two of the steps every opener takes with an immediate "database is
locked" rather than waiting out the connection's busy timeout:

- switching a brand-new file to WAL turns a read lock into an exclusive one, and
  while another connection is mid-write SQLite answers at once rather than wait
  (each would be waiting on the other), so it is retried here;
- a deferred ``BEGIN`` that reads the schema and then writes cannot be upgraded to a
  writer once another connection has committed, so the schema is created under
  ``BEGIN IMMEDIATE``, which takes the write lock first and does wait for it.
"""

from __future__ import annotations

import sqlite3
import time

_WAL_RETRY_S = 5.0


def use_wal(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _WAL_RETRY_S
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def create_schema(conn: sqlite3.Connection, schema: str) -> None:
    # One transaction rather than a disk sync per statement (#3052, #3066).
    conn.executescript(f"BEGIN IMMEDIATE;\n{schema}\nCOMMIT;")
