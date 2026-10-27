"""The optional *Human review pending* column: the one status nothing automatic sets (#1465).

Every other column is a destination the engine drives a card into. This one is not. A person
drags a card into it, or a worker reports ``finish human_review_pending`` because its card said
in so many words that the work has to wait for somebody, and the engine's only job afterwards is
to leave the card alone.

Tests here come in pairs wherever the column matters: the same report with the column bound and
without it. A project that has not made the column must see exactly the old destinations,
because the role is optional and adding one to the engine cannot move another board's cards.
"""

from __future__ import annotations

from datetime import UTC, datetime

from poller_engine_fakes import FakePort, run_wake, store_for

from agentdeck.poller_engine import config, cycle
from agentdeck.poller_engine.config import Role
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot
from agentdeck.poller_engine.worker_control import WorkerControl

_KEY = "store#5"
_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_PENDING = "Human review pending"


def _store(tmp_path, **extra):
    rec = {"generation": "gen-1", "status": "In progress", **extra}
    return store_for(tmp_path, {"issues": {_KEY: rec}})


def _port(*, snapshot=None):
    return FakePort(snapshots={_KEY: snapshot or CardSnapshot(status="In progress")})


# --- a worker reports it, and only when it was asked to ---

def test_a_worker_asked_to_park_for_a_person_moves_the_card_into_the_column(tmp_path):
    port = _port()
    control = WorkerControl(_store(tmp_path), port, human_review_pending=_PENDING)

    result = control.finish("store", 5, "gen-1", "human_review_pending", "Waiting on Eero")

    assert result.ok
    assert [effect.kind for effect in port.applied] == [
        EffectKind.COMMENT,
        EffectKind.SET_STATUS,
    ]
    assert port.applied[1].value == _PENDING
    # The marker names the ending, so a lost board write is recovered as this column and not
    # as the Needs More Info the fallback below means.
    assert "<!-- claude-terminal generation=gen-1 status=human_review_pending -->" in str(
        port.applied[0].value
    )


def test_the_same_report_parks_in_needs_more_info_without_the_column(tmp_path):
    """A status write to an unbound role fails, which would leave the worker holding a landed
    terminal marker and a board that never moved. Needs More Info is where this board already
    looks for a person, so the report still says something true."""
    port = _port()

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "human_review_pending", "Waiting on Eero"
    )

    assert result.ok
    assert port.applied[1].value == Role.NEEDS_INFO.value
    assert "status=needs_more_info" in str(port.applied[0].value)


def test_an_unknown_finish_status_is_refused_and_the_refusal_names_the_new_ending(tmp_path):
    port = _port()

    result = WorkerControl(_store(tmp_path), port).finish(
        "store", 5, "gen-1", "human review", "Summary"
    )

    assert not result.ok
    assert "human_review_pending" in result.reason
    assert port.applied == []


def test_a_repeated_report_is_still_a_clean_noop(tmp_path):
    port = _port(snapshot=CardSnapshot(status=_PENDING, terminal_marker=_PENDING))

    result = WorkerControl(
        _store(tmp_path, status=_PENDING), port, human_review_pending=_PENDING
    ).finish("store", 5, "gen-1", "human_review_pending", "Same summary")

    assert result.ok
    assert result.reason == f"already finished -> {_PENDING}"
    assert port.applied == []


# --- nothing automatic writes it, and nothing automatic takes the card back out ---

def test_no_release_destination_is_this_column(tmp_path):
    """The release progression rests approved-but-unmerged work in In review, or in Ready to
    merge where that column exists. A card waiting on a person is neither."""
    assert _PENDING not in cycle._resting_statuses(None)
    assert _PENDING not in cycle._resting_statuses("Ready to merge")


def test_a_wake_leaves_a_card_parked_there_exactly_where_it_is(tmp_path):
    store = store_for(
        tmp_path,
        {"issues": {_KEY: {"status": _PENDING, "generation": "gen-1", "engine_labeled": True}}},
    )
    port = FakePort(snapshots={_KEY: CardSnapshot(status=_PENDING)})

    outcome = run_wake(store, port, now=_NOW)

    assert store.read()["issues"][_KEY]["status"] == _PENDING
    assert EffectKind.SET_STATUS not in port.kinds()
    assert outcome.directives == []


def test_a_parked_card_stops_costing_a_live_read_every_wake():
    """It is as idle as Needs More Info: nothing automatic will move it, so re-reading it from
    GitHub on every 30-second wake buys nothing."""
    rec = {"status": _PENDING, "last_live_check": _NOW.isoformat()}

    assert not cycle._live_check_due(rec, now=_NOW, terminal_recheck_s=900)
    # A queued control is still what wakes it — that is the "when specifically asked" path.
    assert cycle._live_check_due(rec, now=_NOW, terminal_recheck_s=900, queued_control=True)


# --- the column is optional, exactly like Backlog and Ready to merge ---

def test_the_role_is_optional_so_a_board_without_the_column_still_activates():
    assert Role.HUMAN_REVIEW_PENDING in config.OPTIONAL_ROLES
    assert Role.HUMAN_REVIEW_PENDING not in config.REQUIRED_ROLES
