"""Slice 4 of #176: ownership provenance (label = ownership, not activity)."""

from __future__ import annotations

from datetime import UTC, datetime

from agentdeck.poller_engine import ownership


def test_new_ledger_shape():
    assert ownership.new_ledger() == {"initialized": False, "events": {}}


def test_bootstrap_adopts_world_without_classifying_starts():
    ledger = ownership.new_ledger()
    starts = ownership.detect_starts(ledger, {"store#1": ["e1"], "store#2": ["e2"]})
    assert starts == set()  # an existing label is not a fresh request
    assert ledger["initialized"] is True
    assert ledger["events"] == {"store#1": "e1", "store#2": "e2"}


def test_incomplete_bootstrap_defers_initialization():
    ledger = ownership.new_ledger()
    starts = ownership.detect_starts(ledger, {"store#1": ["e1"]}, all_scanned_ok=False)
    assert starts == set()
    assert ledger["initialized"] is False  # retry next poll
    assert ledger["events"] == {"store#1": "e1"}  # baseline still recorded


def test_unchanged_label_event_is_not_a_start():
    ledger = {"initialized": True, "events": {"store#1": "e1"}}
    assert ownership.detect_starts(ledger, {"store#1": ["e1"]}) == set()


def test_new_label_event_is_a_start_and_updates_baseline():
    ledger = {"initialized": True, "events": {"store#1": "e1"}}
    # A remove + re-add appends a new labeled event.
    starts = ownership.detect_starts(ledger, {"store#1": ["e1", "e2"]})
    assert starts == {"store#1"}
    assert ledger["events"]["store#1"] == "e2"


def test_recovery_does_not_reclassify_old_label():
    # Simulate durable state loss but a surviving ownership ledger: the same old
    # label event must NOT be read as a new start.
    ledger = {"initialized": True, "events": {"store#9": "old"}}
    assert ownership.detect_starts(ledger, {"store#9": ["old"]}) == set()


def test_multiple_cards_mixed():
    ledger = {"initialized": True, "events": {"a#1": "x", "b#2": "y"}}
    starts = ownership.detect_starts(ledger, {"a#1": ["x"], "b#2": ["y", "z"], "c#3": ["w"]})
    assert starts == {"b#2", "c#3"}  # b re-added; c is brand new


def test_unknown_old_event_is_adopted_after_a_complete_scan():
    ledger = {
        "initialized": True,
        "events": {},
        "last_complete_scan_at": "2026-08-09T08:00:00+00:00",
    }
    old = ownership.LabelEvent("old", "2026-08-03T18:59:33Z")

    starts = ownership.detect_starts(
        ledger,
        {"store#2819": [old]},
        scanned_at=datetime(2026, 8, 9, 9, tzinfo=UTC),
    )

    assert starts == set()
    assert ledger["events"]["store#2819"] == "old"


def test_unknown_event_newer_than_the_last_complete_scan_is_a_start():
    ledger = {
        "initialized": True,
        "events": {},
        "last_complete_scan_at": "2026-08-09T08:00:00+00:00",
    }
    fresh = ownership.LabelEvent("fresh", "2026-08-09T08:00:01Z")

    starts = ownership.detect_starts(ledger, {"store#7": [fresh]})

    assert starts == {"store#7"}
