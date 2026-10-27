"""A missing ownership label against a person's instructions (#2417).

On Northwind/issues#478 a `/claude` follow-up posted right after a worker finished was
started and then stopped on the very next wake: the release in the same wake took the label off,
the new dispatch copied "labelled" from the wake's earlier live read, and the next wake read the
missing label as the person handing the card back. These pin the rules that replace that: the
engine's own removal never becomes the new worker's claim, the newest of a removal and an
instruction wins, a hand-back nobody asked for says so, and a person's instruction reopens the
closed issue it is about.
"""

from __future__ import annotations

from datetime import timedelta

from poller_engine_fakes import NOW as _NOW
from poller_engine_fakes import FakePort, comment, run_wake, store_for

from agentdeck.poller_engine import cycle
from agentdeck.poller_engine.ownership import LabelRemoval
from agentdeck.poller_engine.ports import EffectKind
from agentdeck.poller_engine.reconcile import CardSnapshot

_LEDGER = {"ownership": {"initialized": True, "events": {}}}
_BOT = "claude-bot"  # the fixture policy's own account


def _instruction(cid: str, *, at: str, status: str = "processing", **extra) -> dict:
    return {
        "id": cid,
        "repo": "store",
        "number": 5,
        "kind": "claude",
        "payload": "fix it",
        "body": "/claude fix it",
        "url": f"https://github.com/org/store/issues/5#issuecomment-{cid}",
        "author": "eero",
        "created_at": at,
        "status": status,
        **extra,
    }


def _running(*, processing=("1",), **extra) -> dict:
    """A started card that an earlier wake saw wearing the ownership label."""
    return {
        "status": "In progress",
        "generation": "gen1",
        "started": True,
        "engine_claim": True,
        "label_claim": True,
        "processing": list(processing),
        **extra,
    }


def _unlabelled(**extra) -> CardSnapshot:
    return CardSnapshot(status="In progress", owned=False, quiet_seconds=1, **extra)


def _closed(*, status: str = "In progress", at: str = "2026-07-25T11:30:00Z") -> CardSnapshot:
    """Closed before the fixture's comments (11:59:00) were written, unless ``at`` says not."""
    return CardSnapshot(status=status, open=False, owned=False, closed_at=at)


def _comments_posted(port: FakePort) -> list[str]:
    return [str(effect.value) for effect in port.applied if effect.kind is EffectKind.COMMENT]


# --- a release leaves the label on, so the next worker is not handed back ---

def test_a_card_released_and_started_again_in_one_wake_keeps_its_label(tmp_path):
    """The #478 sequence: a finished card still wearing the label is released, and a `/claude`
    written after it starts the card again in the same wake. The release leaves the label on
    (#2497), so the new worker's claim is the label that is really there."""
    port = FakePort(
        comments=[
            comment("c1", "/claude fix the bot", created="2026-07-25T11:59:30Z"),
        ],
        snapshots={"store#5": CardSnapshot(status="Done", owned=True)},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Done", "started": True, "label_claim": True}},
        **_LEDGER,
    })
    outcome = run_wake(store, port, now=_NOW)

    assert port.ownership_edits() == [], "the release must leave the label on"
    assert len(outcome.directives) == 1, outcome.log

    # The next wake sees the issue as the release left it: still labelled. The worker keeps going.
    ack = outcome.directives[0]["dispatch_id"]
    port.acks = {ack}
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    rec = store.read()["issues"]["store#5"]
    assert not rec.get("stopping"), "the new worker was stopped as a hand-back"
    assert rec["status"] == "In progress"
    assert store.read()["controls"]["c1"]["status"] != "cancelled"


# --- the newest of a removal and an instruction wins ---

def test_a_person_removing_the_label_after_the_last_instruction_still_stops_it_quietly(
    tmp_path,
):
    port = FakePort(
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval("2026-07-25T11:59:50Z", "eero")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z")},
        **_LEDGER,
    })
    run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert rec["stopping"] is True
    assert "loud_hand_back" not in rec, "a person's own stop must stay quiet"
    assert _comments_posted(port) == []


def test_an_instruction_written_after_the_removal_keeps_the_card(tmp_path):
    port = FakePort(
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval("2026-07-25T11:40:00Z", "eero")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z")},
        **_LEDGER,
    })
    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert not rec.get("stopping"), outcome.log
    assert "label_claim" not in rec
    assert port.removal_reads == ["store#5"]


def test_an_unreadable_removal_holds_the_hand_back_rather_than_guessing(tmp_path):
    port = FakePort(snapshots={"store#5": _unlabelled()}, removals={"store#5": None})
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z")},
        **_LEDGER,
    })
    outcome = run_wake(store, port, now=_NOW)

    rec = store.read()["issues"]["store#5"]
    assert not rec.get("stopping")
    assert rec["label_claim"] is True, "the claim must survive to be decided later"
    assert any("could not be read" in line for line in outcome.log)


def test_a_hand_back_with_no_instruction_on_the_card_costs_no_read(tmp_path):
    """A label-started card a person takes back is the everyday case, and stays free."""
    port = FakePort(snapshots={"store#5": _unlabelled()})
    store = store_for(tmp_path, {"issues": {"store#5": _running(processing=())}, **_LEDGER})
    run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["stopping"] is True
    assert port.removal_reads == []


def test_an_instruction_written_before_the_removal_is_not_restarted_after_it(tmp_path):
    """Both land between two wakes: the removal is newer, so the older comment goes too."""
    port = FakePort(
        comments=[comment("7", "/claude also this", created="2026-07-25T11:59:00Z")],
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval("2026-07-25T11:59:30Z", "eero")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": _running(processing=())}, **_LEDGER})
    outcome = run_wake(store, port, now=_NOW)

    state = store.read()
    rec = state["issues"]["store#5"]
    assert rec["stopping"] is True
    assert not rec.get("restart_after_stop"), "the older instruction restarted the card"
    assert state["controls"]["7"]["status"] == "cancelled"
    assert outcome.directives == []


def test_an_instruction_written_after_the_removal_in_the_same_window_wins(tmp_path):
    port = FakePort(
        comments=[comment("7", "/claude also this", created="2026-07-25T11:59:40Z")],
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval("2026-07-25T11:59:30Z", "eero")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": _running(processing=())}, **_LEDGER})
    run_wake(store, port, now=_NOW)

    state = store.read()
    assert not state["issues"]["store#5"].get("stopping")
    assert state["controls"]["7"]["status"] != "cancelled"


# --- a hand-back nobody asked for is loud ---

def test_a_removal_by_the_engines_own_account_parks_the_card_and_says_why(tmp_path):
    port = FakePort(
        snapshots={"store#5": _unlabelled(execution_stopped=True)},
        removals={"store#5": LabelRemoval("2026-07-25T11:59:50Z", "Claude-Bot")},
        refuse={(EffectKind.SET_STATUS, "Needs More Info")},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z")},
        **_LEDGER,
    })
    run_wake(store, port, now=_NOW)

    notices = _comments_posted(port)
    assert len(notices) == 1, notices
    assert "#issuecomment-1" in notices[0] and "/claude" in notices[0]
    assert store.read()["issues"]["store#5"]["stopping"] is True, "a refused park is retried"

    # The retry lands the park without a second notice.
    port.refuse = set()
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    assert len(_comments_posted(port)) == 1
    statuses = [e.value for e in port.applied if e.kind is EffectKind.SET_STATUS]
    assert statuses[-1] == "Needs More Info"
    assert "store#5" not in store.read()["issues"]
    assert store.read()["controls"]["1"]["status"] == "cancelled"


def test_an_idle_card_handed_back_by_the_engines_own_account_is_parked_too(tmp_path):
    port = FakePort(
        snapshots={"store#5": CardSnapshot(status="Todo", owned=False)},
        removals={"store#5": LabelRemoval("2026-07-25T11:59:50Z", _BOT)},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": {
            "status": "Todo", "started": True, "label_claim": True, "pending": ["1"],
        }},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z", status="queued")},
        **_LEDGER,
    })
    outcome = run_wake(store, port, now=_NOW)

    assert len(_comments_posted(port)) == 1
    statuses = [e.value for e in port.applied if e.kind is EffectKind.SET_STATUS]
    assert statuses == ["Needs More Info"], outcome.log
    assert store.read()["controls"]["1"]["status"] == "cancelled"


# --- a person's instruction reopens the closed issue it is about ---

def test_a_claude_comment_on_a_closed_issue_reopens_it_before_starting(tmp_path):
    port = FakePort(
        comments=[comment("c1", "/claude one more thing")],
        snapshots={"store#5": _closed(status="Done")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Done"}}, **_LEDGER})
    outcome = run_wake(store, port, now=_NOW)

    assert len(outcome.directives) == 1, outcome.log
    kinds = port.kinds()
    assert EffectKind.REOPEN_ISSUE in kinds
    in_progress = next(
        i for i, e in enumerate(port.applied)
        if e.kind is EffectKind.SET_STATUS and e.value == "In progress"
    )
    assert kinds.index(EffectKind.REOPEN_ISSUE) < in_progress


def test_a_card_first_seen_through_its_comment_is_reopened_on_its_first_read(tmp_path):
    """No live read existed when it was dispatched; the first one settles it, once."""
    port = FakePort(comments=[comment("c1", "/claude one more thing")])
    store = store_for(tmp_path, dict(_LEDGER))
    outcome = run_wake(store, port, now=_NOW)
    assert len(outcome.directives) == 1, outcome.log
    assert store.read()["issues"]["store#5"]["reopen_if_closed"] == "2026-07-25T11:59:00Z"

    port.comments = []
    port.snapshots["store#5"] = _closed()
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    assert port.kinds().count(EffectKind.REOPEN_ISSUE) == 1
    rec = store.read()["issues"]["store#5"]
    assert "reopen_if_closed" not in rec
    assert rec["status"] == "In progress", "the reopened card must not be cleaned up as closed"


def test_the_first_read_spends_the_reopen_marker_on_an_open_issue(tmp_path):
    """So an issue a merged pull request closes later is not reopened by it."""
    port = FakePort(comments=[comment("c1", "/claude one more thing")])
    store = store_for(tmp_path, dict(_LEDGER))
    run_wake(store, port, now=_NOW)

    port.comments = []
    port.snapshots["store#5"] = CardSnapshot(status="In progress", owned=False)
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    assert "reopen_if_closed" not in store.read()["issues"]["store#5"]
    assert EffectKind.REOPEN_ISSUE not in port.kinds()


def test_only_a_persons_comment_counts_as_their_instruction():
    person = _instruction("12", at="2026-07-25T11:50:00Z")
    assert cycle._person_instruction(person)
    assert not cycle._person_instruction({**person, "id": "engine:release-bits:rebase:x"})
    assert not cycle._person_instruction({**person, "app": True})
    assert not cycle._person_instruction({**person, "kind": "stop"})


def test_a_closed_issue_held_by_the_worker_cap_is_reopened_not_cleaned_up(tmp_path):
    """The instruction cannot start this wake; the next read must not cancel it as closed.

    The issue rests in Todo (closed as not planned, say). A Done card held by the cap is
    released on its next read whether it is open or closed — that is its own defect.
    """
    port = FakePort(
        comments=[comment("c1", "/claude one more thing")],
        snapshots={
            "store#5": _closed(status="Todo"),
            "store#9": CardSnapshot(status="In progress", owned=False, quiet_seconds=1),
        },
    )
    store = store_for(tmp_path, {
        "issues": {
            "store#5": {"status": "Todo"},
            "store#9": {"status": "In progress", "generation": "g9", "started": True,
                        "engine_claim": True},
        },
        **_LEDGER,
    })
    outcome = run_wake(store, port, now=_NOW, cap=1)
    assert outcome.directives == [], "the cap should have held the new card"

    port.comments = []
    run_wake(store, port, now=_NOW + timedelta(seconds=40), cap=1)

    assert EffectKind.REOPEN_ISSUE in port.kinds()
    assert store.read()["controls"]["c1"]["status"] == "queued"


def test_a_card_waiting_for_its_first_start_is_not_weighed_at_all(tmp_path):
    """#1663 keeps such a card whatever the label says, so there is nothing to read."""
    port = FakePort(snapshots={"store#5": CardSnapshot(status="Todo", owned=False)})
    store = store_for(tmp_path, {
        "issues": {"store#5": {"status": "Todo", "label_claim": True, "pending": ["1"]}},
        "controls": {"1": _instruction("1", at="2026-07-25T11:50:00Z", status="queued")},
        **_LEDGER,
    })
    run_wake(store, port, now=_NOW, cap=0)

    assert port.removal_reads == []
    assert "loud_hand_back" not in store.read()["issues"]["store#5"]


def test_an_issue_closed_after_the_instruction_is_not_reopened(tmp_path):
    """Closing it later is the newer word: the card is cleaned up as before."""
    port = FakePort(comments=[comment("c1", "/claude one more thing")])
    store = store_for(tmp_path, dict(_LEDGER))
    run_wake(store, port, now=_NOW)

    port.comments = []
    port.snapshots["store#5"] = _closed(at="2026-07-25T11:59:30Z")
    run_wake(store, port, now=_NOW + timedelta(seconds=40))

    assert EffectKind.REOPEN_ISSUE not in port.kinds()
    assert "store#5" not in store.read()["issues"]


def test_close_with_comment_in_the_same_second_is_a_close_not_a_request_to_reopen(tmp_path):
    port = FakePort(
        comments=[comment("c1", "/claude one more thing")],
        snapshots={"store#5": _closed(status="Done", at="2026-07-25T11:59:00Z")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": {"status": "Done"}}, **_LEDGER})
    run_wake(store, port, now=_NOW)

    assert EffectKind.REOPEN_ISSUE not in port.kinds()


# --- a removal and a comment stamped in the same second ---

_SAME_SECOND = "2026-07-25T11:50:00Z"


def test_a_comment_the_timeline_lists_after_a_same_second_removal_keeps_the_card(tmp_path):
    port = FakePort(
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval(
            _SAME_SECOND, "eero", comments_after=frozenset({"1"}),
        )},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at=_SAME_SECOND)},
        **_LEDGER,
    })
    run_wake(store, port, now=_NOW)

    assert not store.read()["issues"]["store#5"].get("stopping")


def test_a_comment_the_timeline_lists_before_a_same_second_removal_is_handed_back(tmp_path):
    port = FakePort(
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval(
            _SAME_SECOND, "eero", comments_before=frozenset({"1"}),
        )},
    )
    store = store_for(tmp_path, {
        "issues": {"store#5": _running()},
        "controls": {"1": _instruction("1", at=_SAME_SECOND)},
        **_LEDGER,
    })
    run_wake(store, port, now=_NOW)

    assert store.read()["issues"]["store#5"]["stopping"] is True


def test_a_same_second_comment_arriving_in_the_hand_back_wake_follows_the_timeline(tmp_path):
    """The same ordering decides the instruction read in the wake that hands the card back."""
    port = FakePort(
        comments=[
            comment("7", "/claude before", created="2026-07-25T11:59:30Z"),
            comment("8", "/claude after", created="2026-07-25T11:59:30Z"),
        ],
        snapshots={"store#5": _unlabelled()},
        removals={"store#5": LabelRemoval(
            "2026-07-25T11:59:30Z", "eero",
            comments_before=frozenset({"7"}), comments_after=frozenset({"8"}),
        )},
    )
    store = store_for(tmp_path, {"issues": {"store#5": _running(processing=())}, **_LEDGER})
    run_wake(store, port, now=_NOW)

    state = store.read()
    assert not state["issues"]["store#5"].get("stopping"), "the later comment keeps the card"
    assert state["controls"]["8"]["status"] != "cancelled"


def test_a_stamp_tie_outside_the_timeline_page_goes_to_the_removal():
    removal = LabelRemoval(_SAME_SECOND, "eero")
    assert not cycle._written_after(_instruction("1", at=_SAME_SECOND), removal)
    assert cycle._written_after(_instruction("1", at="2026-07-25T11:50:01Z"), removal)
    assert cycle._written_after(_instruction("1", at=_SAME_SECOND), LabelRemoval())


# --- a card its worker closed with `resolved` waits for that worker's `reopen` (#2522) ---

def _held(**extra) -> dict:
    return {
        "status": "Done",
        "generation": "gen1",
        "resolved_finish": {"generation": "gen1", "at": _NOW.isoformat()},
        **extra,
    }


def test_a_wake_keeps_a_card_its_own_resolved_closed_inside_the_hold(tmp_path):
    port = FakePort(snapshots={"store#5": _closed(status="Done")})
    store = store_for(tmp_path, {"issues": {"store#5": _held()}, **_LEDGER})

    run_wake(store, port, now=_NOW + timedelta(minutes=5))

    assert store.read()["issues"]["store#5"]["status"] == "Done"
    assert EffectKind.REOPEN_ISSUE not in port.kinds()

    run_wake(store, port, now=_NOW + timedelta(hours=25))

    assert "store#5" not in store.read()["issues"]


def test_a_person_s_instruction_on_a_held_card_starts_a_new_run_and_ends_the_hold(tmp_path):
    port = FakePort(
        comments=[comment("c1", "/claude one more thing")],
        snapshots={"store#5": _closed(status="Done")},
    )
    store = store_for(tmp_path, {"issues": {"store#5": _held()}, **_LEDGER})

    outcome = run_wake(store, port, now=_NOW + timedelta(minutes=5))

    assert len(outcome.directives) == 1, outcome.log
    rec = store.read()["issues"]["store#5"]
    assert rec["generation"] != "gen1"
    assert "resolved_finish" not in rec
