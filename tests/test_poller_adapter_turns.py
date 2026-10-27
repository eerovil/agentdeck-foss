"""Asking the persistent Runtime to stop the Runs the engine waits on (#279, #2159, #2200).

The host no longer works out whether a worker is alive, and no longer writes down how a Run
ended: the engine reads that from the Runtime itself. What is left here is the stop. These tests
pin who is asked, and that no answer short of the Runtime's proof is reported as a stop.
"""

from __future__ import annotations

from agentdeck.poller_adapter import turns

_WORK = "github_issue:eerovil/agentdeck#279"


def _stopping(**overrides) -> dict:
    record = {
        "status": "In progress",
        "stopping": True,
        "stop_waiting_generation": "gen-1",
        "stop_requested_at": "2026-07-27T13:00:00+00:00",
    }
    record.update(overrides)
    return record


def _stop_runs(answer, *, issues=None, asked=None):
    def stop(socket, work, run_id, reason, **_kwargs):
        if asked is not None:
            asked.append((socket, work, run_id, reason))
        return answer

    return turns.stop_runs(
        socket="/run/rt.sock",
        owner="eerovil",
        issues=issues if issues is not None else {"agentdeck#279": _stopping()},
        stop=stop,
    )


def test_a_stop_asks_the_runtime_to_stop_the_invalidated_run():
    """#2159 test 1: the Poller superseded the Run; the Runtime stops the worker."""
    asked: list = []

    lines = _stop_runs({"result": "stopped", "execution": {"account": "main"}}, asked=asked)

    assert asked == [("/run/rt.sock", _WORK, "gen-1", "poller_stop")]
    assert lines == ["agentdeck#279: runtime stopped Run gen-1"]


def test_a_dead_worker_is_proved_stopped_without_any_stop_ack():
    """#2159 test 2: a worker that died answers `already_stopped`; nobody runs a command."""
    assert _stop_runs({"result": "already_stopped"}) == [
        "agentdeck#279: runtime already stopped Run gen-1"
    ]


def test_an_unproved_stop_is_said_to_be_still_waiting():
    for answer in ({"result": "still_stopping"}, {"result": "uncertain"}, None):
        (line,) = _stop_runs(answer)
        assert "not yet proved" in line


def test_the_stop_is_asked_again_until_the_engine_releases_the_card():
    """Nothing is written here any more, so the Runtime's record is the only memory. Asking a
    stopped Run again is safe: it answers ``already_stopped``."""
    asked: list = []
    _stop_runs({"result": "stopped"}, asked=asked)
    _stop_runs({"result": "already_stopped"}, asked=asked)

    assert len(asked) == 2


def test_only_stopping_cards_with_a_run_to_stop_are_asked_about():
    asked: list = []
    issues = {
        "agentdeck#1": {"status": "In progress", "generation": "g"},
        "agentdeck#2": _stopping(stop_waiting_generation=""),
        "agentdeck#3": _stopping(stop_requested_at=""),
        "not-a-card": _stopping(),
    }

    assert _stop_runs({"result": "stopped"}, issues=issues, asked=asked) == []
    assert asked == []


def test_the_host_keeps_no_turn_receipts():
    """#2200: the watch and turn-end receipts are gone; nothing here can write them."""
    for name in ("sweep", "record_watch", "record_end", "watch_dir", "turn_end_dir"):
        assert not hasattr(turns, name), name
