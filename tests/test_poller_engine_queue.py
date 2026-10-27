"""Slice 2 of #176: durable command queue, dedup, FIFO order, and scan cursor."""

from __future__ import annotations

from datetime import UTC, datetime

from agentdeck.poller_engine import queue


def _comment(cid, *, num=1, created, body="/claude go", login="alice", type_="User"):
    base = "https://api.github.com/repos/org/store"
    return {
        "id": cid,
        "issue_url": f"{base}/issues/{num}",
        "html_url": f"https://github.com/org/store/issues/{num}#c{cid}",
        "body": body,
        "created_at": created,
        "user": {"login": login, "type": type_},
    }


def _at(stamp):
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _enq(state, cid, *, num=1, created, kind="claude", payload=None):
    rec = queue.build_record(_comment(cid, num=num, created=created), kind, payload, repo="store")
    assert rec is not None
    return queue.enqueue(state, rec)


def test_build_record_extracts_number_and_repo():
    comment = _comment(10, num=42, created="2026-07-25T10:00:00Z")
    rec = queue.build_record(comment, "claude", "go", repo="store")
    assert rec is not None
    assert rec["number"] == 42
    assert rec["repo"] == "store"
    assert rec["kind"] == "claude"
    assert rec["payload"] == "go"


def test_build_record_returns_none_on_unparseable_number():
    bad = _comment(10, created="x")
    bad["issue_url"] = "https://api.github.com/repos/org/store/issues/not-a-number"
    assert queue.build_record(bad, "claude", "go", repo="store") is None


def test_enqueue_dedups_by_comment_id():
    state: dict = {}
    assert _enq(state, 5, created="2026-07-25T10:00:00Z", kind="merge") is True
    assert _enq(state, 5, created="2026-07-25T10:00:00Z", kind="merge") is False
    assert len(queue.controls(state)) == 1


def test_pending_is_fifo_by_created_then_id():
    state: dict = {}
    # Ingest out of order; expect (created_at, id) ordering back.
    _enq(state, 200, created="2026-07-25T10:05:00Z", payload="b")
    _enq(state, 100, created="2026-07-25T10:00:00Z", payload="a")
    _enq(state, 150, created="2026-07-25T10:05:00Z", payload="c")
    # 'a' earliest; 'b' and 'c' share a timestamp so tie-break on id (150 < 200 -> c before b).
    assert [c["payload"] for c in queue.pending(state)] == ["a", "c", "b"]


def test_two_commands_persist_between_polls_in_fifo_order():
    state: dict = {}
    _enq(state, 1, created="2026-07-25T10:00:00Z", payload="first")
    # A second poll ingests a later comment; the first must still be queued, in order.
    _enq(state, 2, created="2026-07-25T10:01:00Z", payload="second")
    assert [c["payload"] for c in queue.pending(state)] == ["first", "second"]


def test_pending_filters_by_card_and_status():
    state: dict = {}
    _enq(state, 1, num=1, created="2026-07-25T10:00:00Z", payload="x")
    _enq(state, 2, num=2, created="2026-07-25T10:00:01Z", payload="y")
    queue.controls(state)["2"]["status"] = "done"
    got = queue.pending(state, repo="store", number=1, statuses={"new"})
    assert [c["payload"] for c in got] == ["x"]


def test_is_bot_comment():
    assert queue.is_bot_comment({"user": {"login": "dependabot[bot]", "type": "User"}})
    assert queue.is_bot_comment({"user": {"login": "svc", "type": "Bot"}})
    assert not queue.is_bot_comment({"user": {"login": "alice", "type": "User"}})


def test_scan_floor_uses_lookback_when_no_cursor():
    now = datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC)
    assert queue.scan_floor({}, now=now, lookback_s=300) == "2026-07-25T10:00:00+00:00"


def test_scan_floor_overlaps_existing_cursor():
    state = {"control_since": "2026-07-25T10:05:00+00:00"}
    now = datetime(2026, 7, 25, 11, 0, 0, tzinfo=UTC)
    assert queue.scan_floor(state, now=now, overlap_s=10) == "2026-07-25T10:04:50+00:00"


def test_record_scan_advances_cursor_only_when_called():
    state: dict = {}
    # Simulate a failed scan: cursor is not recorded, so nothing is skipped next time.
    assert queue.get_cursor(state) is None
    queue.record_scan(state, datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC), overlap_s=90)
    # An empty scan proves nothing newer than its own horizon.
    assert queue.get_cursor(state) == "2026-07-25T10:03:30+00:00"


# --- the scan lag window (#267) ---

def test_the_overlap_outlasts_a_wake():
    """An overlap shorter than the wake interval leaves a gap no scan ever looks at again."""
    assert queue.DEFAULT_CURSOR_OVERLAP_S > queue.WAKE_INTERVAL_S
    assert queue.DEFAULT_CURSOR_OVERLAP_S == queue.WAKE_INTERVAL_S * queue.CURSOR_OVERLAP_WAKES


def test_the_cursor_never_passes_a_comment_the_scan_did_not_return():
    """GitHub's comment list is not instantaneous: a comment created a moment before the scan
    can be absent from its response. If the cursor jumped to the wall clock anyway, the next
    floor would start after that comment and the command would be dropped for good."""
    state: dict = {}
    scan_to = datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC)
    # Created before the scan, missing from its response.
    lagging = datetime(2026, 7, 25, 10, 4, 59, tzinfo=UTC)
    queue.record_scan(state, scan_to, comments=[])

    assert _at(queue.get_cursor(state)) < lagging
    # And the floor the next wake asks GitHub for still reaches back past it.
    assert _at(queue.scan_floor(state, now=scan_to)) < lagging


def test_the_cursor_advances_to_the_newest_comment_actually_seen():
    state: dict = {}
    queue.record_scan(
        state,
        datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC),
        comments=[
            {"id": 1, "created_at": "2026-07-25T10:04:10Z"},
            {"id": 2, "created_at": "2026-07-25T10:04:50Z"},
        ],
    )
    assert queue.get_cursor(state) == "2026-07-25T10:04:50+00:00"


def test_the_cursor_follows_the_field_github_filters_on():
    """`since` selects on updated_at, so an edited old comment is what the scan proved."""
    state: dict = {}
    queue.record_scan(
        state,
        datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC),
        comments=[{"id": 1, "created_at": "2026-07-25T09:00:00Z",
                   "updated_at": "2026-07-25T10:04:40Z"}],
    )
    assert queue.get_cursor(state) == "2026-07-25T10:04:40+00:00"


def test_a_comment_dated_after_the_scan_does_not_drag_the_cursor_forward():
    """Clock skew is not proof: a timestamp the scan cannot vouch for would skip real comments."""
    state: dict = {}
    queue.record_scan(
        state,
        datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC),
        comments=[{"id": 1, "created_at": "2026-07-25T10:09:00Z"}],
        overlap_s=90,
    )
    assert queue.get_cursor(state) == "2026-07-25T10:03:30+00:00"


def test_the_cursor_never_moves_backwards():
    """A quiet wake must not widen the window it already earned, one overlap per wake."""
    state = {"control_since": "2026-07-25T10:04:55+00:00"}
    queue.record_scan(state, datetime(2026, 7, 25, 10, 5, 0, tzinfo=UTC), overlap_s=90)
    assert queue.get_cursor(state) == "2026-07-25T10:04:55+00:00"
