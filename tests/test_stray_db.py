"""The control stores expand ``~`` and fold the old stray file back in (#2199)."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agentdeck import run_execution, run_trace, stray_db
from agentdeck import work_workspace_claims as claims

RAW = "~/.local/share/agentdeck/agentdeck.db"
_KILLED_WRITER = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("INSERT INTO work_workspace_claims (work_id, repository, purpose, path,"
             " claimed_at) VALUES ('w1', 'o/r', 'primary', '/wt/1', 't1')")
conn.execute("INSERT INTO work_workspace_claims (work_id, repository, purpose, path,"
             " claimed_at, released_at) VALUES ('w2', 'o/r', 'primary', '/wt/2', 't1', 't2')")
conn.execute("INSERT INTO run_execution_outcomes (work_id, run_id, execution_state,"
             " observed_at) VALUES ('w1', 'r1', 'running', 5.0)")
os._exit(0)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def cwd(tmp_path):
    cwd = tmp_path / "checkout"
    cwd.mkdir()
    return cwd


def _claim(conn, work_id, path, *, released_at=None, purpose="primary", claimed_at="t1"):
    conn.execute(
        "INSERT INTO work_workspace_claims (work_id, repository, purpose, path, claimed_at,"
        " released_at) VALUES (?, 'o/r', ?, ?, ?, ?)",
        (work_id, purpose, path, claimed_at, released_at),
    )


def _outcome(conn, work_id, run_id, state, observed_at):
    conn.execute(
        "INSERT INTO run_execution_outcomes (work_id, run_id, execution_state, observed_at)"
        " VALUES (?, ?, ?, ?)",
        (work_id, run_id, state, observed_at),
    )


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(claims.SCHEMA)
    conn.executescript(run_execution.SCHEMA)
    return conn


def _rows(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_the_stores_and_readers_open_the_expanded_path(home, cwd, monkeypatch):
    monkeypatch.chdir(cwd)
    store = claims.ClaimStore(RAW)
    outcomes = run_execution.OutcomeStore(RAW)
    try:
        assert store.path == home / ".local/share/agentdeck/agentdeck.db"
        assert outcomes.path == store.path
    finally:
        store.close()
        outcomes.close()
    conn = _open(store.path)
    _claim(conn, "w1", "/wt/1")
    _outcome(conn, "w1", "r1", "running", 5.0)
    conn.close()

    assert [c.path for c in claims.read_claims(RAW, "w1")] == ["/wt/1"]
    assert [o.run_id for o in run_trace.read_outcomes(RAW, "w1")] == ["r1"]
    assert not (cwd / "~").exists()


def test_the_stray_file_is_merged_verified_and_moved_aside(home, cwd):
    stray = cwd / RAW
    _open(stray).close()
    # Written by a process that dies without closing, as a stopped Runtime does, so the
    # rows are still only in the WAL.
    subprocess.run(
        [sys.executable, "-c", _KILLED_WRITER, str(stray)],
        check=True,
    )
    assert stray.with_name(stray.name + "-wal").stat().st_size > 0

    real = home / ".local/share/agentdeck/agentdeck.db"
    conn = _open(real)
    _outcome(conn, "w1", "r0", "ended", 1.0)
    conn.close()

    assert stray_db.merge_stray_db(RAW, cwd) == "merged"

    assert _rows(
        real, "SELECT work_id, path, released_at FROM work_workspace_claims ORDER BY work_id"
    ) == [("w1", "/wt/1", None), ("w2", "/wt/2", "t2")]
    assert _rows(real, "SELECT run_id FROM run_execution_outcomes ORDER BY run_id") == [
        ("r0",),
        ("r1",),
    ]
    assert not (cwd / "~").exists()
    backups = sorted(p.name for p in real.parent.glob("agentdeck.db.stray-2199-*"))
    assert len(backups) == 1 and not backups[0].endswith(("-wal", "-shm"))
    # The real file is left usable by the stores.
    store = claims.ClaimStore(real)
    try:
        assert [c.work_id for c in store.active()] == ["w1"]
    finally:
        store.close()


def test_a_second_merge_of_the_same_rows_duplicates_nothing(home, cwd):
    real = home / ".local/share/agentdeck/agentdeck.db"
    for _ in range(2):
        conn = _open(cwd / RAW)
        conn.execute("DELETE FROM work_workspace_claims")
        _claim(conn, "w2", "/wt/2", released_at="t2")
        conn.close()
        assert stray_db.merge_stray_db(RAW, cwd) == "merged"
    assert _rows(real, "SELECT count(*) FROM work_workspace_claims") == [(1,)]


def test_the_real_files_active_claim_and_newer_outcome_win(home, cwd):
    real = home / ".local/share/agentdeck/agentdeck.db"
    conn = _open(real)
    _claim(conn, "w1", "/wt/real", claimed_at="t5")
    _outcome(conn, "w1", "r1", "ended", 9.0)
    conn.close()
    conn = _open(cwd / RAW)
    _claim(conn, "w1", "/wt/stray")
    _outcome(conn, "w1", "r1", "running", 5.0)
    _outcome(conn, "w1", "r2", "running", 6.0)
    conn.close()

    assert stray_db.merge_stray_db(RAW, cwd) == "merged"
    assert _rows(real, "SELECT path FROM work_workspace_claims") == [("/wt/real",)]
    assert _rows(
        real, "SELECT run_id, execution_state FROM run_execution_outcomes ORDER BY run_id"
    ) == [("r1", "ended"), ("r2", "running")]


def test_a_newer_stray_outcome_replaces_the_older_real_one(home, cwd):
    real = home / ".local/share/agentdeck/agentdeck.db"
    conn = _open(real)
    _outcome(conn, "w1", "r1", "running", 1.0)
    conn.close()
    conn = _open(cwd / RAW)
    _outcome(conn, "w1", "r1", "stopped", 7.0)
    conn.close()

    assert stray_db.merge_stray_db(RAW, cwd) == "merged"
    assert _rows(real, "SELECT execution_state, observed_at FROM run_execution_outcomes") == [
        ("stopped", 7.0)
    ]


def test_a_failed_merge_leaves_the_stray_file_in_place(home, cwd, monkeypatch):
    stray = cwd / RAW
    conn = _open(stray)
    _claim(conn, "w1", "/wt/1")
    conn.close()
    monkeypatch.setattr(stray_db, "_missing", lambda stray, real: 1)

    assert stray_db.merge_stray_db(RAW, cwd) == "failed"
    assert stray.is_file()
    assert not list((home / ".local/share/agentdeck").glob("*.stray-2199-*"))


@pytest.mark.parametrize("raw", ["/abs/agentdeck.db", "relative.db"])
def test_nothing_to_merge_without_a_tilde_path(raw, cwd):
    assert stray_db.stray_path(raw, cwd) is None
    assert stray_db.merge_stray_db(raw, cwd) == "absent"


def test_no_stray_file_is_nothing_to_merge(home, cwd):
    assert stray_db.merge_stray_db(RAW, cwd) == "absent"
    assert not (home / ".local").exists()


@pytest.mark.anyio
async def test_the_runtime_keeps_both_stores_closed_while_the_stray_file_is_unmerged(
    home, cwd, monkeypatch
):
    """A stray file that did not merge still holds active claims: run without them and a
    conflicting claim could be written, so both services answer unavailable instead."""
    from httpx import ASGITransport, AsyncClient

    from agentdeck.config import AppConfig
    from agentdeck.runtime import create_runtime_app

    conn = _open(cwd / RAW)
    _claim(conn, "w1", "/wt/1")
    conn.close()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(stray_db, "_missing", lambda stray, real: 1)
    config = AppConfig.model_validate(
        {
            "keyed_workers": {"state_dir": str(home / "state")},
            "restart_nudge": {"file": str(home / "restart-nudge.json")},
            "history": {"db_path": RAW},
            "accounts": [],
        }
    )
    app = create_runtime_app(config)
    try:
        assert app.state.work_workspaces is None and app.state.run_executions is None
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://runtime"
        ) as client:
            listed = await client.get(claims.CLAIMS_PATH)
        assert listed.status_code == 503
        assert (cwd / RAW).is_file()
    finally:
        app.state.budget_db.close()
