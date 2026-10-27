"""Ordered SQLite migrations: the only way an existing database is upgraded.

``Db._init_schema`` runs these before ``_SCHEMA``, which then creates any table
that does not exist yet in its current shape. A column added to a table that has
already shipped therefore needs a numbered step here; nothing infers it from
whatever ``_SCHEMA`` happens to declare today (#2601), and
``tests/test_db_schema_baseline.py`` fails when one is missing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[tuple[str, str, str], ...]
    # A step that is more than added columns; runs after ``statements``. It must be
    # idempotent, because two processes may open the shared file during one deploy.
    run: Callable[[sqlite3.Connection], None] | None = None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _add_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = _columns(connection, table)
    if not columns or column in columns:
        return
    try:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    except sqlite3.OperationalError as exc:
        # Two processes may migrate the shared control DB during one deploy. If
        # the other transaction won the ALTER race, re-reading the table proves
        # this exact change is already complete; every other SQL error propagates.
        if "duplicate column name" not in str(exc).lower() or column not in _columns(
            connection, table
        ):
            raise
        return
    log.info("db migration: added %s.%s", table, column)


_SECRETARY_TABLES = (
    "secretary_attempt_windows",
    "secretary_babysit_overrides",
    "secretary_sessions",
    "secretary_desks",
    "secretary_utterances",
    "secretary_dispatch_fences",
    "secretary_memories",
    "secretary_core_memory",
    "secretary_issue_nudges",
    "desk_github_writes",
    "desk_service_restarts",
)


def _drop_secretary_tables(connection: sqlite3.Connection) -> None:
    """Drop the Secretary's tables (#1851), carrying its one surviving fact across.

    Its boss-engagement windows became ``boss_turn_windows``, so they are copied
    before the drop rather than silently reset. That table may not exist yet on an
    old database, since ``_SCHEMA`` runs after the migrations.
    """
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'secretary_attempt_windows'"
    ).fetchone():
        connection.execute(
            "CREATE TABLE IF NOT EXISTS boss_turn_windows ("
            " session_key TEXT PRIMARY KEY, opened_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO boss_turn_windows(session_key, opened_at)"
            " SELECT session_key, opened_at FROM secretary_attempt_windows"
        )
    for table in _SECRETARY_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")


_DECKHAND_ACKNOWLEDGEMENT_TABLES = (
    "assistant_handled",
    "assistant_snooze",
    "assistant_pinned",
    "assistant_triage_stamps",
)


def _drop_deckhand_acknowledgement_tables(connection: sqlite3.Connection) -> None:
    """Drop the tables behind Deckhand's Done, Snooze and Pin (#2599).

    A chat card now reads its colour from whether the chat moved after it was last
    opened, so nothing records an acknowledgement any more; the triage stamps had
    no reader since #1450. ``assistant_checkpoint`` stays: it carries the working
    set the stop edge is measured against.
    """
    for table in _DECKHAND_ACKNOWLEDGEMENT_TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")


def _drop_message_pins(connection: sqlite3.Connection) -> None:
    """Drop the pinned transcript messages: the pin feature was removed (#3478)."""
    connection.execute("DROP TABLE IF EXISTS message_pins")


def _widen_chat_review_heads(connection: sqlite3.Connection) -> None:
    """Key ``chat_review_heads`` by chat *and* pull request (#2637).

    A review chat now covers a whole Work, so one chat has a commit per pull request it
    lists. SQLite cannot change a primary key in place, so the table is rebuilt and every
    row kept: each old row is one chat about the one pull request it names, which is
    exactly a row of the wider key. Repository names are lower-cased on the way, as the
    write path now stores them.

    Idempotent, and safe against a second process opening the file during a deploy: the
    rebuild is one savepoint whose first statement is a write, so it holds SQLite's writer
    lock before it looks at the table, and a second opener that waited for that lock finds
    the table already keyed this way and leaves it alone. A database without the table is
    left alone too; ``_SCHEMA`` then creates it in its current shape.
    """
    connection.execute("SAVEPOINT widen_chat_review_heads")
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS chat_review_heads_2637 ("
            " chat_repository TEXT NOT NULL, chat_number INTEGER NOT NULL,"
            " target_repo TEXT NOT NULL, target_number INTEGER NOT NULL,"
            " head_sha TEXT NOT NULL, recorded_at TEXT NOT NULL,"
            " PRIMARY KEY (chat_repository, chat_number, target_repo, target_number))"
        )
        keyed = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(chat_review_heads)")
            if row[5]
        }
        if keyed and "target_repo" not in keyed:
            connection.execute(
                "INSERT OR REPLACE INTO chat_review_heads_2637(chat_repository, chat_number,"
                " target_repo, target_number, head_sha, recorded_at)"
                " SELECT chat_repository, chat_number, lower(target_repo), target_number,"
                " head_sha, recorded_at FROM chat_review_heads"
            )
            connection.execute("DROP TABLE chat_review_heads")
            connection.execute("ALTER TABLE chat_review_heads_2637 RENAME TO chat_review_heads")
            log.info("db migration: chat_review_heads keyed by chat and pull request")
        else:
            connection.execute("DROP TABLE chat_review_heads_2637")
    except BaseException:
        connection.execute("ROLLBACK TO widen_chat_review_heads")
        connection.execute("RELEASE widen_chat_review_heads")
        raise
    connection.execute("RELEASE widen_chat_review_heads")


MIGRATIONS = (
    Migration(
        1,
        "legacy session budgets",
        (
            ("session_budgets", "boss_granted", "INTEGER NOT NULL DEFAULT 0"),
            (
                "session_budgets",
                "spend_accounting_version",
                "INTEGER NOT NULL DEFAULT 1",
            ),
            ("session_budgets", "token_limit", "INTEGER"),
            ("session_budgets", "tokens_used", "INTEGER"),
        ),
    ),
    Migration(
        2,
        "generated title provenance",
        (("generated_titles", "source", "TEXT NOT NULL DEFAULT 'deckhand'"),),
    ),
    Migration(
        3,
        "delegation parent identity",
        (("delegated_sessions", "parent_session_id", "TEXT"),),
    ),
    # Version 4 added columns to Deckhand's Done table. Done and its table were
    # deleted in #2599 (migration 12 drops it), so the step stays as a no-op: a
    # fresh ledger still records version 4, the same as one that applied it.
    Migration(4, "restorable Deckhand dismissals (retired)", ()),
    Migration(
        5,
        "permission prompt previous policy",
        (
            (
                "session_permission_prompts",
                "previous_policy_json",
                "TEXT NOT NULL DEFAULT 'null'",
            ),
        ),
    ),
    Migration(
        6,
        "monthly usage meter",
        (("usage_history", "month_pct", "REAL"),),
    ),
    Migration(
        7,
        "context delivery preferred model",
        (("context_deliveries", "preferred_model", "TEXT"),),
    ),
    # Version 8 added columns to the Secretary's desk table. The Secretary and
    # its tables were deleted in #1851, so the step is retired rather than
    # renumbered: a deck that already applied it keeps its recorded version.
    Migration(
        9,
        "context delivery work and run correlation",
        (
            ("context_deliveries", "work_id", "TEXT"),
            ("context_deliveries", "run_id", "TEXT"),
        ),
    ),
    Migration(
        10,
        "columns the inferred upgrader used to add",
        # Until #2601 ``Db`` added any declared column a table lacked. These are the
        # ones it still had to add to the oldest database on the host
        # (a #1254 spike), so they are now stated instead of inferred.
        (
            ("sessions_seen", "kind", "TEXT"),
            ("session_attention_reports", "detail", "TEXT NOT NULL DEFAULT ''"),
            ("session_open_questions", "observed_session_key", "TEXT"),
            ("session_open_questions", "answered_session_key", "TEXT"),
        ),
    ),
    Migration(11, "drop Secretary tables", (), _drop_secretary_tables),
    Migration(
        12,
        "drop Deckhand Done, Snooze and Pin tables",
        (),
        _drop_deckhand_acknowledgement_tables,
    ),
    Migration(
        13,
        "key chat_review_heads by chat and pull request",
        (),
        _widen_chat_review_heads,
    ),
    Migration(
        14,
        "messages held while the runtime restarts",
        (
            ("scheduled_messages", "hold_reason", "TEXT"),
            ("scheduled_messages", "attachments", "TEXT"),
        ),
    ),
    Migration(
        15,
        "a Work's live check after its merge",
        (
            ("work_metadata", "live_check_result", "TEXT"),
            ("work_metadata", "live_check_note", "TEXT"),
            ("work_metadata", "live_check_at", "TEXT"),
            ("work_metadata", "live_check_session_key", "TEXT"),
        ),
    ),
    Migration(
        16,
        "the Work a registered pull request is for",
        (("session_pull_requests", "work_id", "TEXT NOT NULL DEFAULT ''"),),
    ),
    Migration(17, "drop pinned messages", (), _drop_message_pins),
)


def apply_ordered_migrations(connection: sqlite3.Connection) -> None:
    """Apply every unapplied migration in ascending version order.

    A migration records itself only after all of its additive changes succeed.
    The runner executes before ``_SCHEMA``: existing tables are upgraded here,
    while tables that do not exist yet are skipped and are then created directly
    in their current shape by ``Db._init_schema``.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations")
    }
    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        for table, column, declaration in migration.statements:
            _add_column(connection, table, column, declaration)
        if migration.run is not None:
            migration.run(connection)
        # Another opener may have completed the same version while this process
        # was waiting for SQLite's writer lock. The schema changes are idempotent,
        # and one ledger row is enough to record that shared result.
        connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)"
            " VALUES (?, ?, ?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )


def applied_versions(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Return the migration ledger for diagnostics/tests."""
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "schema_migrations" not in tables:
        return ()
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
    )
