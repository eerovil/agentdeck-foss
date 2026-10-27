"""The policy that decides whether a reportless ending is restarted or handed over (#802, #835)."""

from __future__ import annotations

import pytest

from agentdeck.poller_engine import revive
from agentdeck.poller_engine.revive import ReviveDecision as D


def decide(rec=None, *, observed="ended", dispatch_ok=True):
    return revive.decide(
        rec if rec is not None else {},
        observed=observed,
        dispatch_ok=dispatch_ok,
    )


# --- which endings are restarted -------------------------------------------


@pytest.mark.parametrize(
    "observed",
    [
        "died",       # OOM, kill -9, a crashed CLI
        "ended",      # the turn is over, whatever the CLI called it
        "abandoned",  # wedged for hours, then stopped by the Runtime (#1400, #2159)
        "stopped",    # the Runtime stopped it and nothing on this card asked it to (#2159)
    ],
)
def test_every_proven_ending_is_restarted(observed):
    """#835: a turn that reached its own end without reporting is restarted too. Its silence
    is still not success, but a second worker can go and look at what it left, and a human
    having to do that looking was the whole failure."""
    assert decide(observed=observed) is D.REVIVE


def test_an_absent_worker_is_reported_and_nothing_else():
    """A runtime restart makes every in-flight card `gone` at once, and that proves nothing
    finished — only that nothing is running."""
    assert decide(observed="gone") is D.REPORT
    assert decide(observed="whatever-comes-next") is D.REPORT


# --- what the ending is called ---------------------------------------------


@pytest.mark.parametrize(
    "observed,subtype",
    [
        ("died", ""),                       # nothing chose to stop
        ("died", "success"),                # a stale subtype from an earlier turn
        ("abandoned", ""),                  # it stopped mid-turn and never moved again
        ("abandoned", "success"),           # likewise a subtype belonging to an earlier turn
        ("stopped", ""),                    # the Runtime ended it; the turn did not finish
        ("ended", "error_during_execution"),
        ("ended", "error_usage_limit"),
        ("ended", "failed"),       # Codex's word for a turn that went wrong
        ("ended", "interrupted"),  # and for one that was stopped part-way
        ("ended", "cancelled"),    # Cursor's
    ],
)
def test_a_killed_worker_is_named_as_one(observed, subtype):
    assert revive.involuntary(observed=observed, subtype=subtype)


@pytest.mark.parametrize(
    "observed,subtype",
    [
        ("ended", "success"),    # Claude Code and Cursor for "it reached its own end"
        ("ended", "completed"),  # Codex's word for exactly the same thing (#1781)
        ("ended", "unknown"),    # Codex when the completion event carried no status
        ("ended", ""),           # no subtype is not evidence of an error
    ],
)
def test_a_turn_that_finished_on_its_own_terms_is_not_called_a_kill(observed, subtype):
    """It is revived all the same; this only decides what the card is told happened."""
    assert not revive.involuntary(observed=observed, subtype=subtype)
    assert decide(observed=observed) is D.REVIVE


# --- the bounds -------------------------------------------------------------


def test_a_card_that_keeps_ending_silently_eventually_reaches_a_human():
    assert decide({"revives": revive.MAX_REVIVES - 1}) is D.REVIVE
    assert decide({"revives": revive.MAX_REVIVES}) is D.PARK


def test_a_legacy_false_budget_flag_does_not_defer_a_revive():
    assert decide(dispatch_ok=False) is D.REVIVE


def test_the_count_survives_a_revive_and_is_forgotten_by_new_work():
    rec: dict = {}
    assert revive.record(rec) == 1
    assert revive.record(rec) == 2
    assert revive.attempts(rec) == 2

    revive.reset(rec)
    assert revive.attempts(rec) == 0


def test_a_nonsense_count_reads_as_none_rather_than_crashing():
    assert revive.attempts({"revives": "two"}) == 0
    assert revive.attempts({"revives": -1}) == 0


def test_a_refused_turn_is_not_an_attempt_and_spends_nothing():
    """A turn the provider refused before it ran is handed out again whatever the count
    (#1519, #2159). There is one Run-attempt count and a refusal does not touch it."""
    spent = {"revives": revive.MAX_REVIVES}
    assert revive.decide(spent, observed="ended", dispatch_ok=True, refused=True) is D.REVIVE
    assert revive.attempts(spent) == revive.MAX_REVIVES
    assert not hasattr(revive, "MAX_REFUSALS")


# --- what a Runtime execution outcome proves (#2200) ---------------------------


def _outcome(state: str, reason: str = "", **extra) -> dict:
    return {
        "execution_state": state,
        "reason_code": reason,
        "provider": "claude_code",
        "account": "main",
        **extra,
    }


def test_the_runtimes_ending_carries_the_clis_own_word_for_it():
    assert revive.observation(_outcome("ended", "error_during_execution")) == {
        "observed": "ended",
        "subtype": "error_during_execution",
        "refused": "",
        "account": "claude_code:main",
        "background_tasks": [],
        "ended_at": None,
        "session_key": "",
    }
    # The Runtime's bookkeeping reasons are not the CLI's word for anything.
    for reason in ("result", "worker_released", "no_turn_observed"):
        assert revive.observation(_outcome("ended", reason))["subtype"] == "", reason
    # A death carries no subtype: `died` comes straight from the Runtime (#2159 test 6).
    assert revive.observation(_outcome("died", "process_gone"))["subtype"] == ""


def test_a_turn_the_provider_refused_is_a_refusal():
    """The engine spends no attempt on a turn that never ran (#1519, #2159)."""
    seen = revive.observation(_outcome("ended", "refused:usage_limit"))
    assert (seen["observed"], seen["subtype"], seen["refused"]) == (
        "ended", "refused", "usage_limit"
    )


def test_an_account_blocked_when_the_route_was_frozen_counts_for_a_killed_turn_only():
    """#1519: the Runtime stamps it at delivery; a turn that reached its own end ran."""
    blocked = {"refused_at_delivery": "seven_day at 100%"}
    assert revive.observation(_outcome("died", "process_gone", **blocked))["refused"] == (
        "seven_day at 100%"
    )
    assert revive.observation(_outcome("ended", "success", **blocked))["refused"] == ""
    assert revive.observation(_outcome("ended", "result", **blocked))["refused"] == ""


@pytest.mark.parametrize("state", ["running", "waiting_human", "uncertain", "abandoned", "new"])
def test_unknown_and_unstopped_states_are_never_endings(state):
    """Unknown is not stopped, and a wedged Run is not over until the Runtime ended it."""
    assert revive.observation(_outcome(state, "runtime_restarted")) is None


def test_an_unreadable_outcome_is_no_ending():
    for answer in (None, [], "ended", {}):
        assert revive.observation(answer) is None


def test_a_stopped_wedge_reads_as_abandoned_and_other_stops_as_stopped():
    assert revive.observation(_outcome("stopped", "abandoned"))["observed"] == "abandoned"
    for reason in ("poller_stop", "superseded_by:gen-2", ""):
        assert revive.observation(_outcome("stopped", reason))["observed"] == "stopped"


def test_an_execution_the_runtime_never_started_is_reported_not_ended():
    seen = revive.observation(_outcome("not_started", "no_execution"))
    assert seen["observed"] == "gone"
    assert "gone" not in revive.ENDINGS


def test_an_ended_turn_names_the_chat_it_ended_in():
    """#2902: the chat a job naming no Run would wake, so that job can still hold the card."""
    seen = revive.observation(_outcome("ended", "success", session_id="s1"))
    assert seen["session_key"] == "claude_code:main:s1"
    assert revive.observation(_outcome("ended", "success"))["session_key"] == ""


def test_an_ended_turn_carries_the_background_commands_it_left_running():
    """#2890: the chat is waiting for them to wake it, so the ending is a pause."""
    seen = revive.observation(
        _outcome(
            "ended", "success", background_tasks=["b2", "b1", ""], terminal_result_at=1000.0
        )
    )
    assert (seen["background_tasks"], seen["ended_at"]) == (["b1", "b2"], 1000.0)
    assert revive.background_wait(seen, now=1000.0 + 60) == ["b1", "b2"]
    # Not forever: a command that never finishes stops holding the card.
    assert revive.background_wait(seen, now=1000.0 + revive.BACKGROUND_HOLD_S) == []


def test_only_a_clean_recent_ending_waits_on_background_commands():
    def wait(outcome):
        seen = revive.observation(outcome)
        return revive.background_wait(seen, now=1060.0) if seen else []

    tasks = {"background_tasks": ["b1"], "terminal_result_at": 1000.0}
    assert wait(_outcome("ended", "success", **tasks)) == ["b1"]
    assert wait(_outcome("died", "process_gone", **tasks)) == []
    assert wait(_outcome("ended", "refused:usage_limit", **tasks)) == []
    # No readable ending time, or no list at all, proves no recent wait.
    assert wait(_outcome("ended", "success", background_tasks=["b1"])) == []
    no_time = _outcome("ended", "success", terminal_result_at=True, background_tasks=["b1"])
    assert wait(no_time) == []
    no_list = _outcome("ended", "success", background_tasks="b1", terminal_result_at=1000.0)
    assert wait(no_list) == []
