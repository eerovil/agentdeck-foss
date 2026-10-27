"""Fold the Runtime's stray control database back into the real one (#2199).

Until #2199 the Runtime opened the Work workspace claims (#2149) and the Run execution
outcomes (#2159) at ``config.history.db_path`` exactly as written. The default is
``~/.local/share/agentdeck/agentdeck.db`` and nothing expanded the ``~``, so both tables
landed in ``<cwd>/~/.local/share/agentdeck/agentdeck.db`` — inside the live checkout, since
that is the Runtime's working directory — while everything else lived in the real file.

The stores expand the path now. This moves what the stray file already holds into the real
one, once, before the stores open. Both tables survive a restart on purpose, so the stray
file is never simply deleted: its rows are copied, the copy is checked row by row, and only
then is the file moved next to the real database as a backup. A check that fails leaves the
stray file exactly where it was, so the next start tries again.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Literal

from . import run_execution
from . import work_workspace_claims as work_claims

log = logging.getLogger(__name__)

_CLAIM_COLUMNS = (
    "work_id, repository, purpose, path, branch, base_ref, claimed_at, confirmed_at,"
    " release_requested_at, released_at, last_error, reserved_at"
)
# One claim, as written: a released row has no unique index, so this is what makes
# a second merge of the same rows a no-op instead of a duplicate.
_CLAIM_IDENTITY = (
    "work_id IS s.work_id AND repository IS s.repository AND purpose IS s.purpose"
    " AND path IS s.path AND claimed_at IS s.claimed_at"
)
_OUTCOME_COLUMNS = (
    "work_id, run_id, execution_state, reason_code, last_activity_at, terminal_result_at,"
    " stop_requested_at, stop_reason, stopped_at, observed_at"
)
_SIDECARS = ("-wal", "-shm")


def stray_path(raw: str | Path, cwd: Path | None = None) -> Path | None:
    """Where the unexpanded ``raw`` path put the database, or None if it never could."""
    text = str(raw)
    if not text.startswith("~"):
        return None
    stray = (cwd or Path.cwd()) / text
    return stray if stray != Path(text).expanduser() else None


MergeResult = Literal["absent", "merged", "failed"]


def merge_stray_db(raw: str | Path, cwd: Path | None = None) -> MergeResult:
    """Move the stray database's claims and outcomes into the real one.

    ``absent`` when there is no stray file, ``merged`` when it was merged and moved aside,
    ``failed`` when it still holds state the real file lacks. Never raises: a failure is
    logged and the stray file is left alone for the next start, and the caller must not
    open the claim or outcome store while it stands, or it would run without that state.
    """
    stray = stray_path(raw, cwd)
    if stray is None or not stray.is_file():
        return "absent"
    real = Path(raw).expanduser()
    try:
        _merge(stray, real)
        missing = _missing(stray, real)
        if missing:
            log.error(
                "stray control database %s not moved: %s rows did not reach %s",
                stray,
                missing,
                real,
            )
            return "failed"
        backup = _move_aside(stray, real, cwd or Path.cwd())
    except Exception:  # noqa: BLE001 -- never block the Runtime; the stray file stays put
        log.exception("could not merge stray control database %s into %s", stray, real)
        return "failed"
    log.warning("merged stray control database %s into %s; kept it as %s", stray, real, backup)
    return "merged"


def _attach(conn: sqlite3.Connection, stray: Path) -> set[str]:
    """Attach the stray file as ``stray`` and name the tables it holds."""
    conn.execute("ATTACH DATABASE ? AS stray", (str(stray),))
    rows = conn.execute("SELECT name FROM stray.sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


def _merge(stray: Path, real: Path) -> None:
    real.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(real), timeout=30, isolation_level=None)
    try:
        conn.executescript(work_claims.SCHEMA)
        conn.executescript(run_execution.SCHEMA)
        tables = _attach(conn, stray)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "work_workspace_claims" in tables:
                # An active claim the real file already holds for the same key or path
                # wins; the unique indexes turn the stray duplicate into a skipped row.
                conn.execute(
                    f"INSERT OR IGNORE INTO work_workspace_claims ({_CLAIM_COLUMNS})"
                    f" SELECT {_CLAIM_COLUMNS} FROM stray.work_workspace_claims AS s"
                    " WHERE NOT EXISTS"
                    f" (SELECT 1 FROM work_workspace_claims WHERE {_CLAIM_IDENTITY})"
                    " ORDER BY s.rowid"
                )
            if "run_execution_outcomes" in tables:
                # One row per Run; the newer observation of it is the one kept.
                conn.execute(
                    f"INSERT INTO run_execution_outcomes ({_OUTCOME_COLUMNS})"
                    f" SELECT {_OUTCOME_COLUMNS} FROM stray.run_execution_outcomes WHERE true"
                    " ON CONFLICT(work_id, run_id) DO UPDATE SET"
                    " execution_state = excluded.execution_state,"
                    " reason_code = excluded.reason_code,"
                    " last_activity_at = excluded.last_activity_at,"
                    " terminal_result_at = excluded.terminal_result_at,"
                    " stop_requested_at = excluded.stop_requested_at,"
                    " stop_reason = excluded.stop_reason,"
                    " stopped_at = excluded.stopped_at,"
                    " observed_at = excluded.observed_at"
                    " WHERE excluded.observed_at > run_execution_outcomes.observed_at"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _missing(stray: Path, real: Path) -> int:
    """How many stray rows have no counterpart in the real file after the merge."""
    conn = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=30)
    try:
        tables = _attach(conn, stray)
        missing = 0
        if "work_workspace_claims" in tables:
            # A stray active claim is kept when the real file has it, or has another
            # active claim holding its key or its path; a released one must be copied.
            missing += conn.execute(
                "SELECT count(*) FROM stray.work_workspace_claims AS s WHERE NOT EXISTS"
                f" (SELECT 1 FROM main.work_workspace_claims WHERE {_CLAIM_IDENTITY})"
                " AND (s.released_at IS NOT NULL OR NOT EXISTS"
                " (SELECT 1 FROM main.work_workspace_claims AS r WHERE r.released_at IS NULL"
                " AND ((r.work_id = s.work_id AND r.repository = s.repository"
                " AND r.purpose = s.purpose) OR r.path = s.path)))"
            ).fetchone()[0]
        if "run_execution_outcomes" in tables:
            missing += conn.execute(
                "SELECT count(*) FROM stray.run_execution_outcomes AS s WHERE NOT EXISTS"
                " (SELECT 1 FROM main.run_execution_outcomes AS r"
                " WHERE r.work_id = s.work_id AND r.run_id = s.run_id"
                " AND r.observed_at >= s.observed_at)"
            ).fetchone()[0]
        return int(missing)
    finally:
        conn.close()


def _move_aside(stray: Path, real: Path, cwd: Path) -> Path:
    """Checkpoint the stray file into one piece and move it next to the real database."""
    conn = sqlite3.connect(str(stray), timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    backup = real.with_name(f"{real.name}.stray-2199-{time.time_ns()}")
    shutil.move(stray, backup)
    for suffix in _SIDECARS:
        sidecar = stray.with_name(stray.name + suffix)
        if sidecar.exists():
            shutil.move(sidecar, backup.with_name(backup.name + suffix))
    # Drop the now-empty ``~/.local/share/agentdeck`` tree it sat in, never past cwd.
    parent = stray.parent
    while parent != cwd and cwd in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return backup
