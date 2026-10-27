"""A project that switched its Works to the deck makes no new card (#2895 step 5, ADR 0008).

The deck's Work router starts a switched project's new Works, so the engine must neither
seed a labelled issue it does not hold nor turn a command on one into a card: either would
put a card on the board, and a board card holds the Work away from the deck's release. A
card the engine already holds keeps every phase until it is done.
"""

from __future__ import annotations

import pytest
from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck import poller_ops
from agentdeck.poller_engine import health, queue
from agentdeck.poller_engine.ownership import LabelEvent
from agentdeck.poller_engine.reconcile import CardSnapshot

_LEDGER = {
    "initialized": True,
    "events": {},
    "last_complete_scan_at": "2026-07-25T12:00:00+00:00",
}


def _fresh_label() -> FakePort:
    return FakePort(
        labelled=["store#7"],
        label_events={"store#7": [LabelEvent("e1", "2026-07-25T12:00:01Z")]},
        snapshots={"store#7": CardSnapshot(status="Todo")},
    )


def test_a_switched_project_does_not_start_a_labelled_issue_it_does_not_hold(tmp_path):
    store = store_for(tmp_path, {"ownership": dict(_LEDGER)})

    wake = run_wake(store, _fresh_label(), now=_NOW, works_router=True)

    assert wake.directives == []
    assert "store#7" not in store.read().get("issues", {})


def test_the_same_label_still_starts_a_card_where_the_project_has_not_switched(tmp_path):
    store = store_for(tmp_path, {"ownership": dict(_LEDGER)})

    wake = run_wake(store, _fresh_label(), now=_NOW)

    assert len(wake.directives) == 1
    assert "store#7" in store.read()["issues"]


def test_a_command_on_an_issue_no_card_holds_makes_no_card_and_is_settled(tmp_path):
    store = store_for(tmp_path, {"ownership": dict(_LEDGER)})
    port = FakePort(comments=[comment(1, "/claude do this")])

    wake = run_wake(store, port, now=_NOW, works_router=True)

    state = store.read()
    assert wake.directives == []
    assert "store#5" not in state.get("issues", {})
    # Settled, so the next wake does not read it again; the Work's chat hears the comment.
    assert queue.controls(state)["1"]["status"] == "cancelled"
    assert "no card: store#5 — the deck runs this project's Works" in wake.log


@pytest.mark.parametrize(
    "body",
    [
        "/merge",
        "/merge review",
        "/reviewed pass abcdef1234567890abcdef1234567890abcdef12 " + "0" * 64,
        "/claude stop",
    ],
)
def test_every_command_on_an_issue_no_card_holds_is_settled_once(tmp_path, body):
    """Not only `/claude`: a verdict or a merge left new would be re-read every wake."""
    store = store_for(tmp_path, {"ownership": dict(_LEDGER)})
    port = FakePort(comments=[comment(1, body)])

    wake = run_wake(store, port, now=_NOW, works_router=True)

    state = store.read()
    assert wake.directives == []
    assert "store#5" not in state.get("issues", {})
    assert queue.controls(state)["1"]["status"] == "cancelled"

    again = run_wake(store, FakePort(comments=[comment(1, body)]), now=_NOW, works_router=True)
    assert "no card: store#5 — the deck runs this project's Works" not in again.log


def test_a_switched_project_hands_every_held_card_to_the_deck(tmp_path):
    """#2895: the owner's call — nothing stays Poller-owned, in-flight cards included."""
    instance = tmp_path / "instances" / "store"
    instance.mkdir(parents=True)
    (instance / "poller.toml").write_text(
        """\
contract = 1
org = "eerovil"
ownership_label = "agent"

[[repos]]
id = "store"
holds_issues = true
holds_code = true
verification = "agentdeck_job"
"""
    )
    store = store_for(
        instance,
        {
            "issues": {
                "store#6": {"status": "Needs More Info"},
                "store#9": {"status": "Done"},
            },
        },
    )
    port = FakePort(comments=[comment(1, "/claude carry on")])
    first = run_wake(store, port, now=_NOW)
    assert len(first.directives) == 1
    before = store.read()
    assert queue.controls(before)["1"]["status"] == "processing"
    with store.transaction() as state:
        controls = queue.controls(state)
        controls.update(
            {
                cid: dict(controls["1"], id=cid, status="queued")
                for cid in ("2", "3", "4")
            }
        )
        rec = state["issues"]["store#5"]
        rec.update(pending=["2"], merge_control="3", stop_control="4")
        # As `dispatch.record_run_session` stores it.
        rec["run_session"] = {
            "session_url": "https://deck.example/chats/chat-77?x=1", "generation": "g1"
        }
        state["issues"]["store#6"]["run_session"] = {
            "session_url": "https://deck.example/sessions/leg-6", "generation": "g2"
        }
    port.comments = []

    wake = run_wake(store, port, now=_NOW, works_router=True)

    state = store.read()
    assert wake.directives == []  # the worker is not told anything by the engine any more
    assert set(state["issues"]) == {"store#9"}  # a Done card is not held
    handed = state["handed_over"]
    assert set(handed) == {"store#5", "store#6"}
    assert handed["store#5"]["status"] == "In progress"
    # The deck is handed the chat key from the Run link, never the stored record.
    assert handed["store#5"]["session"] == "chat-77"
    assert handed["store#6"]["session"] == "leg-6"
    assert "handed to the deck: store#5" in wake.log
    # Every control attachment is settled before `_forget`, so neither durable read recovers it.
    assert {control["status"] for control in queue.controls(state).values()} == {"cancelled"}
    assert poller_ops.read_board(tmp_path).card("eerovil/store", 5) is None
    # The command ledger stays settled, so the next sweep starts nothing.
    again = run_wake(store, port, now=_NOW, works_router=True)
    assert again.directives == [] and set(store.read()["issues"]) == {"store#9"}


def test_an_unswitched_project_keeps_its_cards(tmp_path):
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Todo"}}})
    run_wake(store, FakePort(), now=_NOW)
    state = store.read()
    assert "store#5" in state["issues"] and "handed_over" not in state


def test_a_paused_switched_project_hands_over_cards_without_running_phases(tmp_path):
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "In progress"}}})
    with store.transaction() as state:
        health.set_paused(state, True, note="release freeze")
    port = FakePort(comments=[comment(1, "/claude go")])

    outcome = run_wake(store, port, now=_NOW, works_router=True)
    state = store.read()

    assert outcome.halted == "paused"
    assert port.scans == 0
    assert "store#5" not in state["issues"]
    assert state["handed_over"]["store#5"]["status"] == "In progress"
    assert state["handed_over"]["store#5"]["session"] == ""  # no Run link recorded
