"""Slice 1 of #176: durable engine state — atomic backup IO, lock, versioned schema."""

from __future__ import annotations

import json

import pytest

from agentdeck.poller_engine.state import (
    CURRENT_SCHEMA_VERSION,
    EnginePaths,
    StateSchemaError,
    StateStore,
    read_json_with_backup,
)


def _store(tmp_path) -> StateStore:
    return StateStore(EnginePaths.from_base(tmp_path / "state.json"))


def test_paths_derive_siblings_from_base(tmp_path):
    paths = EnginePaths.from_base(tmp_path / "state.json")
    assert paths.state == tmp_path / "state.json"
    assert paths.backup == tmp_path / "state.json.bak"
    assert paths.lock == tmp_path / "state.json.lock"
    assert paths.controls == tmp_path / "controls.json"
    assert paths.ownership == tmp_path / "ownership.json"
    assert not hasattr(paths, "stop_ack_dir")  # the Runtime proves a stop now (#2159)
    assert paths.dispatch_ack_dir == tmp_path / "dispatch-acks"
    assert paths.dispatch_defer_dir == tmp_path / "dispatch-deferrals"


def test_transaction_round_trips(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as data:
        data["issues"] = {"store#1": {"status": "Todo"}}
    assert store.read() == {"issues": {"store#1": {"status": "Todo"}}}


def test_read_is_empty_before_first_write(tmp_path):
    assert _store(tmp_path).read() == {}


def test_on_disk_format_is_a_versioned_envelope(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as data:
        data["k"] = "v"
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw == {"schema_version": CURRENT_SCHEMA_VERSION, "data": {"k": "v"}}


def test_backup_keeps_previous_value_and_read_falls_back(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as data:
        data["n"] = 1
    with store.transaction() as data:
        data["n"] = 2
    # .bak holds the value before the second write.
    backup = json.loads((tmp_path / "state.json.bak").read_text())
    assert backup == {"schema_version": CURRENT_SCHEMA_VERSION, "data": {"n": 1}}
    # Corrupt the primary → read recovers the last valid snapshot from .bak.
    (tmp_path / "state.json").write_text("{ this is not json")
    assert store.read() == {"n": 1}


def test_exception_in_transaction_persists_nothing(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as data:
        data["kept"] = True
    with pytest.raises(RuntimeError):
        with store.transaction() as data:
            data["kept"] = False
            data["added"] = 1
            raise RuntimeError("boom")
    assert store.read() == {"kept": True}


def test_atomic_write_leaves_no_temp_files(tmp_path):
    store = _store(tmp_path)
    with store.transaction() as data:
        data["x"] = 1
    assert not list(tmp_path.glob("*.tmp"))


def test_legacy_bare_dict_is_migrated_then_reenveloped(tmp_path):
    # A pre-versioning state file is a bare dict with no schema_version.
    (tmp_path / "state.json").write_text(json.dumps({"issues": {"store#7": {}}}))
    store = _store(tmp_path)
    assert store.read() == {"issues": {"store#7": {}}}
    # The first write upgrades it in place to the current envelope.
    with store.transaction() as data:
        data.setdefault("issues", {})["store#8"] = {}
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["schema_version"] == CURRENT_SCHEMA_VERSION
    assert set(raw["data"]["issues"]) == {"store#7", "store#8"}


def test_the_file_overlap_gates_leftovers_are_dropped_on_load(tmp_path):
    """#2081: a live instance carries the dead gate's keys until this migration runs.

    `overlap_hubs` had grown to kilobytes of tally nobody reads, and every card the gate was
    holding still wears `blocked_by_overlap`. A held card has to start on the first wake after
    the deploy, so the hold is not merely ignored — it is removed from the record.
    """
    (tmp_path / "state.json").write_text(json.dumps({"schema_version": 1, "data": {
        "overlap_hubs": {"src/agentdeck/app.py": ["store#5", "store#6", "store#7"]},
        "issues": {
            "store#5": {"status": "In progress"},
            "store#6": {
                "status": "Todo", "pending": ["8"],
                "blocked_by_overlap": {"owner": "store#5", "paths": ["src/agentdeck/app.py"]},
            },
        },
    }}))
    store = _store(tmp_path)

    data = store.read()

    assert "overlap_hubs" not in data
    assert "blocked_by_overlap" not in data["issues"]["store#6"]
    # Everything else about the waiting card survives, so the next wake starts it normally.
    assert data["issues"]["store#6"]["pending"] == ["8"]
    with store.transaction() as live:
        live.setdefault("issues", {})
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["schema_version"] == CURRENT_SCHEMA_VERSION


def test_newer_schema_fails_closed(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": CURRENT_SCHEMA_VERSION + 1, "data": {}})
    )
    store = _store(tmp_path)
    with pytest.raises(StateSchemaError):
        store.read()
    with pytest.raises(StateSchemaError):
        with store.transaction():
            pass


def test_malformed_envelope_fails_closed(tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"schema_version": "one", "data": {}})
    )
    with pytest.raises(StateSchemaError):
        _store(tmp_path).read()


def test_primitive_read_with_backup_returns_default_when_absent(tmp_path):
    default = {"seed": 1}
    got = read_json_with_backup(
        tmp_path / "missing.json", tmp_path / "missing.json.bak", default
    )
    assert got == default
    assert got is not default  # a copy, not the caller's dict
