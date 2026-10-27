"""The one-time backfill runs the legacy parent rule once and says what to save (#2207)."""

from agentdeck.work import WorkRef
from agentdeck.work_backfill import BackfillRow, LegacyChat, plan_backfill

A = WorkRef("eerovil/agentdeck", 1)
B = WorkRef("eerovil/agentdeck", 2)


def _rows(result):
    return sorted((r.session_key, r.work_id, r.evidence, r.parent_chat_id) for r in result.rows)


def test_single_work_parent_is_backfilled_through_a_chain():
    result = plan_backfill(
        [
            LegacyChat("parent", "p", frozenset({A})),
            LegacyChat("child", "c", parent="parent", delegated=True),
            LegacyChat("grandchild", "g", parent="child", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [
        ("c", A.work_id, "backfilled", "parent"),
        ("g", A.work_id, "backfilled", "child"),
    ]
    assert result.summary() == {
        "considered": 2,
        "backfilled": 2,
        "ambiguous": 0,
        "no_work": 0,
        "pending": 0,
        "rows": 2,
    }


def test_parent_with_several_works_saves_every_candidate_as_ambiguous():
    result = plan_backfill(
        [
            LegacyChat("parent", "p", frozenset({A, B})),
            LegacyChat("child", "c", parent="parent", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [
        ("c", A.work_id, "ambiguous", "parent"),
        ("c", B.work_id, "ambiguous", "parent"),
    ]
    assert (result.backfilled, result.ambiguous) == (0, 1)


def test_child_with_its_own_second_work_makes_its_child_ambiguous():
    result = plan_backfill(
        [
            LegacyChat("parent", "p", frozenset({A})),
            LegacyChat("child", "c", frozenset({B}), parent="parent", delegated=True),
            LegacyChat("grandchild", "g", parent="child", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [
        ("c", A.work_id, "backfilled", "parent"),
        ("g", A.work_id, "ambiguous", "child"),
        ("g", B.work_id, "ambiguous", "child"),
    ]


def test_legs_naming_different_parents_are_ambiguous():
    result = plan_backfill(
        [
            LegacyChat("p1", "p1", frozenset({A})),
            LegacyChat("p2", "p2", frozenset({B})),
            LegacyChat("child", "c1", parent="p1", delegated=True),
            LegacyChat("child", "c2", parent="p2", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [
        ("c1", A.work_id, "ambiguous", "p1,p2"),
        ("c1", B.work_id, "ambiguous", "p1,p2"),
        ("c2", A.work_id, "ambiguous", "p1,p2"),
        ("c2", B.work_id, "ambiguous", "p1,p2"),
    ]


def test_workless_parent_saves_none_and_unresolved_parent_stays_pending():
    result = plan_backfill(
        [
            LegacyChat("idle", "i"),
            LegacyChat("a", "a", parent="unresolved:gone", delegated=True),
            LegacyChat("b", "b", parent="idle", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [("b", "", "none", "idle")]
    assert (result.considered, result.no_work, result.pending) == (2, 1, 1)


def test_cycle_places_nothing_but_names_the_candidates():
    result = plan_backfill(
        [
            LegacyChat("a", "a", frozenset({A}), parent="b", delegated=True),
            LegacyChat("b", "b", parent="a", delegated=True),
            LegacyChat("self", "s", parent="self", delegated=True),
        ],
        decided=set(),
    )

    assert _rows(result) == [("a", "", "none", "b"), ("b", A.work_id, "ambiguous", "a")]
    assert result.pending == 1  # "self" names only itself


def test_decided_undelegated_and_provider_internal_chats_are_skipped():
    result = plan_backfill(
        [
            LegacyChat("parent", "p", frozenset({A})),
            LegacyChat("done", "d", parent="parent", delegated=True),
            LegacyChat("plain", "x", parent="parent"),
            LegacyChat("internal", "t", parent="parent", delegated=True, internal=True),
        ],
        decided={"done"},
    )

    assert result.rows == []
    assert result.considered == 0


def test_the_row_shape_is_what_the_store_takes():
    (row,) = plan_backfill(
        [
            LegacyChat("parent", "p", frozenset({A})),
            LegacyChat("child", "c", parent="parent", delegated=True),
        ],
        decided=set(),
    ).rows
    assert row == BackfillRow("c", A.work_id, "backfilled", "parent")


def test_collector_retries_a_failed_backfill_and_then_settles_every_scan():
    from types import SimpleNamespace

    from agentdeck.collector import Collector

    calls: list[str] = []
    marker: list[bool] = []

    def backfill():
        calls.append("backfill")
        if calls.count("backfill") == 1:
            raise RuntimeError("database is locked")
        marker.append(True)

    collector = SimpleNamespace(
        accounts=[SimpleNamespace(key="a"), SimpleNamespace(key="b")],
        _initial_scan_successes=set(),
        _work_backfill_checked=False,
        state=SimpleNamespace(
            backfill_delegated_work=backfill,
            delegated_work_backfill_done=lambda: bool(marker),
            finalize_pending_delegations=lambda: calls.append("finalize"),
        ),
    )

    collector._initial_scan_successes.add("a")
    Collector._settle_delegated_work(collector)
    assert calls == []
    collector._initial_scan_successes.add("b")
    Collector._settle_delegated_work(collector)  # fails, leaves the latch open
    assert calls == ["backfill"]
    assert not collector._work_backfill_checked
    Collector._settle_delegated_work(collector)  # retried, then settles
    Collector._settle_delegated_work(collector)
    assert calls == ["backfill", "backfill", "finalize", "finalize"]


def test_a_parent_chat_with_no_scanned_leg_is_resolved_but_carries_no_work():
    result = plan_backfill(
        [LegacyChat("child", "c", parent="chat-not-scanned", delegated=True)],
        decided=set(),
    )

    assert _rows(result) == [("c", "", "none", "chat-not-scanned")]
    assert (result.no_work, result.pending) == (1, 0)
