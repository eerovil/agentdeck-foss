"""The engine never takes the ownership label off an issue (#2497).

A release used to remove it, and that removal was also what kept a released card from being
picked up again: the labelled-issue sweep only lists open issues that still wear the label. With
the label left on, a released card that is still open is swept again, so the ownership ledger
keeps the label event it last saw. The same event is then already accounted for — no actor
lookup and no start — while a person removing and re-adding the label is a new event and starts
the card as it always did.
"""
from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine.ownership import LabelEvent
from agentdeck.poller_engine.reconcile import CardSnapshot

_KEY = "store#5"
# Added a moment before the first wake, so it is newer than the last complete scan recorded
# below: a card the ledger had forgotten would read this label as freshly added.
_LABEL = LabelEvent("e1", "2026-07-25T11:59:50Z")


def _ledger(**events) -> dict:
    return {
        "ownership": {
            "initialized": True,
            "events": dict(events),
            "last_complete_scan_at": "2026-07-25T11:59:00+00:00",
        }
    }


def _labelled_port(snapshot: CardSnapshot, **kwargs) -> FakePort:
    return FakePort(
        labelled=[_KEY], label_events={_KEY: [_LABEL]}, snapshots={_KEY: snapshot}, **kwargs
    )


def test_a_closed_card_keeps_its_label_and_the_ledger_keeps_its_event(tmp_path):
    """The #504 case: the worker closed the issue, and the release leaves `claude` on it."""
    port = FakePort(snapshots={_KEY: CardSnapshot(open=False)})
    store = store_for(
        tmp_path, {"issues": {_KEY: {"status": "In review"}}, **_ledger(**{_KEY: "e1"})}
    )

    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    assert _KEY not in state["issues"]
    assert outcome.report.releases == 1
    assert port.ownership_edits() == []
    assert state["ownership"]["events"][_KEY] == "e1"


def test_an_open_done_card_is_not_looked_up_or_started_again_after_its_release(tmp_path):
    port = _labelled_port(CardSnapshot(status="Done", owned=True))
    store = store_for(tmp_path, {"issues": {_KEY: {"status": "Done"}}, **_ledger(**{_KEY: "e1"})})

    run_wake(store, port, now=_NOW)
    assert _KEY not in store.read()["issues"]

    for seconds in (30, 60):
        later = run_wake(store, port, now=_NOW + timedelta(seconds=seconds))
        assert later.directives == []
        assert port.ownership_actor_requests == [], "the same label was looked up again"
    assert port.ownership_edits() == []
    assert store.read()["ownership"]["events"][_KEY] == "e1"


def test_an_idle_stop_leaves_the_label_on_and_the_card_is_not_started_again(tmp_path):
    port = _labelled_port(
        CardSnapshot(status="In review", owned=True), comments=[comment(2, "/claude stop")]
    )
    store = store_for(
        tmp_path,
        {"issues": {_KEY: {"status": "In review", "started": True}}, **_ledger(**{_KEY: "e1"})},
    )

    first = run_wake(store, port, now=_NOW)
    assert first.report.releases == 1
    assert store.read()["controls"]["2"]["status"] == "done"

    port.comments = []
    port.snapshots[_KEY] = CardSnapshot(status="Todo", owned=True)
    for seconds in (30, 60):
        later = run_wake(store, port, now=_NOW + timedelta(seconds=seconds))
        assert later.directives == []
    assert port.ownership_actor_requests == []
    assert port.ownership_edits() == []


def test_a_stopped_card_is_not_restarted_by_a_wake_whose_scan_is_incomplete(tmp_path):
    """A labeler another card's start could not authorize leaves the scan incomplete, so the
    ledger's last complete scan stops advancing. A stopped card whose label event the ledger had
    dropped would then read that label as newly added and restart."""
    port = FakePort(
        labelled=[_KEY, "store#9"],
        label_events={
            _KEY: [_LABEL],
            "store#9": [LabelEvent("n1", "2026-07-25T12:00:10Z")],
        },
        snapshots={
            _KEY: CardSnapshot(status="Todo", owned=True),
            "store#9": CardSnapshot(status="Todo", owned=True),
        },
        actors={"store#9": None},
        comments=[comment(2, "/claude stop")],
    )
    store = store_for(
        tmp_path,
        {"issues": {_KEY: {"status": "Todo", "started": True}}, **_ledger(**{_KEY: "e1"})},
    )

    run_wake(store, port, now=_NOW)
    port.comments = []
    later = run_wake(store, port, now=_NOW + timedelta(seconds=30))

    assert later.directives == []
    assert not (store.read()["issues"].get(_KEY) or {}).get("manual_start")


def test_removing_and_re_adding_the_label_on_a_released_card_starts_it(tmp_path):
    port = _labelled_port(CardSnapshot(status="Done", owned=True))
    store = store_for(tmp_path, {"issues": {_KEY: {"status": "Done"}}, **_ledger(**{_KEY: "e1"})})
    run_wake(store, port, now=_NOW)
    assert _KEY not in store.read()["issues"]

    port._label_events[_KEY] = [_LABEL, LabelEvent("e2", "2026-07-25T12:00:40Z")]
    port.snapshots[_KEY] = CardSnapshot(status="Todo", owned=True)
    again = run_wake(store, port, now=_NOW + timedelta(seconds=60))

    assert any("label added -> start" in line for line in again.log), again.log
    assert store.read()["ownership"]["events"][_KEY] == "e2"
