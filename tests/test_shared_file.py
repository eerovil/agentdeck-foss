"""Stores two web workers share during a hot reload keep each other's writes (#3070)."""

from __future__ import annotations

import time

from agentdeck.shared_file import SharedFile, add_drained_hook, refresh_shared_files
from agentdeck.wake_on import WakeOnRegistry, WakeRequest
from agentdeck.work_release import ReleaseStore
from agentdeck.work_router import PendingActions, ReviewedHeads


def _wait(id: str, chat: str = "chat-1") -> WakeRequest:
    now = time.time()
    return WakeRequest(
        id=id,
        session_key=f"claude_code:main:{chat}",
        chat_id=chat,
        registered_at=now,
        deadline=now + 600,
        kind="time",
        repository="",
        note=f"wait {id}",
    )


def test_a_wake_on_the_old_worker_accepted_is_seen_and_kept_by_the_new_one(tmp_path):
    path = tmp_path / "wake-on.json"
    new, old = WakeOnRegistry(path), WakeOnRegistry(path)
    assert new.all() == ()  # the new worker loads at start

    old.add(_wait("from-old"))  # the old worker still serves for minutes

    assert [item.id for item in new.all()] == ["from-old"]
    new.add(_wait("from-new", chat="chat-2"))
    assert sorted(item.id for item in WakeOnRegistry(path).all()) == ["from-new", "from-old"]


def test_a_foreign_write_replans_the_wake_on_deadlines_once_the_old_worker_drained(tmp_path):
    path = tmp_path / "wake-on.json"
    new, old = WakeOnRegistry(path), WakeOnRegistry(path)
    new.all()
    changes: list[str] = []
    new.on_change = lambda: changes.append("changed")

    old.add(_wait("late"))
    assert refresh_shared_files() >= 1

    assert changes == ["changed"]
    assert refresh_shared_files() == 0  # nothing new since
    assert changes == ["changed"]

    # State the database holds runs its own hook each time, written or not (#3244).
    drained: list[str] = []
    remove = add_drained_hook(lambda: drained.append("drained"))
    refresh_shared_files()
    remove()
    refresh_shared_files()
    assert drained == ["drained"]


def test_release_writes_from_both_workers_survive(tmp_path):
    path = tmp_path / "work-release.json"
    new, old = ReleaseStore(path), ReleaseStore(path)
    new.works  # noqa: B018 - loaded at start

    old.put("work-a", {"asked": True})
    new.put("work-b", {"asked": True})

    assert set(ReleaseStore(path).works) == {"work-a", "work-b"}


def test_a_queue_edit_keeps_the_other_workers_release_facts(tmp_path):
    path = tmp_path / "work-release.json"
    new, old = ReleaseStore(path), ReleaseStore(path)
    new.queues.setdefault("eerovil/agentdeck", []).append({"pull": "#1", "work": "w"})
    old.put("work-a", {"asked": True})

    new.put_queues()

    fresh = ReleaseStore(path)
    assert set(fresh.works) == {"work-a"}
    assert fresh.queues == {"eerovil/agentdeck": [{"pull": "#1", "work": "w"}]}


def test_router_lists_keep_both_workers_entries(tmp_path):
    heads_new, heads_old = ReviewedHeads(tmp_path / "h.json"), ReviewedHeads(tmp_path / "h.json")
    assert "x" not in heads_new
    heads_old.add("x")
    heads_new.add("y")
    fresh_heads = ReviewedHeads(tmp_path / "h.json")
    assert "x" in fresh_heads and "y" in fresh_heads

    pending_new = PendingActions(tmp_path / "p.json")
    pending_old = PendingActions(tmp_path / "p.json")
    assert pending_new.entries == {}
    pending_old.put("message:1", {"text": "hi"}, now=100.0)
    pending_new.changed.clear()

    refresh_shared_files()

    assert pending_new.changed.is_set()  # its retry loop re-plans
    pending_new.put("message:2", {"text": "yo"}, now=100.0)
    assert set(PendingActions(tmp_path / "p.json").entries) == {"message:1", "message:2"}


def test_the_lock_is_reentrant_and_a_missing_file_reads_as_none(tmp_path):
    shared = SharedFile(tmp_path / "s.json", prefix=".s.")
    assert shared.stale()
    assert shared.read() is None
    assert not shared.stale()
    with shared.locked(), shared.locked():
        shared.write({"a": 1})
    assert not shared.stale()
    SharedFile(tmp_path / "s.json", prefix=".s.").write({"a": 2})
    assert shared.stale()
