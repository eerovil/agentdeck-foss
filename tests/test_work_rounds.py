"""Rounds of a reopened Work (#4585): the stored round, its rules and the facts split."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agentdeck import work_rounds
from agentdeck.db import Db, WorkRound
from agentdeck.work import WorkRef
from agentdeck.work_facts import Registration, pull_state, work_facts

WORK = "github_issue:eerovil/agentdeck#7"
NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


# --- the stored round ---


def test_a_round_joins_its_earlier_pull_requests_and_ages_only_when_they_grow(tmp_path):
    db = Db(tmp_path / "control.db")
    assert db.load_work_rounds() == {}
    assert db.start_work_round(WORK, ["Eerovil/Agentdeck#9"], work_rounds.REOPENED)
    first = db.load_work_rounds()[WORK]
    assert first.earlier == {"eerovil/agentdeck#9"}
    # A redelivered reopen, or a second message, adds nothing: the round keeps its start.
    assert not db.start_work_round(WORK, ["eerovil/agentdeck#9"], work_rounds.FOLLOW_UP)
    assert not db.start_work_round(WORK, [], work_rounds.FOLLOW_UP)
    assert db.load_work_rounds()[WORK] == first
    # The other writer's set is joined to the stored one, never put in its place.
    assert db.start_work_round(WORK, ["eerovil/agentdeck#12"], work_rounds.FOLLOW_UP)
    second = db.load_work_rounds([WORK])[WORK]
    assert second.earlier == {"eerovil/agentdeck#9", "eerovil/agentdeck#12"}
    assert second.started_at >= first.started_at
    assert db.load_work_rounds(["github_issue:eerovil/agentdeck#8"]) == {}
    assert db.load_work_rounds([]) == {}


def test_an_earlier_rounds_merge_no_longer_counts_its_work_as_done(tmp_path):
    """#4398's merged blocker counts only in the round it merged in."""
    db = Db(tmp_path / "control.db")
    assert db.note_merged_pull(WORK, "eerovil/agentdeck#9", "master")
    assert WORK in db.load_done_blockers()
    db.start_work_round(WORK, ["eerovil/agentdeck#9"], work_rounds.REOPENED)
    assert WORK not in db.load_done_blockers()
    # The new round's own merge counts again.
    assert db.note_merged_pull(WORK, "eerovil/agentdeck#12", "master")
    assert WORK in db.load_done_blockers()
    # An unread rounds table counts every merge, as before rounds, and loses nothing else.
    db.start_work_round(WORK, ["eerovil/agentdeck#12"], work_rounds.FOLLOW_UP)
    assert WORK not in db.load_done_blockers()

    def unread(*args):
        raise OSError("database is locked")

    db.load_work_rounds = unread
    assert WORK in db.load_done_blockers()


def test_a_round_that_cannot_be_read_or_written_starts_nothing():
    class Broken:
        def start_work_round(self, *args):
            raise OSError("database is locked")

        def load_work_rounds(self, *args):
            raise OSError("database is locked")

    assert not work_rounds.start(Broken(), WORK, ["eerovil/agentdeck#9"], work_rounds.REOPENED)
    assert work_rounds.load(Broken()) == {}
    assert work_rounds.load(None) == {}


# --- which pull requests a round leaves out ---


def test_only_a_pull_request_the_cache_shows_settled_goes_earlier():
    """An open pull request, or one the deck never resolved, stays in the new round."""
    rows = {
        ("eerovil/agentdeck", 9): {"state": "MERGED"},
        ("eerovil/agentdeck", 10): {"state": "closed"},
        ("eerovil/agentdeck", 11): {"state": "OPEN"},
        ("eerovil/agentdeck", 12): {},
    }
    asked = []

    def cached(refs):
        asked.append(refs)
        return rows

    pulls = [f"eerovil/AgentDeck#{n}" for n in (9, 10, 11, 12, 13)] + ["not a ref"]
    assert work_rounds.settled(pulls, cached, pull_state) == [
        "eerovil/agentdeck#10",
        "eerovil/agentdeck#9",
    ]
    assert work_rounds.settled([], cached, pull_state) == [] and len(asked) == 1


def test_a_round_filters_pull_requests_by_their_name_or_url():
    round_ = WorkRound(NOW, frozenset({"eerovil/agentdeck#9"}))
    assert work_rounds.current(["eerovil/AgentDeck#9", "eerovil/agentdeck#12"], round_) == (
        "eerovil/agentdeck#12",
    )
    assert work_rounds.current(["eerovil/agentdeck#9"], None) == ("eerovil/agentdeck#9",)
    assert work_rounds.is_earlier("https://github.com/eerovil/agentdeck/pull/9", round_)
    assert not work_rounds.is_earlier("https://github.com/eerovil/agentdeck/pull/90", round_)
    assert not work_rounds.is_earlier("https://github.com/eerovil/agentdeck/pull/9", None)


def test_a_new_round_drops_only_what_the_release_said_about_the_earlier_round():
    """Pinned (#4585): the merge, press and live-check keys lose their earlier pull requests;
    held and unrecorded verdicts, sent messages and the looks' own keys stay, so an older
    pass never merges for want of its hold."""
    assert work_rounds.RELEASE_ROUND_KEYS == {
        "live_check_asked",
        "merged_now",
        "merged_pulls",
        "asked",
        "asked_cleared",
        "press_waits",
        "unlanded",
    }
    old, new = "eerovil/agentdeck#9", "eerovil/agentdeck#12"
    kept = {
        "told": ["k"],
        "light_out": {new: 1},
        "unrecorded": {new: {"head": "abc"}},
        "held_passes": {new: {"head": "abc"}},
        "awaiting_merge": {new: "abc"},
    }
    entry = kept | {
        "merged_now": True,
        "merged_pulls": [old, new],
        "unlanded": {old: "a", new: "b"},
        "live_check_asked": {"at": 1.0, "pulls": [old]},
        "asked": {"at": 1.0, "pulls": {old: {"digest": "d"}}},
        "asked_cleared": {"pull": old},
        "press_waits": True,
    }
    round_ = WorkRound(NOW, frozenset({old}))
    assert work_rounds.drop_release_keys(entry, round_)
    assert entry == kept | {
        "merged_pulls": [new],
        "unlanded": {new: "b"},
        "round_seen": NOW.timestamp(),
    }
    # Once per round: what the release writes in the new round stays.
    entry["merged_now"] = True
    assert not work_rounds.drop_release_keys(entry, round_)
    assert entry["merged_now"] is True
    assert not work_rounds.drop_release_keys({"merged_now": True}, None)
    # A press or an ask that names a current pull request is the new round's own.
    current = {
        "asked": {"at": 1.0, "pulls": {old: {}, new: {}}},
        "press_waits": True,
        "live_check_asked": {"at": 1.0, "pulls": [new]},
        "merged_pulls": [old],
    }
    assert work_rounds.drop_release_keys(current, round_)
    assert set(current) == {"asked", "press_waits", "live_check_asked", "round_seen"}
    # A press from before #3320 kept its pulls at the top.
    assert work_rounds.drop_release_keys(legacy := {"asked": {new: "d"}}, round_)
    assert "asked" in legacy


# --- when a person's message starts a round ---


def _output(state="merged", merged_at=NOW):
    return SimpleNamespace(state=state, settled=state in {"merged", "closed"}, merged_at=merged_at)


@pytest.mark.parametrize(
    ("outputs", "kw", "due"),
    [
        # The round was merged and checked live after: the next message starts a round.
        ([_output()], {}, True),
        ([_output(), _output("closed", None)], {}, True),
        ([_output("closed", None)], {}, False),  # nothing merged
        ([_output(), _output("open", None)], {}, False),  # one still open
        ([_output("", None)], {}, False),  # unknown state
        ([], {}, False),
        ([_output()], {"asked_pending": True}, False),
        ([_output()], {"merged_now": True}, False),
        # The answer to a Waiting-for-a-try belongs to the round it was asked in.
        ([_output()], {"result": "needs_person"}, False),
        ([_output()], {"result": "not_live"}, False),
        ([_output()], {"result": None}, False),
        ([_output()], {"result": "failed"}, True),
        ([_output()], {"result": "nothing_to_check"}, True),
        ([_output()], {"checked_at": None}, False),
        # A result from before the newest merge is about an earlier merge.
        ([_output(), _output(merged_at=NOW + timedelta(hours=2))], {}, False),
        ([_output(merged_at=None)], {}, False),
        # A naive time reads as UTC.
        ([_output(merged_at=NOW.replace(tzinfo=None))], {}, True),
    ],
)
def test_a_message_starts_a_round_only_once_the_round_was_checked_live(outputs, kw, due):
    given = {
        "asked_pending": False,
        "merged_now": False,
        "result": "verified",
        "checked_at": NOW + timedelta(hours=1),
    } | kw
    assert work_rounds.follow_up_due(outputs, **given) is due


# --- the facts ---


def test_a_rounds_earlier_pull_requests_leave_the_works_outputs():
    ref = WorkRef("eerovil/agentdeck", 7)
    old = "https://github.com/eerovil/agentdeck/pull/9"
    new = "https://github.com/eerovil/agentdeck/pull/12"
    rows = {
        old: {"state": "MERGED", "mergedAt": "2026-11-01T10:00:00Z"},
        new: {"state": "OPEN", "headRefOid": "abc"},
    }
    registered = (Registration(old, True), Registration(new, True))
    round_ = WorkRound(NOW, frozenset({"eerovil/agentdeck#9"}))
    facts = work_facts(ref, rows=rows, registered=registered, round_=round_)
    assert [output.number for output in facts.outputs] == [12]
    assert [output.number for output in facts.earlier_outputs] == [9]
    assert facts.earlier_outputs[0].merged_at == datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert facts.round_started_at == NOW
    plain = work_facts(ref, rows=rows, registered=registered)
    assert [output.number for output in plain.outputs] == [9, 12]
    assert plain.earlier_outputs == () and plain.round_started_at is None


# --- what the Work page reads as the current round's ---


def test_an_earlier_rounds_live_check_ask_and_receipt_are_not_this_rounds():
    from agentdeck.web.work_view import _live_state, _this_round

    asked = {"live_check_asked": {"at": (NOW - timedelta(days=2)).timestamp()}}
    check = SimpleNamespace(result="verified", line="ok", checked_at=NOW - timedelta(days=1))
    app = SimpleNamespace(
        state=SimpleNamespace(
            work_release=SimpleNamespace(store=SimpleNamespace(get=lambda work_id: asked)),
            control_db=SimpleNamespace(work_live_check=lambda work_id: check),
        )
    )
    assert _live_state(app, WORK).result == "verified"
    assert _live_state(app, WORK, NOW).result == "" and not _live_state(app, WORK, NOW).pending
    # The round's own ask is pending: the earlier check does not answer it.
    asked["live_check_asked"]["at"] = (NOW + timedelta(minutes=1)).timestamp()
    assert _live_state(app, WORK, NOW).pending
    press = SimpleNamespace(reserved_at=(NOW - timedelta(hours=1)).isoformat())
    assert _this_round(press, None) is press
    assert _this_round(press, NOW) is None
    assert _this_round(SimpleNamespace(reserved_at=NOW.isoformat()), NOW) is not None


def test_a_run_that_tested_only_earlier_heads_is_not_the_rounds_verification():
    from agentdeck.work_overview import _this_round_runs
    from agentdeck.work_workspace import RunVerification

    def run(job_id, *heads):
        tested = tuple(("eerovil/agentdeck", SimpleNamespace(head=head)) for head in heads)
        return RunVerification(job_id, "run", "done", True, tested)

    earlier = (SimpleNamespace(head="a" * 40),)
    old, new, both = run("old", "a" * 12), run("new", "b" * 40), run("both", "a" * 40, "c" * 40)
    assert _this_round_runs([old, new, both], earlier) == [new, both]
    assert _this_round_runs([old], ()) == [old]


def test_an_earlier_rounds_pre_review_hold_is_no_wait_of_this_one(tmp_path, monkeypatch):
    from agentdeck import work_router
    from agentdeck.web.work_view import _pre_review_waits

    db = Db(tmp_path / "control.db")
    pulls = ("eerovil/agentdeck#9", "eerovil/agentdeck#12")
    monkeypatch.setattr(work_router, "work_pulls", lambda state, work_id: pulls)
    app = SimpleNamespace(
        state=SimpleNamespace(
            control_db=db, work_router=SimpleNamespace(pre_review_held=lambda pull: True)
        )
    )
    assert "#9, eerovil/agentdeck#12" in _pre_review_waits(app, None, WORK)
    db.start_work_round(WORK, ["eerovil/agentdeck#9"], work_rounds.REOPENED)
    assert "on eerovil/agentdeck#12." in _pre_review_waits(app, None, WORK)
    assert "#9" not in _pre_review_waits(app, None, WORK)
