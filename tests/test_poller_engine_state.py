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
    write_json_atomic,
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


def test_durable_json_is_written_compact(tmp_path):
    path = tmp_path / "x.json"
    write_json_atomic(path, {"a": [1, {"b": 2}]})
    assert path.read_text() == '{"a":[1,{"b":2}]}'


def test_settled_commands_move_to_the_archive_and_live_ones_stay(tmp_path):
    paths = EnginePaths.from_base(tmp_path / "state.json")
    live = {
        str(number): {"id": str(number), "status": status}
        for number, status in enumerate(("new", "queued", "processing", "from-the-future"))
    }
    settled = {
        status: {"id": status, "status": status, "body": "/claude go"}
        for status in ("done", "cancelled", "rejected", "adopted")
    }
    # The shape an instance has before this change: the same commands in both files.
    envelope = {"schema_version": CURRENT_SCHEMA_VERSION, "data": {"controls": {**live, **settled}}}
    paths.state.write_text(json.dumps(envelope))
    paths.controls.write_text(json.dumps({"controls": {**live, **settled}}))
    # Moved by an earlier wake: a later move must add to the archive, never replace it.
    older = {"older": {"id": "older", "status": "done"}}
    paths.controls_archive.write_text(json.dumps(older))
    store = StateStore(paths)

    with store.transaction():
        pass

    for commands in (
        json.loads(paths.state.read_text())["data"]["controls"],
        json.loads(paths.controls.read_text())["controls"],
        store.read()["controls"],
    ):
        assert commands == live
    assert json.loads(paths.controls_archive.read_text()) == {**older, **settled}
    # The value it replaced is still the backup, until the next publish.
    assert set(json.loads(paths.backup.read_text())["data"]["controls"]) == {*live, *settled}

    paths.controls_archive.unlink()
    with store.transaction():
        pass
    assert not paths.controls_archive.exists(), "a wake with nothing to move writes no archive"


def test_a_crash_between_the_two_publishes_loses_no_settled_command(tmp_path):
    paths = EnginePaths.from_base(tmp_path / "state.json")
    done = {"7": {"id": "7", "status": "done"}}
    envelope = {"schema_version": CURRENT_SCHEMA_VERSION, "data": {"controls": dict(done)}}
    paths.state.write_text(json.dumps(envelope))
    # The ledger was published without them; the state was not.
    paths.controls.write_text(json.dumps({"controls": {}}))
    paths.controls_archive.write_text(json.dumps(done))
    store = StateStore(paths)

    with store.transaction():
        pass

    assert json.loads(paths.controls_archive.read_text()) == done
    assert store.read().get("controls", {}) == {}


def _instance_with_a_done_command(tmp_path) -> EnginePaths:
    paths = EnginePaths.from_base(tmp_path / "state.json")
    envelope = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "data": {"controls": {"7": {"id": "7", "status": "done"}}},
    }
    paths.state.write_text(json.dumps(envelope))
    return paths


def test_an_archive_nothing_can_read_is_never_overwritten(tmp_path):
    paths = _instance_with_a_done_command(tmp_path)
    paths.controls_archive.write_text("{half a fi")
    store = StateStore(paths)

    with store.transaction():
        pass

    assert paths.controls_archive.read_text() == "{half a fi"
    assert not paths.controls_archive_backup.exists()
    assert set(store.read()["controls"]) == {"7"}, "the command stays in the state instead"

    # Once a person repairs it, the next wake moves the command after all.
    paths.controls_archive.write_text('{"older":{"id":"older","status":"done"}}')
    with store.transaction():
        pass
    assert set(json.loads(paths.controls_archive.read_text())) == {"older", "7"}
    assert store.read().get("controls", {}) == {}


def test_a_corrupt_archive_recovers_from_its_backup(tmp_path):
    paths = _instance_with_a_done_command(tmp_path)
    paths.controls_archive.write_text("{half a fi")
    paths.controls_archive_backup.write_text('{"older":{"id":"older","status":"done"}}')

    with StateStore(paths).transaction():
        pass

    assert set(json.loads(paths.controls_archive.read_text())) == {"older", "7"}
    assert set(json.loads(paths.controls_archive_backup.read_text())) == {"older"}


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
